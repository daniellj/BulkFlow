"""Estimativas explicitas e conservadoras para planejamento do fluxo BCP.

Nenhuma estimativa e convertida em garantia. Ausencia de permissao, metadado
ou espaco observavel e representada por ``None``, nunca por zero ficticio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping, Sequence

from .catalog import qualified_table, query_rows
from .util import qi


METADATA_ROW_COUNT_METHOD = "sys.partitions.rows.index_id_in_0_1 (aproximado)"
SAMPLE_METHOD = "TOP limitado + DATALENGTH por coluna + overhead estimado do formato BCP"
SAMPLE_UNCERTAINTY = "amostra de conveniencia limitada; nao e garantia estatistica"


@dataclass(frozen=True)
class RowCountEstimate:
    value: int | None
    method: str
    observed_at: str
    approximate: bool
    uncertainty: str
    error: str | None = None


@dataclass(frozen=True)
class RowSizeEstimate:
    average_bytes: Decimal | None
    minimum_bytes: int | None
    maximum_bytes: int | None
    sampled_rows: int
    sample_limit: int
    method: str
    observed_at: str
    uncertainty: str
    error: str | None = None


@dataclass(frozen=True)
class SpacePlan:
    estimated_rows: int | None
    average_row_bytes: Decimal | None
    estimated_bcp_total_bytes: int | None
    estimated_bcp_block_bytes: int | None
    approximate_blocks: int | None
    planning_reserve_bytes: int | None
    predicted_peak_bytes: int | None
    available_bytes: int | None
    minimum_free_bytes: int
    capacity_ok: bool | None
    peak_model: str
    destination_data_bytes: int | None = None
    destination_log_bytes: int | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def metadata_row_count_sql() -> str:
    """Conta somente heap/clustered, sem somar cada indice secundario."""

    return """
SELECT SUM(CONVERT(bigint, p.[rows])) AS estimated_rows
FROM sys.partitions AS p
WHERE p.object_id = OBJECT_ID(?, N'U')
  AND p.index_id IN (0, 1);
"""


def estimate_row_count(
    connection: Any,
    schema: str,
    table: str,
    *,
    method: str = "metadata",
) -> RowCountEstimate:
    """Estima linhas; falha de observacao produz ``value=None``."""

    if method != "metadata":
        raise ValueError("count_method deve ser metadata")
    observed = _now()
    try:
        rows = query_rows(
            connection,
            metadata_row_count_sql(),
            (qualified_table(schema, table),),
        )
        value = None if not rows or rows[0]["estimated_rows"] is None else int(rows[0]["estimated_rows"])
        return RowCountEstimate(
            value=value,
            method=METADATA_ROW_COUNT_METHOD,
            observed_at=observed,
            approximate=True,
            uncertainty="contagem aproximada mantida pelo mecanismo",
        )
    except Exception as exc:
        return RowCountEstimate(
            value=None,
            method=METADATA_ROW_COUNT_METHOD,
            observed_at=observed,
            approximate=True,
            uncertainty="indisponivel",
            error=str(exc),
        )


def sample_row_size_sql(
    schema: str,
    table: str,
    columns: Sequence[str | Mapping[str, Any]],
    sample_limit: int,
    *,
    row_overhead_bytes: int = 8,
    field_overhead_bytes: int = 4,
) -> str:
    """Gera amostra TOP sem ORDER BY aleatorio nem varredura deliberada."""

    if sample_limit < 1:
        raise ValueError("sample_limit deve ser positivo")
    if row_overhead_bytes < 0 or field_overhead_bytes < 0:
        raise ValueError("Overheads da estimativa nao podem ser negativos")
    names = [item if isinstance(item, str) else str(item["name"]) for item in columns]
    base = row_overhead_bytes + field_overhead_bytes * len(names)
    length_terms = [
        f"COALESCE(CONVERT(bigint, DATALENGTH({qi(name)})), CONVERT(bigint, 0))"
        for name in names
    ]
    expression = "CONVERT(bigint, " + str(base) + ")"
    if length_terms:
        expression += " + " + " + ".join(length_terms)
    return f"""SELECT COUNT(*) AS sampled_rows,
       AVG(CONVERT(decimal(38,6), sample.row_bytes)) AS average_bytes,
       MIN(sample.row_bytes) AS minimum_bytes,
       MAX(sample.row_bytes) AS maximum_bytes
FROM (
 SELECT TOP ({int(sample_limit)}) {expression} AS row_bytes
 FROM {qualified_table(schema, table)}
) AS sample;"""


def estimate_row_size(
    connection: Any,
    schema: str,
    table: str,
    columns: Sequence[str | Mapping[str, Any]],
    sample_limit: int,
    *,
    row_overhead_bytes: int = 8,
    field_overhead_bytes: int = 4,
) -> RowSizeEstimate:
    observed = _now()
    try:
        sql = sample_row_size_sql(
            schema,
            table,
            columns,
            sample_limit,
            row_overhead_bytes=row_overhead_bytes,
            field_overhead_bytes=field_overhead_bytes,
        )
        rows = query_rows(connection, sql)
        row = rows[0] if rows else {}
        sampled = int(row.get("sampled_rows") or 0)
        average = row.get("average_bytes")
        return RowSizeEstimate(
            average_bytes=None if average is None else Decimal(str(average)),
            minimum_bytes=None if row.get("minimum_bytes") is None else int(row["minimum_bytes"]),
            maximum_bytes=None if row.get("maximum_bytes") is None else int(row["maximum_bytes"]),
            sampled_rows=sampled,
            sample_limit=int(sample_limit),
            method=SAMPLE_METHOD,
            observed_at=observed,
            uncertainty=SAMPLE_UNCERTAINTY,
        )
    except Exception as exc:
        return RowSizeEstimate(
            average_bytes=None,
            minimum_bytes=None,
            maximum_bytes=None,
            sampled_rows=0,
            sample_limit=int(sample_limit),
            method=SAMPLE_METHOD,
            observed_at=observed,
            uncertainty="indisponivel; " + SAMPLE_UNCERTAINTY,
            error=str(exc),
        )


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def calculate_space_plan(
    estimated_rows: int | None,
    average_row_bytes: Decimal | int | float | str | None,
    *,
    rows_per_block: int,
    safety_factor: Decimal | int | float | str,
    execute_import: bool,
    delete_confirmed: bool,
    retained_bytes: int = 0,
    pending_retry_blocks: int = 1,
    available_bytes: int | None = None,
    minimum_free_bytes: int = 0,
) -> SpacePlan:
    """Calcula acumulacao export-only ou pico por blocos com importacao.

    Espaco/log do banco destino permanecem ``None``: bytes BCP nao sao uma
    estimativa valida dessas grandezas.
    """

    if rows_per_block < 1:
        raise ValueError("rows_per_block deve ser positivo")
    factor = Decimal(str(safety_factor))
    if factor < Decimal("1"):
        raise ValueError("safety_factor deve ser maior ou igual a 1")
    if retained_bytes < 0 or pending_retry_blocks < 0 or minimum_free_bytes < 0:
        raise ValueError("Valores de espaco e tentativas nao podem ser negativos")
    if estimated_rows is not None and estimated_rows < 0:
        raise ValueError("estimated_rows nao pode ser negativo")

    average = None if average_row_bytes is None else Decimal(str(average_row_bytes))
    if average is not None and average < 0:
        raise ValueError("average_row_bytes nao pode ser negativo")

    if estimated_rows == 0:
        total = block = blocks = reserve = 0
        peak = retained_bytes
        if not execute_import:
            peak_model = "export_only_accumulated"
        elif not delete_confirmed:
            peak_model = "import_with_retention"
        else:
            peak_model = "block_import_with_post_commit_deletion"
    elif estimated_rows is None or average is None:
        total = block = blocks = reserve = peak = None
        peak_model = "unavailable"
    else:
        total = _ceil_decimal(Decimal(estimated_rows) * average)
        rows_in_block = min(estimated_rows, rows_per_block)
        block = _ceil_decimal(Decimal(rows_in_block) * average)
        blocks = (estimated_rows + rows_per_block - 1) // rows_per_block
        reserve = _ceil_decimal(Decimal(total) * factor)
        retain_all = (not execute_import) or (not delete_confirmed)
        if retain_all:
            peak = retained_bytes + reserve
            peak_model = "export_only_accumulated" if not execute_import else "import_with_retention"
        else:
            active_blocks = 1 + pending_retry_blocks
            peak = retained_bytes + _ceil_decimal(Decimal(block * active_blocks) * factor)
            peak_model = "block_import_with_post_commit_deletion"

    if available_bytes is None or peak is None:
        capacity = None
    else:
        capacity = available_bytes - minimum_free_bytes >= peak

    return SpacePlan(
        estimated_rows=estimated_rows,
        average_row_bytes=average,
        estimated_bcp_total_bytes=total,
        estimated_bcp_block_bytes=block,
        approximate_blocks=blocks,
        planning_reserve_bytes=reserve,
        predicted_peak_bytes=peak,
        available_bytes=available_bytes,
        minimum_free_bytes=int(minimum_free_bytes),
        capacity_ok=capacity,
        peak_model=peak_model,
    )


__all__ = [
    "METADATA_ROW_COUNT_METHOD",
    "SAMPLE_METHOD",
    "SAMPLE_UNCERTAINTY",
    "RowCountEstimate",
    "RowSizeEstimate",
    "SpacePlan",
    "metadata_row_count_sql",
    "estimate_row_count",
    "sample_row_size_sql",
    "estimate_row_size",
    "calculate_space_plan",
]
