"""Validações de origem que não modificam banco ou isolamento global."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .catalog import qualified_table, query_rows
from .sql import scalar
from .util import sha256_json


def validate_source_columns(
    columns: Sequence[Mapping[str, Any]],
    *,
    metadata_mapping: Mapping[str, Any] | None = None,
) -> None:
    """Recusa semanticas que a copia nativa V2 nao promete preservar.

    O motor copia tipos escalares suportados sem transformar valores em texto.
    Colunas especiais exigem um perfil/mapeamento futuro explicitamente capaz
    de preservar sua semantica; portanto falham antes de exportar qualquer byte.
    """

    unsupported: list[str] = []
    for column in columns:
        name = str(column.get("name", "<sem-nome>"))
        reasons: list[str] = []
        if column.get("is_filestream"):
            reasons.append("FILESTREAM")
        if column.get("is_column_set"):
            reasons.append("COLUMN_SET")
        if column.get("is_hidden") or int(column.get("generated_always_type") or 0):
            reasons.append("hidden/generated_always")
        if column.get("encryption_type") is not None:
            reasons.append("Always Encrypted")
        if column.get("is_masked"):
            reasons.append("dynamic data masking")
        if int(column.get("xml_collection_id") or 0):
            reasons.append("XML tipado")
        if column.get("is_assembly_type"):
            reasons.append("tipo CLR")
        elif column.get("is_user_defined"):
            reasons.append("tipo alias definido pelo usuario")
        if reasons:
            unsupported.append(f"{name} ({', '.join(reasons)})")
    if unsupported:
        raise RuntimeError(
            "Colunas especiais nao suportadas pela copia nativa automatica: "
            + "; ".join(unsupported)
        )

    available = {str(column.get("name", "")).casefold() for column in columns}
    for target, mapping in (metadata_mapping or {}).items():
        if isinstance(mapping, Mapping) and "source_column" in mapping:
            source = str(mapping["source_column"])
            if source.casefold() not in available:
                raise RuntimeError(
                    f"metadata_mapping.{target}.source_column referencia coluna ausente: {source}"
                )


def database_identity(connection: Any) -> dict[str, Any]:
    rows = query_rows(connection, """
SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')) AS [server],
       name AS [database], database_id, create_date, source_database_id, state_desc,
       compatibility_level
FROM sys.databases WHERE database_id=DB_ID();""")
    if not rows:
        raise RuntimeError("Não foi possível identificar o banco de origem")
    row = rows[0]
    if row["state_desc"] != "ONLINE":
        raise RuntimeError("Banco de origem não está ONLINE")
    # is_read_only é deliberadamente ausente: READ_WRITE é suportado e uma
    # mudança dessa propriedade não invalida artefatos.
    return row


def validate_source_table(connection: Any, schema: str, table: str) -> dict[str, Any]:
    name = qualified_table(schema, table)
    rows = query_rows(connection, """
SELECT t.object_id,t.create_date,t.modify_date,t.is_memory_optimized,t.is_filetable,
       t.temporal_type
FROM sys.tables AS t WHERE t.object_id=OBJECT_ID(?,N'U');""", (name,))
    if not rows:
        raise RuntimeError(f"Tabela de origem ausente: {schema}.{table}")
    row = rows[0]
    if row["is_memory_optimized"] or row["is_filetable"] or row["temporal_type"]:
        raise RuntimeError(f"Tabela especial não suportada automaticamente: {schema}.{table}")
    if scalar(connection, """SELECT COUNT_BIG(*) FROM sys.security_predicates AS p
JOIN sys.security_policies AS s ON s.object_id=p.object_id
WHERE p.target_object_id=OBJECT_ID(?,N'U') AND s.is_enabled=1;""", (name,)):
        raise RuntimeError(f"RLS ativo exige revisão específica: {schema}.{table}")
    return row


def source_fingerprint(database: dict[str, Any], table: dict[str, Any], columns: list[dict[str, Any]]) -> str:
    return sha256_json({
        "database": {
            "server": database.get("server"),
            "database": database.get("database"),
            "database_id": database.get("database_id"),
            "create_date": database.get("create_date"),
            "source_database_id": database.get("source_database_id"),
        },
        "table": {"object_id": table.get("object_id"), "create_date": table.get("create_date")},
        "columns": columns,
    })


def validate_origin_id_column(connection: Any, schema: str, table: str, column: str) -> None:
    name = qualified_table(schema, table)
    result = query_rows(connection, f"""SELECT
 CASE WHEN EXISTS(SELECT 1 FROM {name} WHERE [{column.replace(']', ']]')}] IS NULL) THEN 1 ELSE 0 END AS has_null,
 CASE WHEN EXISTS(
   SELECT [{column.replace(']', ']]')}] FROM {name}
   GROUP BY [{column.replace(']', ']]')}] HAVING COUNT_BIG(*)>1
 ) THEN 1 ELSE 0 END AS has_duplicate;""")
    if not result:
        raise RuntimeError("Não foi possível validar source_column do ID Bronze")
    if result[0]["has_null"] or result[0]["has_duplicate"]:
        raise RuntimeError("source_column do ID Bronze contém NULL ou duplicidade")
