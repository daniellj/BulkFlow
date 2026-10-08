"""Inspecao somente-leitura do catalogo fisico do destino SQL Server.

O snapshot retornado e aceito diretamente por :func:`ddl.compare_catalog`.
Nenhuma funcao deste modulo cria, altera ou remove objetos.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .profiles import TableLayout
from .sql import fetch_dicts
from .util import qi, type_sql


def _qualified(schema: str, name: str) -> str:
    return f"{qi(schema)}.{qi(name)}"


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _object_row(connection: Any, schema: str, table: str) -> dict[str, Any] | None:
    rows = fetch_dicts(
        connection,
        """
SELECT o.object_id, o.type AS object_type, o.type_desc, o.create_date,
       SCHEMA_NAME(o.schema_id) AS schema_name, o.name
FROM sys.objects AS o
WHERE o.object_id = OBJECT_ID(?);
""",
        (_qualified(schema, table),),
    )
    return rows[0] if rows else None


def _columns(connection: Any, object_id: int) -> list[dict[str, Any]]:
    rows = fetch_dicts(
        connection,
        """
SELECT c.column_id, c.name, st.name AS type_name, c.max_length,
       c.precision, c.scale, c.collation_name, c.is_nullable,
       c.is_identity, CONVERT(decimal(38,0),ic.seed_value) AS identity_seed,
       CONVERT(decimal(38,0),ic.increment_value) AS identity_increment,
       c.is_computed, cc.definition AS computed_definition,
       ISNULL(cc.is_persisted, 0) AS is_persisted,
       dc.name AS default_name, dc.definition AS default_definition
FROM sys.columns AS c
JOIN sys.types AS st
  ON st.system_type_id = c.system_type_id
 AND st.user_type_id = st.system_type_id
LEFT JOIN sys.identity_columns AS ic
  ON ic.object_id = c.object_id AND ic.column_id = c.column_id
LEFT JOIN sys.computed_columns AS cc
  ON cc.object_id = c.object_id AND cc.column_id = c.column_id
LEFT JOIN sys.default_constraints AS dc
  ON dc.object_id = c.default_object_id
WHERE c.object_id = ?
ORDER BY c.column_id;
""",
        (object_id,),
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        record = {
            "column_id": int(row["column_id"]),
            "name": str(row["name"]),
            "type_name": str(row["type_name"]).casefold(),
            "max_length": int(row["max_length"]),
            "precision": int(row["precision"]),
            "scale": int(row["scale"]),
            "nullable": bool(row["is_nullable"]),
            "is_nullable": bool(row["is_nullable"]),
            "identity": bool(row["is_identity"]),
            "is_identity": bool(row["is_identity"]),
            "identity_seed": _optional_int(row.get("identity_seed")),
            "identity_increment": _optional_int(row.get("identity_increment")),
            "computed": bool(row["is_computed"]),
            "is_computed": bool(row["is_computed"]),
            "computed_expression": row.get("computed_definition"),
            "computed_definition": row.get("computed_definition"),
            "persisted": bool(row.get("is_persisted", False)),
            "is_persisted": bool(row.get("is_persisted", False)),
            "default_expression": row.get("default_definition"),
            "default_definition": row.get("default_definition"),
            "default_name": row.get("default_name"),
            "collation": row.get("collation_name"),
            "collation_name": row.get("collation_name"),
        }
        record["sql_type"] = type_sql(record)
        result.append(record)
    return result


def _indexes(connection: Any, object_id: int) -> list[dict[str, Any]]:
    rows = fetch_dicts(
        connection,
        """
SELECT i.index_id, i.name AS index_name, i.type, i.type_desc,
       i.is_unique, i.is_primary_key, i.is_disabled,
       i.is_hypothetical, i.has_filter, i.filter_definition,
       ic.index_column_id, ic.key_ordinal, ic.is_descending_key,
       ic.is_included_column, ic.partition_ordinal, c.name AS column_name,
       ds.name AS data_space_name, ds.type AS data_space_type
FROM sys.indexes AS i
LEFT JOIN sys.data_spaces AS ds ON ds.data_space_id = i.data_space_id
LEFT JOIN sys.index_columns AS ic
  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
LEFT JOIN sys.columns AS c
  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = ?
  AND i.index_id > 0
ORDER BY i.index_id,
         CASE WHEN ic.key_ordinal > 0 THEN 0 ELSE 1 END,
         ic.key_ordinal, ic.index_column_id;
""",
        (object_id,),
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["index_id"]), []).append(row)

    result: list[dict[str, Any]] = []
    for index_id in sorted(grouped):
        index_rows = grouped[index_id]
        first = index_rows[0]
        keys = [
            {
                "name": str(row["column_name"]),
                "direction": "DESC" if bool(row["is_descending_key"]) else "ASC",
            }
            for row in sorted(index_rows, key=lambda item: int(item.get("key_ordinal") or 0))
            if int(row.get("key_ordinal") or 0) > 0 and row.get("column_name") is not None
        ]
        includes = [
            str(row["column_name"])
            for row in sorted(index_rows, key=lambda item: int(item.get("index_column_id") or 0))
            if bool(row.get("is_included_column")) and row.get("column_name") is not None
        ]
        type_desc = str(first.get("type_desc") or "")
        partition_columns = [
            str(row["column_name"])
            for row in sorted(
                index_rows, key=lambda item: int(item.get("partition_ordinal") or 0)
            )
            if int(row.get("partition_ordinal") or 0) > 0
            and row.get("column_name") is not None
        ]
        result.append(
            {
                "index_id": index_id,
                "name": str(first.get("index_name") or ""),
                "type": int(first["type"]),
                "type_desc": type_desc,
                "keys": keys,
                "includes": includes,
                "unique": bool(first["is_unique"]),
                "is_unique": bool(first["is_unique"]),
                "clustered": type_desc.upper().startswith("CLUSTERED"),
                "primary_key": bool(first["is_primary_key"]),
                "is_primary_key": bool(first["is_primary_key"]),
                "disabled": bool(first["is_disabled"]),
                "is_disabled": bool(first["is_disabled"]),
                "hypothetical": bool(first.get("is_hypothetical", False)),
                "is_hypothetical": bool(first.get("is_hypothetical", False)),
                "has_filter": bool(first["has_filter"]),
                "filter_predicate": first.get("filter_definition"),
                "filter_definition": first.get("filter_definition"),
                "data_space_name": (
                    str(first["data_space_name"])
                    if first.get("data_space_name") is not None
                    else None
                ),
                "data_space_type": (
                    str(first["data_space_type"])
                    if first.get("data_space_type") is not None
                    else None
                ),
                "partition_column": (
                    partition_columns[0] if partition_columns else None
                ),
            }
        )
    return result


def _partitioning(connection: Any, object_id: int) -> dict[str, Any] | None:
    rows = fetch_dicts(
        connection,
        """
SELECT TOP (1)
       ps.name AS scheme_name,
       pf.name AS function_name,
       pf.boundary_value_on_right,
       typ.name AS type_name,
       pp.max_length, pp.precision, pp.scale,
       c.name AS partition_column
FROM sys.indexes AS i
JOIN sys.partition_schemes AS ps ON ps.data_space_id = i.data_space_id
JOIN sys.partition_functions AS pf ON pf.function_id = ps.function_id
JOIN sys.partition_parameters AS pp ON pp.function_id = pf.function_id
JOIN sys.types AS typ ON typ.user_type_id = pp.user_type_id
JOIN sys.index_columns AS ic
  ON ic.object_id = i.object_id
 AND ic.index_id = i.index_id
 AND ic.partition_ordinal = pp.parameter_id
JOIN sys.columns AS c
  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = ?
  AND i.type IN (0, 1)
ORDER BY i.index_id;
""",
        (object_id,),
    )
    if not rows:
        return None
    row = rows[0]
    scheme_name = str(row["scheme_name"])
    function_name = str(row["function_name"])
    boundaries = fetch_dicts(
        connection,
        """
SELECT CONVERT(nvarchar(40), CONVERT(datetime2(7), prv.value), 126) AS boundary_value
FROM sys.partition_range_values AS prv
JOIN sys.partition_functions AS pf ON pf.function_id = prv.function_id
WHERE pf.name = ?
ORDER BY prv.boundary_id;
""",
        (function_name,),
    )
    filegroups = fetch_dicts(
        connection,
        """
SELECT DISTINCT fg.name AS filegroup_name
FROM sys.partition_schemes AS ps
JOIN sys.destination_data_spaces AS dds
  ON dds.partition_scheme_id = ps.data_space_id
JOIN sys.filegroups AS fg ON fg.data_space_id = dds.data_space_id
WHERE ps.name = ?
ORDER BY fg.name;
""",
        (scheme_name,),
    )
    return {
        "scheme_name": scheme_name,
        "function_name": function_name,
        "boundary_on_right": bool(row["boundary_value_on_right"]),
        "type_name": str(row["type_name"]).casefold(),
        "max_length": int(row["max_length"]),
        "precision": int(row["precision"]),
        "scale": int(row["scale"]),
        "partition_column": str(row["partition_column"]),
        "boundaries": [str(item["boundary_value"]) for item in boundaries],
        "filegroups": [str(item["filegroup_name"]) for item in filegroups],
    }


def _row_count(connection: Any, object_id: int) -> int | None:
    rows = fetch_dicts(
        connection,
        """
SELECT SUM(CONVERT(bigint, p.[rows])) AS row_count
FROM sys.partitions AS p
WHERE p.object_id = ?
  AND p.index_id IN (0, 1);
""",
        (object_id,),
    )
    if not rows or rows[0].get("row_count") is None:
        return None
    return int(rows[0]["row_count"])


def _sequence(
    connection: Any, schema: str, sequence_name: str | None
) -> dict[str, Any] | None:
    if sequence_name is None:
        return None
    rows = fetch_dicts(
        connection,
        """
SELECT seq.name, st.name AS type_name,
       CONVERT(decimal(38,0),seq.start_value) AS start_value,
       CONVERT(decimal(38,0),seq.increment) AS increment_by,
       CONVERT(decimal(38,0),seq.minimum_value) AS minimum_value,
       CONVERT(decimal(38,0),seq.maximum_value) AS maximum_value,
       seq.is_cycling, seq.is_cached,
       CONVERT(decimal(38,0),seq.cache_size) AS cache_size,
       CONVERT(decimal(38,0),seq.current_value) AS current_value
FROM sys.sequences AS seq
JOIN sys.types AS st
  ON st.system_type_id = seq.system_type_id
 AND st.user_type_id = st.system_type_id
WHERE seq.schema_id = SCHEMA_ID(?)
  AND seq.name = ?;
""",
        (schema, sequence_name),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "name": str(row["name"]),
        "sql_type": str(row["type_name"]),
        "type_name": str(row["type_name"]),
        "start": _optional_int(row.get("start_value")),
        "increment": int(row["increment_by"]),
        "increment_by": int(row["increment_by"]),
        "minimum_value": _optional_int(row.get("minimum_value")),
        "maximum_value": _optional_int(row.get("maximum_value")),
        "cycle": bool(row["is_cycling"]),
        "is_cycling": bool(row["is_cycling"]),
        "is_cached": bool(row.get("is_cached", False)),
        "cache_size": _optional_int(row.get("cache_size")),
        "current_value": _optional_int(row.get("current_value")),
    }


def inspect_destination_catalog(
    connection: Any,
    schema: str,
    table: str,
    *,
    sequence_name: str | None = None,
) -> dict[str, Any]:
    """Retorna snapshot completo e canônico do objeto de destino."""

    object_row = _object_row(connection, schema, table)
    sequence = _sequence(connection, schema, sequence_name)
    if object_row is None:
        return {
            "exists": False,
            "object_type": None,
            "schema": schema,
            "table": table,
            "row_count": None,
            "columns": [],
            "primary_key": None,
            "indexes": [],
            "sequence": sequence,
            "partition": None,
        }

    object_id = int(object_row["object_id"])
    columns = _columns(connection, object_id)
    indexes = _indexes(connection, object_id)
    primary = next((index for index in indexes if index["primary_key"]), None)
    base_index = next((index for index in indexes if index["clustered"]), None)
    partition = (
        _partitioning(connection, object_id)
        if base_index is not None and base_index.get("data_space_type") == "PS"
        else None
    )
    return {
        "exists": True,
        "object_id": object_id,
        "object_type": str(object_row["object_type"]).strip(),
        "type_desc": object_row.get("type_desc"),
        "create_date": object_row.get("create_date"),
        "schema": str(object_row.get("schema_name") or schema),
        "table": str(object_row.get("name") or table),
        "row_count": _row_count(connection, object_id),
        "columns": columns,
        "primary_key": primary,
        "indexes": indexes,
        "sequence": sequence,
        "partition": partition,
    }


def inspect_layout_catalog(connection: Any, layout: TableLayout) -> dict[str, Any]:
    """Atalho que usa schema, tabela e sequence do perfil resolvido."""

    return inspect_destination_catalog(
        connection,
        layout.schema,
        layout.table,
        sequence_name=layout.sequence.name if layout.sequence else None,
    )


__all__ = ["inspect_destination_catalog", "inspect_layout_catalog"]
