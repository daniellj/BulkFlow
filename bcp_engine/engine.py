"""Serviços de aplicação do BulkFlow - SQL Server Data Export & Load.

O orquestrador mantém a ordem estrita tabela -> bloco -> importação. A CLI é
apenas uma adaptação desses serviços e não contém regras de negócio.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from typing import Any, Callable, Iterable, Mapping

from .artifacts import ArtifactStore, read_bounded_json
from .auth import SecretResolver, authentication_identity, build_bcp_invocation
from .batching import (
    BatchWindow,
    DirectSourceDriftError,
    capture_ceiling,
    direct_keyless_export_query,
    direct_keyless_window,
    export_query,
    observe_direct_keyless_export,
    observe_export,
    plan_next_batch,
)
from .bcp import BcpRunner, make_format_file
from .catalog import (
    DirectKeylessCountUnavailableError,
    DirectKeylessLimitExceededError,
    InvalidWatermarkError,
    NoEligibleWatermarkError,
    WatermarkContainsNullError,
    WatermarkIndexRequiredError,
    discover_columns,
    discover_watermark,
)
from .cdc import CdcCoordinator, DEFAULT_CDC_RETENTION_MINUTES, TableCdcResult
from .config import (
    active_destination,
    effective_delete_confirmed_files,
    effective_tables,
    operational_fingerprint,
    structural_fingerprint,
)
from .connections import ConnectionFactory
from .ddl import (
    CatalogState,
    DdlStage,
    SchemaEvolutionAnalysis,
    SchemaEvolutionRequiredError,
    SchemaEvolutionSafetyError,
    analyze_schema_evolution,
    build_apply_batches,
    build_schema_evolution_batches,
    compare_catalog,
    generate_ddl_script,
)
from .estimates import calculate_space_plan, estimate_row_count, estimate_row_size
from .importer import DestinationImporter
from .models import BlockManifest, ExecutionReport, TableResult, TableStatus
from .planner import DestinationSpaceAssessment, build_plan, inspect_destination_space
from .profiles import TableLayout, resolve_profile
from .projection import (
    build_import_plan,
    layout_contract,
    projection_contract,
)
from .reporting import finish_timestamp, render_plan_rows, write_reports
from .source import (
    database_identity,
    source_fingerprint,
    validate_origin_id_column,
    validate_source_columns,
    validate_source_table,
)
from .sql import close_all, execute, scalar
from .state import LocalState, StructuralResumeError, utc_now
from .util import (
    DEFAULT_REDACTOR,
    digest,
    qi,
    redacted_exception,
    stable_json,
    validate_canonical_uuid,
)


EventSink = Callable[[str, Mapping[str, Any]], None]


class _CdcTableSkip(RuntimeError):
    """Controlled per-table stop after CDC could not be confirmed."""

    def __init__(self, result: TableCdcResult) -> None:
        self.result = result
        super().__init__(result.message or result.error_code or "CDC activation failed")


class _DestinationSpaceSkip(RuntimeError):
    """Controlled per-table stop when destination capacity is insufficient."""

    def __init__(self, assessment: DestinationSpaceAssessment) -> None:
        self.assessment = assessment
        super().__init__(
            "DESTINATION_INSUFFICIENT_SPACE: "
            f"necessario={assessment.required_bytes}; "
            f"disponivel={assessment.available_bytes}"
        )


def _noop_event(_name: str, _payload: Mapping[str, Any]) -> None:
    pass


def _table_id(table: Mapping[str, Any]) -> str:
    destination = table["destination"]
    # O identificador acompanha o dataset/logica de projecao, nao o endereco
    # fisico em que ele sera importado. Assim, trocar servidor, banco ou schema
    # operacional nao quebra checkpoints nem manifestos validos.
    return digest({
        "logical_source": {
            name: table["source"].get(name)
            for name in ("database", "read_database", "schema", "table")
        },
        "logical_destination": {
            "area": destination.get("area"),
            "table": destination.get("table"),
        },
        "watermark": table.get("watermark"),
        "metadata_mapping": table.get("metadata_mapping", {}),
    })[:64]


def _profile_path(profile: str, config_directory: Path) -> tuple[str, Path | None]:
    path = Path(profile)
    if path.is_absolute():
        return str(path), None
    return profile, config_directory


def _watermark_manifest(selection: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": column.name,
            "direction": "DESC" if column.descending else "ASC",
            "type_name": column.type_name,
            "collation": column.collation_name,
        }
        for column in selection.columns
    ]


def _is_direct_keyless(selection: Any) -> bool:
    return bool(getattr(selection, "is_direct_keyless", False))


def _transfer_mode(selection: Any) -> str:
    return str(getattr(selection, "transfer_mode", "KEYSET"))


def _captured_row_count(selection: Any) -> int | None:
    value = getattr(selection, "captured_row_count", None)
    return None if value is None else int(value)


def _state_transfer_contract(selection: Any) -> Any:
    watermark = _watermark_manifest(selection)
    if not _is_direct_keyless(selection):
        return watermark
    return {
        "transfer_mode": "DIRECT_KEYLESS",
        "source_rows_at_capture": _captured_row_count(selection),
        "watermark": [],
    }


def _manifest_state_transfer_contract(manifest: BlockManifest) -> Any:
    if getattr(manifest, "transfer_mode", "KEYSET") != "DIRECT_KEYLESS":
        return list(manifest.watermark)
    return {
        "transfer_mode": getattr(manifest, "transfer_mode", "KEYSET"),
        "source_rows_at_capture": getattr(manifest, "source_rows_at_capture", None),
        "watermark": [],
    }


def _stored_direct_row_count(table_state: Mapping[str, Any] | None) -> int | None:
    if not table_state or table_state.get("watermark_json") is None:
        return None
    try:
        contract = json.loads(str(table_state["watermark_json"]))
    except (TypeError, ValueError) as exc:
        raise StructuralResumeError(
            "Contrato de transferencia do controle local nao e JSON valido"
        ) from exc
    if not isinstance(contract, dict) or contract.get("transfer_mode") != "DIRECT_KEYLESS":
        return None
    count = contract.get("source_rows_at_capture")
    if type(count) is not int or count < 0 or contract.get("watermark") != []:
        raise StructuralResumeError(
            "Contrato persistido da carga direta sem chave e invalido"
        )
    return count


def _prepared_table_is_empty(prepared: "PreparedTable", window: BatchWindow) -> bool:
    if _is_direct_keyless(prepared.watermark):
        # A contagem de metadados e aproximada e nunca autoriza pular o BCP.
        return False
    return not bool(window.ceiling)


def _safe_file_component(value: str) -> str:
    """Converte nome SQL em componente local sem permitir escape de diretório."""

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not safe:
        safe = "object"
    return safe[:80] + "_" + digest(value)[:12]


@dataclass
class PreparedTable:
    effective: dict[str, Any]
    table_id: str
    columns: list[dict[str, Any]]
    watermark: Any
    layout: TableLayout
    projection: dict[str, Any]
    layout_contract: dict[str, Any]
    table_structural_hash: str
    final_limit: tuple[str, ...] | None
    average_row_bytes: Decimal | None = None


@dataclass(frozen=True)
class ValidatedImport:
    path: Path
    manifest: BlockManifest
    effective: dict[str, Any]
    layout: TableLayout
    import_plan: dict[str, Any]


class BcpEngine:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        config_path: Path | None = None,
        resolver: SecretResolver | None = None,
        connection_factory: ConnectionFactory | None = None,
        bcp_runner: BcpRunner | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.config_directory = (config_path.parent if config_path else Path.cwd()).resolve()
        self.resolver = resolver or SecretResolver()
        self.connection_factory = connection_factory or ConnectionFactory(self.resolver)
        self.bcp_runner = bcp_runner or BcpRunner(
            windows_context_adapter=self.connection_factory.windows_adapter
        )
        self.event = event_sink or _noop_event

    def _connect_source(self) -> tuple[Any, Any]:
        return self.connection_factory.connect(
            self.config["source"], self.config, endpoint_name="source"
        )

    def _connect_destination(self, area: str | None = None) -> tuple[Any, Any]:
        selected = area or self.config["active_destination"]
        endpoint = self.config.get(selected + "_destination")
        if endpoint is None:
            raise RuntimeError(f"Destino {selected} não configurado")
        return self.connection_factory.connect(
            endpoint, self.config, endpoint_name=selected + "_destination"
        )

    @staticmethod
    def _connection_identity_payload(
        environment: str,
        endpoint: Mapping[str, Any],
        identity: Any,
    ) -> dict[str, Any]:
        payload = dict(identity.as_dict())
        authentication = endpoint.get("authentication", {})
        payload.update(
            {
                "environment": environment,
                "instance": endpoint.get("instance"),
                "port": endpoint.get("port", 1433),
                "database": endpoint.get("database"),
                "username": authentication.get("username")
                or payload.get("suser_sname")
                or payload.get("system_user"),
            }
        )
        return payload

    def _assert_bronze_data_route(self) -> None:
        """Fail closed before artifacts, state, connections, DDL, or DML.

        DLAN684 is a structure-only endpoint.  Keeping this guard in the
        service layer protects callers that construct ``BcpEngine`` directly
        instead of going through the validated CLI/GUI configuration path.
        """

        # ``bronze`` is also the V2 contract default.  Accepting an omitted
        # value keeps direct service-layer test/adaptor callers compatible;
        # any explicit non-Bronze route is rejected.
        if self.config.get("active_destination", "bronze") != "bronze":
            raise RuntimeError(
                "Carga de dados permitida somente para Bronze; Landing é somente estrutura"
            )
        for index, table in enumerate(self.config.get("tables", [])):
            if table.get("destination_area", "bronze") != "bronze":
                raise RuntimeError(
                    f"tables[{index}].destination_area deve ser bronze; "
                    "Landing é somente estrutura"
                )

    def _configured_table_for_manifest(self, manifest: BlockManifest) -> dict[str, Any]:
        """Resolve um manifesto para exatamente uma tabela efetiva da configuração.

        O ``table_id`` é a chave preferencial. A identidade completa da origem
        continua obrigatória para impedir que um manifesto seja direcionado a
        uma tabela apenas por coincidir nome ou posição na lista.
        """

        def same(left: Any, right: Any) -> bool:
            return str(left).casefold() == str(right).casefold()

        # O nome/alias da instância não participa da resolução: a importação
        # posterior pode ocorrer em outro executor e não abre a origem. O
        # objeto lógico de origem, entretanto, deve coincidir por inteiro.
        configured_items = self.config.get("tables")
        if not configured_items:
            raise RuntimeError(
                "Importação por manifesto exige mapeamento explícito em tables"
            )

        source_fields = ("database", "read_database", "schema", "table")
        candidates = [
            table
            for table in effective_tables(self.config)
            if all(
                field in manifest.source
                and same(manifest.source[field], table["source"][field])
                for field in source_fields
            )
        ]
        by_id = [table for table in candidates if _table_id(table) == manifest.table_id]
        if len(by_id) == 1:
            selected = by_id[0]
        else:
            raise RuntimeError(
                "Manifesto V2 não corresponde exatamente ao table_id e à origem configurados"
            )

        if manifest.destination is not None:
            expected = selected["destination"]
            # Instancia, banco e schema sao roteamento operacional. O contrato
            # dos bytes fixa area/perfil e nome logico da tabela, mas permite
            # que o operador importe em outro endpoint/schema compativel.
            for field in ("area", "table"):
                if field not in manifest.destination or not same(
                    manifest.destination[field], expected[field]
                ):
                    raise RuntimeError(
                        f"Destino do manifesto diverge do mapeamento configurado ({field})"
                    )
        return selected

    def _resolve_layout(
        self,
        table: Mapping[str, Any],
        columns: list[dict[str, Any]],
        *,
        area: str | None = None,
    ) -> TableLayout:
        target_area = area or str(table["destination"]["area"])
        endpoint = self.config.get(target_area + "_destination")
        profile_name = (
            table["destination"].get("structure_profile")
            if target_area == table["destination"]["area"]
            else endpoint.get("structure_profile", f"templates/{target_area}.json")
            if endpoint is not None
            else f"templates/{target_area}.json"
        )
        profile, directory = _profile_path(str(profile_name), self.config_directory)
        target_schema = (
            table["destination"].get("schema")
            if target_area == table["destination"]["area"]
            else endpoint["schema"] if endpoint is not None else None
        )
        if not target_schema:
            if self.config.get("execute_import", True):
                raise RuntimeError(
                    f"Esquema do destino {target_area} ausente; configure o endpoint ou destination_schema"
                )
            # Artefatos export-only nao dependem do roteamento fisico. O hash
            # de layout/projecao exclui este marcador e a importacao posterior
            # resolve o esquema no mapeamento de destino configurado.
            target_schema = "__artifact_without_destination__"
        layout = resolve_profile(
            profile,
            columns,
            source_database=table["source"]["database"],
            source_table=table["source"]["table"],
            destination_schema=target_schema,
            destination_table=table["destination"]["table"],
            partition_column=table.get("partition_column"),
            directory=directory,
            bronze_event_id=(
                self.config.get("bronze_event_id")
                if target_area == "bronze"
                else None
            ),
        )
        if layout.profile_name.casefold() != target_area.casefold():
            raise RuntimeError(
                f"O perfil {layout.profile_name!r} não corresponde à área "
                f"de destino {target_area!r}."
            )
        return layout

    def prepare_table(
        self,
        source: Any,
        table: dict[str, Any],
        db_identity: dict[str, Any],
        *,
        direct_keyless_captured_row_count: int | None = None,
    ) -> PreparedTable:
        src = table["source"]
        table_identity = validate_source_table(source, src["schema"], src["table"])
        columns = discover_columns(source, src["schema"], src["table"])
        validate_source_columns(
            columns,
            metadata_mapping=table.get("metadata_mapping"),
        )
        watermark = discover_watermark(
            source,
            src["schema"],
            src["table"],
            table.get("watermark"),
            require_index=bool(self.config["batching"]["require_watermark_index"]),
            columns=columns,
            direct_keyless_row_limit=int(self.config["keyless_direct_load_max_rows"]),
            direct_keyless_captured_row_count=direct_keyless_captured_row_count,
        )
        layout = self._resolve_layout(table, columns)
        if layout.technical_id_strategy == "source_column" and layout.technical_id_source_column:
            validate_origin_id_column(
                source, src["schema"], src["table"], layout.technical_id_source_column
            )
        projection = projection_contract(layout, columns)
        physical = layout_contract(layout)
        actual_hash = digest({
            "configured": structural_fingerprint(self.config, table["index"]),
            "source": source_fingerprint(db_identity, table_identity, columns),
            "watermark": _watermark_manifest(watermark),
            "transfer_mode": _transfer_mode(watermark),
            "projection": projection["projection_hash"],
            "layout": physical["layout_hash"],
        })
        return PreparedTable(
            effective=table,
            table_id=_table_id(table),
            columns=columns,
            watermark=watermark,
            layout=layout,
            projection=projection,
            layout_contract=physical,
            table_structural_hash=actual_hash,
            final_limit=None,
        )

    def _table_plan(self, source: Any, prepared: PreparedTable, retained_bytes: int = 0) -> dict[str, Any]:
        from .planner import inspect_source_database_storage
        src, dst = prepared.effective["source"], prepared.effective["destination"]
        row_count = None
        if not _is_direct_keyless(prepared.watermark):
            row_count = estimate_row_count(
                source,
                src["schema"],
                src["table"],
                method="metadata",
            )
        row_count_value = (
            _captured_row_count(prepared.watermark)
            if _is_direct_keyless(prepared.watermark)
            else row_count.value
        )
        row_count_method = (
            "Metadados aproximados para elegibilidade da carga direta sem chave"
            if _is_direct_keyless(prepared.watermark)
            else row_count.method
        )
        row_count_error = None if row_count is None else row_count.error
        row_size = estimate_row_size(
            source, src["schema"], src["table"], prepared.columns,
            self.config["estimates"]["maximum_sample_rows"],
        )
        try:
            available = int(shutil.disk_usage(self.config["executor_directory"]).free)
        except OSError:
            available = None
        plan = calculate_space_plan(
            row_count_value,
            row_size.average_bytes,
            rows_per_block=(
                max(1, int(row_count_value or 0))
                if _is_direct_keyless(prepared.watermark)
                else int(prepared.effective["rows_per_block"])
            ),
            safety_factor=self.config["estimates"]["safety_factor"],
            execute_import=bool(self.config["execute_import"]),
            delete_confirmed=effective_delete_confirmed_files(self.config),
            retained_bytes=retained_bytes,
            available_bytes=available,
            minimum_free_bytes=int(self.config["minimum_free_space_bytes"]),
        )
        prepared.average_row_bytes = row_size.average_bytes
        try:
            storage = inspect_source_database_storage(source)
        except Exception:
            storage = None
        unavailable = row_count_value is None or (
            row_count_value != 0 and row_size.average_bytes is None
        ) or available is None
        execution_allowed = not (
            unavailable and self.config["estimates"].get("on_unavailable", "stop") == "stop"
        )
        if plan.capacity_ok is False:
            execution_allowed = False
        return {
            "source": f"{src['instance']}/{src['read_database']}.{src['schema']}.{src['table']}",
            "destination": (
                f"{dst['instance']}/{dst['database']}.{dst['schema']}.{dst['table']}"
                if self.config["execute_import"] else None
            ),
            "watermark_description": (
                "não aplicável (carga direta sem chave)"
                if _is_direct_keyless(prepared.watermark)
                else ", ".join(
                    column.name + (" DESC" if column.descending else " ASC")
                    for column in prepared.watermark.columns
                )
            ),
            "batch_strategy": (
                "bloco único direto, sem cursor ou ordenação inventada"
                if _is_direct_keyless(prepared.watermark)
                else "keyset com grupo completo de empates e teto inicial"
            ),
            "transfer_mode": _transfer_mode(prepared.watermark),
            "source_rows_at_capture": _captured_row_count(prepared.watermark),
            "estimated_rows": row_count_value,
            "row_count_method": row_count_method,
            "estimated_bcp_bytes": plan.estimated_bcp_total_bytes,
            "estimated_block_bytes": plan.estimated_bcp_block_bytes,
            "estimated_blocks": plan.approximate_blocks,
            "free_bytes": plan.available_bytes,
            "peak_bytes": plan.predicted_peak_bytes,
            "safety_factor": self.config["estimates"]["safety_factor"],
            "sample_rows": row_size.sampled_rows,
            "sample_method": row_size.method,
            "sampled_at": row_size.observed_at,
            "uncertainty": row_size.uncertainty,
            "source_database_bytes": storage.data_used_bytes if storage else None,
            "source_database_allocated_bytes": storage.data_allocated_bytes if storage else None,
            "source_database_log_bytes": storage.log_allocated_bytes if storage else None,
            "destination_data_bytes": None,
            "destination_log_bytes": None,
            "execution_allowed": execution_allowed,
            "availability_policy": self.config["estimates"].get("on_unavailable", "stop"),
            "warnings": [
                *prepared.watermark.warnings,
                *(filter(None, [row_count_error, row_size.error])),
                (
                    "Origem em escrita: carga direta bloqueia escritores durante a leitura BCP; "
                    "a pré-contagem por metadados não lê nem bloqueia a tabela."
                    if _is_direct_keyless(prepared.watermark)
                    else "Origem em escrita: consistência LIVE_BEST_EFFORT; o teto não constitui snapshot."
                ),
            ],
        }

    def plan(self) -> tuple[list[dict[str, Any]], list[TableResult]]:
        opened: list[Any] = []
        try:
            source, source_identity = self._connect_source()
            opened.append(source)
            self.event("identity", source_identity.as_dict())
            self.event(
                "connection_identity",
                self._connection_identity_payload(
                    "source", self.config["source"], source_identity
                ),
            )
            destination_connections: dict[str, Any] = {}
            # Planning is the explicit credential/connectivity checkpoint for
            # every configured environment, including structure-only Landing.
            for area in ("landing", "bronze"):
                endpoint = self.config.get(area + "_destination")
                if endpoint is None:
                    continue
                connection, identity = self._connect_destination(area)
                opened.append(connection)
                destination_connections[area] = connection
                if area == self.config.get("active_destination"):
                    self.event("destination_identity", identity.as_dict())
                self.event(
                    "connection_identity",
                    self._connection_identity_payload(area, endpoint, identity),
                )
            planning = build_plan(
                self.config,
                source,
                destination_connections=destination_connections or None,
                profile_directory=self.config_directory,
            )
            plans = [item.as_dict() for item in planning.tables]
            results = [
                TableResult(
                    source_table=str(item.source["table"]),
                    destination_table=(
                        str(item.destination["table"])
                        if item.destination is not None
                        and self.config["execute_import"]
                        else None
                    ),
                    status=item.status,
                    reason=item.reason,
                    estimated_rows=item.estimated_rows,
                    watermark=list(item.watermark),
                    transfer_mode=item.transfer_mode,
                    source_rows_at_capture=item.source_rows_at_capture,
                    warnings=list(item.warnings),
                )
                for item in planning.tables
            ]
            return plans, results
        finally:
            close_all(opened)

    def _schema_evolution_payload(
        self,
        area: str,
        layout: TableLayout,
        analysis: SchemaEvolutionAnalysis,
        *,
        action: str,
        catalog_after: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        endpoint = self.config.get(area + "_destination") or {}
        after_ordinals = {
            str(column.get("name", "")): int(
                column.get("column_id", position)
            )
            for position, column in enumerate(
                (catalog_after or {}).get("columns", ()), start=1
            )
        }
        return {
            "action": action,
            "area": area,
            "target_database": endpoint.get("database"),
            "target_schema": layout.schema,
            "target_table": layout.table,
            "allow_schema_evolution": bool(
                self.config.get("allow_schema_evolution", False)
            ),
            "layout_hash": layout_contract(layout)["layout_hash"],
            "observed_row_count": analysis.row_count,
            "physical_order_differs": bool(
                analysis.physical_order_differs
                or catalog_after is not None
                and tuple(
                    str(column.get("name", ""))
                for column in catalog_after.get("columns", ())
                )
                != tuple(column.name.casefold() for column in layout.columns)
            ),
            "columns": [
                {
                    "name": item.column.name,
                    "sql_type": item.column.sql_type,
                    "nullable": item.column.nullable,
                    "logical_ordinal": item.logical_ordinal,
                    "physical_ordinal_before": item.physical_ordinal_before,
                    "physical_ordinal_after": after_ordinals.get(item.column.name),
                }
                for item in analysis.missing_columns
            ],
            "reason": reason,
        }

    def _preflight_schema_evolution(
        self,
        *,
        area: str,
        layout: TableLayout,
        catalog: Mapping[str, Any],
        comparison: Any,
        emit_detected: bool = True,
    ) -> SchemaEvolutionAnalysis:
        analysis = analyze_schema_evolution(layout, catalog)
        if not analysis.required:
            return analysis
        if emit_detected:
            self.event(
                "schema_evolution_detected",
                self._schema_evolution_payload(
                    area, layout, analysis, action="detected"
                ),
            )
        if comparison.state in {
            CatalogState.INCOMPATIBLE,
            CatalogState.CLUSTERED_CONFLICT,
        }:
            reason = "layout_incompatible"
            self.event(
                "schema_evolution_blocked",
                self._schema_evolution_payload(
                    area, layout, analysis, action="blocked", reason=reason
                ),
            )
            raise RuntimeError(
                "Layout existente incompatível além das colunas de negócio ausentes: "
                + "; ".join(comparison.errors)
            )
        if not self.config.get("allow_schema_evolution", False):
            reason = "allow_schema_evolution_disabled"
            self.event(
                "schema_evolution_blocked",
                self._schema_evolution_payload(
                    area, layout, analysis, action="blocked", reason=reason
                ),
            )
            names = ", ".join(item.column.name for item in analysis.missing_columns)
            raise SchemaEvolutionRequiredError(
                "Evolução de schema pendente no layout de destino; "
                f"allow_schema_evolution=false. Colunas de negócio ausentes: {names}"
            )
        if not analysis.safe:
            reason = "not_null_column_on_populated_or_unknown_table"
            self.event(
                "schema_evolution_blocked",
                self._schema_evolution_payload(
                    area, layout, analysis, action="blocked", reason=reason
                ),
            )
            raise SchemaEvolutionSafetyError(
                "Evolução de schema insegura para o layout: tabela povoada (ou contagem "
                "indisponível) possui nova coluna NOT NULL sem default/backfill autorizado: "
                + ", ".join(analysis.unsafe_not_null_columns)
            )
        return analysis

    def _apply_schema_evolution(
        self,
        destination: Any,
        *,
        area: str,
        layout: TableLayout,
        catalog: Mapping[str, Any],
        comparison: Any,
        emit_detected: bool = True,
    ) -> tuple[Mapping[str, Any], Any]:
        from .inspection import inspect_layout_catalog

        analysis = self._preflight_schema_evolution(
            area=area,
            layout=layout,
            catalog=catalog,
            comparison=comparison,
            emit_detected=emit_detected,
        )
        if not analysis.required:
            return catalog, comparison
        try:
            for batch in build_schema_evolution_batches(layout):
                execute(destination, batch)
        except Exception:
            self.event(
                "schema_evolution_blocked",
                self._schema_evolution_payload(
                    area,
                    layout,
                    analysis,
                    action="blocked",
                    reason="sql_apply_failed",
                ),
            )
            raise
        catalog_after = inspect_layout_catalog(destination, layout)
        comparison_after = compare_catalog(layout, catalog_after)
        if comparison_after.state in {
            CatalogState.SCHEMA_EVOLUTION_PENDING,
            CatalogState.INCOMPATIBLE,
            CatalogState.CLUSTERED_CONFLICT,
        }:
            raise RuntimeError(
                "Evolução de schema aplicada, mas o layout permaneceu incompatível: "
                + "; ".join(
                    comparison_after.errors + comparison_after.missing_objects
                )
            )
        self.event(
            "schema_evolution_applied",
            self._schema_evolution_payload(
                area,
                layout,
                analysis,
                action="applied",
                catalog_after=catalog_after,
            ),
        )
        return catalog_after, comparison_after

    def generate_ddl(
        self,
        *,
        areas: Iterable[str],
        output_directory: Path,
        apply: bool = False,
    ) -> list[Path]:
        source = None
        destinations: dict[str, Any] = {}
        output_directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        try:
            source, _ = self._connect_source()
            db = database_identity(source)
            for area in areas:
                if area not in {"bronze", "landing"}:
                    raise ValueError(f"Área inválida: {area}")
                if self.config.get(area + "_destination") is None:
                    raise RuntimeError(f"{area}_destination não configurado")
                if apply:
                    destinations[area], _ = self._connect_destination(area)
                area_scripts: list[str] = []
                for table in effective_tables(self.config):
                    src = table["source"]
                    validate_source_table(source, src["schema"], src["table"])
                    columns = discover_columns(source, src["schema"], src["table"])
                    layout = self._resolve_layout(table, columns, area=area)
                    script = generate_ddl_script(
                        layout,
                        stage=DdlStage.FULL,
                        allow_schema_evolution=bool(
                            self.config.get("allow_schema_evolution", False)
                        ),
                    )
                    area_scripts.append(script)
                    path = output_directory / f"{area}_{_safe_file_component(layout.table)}.sql"
                    path.write_text(script, encoding="utf-8")
                    written.append(path)
                    if apply:
                        from .inspection import inspect_layout_catalog
                        catalog_before = inspect_layout_catalog(
                            destinations[area], layout
                        )
                        before = compare_catalog(layout, catalog_before)
                        catalog_before, before = self._apply_schema_evolution(
                            destinations[area],
                            area=area,
                            layout=layout,
                            catalog=catalog_before,
                            comparison=before,
                        )
                        if before.state in {
                            CatalogState.INCOMPATIBLE,
                            CatalogState.CLUSTERED_CONFLICT,
                        }:
                            raise RuntimeError(
                                f"Layout existente {area}.{layout.table} incompatível; "
                                "nenhuma mutação foi iniciada: "
                                + "; ".join(before.errors)
                            )
                        for batch in build_apply_batches(
                            layout,
                            stage=DdlStage.FULL,
                            allow_schema_evolution=bool(
                                self.config.get("allow_schema_evolution", False)
                            ),
                        ):
                            execute(destinations[area], batch)
                        comparison = compare_catalog(layout, inspect_layout_catalog(destinations[area], layout))
                        if comparison.state is not CatalogState.COMPATIBLE:
                            raise RuntimeError(
                                f"DDL aplicado mas layout {area}.{layout.table} não é exato: "
                                + "; ".join(comparison.errors + comparison.missing_objects)
                            )
                combined = output_directory / f"{area}_completo.sql"
                combined.write_text("\n".join(area_scripts), encoding="utf-8")
                written.append(combined)
            return written
        finally:
            close_all([source, *destinations.values()])

    def _provision_initial(
        self,
        destination: Any,
        layout: TableLayout,
        *,
        area: str | None = None,
        schema_preflight_logged: bool = False,
    ) -> None:
        from .inspection import inspect_layout_catalog
        target_area = area or layout.profile_name.casefold()
        include_secondary = (
            self.config["structure"]["secondary_indexes_phase"] == "before_load"
        )
        before = inspect_layout_catalog(destination, layout)
        comparison = compare_catalog(layout, before, include_secondary=include_secondary)
        before, comparison = self._apply_schema_evolution(
            destination,
            area=target_area,
            layout=layout,
            catalog=before,
            comparison=comparison,
            emit_detected=not schema_preflight_logged,
        )
        if comparison.state in {CatalogState.INCOMPATIBLE, CatalogState.CLUSTERED_CONFLICT}:
            raise RuntimeError("Layout existente incompatível: " + "; ".join(comparison.errors))
        if comparison.state in {CatalogState.ABSENT, CatalogState.OBJECTS_PENDING}:
            if not self.config["create_structure_if_needed"]:
                raise RuntimeError("Estrutura ausente/incompleta e criação automática desabilitada")
            for batch in build_apply_batches(
                layout,
                stage=DdlStage.INITIAL,
                index_moment=self.config["structure"]["secondary_indexes_phase"],
                allow_schema_evolution=bool(
                    self.config.get("allow_schema_evolution", False)
                ),
            ):
                execute(destination, batch)
        after = compare_catalog(
            layout,
            inspect_layout_catalog(destination, layout),
            include_secondary=include_secondary,
        )
        if after.state not in {CatalogState.COMPATIBLE, CatalogState.INDEXES_PENDING}:
            raise RuntimeError(
                "Estrutura base não corresponde exatamente ao perfil: "
                + "; ".join(after.errors + after.missing_objects)
            )

    def _finish_indexes(self, destination: Any, layout: TableLayout) -> None:
        from .inspection import inspect_layout_catalog
        comparison = compare_catalog(layout, inspect_layout_catalog(destination, layout), data_completed=True)
        if comparison.state is CatalogState.COMPATIBLE:
            return
        if comparison.state not in {CatalogState.INDEXES_PENDING, CatalogState.DATA_COMPLETE_INDEXES_PENDING}:
            raise RuntimeError("Layout final incompatível: " + "; ".join(comparison.errors + comparison.missing_objects))
        if not self.config["create_structure_if_needed"]:
            raise RuntimeError(
                "Índices secundários ausentes e criação automática desabilitada"
            )
        for batch in build_apply_batches(layout, stage=DdlStage.SECONDARY_INDEXES):
            execute(destination, batch)
        final = compare_catalog(layout, inspect_layout_catalog(destination, layout), data_completed=True)
        if final.state is not CatalogState.COMPATIBLE:
            raise RuntimeError("Índices finais pendentes/incompatíveis: " + "; ".join(final.errors + final.missing_objects))

    def _sql_path_probe(self, destination: Any, store: ArtifactStore) -> None:
        token = os.urandom(32)
        local = store.execution_root / ("probe_" + uuid.uuid4().hex + ".bin")
        local.write_bytes(token)
        try:
            from .importer import sql_artifact_path
            remote = sql_artifact_path(self.config["executor_directory"], self.config["destination_sql_directory"], local)
            escaped_remote = remote.replace("'", "''")
            value = scalar(
                destination,
                f"SELECT BulkColumn FROM OPENROWSET(BULK N'{escaped_remote}',SINGLE_BLOB) AS p;",
            )
            if bytes(value) != token:
                raise RuntimeError("executor_directory e destination_sql_directory não apontam para os mesmos bytes")
        finally:
            local.unlink(missing_ok=True)

    @staticmethod
    def _destination_table_exists(destination: Any, layout: TableLayout) -> bool:
        return bool(
            scalar(
                destination,
                "SELECT CASE WHEN OBJECT_ID(?,N'U') IS NULL THEN 0 ELSE 1 END",
                (f"{layout.schema}.{layout.table}",),
            )
        )

    def _bind_and_provision(
        self,
        destination: Any,
        importer: DestinationImporter,
        *,
        execution_id: str,
        dataset_id: str,
        structural_hash: str,
        area: str,
        table_id: str,
        layout: TableLayout,
        layout_hash: str,
        projection_hash: str,
        final_limit_json: str,
    ) -> None:
        """Vincula antes de alterar tabela existente; cria antes apenas se ausente."""

        register = lambda: importer.register_execution_table(
            execution_id=execution_id,
            dataset_id=dataset_id,
            structural_hash=structural_hash,
            area=area,
            table_id=table_id,
            target_schema=layout.schema,
            target_table=layout.table,
            layout_hash=layout_hash,
            projection_hash=projection_hash,
            final_limit_json=final_limit_json,
        )
        if self._destination_table_exists(destination, layout):
            from .inspection import inspect_layout_catalog

            catalog = inspect_layout_catalog(destination, layout)
            comparison = compare_catalog(
                layout,
                catalog,
                include_secondary=(
                    self.config["structure"]["secondary_indexes_phase"]
                    == "before_load"
                ),
            )
            analysis = self._preflight_schema_evolution(
                area=area,
                layout=layout,
                catalog=catalog,
                comparison=comparison,
            )
            register()
            self._provision_initial(
                destination,
                layout,
                area=area,
                schema_preflight_logged=analysis.required,
            )
        else:
            self._provision_initial(destination, layout, area=area)
            register()

    def _manifest_for(
        self,
        prepared: PreparedTable,
        execution_id: str,
        dataset_id: str,
        block: Mapping[str, Any],
        attempt: int,
        window: BatchWindow,
        rows: int,
        exported_at: str,
        source_identity: Mapping[str, Any],
        destination_identity: Mapping[str, Any] | None,
        empty: bool,
    ) -> BlockManifest:
        src, dst = prepared.effective["source"], prepared.effective["destination"]
        auth = {
            "source": stable_json(authentication_identity(self.config["source"]["authentication"])),
        }
        if self.config["execute_import"]:
            endpoint = self.config[dst["area"] + "_destination"]
            auth["destination"] = stable_json(authentication_identity(endpoint["authentication"]))
        return BlockManifest(
            manifest_version=2,
            complete=False,
            execution_id=execution_id,
            dataset_id=dataset_id,
            table_id=prepared.table_id,
            block_id=str(block["block_id"]),
            block_number=int(block["block_number"]),
            attempt=attempt,
            source={
                "instance": src["instance"], "port": src.get("port", 1433),
                "database": src["database"],
                "read_database": src["read_database"], "schema": src["schema"],
                "table": src["table"], "effective_identity": dict(source_identity),
            },
            destination=(
                {"area": dst["area"], "instance": dst["instance"],
                 "port": dst.get("port", 1433), "database": dst["database"],
                 "schema": dst["schema"], "table": dst["table"],
                 "effective_identity": dict(destination_identity or {})}
                if self.config["execute_import"] else None
            ),
            authentication=auth,
            profile=prepared.layout.profile_name,
            projection=prepared.projection["columns"],
            projection_hash=prepared.projection["projection_hash"],
            layout_hash=prepared.layout_contract["layout_hash"],
            watermark=_watermark_manifest(prepared.watermark),
            lower_bound=list(window.lower) if window.lower else None,
            upper_bound=list(window.upper),
            final_limit=list(window.ceiling),
            rows_exported=rows,
            file_bytes=0,
            data_file=None,
            data_sha256=None,
            format_file="pending",
            format_sha256="pending",
            exported_at=exported_at,
            empty_range=empty,
            metadata_mapping=dict(prepared.effective.get("metadata_mapping", {})),
            warnings=[
                (
                    "DIRECT_KEYLESS: bloco único sem bookmark; a estimativa vem de "
                    "metadados e o limite é validado com rows_copied antes da publicação."
                    if _is_direct_keyless(prepared.watermark)
                    else "LIVE_BEST_EFFORT: o teto limita o cursor, mas não cria uma imagem transacional da origem."
                )
            ],
            layout_contract=dict(prepared.layout_contract),
            table_empty=(rows == 0 if _is_direct_keyless(prepared.watermark)
                         else _prepared_table_is_empty(prepared, window)),
            transfer_mode=_transfer_mode(prepared.watermark),
            source_rows_at_capture=_captured_row_count(prepared.watermark),
        )

    @staticmethod
    def _validate_manifest_for_planned_block(
        manifest: BlockManifest,
        prepared: PreparedTable,
        *,
        execution_id: str,
        dataset_id: str,
        block: Mapping[str, Any],
        window: BatchWindow,
    ) -> None:
        expected = {
            "execution_id": execution_id,
            "dataset_id": dataset_id,
            "table_id": prepared.table_id,
            "block_id": str(block["block_id"]),
            "block_number": int(block["block_number"]),
            "lower_bound": list(window.lower) if window.lower else None,
            "upper_bound": list(window.upper),
            "final_limit": list(window.ceiling),
            "layout_hash": prepared.layout_contract["layout_hash"],
            "projection_hash": prepared.projection["projection_hash"],
            "watermark": _watermark_manifest(prepared.watermark),
            "table_empty": (
                manifest.rows_exported == 0
                if _is_direct_keyless(prepared.watermark)
                else _prepared_table_is_empty(prepared, window)
            ),
            "transfer_mode": _transfer_mode(prepared.watermark),
            "source_rows_at_capture": _captured_row_count(prepared.watermark),
        }
        actual = {name: getattr(manifest, name) for name in expected}
        if actual != expected:
            divergent = sorted(name for name in expected if actual[name] != expected[name])
            raise StructuralResumeError(
                "Manifesto publicado diverge do bloco planejado: " + ", ".join(divergent)
            )
        source = prepared.effective["source"]
        for field in ("database", "read_database", "schema", "table"):
            if str(manifest.source.get(field)).casefold() != str(source[field]).casefold():
                raise StructuralResumeError(
                    f"Manifesto publicado diverge da origem planejada ({field})"
                )
        if (
            manifest.layout_contract
            and stable_json(manifest.layout_contract)
            != stable_json(prepared.layout_contract)
        ):
            raise StructuralResumeError("Snapshot físico do manifesto diverge do layout planejado")

    def run(self, *, execution_id: str | None = None, resume: bool = False) -> ExecutionReport:
        self._assert_bronze_data_route()
        if resume and not execution_id:
            raise ValueError("resume exige execution_id existente; uma nova identidade nao sera gerada")
        execution_id = execution_id or str(uuid.uuid4())
        validate_canonical_uuid(execution_id, "execution_id")
        dataset_id = structural_fingerprint(self.config)[:64]
        report = ExecutionReport(execution_id, "resume" if resume else "run", utc_now())
        source = destination = None
        state: LocalState | None = None
        store: ArtifactStore | None = None
        importer: DestinationImporter | None = None
        sql_execution_registered = False
        leases = ExitStack()
        try:
            # Create the report destination before opening the control state so
            # even a corrupt/incompatible SQLite control can produce durable
            # JSON/CSV diagnostics.
            store = ArtifactStore(
                self.config["executor_directory"],
                execution_id,
                reader_sids=self.config.get("artifact_reader_sids", []),
                writer_sids=self.config.get("artifact_writer_sids", []),
            )
            state = LocalState(self.config["local_control_directory"])
            leases.enter_context(
                state.execution_lease(execution_id)
                if hasattr(state, "execution_lease") else nullcontext()
            )
            if resume:
                state.resume_execution(
                    execution_id, structural_fingerprint(self.config), operational_fingerprint(self.config)
                )
            else:
                state.start_execution(
                    execution_id, dataset_id, "run", structural_fingerprint(self.config),
                    operational_fingerprint(self.config), self.config["execute_import"],
                    str(self.config_path) if self.config_path else None,
                )
            store.probe(require_delete=effective_delete_confirmed_files(self.config))
            source, source_identity = self._connect_source()
            db = database_identity(source)
            base_bcp = build_bcp_invocation(
                self.config["source"], self.config, self.resolver, endpoint_name="source"
            )
            self.bcp_runner.require_version(base_bcp.executable)
            self.bcp_runner.preflight(base_bcp)
            destination_identity = None
            if self.config["execute_import"]:
                destination, destination_identity = self._connect_destination()
                importer = DestinationImporter(destination, self.config["control_schema"])
                self._sql_path_probe(destination, store)
                importer.ensure_control()
            tables = effective_tables(self.config)
            cdc = CdcCoordinator(
                retention_minutes=self.config.get(
                    "cdc_retention_minutes",
                    DEFAULT_CDC_RETENTION_MINUTES,
                )
            )
            database_cdc = None
            if any(table.get("enable_cdc") is True for table in tables):
                source_endpoint = self.config["source"]
                source_database = (
                    source_endpoint.get("read_database")
                    or source_endpoint["database"]
                )
                database_cdc = cdc.preflight(
                    source,
                    database=source_database,
                    tables=tables,
                )
            if database_cdc is not None:
                report.cdc_database = database_cdc.as_dict()
                self.event("cdc_database", report.cdc_database)
                if not database_cdc.success:
                    report.warnings.append(
                        "CDC_DATABASE: "
                        + (database_cdc.error_code or "CDC_DATABASE_ENABLE_FAILED")
                        + " - "
                        + (database_cdc.message or "CDC não confirmado no banco de origem.")
                    )
            database_cdc_pending = bool(
                database_cdc is not None and database_cdc.retention_pending
            )
            retained_bytes = (
                sum(
                    int(row.get("file_bytes") or 0)
                    for configured in tables
                    for row in state.blocks(execution_id, _table_id(configured))
                    if row.get("status") in {"EXPORTED", "IMPORTING"}
                )
                if hasattr(state, "blocks")
                else 0
            )
            for table in tables:
                started = time.perf_counter()
                prepared: PreparedTable | None = None
                sql_table_registered = False
                sql_table_finished = False
                stop_after_table = False
                result = TableResult(
                    table["source"]["table"],
                    table["destination"]["table"] if self.config["execute_import"] else None,
                    TableStatus.RUNNING.value,
                )
                report.tables.append(result)
                try:
                    if table.get("enable_cdc") is True:
                        table_cdc = cdc.ensure_table(
                            source,
                            database=table["source"]["read_database"],
                            schema=table["source"]["schema"],
                            table=table["source"]["table"],
                        )
                        self.event("cdc_table", table_cdc.as_dict())
                        if database_cdc_pending and (
                            table_cdc.retention_minutes is not None
                            or table_cdc.stage.startswith("retention_")
                        ):
                            report.cdc_database = {
                                **(report.cdc_database or {}),
                                "success": table_cdc.success,
                                "enabled": table_cdc.enabled,
                                "changed": table_cdc.changed,
                                "stage": table_cdc.stage,
                                "error_code": table_cdc.error_code,
                                "message": table_cdc.message,
                                "permission_hint": table_cdc.permission_hint,
                                "retention_minutes": table_cdc.retention_minutes,
                                "retention_changed": table_cdc.retention_changed,
                                "retention_restarted": table_cdc.retention_restarted,
                                "retention_pending": False,
                            }
                            self.event("cdc_retention", report.cdc_database)
                            database_cdc_pending = False
                        if not table_cdc.success:
                            raise _CdcTableSkip(table_cdc)
                    prior = state.table(execution_id, _table_id(table))
                    stored_direct_count = _stored_direct_row_count(prior)
                    prepared = (
                        self.prepare_table(
                            source,
                            table,
                            db,
                            direct_keyless_captured_row_count=stored_direct_count,
                        )
                        if stored_direct_count is not None
                        else self.prepare_table(source, table, db)
                    )
                    result.destination_table = prepared.layout.table if self.config["execute_import"] else None
                    result.watermark = _watermark_manifest(prepared.watermark)
                    result.transfer_mode = _transfer_mode(prepared.watermark)
                    result.source_rows_at_capture = _captured_row_count(prepared.watermark)
                    prior = state.table(execution_id, prepared.table_id)
                    if prior and prior["structural_hash"] != prepared.table_structural_hash:
                        if hasattr(state, "refresh_unstarted_table"):
                            state.refresh_unstarted_table(
                                execution_id,
                                prepared.table_id,
                                destination_area=(
                                    table["destination"]["area"]
                                    if self.config["execute_import"] else None
                                ),
                                destination_schema=(
                                    table["destination"]["schema"]
                                    if self.config["execute_import"] else None
                                ),
                                destination_table=(
                                    table["destination"]["table"]
                                    if self.config["execute_import"] else None
                                ),
                                structural_hash=prepared.table_structural_hash,
                                watermark=_state_transfer_contract(prepared.watermark),
                            )
                        else:
                            raise StructuralResumeError(
                                "Layout/projeção/origem/marca mudou para a tabela"
                            )
                    state.register_table(
                        execution_id, prepared.table_id, table["index"], table["source"]["schema"],
                        table["source"]["table"], table["destination"]["area"] if self.config["execute_import"] else None,
                        table["destination"]["schema"] if self.config["execute_import"] else None,
                        table["destination"]["table"] if self.config["execute_import"] else None,
                        prepared.table_structural_hash,
                        _state_transfer_contract(prepared.watermark),
                    )
                    local_row = state.table(execution_id, prepared.table_id)
                    assert local_row is not None
                    if _is_direct_keyless(prepared.watermark) and local_row.get(
                        "watermark_json"
                    ) != stable_json(_state_transfer_contract(prepared.watermark)):
                        raise StructuralResumeError(
                            "Contagem capturada da carga direta diverge do controle local"
                        )
                    if local_row["final_limit_json"] is not None:
                        stored_limit = json.loads(local_row["final_limit_json"])
                        prepared.final_limit = tuple(stored_limit)
                    else:
                        captured_limit = (
                            ()
                            if _is_direct_keyless(prepared.watermark)
                            else capture_ceiling(
                                source,
                                table["source"]["schema"],
                                table["source"]["table"],
                                prepared.watermark.columns,
                            )
                        )
                        prepared.final_limit = tuple(captured_limit or ())
                        state.update_table(
                            execution_id, prepared.table_id,
                            # [] e o sentinel duravel de "teto capturado em
                            # tabela vazia"; NULL significa ainda nao capturado.
                            final_limit_json=stable_json(
                                list(prepared.final_limit)
                            ),
                            started_at=utc_now(), status=TableStatus.RUNNING.value,
                        )
                    result.final_limit = list(prepared.final_limit)
                    plan = self._table_plan(source, prepared, retained_bytes)
                    result.estimated_rows = plan["estimated_rows"]
                    result.warnings.extend(plan["warnings"])
                    self.event("table_plan", {"text": render_plan_rows([plan]), "plan": plan})
                    if not plan["execution_allowed"]:
                        raise RuntimeError(
                            "Panorama bloqueou a tabela por estimativa/espaço indisponível ou insuficiente; "
                            "revise estimates.on_unavailable e a capacidade real."
                        )
                    if destination is not None and importer is not None:
                        estimated = plan.get("estimated_bcp_bytes")
                        if estimated is not None:
                            required_destination_bytes = int(
                                (
                                    Decimal(int(estimated))
                                    * Decimal(
                                        str(self.config["estimates"]["safety_factor"])
                                    )
                                ).to_integral_value(rounding=ROUND_CEILING)
                            )
                            space = inspect_destination_space(
                                destination, required_destination_bytes
                            )
                            self.event(
                                "destination_space",
                                {
                                    "source_table": table["source"]["table"],
                                    "destination_table": table["destination"]["table"],
                                    "required_bytes": space.required_bytes,
                                    "available_bytes": space.available_bytes,
                                    "sufficient": space.sufficient,
                                    "volumes": list(space.volumes),
                                    "error": space.error,
                                },
                            )
                            if space.sufficient is False:
                                raise _DestinationSpaceSkip(space)
                            if space.sufficient is None:
                                result.warnings.append(
                                    "DESTINATION_SPACE_UNAVAILABLE: "
                                    + (space.error or "sem evidencia de volume")
                                )
                        self._bind_and_provision(
                            destination,
                            importer,
                            execution_id=execution_id,
                            dataset_id=dataset_id,
                            structural_hash=dataset_id,
                            area=table["destination"]["area"],
                            table_id=prepared.table_id,
                            layout=prepared.layout,
                            layout_hash=prepared.layout_contract["layout_hash"],
                            projection_hash=prepared.projection["projection_hash"],
                            final_limit_json=stable_json(prepared.final_limit),
                        )
                        sql_execution_registered = True
                        sql_table_registered = True
                    self._process_blocks(
                        source, destination, importer, state, store, base_bcp, prepared,
                        execution_id, dataset_id, source_identity.as_dict(),
                        destination_identity.as_dict() if destination_identity else None,
                        result,
                    )
                    if destination is not None and importer is not None:
                        result.destination_rows_verified = importer.verify_destination_cardinality(
                            execution_id=execution_id,
                            dataset_id=dataset_id,
                            table_id=prepared.table_id,
                            target_schema=prepared.layout.schema,
                            target_table=prepared.layout.table,
                        )
                    indexes_completed = True
                    if destination is not None:
                        try:
                            self._finish_indexes(destination, prepared.layout)
                            result.index_state = "COMPLETED"
                        except Exception as exc:
                            indexes_completed = False
                            result.status = TableStatus.DATA_COMPLETE_INDEXES_PENDING.value
                            result.index_state = "PENDING"
                            result.reason = redacted_exception(exc)
                            result.next_action = "Retomar a execução; apenas os índices faltantes serão tentados."
                            state.update_table(
                                execution_id, prepared.table_id, status=result.status,
                                index_state="PENDING", reason=result.reason, finished_at=utc_now(),
                            )
                            if importer is not None:
                                try:
                                    importer.finish_table(
                                        execution_id, dataset_id, prepared.table_id,
                                        state=result.status, index_state="PENDING",
                                    )
                                    sql_table_finished = True
                                except Exception as control_error:
                                    result.warnings.append(
                                        "Falha ao persistir estado de indices no controle SQL: "
                                        + redacted_exception(control_error)
                                    )
                    if indexes_completed:
                        result.status = (
                            TableStatus.EMPTY.value
                            if (
                                _is_direct_keyless(prepared.watermark)
                                and result.rows_exported == 0
                            )
                            or (
                                not _is_direct_keyless(prepared.watermark)
                                and prepared.final_limit == ()
                            )
                            else TableStatus.COMPLETED.value if self.config["execute_import"]
                            else TableStatus.EXPORTED.value
                        )
                        if importer is not None:
                            importer.finish_table(
                                execution_id, dataset_id, prepared.table_id,
                                state=result.status, index_state=result.index_state,
                            )
                            sql_table_finished = True
                        state.update_table(
                            execution_id, prepared.table_id, status=result.status,
                            index_state=result.index_state, finished_at=utc_now(),
                        )
                except _CdcTableSkip as exc:
                    result.status = TableStatus.SKIPPED_CDC_ACTIVATION_FAILED.value
                    result.reason = exc.result.message or str(exc)
                    if exc.result.error_code:
                        result.warnings.append("CDC_ERROR_CODE=" + exc.result.error_code)
                    if exc.result.permission_hint:
                        result.warnings.append(exc.result.permission_hint)
                    result.next_action = (
                        "Habilite e confirme o CDC na origem com uma conta autorizada; "
                        "depois retome a mesma execução."
                    )
                except _DestinationSpaceSkip as exc:
                    result.status = TableStatus.SKIPPED_DESTINATION_SPACE.value
                    result.reason = str(exc)
                    result.warnings.append(
                        "Carga Bronze nao iniciada; nenhuma linha desta tabela foi importada."
                    )
                    result.next_action = (
                        "Libere ou amplie o espaco dos volumes do banco Bronze e retome "
                        "a mesma execucao."
                    )
                except DirectKeylessLimitExceededError as exc:
                    result.status, result.reason = (
                        TableStatus.SKIPPED_DIRECT_LIMIT.value,
                        str(exc),
                    )
                    result.transfer_mode = "DIRECT_KEYLESS"
                except DirectKeylessCountUnavailableError as exc:
                    result.status, result.reason = (
                        TableStatus.SKIPPED_DIRECT_COUNT_UNAVAILABLE.value,
                        str(exc),
                    )
                    result.transfer_mode = "DIRECT_KEYLESS"
                except NoEligibleWatermarkError as exc:
                    result.status, result.reason = TableStatus.SKIPPED_NO_KEY.value, str(exc)
                except WatermarkContainsNullError as exc:
                    result.status, result.reason = TableStatus.SKIPPED_NULL_WATERMARK.value, str(exc)
                except (InvalidWatermarkError, WatermarkIndexRequiredError) as exc:
                    result.status, result.reason = TableStatus.SKIPPED_INVALID_WATERMARK.value, str(exc)
                except DirectSourceDriftError as exc:
                    result.status, result.reason = TableStatus.DIRECT_SOURCE_DRIFT.value, str(exc)
                    result.next_action = (
                        "Estabilize a origem e inicie uma nova execução; a contagem "
                        "capturada desta execução é imutável e nenhum manifesto divergente foi publicado."
                    )
                except SchemaEvolutionRequiredError as exc:
                    result.status = TableStatus.SCHEMA_EVOLUTION_PENDING.value
                    result.reason = redacted_exception(exc)
                    result.next_action = (
                        "Revise as colunas informadas e habilite allow_schema_evolution "
                        "para autorizar somente os ADDs seguros."
                    )
                except SchemaEvolutionSafetyError as exc:
                    result.status = TableStatus.LAYOUT_ERROR.value
                    result.reason = redacted_exception(exc)
                    result.next_action = (
                        "Migre/backfill explicitamente a coluna NOT NULL; o motor não "
                        "degrada nulabilidade nem inventa valor padrão."
                    )
                except Exception as exc:
                    result.reason = redacted_exception(exc)
                    current = None
                    try:
                        if prepared is not None:
                            current = state.table(execution_id, prepared.table_id)
                    except Exception:
                        current = None
                    durable_rows = int(current["rows_exported"] or 0) if current else 0
                    if result.status == TableStatus.TIE_GROUP_TOO_LARGE.value:
                        pass
                    elif result.rows_imported or result.rows_exported or durable_rows:
                        result.status = TableStatus.PARTIAL_ERROR.value
                    elif "layout" in result.reason.casefold() or "structure" in result.reason.casefold():
                        result.status = TableStatus.LAYOUT_ERROR.value
                    else:
                        result.status = TableStatus.EXPORT_ERROR.value
                    result.next_action = "Corrigir a causa e executar resume com o mesmo UUID."
                    if current is not None:
                        try:
                            state.update_table(
                                execution_id, prepared.table_id, status=result.status,
                                reason=result.reason, finished_at=utc_now(),
                            )
                        except Exception:
                            pass
                finally:
                    if prepared is not None:
                        try:
                            self._restore_result_progress(
                                state,
                                store,
                                execution_id,
                                prepared.table_id,
                                result,
                            )
                        except Exception as progress_error:
                            result.warnings.append(
                                "Falha ao reconstruir o progresso duravel no relatorio: "
                                + redacted_exception(progress_error)
                            )
                    result.duration_seconds = time.perf_counter() - started
                # Falhas de descoberta acontecem antes do registro normal da
                # tabela. Ainda assim precisam aparecer no status durável.
                durable_table_id = prepared.table_id if prepared else _table_id(table)
                try:
                    if state.table(execution_id, durable_table_id) is None:
                        state.register_table(
                            execution_id,
                            durable_table_id,
                            table["index"],
                            table["source"]["schema"],
                            table["source"]["table"],
                            table["destination"]["area"]
                            if self.config["execute_import"] else None,
                            table["destination"]["schema"]
                            if self.config["execute_import"] else None,
                            table["destination"]["table"]
                            if self.config["execute_import"] else None,
                            (
                                prepared.table_structural_hash
                                if prepared
                                else structural_fingerprint(self.config, table["index"])
                            ),
                            (
                                _state_transfer_contract(prepared.watermark)
                                if prepared
                                else result.watermark or table.get("watermark")
                            ),
                        )
                    state.update_table(
                        execution_id,
                        durable_table_id,
                        status=result.status,
                        reason=result.reason,
                        warnings_json=stable_json(result.warnings),
                        index_state=result.index_state,
                        finished_at=utc_now(),
                    )
                except Exception as persistence_error:
                    result.warnings.append(
                        "Falha ao persistir o resultado local da tabela: "
                        + redacted_exception(persistence_error)
                    )
                # A SQL-control table must never remain artificially RUNNING
                # when processing ends after destination provisioning. Mirror
                # the terminal local state for every post-registration exit.
                if (
                    importer is not None
                    and sql_table_registered
                    and not sql_table_finished
                    and prepared is not None
                ):
                    try:
                        importer.finish_table(
                            execution_id,
                            dataset_id,
                            prepared.table_id,
                            state=result.status,
                            index_state=result.index_state,
                        )
                        sql_table_finished = True
                    except Exception as control_error:
                        result.warnings.append(
                            "Falha ao persistir o resultado da tabela no controle SQL: "
                            + redacted_exception(control_error)
                        )
                if (
                    not self.config["continue_after_table_error"]
                    and result.status
                    not in {
                        TableStatus.COMPLETED.value,
                        TableStatus.EXPORTED.value,
                        TableStatus.EMPTY.value,
                        # O contrato CDC exige sempre pular apenas a tabela
                        # afetada e tentar a próxima, independentemente da
                        # política geral de erros.
                        TableStatus.SKIPPED_CDC_ACTIVATION_FAILED.value,
                        TableStatus.SKIPPED_DESTINATION_SPACE.value,
                    }
                ):
                    stop_after_table = True
                if not self.config["execute_import"]:
                    retained_bytes = (
                        sum(
                            int(row.get("file_bytes") or 0)
                            for configured in effective_tables(self.config)
                            for row in state.blocks(execution_id, _table_id(configured))
                            if row.get("status") in {"EXPORTED", "IMPORTING"}
                        )
                        if hasattr(state, "blocks")
                        else retained_bytes + result.bytes_exported
                    )
                if stop_after_table:
                    break
            if importer is not None and sql_execution_registered:
                importer.finish_execution(
                    execution_id,
                    dataset_id,
                    state=(
                        "COMPLETED" if report.exit_code() == 0
                        else "COMPLETED_WITH_PENDING_ITEMS"
                    ),
                )
            state.finish_execution(
                execution_id, "COMPLETED" if report.exit_code() == 0 else "COMPLETED_WITH_PENDING_ITEMS"
            )
            return report
        except BaseException as exc:
            report.global_error = redacted_exception(exc) if isinstance(exc, Exception) else type(exc).__name__
            if state is not None:
                try:
                    state.finish_execution(execution_id, "GLOBAL_ERROR", "GLOBAL_ERROR", report.global_error)
                except Exception:
                    pass
            return report
        finally:
            finish_timestamp(report)
            report_root = (
                store.execution_root
                if store is not None
                else Path(self.config["executor_directory"]) / execution_id
            )
            try:
                write_reports(report, report_root)
            except Exception as report_error:
                message = "Falha ao gravar relatorios: " + redacted_exception(report_error)
                if report.global_error is None:
                    report.global_error = message
                else:
                    report.warnings.append(message)
            close_all([source, destination])
            leases.close()
            if state is not None:
                state.close()
            self.resolver.clear_cache()

    @staticmethod
    def _restore_result_progress(
        state: LocalState,
        store: ArtifactStore,
        execution_id: str,
        table_id: str,
        result: TableResult,
    ) -> None:
        """Hydrate cumulative report fields from durable local checkpoints."""

        local = state.table(execution_id, table_id)
        if local is None:
            return
        for source_name, target_name in (
            ("rows_exported", "rows_exported"),
            ("rows_imported", "rows_imported"),
            ("bytes_exported", "bytes_exported"),
        ):
            if source_name in local:
                setattr(result, target_name, int(local[source_name] or 0))

        def cursor(name: str) -> list[str] | None:
            raw = local.get(name)
            if raw is None:
                return None
            value = json.loads(raw)
            if not isinstance(value, list):
                raise StructuralResumeError(f"{name} do controle local nao e um array JSON")
            return value

        result.last_exported = cursor("export_cursor_json")
        result.last_imported = cursor("import_cursor_json")

        retained = 0
        if hasattr(state, "blocks"):
            for block in state.blocks(execution_id, table_id):
                if block.get("status") not in {
                    "EXPORTED",
                    "IMPORTING",
                    "IMPORTED",
                    "EMPTY_CONFIRMED",
                }:
                    continue
                manifest_path = block.get("manifest_path")
                if manifest_path and store.retained_data_exists(manifest_path):
                    retained += 1
        result.retained_files = retained

    def _process_blocks(
        self,
        source: Any,
        destination: Any,
        importer: DestinationImporter | None,
        state: LocalState,
        store: ArtifactStore,
        base_bcp: Any,
        prepared: PreparedTable,
        execution_id: str,
        dataset_id: str,
        source_identity: Mapping[str, Any],
        destination_identity: Mapping[str, Any] | None,
        result: TableResult,
    ) -> None:
        import_plan = (
            build_import_plan(
                prepared.layout,
                id_strategy=self.config.get("bronze_event_id"),
                metadata_mapping=prepared.effective.get("metadata_mapping"),
            )
            if self.config["execute_import"]
            else None
        )
        # Primeiro reconcilia/importa artefatos já publicados, sem reexportar.
        for block in state.blocks(execution_id, prepared.table_id):
            status = block["status"]
            if status in {"EXPORTED", "IMPORTING", "EMPTY_CONFIRMED"} and self.config["execute_import"]:
                assert importer is not None and block["manifest_path"]
                assert import_plan is not None
                stored_lower = (
                    tuple(json.loads(block["lower_bound_json"]))
                    if block["lower_bound_json"] else None
                )
                stored_upper = tuple(json.loads(block["upper_bound_json"]))
                stored_final = tuple(json.loads(block["final_limit_json"]))
                reconciled_window = BatchWindow(
                    stored_lower,
                    stored_upper,
                    stored_final,
                    int(block["rows_exported"] or 0),
                    0,
                    int(prepared.effective["rows_per_block"]),
                )
                published = store.load_for_reconciliation(block["manifest_path"])
                self._validate_manifest_for_planned_block(
                    published,
                    prepared,
                    execution_id=execution_id,
                    dataset_id=dataset_id,
                    block=block,
                    window=reconciled_window,
                )
                manifest = importer.import_manifest(
                    store, block["manifest_path"], import_plan,
                    self.config["executor_directory"], self.config["destination_sql_directory"],
                )
                state.mark_imported(execution_id, prepared.table_id, block["block_id"], manifest.rows_exported)
                result.rows_imported += manifest.rows_exported
                result.last_imported = manifest.upper_bound
                if effective_delete_confirmed_files(self.config):
                    if not manifest.empty_range:
                        store.remove_confirmed_data(block["manifest_path"])
            if status == "IMPORTED":
                result.rows_imported += int(block["rows_imported"] or 0)
            if status in {"EXPORTED", "IMPORTED", "EMPTY_CONFIRMED"}:
                result.rows_exported += int(block["rows_exported"] or 0)
                result.bytes_exported += int(block["file_bytes"] or 0)

        local = state.table(execution_id, prepared.table_id)
        assert local is not None and prepared.final_limit is not None
        lower = tuple(json.loads(local["export_cursor_json"])) if local["export_cursor_json"] else None
        pending = [
            block for block in state.blocks(execution_id, prepared.table_id)
            if block["status"] not in {"EXPORTED", "IMPORTED", "EMPTY_CONFIRMED"}
        ]
        if len(pending) > 1:
            raise StructuralResumeError(
                "Controle local possui mais de um bloco nao duravel; reconciliacao manual necessaria"
            )
        while lower != prepared.final_limit:
            forced_empty = False
            exported_this_invocation = False
            if pending:
                block = pending.pop(0)
                stored_lower = (
                    tuple(json.loads(block["lower_bound_json"]))
                    if block["lower_bound_json"] else None
                )
                stored_final = (
                    tuple(json.loads(block["final_limit_json"]))
                    if block["final_limit_json"] else None
                )
                if stored_lower != lower or stored_final != prepared.final_limit:
                    raise StructuralResumeError(
                        "Bloco nao duravel possui limite inferior/teto divergente do checkpoint"
                    )
                if _is_direct_keyless(prepared.watermark):
                    window = direct_keyless_window(
                        int(_captured_row_count(prepared.watermark) or 0)
                    )
                    if tuple(json.loads(block["upper_bound_json"])) != ():
                        raise StructuralResumeError(
                            "Carga direta sem chave possui bookmark indevido no bloco pendente"
                        )
                else:
                    window = BatchWindow(
                        lower,
                        tuple(json.loads(block["upper_bound_json"])),
                        prepared.final_limit,
                        0,
                        0,
                        int(prepared.effective["rows_per_block"]),
                    )
                number = int(block["block_number"])
            else:
                if _is_direct_keyless(prepared.watermark):
                    window = direct_keyless_window(
                        int(_captured_row_count(prepared.watermark) or 0)
                    )
                    # Metadata can report zero while rows already exist.  A
                    # direct-keyless table is always exported and BCP is the
                    # authoritative count for the hard limit.
                    forced_empty = False
                elif prepared.final_limit == ():
                    window = BatchWindow(
                        None, (), (), 0, 0,
                        int(prepared.effective["rows_per_block"]),
                    )
                    forced_empty = True
                else:
                    window = plan_next_batch(
                        source,
                        prepared.effective["source"]["schema"],
                        prepared.effective["source"]["table"],
                        prepared.watermark.columns,
                        lower,
                        prepared.final_limit,
                        int(prepared.effective["rows_per_block"]),
                    )
                    if window is None:
                        window = BatchWindow(
                            lower, prepared.final_limit, prepared.final_limit, 0, 0,
                            int(prepared.effective["rows_per_block"]),
                        )
                        forced_empty = True
                number = state.next_block_number(execution_id, prepared.table_id)
                block = state.plan_block(
                    execution_id, prepared.table_id, number,
                    list(window.lower) if window.lower else None,
                    list(window.upper), list(window.ceiling),
                )
            if prepared.average_row_bytes is not None:
                group_bytes = int(prepared.average_row_bytes * window.boundary_group_rows)
                if group_bytes > int(self.config["max_file_bytes"]):
                    result.status = TableStatus.TIE_GROUP_TOO_LARGE.value
                    raise RuntimeError(
                        "TIE_GROUP_TOO_LARGE: amplie a marca d'água com desempate "
                        "ou ajuste conscientemente o limite de arquivo."
                    )
            paths = store.block_paths(prepared.table_id, number, block["block_id"])
            if paths.manifest.exists():
                manifest = store.load_and_verify(paths.manifest)
                self._validate_manifest_for_planned_block(
                    manifest,
                    prepared,
                    execution_id=execution_id,
                    dataset_id=dataset_id,
                    block=block,
                    window=window,
                )
                state.mark_exported(
                    execution_id, prepared.table_id, block["block_id"], str(paths.manifest),
                    manifest.rows_exported, manifest.file_bytes, manifest.data_sha256,
                    manifest.format_sha256, empty_range=manifest.empty_range,
                )
            else:
                paths.partial.unlink(missing_ok=True)
                paths.format.unlink(missing_ok=True)
                attempt = state.begin_attempt(
                    execution_id, prepared.table_id, block["block_id"], str(paths.log)
                )
                try:
                    source_name = qi(prepared.effective["source"]["schema"]) + "." + qi(prepared.effective["source"]["table"])
                    make_format_file(
                        self.bcp_runner, base_bcp, source_name, prepared.columns,
                        paths.format, paths.directory / "format.log",
                        timeout=int(self.config["bcp_timeout_seconds"]),
                    )
                    actual_rows = 0
                    if not forced_empty:
                        query = (
                            direct_keyless_export_query(
                                prepared.effective["source"]["schema"],
                                prepared.effective["source"]["table"],
                                prepared.columns,
                            )
                            if _is_direct_keyless(prepared.watermark)
                            else export_query(
                                prepared.effective["source"]["schema"],
                                prepared.effective["source"]["table"],
                                prepared.columns,
                                prepared.watermark.columns,
                                window.lower,
                                window.upper,
                                window.ceiling,
                            )
                        )
                        invocation = base_bcp.with_operation(query, "queryout", str(paths.partial))
                        bcp_result = self.bcp_runner.run(
                            invocation, paths.log, monitor_path=paths.partial,
                            timeout=int(self.config["bcp_timeout_seconds"]),
                            maximum_bytes=int(self.config["max_file_bytes"]),
                            minimum_free=int(self.config["minimum_free_space_bytes"]),
                        )
                        actual_rows = int(bcp_result.rows_copied or 0)
                    actual_bytes = (
                        paths.partial.stat().st_size if paths.partial.exists() else 0
                    )
                    observation = (
                        observe_direct_keyless_export(
                            window,
                            actual_rows,
                            actual_bytes,
                            maximum_rows=int(self.config["keyless_direct_load_max_rows"]),
                        )
                        if _is_direct_keyless(prepared.watermark)
                        else observe_export(window, actual_rows, actual_bytes)
                    )
                    if observation.actual_rows == 0:
                        paths.partial.unlink(missing_ok=True)
                    manifest = self._manifest_for(
                        prepared, execution_id, dataset_id, block, attempt, window,
                        observation.actual_rows, utc_now(), source_identity,
                        destination_identity, observation.actual_rows == 0,
                    )
                    manifest = store.publish(paths, manifest)
                    state.mark_exported(
                        execution_id, prepared.table_id, block["block_id"], str(paths.manifest),
                        manifest.rows_exported, manifest.file_bytes, manifest.data_sha256,
                        manifest.format_sha256, empty_range=manifest.empty_range,
                    )
                    exported_this_invocation = True
                except Exception as exc:
                    if hasattr(state, "fail_attempt"):
                        state.fail_attempt(
                            execution_id,
                            prepared.table_id,
                            str(block["block_id"]),
                            "EXPORT_ERROR",
                            redacted_exception(exc),
                        )
                    raise
            if self.config["execute_import"] and not manifest.empty_range:
                assert importer is not None
                assert import_plan is not None
                importer.import_manifest(
                    store, paths.manifest, import_plan,
                    self.config["executor_directory"], self.config["destination_sql_directory"],
                )
                state.mark_imported(
                    execution_id, prepared.table_id, block["block_id"], manifest.rows_exported
                )
                result.rows_imported += manifest.rows_exported
                if effective_delete_confirmed_files(self.config):
                    store.remove_confirmed_data(paths.manifest)
            elif manifest.empty_range and self.config["execute_import"]:
                # Registra o bloco vazio também no controle SQL, sem OPENROWSET.
                assert importer is not None
                assert import_plan is not None
                importer.import_manifest(
                    store, paths.manifest, import_plan,
                    self.config["executor_directory"], self.config["destination_sql_directory"],
                )
                state.mark_imported(execution_id, prepared.table_id, block["block_id"], 0)
            else:
                result.retained_files += 0 if manifest.empty_range else 1
            result.rows_exported += manifest.rows_exported
            result.bytes_exported += manifest.file_bytes
            if exported_this_invocation:
                result.bytes_exported_this_invocation += manifest.file_bytes
            result.last_exported = list(window.upper)
            if self.config["execute_import"]:
                result.last_imported = list(window.upper)
            lower = window.upper

        self._restore_result_progress(
            state,
            store,
            execution_id,
            prepared.table_id,
            result,
        )

    def _resolve_manifest_layout(
        self,
        manifest: BlockManifest,
        configured: Mapping[str, Any],
    ) -> TableLayout:
        destination_info = configured["destination"]
        area = str(destination_info["area"])
        if manifest.profile.casefold() != area.casefold():
            raise RuntimeError("Perfil do manifesto diverge da area de destino")

        # O snapshot embutido e evidencia auditavel, nunca codigo confiavel.
        # DDL e DML sao materializados exclusivamente por um perfil local
        # validado, seja ele built-in ou customizado.
        profile_value, directory = _profile_path(
            str(destination_info["structure_profile"]), self.config_directory
        )
        layout = resolve_profile(
            profile_value,
            manifest.projection,
            source_database=str(manifest.source["database"]),
            source_table=str(manifest.source["table"]),
            destination_schema=str(destination_info["schema"]),
            destination_table=str(destination_info["table"]),
            partition_column=configured.get("partition_column"),
            directory=directory,
            bronze_event_id=(
                self.config.get("bronze_event_id") if area == "bronze" else None
            ),
        )
        if layout.table.casefold() != str(destination_info["table"]).casefold():
            raise RuntimeError("Snapshot de layout aponta para tabela de destino divergente")
        if layout.profile_name.casefold() != area.casefold():
            raise RuntimeError("Perfil local diverge da área de destino")
        physical = layout_contract(layout)
        projection = projection_contract(layout, manifest.projection)
        if (
            physical["layout_hash"] != manifest.layout_hash
            or projection["projection_hash"] != manifest.projection_hash
        ):
            raise RuntimeError("Manifesto diverge do layout/projeção materializados")

        preserved_layout = getattr(manifest, "layout_contract", {})
        if preserved_layout:
            # O schema e roteamento operacional e pode ser alterado entre os
            # hosts. Todo o restante do contrato deve reproduzir exatamente o
            # perfil local confiavel, inclusive expressoes e fill rules.
            trusted_contract = {
                key: value for key, value in physical.items() if key != "schema"
            }
            embedded_contract = {
                key: value
                for key, value in preserved_layout.items()
                if key != "schema"
            }
            if stable_json(embedded_contract) != stable_json(trusted_contract):
                raise RuntimeError("Snapshot de layout diverge do perfil local confiavel")
        return layout

    def _prevalidate_imports(
        self,
        paths: list[Path],
    ) -> tuple[BlockManifest, ArtifactStore, list[ValidatedImport]]:
        """Valida todo o conjunto antes de abrir ou alterar o SQL destino."""

        first = BlockManifest.from_dict(read_bounded_json(paths[0]))
        store = ArtifactStore(
            self.config["executor_directory"],
            first.execution_id,
            reader_sids=self.config.get("artifact_reader_sids", []),
            writer_sids=self.config.get("artifact_writer_sids", []),
        )
        validated: list[ValidatedImport] = []
        seen_ids: set[tuple[str, str]] = set()
        seen_numbers: set[tuple[str, int]] = set()
        target_owners: dict[tuple[str, str, str], str] = {}
        for path in paths:
            loader = getattr(store, "load_for_reconciliation", store.load_and_verify)
            manifest = loader(path)
            if manifest.execution_id != first.execution_id:
                raise RuntimeError("Lista mistura execuções diferentes")
            if manifest.dataset_id != first.dataset_id:
                raise RuntimeError("Lista mistura datasets estruturais diferentes")
            id_key = (manifest.table_id, manifest.block_id)
            number_key = (manifest.table_id, manifest.block_number)
            if id_key in seen_ids or number_key in seen_numbers:
                raise RuntimeError("Lista contém bloco ou número de bloco duplicado")
            seen_ids.add(id_key)
            seen_numbers.add(number_key)
            configured = self._configured_table_for_manifest(manifest)
            area = str(configured["destination"]["area"])
            target_key = (
                area.casefold(),
                str(configured["destination"]["schema"]).casefold(),
                str(configured["destination"]["table"]).casefold(),
            )
            prior_owner = target_owners.setdefault(target_key, manifest.table_id)
            if prior_owner != manifest.table_id:
                raise RuntimeError(
                    "Conjunto de manifestos mapeia table_id distintos para o mesmo destino"
                )
            if self.config.get(area + "_destination") is None:
                raise RuntimeError(f"Destino {area} não configurado")
            if area != self.config["active_destination"]:
                raise RuntimeError(
                    "Importação por manifesto exige que a área mapeada seja active_destination"
                )
            layout = self._resolve_manifest_layout(manifest, configured)
            plan = build_import_plan(
                layout,
                id_strategy=self.config.get("bronze_event_id"),
                metadata_mapping=manifest.metadata_mapping,
            )
            validated.append(ValidatedImport(path.resolve(), manifest, configured, layout, plan))

        by_table: dict[str, list[ValidatedImport]] = {}
        for item in validated:
            by_table.setdefault(item.manifest.table_id, []).append(item)
        for items in by_table.values():
            items.sort(key=lambda item: item.manifest.block_number)
            anchor = items[0]
            for item in items[1:]:
                left, right = anchor.manifest, item.manifest
                if (
                    right.block_number != left.block_number + 1
                    or right.lower_bound != left.upper_bound
                ):
                    raise RuntimeError(
                        "Conjunto de manifestos possui gap/overlap ou numeração descontínua"
                    )
                if (
                    left.layout_hash != right.layout_hash
                    or left.projection_hash != right.projection_hash
                    or left.final_limit != right.final_limit
                    or left.watermark != right.watermark
                    or left.transfer_mode != right.transfer_mode
                    or left.source_rows_at_capture != right.source_rows_at_capture
                    or left.metadata_mapping != right.metadata_mapping
                    or getattr(left, "layout_contract", {})
                    != getattr(right, "layout_contract", {})
                ):
                    raise RuntimeError(
                        "Manifestos da mesma tabela possuem contratos estruturais divergentes"
                    )
                anchor = item
        validated.sort(
            key=lambda item: (
                int(item.effective.get("index", 0)),
                item.manifest.table_id,
                item.manifest.block_number,
            )
        )
        return first, store, validated

    def import_manifests(self, manifest_path: Path) -> ExecutionReport:
        """Importa artefatos sem abrir ou consultar a origem."""

        self._assert_bronze_data_route()

        if manifest_path.is_dir():
            paths = sorted(manifest_path.rglob("*.manifest.json"))
        else:
            payload = read_bounded_json(manifest_path)
            if isinstance(payload, dict) and "manifests" in payload:
                if not isinstance(payload["manifests"], list) or not all(
                    isinstance(item, str) for item in payload["manifests"]
                ):
                    raise RuntimeError("Índice de manifestos inválido")
                paths = [(manifest_path.parent / item).resolve() for item in payload["manifests"]]
            else:
                paths = [manifest_path.resolve()]
        if not paths:
            raise RuntimeError("Nenhum manifesto encontrado")

        # Esta fase é deliberadamente anterior à conexão de destino: path,
        # hashes, contratos e conjunto são validados sem DDL/DML parcial.
        first, store, validated = self._prevalidate_imports(sorted(paths))
        report = ExecutionReport(first.execution_id, "import", utc_now())
        destination = None
        state: LocalState | None = None
        importer: DestinationImporter | None = None
        leases = ExitStack()
        registered_any = False
        destination_prepared = False
        try:
            state = LocalState(self.config["local_control_directory"])
            leases.enter_context(
                state.execution_lease(first.execution_id)
                if hasattr(state, "execution_lease") else nullcontext()
            )
            existing_execution = state.execution(first.execution_id)
            if existing_execution is None:
                state.start_execution(
                    first.execution_id,
                    first.dataset_id,
                    "import",
                    first.dataset_id,
                    operational_fingerprint(self.config),
                    True,
                    str(self.config_path) if self.config_path else None,
                )
            else:
                if existing_execution.get("dataset_id") != first.dataset_id:
                    raise StructuralResumeError(
                        "O UUID da execução local já pertence a outro dataset"
                    )
                state.resume_execution(
                    first.execution_id,
                    first.dataset_id,
                    operational_fingerprint(self.config),
                )
            destination, _ = self._connect_destination()
            importer = DestinationImporter(destination, self.config["control_schema"])

            grouped: dict[str, list[ValidatedImport]] = {}
            for item in validated:
                grouped.setdefault(item.manifest.table_id, []).append(item)

            for table_id, items in grouped.items():
                anchor = items[0]
                manifest = anchor.manifest
                layout = anchor.layout
                area = str(anchor.effective["destination"]["area"])
                result = TableResult(
                    str(manifest.source["table"]),
                    layout.table,
                    TableStatus.RUNNING.value,
                    watermark=list(manifest.watermark),
                    transfer_mode=getattr(manifest, "transfer_mode", "KEYSET"),
                    source_rows_at_capture=getattr(
                        manifest, "source_rows_at_capture", None
                    ),
                    final_limit=list(manifest.final_limit),
                )
                report.tables.append(result)
                table_registered = False
                table_finished = False
                try:
                    # Materialize o checkpoint local antes de qualquer DDL/DML no
                    # destino. Assim, uma recusa por capacidade continua
                    # auditavel sem criar objetos no controle SQL da Bronze.
                    if state.table(manifest.execution_id, table_id) is None:
                        state.register_table(
                            manifest.execution_id,
                            table_id,
                            int(anchor.effective.get("index", len(report.tables) - 1)),
                            str(manifest.source["schema"]),
                            str(manifest.source["table"]),
                            area,
                            layout.schema,
                            layout.table,
                            manifest.dataset_id,
                            _manifest_state_transfer_contract(manifest),
                        )
                        state.update_table(
                            manifest.execution_id,
                            table_id,
                            final_limit_json=stable_json(manifest.final_limit),
                            status=TableStatus.RUNNING.value,
                            started_at=utc_now(),
                        )

                    binding_preexisting = importer.compatible_table_binding_exists(
                        execution_id=manifest.execution_id,
                        dataset_id=manifest.dataset_id,
                        structural_hash=manifest.dataset_id,
                        area=area,
                        table_id=table_id,
                        target_schema=layout.schema,
                        target_table=layout.table,
                        layout_hash=manifest.layout_hash,
                        projection_hash=manifest.projection_hash,
                        final_limit_json=stable_json(manifest.final_limit),
                    )
                    pending_items = [
                        item
                        for item in items
                        if not importer.block_is_confirmed(item.manifest)
                    ]
                    manifest_bytes = sum(
                        int(item.manifest.file_bytes) for item in pending_items
                    )
                    required_destination_bytes = int(
                        (
                            Decimal(manifest_bytes)
                            * Decimal(
                                str(self.config["estimates"]["safety_factor"])
                            )
                        ).to_integral_value(rounding=ROUND_CEILING)
                    )
                    space = inspect_destination_space(
                        destination, required_destination_bytes
                    )
                    self.event(
                        "destination_space",
                        {
                            "source_table": str(manifest.source["table"]),
                            "destination_table": layout.table,
                            "required_bytes": space.required_bytes,
                            "available_bytes": space.available_bytes,
                            "sufficient": space.sufficient,
                            "volumes": list(space.volumes),
                            "error": space.error,
                        },
                    )
                    if space.sufficient is False:
                        # Uma retomada pode ja possuir o vinculo SQL mesmo sem
                        # blocos confirmados (por exemplo, pane logo apos o
                        # provisionamento). Nesse caso o estado terminal deve
                        # ser espelhado no controle existente. Uma importacao
                        # fresca continua sem qualquer mutacao SQL.
                        if binding_preexisting:
                            table_registered = True
                            registered_any = True
                        raise _DestinationSpaceSkip(space)
                    if space.sufficient is None:
                        result.warnings.append(
                            "DESTINATION_SPACE_UNAVAILABLE: "
                            + (space.error or "sem evidencia de volume")
                        )

                    # O controle SQL e a sonda do caminho sao adiados ate que
                    # exista ao menos uma tabela apta a importar. Tabelas
                    # recusadas por espaco nao deixam DDL nem linhas de controle.
                    if not destination_prepared:
                        importer.ensure_control()
                        self._sql_path_probe(destination, store)
                        destination_prepared = True

                    self._bind_and_provision(
                        destination,
                        importer,
                        execution_id=manifest.execution_id,
                        dataset_id=manifest.dataset_id,
                        structural_hash=manifest.dataset_id,
                        area=area,
                        table_id=table_id,
                        layout=layout,
                        layout_hash=manifest.layout_hash,
                        projection_hash=manifest.projection_hash,
                        final_limit_json=stable_json(manifest.final_limit),
                    )
                    table_registered = registered_any = True

                    for item in items:
                        current = item.manifest
                        importer.import_manifest(
                            store,
                            item.path,
                            item.import_plan,
                            self.config["executor_directory"],
                            self.config["destination_sql_directory"],
                        )
                        # O manifesto pode ter sido produzido em outro host.
                        # Depois de o destino confirmar o bloco exato, materialize
                        # também seu checkpoint no SQLite deste executor antes de
                        # avançar o cursor local de importação.
                        state.reconcile_manifest_block(
                            current.execution_id,
                            current.table_id,
                            current.block_id,
                            current.block_number,
                            current.lower_bound,
                            current.upper_bound,
                            current.final_limit,
                            str(item.path),
                            current.rows_exported,
                            current.file_bytes,
                            current.data_sha256,
                            current.format_sha256,
                            empty_range=current.empty_range,
                            exported_at=current.exported_at,
                        )
                        state.mark_imported(
                            current.execution_id,
                            current.table_id,
                            current.block_id,
                            current.rows_exported,
                        )
                        result.rows_exported += current.rows_exported
                        result.rows_imported += current.rows_exported
                        result.bytes_exported += current.file_bytes
                        result.last_exported = list(current.upper_bound)
                        result.last_imported = list(current.upper_bound)
                        if (
                            effective_delete_confirmed_files(self.config)
                            and not getattr(current, "empty_range", False)
                        ):
                            store.remove_confirmed_data(item.path)
                        data_file = getattr(current, "data_file", None)
                        data_exists = bool(
                            data_file
                            and (item.path.parent / data_file).exists()
                        )
                        result.retained_files += int(data_exists)

                    complete = importer.table_is_complete(manifest)
                    if not complete:
                        result.status = TableStatus.PARTIALLY_IMPORTED.value
                        result.index_state = "DEFERRED"
                        result.next_action = (
                            "Fornecer os manifestos restantes; índices secundários serão "
                            "finalizados somente quando o cursor atingir o teto capturado."
                        )
                    else:
                        result.destination_rows_verified = importer.verify_destination_cardinality(
                            execution_id=manifest.execution_id,
                            dataset_id=manifest.dataset_id,
                            table_id=table_id,
                            target_schema=layout.schema,
                            target_table=layout.table,
                        )
                        try:
                            self._finish_indexes(destination, layout)
                        except Exception as exc:
                            result.status = TableStatus.DATA_COMPLETE_INDEXES_PENDING.value
                            result.index_state = "PENDING"
                            result.reason = redacted_exception(exc)
                            result.next_action = (
                                "Executar novamente o import; blocos confirmados não serão reinseridos."
                            )
                        else:
                            result.status = (
                                TableStatus.EMPTY.value
                                if getattr(manifest, "table_empty", False)
                                else TableStatus.COMPLETED.value
                            )
                            result.index_state = "COMPLETED"
                    importer.finish_table(
                        manifest.execution_id,
                        manifest.dataset_id,
                        table_id,
                        state=result.status,
                        index_state=result.index_state,
                    )
                    table_finished = True
                except _DestinationSpaceSkip as exc:
                    result.status = TableStatus.SKIPPED_DESTINATION_SPACE.value
                    result.reason = str(exc)
                    result.warnings.append(
                        "Carga Bronze nao iniciada; nenhuma linha desta tabela foi importada."
                    )
                    result.next_action = (
                        "Libere ou amplie o espaco dos volumes do banco Bronze e retome "
                        "a mesma execucao."
                    )
                except SchemaEvolutionRequiredError as exc:
                    result.reason = redacted_exception(exc)
                    result.status = TableStatus.SCHEMA_EVOLUTION_PENDING.value
                    result.next_action = (
                        "Habilite allow_schema_evolution após revisar as colunas; "
                        "nenhum bloco foi importado."
                    )
                except SchemaEvolutionSafetyError as exc:
                    result.reason = redacted_exception(exc)
                    result.status = TableStatus.LAYOUT_ERROR.value
                    result.next_action = (
                        "Migre/backfill explicitamente a coluna NOT NULL antes de reenviar "
                        "o mesmo conjunto de manifestos."
                    )
                except Exception as exc:
                    result.reason = redacted_exception(exc)
                    result.status = (
                        TableStatus.PARTIAL_ERROR.value
                        if result.rows_imported
                        else TableStatus.IMPORT_ERROR.value
                    )
                    result.next_action = "Corrigir a causa e reenviar o mesmo conjunto de manifestos."
                    if table_registered:
                        try:
                            importer.finish_table(
                                manifest.execution_id,
                                manifest.dataset_id,
                                table_id,
                                state=result.status,
                                index_state=result.index_state,
                            )
                            table_finished = True
                        except Exception as control_error:
                            result.warnings.append(
                                "Falha ao registrar erro no controle SQL: "
                                + redacted_exception(control_error)
                            )
                finally:
                    local = state.table(manifest.execution_id, table_id)
                    if local is not None:
                        state.update_table(
                            manifest.execution_id,
                            table_id,
                            status=result.status,
                            index_state=result.index_state,
                            reason=result.reason,
                            warnings_json=stable_json(result.warnings),
                            finished_at=utc_now(),
                        )
                    if table_registered and not table_finished:
                        try:
                            importer.finish_table(
                                manifest.execution_id,
                                manifest.dataset_id,
                                table_id,
                                state=result.status,
                                index_state=result.index_state,
                            )
                            table_finished = True
                        except Exception as control_error:
                            result.warnings.append(
                                "Falha ao registrar estado terminal no controle SQL: "
                                + redacted_exception(control_error)
                            )
                if result.status in {
                    TableStatus.IMPORT_ERROR.value,
                    TableStatus.PARTIAL_ERROR.value,
                    TableStatus.LAYOUT_ERROR.value,
                    TableStatus.SCHEMA_EVOLUTION_PENDING.value,
                } and not self.config["continue_after_table_error"]:
                    break

            execution_state = (
                "COMPLETED" if report.exit_code() == 0 else "COMPLETED_WITH_PENDING_ITEMS"
            )
            if registered_any:
                importer.finish_execution(
                    first.execution_id, first.dataset_id, state=execution_state
                )
            state.finish_execution(first.execution_id, execution_state)
        except Exception as exc:
            report.global_error = redacted_exception(exc)
            if state is not None and state.execution(first.execution_id) is not None:
                try:
                    state.finish_execution(
                        first.execution_id,
                        "GLOBAL_ERROR",
                        "GLOBAL_ERROR",
                        report.global_error,
                    )
                except Exception:
                    pass
        finally:
            finish_timestamp(report)
            try:
                write_reports(report, store.execution_root)
            except Exception as report_error:
                if report.global_error is None:
                    report.global_error = "Falha ao gravar relatórios: " + redacted_exception(
                        report_error
                    )
            close_all([destination])
            leases.close()
            if state is not None:
                state.close()
            self.resolver.clear_cache()
        return report

    def status(self, execution_id: str) -> dict[str, Any] | None:
        validate_canonical_uuid(execution_id, "execution_id")
        with LocalState(self.config["local_control_directory"]) as state:
            return state.execution(execution_id)
