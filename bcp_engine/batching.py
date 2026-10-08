"""Keyset V2 para marcas simples/compostas, inclusive nao unicas.

Bookmarks sao persistidos como texto canonico e reconvertidos para o tipo SQL
na montagem dos predicados. Nenhum cursor depende de OFFSET, numero fisico,
concatenacao de campos ou comparacao de tuplas nao suportada pelo SQL Server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .catalog import WatermarkColumn, qualified_table, query_rows
from .util import collation_sql, qi, qs, type_sql


Bookmark = tuple[str, ...]


class BatchingError(RuntimeError):
    """Erro de contrato ou estado do loteamento."""


class DirectSourceDriftError(BatchingError):
    """A cardinalidade da tabela sem chave mudou antes/durante a exportacao."""


@dataclass(frozen=True)
class BatchWindow:
    """Faixa logica planejada; ``upper`` sempre representa um grupo completo."""

    lower: Bookmark | None
    upper: Bookmark
    ceiling: Bookmark
    observed_rows: int
    boundary_group_rows: int
    target_rows: int

    @property
    def empty_at_planning(self) -> bool:
        return self.observed_rows == 0


@dataclass(frozen=True)
class ExportObservation:
    """Resultado real do BCP associado a uma faixa ja planejada."""

    status: str
    actual_rows: int
    actual_bytes: int
    checkpoint: Bookmark
    import_required: bool


def _validate_bookmark(
    columns: Sequence[WatermarkColumn], values: Sequence[str] | None
) -> Bookmark | None:
    if values is None:
        return None
    if len(columns) != len(values):
        raise BatchingError("Bookmark incompativel com as colunas da marca d'agua")
    if any(value is None for value in values):
        raise BatchingError("Bookmark NULL nao e suportado")
    return tuple(str(value) for value in values)


def _type_declaration(column: WatermarkColumn) -> str:
    return type_sql(column.as_catalog_dict())


def bookmark_literal(column: WatermarkColumn, value: str) -> str:
    """Reconverte um bookmark textual sem perder escala ou precisao temporal."""

    if value is None:
        raise BatchingError("Bookmark NULL nao e suportado")
    literal = qs(str(value))
    if column.collation_name:
        literal += " COLLATE " + collation_sql(column.collation_name)
    if column.type_name in {"binary", "varbinary"}:
        style = ", 1"
    elif column.type_name in {
        "date",
        "datetime",
        "smalldatetime",
        "datetime2",
        "datetimeoffset",
        "time",
    }:
        style = ", 126"
    elif column.type_name in {"money", "smallmoney"}:
        style = ", 2"
    else:
        style = ""
    return f"CONVERT({_type_declaration(column)}, {literal}{style})"


def bookmark_text_expression(column: WatermarkColumn) -> str:
    """Serializacao SQL reversivel usada para limites e checkpoints."""

    name = qi(column.name)
    if column.type_name in {"binary", "varbinary"}:
        return f"CONVERT(nvarchar(max), {name}, 1)"
    if column.type_name in {
        "date",
        "datetime",
        "smalldatetime",
        "datetime2",
        "datetimeoffset",
        "time",
    }:
        return f"CONVERT(nvarchar(100), {name}, 126)"
    if column.type_name in {"money", "smallmoney"}:
        return f"CONVERT(nvarchar(100), {name}, 2)"
    return f"CONVERT(nvarchar(max), {name})"


def order_by(columns: Sequence[WatermarkColumn], *, reverse: bool = False) -> str:
    if not columns:
        raise BatchingError("Marca d'agua vazia")
    parts = []
    for column in columns:
        descending = column.descending != reverse
        parts.append(f"{qi(column.name)} {'DESC' if descending else 'ASC'}")
    return ", ".join(parts)


def equality_predicate(
    columns: Sequence[WatermarkColumn], values: Sequence[str]
) -> str:
    bookmark = _validate_bookmark(columns, values)
    assert bookmark is not None
    return "(" + " AND ".join(
        f"{qi(column.name)} = {bookmark_literal(column, value)}"
        for column, value in zip(columns, bookmark)
    ) + ")"


def after_predicate(
    columns: Sequence[WatermarkColumn], values: Sequence[str] | None
) -> str:
    """Comparacao lexicografica estrita na direcao logica da marca."""

    bookmark = _validate_bookmark(columns, values)
    if bookmark is None:
        return "1=1"
    terms: list[str] = []
    for position, (column, value) in enumerate(zip(columns, bookmark)):
        prefix = [
            f"{qi(previous.name)} = {bookmark_literal(previous, bookmark[index])}"
            for index, previous in enumerate(columns[:position])
        ]
        operator = "<" if column.descending else ">"
        prefix.append(f"{qi(column.name)} {operator} {bookmark_literal(column, value)}")
        terms.append("(" + " AND ".join(prefix) + ")")
    return "(" + " OR ".join(terms) + ")"


def at_or_before_predicate(
    columns: Sequence[WatermarkColumn], values: Sequence[str]
) -> str:
    """Comparacao lexicografica inclusiva ate o limite na ordem configurada."""

    bookmark = _validate_bookmark(columns, values)
    assert bookmark is not None
    terms: list[str] = []
    for position, (column, value) in enumerate(zip(columns, bookmark)):
        prefix = [
            f"{qi(previous.name)} = {bookmark_literal(previous, bookmark[index])}"
            for index, previous in enumerate(columns[:position])
        ]
        operator = ">" if column.descending else "<"
        prefix.append(f"{qi(column.name)} {operator} {bookmark_literal(column, value)}")
        terms.append("(" + " AND ".join(prefix) + ")")
    terms.append(equality_predicate(columns, bookmark))
    return "(" + " OR ".join(terms) + ")"


def range_predicate(
    columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    upper: Sequence[str],
    ceiling: Sequence[str],
) -> str:
    """Faixa ``lower < chave <= upper`` limitada tambem pelo teto inicial."""

    parts = [
        after_predicate(columns, lower),
        at_or_before_predicate(columns, upper),
        at_or_before_predicate(columns, ceiling),
    ]
    return " AND ".join(f"({part})" for part in parts)


def _bookmark_projection(columns: Sequence[WatermarkColumn]) -> str:
    return ", ".join(
        f"{bookmark_text_expression(column)} AS {qi(f'bookmark_{index}')}"
        for index, column in enumerate(columns)
    )


def _bookmark_from_row(
    row: Mapping[str, Any], columns: Sequence[WatermarkColumn]
) -> Bookmark:
    values = tuple(row[f"bookmark_{index}"] for index in range(len(columns)))
    bookmark = _validate_bookmark(columns, values)
    assert bookmark is not None
    return bookmark


def capture_ceiling_sql(
    schema: str, table: str, columns: Sequence[WatermarkColumn]
) -> str:
    """Seleciona uma tupla existente final; nunca MAX independente por coluna."""

    return (
        f"SELECT TOP (1) {_bookmark_projection(columns)} "
        f"FROM {qualified_table(schema, table)} "
        f"ORDER BY {order_by(columns, reverse=True)};"
    )


def capture_ceiling(
    connection: Any, schema: str, table: str, columns: Sequence[WatermarkColumn]
) -> Bookmark | None:
    rows = query_rows(connection, capture_ceiling_sql(schema, table, columns))
    return None if not rows else _bookmark_from_row(rows[0], columns)


def candidate_limit_sql(
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    ceiling: Sequence[str],
    target_rows: int,
) -> str:
    """Obtem o limite candidato N; a exportacao posterior inclui seus empates."""

    if target_rows < 1:
        raise BatchingError("target_rows deve ser positivo")
    ceiling_bookmark = _validate_bookmark(columns, ceiling)
    assert ceiling_bookmark is not None
    where = " AND ".join(
        [
            f"({after_predicate(columns, lower)})",
            f"({at_or_before_predicate(columns, ceiling_bookmark)})",
        ]
    )
    key_names = ", ".join(qi(column.name) for column in columns)
    return f"""WITH candidate AS (
 SELECT TOP ({int(target_rows)}) {key_names}
 FROM {qualified_table(schema, table)}
 WHERE {where}
 ORDER BY {order_by(columns)}
)
SELECT TOP (1) {_bookmark_projection(columns)}
FROM candidate
ORDER BY {order_by(columns, reverse=True)};"""


def find_candidate_limit(
    connection: Any,
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    ceiling: Sequence[str],
    target_rows: int,
) -> Bookmark | None:
    rows = query_rows(
        connection,
        candidate_limit_sql(schema, table, columns, lower, ceiling, target_rows),
    )
    return None if not rows else _bookmark_from_row(rows[0], columns)


def count_range_sql(
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    upper: Sequence[str],
    ceiling: Sequence[str],
) -> str:
    return (
        "SELECT COUNT_BIG(*) AS row_count FROM "
        + qualified_table(schema, table)
        + " WHERE "
        + range_predicate(columns, lower, upper, ceiling)
        + ";"
    )


def count_boundary_group_sql(
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
    upper: Sequence[str],
) -> str:
    return (
        "SELECT COUNT_BIG(*) AS row_count FROM "
        + qualified_table(schema, table)
        + " WHERE "
        + equality_predicate(columns, upper)
        + ";"
    )


def export_query(
    schema: str,
    table: str,
    export_columns: Sequence[str | Mapping[str, Any]],
    watermark_columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    upper: Sequence[str],
    ceiling: Sequence[str],
) -> str:
    """Consulta BCP explicita; o grupo do limite e sempre exportado inteiro."""

    if not export_columns:
        raise BatchingError("A projecao de exportacao nao pode ser vazia")
    names = [
        item if isinstance(item, str) else str(item["name"])
        for item in export_columns
    ]
    return (
        "SELECT "
        + ", ".join(qi(name) for name in names)
        + " FROM "
        + qualified_table(schema, table)
        + " WHERE "
        + range_predicate(watermark_columns, lower, upper, ceiling)
    )


def direct_keyless_export_query(
    schema: str,
    table: str,
    export_columns: Sequence[str | Mapping[str, Any]],
) -> str:
    """Exporta uma tabela sem chave inteira, sem inventar ordenacao/cursor.

    ``TABLOCK,HOLDLOCK`` mantem uma leitura estavel durante a instrucao BCP.
    Nao ha ``TOP``, ``OFFSET`` ou numeracao artificial: a pre-admissao usa
    metadados e o limite global e imposto sobre a contagem real do BCP antes
    da publicacao duravel.
    """

    if not export_columns:
        raise BatchingError("A projecao de exportacao nao pode ser vazia")
    names = [
        item if isinstance(item, str) else str(item["name"])
        for item in export_columns
    ]
    return (
        "SELECT "
        + ", ".join(qi(name) for name in names)
        + " FROM "
        + qualified_table(schema, table)
        + " WITH (HOLDLOCK, TABLOCK)"
    )


def direct_keyless_window(expected_rows: int) -> BatchWindow:
    """Cria o unico bloco de uma tabela sem chave.

    Arrays vazios sao sentinelas de ausencia deliberada de bookmark, nao uma
    marca d'agua sintetica. ``expected_rows`` e a estimativa de metadados
    usada no planejamento; a contagem real do BCP impoe o limite definitivo.
    """

    if expected_rows < 0:
        raise BatchingError("Contagem esperada da carga direta nao pode ser negativa")
    return BatchWindow(
        lower=None,
        upper=(),
        ceiling=(),
        observed_rows=int(expected_rows),
        boundary_group_rows=0,
        target_rows=int(expected_rows),
    )


def observe_direct_keyless_export(
    window: BatchWindow,
    actual_rows: int,
    actual_bytes: int,
    *,
    maximum_rows: int | None = None,
) -> ExportObservation:
    """Enforce the hard cap using the authoritative BCP row count."""

    if maximum_rows is None:
        # Compatibility for direct callers that still use an exact captured
        # count.  The engine always supplies the configured hard cap because
        # its pre-admission count now comes from approximate metadata.
        if actual_rows != window.observed_rows:
            raise DirectSourceDriftError(
                "KEYLESS_DIRECT_LOAD_SOURCE_DRIFT: BCP copiou "
                f"{actual_rows} linhas, mas a contagem esperada era "
                f"{window.observed_rows}; manifesto nao sera publicado"
            )
        return observe_export(window, actual_rows, actual_bytes)
    limit = int(maximum_rows)
    if limit < 0:
        raise BatchingError("Limite da carga direta nao pode ser negativo")
    if actual_rows > limit:
        raise DirectSourceDriftError(
            "KEYLESS_DIRECT_LOAD_OVER_LIMIT: BCP copiou "
            f"{actual_rows} linhas, acima do limite global {limit}; "
            "manifesto e importacao foram bloqueados"
        )
    return observe_export(window, actual_rows, actual_bytes)


def _scalar_count(connection: Any, sql: str) -> int:
    rows = query_rows(connection, sql)
    if not rows:
        raise BatchingError("Consulta de contagem nao retornou resultado")
    return int(rows[0]["row_count"])


def plan_next_batch(
    connection: Any,
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
    lower: Sequence[str] | None,
    ceiling: Sequence[str],
    target_rows: int,
) -> BatchWindow | None:
    """Planeja uma faixa, expandindo o limite candidato para todos os empates."""

    candidate = find_candidate_limit(
        connection, schema, table, columns, lower, ceiling, target_rows
    )
    if candidate is None:
        return None
    observed = _scalar_count(
        connection,
        count_range_sql(schema, table, columns, lower, candidate, ceiling),
    )
    group_rows = _scalar_count(
        connection,
        count_boundary_group_sql(schema, table, columns, candidate),
    )
    checked_lower = _validate_bookmark(columns, lower)
    checked_ceiling = _validate_bookmark(columns, ceiling)
    assert checked_ceiling is not None
    return BatchWindow(
        lower=checked_lower,
        upper=candidate,
        ceiling=checked_ceiling,
        observed_rows=observed,
        boundary_group_rows=group_rows,
        target_rows=int(target_rows),
    )


def observe_export(
    window: BatchWindow, actual_rows: int, actual_bytes: int
) -> ExportObservation:
    """Converte a contagem real do BCP em checkpoint, inclusive faixa vazia."""

    if actual_rows < 0 or actual_bytes < 0:
        raise BatchingError("Contagens reais de exportacao nao podem ser negativas")
    empty = actual_rows == 0
    return ExportObservation(
        status="EMPTY_RANGE_CONFIRMED" if empty else "EXPORTED",
        actual_rows=int(actual_rows),
        actual_bytes=int(actual_bytes),
        checkpoint=window.upper,
        import_required=not empty,
    )


__all__ = [
    "Bookmark",
    "BatchingError",
    "DirectSourceDriftError",
    "BatchWindow",
    "ExportObservation",
    "bookmark_literal",
    "bookmark_text_expression",
    "order_by",
    "equality_predicate",
    "after_predicate",
    "at_or_before_predicate",
    "range_predicate",
    "capture_ceiling_sql",
    "capture_ceiling",
    "candidate_limit_sql",
    "find_candidate_limit",
    "count_range_sql",
    "count_boundary_group_sql",
    "export_query",
    "direct_keyless_export_query",
    "direct_keyless_window",
    "observe_direct_keyless_export",
    "plan_next_batch",
    "observe_export",
]
