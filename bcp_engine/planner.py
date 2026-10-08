"""Planejamento somente-leitura do BulkFlow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping, Sequence

from .catalog import (
    DirectKeylessCountUnavailableError,
    DirectKeylessLimitExceededError,
    InvalidWatermarkError,
    NoEligibleWatermarkError,
    WatermarkContainsNullError,
    WatermarkIndexRequiredError,
    WatermarkSelection,
    discover_columns,
    discover_watermark,
)
from .config import effective_delete_confirmed_files, effective_tables
from .ddl import CatalogState, analyze_schema_evolution, compare_catalog
from .estimates import (
    METADATA_ROW_COUNT_METHOD,
    SAMPLE_METHOD,
    SAMPLE_UNCERTAINTY,
    RowCountEstimate,
    RowSizeEstimate,
    calculate_space_plan,
    estimate_row_count,
    estimate_row_size,
)
from .inspection import inspect_destination_catalog
from .models import TableStatus
from .profiles import TableLayout, resolve_profile
from .projection import build_import_plan
from .source import validate_source_columns
from .sql import fetch_dicts
from .util import redacted_exception


@dataclass(frozen=True)
class SourceDatabaseStorage:
    data_used_bytes: int | None
    data_allocated_bytes: int | None
    log_allocated_bytes: int | None


@dataclass(frozen=True)
class DestinationSpaceAssessment:
    """Per-volume evidence; ``available_bytes`` is the limiting minimum."""

    required_bytes: int
    available_bytes: int | None
    sufficient: bool | None
    volumes: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass
class TablePlan:
    source: dict[str, Any]
    destination: dict[str, Any] | None
    status: str
    reason: str | None = None
    execution_allowed: bool = True
    availability_policy: str = "stop"
    batching_strategy: str | None = None
    transfer_mode: str = "KEYSET"
    source_rows_at_capture: int | None = None
    watermark_source: str | None = None
    watermark_index: str | None = None
    watermark_unique: bool | None = None
    watermark: list[dict[str, Any]] = field(default_factory=list)
    estimated_rows: int | None = None
    row_count_method: str | None = None
    row_count_approximate: bool | None = None
    row_count_observed_at: str | None = None
    average_row_bytes: float | None = None
    sampled_rows: int | None = None
    sample_limit: int | None = None
    sample_method: str | None = None
    sample_observed_at: str | None = None
    sample_uncertainty: str | None = None
    estimated_bcp_total_bytes: int | None = None
    estimated_bcp_block_bytes: int | None = None
    approximate_blocks: int | None = None
    safety_factor: float = 1.25
    planning_reserve_bytes: int | None = None
    predicted_peak_bytes: int | None = None
    peak_model: str | None = None
    executor_free_bytes: int | None = None
    minimum_free_bytes: int = 0
    capacity_ok: bool | None = None
    source_database_used_bytes: int | None = None
    source_database_allocated_bytes: int | None = None
    source_database_log_allocated_bytes: int | None = None
    destination_data_bytes: int | None = None
    destination_log_bytes: int | None = None
    destination_catalog_state: str | None = None
    destination_catalog_errors: list[str] = field(default_factory=list)
    destination_missing_objects: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanningResult:
    generated_at: str
    executor_directory: str
    executor_free_bytes: int | None
    tables: list[TablePlan]
    destination_inspection_requested: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "executor_directory": self.executor_directory,
            "executor_free_bytes": self.executor_free_bytes,
            "destination_inspection_requested": self.destination_inspection_requested,
            "warnings": list(self.warnings),
            "tables": [table.as_dict() for table in self.tables],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inspect_source_database_storage(connection: Any) -> SourceDatabaseStorage:
    """Mede alocacao/uso do banco; nao estima bytes dos arquivos BCP."""

    rows = fetch_dicts(
        connection,
        """
SELECT
 SUM(CASE WHEN df.type = 0
          THEN CONVERT(bigint, FILEPROPERTY(df.name, 'SpaceUsed')) * 8192 END)
     AS data_used_bytes,
 SUM(CASE WHEN df.type = 0 THEN CONVERT(bigint, df.size) * 8192 END)
     AS data_allocated_bytes,
 SUM(CASE WHEN df.type = 1 THEN CONVERT(bigint, df.size) * 8192 END)
     AS log_allocated_bytes
FROM sys.database_files AS df;
""",
    )
    row = rows[0] if rows else {}

    def optional(name: str) -> int | None:
        return None if row.get(name) is None else int(row[name])

    return SourceDatabaseStorage(
        data_used_bytes=optional("data_used_bytes"),
        data_allocated_bytes=optional("data_allocated_bytes"),
        log_allocated_bytes=optional("log_allocated_bytes"),
    )


def inspect_destination_space(
    connection: Any,
    required_bytes: int,
) -> DestinationSpaceAssessment:
    """Check every distinct volume used by destination data and log files.

    ``sys.dm_os_volume_stats`` is authoritative for the host filesystem.  The
    check is deliberately read-only. Data and log volumes are not
    interchangeable, so the limiting capacity is the smallest free-space value
    and every volume must independently satisfy ``required_bytes``. Repeated
    mount points retain the smallest observation. Lack of permission or a NULL
    observation is reported as unknown rather than incorrectly claiming that
    capacity exists.
    """

    if required_bytes < 0:
        raise ValueError("required_bytes nao pode ser negativo")
    try:
        rows = fetch_dicts(
            connection,
            """
SELECT DISTINCT
       CONVERT(nvarchar(512), vs.volume_mount_point) AS volume_mount_point,
       CONVERT(bigint, vs.available_bytes) AS available_bytes
FROM sys.database_files AS df
CROSS APPLY sys.dm_os_volume_stats(DB_ID(), df.file_id) AS vs;
""",
        )
        by_mount: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            mount = str(row.get("volume_mount_point") or "<unknown>")
            windows_mount = (
                len(mount) >= 2 and mount[0].isalpha() and mount[1] == ":"
            ) or mount.startswith("\\\\")
            key = (
                ("windows", mount.casefold())
                if windows_mount
                else ("case-sensitive", mount)
            )
            value = row.get("available_bytes")
            observed = None if value is None else int(value)
            current = by_mount.get(key)
            if current is None:
                by_mount[key] = {
                    "volume_mount_point": mount,
                    "available_bytes": observed,
                }
            elif current["available_bytes"] is None or observed is None:
                current["available_bytes"] = None
            else:
                current["available_bytes"] = min(
                    int(current["available_bytes"]), observed
                )
        volumes = list(by_mount.values())
        if not volumes:
            return DestinationSpaceAssessment(
                required_bytes, None, None, (), "DESTINATION_SPACE_UNAVAILABLE"
            )
        for item in volumes:
            available = item["available_bytes"]
            item["sufficient"] = (
                None if available is None else int(available) >= required_bytes
            )
        if any(item["available_bytes"] is None for item in volumes):
            return DestinationSpaceAssessment(
                required_bytes,
                None,
                None,
                tuple(volumes),
                "DESTINATION_SPACE_UNAVAILABLE",
            )
        limiting_available = min(int(item["available_bytes"]) for item in volumes)
        return DestinationSpaceAssessment(
            required_bytes,
            limiting_available,
            all(bool(item["sufficient"]) for item in volumes),
            tuple(volumes),
        )
    except Exception as exc:
        return DestinationSpaceAssessment(
            required_bytes,
            None,
            None,
            (),
            redacted_exception(exc),
        )


def _read_free_bytes(
    directory: str | Path,
    provider: Callable[[str | Path], Any],
) -> int | None:
    try:
        value = provider(directory)
        free = value.free if hasattr(value, "free") else value
        return None if free is None else int(free)
    except Exception:
        return None


def _unavailable_row_count(method: str, error: Exception) -> RowCountEstimate:
    if method != "metadata":
        raise ValueError("row_count_method deve ser metadata")
    return RowCountEstimate(
        value=None,
        method=METADATA_ROW_COUNT_METHOD,
        observed_at=_utc_now(),
        approximate=method == "metadata",
        uncertainty="indisponivel",
        error=redacted_exception(error),
    )


def _unavailable_row_size(limit: int, error: Exception) -> RowSizeEstimate:
    return RowSizeEstimate(
        average_bytes=None,
        minimum_bytes=None,
        maximum_bytes=None,
        sampled_rows=0,
        sample_limit=limit,
        method=SAMPLE_METHOD,
        observed_at=_utc_now(),
        uncertainty="indisponivel; " + SAMPLE_UNCERTAINTY,
        error=redacted_exception(error),
    )


def _watermark_payload(selection: WatermarkSelection) -> list[dict[str, Any]]:
    return [
        {
            "name": column.name,
            "direction": "DESC" if column.descending else "ASC",
            "sql_type": column.type_name,
            "precision": column.precision,
            "scale": column.scale,
            "collation": column.collation_name,
        }
        for column in selection.columns
    ]


def _failure_plan(
    table: Mapping[str, Any],
    status: str,
    reason: str,
    *,
    policy: str,
    free_bytes: int | None,
    minimum_free: int,
    storage: SourceDatabaseStorage,
    warnings: Sequence[str] = (),
) -> TablePlan:
    return TablePlan(
        source=dict(table["source"]),
        destination=dict(table["destination"]) if table.get("destination") else None,
        status=status,
        reason=reason,
        execution_allowed=False,
        availability_policy=policy,
        executor_free_bytes=free_bytes,
        minimum_free_bytes=minimum_free,
        source_database_used_bytes=storage.data_used_bytes,
        source_database_allocated_bytes=storage.data_allocated_bytes,
        source_database_log_allocated_bytes=storage.log_allocated_bytes,
        warnings=list(warnings),
    )


def build_plan(
    config: Mapping[str, Any],
    source_connection: Any,
    *,
    destination_connections: Mapping[str, Any] | None = None,
    disk_free_provider: Callable[[str | Path], Any] = shutil.disk_usage,
    columns_discoverer: Callable[..., list[dict[str, Any]]] = discover_columns,
    watermark_discoverer: Callable[..., WatermarkSelection] = discover_watermark,
    row_count_estimator: Callable[..., RowCountEstimate] = estimate_row_count,
    row_size_estimator: Callable[..., RowSizeEstimate] = estimate_row_size,
    source_storage_inspector: Callable[[Any], SourceDatabaseStorage] = inspect_source_database_storage,
    destination_inspector: Callable[..., dict[str, Any]] = inspect_destination_catalog,
    profile_resolver: Callable[..., Any] = resolve_profile,
    catalog_comparer: Callable[..., Any] = compare_catalog,
    profile_directory: Path | None = None,
) -> PlanningResult:
    """Produz panorama individual sem executar DDL/DML.

    ``destination_connections`` e opcional. Sua ausencia nunca dispara criacao
    de conexao, resolucao de segredo ou tentativa de acesso ao destino.
    """

    tables = effective_tables(config)
    directory = str(config["executor_directory"])
    free_bytes = _read_free_bytes(directory, disk_free_provider)
    global_warnings: list[str] = []
    if free_bytes is None:
        global_warnings.append("EXECUTOR_SPACE_UNAVAILABLE")
    try:
        source_storage = source_storage_inspector(source_connection)
    except Exception as exc:
        source_storage = SourceDatabaseStorage(None, None, None)
        global_warnings.append(
            "SOURCE_DATABASE_SPACE_UNAVAILABLE: " + redacted_exception(exc)
        )

    policy = str(config["estimates"]["on_unavailable"])
    minimum_free = int(config["minimum_free_space_bytes"])
    retain_all = not effective_delete_confirmed_files(config)
    retained_planning_bytes: int | None = 0
    results: list[TablePlan] = []

    for table in tables:
        source = table["source"]
        destination = table["destination"]
        try:
            source_columns = columns_discoverer(
                source_connection, source["schema"], source["table"]
            )
            validate_source_columns(
                source_columns,
                metadata_mapping=table.get("metadata_mapping"),
            )
            selection = watermark_discoverer(
                source_connection,
                source["schema"],
                source["table"],
                explicit=table.get("watermark"),
                require_index=bool(config["batching"]["require_watermark_index"]),
                columns=source_columns,
                direct_keyless_row_limit=int(
                    config.get("keyless_direct_load_max_rows", 5_000_000)
                ),
            )
        except DirectKeylessLimitExceededError as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.SKIPPED_DIRECT_LIMIT.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue
        except DirectKeylessCountUnavailableError as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.SKIPPED_DIRECT_COUNT_UNAVAILABLE.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue
        except NoEligibleWatermarkError as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.SKIPPED_NO_KEY.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue
        except WatermarkContainsNullError as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.SKIPPED_NULL_WATERMARK.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue
        except (InvalidWatermarkError, WatermarkIndexRequiredError) as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.SKIPPED_INVALID_WATERMARK.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue
        except Exception as exc:
            results.append(
                _failure_plan(
                    table,
                    TableStatus.LAYOUT_ERROR.value,
                    redacted_exception(exc),
                    policy=policy,
                    free_bytes=free_bytes,
                    minimum_free=minimum_free,
                    storage=source_storage,
                )
            )
            continue

        count_method = "metadata"
        sample_limit = int(config["estimates"]["maximum_sample_rows"])
        if selection.is_direct_keyless:
            row_count = RowCountEstimate(
                value=selection.captured_row_count,
                method="Metadados aproximados para elegibilidade da carga direta sem chave",
                observed_at=_utc_now(),
                approximate=True,
                uncertainty="aproximada; o limite real e validado pelo rows_copied do BCP",
            )
        else:
            try:
                row_count = row_count_estimator(
                    source_connection,
                    source["schema"],
                    source["table"],
                    method=count_method,
                )
            except Exception as exc:
                row_count = _unavailable_row_count(count_method, exc)
        try:
            row_size = row_size_estimator(
                source_connection,
                source["schema"],
                source["table"],
                source_columns,
                sample_limit,
            )
        except Exception as exc:
            row_size = _unavailable_row_size(sample_limit, exc)

        space = calculate_space_plan(
            row_count.value,
            row_size.average_bytes,
            rows_per_block=(
                max(1, int(selection.captured_row_count or 0))
                if selection.is_direct_keyless
                else int(table["rows_per_block"])
            ),
            safety_factor=config["estimates"]["safety_factor"],
            execute_import=bool(config["execute_import"]),
            delete_confirmed=effective_delete_confirmed_files(config),
            retained_bytes=retained_planning_bytes or 0,
            available_bytes=free_bytes,
            minimum_free_bytes=minimum_free,
        )
        if retain_all and retained_planning_bytes is None:
            space = replace(space, predicted_peak_bytes=None, capacity_ok=None)

        warnings = list(selection.warnings)
        unavailable = False
        if row_count.value is None:
            warnings.append("ROW_COUNT_UNAVAILABLE")
            if row_count.error:
                warnings.append("ROW_COUNT_ERROR: " + redacted_exception(RuntimeError(row_count.error)))
            unavailable = True
        if row_count.value != 0 and row_size.average_bytes is None:
            warnings.append("AVERAGE_ROW_SIZE_UNAVAILABLE")
            if row_size.error:
                warnings.append("SAMPLE_ERROR: " + redacted_exception(RuntimeError(row_size.error)))
            unavailable = True
        if free_bytes is None:
            warnings.append("EXECUTOR_SPACE_UNAVAILABLE")
            unavailable = True
        if source_storage.data_used_bytes is None:
            warnings.append("SOURCE_DATABASE_SPACE_UNAVAILABLE")

        execution_allowed = not (unavailable and policy == "stop")
        if space.capacity_ok is False:
            warnings.append("INSUFFICIENT_EXECUTOR_SPACE")
            execution_allowed = False
        plan = TablePlan(
            source=dict(source),
            destination=dict(destination),
            status=TableStatus.PENDING.value,
            execution_allowed=execution_allowed,
            availability_policy=policy,
            batching_strategy=(
                "direct_keyless_single_block"
                if selection.is_direct_keyless
                else "keyset_complete_tie_group_with_initial_ceiling"
            ),
            transfer_mode=selection.transfer_mode,
            source_rows_at_capture=selection.captured_row_count,
            watermark_source=selection.source,
            watermark_index=selection.index_name,
            watermark_unique=selection.is_unique,
            watermark=_watermark_payload(selection),
            estimated_rows=row_count.value,
            row_count_method=row_count.method,
            row_count_approximate=row_count.approximate,
            row_count_observed_at=row_count.observed_at,
            average_row_bytes=(
                None if row_size.average_bytes is None else float(row_size.average_bytes)
            ),
            sampled_rows=row_size.sampled_rows,
            sample_limit=row_size.sample_limit,
            sample_method=row_size.method,
            sample_observed_at=row_size.observed_at,
            sample_uncertainty=row_size.uncertainty,
            estimated_bcp_total_bytes=space.estimated_bcp_total_bytes,
            estimated_bcp_block_bytes=space.estimated_bcp_block_bytes,
            approximate_blocks=space.approximate_blocks,
            safety_factor=float(config["estimates"]["safety_factor"]),
            planning_reserve_bytes=space.planning_reserve_bytes,
            predicted_peak_bytes=space.predicted_peak_bytes,
            peak_model=space.peak_model,
            executor_free_bytes=free_bytes,
            minimum_free_bytes=minimum_free,
            capacity_ok=space.capacity_ok,
            source_database_used_bytes=source_storage.data_used_bytes,
            source_database_allocated_bytes=source_storage.data_allocated_bytes,
            source_database_log_allocated_bytes=source_storage.log_allocated_bytes,
            destination_data_bytes=space.destination_data_bytes,
            destination_log_bytes=space.destination_log_bytes,
            warnings=warnings,
        )

        layout = None
        if destination.get("schema"):
            try:
                layout = profile_resolver(
                    destination["structure_profile"],
                    source_columns,
                    source_database=source["database"],
                    source_table=source["table"],
                    destination_schema=destination["schema"],
                    destination_table=destination["table"],
                    partition_column=table.get("partition_column"),
                    bronze_event_id=(
                        config.get("bronze_event_id")
                        if destination["area"] == "bronze"
                        else None
                    ),
                    directory=profile_directory,
                )
                profile_name = getattr(layout, "profile_name", destination["area"])
                if str(profile_name).casefold() != str(destination["area"]).casefold():
                    raise RuntimeError(
                        f"Perfil {profile_name!r} não corresponde à área "
                        f"de destino {destination['area']!r}."
                    )
                if isinstance(layout, TableLayout):
                    build_import_plan(
                        layout,
                        id_strategy=(
                            config.get("bronze_event_id")
                            if destination["area"] == "bronze"
                            else None
                        ),
                        metadata_mapping=table.get("metadata_mapping"),
                    )
            except Exception as exc:
                plan.status = TableStatus.LAYOUT_ERROR.value
                plan.reason = redacted_exception(exc)
                plan.execution_allowed = False
        else:
            plan.warnings.append("DESTINATION_WITHOUT_SCHEMA_NOT_INSPECTED")

        connections = destination_connections or {}
        destination_connection = connections.get(destination["area"])
        if layout is not None and destination_connection is not None:
            try:
                snapshot = destination_inspector(
                    destination_connection,
                    layout.schema,
                    layout.table,
                    sequence_name=layout.sequence.name if layout.sequence else None,
                )
                comparison = catalog_comparer(layout, snapshot)
                plan.destination_catalog_state = comparison.state.value
                plan.destination_catalog_errors = list(comparison.errors)
                plan.destination_missing_objects = list(comparison.missing_objects)
                plan.warnings.extend(comparison.warnings)
                if comparison.state is CatalogState.SCHEMA_EVOLUTION_PENDING:
                    evolution = analyze_schema_evolution(layout, snapshot)
                    if not bool(config.get("allow_schema_evolution", False)):
                        plan.status = TableStatus.SCHEMA_EVOLUTION_PENDING.value
                        plan.reason = (
                            "Evolução de schema pendente e allow_schema_evolution=false: "
                            + ", ".join(comparison.missing_business_columns)
                        )
                        plan.execution_allowed = False
                    elif not evolution.safe:
                        plan.status = TableStatus.LAYOUT_ERROR.value
                        plan.reason = (
                            "Evolução de schema insegura: colunas NOT NULL ausentes em "
                            "tabela povoada ou com contagem indisponível: "
                            + ", ".join(evolution.unsafe_not_null_columns)
                        )
                        plan.execution_allowed = False
                    else:
                        plan.warnings.append("ADDITIVE_SCHEMA_EVOLUTION_AUTHORIZED")
                elif comparison.state in {
                    CatalogState.INCOMPATIBLE,
                    CatalogState.CLUSTERED_CONFLICT,
                }:
                    plan.status = TableStatus.LAYOUT_ERROR.value
                    plan.reason = "; ".join(comparison.errors)
                    plan.execution_allowed = False
                elif (
                    not comparison.compatible
                    and not comparison.provisionable
                ):
                    plan.execution_allowed = False
                elif (
                    comparison.state is CatalogState.ABSENT
                    and not bool(config["create_structure_if_needed"])
                ):
                    plan.execution_allowed = False
                    plan.warnings.append("DESTINATION_MISSING_AND_CREATION_DISABLED")
            except Exception as exc:
                plan.destination_catalog_state = None
                plan.warnings.append(
                    "DESTINATION_INSPECTION_UNAVAILABLE: " + redacted_exception(exc)
                )
        elif layout is not None:
            plan.warnings.append("DESTINATION_NOT_INSPECTED")

        results.append(plan)
        if retain_all and plan.status == TableStatus.PENDING.value:
            if retained_planning_bytes is None or space.planning_reserve_bytes is None:
                retained_planning_bytes = None
            else:
                retained_planning_bytes += space.planning_reserve_bytes

    return PlanningResult(
        generated_at=_utc_now(),
        executor_directory=directory,
        executor_free_bytes=free_bytes,
        tables=results,
        destination_inspection_requested=bool(destination_connections),
        warnings=global_warnings,
    )


plan_tables = build_plan


__all__ = [
    "SourceDatabaseStorage",
    "DestinationSpaceAssessment",
    "TablePlan",
    "PlanningResult",
    "inspect_source_database_storage",
    "inspect_destination_space",
    "build_plan",
    "plan_tables",
]
