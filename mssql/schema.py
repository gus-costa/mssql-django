# Copyright (c) Microsoft Corporation.
# Licensed under the BSD license.

import binascii
import datetime

from django.db.backends.base.schema import (
    BaseDatabaseSchemaEditor,
    _is_relevant_relation,
    _related_non_m2m_objects,
    logger,
)
from django.db.backends.ddl_references import (
    Columns,
    IndexName,
    Statement as DjStatement,
    Table,
)
from django import VERSION as django_version
from django.db.models import Index, UniqueConstraint
from django.db.models.fields import AutoField, BigAutoField
from django.db.models.sql.where import AND
from django.db.transaction import TransactionManagementError
from django.utils.encoding import force_str


class Statement(DjStatement):
    def __hash__(self):
        return hash((self.template, str(self.parts['name'])))

    def __eq__(self, other):
        return self.template == other.template and str(self.parts['name']) == str(other.parts['name'])


class DatabaseSchemaEditor(BaseDatabaseSchemaEditor):

    _sql_check_constraint = " CONSTRAINT %(name)s CHECK (%(check)s)"
    _sql_select_default_constraint_name = "SELECT" \
                                          " d.name " \
                                          "FROM sys.default_constraints d " \
                                          "INNER JOIN sys.tables t ON" \
                                          " d.parent_object_id = t.object_id " \
                                          "INNER JOIN sys.columns c ON" \
                                          " d.parent_object_id = c.object_id AND" \
                                          " d.parent_column_id = c.column_id " \
                                          "INNER JOIN sys.schemas s ON" \
                                          " t.schema_id = s.schema_id " \
                                          "WHERE" \
                                          " t.name = %(table)s AND" \
                                          " c.name = %(column)s"
    sql_alter_column_default = "ADD DEFAULT %(default)s FOR %(column)s"
    sql_alter_column_no_default = "DROP CONSTRAINT %(column)s"
    sql_alter_column_not_null = "ALTER COLUMN %(column)s %(type)s NOT NULL"
    sql_alter_column_null = "ALTER COLUMN %(column)s %(type)s NULL"
    sql_alter_column_type = "ALTER COLUMN %(column)s %(type)s"
    sql_create_column = "ALTER TABLE %(table)s ADD %(column)s %(definition)s"
    sql_delete_column = "ALTER TABLE %(table)s DROP COLUMN %(column)s"
    sql_delete_index = "DROP INDEX %(name)s ON %(table)s"
    sql_delete_table = """
        DECLARE @sql_froeign_constraint_name nvarchar(128)
        DECLARE @sql_drop_constraint nvarchar(300)
        WHILE EXISTS(SELECT 1
            FROM sys.foreign_keys
            WHERE referenced_object_id = object_id('%(table)s'))
        BEGIN
            SELECT TOP 1 @sql_froeign_constraint_name = name
            FROM sys.foreign_keys
            WHERE referenced_object_id = object_id('%(table)s')
            SELECT
            @sql_drop_constraint = 'ALTER TABLE [' + OBJECT_NAME(parent_object_id) + '] ' +
            'DROP CONSTRAINT [' + @sql_froeign_constraint_name + '] '
            FROM sys.foreign_keys
            WHERE referenced_object_id = object_id('%(table)s')
            exec sp_executesql @sql_drop_constraint
        END
        DROP TABLE %(table)s
"""
    sql_rename_column = "EXEC sp_rename '%(table)s.%(old_column)s', %(new_column)s, 'COLUMN'"
    sql_rename_table = "EXEC sp_rename %(old_table)s, %(new_table)s"
    sql_create_unique_null = "CREATE UNIQUE INDEX %(name)s ON %(table)s(%(columns)s) " \
                             "WHERE %(columns)s IS NOT NULL"

    def _alter_column_default_sql(self, model, old_field, new_field, drop=False):
        """
        Hook to specialize column default alteration.

        Return a (sql, params) fragment to add or drop (depending on the drop
        argument) a default to new_field's column.
        """
        new_default = self.effective_default(new_field)
        default = '%s'
        params = [new_default]
        column = self.quote_name(new_field.column)

        if drop:
            params = []
            # SQL Server requires the name of the default constraint
            result = self.execute(
                self._sql_select_default_constraint_name % {
                    "table": self.quote_value(model._meta.db_table),
                    "column": self.quote_value(new_field.column),
                },
                has_result=True
            )
            if result:
                for row in result:
                    column = self.quote_name(next(iter(row)))
        elif self.connection.features.requires_literal_defaults:
            # Some databases (Oracle) can't take defaults as a parameter
            # If this is the case, the SchemaEditor for that database should
            # implement prepare_default().
            default = self.prepare_default(new_default)
            params = []

        new_db_params = new_field.db_parameters(connection=self.connection)
        sql = self.sql_alter_column_no_default if drop else self.sql_alter_column_default
        return (
            sql % {
                'column': column,
                'type': new_db_params['type'],
                'default': default,
            },
            params,
        )

    def _alter_column_null_sql(self, model, old_field, new_field):
        """
        Hook to specialize column null alteration.

        Return a (sql, params) fragment to set a column to null or non-null
        as required by new_field, or None if no changes are required.
        """
        if (self.connection.features.interprets_empty_strings_as_nulls and
                new_field.get_internal_type() in ("CharField", "TextField")):
            # The field is nullable in the database anyway, leave it alone.
            return
        else:
            new_db_params = new_field.db_parameters(connection=self.connection)
            sql = self.sql_alter_column_null if new_field.null else self.sql_alter_column_not_null
            return (
                sql % {
                    'column': self.quote_name(new_field.column),
                    'type': new_db_params['type'],
                },
                [],
            )

    def _alter_column_type_sql(self, model, old_field, new_field, new_type):
        new_type = self._set_field_new_type_null_status(old_field, new_type)
        return super()._alter_column_type_sql(model, old_field, new_field, new_type)

    def alter_unique_together(self, model, old_unique_together, new_unique_together):
        """
        Deal with a model changing its unique_together. The input
        unique_togethers must be doubly-nested, not the single-nested
        ["foo", "bar"] format.
        """
        olds = {tuple(fields) for fields in old_unique_together}
        news = {tuple(fields) for fields in new_unique_together}
        # Deleted uniques
        for fields in olds.difference(news):
            self._delete_composed_index(model, fields, {'unique': True}, self.sql_delete_index)
        # Created uniques
        for fields in news.difference(olds):
            columns = [model._meta.get_field(field).column for field in fields]
            condition = ' AND '.join(["[%s] IS NOT NULL" % col for col in columns])
            sql = self._create_unique_sql(model, columns, condition=condition)
            self.execute(sql)

    def _model_indexes_sql(self, model):
        """
        Return a list of all index SQL statements (field indexes,
        index_together, Meta.indexes) for the specified model.
        """
        if not model._meta.managed or model._meta.proxy or model._meta.swapped:
            return []
        output = []
        for field in model._meta.local_fields:
            output.extend(self._field_indexes_sql(model, field))

        for field_names in model._meta.index_together:
            fields = [model._meta.get_field(field) for field in field_names]
            output.append(self._create_index_sql(model, fields, suffix="_idx"))

        for field_names in model._meta.unique_together:
            columns = [model._meta.get_field(field).column for field in field_names]
            condition = ' AND '.join(["[%s] IS NOT NULL" % col for col in columns])
            sql = self._create_unique_sql(model, columns, condition=condition)
            output.append(sql)

        for index in model._meta.indexes:
            if django_version >= (3, 2) and (
                not index.contains_expressions or
                self.connection.features.supports_expression_indexes
            ):
                output.append(index.create_sql(model, self))
            else:
                output.append(index.create_sql(model, self))
        return output

    def _alter_many_to_many(self, model, old_field, new_field, strict):
        """Alter M2Ms to repoint their to= endpoints."""

        for idx in self._constraint_names(old_field.remote_field.through, index=True, unique=True):
            self.execute(self.sql_delete_index % {'name': idx, 'table': old_field.remote_field.through._meta.db_table})

        return super()._alter_many_to_many(model, old_field, new_field, strict)

    def _db_table_constraint_names(self, db_table, column_names=None, unique=None,
                                   primary_key=None, index=None, foreign_key=None,
                                   check=None, type_=None, exclude=None):
        """Return all constraint names matching the columns and conditions."""
        if column_names is not None:
            column_names = [
                self.connection.introspection.identifier_converter(name)
                for name in column_names
            ]
        with self.connection.cursor() as cursor:
            constraints = self.connection.introspection.get_constraints(cursor, db_table)
        result = []
        for name, infodict in constraints.items():
            if column_names is None or column_names == infodict['columns']:
                if unique is not None and infodict['unique'] != unique:
                    continue
                if primary_key is not None and infodict['primary_key'] != primary_key:
                    continue
                if index is not None and infodict['index'] != index:
                    continue
                if check is not None and infodict['check'] != check:
                    continue
                if foreign_key is not None and not infodict['foreign_key']:
                    continue
                if type_ is not None and infodict['type'] != type_:
                    continue
                if not exclude or name not in exclude:
                    result.append(name)
        return result

    def _db_table_delete_constraint_sql(self, template, db_table, name):
        return Statement(
            template,
            table=Table(db_table, self.quote_name),
            name=self.quote_name(name),
            include=''
        )

    def alter_db_table(self, model, old_db_table, new_db_table):
        index_names = self._db_table_constraint_names(old_db_table, index=True)
        for index_name in index_names:
            self.execute(self._db_table_delete_constraint_sql(self.sql_delete_index, old_db_table, index_name))

        index_names = self._db_table_constraint_names(new_db_table, index=True)
        for index_name in index_names:
            self.execute(self._db_table_delete_constraint_sql(self.sql_delete_index, new_db_table, index_name))

        return super().alter_db_table(model, old_db_table, new_db_table)

    def _alter_field(self, model, old_field, new_field, old_type, new_type,
                     old_db_params, new_db_params, strict=False):
        """Actually perform a "physical" (non-ManyToMany) field update."""

        # the backend doesn't support altering from/to (Big)AutoField
        # because of the limited capability of SQL Server to edit IDENTITY property
        for t in (AutoField, BigAutoField):
            if isinstance(old_field, t) or isinstance(new_field, t):
                raise NotImplementedError("the backend doesn't support altering from/to %s." % t.__name__)
        # Drop any FK constraints, we'll remake them later
        fks_dropped = set()
        if old_field.remote_field and old_field.db_constraint:
            # Drop index, SQL Server requires explicit deletion
            if not hasattr(new_field, 'db_constraint') or not new_field.db_constraint:
                index_names = self._constraint_names(model, [old_field.column], index=True)
                for index_name in index_names:
                    self.execute(self._delete_constraint_sql(self.sql_delete_index, model, index_name))

            fk_names = self._constraint_names(model, [old_field.column], foreign_key=True)
            if strict and len(fk_names) != 1:
                raise ValueError("Found wrong number (%s) of foreign key constraints for %s.%s" % (
                    len(fk_names),
                    model._meta.db_table,
                    old_field.column,
                ))
            for fk_name in fk_names:
                fks_dropped.add((old_field.column,))
                self.execute(self._delete_constraint_sql(self.sql_delete_fk, model, fk_name))
        # Has unique been removed?
        if old_field.unique and (not new_field.unique or self._field_became_primary_key(old_field, new_field)):
            # Find the unique constraint for this field
            constraint_names = self._constraint_names(model, [old_field.column], unique=True, primary_key=False)
            if strict and len(constraint_names) != 1:
                raise ValueError("Found wrong number (%s) of unique constraints for %s.%s" % (
                    len(constraint_names),
                    model._meta.db_table,
                    old_field.column,
                ))
            for constraint_name in constraint_names:
                self.execute(self._delete_constraint_sql(self.sql_delete_unique, model, constraint_name))
        # Drop incoming FK constraints if the field is a primary key or unique,
        # which might be a to_field target, and things are going to change.
        drop_foreign_keys = (
            (
                (old_field.primary_key and new_field.primary_key) or
                (old_field.unique and new_field.unique)
            ) and old_type != new_type
        )
        if drop_foreign_keys:
            # '_meta.related_field' also contains M2M reverse fields, these
            # will be filtered out
            for _old_rel, new_rel in _related_non_m2m_objects(old_field, new_field):
                rel_fk_names = self._constraint_names(
                    new_rel.related_model, [new_rel.field.column], foreign_key=True
                )
                for fk_name in rel_fk_names:
                    self.execute(self._delete_constraint_sql(self.sql_delete_fk, new_rel.related_model, fk_name))
        # Removed an index? (no strict check, as multiple indexes are possible)
        # Remove indexes if db_index switched to False or a unique constraint
        # will now be used in lieu of an index. The following lines from the
        # truth table show all True cases; the rest are False:
        #
        # old_field.db_index | old_field.unique | new_field.db_index | new_field.unique
        # ------------------------------------------------------------------------------
        # True               | False            | False              | False
        # True               | False            | False              | True
        # True               | False            | True               | True
        if (old_field.db_index and not old_field.unique and (not new_field.db_index or new_field.unique)) or (
                # Drop indexes on nvarchar columns that are changing to a different type
                # SQL Server requires explicit deletion
                (old_field.db_index or old_field.unique) and (
                    (old_type.startswith('nvarchar') and not new_type.startswith('nvarchar'))
                )):
            # Find the index for this field
            meta_index_names = {index.name for index in model._meta.indexes}
            # Retrieve only BTREE indexes since this is what's created with
            # db_index=True.
            index_names = self._constraint_names(model, [old_field.column], index=True, type_=Index.suffix)
            for index_name in index_names:
                if index_name not in meta_index_names:
                    # The only way to check if an index was created with
                    # db_index=True or with Index(['field'], name='foo')
                    # is to look at its name (refs #28053).
                    self.execute(self._delete_constraint_sql(self.sql_delete_index, model, index_name))
        # Change check constraints?
        if (old_db_params['check'] != new_db_params['check'] and old_db_params['check']) or (
            # SQL Server requires explicit deletion befor altering column type with the same constraint
            old_db_params['check'] == new_db_params['check'] and old_db_params['check'] and
            old_db_params['type'] != new_db_params['type']
        ):
            constraint_names = self._constraint_names(model, [old_field.column], check=True)
            if strict and len(constraint_names) != 1:
                raise ValueError("Found wrong number (%s) of check constraints for %s.%s" % (
                    len(constraint_names),
                    model._meta.db_table,
                    old_field.column,
                ))
            for constraint_name in constraint_names:
                self.execute(self._delete_constraint_sql(self.sql_delete_check, model, constraint_name))
        # Have they renamed the column?
        if old_field.column != new_field.column:
            # remove old indices
            self._delete_indexes(model, old_field, new_field)

            self.execute(self._rename_field_sql(model._meta.db_table, old_field, new_field, new_type))
            # Rename all references to the renamed column.
            for sql in self.deferred_sql:
                if isinstance(sql, DjStatement):
                    sql.rename_column_references(model._meta.db_table, old_field.column, new_field.column)

        # Next, start accumulating actions to do
        actions = []
        null_actions = []
        post_actions = []
        # Type change?
        if old_type != new_type:
            fragment, other_actions = self._alter_column_type_sql(model, old_field, new_field, new_type)
            actions.append(fragment)
            post_actions.extend(other_actions)
            # Drop unique constraint, SQL Server requires explicit deletion
            self._delete_unique_constraints(model, old_field, new_field, strict)
            # Drop indexes, SQL Server requires explicit deletion
            self._delete_indexes(model, old_field, new_field)
        # When changing a column NULL constraint to NOT NULL with a given
        # default value, we need to perform 4 steps:
        #  1. Add a default for new incoming writes
        #  2. Update existing NULL rows with new default
        #  3. Replace NULL constraint with NOT NULL
        #  4. Drop the default again.
        # Default change?
        old_default = self.effective_default(old_field)
        new_default = self.effective_default(new_field)
        needs_database_default = (
            old_field.null and
            not new_field.null and
            old_default != new_default and
            new_default is not None and
            not self.skip_default(new_field)
        )
        if needs_database_default:
            actions.append(self._alter_column_default_sql(model, old_field, new_field))
        # Nullability change?
        if old_field.null != new_field.null:
            fragment = self._alter_column_null_sql(model, old_field, new_field)
            if fragment:
                null_actions.append(fragment)
                if not new_field.null:
                    # Drop unique constraint, SQL Server requires explicit deletion
                    self._delete_unique_constraints(model, old_field, new_field, strict)
                    # Drop indexes, SQL Server requires explicit deletion
                    self._delete_indexes(model, old_field, new_field)
        # Only if we have a default and there is a change from NULL to NOT NULL
        four_way_default_alteration = (
            new_field.has_default() and
            (old_field.null and not new_field.null)
        )
        if actions or null_actions:
            if not four_way_default_alteration:
                # If we don't have to do a 4-way default alteration we can
                # directly run a (NOT) NULL alteration
                actions = actions + null_actions
            # Combine actions together if we can (e.g. postgres)
            if self.connection.features.supports_combined_alters and actions:
                sql, params = tuple(zip(*actions))
                actions = [(", ".join(sql), sum(params, []))]
            # Apply those actions
            for sql, params in actions:
                self._delete_indexes(model, old_field, new_field)
                self.execute(
                    self.sql_alter_column % {
                        "table": self.quote_name(model._meta.db_table),
                        "changes": sql,
                    },
                    params,
                )
            if four_way_default_alteration:
                # Update existing rows with default value
                self.execute(
                    self.sql_update_with_default % {
                        "table": self.quote_name(model._meta.db_table),
                        "column": self.quote_name(new_field.column),
                        "default": "%s",
                    },
                    [new_default],
                )
                # Since we didn't run a NOT NULL change before we need to do it
                # now
                for sql, params in null_actions:
                    self.execute(
                        self.sql_alter_column % {
                            "table": self.quote_name(model._meta.db_table),
                            "changes": sql,
                        },
                        params,
                    )
        if post_actions:
            for sql, params in post_actions:
                self.execute(sql, params)
        # If primary_key changed to False, delete the primary key constraint.
        if old_field.primary_key and not new_field.primary_key:
            self._delete_primary_key(model, strict)
        # Added a unique?
        if self._unique_should_be_added(old_field, new_field):
            if (self.connection.features.supports_nullable_unique_constraints and
                    not new_field.many_to_many and new_field.null):

                self.execute(
                    self._create_index_sql(
                        model, [new_field], sql=self.sql_create_unique_null, suffix="_uniq"
                    )
                )
            else:
                self.execute(self._create_unique_sql(model, [new_field.column]))
        # Added an index?
        # constraint will no longer be used in lieu of an index. The following
        # lines from the truth table show all True cases; the rest are False:
        #
        # old_field.db_index | old_field.unique | new_field.db_index | new_field.unique
        # ------------------------------------------------------------------------------
        # False              | False            | True               | False
        # False              | True             | True               | False
        # True               | True             | True               | False
        if (not old_field.db_index or old_field.unique) and new_field.db_index and not new_field.unique:
            self.execute(self._create_index_sql(model, [new_field]))

        # Restore indexes & unique constraints deleted above, SQL Server requires explicit restoration
        if (old_type != new_type or (old_field.null and not new_field.null)) and (
            old_field.column == new_field.column
        ):
            # Restore unique constraints
            # Note: if nullable they are implemented via an explicit filtered UNIQUE INDEX (not CONSTRAINT)
            # in order to get ANSI-compliant NULL behaviour (i.e. NULL != NULL, multiple are allowed)
            if old_field.unique and new_field.unique:
                if new_field.null:
                    self.execute(
                        self._create_index_sql(
                            model, [old_field], sql=self.sql_create_unique_null, suffix="_uniq"
                        )
                    )
                else:
                    self.execute(self._create_unique_sql(model, columns=[old_field.column]))
            else:
                for fields in model._meta.unique_together:
                    columns = [model._meta.get_field(field).column for field in fields]
                    if old_field.column in columns:
                        condition = ' AND '.join(["[%s] IS NOT NULL" % col for col in columns])
                        self.execute(self._create_unique_sql(model, columns, condition=condition))
            # Restore indexes
            index_columns = []
            if old_field.db_index and new_field.db_index:
                index_columns.append([old_field])
            else:
                for fields in model._meta.index_together:
                    columns = [model._meta.get_field(field) for field in fields]
                    if old_field.column in [c.column for c in columns]:
                        index_columns.append(columns)
            if index_columns:
                for columns in index_columns:
                    self.execute(self._create_index_sql(model, columns, suffix='_idx'))
        # Type alteration on primary key? Then we need to alter the column
        # referring to us.
        rels_to_update = []
        if old_field.primary_key and new_field.primary_key and old_type != new_type:
            rels_to_update.extend(_related_non_m2m_objects(old_field, new_field))
        # Changed to become primary key?
        if self._field_became_primary_key(old_field, new_field):
            # Make the new one
            self.execute(
                self.sql_create_pk % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(
                        self._create_index_name(model._meta.db_table, [new_field.column], suffix="_pk")
                    ),
                    "columns": self.quote_name(new_field.column),
                }
            )
            # Update all referencing columns
            rels_to_update.extend(_related_non_m2m_objects(old_field, new_field))
        # Handle our type alters on the other end of rels from the PK stuff above
        for old_rel, new_rel in rels_to_update:
            rel_db_params = new_rel.field.db_parameters(connection=self.connection)
            rel_type = rel_db_params['type']
            fragment, other_actions = self._alter_column_type_sql(
                new_rel.related_model, old_rel.field, new_rel.field, rel_type
            )
            self.execute(
                self.sql_alter_column % {
                    "table": self.quote_name(new_rel.related_model._meta.db_table),
                    "changes": fragment[0],
                },
                fragment[1],
            )
            for sql, params in other_actions:
                self.execute(sql, params)
        # Does it have a foreign key?
        if (new_field.remote_field and
                (fks_dropped or not old_field.remote_field or not old_field.db_constraint) and
                new_field.db_constraint):
            self.execute(self._create_fk_sql(model, new_field, "_fk_%(to_table)s_%(to_column)s"))
        # Rebuild FKs that pointed to us if we previously had to drop them
        if drop_foreign_keys:
            for rel in new_field.model._meta.related_objects:
                if _is_relevant_relation(rel, new_field) and rel.field.db_constraint:
                    self.execute(self._create_fk_sql(rel.related_model, rel.field, "_fk"))
        # Does it have check constraints we need to add?
        if (old_db_params['check'] != new_db_params['check'] and new_db_params['check']) or (
            # SQL Server requires explicit creation after altering column type with the same constraint
            old_db_params['check'] == new_db_params['check'] and new_db_params['check'] and
            old_db_params['type'] != new_db_params['type']
        ):
            self.execute(
                self.sql_create_check % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(
                        self._create_index_name(model._meta.db_table, [new_field.column], suffix="_check")
                    ),
                    "column": self.quote_name(new_field.column),
                    "check": new_db_params['check'],
                }
            )
        # Drop the default if we need to
        # (Django usually does not use in-database defaults)
        if needs_database_default:
            changes_sql, params = self._alter_column_default_sql(model, old_field, new_field, drop=True)
            sql = self.sql_alter_column % {
                "table": self.quote_name(model._meta.db_table),
                "changes": changes_sql,
            }
            self.execute(sql, params)

        # Reset connection if required
        if self.connection.features.connection_persists_old_columns:
            self.connection.close()

    def _delete_indexes(self, model, old_field, new_field):
        index_columns = []
        if old_field.db_index and new_field.db_index:
            index_columns.append([old_field.column])
        for fields in model._meta.index_together:
            columns = [model._meta.get_field(field).column for field in fields]
            if old_field.column in columns:
                index_columns.append(columns)

        for fields in model._meta.unique_together:
            columns = [model._meta.get_field(field).column for field in fields]
            if old_field.column in columns:
                index_columns.append(columns)
        if index_columns:
            for columns in index_columns:
                index_names = self._constraint_names(model, columns, index=True)
                for index_name in index_names:
                    self.execute(self._delete_constraint_sql(self.sql_delete_index, model, index_name))

    def _delete_unique_constraints(self, model, old_field, new_field, strict=False):
        unique_columns = []
        if old_field.unique and new_field.unique:
            unique_columns.append([old_field.column])
        if unique_columns:
            for columns in unique_columns:
                constraint_names_normal = self._constraint_names(model, columns, unique=True, index=False)
                constraint_names_index = self._constraint_names(model, columns, unique=True, index=True)
                constraint_names = constraint_names_normal + constraint_names_index
                if strict and len(constraint_names) != 1:
                    raise ValueError("Found wrong number (%s) of unique constraints for %s.%s" % (
                        len(constraint_names),
                        model._meta.db_table,
                        old_field.column,
                    ))
                for constraint_name in constraint_names_normal:
                    self.execute(self._delete_constraint_sql(self.sql_delete_unique, model, constraint_name))
                # Unique indexes which are not table constraints must be deleted using the appropriate SQL.
                # These may exist for example to enforce ANSI-compliant unique constraints on nullable columns.
                for index_name in constraint_names_index:
                    self.execute(self._delete_constraint_sql(self.sql_delete_index, model, index_name))

    def _rename_field_sql(self, table, old_field, new_field, new_type):
        new_type = self._set_field_new_type_null_status(old_field, new_type)
        return super()._rename_field_sql(table, old_field, new_field, new_type)

    def _set_field_new_type_null_status(self, field, new_type):
        """
        Keep the null property of the old field. If it has changed, it will be
        handled separately.
        """
        if field.null:
            new_type += " NULL"
        else:
            new_type += " NOT NULL"
        return new_type

    def add_field(self, model, field):
        """
        Create a field on a model. Usually involves adding a column, but may
        involve adding a table instead (for M2M fields).
        """
        # Special-case implicit M2M tables
        if field.many_to_many and field.remote_field.through._meta.auto_created:
            return self.create_model(field.remote_field.through)
        # Get the column's definition
        definition, params = self.column_sql(model, field, include_default=True)
        # It might not actually have a column behind it
        if definition is None:
            return

        if (self.connection.features.supports_nullable_unique_constraints and
                not field.many_to_many and field.null and field.unique):

            definition = definition.replace(' UNIQUE', '')
            self.deferred_sql.append(self._create_index_sql(
                model, [field], sql=self.sql_create_unique_null, suffix="_uniq"
            ))

        # Check constraints can go on the column SQL here
        db_params = field.db_parameters(connection=self.connection)
        if db_params['check']:
            definition += " CHECK (%s)" % db_params['check']
        # Build the SQL and run it
        sql = self.sql_create_column % {
            "table": self.quote_name(model._meta.db_table),
            "column": self.quote_name(field.column),
            "definition": definition,
        }
        self.execute(sql, params)
        # Drop the default if we need to
        # (Django usually does not use in-database defaults)
        if not self.skip_default(field) and self.effective_default(field) is not None:
            changes_sql, params = self._alter_column_default_sql(model, None, field, drop=True)
            sql = self.sql_alter_column % {
                "table": self.quote_name(model._meta.db_table),
                "changes": changes_sql,
            }
            self.execute(sql, params)
        # Add an index, if required
        self.deferred_sql.extend(self._field_indexes_sql(model, field))
        # Add any FK constraints later
        if field.remote_field and self.connection.features.supports_foreign_keys and field.db_constraint:
            self.deferred_sql.append(self._create_fk_sql(model, field, "_fk_%(to_table)s_%(to_column)s"))
        # Reset connection if required
        if self.connection.features.connection_persists_old_columns:
            self.connection.close()

    def _create_unique_sql(self, model, columns, name=None, condition=None, deferrable=None, include=None, opclasses=None):
        if (deferrable and not getattr(self.connection.features, 'supports_deferrable_unique_constraints', False)):
            return None

        def create_unique_name(*args, **kwargs):
            return self.quote_name(self._create_index_name(*args, **kwargs))

        table = Table(model._meta.db_table, self.quote_name)
        if name is None:
            name = IndexName(model._meta.db_table, columns, '_uniq', create_unique_name)
        else:
            name = self.quote_name(name)
        columns = Columns(table, columns, self.quote_name)
        statement_args = {
            "deferrable": self._deferrable_constraint_sql(deferrable)
        } if django_version >= (3, 1) else {}
        include = self._index_include_sql(model, include) if django_version >=(3, 2) else ''

        if condition:
            return Statement(
                self.sql_create_unique_index,
                table=table,
                name=name,
                columns=columns,
                condition=' WHERE ' + condition,
                **statement_args,
                include=include,
            ) if self.connection.features.supports_partial_indexes else None
        else:
            return Statement(
                self.sql_create_unique,
                table=table,
                name=name,
                columns=columns,
                **statement_args,
                include=include,
            )

    def _create_index_sql(self, model, fields, *, name=None, suffix='', using='',
                          db_tablespace=None, col_suffixes=(), sql=None, opclasses=(),
                          condition=None, include=None, expressions=None):
        """
        Return the SQL statement to create the index for one or several fields.
        `sql` can be specified if the syntax differs from the standard (GIS
        indexes, ...).
        """
        if django_version >= (3, 2):
            return super()._create_index_sql(
                model, fields=fields, name=name, suffix=suffix, using=using,
                db_tablespace=db_tablespace, col_suffixes=col_suffixes, sql=sql,
                opclasses=opclasses, condition=condition, include=include,
                expressions=expressions,
            )
        return super()._create_index_sql(
            model, fields=fields, name=name, suffix=suffix, using=using,
            db_tablespace=db_tablespace, col_suffixes=col_suffixes, sql=sql,
            opclasses=opclasses, condition=condition,
        )

    def create_model(self, model):
        """
        Takes a model and creates a table for it in the database.
        Will also create any accompanying indexes or unique constraints.
        """
        # Create column SQL, add FK deferreds if needed
        column_sqls = []
        params = []
        for field in model._meta.local_fields:
            # SQL
            definition, extra_params = self.column_sql(model, field)
            if definition is None:
                continue

            if (self.connection.features.supports_nullable_unique_constraints and
                    not field.many_to_many and field.null and field.unique):

                definition = definition.replace(' UNIQUE', '')
                self.deferred_sql.append(self._create_index_sql(
                    model, [field], sql=self.sql_create_unique_null, suffix="_uniq"
                ))

            # Check constraints can go on the column SQL here
            db_params = field.db_parameters(connection=self.connection)
            if db_params['check']:
                # SQL Server requires a name for the check constraint
                definition += self._sql_check_constraint % {
                    "name": self._create_index_name(model._meta.db_table, [field.column], suffix="_check"),
                    "check": db_params['check']
                }
            # Autoincrement SQL (for backends with inline variant)
            col_type_suffix = field.db_type_suffix(connection=self.connection)
            if col_type_suffix:
                definition += " %s" % col_type_suffix
            params.extend(extra_params)
            # FK
            if field.remote_field and field.db_constraint:
                to_table = field.remote_field.model._meta.db_table
                to_column = field.remote_field.model._meta.get_field(field.remote_field.field_name).column
                if self.sql_create_inline_fk:
                    definition += " " + self.sql_create_inline_fk % {
                        "to_table": self.quote_name(to_table),
                        "to_column": self.quote_name(to_column),
                    }
                elif self.connection.features.supports_foreign_keys:
                    self.deferred_sql.append(self._create_fk_sql(model, field, "_fk_%(to_table)s_%(to_column)s"))
            # Add the SQL to our big list
            column_sqls.append("%s %s" % (
                self.quote_name(field.column),
                definition,
            ))
            # Autoincrement SQL (for backends with post table definition variant)
            if field.get_internal_type() in ("AutoField", "BigAutoField", "SmallAutoField"):
                autoinc_sql = self.connection.ops.autoinc_sql(model._meta.db_table, field.column)
                if autoinc_sql:
                    self.deferred_sql.extend(autoinc_sql)

        # Add any unique_togethers (always deferred, as some fields might be
        # created afterwards, like geometry fields with some backends)
        for fields in model._meta.unique_together:
            columns = [model._meta.get_field(field).column for field in fields]
            condition = ' AND '.join(["[%s] IS NOT NULL" % col for col in columns])
            self.deferred_sql.append(self._create_unique_sql(model, columns, condition=condition))

        constraints = [constraint.constraint_sql(model, self) for constraint in model._meta.constraints]
        # Make the table
        sql = self.sql_create_table % {
            "table": self.quote_name(model._meta.db_table),
            'definition': ', '.join(constraint for constraint in (*column_sqls, *constraints) if constraint),
        }
        if model._meta.db_tablespace:
            tablespace_sql = self.connection.ops.tablespace_sql(model._meta.db_tablespace)
            if tablespace_sql:
                sql += ' ' + tablespace_sql
        # Prevent using [] as params, in the case a literal '%' is used in the definition
        self.execute(sql, params or None)

        # Add any field index and index_together's (deferred as SQLite3 _remake_table needs it)
        self.deferred_sql.extend(self._model_indexes_sql(model))
        self.deferred_sql = list(set(self.deferred_sql))

        # Make M2M tables
        for field in model._meta.local_many_to_many:
            if field.remote_field.through._meta.auto_created:
                self.create_model(field.remote_field.through)

    def _delete_unique_sql(
        self, model, name, condition=None, deferrable=None, include=None,
        opclasses=None,
    ):
        if (
            (
                deferrable and
                not self.connection.features.supports_deferrable_unique_constraints
            ) or
            (condition and not self.connection.features.supports_partial_indexes) or
            (include and not self.connection.features.supports_covering_indexes)
        ):
            return None
        if condition or include or opclasses:
            sql = self.sql_delete_index
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE WHERE CONSTRAINT_NAME = '%s'" % name)
                row = cursor.fetchone()
                if row:
                    sql = self.sql_delete_unique
        else:
            sql = self.sql_delete_unique
        return self._delete_constraint_sql(sql, model, name)

    def delete_model(self, model):
        super().delete_model(model)

    def execute(self, sql, params=(), has_result=False):
        """
        Executes the given SQL statement, with optional parameters.
        """
        result = None
        # Don't perform the transactional DDL check if SQL is being collected
        # as it's not going to be executed anyway.
        if not self.collect_sql and self.connection.in_atomic_block and not self.connection.features.can_rollback_ddl:
            raise TransactionManagementError(
                "Executing DDL statements while in a transaction on databases "
                "that can't perform a rollback is prohibited."
            )
        # Account for non-string statement objects.
        sql = str(sql)
        # Log the command we're running, then run it
        logger.debug("%s; (params %r)", sql, params, extra={'params': params, 'sql': sql})
        if self.collect_sql:
            ending = "" if sql.endswith(";") else ";"
            if params is not None:
                self.collected_sql.append((sql % tuple(map(self.quote_value, params))) + ending)
            else:
                self.collected_sql.append(sql + ending)
        else:
            cursor = self.connection.cursor()
            cursor.execute(sql, params)
            if has_result:
                result = cursor.fetchall()
            # the cursor can be closed only when the driver supports opening
            # multiple cursors on a connection because the migration command
            # has already opened a cursor outside this method
            if self.connection.supports_mars:
                cursor.close()
        return result

    def prepare_default(self, value):
        return self.quote_value(value)

    def quote_value(self, value):
        """
        Returns a quoted version of the value so it's safe to use in an SQL
        string. This is not safe against injection from user code; it is
        intended only for use in making SQL scripts or preparing default values
        for particularly tricky backends (defaults are not user-defined, though,
        so this is safe).
        """
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return "'%s'" % value
        elif isinstance(value, str):
            return "'%s'" % value.replace("'", "''")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            return "0x%s" % force_str(binascii.hexlify(value))
        elif isinstance(value, bool):
            return "1" if value else "0"
        else:
            return str(value)

    def remove_field(self, model, field):
        """
        Removes a field from a model. Usually involves deleting a column,
        but for M2Ms may involve deleting a table.
        """
        # Special-case implicit M2M tables
        if field.many_to_many and field.remote_field.through._meta.auto_created:
            return self.delete_model(field.remote_field.through)
        # It might not actually have a column behind it
        if field.db_parameters(connection=self.connection)['type'] is None:
            return
        # Drop any FK constraints, SQL Server requires explicit deletion
        with self.connection.cursor() as cursor:
            constraints = self.connection.introspection.get_constraints(cursor, model._meta.db_table)
        for name, infodict in constraints.items():
            if field.column in infodict['columns'] and infodict['foreign_key']:
                self.execute(self._delete_constraint_sql(self.sql_delete_fk, model, name))
        # Drop any indexes, SQL Server requires explicit deletion
        for name, infodict in constraints.items():
            if field.column in infodict['columns'] and infodict['index']:
                self.execute(self.sql_delete_index % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(name),
                })
        # Drop primary key constraint, SQL Server requires explicit deletion
        for name, infodict in constraints.items():
            if field.column in infodict['columns'] and infodict['primary_key']:
                self.execute(self.sql_delete_pk % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(name),
                })
        # Drop check constraints, SQL Server requires explicit deletion
        for name, infodict in constraints.items():
            if field.column in infodict['columns'] and infodict['check']:
                self.execute(self.sql_delete_check % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(name),
                })
        # Drop unique constraints, SQL Server requires explicit deletion
        for name, infodict in constraints.items():
            if (field.column in infodict['columns'] and infodict['unique'] and
                    not infodict['primary_key'] and not infodict['index']):
                self.execute(self.sql_delete_unique % {
                    "table": self.quote_name(model._meta.db_table),
                    "name": self.quote_name(name),
                })
        # Delete the column
        sql = self.sql_delete_column % {
            "table": self.quote_name(model._meta.db_table),
            "column": self.quote_name(field.column),
        }
        self.execute(sql)
        # Reset connection if required
        if self.connection.features.connection_persists_old_columns:
            self.connection.close()
        # Remove all deferred statements referencing the deleted column.
        for sql in list(self.deferred_sql):
            if isinstance(sql, Statement) and sql.references_column(model._meta.db_table, field.column):
                self.deferred_sql.remove(sql)

    def add_constraint(self, model, constraint):
        if isinstance(constraint, UniqueConstraint) and constraint.condition and constraint.condition.connector != AND:
            raise NotImplementedError("The backend does not support %s conditions on unique constraint %s." %
                                      (constraint.condition.connector, constraint.name))
        super().add_constraint(model, constraint)
