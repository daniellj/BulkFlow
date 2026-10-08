"""Idempotent SQL Server Change Data Capture activation.

Database activation is deliberately separate from table activation. The
coordinator also applies the cleanup retention once per source database. On a
new CDC database, SQL Server creates that job only when the first table is
enabled, so retention is completed after that table and before any BCP work.
Operational SQL failures are returned as structured results so one table
cannot accidentally abort the whole dataset.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from .config import DEFAULT_CDC_RETENTION_MINUTES, MAX_CDC_RETENTION_MINUTES
from .sql import execute, fetch_dicts
from .util import qi, redacted_exception, validate_sql_identifier


__all__ = [
    "CdcCoordinator",
    "DEFAULT_CDC_RETENTION_MINUTES",
    "MAX_CDC_RETENTION_MINUTES",
    "DatabaseCdcResult",
    "TableCdcResult",
    "ensure_database_cdc",
    "ensure_table_cdc",
]


DATABASE_LOOKUP_SQL = """
SELECT
    CONVERT(nvarchar(60), state_desc) AS [state],
    CONVERT(bit, is_cdc_enabled) AS [is_cdc_enabled]
FROM sys.databases
WHERE name = ?;
"""

TABLE_LOOKUP_SQL = """
USE {database};
SELECT
    CONVERT(bit, t.is_tracked_by_cdc) AS [is_tracked_by_cdc],
    CONVERT(bit, CASE WHEN EXISTS
    (
        SELECT 1
        FROM sys.key_constraints AS kc
        WHERE kc.parent_object_id = t.object_id
          AND kc.[type] = N'PK'
    ) THEN 1 ELSE 0 END) AS [supports_net_changes]
FROM sys.tables AS t
INNER JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE s.name = ?
  AND t.name = ?;
"""

ENABLE_DATABASE_SQL = """
USE {database};
EXEC sys.sp_cdc_enable_db;
"""

RETENTION_LOOKUP_SQL = """
USE {database};
SELECT CONVERT(bigint, retention) AS [retention_minutes]
FROM msdb.dbo.cdc_jobs
WHERE database_id = DB_ID()
  AND job_type = N'cleanup';
"""

CHANGE_RETENTION_SQL = """
USE {database};
EXEC sys.sp_cdc_change_job
    @job_type = N'cleanup',
    @retention = ?;
"""

STOP_CLEANUP_JOB_SQL = """
USE {database};
EXEC sys.sp_cdc_stop_job @job_type = N'cleanup';
"""

START_CLEANUP_JOB_SQL = """
USE {database};
EXEC sys.sp_cdc_start_job @job_type = N'cleanup';
"""

ENABLE_TABLE_SQL = """
USE {database};
EXEC sys.sp_cdc_enable_table
    @source_schema = ?,
    @source_name = ?,
    @role_name = NULL,
    @supports_net_changes = ?;
"""


@dataclass(frozen=True, slots=True)
class DatabaseCdcResult:
    database: str
    success: bool
    enabled: bool
    changed: bool
    state: str | None
    stage: str
    error_code: str | None = None
    message: str | None = None
    permission_hint: str | None = None
    retention_minutes: int | None = None
    retention_changed: bool = False
    retention_restarted: bool = False
    retention_pending: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TableCdcResult:
    database: str
    schema: str
    table: str
    success: bool
    enabled: bool
    changed: bool
    supports_net_changes: bool | None
    stage: str
    error_code: str | None = None
    message: str | None = None
    permission_hint: str | None = None
    retention_minutes: int | None = None
    retention_changed: bool = False
    retention_restarted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _one_row(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = fetch_dicts(connection, sql, params)
    return rows[0] if rows else None


def _database_state(connection: Any, database: str) -> dict[str, Any] | None:
    return _one_row(connection, DATABASE_LOOKUP_SQL, (database,))


def _table_state(
    connection: Any,
    database: str,
    schema: str,
    table: str,
) -> dict[str, Any] | None:
    return _one_row(
        connection,
        TABLE_LOOKUP_SQL.format(database=qi(database)),
        (schema, table),
    )


def _permission_failure(error: BaseException) -> bool:
    text = redacted_exception(error).casefold()
    return bool(
        re.search(r"(?:\(|\b)(?:229|15247)(?:\)|\b)", text)
        or "permission" in text
        or "permissão" in text
        or "sysadmin" in text
        or "db_owner" in text
        or "not authorized" in text
    )


def _failure_code(default_code: str, error: BaseException) -> str:
    return "CDC_PERMISSION_DENIED" if _permission_failure(error) else default_code


def _database_permission_hint() -> str:
    return (
        "sys.sp_cdc_enable_db exige login membro da server role sysadmin; "
        "um usuário somente leitura pode apenas confirmar CDC previamente provisionado."
    )


def _table_permission_hint() -> str:
    return (
        "sys.sp_cdc_enable_table exige sysadmin ou db_owner no banco de origem; "
        "um usuário somente leitura pode apenas confirmar uma tabela previamente habilitada."
    )


def _retention_permission_hint() -> str:
    return (
        "sys.sp_cdc_change_job exige uma conta autorizada a administrar o CDC; "
        "um usuário somente leitura pode apenas confirmar a retenção previamente provisionada."
    )


def _validate_retention_minutes(retention_minutes: int) -> int:
    if isinstance(retention_minutes, bool) or not isinstance(retention_minutes, int):
        raise ValueError("retention_minutes must be an integer")
    if retention_minutes < 1 or retention_minutes > MAX_CDC_RETENTION_MINUTES:
        raise ValueError(
            "retention_minutes must be between 1 and "
            f"{MAX_CDC_RETENTION_MINUTES}"
        )
    return retention_minutes


def _ensure_cleanup_retention(
    connection: Any,
    *,
    database: str,
    state: str,
    database_changed: bool,
    retention_minutes: int,
    allow_missing_job: bool = False,
) -> DatabaseCdcResult:
    """Apply and verify the cleanup retention after database CDC is enabled."""

    lookup_sql = RETENTION_LOOKUP_SQL.format(database=qi(database))
    try:
        before = _one_row(connection, lookup_sql)
    except Exception as error:
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=database_changed,
            state=state,
            stage="retention_inspection",
            error_code=_failure_code("CDC_RETENTION_INSPECTION_FAILED", error),
            message=(
                f"Não foi possível consultar a retenção do CDC no banco {database}: "
                f"{redacted_exception(error)}"
            ),
            permission_hint=_retention_permission_hint(),
            retention_minutes=None,
        )

    if before is None:
        if allow_missing_job:
            # SQL Server normally creates the capture/cleanup jobs when the
            # first source table is enabled, not merely when the database is
            # enabled. The coordinator completes this deferred step directly
            # after successfully enabling the first requested table.
            return DatabaseCdcResult(
                database=database,
                success=True,
                enabled=True,
                changed=database_changed,
                state=state,
                stage="retention_pending",
                message=(
                    f"CDC confirmado no banco {database}; retenção pendente até "
                    "a criação do job de cleanup."
                ),
                retention_minutes=None,
                retention_pending=True,
            )
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=database_changed,
            state=state,
            stage="retention_inspection",
            error_code="CDC_CLEANUP_JOB_NOT_FOUND",
            message=f"Job de cleanup do CDC não encontrado para o banco {database}.",
            permission_hint=_retention_permission_hint(),
            retention_minutes=None,
        )

    current = int(before["retention_minutes"])
    if current == retention_minutes:
        return DatabaseCdcResult(
            database=database,
            success=True,
            enabled=True,
            changed=database_changed,
            state=state,
            stage="retention_verification",
            message=(
                f"CDC confirmado no banco {database}; retenção do cleanup já configurada "
                f"para {retention_minutes} minutos."
            ),
            retention_minutes=retention_minutes,
            retention_changed=False,
        )

    change_error: BaseException | None = None
    try:
        execute(
            connection,
            CHANGE_RETENTION_SQL.format(database=qi(database)),
            (retention_minutes,),
        )
    except Exception as error:
        change_error = error

    # Verify even after an error so a concurrent, successful adjustment can be
    # accepted. The cleanup job is still restarted below so the confirmed
    # catalog value is effective before any BCP work begins.
    try:
        after = _one_row(connection, lookup_sql)
    except Exception as verification_error:
        cause = change_error or verification_error
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=database_changed,
            state=state,
            stage="retention_verification",
            error_code=_failure_code("CDC_RETENTION_VERIFICATION_FAILED", cause),
            message=(
                f"Não foi possível confirmar a retenção do CDC no banco {database}: "
                f"{redacted_exception(cause)}"
            ),
            permission_hint=_retention_permission_hint(),
            retention_minutes=None,
        )

    confirmed = int(after["retention_minutes"]) if after is not None else None
    if confirmed != retention_minutes:
        if change_error is not None:
            code = _failure_code("CDC_RETENTION_CHANGE_FAILED", change_error)
            detail = redacted_exception(change_error)
        elif after is None:
            code = "CDC_CLEANUP_JOB_NOT_FOUND"
            detail = "o job de cleanup não foi encontrado na verificação"
        else:
            code = "CDC_RETENTION_NOT_CONFIRMED"
            detail = f"o catálogo permaneceu com retention={confirmed}"
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=database_changed,
            state=state,
            stage="retention_verification",
            error_code=code,
            message=f"Falha ao ajustar a retenção do CDC no banco {database}: {detail}.",
            permission_hint=_retention_permission_hint(),
            retention_minutes=confirmed,
        )

    # SQL Server reads the new configuration when the Agent job starts. The
    # documented stop/start is therefore part of the postcondition, not a
    # best-effort side effect. Always attempt START even if STOP reports an
    # error, so a job that was already stopped is not left unavailable.
    stop_error: BaseException | None = None
    start_error: BaseException | None = None
    try:
        execute(connection, STOP_CLEANUP_JOB_SQL.format(database=qi(database)))
    except Exception as error:
        stop_error = error
    start_sql = START_CLEANUP_JOB_SQL.format(database=qi(database))
    for attempt in range(31):
        try:
            execute(connection, start_sql)
            start_error = None
            break
        except Exception as error:
            start_error = error
            # Permission failures cannot heal by waiting. Other SQL Agent
            # failures can be transient while a stop/start request is queued;
            # retry for at most 30 seconds before failing closed.
            if _permission_failure(error) or attempt >= 30:
                break
            time.sleep(1)

    if start_error is not None:
        cause = start_error
        stop_detail = (
            f"; falha anterior ao parar o job: {redacted_exception(stop_error)}"
            if stop_error is not None
            else ""
        )
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=database_changed or change_error is None,
            state=state,
            stage="retention_restart",
            error_code=_failure_code("CDC_RETENTION_JOB_RESTART_FAILED", cause),
            message=(
                f"A retenção do CDC foi gravada, mas o job de cleanup não pôde "
                f"ser reiniciado no banco {database}: {redacted_exception(cause)}"
                f"{stop_detail}."
            ),
            permission_hint=_retention_permission_hint(),
            retention_minutes=confirmed,
            retention_changed=change_error is None,
        )

    try:
        final = _one_row(connection, lookup_sql)
    except Exception as verification_error:
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=True,
            state=state,
            stage="retention_verification",
            error_code=_failure_code(
                "CDC_RETENTION_VERIFICATION_FAILED", verification_error
            ),
            message=(
                f"O job de cleanup foi reiniciado, mas não foi possível confirmar "
                f"a retenção do CDC no banco {database}: "
                f"{redacted_exception(verification_error)}"
            ),
            permission_hint=_retention_permission_hint(),
            retention_minutes=None,
            retention_changed=change_error is None,
            retention_restarted=True,
        )
    final_value = int(final["retention_minutes"]) if final is not None else None
    if final_value != retention_minutes:
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=True,
            changed=True,
            state=state,
            stage="retention_verification",
            error_code="CDC_RETENTION_NOT_CONFIRMED",
            message=(
                f"O job de cleanup foi reiniciado, mas a retenção final do CDC "
                f"no banco {database} permaneceu em {final_value}."
            ),
            permission_hint=_retention_permission_hint(),
            retention_minutes=final_value,
            retention_changed=change_error is None,
            retention_restarted=True,
        )
    return DatabaseCdcResult(
        database=database,
        success=True,
        enabled=True,
        changed=True,
        state=state,
        stage="retention_verification",
        message=(
            f"CDC confirmado no banco {database}; retenção do cleanup ajustada "
            f"para {retention_minutes} minutos e job reiniciado."
        ),
        retention_minutes=retention_minutes,
        retention_changed=change_error is None,
        retention_restarted=True,
    )


def ensure_database_cdc(
    connection: Any,
    *,
    database: str,
    retention_minutes: int = DEFAULT_CDC_RETENTION_MINUTES,
    defer_missing_cleanup_job: bool = False,
) -> DatabaseCdcResult:
    """Ensure and verify CDC at database level without leaking SQL failures.

    The caller should invoke this function at most once per source database in
    an execution.  Identifiers are validated/quoted; data values use DB-API
    parameters.
    """

    validate_sql_identifier(database, "database")
    retention_minutes = _validate_retention_minutes(retention_minutes)
    try:
        before = _database_state(connection, database)
    except Exception as error:
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=False,
            changed=False,
            state=None,
            stage="database_inspection",
            error_code=_failure_code("CDC_DATABASE_INSPECTION_FAILED", error),
            message=f"Não foi possível consultar o CDC do banco {database}: {redacted_exception(error)}",
            permission_hint=_database_permission_hint(),
        )

    if before is None:
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=False,
            changed=False,
            state=None,
            stage="database_inspection",
            error_code="CDC_DATABASE_NOT_FOUND",
            message=f"Banco de origem não encontrado ou não visível: {database}.",
        )

    state = str(before.get("state") or "")
    if state.upper() != "ONLINE":
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=bool(before.get("is_cdc_enabled")),
            changed=False,
            state=state or None,
            stage="database_inspection",
            error_code="CDC_DATABASE_NOT_ONLINE",
            message=f"O banco de origem {database} não está ONLINE; estado atual: {state or '<desconhecido>'}.",
        )

    if bool(before.get("is_cdc_enabled")):
        return _ensure_cleanup_retention(
            connection,
            database=database,
            state=state,
            database_changed=False,
            retention_minutes=retention_minutes,
            allow_missing_job=defer_missing_cleanup_job,
        )

    activation_error: BaseException | None = None
    try:
        execute(connection, ENABLE_DATABASE_SQL.format(database=qi(database)))
    except Exception as error:
        activation_error = error

    # Always verify after an activation attempt.  This accepts the benign race
    # where another worker enabled CDC between inspection and activation.
    try:
        after = _database_state(connection, database)
    except Exception as verification_error:
        cause = activation_error or verification_error
        return DatabaseCdcResult(
            database=database,
            success=False,
            enabled=False,
            changed=False,
            state=state,
            stage="database_verification",
            error_code=_failure_code("CDC_DATABASE_VERIFICATION_FAILED", cause),
            message=(
                f"Não foi possível confirmar o CDC no banco {database}: "
                f"{redacted_exception(cause)}"
            ),
            permission_hint=_database_permission_hint(),
        )

    confirmed = bool(after and after.get("is_cdc_enabled"))
    confirmed_state = str(after.get("state") or state) if after else state
    if confirmed:
        return _ensure_cleanup_retention(
            connection,
            database=database,
            state=confirmed_state,
            database_changed=activation_error is None,
            retention_minutes=retention_minutes,
            allow_missing_job=defer_missing_cleanup_job,
        )

    if activation_error is not None:
        code = _failure_code("CDC_DATABASE_ENABLE_FAILED", activation_error)
        detail = redacted_exception(activation_error)
    else:
        code = "CDC_DATABASE_ENABLE_NOT_CONFIRMED"
        detail = "o catálogo permaneceu com is_cdc_enabled=0"
    return DatabaseCdcResult(
        database=database,
        success=False,
        enabled=False,
        changed=False,
        state=confirmed_state or None,
        stage="database_verification",
        error_code=code,
        message=f"Falha ao habilitar CDC no banco {database}: {detail}.",
        permission_hint=_database_permission_hint(),
    )


def ensure_table_cdc(
    connection: Any,
    *,
    database: str,
    schema: str,
    table: str,
) -> TableCdcResult:
    """Ensure and verify CDC for one source table.

    This function intentionally does not enable CDC at database level.  Call
    :func:`ensure_database_cdc` once first.  A table without a primary key is
    enabled with ``supports_net_changes=0``, matching the supplied SQL script.
    """

    validate_sql_identifier(database, "database")
    validate_sql_identifier(schema, "schema")
    validate_sql_identifier(table, "table")
    try:
        before = _table_state(connection, database, schema, table)
    except Exception as error:
        return TableCdcResult(
            database=database,
            schema=schema,
            table=table,
            success=False,
            enabled=False,
            changed=False,
            supports_net_changes=None,
            stage="table_inspection",
            error_code=_failure_code("CDC_TABLE_INSPECTION_FAILED", error),
            message=(
                f"Não foi possível consultar o CDC da tabela "
                f"{database}.{schema}.{table}: {redacted_exception(error)}"
            ),
            permission_hint=_table_permission_hint(),
        )

    if before is None:
        return TableCdcResult(
            database=database,
            schema=schema,
            table=table,
            success=False,
            enabled=False,
            changed=False,
            supports_net_changes=None,
            stage="table_inspection",
            error_code="CDC_TABLE_NOT_FOUND",
            message=f"Tabela de origem não encontrada: {database}.{schema}.{table}.",
        )

    supports_net_changes = bool(before.get("supports_net_changes"))
    if bool(before.get("is_tracked_by_cdc")):
        return TableCdcResult(
            database=database,
            schema=schema,
            table=table,
            success=True,
            enabled=True,
            changed=False,
            supports_net_changes=supports_net_changes,
            stage="table_verification",
            message=f"CDC já estava habilitado na tabela {schema}.{table}.",
        )

    activation_error: BaseException | None = None
    try:
        execute(
            connection,
            ENABLE_TABLE_SQL.format(database=qi(database)),
            (schema, table, int(supports_net_changes)),
        )
    except Exception as error:
        activation_error = error

    try:
        after = _table_state(connection, database, schema, table)
    except Exception as verification_error:
        cause = activation_error or verification_error
        return TableCdcResult(
            database=database,
            schema=schema,
            table=table,
            success=False,
            enabled=False,
            changed=False,
            supports_net_changes=supports_net_changes,
            stage="table_verification",
            error_code=_failure_code("CDC_TABLE_VERIFICATION_FAILED", cause),
            message=(
                f"Não foi possível confirmar o CDC da tabela "
                f"{database}.{schema}.{table}: {redacted_exception(cause)}"
            ),
            permission_hint=_table_permission_hint(),
        )

    confirmed = bool(after and after.get("is_tracked_by_cdc"))
    if confirmed:
        return TableCdcResult(
            database=database,
            schema=schema,
            table=table,
            success=True,
            enabled=True,
            changed=activation_error is None,
            supports_net_changes=supports_net_changes,
            stage="table_verification",
            message=f"CDC habilitado e confirmado na tabela {schema}.{table}.",
        )

    if activation_error is not None:
        code = _failure_code("CDC_TABLE_ENABLE_FAILED", activation_error)
        detail = redacted_exception(activation_error)
    else:
        code = "CDC_TABLE_ENABLE_NOT_CONFIRMED"
        detail = "o catálogo permaneceu com is_tracked_by_cdc=0"
    return TableCdcResult(
        database=database,
        schema=schema,
        table=table,
        success=False,
        enabled=False,
        changed=False,
        supports_net_changes=supports_net_changes,
        stage="table_verification",
        error_code=code,
        message=f"Falha ao habilitar CDC na tabela {database}.{schema}.{table}: {detail}.",
        permission_hint=_table_permission_hint(),
    )


class CdcCoordinator:
    """Single-source execution coordinator that runs database preflight once."""

    def __init__(
        self,
        *,
        retention_minutes: int = DEFAULT_CDC_RETENTION_MINUTES,
    ) -> None:
        self._retention_minutes = _validate_retention_minutes(retention_minutes)
        self._database_results: dict[str, DatabaseCdcResult] = {}

    def ensure_database(self, connection: Any, *, database: str) -> DatabaseCdcResult:
        validate_sql_identifier(database, "database")
        key = database.casefold()
        result = self._database_results.get(key)
        if result is None:
            result = ensure_database_cdc(
                connection,
                database=database,
                retention_minutes=self._retention_minutes,
                defer_missing_cleanup_job=True,
            )
            self._database_results[key] = result
        return result

    def preflight(
        self,
        connection: Any,
        *,
        database: str,
        tables: Iterable[Mapping[str, Any]],
    ) -> DatabaseCdcResult | None:
        """Run database CDC only when at least one table explicitly requests it."""

        if not any(table.get("enable_cdc") is True for table in tables):
            return None
        return self.ensure_database(connection, database=database)

    def ensure_table(
        self,
        connection: Any,
        *,
        database: str,
        schema: str,
        table: str,
    ) -> TableCdcResult:
        validate_sql_identifier(database, "database")
        validate_sql_identifier(schema, "schema")
        validate_sql_identifier(table, "table")
        database_result = self.ensure_database(connection, database=database)
        if not database_result.success:
            return TableCdcResult(
                database=database,
                schema=schema,
                table=table,
                success=False,
                enabled=False,
                changed=False,
                supports_net_changes=None,
                stage="database_preflight",
                error_code=database_result.error_code,
                message=database_result.message,
                permission_hint=database_result.permission_hint,
            )
        table_result = ensure_table_cdc(
            connection,
            database=database,
            schema=schema,
            table=table,
        )
        if not table_result.success or not database_result.retention_pending:
            return table_result

        retention_result = _ensure_cleanup_retention(
            connection,
            database=database,
            state=database_result.state or "ONLINE",
            database_changed=database_result.changed,
            retention_minutes=self._retention_minutes,
        )
        self._database_results[database.casefold()] = retention_result
        if not retention_result.success:
            return TableCdcResult(
                database=database,
                schema=schema,
                table=table,
                success=False,
                enabled=table_result.enabled,
                changed=table_result.changed,
                supports_net_changes=table_result.supports_net_changes,
                stage=retention_result.stage,
                error_code=retention_result.error_code,
                message=retention_result.message,
                permission_hint=retention_result.permission_hint,
            retention_minutes=retention_result.retention_minutes,
            retention_restarted=retention_result.retention_restarted,
        )
        return replace(
            table_result,
            message=(
                f"{table_result.message or ''} "
                f"Retenção do cleanup confirmada em "
                f"{retention_result.retention_minutes} minutos."
            ).strip(),
            retention_minutes=retention_result.retention_minutes,
            retention_changed=retention_result.retention_changed,
            retention_restarted=retention_result.retention_restarted,
        )
