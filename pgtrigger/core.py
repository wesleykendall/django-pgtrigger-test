import contextlib
import copy
import hashlib
import re

from django.db import DEFAULT_DB_ALIAS, models, router, transaction
from django.db.models.expressions import Col
from django.db.models.fields.related import RelatedField
from django.db.models.sql import Query
from django.db.models.sql.datastructures import BaseTable
from django.db.utils import ProgrammingError
import psycopg2.extensions

from pgtrigger import compiler, features, registry, utils


# Postgres only allows identifiers to be 63 chars max. Since "pgtrigger_"
# is the prefix for trigger names, and since an additional "_" and
# 5 character hash is added, the user-defined name of the trigger can only
# be 47 chars.
# NOTE: We can do something more sophisticated later by allowing users
# to name their triggers and then hashing the names when actually creating
# the triggers.
MAX_NAME_LENGTH = 47

# Installation states for a triggers
INSTALLED = "INSTALLED"
UNINSTALLED = "UNINSTALLED"
OUTDATED = "OUTDATED"
PRUNE = "PRUNE"
UNALLOWED = "UNALLOWED"


class _Primitive:
    """Boilerplate for some of the primitive operations"""

    def __init__(self, name):
        assert name in self.values
        self.name = name

    def __str__(self):
        return self.name


class Level(_Primitive):
    values = ("ROW", "STATEMENT")


#: For specifying row-level triggers (the default)
Row = Level("ROW")

#: For specifying statement-level triggers
Statement = Level("STATEMENT")


class Referencing:
    """For specifying the REFERENCING clause of a statement-level trigger"""

    def __init__(self, *, old=None, new=None):
        if not old and not new:
            raise ValueError(
                'Must provide either "old" and/or "new" to the referencing'
                " construct of a trigger"
            )

        self.old = old
        self.new = new

    def __str__(self):
        ref = "REFERENCING"
        if self.old:
            ref += f" OLD TABLE AS {self.old} "

        if self.new:
            ref += f" NEW TABLE AS {self.new} "

        return ref


class When(_Primitive):
    values = ("BEFORE", "AFTER", "INSTEAD OF")


#: For specifying ``BEFORE`` in the when clause of a trigger.
Before = When("BEFORE")

#: For specifying ``AFTER`` in the when clause of a trigger.
After = When("AFTER")

#: For specifying ``INSTEAD OF`` in the when clause of a trigger.
InsteadOf = When("INSTEAD OF")


class Operation(_Primitive):
    values = ("UPDATE", "DELETE", "TRUNCATE", "INSERT")

    def __or__(self, other):
        assert isinstance(other, Operation)
        return Operations(self, other)


class Operations(Operation):
    """For providing multiple operations ``OR``ed together.

    Note that using the ``|`` operator is preferred syntax.
    """

    def __init__(self, *operations):
        for operation in operations:
            assert isinstance(operation, Operation)

        self.operations = operations

    def __str__(self):
        return " OR ".join(str(operation) for operation in self.operations)


#: For specifying ``UPDATE`` as the trigger operation.
Update = Operation("UPDATE")

#: For specifying ``DELETE`` as the trigger operation.
Delete = Operation("DELETE")

#: For specifying ``TRUNCATE`` as the trigger operation.
Truncate = Operation("TRUNCATE")

#: For specifying ``INSERT`` as the trigger operation.
Insert = Operation("INSERT")


class UpdateOf(Operation):
    """For specifying ``UPDATE OF`` as the trigger operation."""

    def __init__(self, *columns):
        if not columns:
            raise ValueError("Must provide at least one column")

        self.columns = columns

    def __str__(self):
        columns = ", ".join(f"{utils.quote(col)}" for col in self.columns)
        return f"UPDATE OF {columns}"


class Timing(_Primitive):
    values = ("IMMEDIATE", "DEFERRED")


#: For deferrable triggers that run immediately by default
Immediate = Timing("IMMEDIATE")

#: For deferrable triggers that run at the end of the transaction by default
Deferred = Timing("DEFERRED")


class Condition:
    """For specifying free-form SQL in the condition of a trigger."""

    sql = None

    def __init__(self, sql=None):
        self.sql = sql or self.sql

        if not self.sql:
            raise ValueError("Must provide SQL to condition")

    def resolve(self, model):
        return self.sql


class _OldNewQuery(Query):
    """
    A special Query object for referencing the ``OLD`` and ``NEW`` variables in a
    trigger. Only used by the `pgtrigger.Q` object.
    """

    def build_lookup(self, lookups, lhs, rhs):
        # Django does not allow custom lookups on foreign keys, even though
        # DISTINCT FROM is a comnpletely valid lookup. Trick django into
        # being able to apply this lookup to related fields.
        if lookups == ["df"] and isinstance(lhs.output_field, RelatedField):
            lhs = copy.deepcopy(lhs)
            lhs.output_field = models.IntegerField(null=lhs.output_field.null)

        return super().build_lookup(lookups, lhs, rhs)

    def build_filter(self, filter_expr, *args, **kwargs):
        if isinstance(filter_expr, Q):
            return super().build_filter(filter_expr, *args, **kwargs)

        if filter_expr[0].startswith("old__"):
            alias = "OLD"
        elif filter_expr[0].startswith("new__"):
            alias = "NEW"
        else:  # pragma: no cover
            raise ValueError("Filter expression on trigger.Q object must reference old__ or new__")

        filter_expr = (filter_expr[0][5:], filter_expr[1])
        node, _ = super().build_filter(filter_expr, *args, **kwargs)

        self.alias_map[alias] = BaseTable(alias, alias)
        for child in node.children:
            child.lhs = Col(
                alias=alias,
                target=child.lhs.target,
                output_field=child.lhs.output_field,
            )

        return node, {alias}


class F(models.F):
    """
    Similar to Django's ``F`` object, allows referencing the old and new
    rows in a trigger condition.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.name.startswith("old__"):
            self.row_alias = "OLD"
        elif self.name.startswith("new__"):
            self.row_alias = "NEW"
        else:
            raise ValueError("F() values must reference old__ or new__")

        self.col_name = self.name[5:]

    @property
    def resolved_name(self):
        return f"{self.row_alias}.{utils.quote(self.col_name)}"

    def resolve_expression(self, query=None, *args, **kwargs):
        return Col(
            alias=self.row_alias,
            target=query.model._meta.get_field(self.col_name),
        )


@models.fields.Field.register_lookup
class IsDistinctFrom(models.Lookup):
    """
    A custom ``IS DISTINCT FROM`` field lookup for common trigger conditions.
    For example, ``pgtrigger.Q(old__field__df=pgtrigger.F("new__field"))``.
    """

    lookup_name = "df"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return "%s IS DISTINCT FROM %s" % (lhs, rhs), params


@models.fields.Field.register_lookup
class IsNotDistinctFrom(models.Lookup):
    """
    A custom ``IS NOT DISTINCT FROM`` field lookup for common trigger conditions.
    For example, ``pgtrigger.Q(old__field__ndf=pgtrigger.F("new__field"))``.
    """

    lookup_name = "ndf"

    def as_sql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        params = lhs_params + rhs_params
        return "%s IS NOT DISTINCT FROM %s" % (lhs, rhs), params


class Q(models.Q, Condition):
    """
    Similar to Django's ``Q`` object, allows referencing the old and new
    rows in a trigger condition.
    """

    def resolve(self, model):
        query = _OldNewQuery(model)
        sql, args = self.resolve_expression(query).as_sql(
            compiler=query.get_compiler("default"),
            connection=utils.connection(),
        )
        sql = sql.replace('"OLD"', "OLD").replace('"NEW"', "NEW")
        args = tuple(psycopg2.extensions.adapt(arg).getquoted().decode() for arg in args)

        return sql % args


class Func:
    """
    Allows for rendering a function with access to the "meta", "fields",
    and "columns" variables of the current model.

    For example, ``func=Func("SELECT {columns.id} FROM {meta.db_table};")`` makes it
    possible to do inline SQL in the ``Meta`` of a model and reference its properties.
    """

    def __init__(self, func):
        self.func = func

    def render(self, model):
        fields = utils.AttrDict({field.name: field for field in model._meta.fields})
        columns = utils.AttrDict({field.name: field.column for field in model._meta.fields})
        return self.func.format(meta=model._meta, fields=fields, columns=columns)


# Allows Trigger methods to be used as context managers, mostly for
# testing purposes
@contextlib.contextmanager
def _cleanup_on_exit(cleanup):
    yield
    cleanup()


def _ignore_func_name():
    ignore_func = "_pgtrigger_should_ignore"
    if features.schema():  # pragma: no branch
        ignore_func = f"{utils.quote(features.schema())}.{ignore_func}"

    return ignore_func


class Trigger:
    """
    For specifying a free-form PL/pgSQL trigger function or for
    creating derived trigger classes.
    """

    name = None
    level = Row
    when = None
    operation = None
    condition = None
    referencing = None
    func = None
    declare = None
    timing = None

    def __init__(
        self,
        *,
        name=None,
        level=None,
        when=None,
        operation=None,
        condition=None,
        referencing=None,
        func=None,
        declare=None,
        timing=None,
    ):
        self.name = name or self.name
        self.level = level or self.level
        self.when = when or self.when
        self.operation = operation or self.operation
        self.condition = condition or self.condition
        self.referencing = referencing or self.referencing
        self.func = func or self.func
        self.declare = declare or self.declare
        self.timing = timing or self.timing

        if not self.level or not isinstance(self.level, Level):
            raise ValueError(f'Invalid "level" attribute: {self.level}')

        if not self.when or not isinstance(self.when, When):
            raise ValueError(f'Invalid "when" attribute: {self.when}')

        if not self.operation or not isinstance(self.operation, Operation):
            raise ValueError(f'Invalid "operation" attribute: {self.operation}')

        if self.timing and not isinstance(self.timing, Timing):
            raise ValueError(f'Invalid "timing" attribute: {self.timing}')

        if self.level == Row and self.referencing:
            raise ValueError('Row-level triggers cannot have a "referencing" attribute')

        if self.timing and self.level != Row:
            raise ValueError('Deferrable triggers must have "level" attribute as "pgtrigger.Row"')

        if self.timing and self.when != After:
            raise ValueError('Deferrable triggers must have "when" attribute as "pgtrigger.After"')

        if not self.name:
            raise ValueError('Trigger must have "name" attribute')

        self.validate_name()

    def __str__(self):
        return self.name

    def validate_name(self):
        """Verifies the name is under the maximum length"""
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError(f'Trigger name "{self.name}" > {MAX_NAME_LENGTH} characters.')

        if not re.match(r"^[a-zA-Z0-9-_]+$", self.name):
            raise ValueError(
                f'Trigger name "{self.name}" has invalid characters.'
                " Only alphanumeric characters, hyphens, and underscores are allowed."
            )

    def get_pgid(self, model):
        """The ID of the trigger and function object in postgres

        All objects are prefixed with "pgtrigger_" in order to be
        discovered/managed by django-pgtrigger
        """
        model_hash = hashlib.sha1(self.get_uri(model).encode()).hexdigest()[:5]
        pgid = f"pgtrigger_{self.name}_{model_hash}"

        if len(pgid) > 63:
            raise ValueError(f'Trigger identifier "{pgid}" is greater than 63 chars')

        # NOTE - Postgres always stores names in lowercase. Ensure that all
        # generated IDs are lowercase so that we can properly do installation
        # and pruning tasks.
        return pgid.lower()

    def get_condition(self, model):
        return self.condition

    def get_declare(self, model):
        """
        Gets the DECLARE part of the trigger function if any variables
        are used.

        Returns:
            List[tuple]: A list of variable name / type tuples that will
            be shown in the DECLARE. For example [('row_data', 'JSONB')]
        """
        return self.declare or []

    def get_func(self, model):
        """
        Returns the trigger function that comes between the BEGIN and END
        clause
        """
        if not self.func:
            raise ValueError("Must define func attribute or implement get_func")
        return self.func

    def get_uri(self, model):
        """The URI for the trigger"""

        return f"{model._meta.app_label}.{model._meta.object_name}:{self.name}"

    def render_condition(self, model):
        """Renders the condition SQL in the trigger declaration"""
        condition = self.get_condition(model)
        resolved = condition.resolve(model).strip() if condition else ""

        if resolved:
            if not resolved.startswith("("):
                resolved = f"({resolved})"
            resolved = f"WHEN {resolved}"

        return resolved

    def render_declare(self, model):
        """Renders the DECLARE of the trigger function, if any"""
        declare = self.get_declare(model)
        if declare:
            rendered_declare = "DECLARE " + " ".join(
                f"{var_name} {var_type};" for var_name, var_type in declare
            )
        else:
            rendered_declare = ""

        return rendered_declare

    def render_execute(self, model):
        """
        Renders what should be executed by the trigger. This defaults
        to the trigger function
        """
        return f"{self.get_pgid(model)}()"

    def render_func(self, model):
        """
        Renders the func
        """
        func = self.get_func(model)

        if isinstance(func, Func):
            return func.render(model)
        else:
            return func

    def compile(self, model):
        return compiler.Trigger(
            name=self.name,
            sql=compiler.UpsertTriggerSql(
                ignore_func_name=_ignore_func_name(),
                pgid=self.get_pgid(model),
                declare=self.render_declare(model),
                func=self.render_func(model),
                table=model._meta.db_table,
                constraint="CONSTRAINT" if self.timing else "",
                when=self.when,
                operation=self.operation,
                timing=f"DEFERRABLE INITIALLY {self.timing}" if self.timing else "",
                referencing=self.referencing or "",
                level=self.level,
                condition=self.render_condition(model),
                execute=self.render_execute(model),
            ),
        )

    def allow_migrate(self, model, database=None):
        """True if the trigger for this model can be migrated.

        Defaults to using the router's allow_migrate
        """
        model = model._meta.concrete_model
        return utils.is_postgres(database) and router.allow_migrate(
            database or DEFAULT_DB_ALIAS, model._meta.app_label, model_name=model._meta.model_name
        )

    def format_sql(self, sql):
        """Returns SQL as one line that has trailing whitespace removed from each line"""
        return " ".join(line.strip() for line in sql.split("\n") if line.strip()).strip()

    def exec_sql(self, sql, model, database=None, fetchall=False):
        """Conditionally execute SQL if migrations are allowed"""
        if self.allow_migrate(model, database=database):
            return utils.exec_sql(str(sql), database=database, fetchall=fetchall)

    def get_installation_status(self, model, database=None):
        """Returns the installation status of a trigger.

        The return type is (status, enabled), where status is one of:

        1. ``INSTALLED``: If the trigger is installed
        2. ``UNINSTALLED``: If the trigger is not installed
        3. ``OUTDATED``: If the trigger is installed but has been modified
        4. ``IGNORED``: If migrations are not allowed

        "enabled" is True if the trigger is installed and enabled or false
        if installed and disabled (or uninstalled).
        """
        if not self.allow_migrate(model, database=database):
            return (UNALLOWED, None)

        trigger_exists_sql = f"""
            SELECT oid, obj_description(oid) AS hash, tgenabled AS enabled
            FROM pg_trigger
            WHERE tgname='{self.get_pgid(model)}'
                AND tgrelid='{utils.quote(model._meta.db_table)}'::regclass;
        """
        try:
            with transaction.atomic(using=database):
                results = self.exec_sql(
                    trigger_exists_sql, model, database=database, fetchall=True
                )
        except ProgrammingError:  # pragma: no cover
            # When the table doesn't exist yet, possibly because migrations
            # haven't been executed, a ProgrammingError will happen because
            # of an invalid regclass cast. Return 'UNINSTALLED' for this
            # case
            return (UNINSTALLED, None)

        if not results:
            return (UNINSTALLED, None)
        else:
            hash = self.compile(model).hash
            if hash != results[0][1]:
                return (OUTDATED, results[0][2] == "O")
            else:
                return (INSTALLED, results[0][2] == "O")

    def register(self, *models):
        """Register model classes with the trigger"""
        for model in models:
            registry.set(self.get_uri(model), model=model, trigger=self)

        return _cleanup_on_exit(lambda: self.unregister(*models))

    def unregister(self, *models):
        """Unregister model classes with the trigger"""
        for model in models:
            registry.delete(self.get_uri(model))

        return _cleanup_on_exit(lambda: self.register(*models))

    def install(self, model, database=None):
        """Installs the trigger for a model"""
        install_sql = self.compile(model).install_sql
        with transaction.atomic(using=database):
            self.exec_sql(install_sql, model, database=database)
        return _cleanup_on_exit(lambda: self.uninstall(model, database=database))

    def uninstall(self, model, database=None):
        """Uninstalls the trigger for a model"""
        uninstall_sql = self.compile(model).uninstall_sql
        self.exec_sql(uninstall_sql, model, database=database)
        return _cleanup_on_exit(  # pragma: no branch
            lambda: self.install(model, database=database)
        )

    def enable(self, model, database=None):
        """Enables the trigger for a model"""
        enable_sql = self.compile(model).enable_sql
        self.exec_sql(enable_sql, model, database=database)
        return _cleanup_on_exit(  # pragma: no branch
            lambda: self.disable(model, database=database)
        )

    def disable(self, model, database=None):
        """Disables the trigger for a model"""
        disable_sql = self.compile(model).disable_sql
        self.exec_sql(disable_sql, model, database=database)
        return _cleanup_on_exit(lambda: self.enable(model, database=database))  # pragma: no branch
