"""Relatórios console/JSON/CSV sem dependência da CLI."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ExecutionReport
from .util import redact_structure, stable_json


def display(value: Any, suffix: str = "") -> str:
    return "indisponível" if value is None else f"{value}{suffix}"


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "indisponível"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{number:.2f} PiB"


def _first(plan: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in plan and plan[name] is not None:
            return plan[name]
    return default


def _endpoint_label(value: Any, *, source: bool = False) -> str:
    if not isinstance(value, Mapping):
        return str(value)
    database = value.get("read_database") if source else value.get("database")
    database = database or value.get("database")
    object_name = value.get("table")
    qualified = ".".join(
        str(part) for part in (database, value.get("schema"), object_name) if part
    )
    return "/".join(
        str(part) for part in (value.get("instance"), qualified) if part
    ) or "indisponível"


def _watermark_label(plan: Mapping[str, Any]) -> str:
    explicit = plan.get("watermark_description")
    if explicit:
        return str(explicit)
    watermark = plan.get("watermark")
    if not isinstance(watermark, list) or not watermark:
        return "não aplicável" if plan.get("transfer_mode") == "DIRECT_KEYLESS" else "indisponível"
    return ", ".join(
        f"{item.get('name', '?')} {item.get('direction', 'ASC')}"
        for item in watermark
        if isinstance(item, Mapping)
    )


def render_plan_rows(plans: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for plan in plans:
        source = _endpoint_label(plan.get("source"), source=True)
        destination_value = plan.get("destination")
        destination = (
            _endpoint_label(destination_value)
            if destination_value
            else "somente exportação"
        )
        lines.extend([
            f"{source} -> {destination}",
            f"  marca: {_watermark_label(plan)}",
            f"  loteamento: {_first(plan, 'batching_strategy', 'batch_strategy', default='keyset/grupo_completo')}",
            f"  linhas ({plan.get('row_count_method', 'indisponível')}): {display(plan.get('estimated_rows'))}",
            f"  banco origem utilizado: {format_bytes(_first(plan, 'source_database_used_bytes', 'source_database_bytes'))}",
            f"  BCP total estimado: {format_bytes(_first(plan, 'estimated_bcp_total_bytes', 'estimated_bcp_bytes'))}",
            f"  BCP por bloco estimado: {format_bytes(_first(plan, 'estimated_bcp_block_bytes', 'estimated_block_bytes'))}",
            f"  blocos aproximados: {display(_first(plan, 'approximate_blocks', 'estimated_blocks'))}",
            f"  espaço executor disponível: {format_bytes(_first(plan, 'executor_free_bytes', 'free_bytes'))}",
            f"  pico previsto: {format_bytes(_first(plan, 'predicted_peak_bytes', 'peak_bytes'))}",
            f"  reserva calculada: {format_bytes(plan.get('planning_reserve_bytes'))}",
            f"  fator de segurança: {display(plan.get('safety_factor'))}",
            f"  destino dados/log: {format_bytes(plan.get('destination_data_bytes'))} / "
            f"{format_bytes(plan.get('destination_log_bytes'))}",
            f"  amostra: {display(_first(plan, 'sampled_rows', 'sample_rows'))} linhas, "
            f"método={plan.get('sample_method', 'indisponível')}, "
            f"horário={_first(plan, 'sample_observed_at', 'sampled_at', default='indisponível')}, "
            f"incerteza={_first(plan, 'sample_uncertainty', 'uncertainty', default='indisponível')}",
        ])
        for warning in plan.get("warnings", []):
            lines.append(f"  ADVERTÊNCIA: {warning}")
    return "\n".join(lines)


def render_execution(report: ExecutionReport) -> str:
    lines = [
        f"Execução: {report.execution_id}",
        f"Comando: {report.command}",
        f"Consistência da origem: {report.consistency}",
    ]
    if report.cdc_database:
        cdc = report.cdc_database
        lines.append(
            "CDC do banco: "
            f"success={cdc.get('success')} stage={cdc.get('stage')} "
            f"retention_minutes={cdc.get('retention_minutes')} "
            f"retention_changed={cdc.get('retention_changed')} "
            f"retention_restarted={cdc.get('retention_restarted')}"
        )
    for table in report.tables:
        rate = table.throughput_bytes_per_second
        lines.append(
            f"{table.source_table} -> {table.destination_table or 'arquivos'} | {table.status} | "
            f"exportadas={table.rows_exported} importadas={table.rows_imported} "
            f"bytes={table.bytes_exported} throughput={format_bytes(rate)}/s"
        )
        if table.reason:
            lines.append(f"  motivo: {table.reason}")
        if table.next_action:
            lines.append(f"  próxima ação: {table.next_action}")
        for warning in table.warnings:
            lines.append(f"  ADVERTÊNCIA: {warning}")
    if report.global_error:
        lines.append(f"Falha global: {report.global_error}")
    elif report.exit_code() == 0:
        lines.append("Tudo que foi solicitado foi concluído.")
    else:
        lines.append("Fluxo encerrado com tabelas puladas, falhas parciais ou índices pendentes.")
    return "\n".join(lines)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    with partial.open("x", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def write_reports(report: ExecutionReport, directory: Path | str) -> tuple[Path, Path]:
    root = Path(directory)
    value = redact_structure(report.as_dict())
    json_path = root / f"report_{report.execution_id}.json"
    csv_path = root / f"report_{report.execution_id}.csv"
    _atomic_text(json_path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    columns = [
        "execution_id", "source_table", "destination_table", "status", "reason",
        "estimated_rows", "rows_exported", "rows_imported",
        "destination_rows_verified", "bytes_exported", "bytes_exported_this_invocation",
        "retained_files", "duration_seconds", "throughput_bytes_per_second",
        "watermark", "transfer_mode", "source_rows_at_capture", "final_limit",
        "last_exported", "last_imported", "index_state",
        "warnings", "next_action",
    ]
    import io
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for item in value["tables"]:
        row = dict(item)
        row["execution_id"] = report.execution_id
        for name in ("watermark", "final_limit", "last_exported", "last_imported", "warnings"):
            row[name] = stable_json(row.get(name))
        writer.writerow(row)
    _atomic_text(csv_path, output.getvalue())
    return json_path, csv_path


def finish_timestamp(report: ExecutionReport) -> None:
    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
