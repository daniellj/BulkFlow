"""Importação bulk transacional e idempotente a partir de manifestos V2."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

from .artifacts import ArtifactStore
from .models import (
    SUPPORTED_WATERMARK_TYPES,
    BlockManifest,
    ExecutionState,
    IndexState,
    TableStatus,
)
from .sql import execute, scalar
from .util import collation_sql, qi, qs, target_lock_resource


_INTEGER_WATERMARK_TYPES = frozenset({"bigint", "int", "smallint", "tinyint"})
_DECIMAL_WATERMARK_TYPES = frozenset({"decimal", "numeric"})
_STRING_WATERMARK_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar"})
_TEMPORAL_WATERMARK_TYPES = frozenset(
    {"date", "datetime", "smalldatetime", "datetime2", "datetimeoffset", "time"}
)
def _decimal_conversion_type(manifest: BlockManifest, position: int) -> str:
    """Escolhe escala que represente exatamente os bookmarks decimais."""

    values: list[str] = [manifest.upper_bound[position], manifest.final_limit[position]]
    if manifest.lower_bound is not None:
        values.append(manifest.lower_bound[position])
    scale = 0
    integer_digits = 1
    for value in values:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if not parsed.is_finite():
            continue
        _sign, digits, exponent = parsed.as_tuple()
        current_scale = max(0, -exponent)
        current_integer = (
            max(1, len(digits) + exponent)
            if exponent >= 0
            else max(1, len(digits) - current_scale)
        )
        scale = max(scale, current_scale)
        integer_digits = max(integer_digits, current_integer)
    # Nao arredondar silenciosamente. Se o texto nao couber, esta declaracao
    # faz TRY_CONVERT devolver NULL e o SQL recusa o manifesto.
    if scale > 38 or integer_digits + scale > 38:
        return "decimal(38,38)"
    return f"decimal(38,{scale})"


def _watermark_specs(manifest: BlockManifest) -> list[dict[str, str | None]]:
    specs: list[dict[str, str | None]] = []
    for position, item in enumerate(manifest.watermark):
        kind = str(item.get("type_name", "")).casefold()
        if kind not in SUPPORTED_WATERMARK_TYPES:
            raise ValueError(
                f"Tipo da marca d'agua ausente ou nao suportado no manifesto: posicao {position}"
            )
        direction = str(item.get("direction", "ASC")).upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError(f"Ordem invalida da marca d'agua na posicao {position}")
        collation = item.get("collation")
        if collation is not None and not isinstance(collation, str):
            raise ValueError(f"Collation invalida da marca d'agua na posicao {position}")
        specs.append({"kind": kind, "direction": direction, "collation": collation})
    return specs


def _json_bookmark_value(
    variable: str,
    position: int,
    spec: Mapping[str, str | None],
    manifest: BlockManifest,
) -> str:
    raw = (
        f"(SELECT [value] FROM OPENJSON({variable}) "
        f"WHERE [key]=N'{position}')"
    )
    kind = str(spec["kind"])
    if kind in _STRING_WATERMARK_TYPES:
        expression = f"CONVERT(nvarchar(max),{raw})"
        if spec.get("collation"):
            expression += " COLLATE " + collation_sql(str(spec["collation"]))
        return expression
    if kind in _INTEGER_WATERMARK_TYPES:
        declaration = kind
        style = ""
    elif kind == "bit":
        # SQL Server armazena bit, mas a relacao de ordem e comparada como 0/1.
        declaration = "tinyint"
        style = ""
    elif kind in _DECIMAL_WATERMARK_TYPES:
        declaration = _decimal_conversion_type(manifest, position)
        style = ""
    elif kind in {"binary", "varbinary"}:
        declaration = "varbinary(max)"
        style = ",1"
    elif kind in _TEMPORAL_WATERMARK_TYPES:
        declaration = {
            "datetime2": "datetime2(7)",
            "datetimeoffset": "datetimeoffset(7)",
            "time": "time(7)",
        }.get(kind, kind)
        style = ",126"
    elif kind in {"money", "smallmoney"}:
        declaration = kind
        style = ",2"
    else:
        declaration = kind
        style = ""
    return f"TRY_CONVERT({declaration},{raw}{style})"


def _bookmark_order_guard(manifest: BlockManifest) -> str:
    """SQL que valida forma, conversion, progresso e teto dos bookmarks."""

    if manifest.transfer_mode == "DIRECT_KEYLESS":
        # [] representa deliberadamente a ausencia de bookmark. A cardinalidade
        # previa e apenas uma estimativa de catalogo. A quantidade autoritativa
        # e rows_exported do BCP, conferida novamente contra ROWCOUNT_BIG da
        # insercao abaixo; nenhuma coluna e promovida a cursor.
        return f"""IF @lower IS NOT NULL
   OR ISJSON(@upper)<>1 OR (SELECT COUNT_BIG(*) FROM OPENJSON(@upper))<>0
   OR ISJSON(@ceiling)<>1 OR (SELECT COUNT_BIG(*) FROM OPENJSON(@ceiling))<>0
   THROW 51109,'Contrato da carga direta sem chave invalido.',1;"""

    if manifest.table_empty:
        # Uma tabela vazia comprovada usa um sentinela portavel: os dois
        # limites sao arrays vazios e nao existe limite inferior.
        return """IF @lower IS NOT NULL
   OR ISJSON(@upper)<>1 OR (SELECT COUNT_BIG(*) FROM OPENJSON(@upper))<>0
   OR ISJSON(@ceiling)<>1 OR (SELECT COUNT_BIG(*) FROM OPENJSON(@ceiling))<>0
   THROW 51109,'Sentinela de tabela vazia invalido.',1;"""

    specs = _watermark_specs(manifest)
    width = len(specs)
    upper = [
        _json_bookmark_value("@upper", position, spec, manifest)
        for position, spec in enumerate(specs)
    ]
    ceiling = [
        _json_bookmark_value("@ceiling", position, spec, manifest)
        for position, spec in enumerate(specs)
    ]
    lower = [
        _json_bookmark_value("@lower", position, spec, manifest)
        for position, spec in enumerate(specs)
    ]

    invalid_conversion = " OR ".join(
        f"{expression} IS NULL" for expression in [*upper, *ceiling]
    )
    invalid_lower = " OR ".join(f"{expression} IS NULL" for expression in lower)

    def lexicographic(left: Sequence[str], right: Sequence[str], *, inclusive: bool) -> str:
        terms: list[str] = []
        for position, spec in enumerate(specs):
            prefix = [f"{left[index]}={right[index]}" for index in range(position)]
            operator = "<" if spec["direction"] == "ASC" else ">"
            prefix.append(f"{left[position]}{operator}{right[position]}")
            terms.append("(" + " AND ".join(prefix) + ")")
        if inclusive:
            terms.append(
                "(" + " AND ".join(
                    f"{left[index]}={right[index]}" for index in range(width)
                ) + ")"
            )
        return "(" + " OR ".join(terms) + ")"

    upper_within_ceiling = lexicographic(upper, ceiling, inclusive=True)
    lower_before_upper = lexicographic(lower, upper, inclusive=False)
    return f"""IF ISJSON(@upper)<>1 OR ISJSON(@ceiling)<>1
   OR (SELECT COUNT_BIG(*) FROM OPENJSON(@upper))<>{width}
   OR (SELECT COUNT_BIG(*) FROM OPENJSON(@ceiling))<>{width}
   OR {invalid_conversion}
   THROW 51109,'Bookmarks superior/teto invalidos para a marca d''agua.',1;
 IF NOT {upper_within_ceiling}
   THROW 51108,'Limite superior ultrapassa o teto final da execucao.',1;
 IF @lower IS NOT NULL
 BEGIN
   IF ISJSON(@lower)<>1 OR (SELECT COUNT_BIG(*) FROM OPENJSON(@lower))<>{width}
      OR {invalid_lower}
     THROW 51109,'Bookmark inferior invalido para a marca d''agua.',1;
   IF NOT {lower_before_upper}
     THROW 51107,'Limite superior nao avanca o checkpoint na ordem da marca d''agua.',1;
 END;"""


def control_object_names(control_schema: str) -> dict[str, str]:
    if control_schema != "dbo":
        raise ValueError(
            "control_schema deve ser dbo; o controle SQL persistente possui "
            "contrato fixo no schema dbo do destino de dados"
        )
    schema = qi(control_schema)
    return {
        "version": f"{schema}.[ctl_exec_versao]",
        "execution": f"{schema}.[ctl_exec]",
        "table": f"{schema}.[ctl_exec_tabela]",
        "block": f"{schema}.[ctl_exec_lote]",
    }


_SQL_CONTROL_COLUMNS: dict[
    str, tuple[tuple[str, str, int, int, int, int, int], ...]
] = {
    # name, system type, max_length, precision, scale, nullable, uses DB collation
    "version": (
        ("version", "int", 4, 10, 0, 0, 0),
        ("created_at", "datetime2", 8, 27, 7, 0, 0),
    ),
    "execution": (
        ("execution_id", "uniqueidentifier", 16, 0, 0, 0, 0),
        ("dataset_id", "varchar", 64, 0, 0, 0, 1),
        ("structural_hash", "char", 64, 0, 0, 0, 1),
        ("destination_area", "varchar", 16, 0, 0, 0, 1),
        ("started_at", "datetime2", 8, 27, 7, 0, 0),
        ("updated_at", "datetime2", 8, 27, 7, 0, 0),
        ("state", "varchar", 50, 0, 0, 0, 1),
    ),
    "table": (
        ("execution_id", "uniqueidentifier", 16, 0, 0, 0, 0),
        ("dataset_id", "varchar", 64, 0, 0, 0, 1),
        ("table_id", "varchar", 64, 0, 0, 0, 1),
        ("destination_schema", "nvarchar", 256, 0, 0, 0, 1),
        ("destination_table", "nvarchar", 256, 0, 0, 0, 1),
        ("layout_hash", "char", 64, 0, 0, 0, 1),
        ("projection_hash", "char", 64, 0, 0, 0, 1),
        ("final_limit_json", "nvarchar", -1, 0, 0, 1, 1),
        ("last_exported_block", "bigint", 8, 19, 0, 0, 0),
        ("last_imported_block", "bigint", 8, 19, 0, 0, 0),
        ("import_cursor_json", "nvarchar", -1, 0, 0, 1, 1),
        ("imported_rows", "bigint", 8, 19, 0, 0, 0),
        ("state", "varchar", 50, 0, 0, 0, 1),
        ("index_state", "varchar", 50, 0, 0, 0, 1),
        ("updated_at", "datetime2", 8, 27, 7, 0, 0),
    ),
    "block": (
        ("execution_id", "uniqueidentifier", 16, 0, 0, 0, 0),
        ("dataset_id", "varchar", 64, 0, 0, 0, 1),
        ("table_id", "varchar", 64, 0, 0, 0, 1),
        ("block_id", "uniqueidentifier", 16, 0, 0, 0, 0),
        ("block_number", "bigint", 8, 19, 0, 0, 0),
        ("lower_bound_json", "nvarchar", -1, 0, 0, 1, 1),
        ("upper_bound_json", "nvarchar", -1, 0, 0, 0, 1),
        ("manifest_rows", "bigint", 8, 19, 0, 0, 0),
        ("imported_rows", "bigint", 8, 19, 0, 0, 0),
        ("file_bytes", "bigint", 8, 19, 0, 0, 0),
        ("data_sha256", "char", 64, 0, 0, 1, 1),
        ("format_sha256", "char", 64, 0, 0, 0, 1),
        ("data_file_name", "nvarchar", 2048, 0, 0, 1, 1),
        ("format_file_name", "nvarchar", 2048, 0, 0, 0, 1),
        ("import_started_at", "datetime2", 8, 27, 7, 0, 0),
        ("commit_recorded_at", "datetime2", 8, 27, 7, 0, 0),
    ),
}


def _control_validation_sql(names: Mapping[str, str]) -> str:
    column_rows: list[str] = []
    for object_key, columns in _SQL_CONTROL_COLUMNS.items():
        for column_id, column in enumerate(columns, 1):
            name, type_name, max_length, precision, scale, nullable, collated = column
            column_rows.append(
                "(" + ",".join(
                    (
                        qs(names[object_key]),
                        str(column_id),
                        qs(name),
                        qs(type_name),
                        str(max_length),
                        str(precision),
                        str(scale),
                        str(nullable),
                        str(collated),
                    )
                ) + ")"
            )
    column_values = ",\n   ".join(column_rows)
    expected_column_count = sum(len(columns) for columns in _SQL_CONTROL_COLUMNS.values())
    object_ids = ",".join(
        f"OBJECT_ID({qs(name)},N'U')" for name in names.values()
    )

    index_rows = (
        ("version", "pk_ctl_exec_versao", 1, 1, 1, 0, "version:ASC"),
        ("execution", "pk_ctl_exec", 1, 1, 1, 0, "execution_id:ASC|dataset_id:ASC"),
        ("table", "pk_ctl_exec_tabela", 1, 1, 1, 0, "execution_id:ASC|dataset_id:ASC|table_id:ASC"),
        ("table", "uq_ctl_exec_tabela_destino", 2, 1, 0, 0, "destination_schema:ASC|destination_table:ASC"),
        ("block", "pk_ctl_exec_lote", 1, 1, 1, 0, "execution_id:ASC|dataset_id:ASC|table_id:ASC|block_id:ASC"),
        ("block", "uq_ctl_exec_lote_numero", 2, 1, 0, 1, "execution_id:ASC|dataset_id:ASC|table_id:ASC|block_number:ASC"),
    )
    index_values = ",\n   ".join(
        "(" + ",".join(
            (
                qs(names[object_key]),
                qs(index_name),
                str(index_type),
                str(unique),
                str(primary),
                str(unique_constraint),
                qs(keys),
            )
        ) + ")"
        for object_key, index_name, index_type, unique, primary, unique_constraint, keys
        in index_rows
    )

    default_rows = (
        ("version", "created_at", "df_ctl_exec_versao_criado_em", "sysdatetime"),
        ("table", "last_exported_block", "df_ctl_exec_tabela_ultimo_lote_exportado", "0"),
        ("table", "last_imported_block", "df_ctl_exec_tabela_ultimo_lote_importado", "0"),
        ("table", "imported_rows", "df_ctl_exec_tabela_linhas_importadas", "0"),
    )
    default_values = ",\n   ".join(
        f"({qs(names[object_key])},{qs(column_name)},{qs(constraint_name)},{qs(definition)})"
        for object_key, column_name, constraint_name, definition in default_rows
    )

    foreign_key_rows = (
        ("table", "fk_ctl_exec_tabela_exec", 1, "execution_id", "execution", "execution_id"),
        ("table", "fk_ctl_exec_tabela_exec", 2, "dataset_id", "execution", "dataset_id"),
        ("block", "fk_ctl_exec_lote_exec_tabela", 1, "execution_id", "table", "execution_id"),
        ("block", "fk_ctl_exec_lote_exec_tabela", 2, "dataset_id", "table", "dataset_id"),
        ("block", "fk_ctl_exec_lote_exec_tabela", 3, "table_id", "table", "table_id"),
    )
    foreign_key_values = ",\n   ".join(
        "(" + ",".join(
            (
                qs(names[parent_key]),
                qs(constraint_name),
                str(ordinal),
                qs(parent_column),
                qs(names[referenced_key]),
                qs(referenced_column),
            )
        ) + ")"
        for parent_key, constraint_name, ordinal, parent_column, referenced_key, referenced_column
        in foreign_key_rows
    )

    return f"""
 IF (SELECT COUNT_BIG(*) FROM {names['version']})<>1
    OR NOT EXISTS(SELECT 1 FROM {names['version']} WHERE version=3)
   THROW 51100,'Controle SQL incompativel: versao deve conter exatamente a linha 3.',1;

 DECLARE @expected_columns table(
   object_name nvarchar(517) NOT NULL,
   column_id int NOT NULL,
   column_name sysname NOT NULL,
   type_name sysname NOT NULL,
   max_length smallint NOT NULL,
   [precision] tinyint NOT NULL,
   scale tinyint NOT NULL,
   is_nullable bit NOT NULL,
   uses_database_collation bit NOT NULL
 );
 INSERT @expected_columns VALUES
   {column_values};
 IF (SELECT COUNT_BIG(*) FROM sys.columns WHERE object_id IN({object_ids}))<>{expected_column_count}
   THROW 51101,'Controle SQL incompativel: quantidade de colunas divergente.',1;
 IF EXISTS(
   SELECT 1
   FROM @expected_columns AS e
   LEFT JOIN sys.columns AS c
     ON c.object_id=OBJECT_ID(e.object_name,N'U') AND c.column_id=e.column_id
   WHERE c.column_id IS NULL
      OR c.name<>e.column_name
      OR TYPE_NAME(c.system_type_id)<>e.type_name
      OR c.max_length<>e.max_length OR c.[precision]<>e.[precision] OR c.scale<>e.scale
      OR c.is_nullable<>e.is_nullable
      OR c.is_identity<>0 OR c.is_computed<>0 OR c.is_rowguidcol<>0
      OR c.is_filestream<>0 OR c.is_sparse<>0 OR c.is_column_set<>0
      OR c.generated_always_type<>0 OR c.encryption_type IS NOT NULL
      OR c.is_hidden<>0 OR c.is_masked<>0
      OR (e.uses_database_collation=1 AND
          c.collation_name<>CONVERT(sysname,DATABASEPROPERTYEX(DB_NAME(),N'Collation')))
      OR (e.uses_database_collation=0 AND c.collation_name IS NOT NULL)
 ) THROW 51101,'Controle SQL incompativel: assinatura de coluna divergente.',1;
 IF EXISTS(
   SELECT 1 FROM sys.tables
   WHERE object_id IN({object_ids})
     AND (is_memory_optimized<>0 OR temporal_type<>0 OR is_filetable<>0)
 ) THROW 51101,'Controle SQL incompativel: opcoes fisicas de tabela divergentes.',1;

 DECLARE @expected_indexes table(
   object_name nvarchar(517) NOT NULL,
   index_name sysname NOT NULL,
   index_type tinyint NOT NULL,
   is_unique bit NOT NULL,
   is_primary_key bit NOT NULL,
   is_unique_constraint bit NOT NULL,
   key_signature nvarchar(max) NOT NULL
 );
 INSERT @expected_indexes VALUES
   {index_values};
 IF (SELECT COUNT_BIG(*) FROM sys.indexes
     WHERE object_id IN({object_ids}) AND index_id>0)<>{len(index_rows)}
   THROW 51102,'Controle SQL incompativel: quantidade de indices divergente.',1;
 IF EXISTS(
   SELECT 1
   FROM @expected_indexes AS e
   LEFT JOIN sys.indexes AS i
     ON i.object_id=OBJECT_ID(e.object_name,N'U') AND i.name=e.index_name
   OUTER APPLY(
     SELECT STRING_AGG(CONVERT(nvarchar(max),c.name+N':'
              +CASE WHEN ic.is_descending_key=1 THEN N'DESC' ELSE N'ASC' END),N'|')
              WITHIN GROUP(ORDER BY ic.key_ordinal) AS key_signature,
            COUNT_BIG(*) AS key_count
     FROM sys.index_columns AS ic
     JOIN sys.columns AS c
       ON c.object_id=ic.object_id AND c.column_id=ic.column_id
     WHERE ic.object_id=i.object_id AND ic.index_id=i.index_id
       AND ic.key_ordinal>0 AND ic.is_included_column=0
   ) AS k
   WHERE i.index_id IS NULL OR i.type<>e.index_type OR i.is_unique<>e.is_unique
      OR i.is_primary_key<>e.is_primary_key
      OR i.is_unique_constraint<>e.is_unique_constraint
      OR i.is_disabled<>0 OR i.is_hypothetical<>0 OR i.has_filter<>0
      OR i.filter_definition IS NOT NULL OR i.ignore_dup_key<>0
      OR i.allow_row_locks<>1 OR i.allow_page_locks<>1
      OR i.fill_factor<>0 OR i.optimize_for_sequential_key<>0
      OR k.key_signature<>e.key_signature
      OR EXISTS(SELECT 1 FROM sys.index_columns AS included
                WHERE included.object_id=i.object_id AND included.index_id=i.index_id
                  AND (included.is_included_column=1 OR included.key_ordinal=0))
 ) THROW 51102,'Controle SQL incompativel: assinatura de indice divergente.',1;

 DECLARE @expected_defaults table(
   object_name nvarchar(517) NOT NULL,
   column_name sysname NOT NULL,
   constraint_name sysname NOT NULL,
   normalized_definition nvarchar(4000) NOT NULL
 );
 INSERT @expected_defaults VALUES
   {default_values};
 IF (SELECT COUNT_BIG(*) FROM sys.default_constraints
     WHERE parent_object_id IN({object_ids}))<>{len(default_rows)}
   THROW 51103,'Controle SQL incompativel: quantidade de defaults divergente.',1;
 IF EXISTS(
   SELECT 1
   FROM @expected_defaults AS e
   JOIN sys.columns AS c
     ON c.object_id=OBJECT_ID(e.object_name,N'U') AND c.name=e.column_name
   LEFT JOIN sys.default_constraints AS d ON d.object_id=c.default_object_id
   WHERE d.object_id IS NULL OR d.name<>e.constraint_name
      OR LOWER(REPLACE(REPLACE(REPLACE(d.definition,N'(',N''),N')',N''),N' ',N''))
         <>e.normalized_definition
 ) THROW 51103,'Controle SQL incompativel: assinatura de default divergente.',1;

 DECLARE @expected_foreign_key_columns table(
   parent_object_name nvarchar(517) NOT NULL,
   constraint_name sysname NOT NULL,
   constraint_column_id int NOT NULL,
   parent_column sysname NOT NULL,
   referenced_object_name nvarchar(517) NOT NULL,
   referenced_column sysname NOT NULL
 );
 INSERT @expected_foreign_key_columns VALUES
   {foreign_key_values};
 IF (SELECT COUNT_BIG(*) FROM sys.foreign_keys
     WHERE parent_object_id IN({object_ids}))<>2
   THROW 51104,'Controle SQL incompativel: quantidade de chaves estrangeiras divergente.',1;
 IF EXISTS(
   SELECT 1 FROM sys.foreign_keys
   WHERE referenced_object_id IN({object_ids})
     AND parent_object_id NOT IN({object_ids})
 ) THROW 51104,'Controle SQL incompativel: dependencia externa nao versionada.',1;
 IF (SELECT COUNT_BIG(*) FROM sys.foreign_key_columns AS fkc
     JOIN sys.foreign_keys AS fk ON fk.object_id=fkc.constraint_object_id
     WHERE fk.parent_object_id IN({object_ids}))<>{len(foreign_key_rows)}
   THROW 51104,'Controle SQL incompativel: colunas de chave estrangeira divergentes.',1;
 IF EXISTS(
   SELECT 1
   FROM @expected_foreign_key_columns AS e
   LEFT JOIN sys.foreign_keys AS fk
     ON fk.parent_object_id=OBJECT_ID(e.parent_object_name,N'U')
    AND fk.name=e.constraint_name
   LEFT JOIN sys.foreign_key_columns AS fkc
     ON fkc.constraint_object_id=fk.object_id
    AND fkc.constraint_column_id=e.constraint_column_id
   LEFT JOIN sys.columns AS pc
     ON pc.object_id=fkc.parent_object_id AND pc.column_id=fkc.parent_column_id
   LEFT JOIN sys.columns AS rc
     ON rc.object_id=fkc.referenced_object_id AND rc.column_id=fkc.referenced_column_id
   WHERE fk.object_id IS NULL OR fkc.constraint_column_id IS NULL
      OR fk.referenced_object_id<>OBJECT_ID(e.referenced_object_name,N'U')
      OR pc.name<>e.parent_column OR rc.name<>e.referenced_column
      OR fk.is_disabled<>0 OR fk.is_not_trusted<>0 OR fk.is_not_for_replication<>0
      OR fk.delete_referential_action<>0 OR fk.update_referential_action<>0
 ) THROW 51104,'Controle SQL incompativel: assinatura de chave estrangeira divergente.',1;

 IF EXISTS(SELECT 1 FROM sys.check_constraints WHERE parent_object_id IN({object_ids}))
   THROW 51105,'Controle SQL incompativel: CHECK nao versionado.',1;
 IF EXISTS(SELECT 1 FROM sys.triggers WHERE parent_class=1 AND parent_id IN({object_ids}))
   THROW 51105,'Controle SQL incompativel: trigger nao versionado.',1;
"""


def control_ddl(control_schema: str) -> str:
    """Cria e valida o contrato persistente de controle, sem alterar legados."""

    names = control_object_names(control_schema)
    schema_literal = qs(control_schema)
    validation_sql = _control_validation_sql(names)
    legacy_names = (
        "[dbo].[versao_esquema]",
        "[dbo].[execucao]",
        "[dbo].[execucao_tabela]",
        "[dbo].[execucao_lote]",
        "[controle_transferencia].[versao_esquema]",
        "[controle_transferencia].[execucao]",
        "[controle_transferencia].[execucao_tabela]",
        "[controle_transferencia].[execucao_lote]",
        "[bcp_control_v2].[bcp_schema_version]",
        "[bcp_control_v2].[bcp_execution_v2]",
        "[bcp_control_v2].[bcp_table_v2]",
        "[bcp_control_v2].[bcp_block_v2]",
    )
    legacy_condition = " OR ".join(
        f"OBJECT_ID({qs(name)}) IS NOT NULL" for name in legacy_names
    )
    legacy_guard = f"""
 IF ({legacy_condition})
   THROW 51106,'Controle SQL legado detectado. Execute a migracao administrativa e preserve ou descarte o historico explicitamente antes de criar ou usar o controle canonico.',1;
"""
    return f"""SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
BEGIN TRY
 BEGIN TRANSACTION;
 IF SCHEMA_ID({schema_literal}) IS NULL
   EXEC({qs('CREATE SCHEMA ' + qi(control_schema))});

 IF (OBJECT_ID({qs(names['version'])}) IS NOT NULL AND OBJECT_ID({qs(names['version'])},N'U') IS NULL)
    OR (OBJECT_ID({qs(names['execution'])}) IS NOT NULL AND OBJECT_ID({qs(names['execution'])},N'U') IS NULL)
    OR (OBJECT_ID({qs(names['table'])}) IS NOT NULL AND OBJECT_ID({qs(names['table'])},N'U') IS NULL)
    OR (OBJECT_ID({qs(names['block'])}) IS NOT NULL AND OBJECT_ID({qs(names['block'])},N'U') IS NULL)
   THROW 51100,'Controle SQL incompativel: nome reservado pertence a outro tipo de objeto.',1;

 DECLARE @control_object_count int =
   IIF(OBJECT_ID({qs(names['version'])},N'U') IS NULL,0,1)
  +IIF(OBJECT_ID({qs(names['execution'])},N'U') IS NULL,0,1)
  +IIF(OBJECT_ID({qs(names['table'])},N'U') IS NULL,0,1)
  +IIF(OBJECT_ID({qs(names['block'])},N'U') IS NULL,0,1);
 IF @control_object_count<>0 AND @control_object_count<>4
   THROW 51100,'Controle SQL parcial: preserve os objetos e faca migracao explicita.',1;
 {legacy_guard}

 IF @control_object_count=0
 BEGIN
   CREATE TABLE {names['version']} (
     version int NOT NULL CONSTRAINT [pk_ctl_exec_versao] PRIMARY KEY,
     created_at datetime2(7) NOT NULL CONSTRAINT [df_ctl_exec_versao_criado_em] DEFAULT(SYSDATETIME())
   );

   CREATE TABLE {names['execution']} (
     execution_id uniqueidentifier NOT NULL,
     dataset_id varchar(64) NOT NULL,
     structural_hash char(64) NOT NULL,
     destination_area varchar(16) NOT NULL,
     started_at datetime2(7) NOT NULL,
     updated_at datetime2(7) NOT NULL,
     state varchar(50) NOT NULL,
     CONSTRAINT [pk_ctl_exec] PRIMARY KEY(execution_id,dataset_id)
   );

   CREATE TABLE {names['table']} (
     execution_id uniqueidentifier NOT NULL,
     dataset_id varchar(64) NOT NULL,
     table_id varchar(64) NOT NULL,
     destination_schema sysname NOT NULL,
     destination_table sysname NOT NULL,
     layout_hash char(64) NOT NULL,
     projection_hash char(64) NOT NULL,
     final_limit_json nvarchar(max) NULL,
     last_exported_block bigint NOT NULL CONSTRAINT [df_ctl_exec_tabela_ultimo_lote_exportado] DEFAULT(0),
     last_imported_block bigint NOT NULL CONSTRAINT [df_ctl_exec_tabela_ultimo_lote_importado] DEFAULT(0),
     import_cursor_json nvarchar(max) NULL,
     imported_rows bigint NOT NULL CONSTRAINT [df_ctl_exec_tabela_linhas_importadas] DEFAULT(0),
     state varchar(50) NOT NULL,
     index_state varchar(50) NOT NULL,
     updated_at datetime2(7) NOT NULL,
     CONSTRAINT [pk_ctl_exec_tabela]
       PRIMARY KEY(execution_id,dataset_id,table_id),
     CONSTRAINT [fk_ctl_exec_tabela_exec]
       FOREIGN KEY(execution_id,dataset_id) REFERENCES {names['execution']}(execution_id,dataset_id)
   );
   CREATE UNIQUE INDEX [uq_ctl_exec_tabela_destino]
     ON {names['table']}(destination_schema,destination_table) WITH(IGNORE_DUP_KEY=OFF);

   CREATE TABLE {names['block']} (
     execution_id uniqueidentifier NOT NULL,
     dataset_id varchar(64) NOT NULL,
     table_id varchar(64) NOT NULL,
     block_id uniqueidentifier NOT NULL,
     block_number bigint NOT NULL,
     lower_bound_json nvarchar(max) NULL,
     upper_bound_json nvarchar(max) NOT NULL,
     manifest_rows bigint NOT NULL,
     imported_rows bigint NOT NULL,
     file_bytes bigint NOT NULL,
     data_sha256 char(64) NULL,
     format_sha256 char(64) NOT NULL,
     data_file_name nvarchar(1024) NULL,
     format_file_name nvarchar(1024) NOT NULL,
     import_started_at datetime2(7) NOT NULL,
     commit_recorded_at datetime2(7) NOT NULL,
     CONSTRAINT [pk_ctl_exec_lote]
       PRIMARY KEY(execution_id,dataset_id,table_id,block_id),
     CONSTRAINT [uq_ctl_exec_lote_numero]
       UNIQUE(execution_id,dataset_id,table_id,block_number),
     CONSTRAINT [fk_ctl_exec_lote_exec_tabela]
       FOREIGN KEY(execution_id,dataset_id,table_id)
         REFERENCES {names['table']}(execution_id,dataset_id,table_id)
   );
   INSERT {names['version']}(version) VALUES(3);
 END;

 {validation_sql}
 COMMIT;
END TRY
BEGIN CATCH
 IF XACT_STATE()<>0 ROLLBACK;
 THROW;
END CATCH;"""


def sql_artifact_path(
    executor_root: Path | str,
    sql_root: str,
    local_path: Path | str,
) -> str:
    """Mapeia somente o caminho relativo; não copia bytes entre as raízes."""

    root = Path(executor_root).resolve(strict=False)
    path = Path(local_path).resolve(strict=False)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Artefato não pertence a executor_directory") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Caminho relativo de artefato inválido")
    root_path: PureWindowsPath | PurePosixPath
    root_path = PurePosixPath(sql_root) if str(sql_root).startswith("/") else PureWindowsPath(sql_root)
    if not root_path.is_absolute():
        raise ValueError("raiz SQL deve ser um caminho absoluto")
    if any(part == ".." for part in root_path.parts):
        raise ValueError("raiz SQL inválida: segmentos '..' não são permitidos")
    return str(root_path.joinpath(*relative.parts))


def build_import_sql(
    manifest: BlockManifest,
    import_plan: Mapping[str, Any],
    data_path_sql: str | None,
    format_path_sql: str,
    control_schema: str,
) -> str:
    """Gera a transação única de dados + controle + checkpoint.

    ``import_plan`` é produzido pelo layout resolvido e contém:
    ``target_schema``, ``target_table``, ``insert_columns`` e
    ``select_expressions``. As expressões são código administrativo do perfil,
    nunca valores livres do operador.
    """

    names = control_object_names(control_schema)
    target = qi(str(import_plan["target_schema"])) + "." + qi(str(import_plan["target_table"]))
    insert_columns = list(import_plan.get("insert_columns", []))
    select_expressions = list(import_plan.get("select_expressions", []))
    if len(insert_columns) != len(select_expressions):
        raise ValueError("Plano de importação possui colunas/expressões incompatíveis")
    if not insert_columns and not manifest.empty_range:
        raise ValueError("Plano de importação vazio")
    insert_sql = ""
    if not manifest.empty_range:
        if not data_path_sql:
            raise ValueError("Arquivo SQL obrigatório para bloco não vazio")
        insert_sql = f"""
 INSERT INTO {target} WITH (TABLOCK)
 ({', '.join(qi(str(name)) for name in insert_columns)})
 SELECT {', '.join(str(value) for value in select_expressions)}
 FROM OPENROWSET(BULK {qs(data_path_sql)},FORMATFILE={qs(format_path_sql)},
      CODEPAGE='RAW',MAXERRORS=1) AS b;
 SET @inserted=ROWCOUNT_BIG();"""
    else:
        insert_sql = "\n SET @inserted=CONVERT(bigint,0);"
    destination = manifest.destination or {}
    area = str(destination.get("area", manifest.profile)).casefold()
    target_schema = str(import_plan["target_schema"])
    target_table = str(import_plan["target_table"])
    resource = target_lock_resource(target_schema, target_table)
    bookmark_guard = _bookmark_order_guard(manifest)
    return f"""SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
DECLARE @exec uniqueidentifier=?, @dataset varchar(64)=?, @table_id varchar(64)=?,
 @block uniqueidentifier=?, @seq bigint=?, @lower nvarchar(max)=?, @upper nvarchar(max)=?,
 @ceiling nvarchar(max)=?, @expected bigint=?, @bytes bigint=?, @data_sha char(64)=?, @fmt_sha char(64)=?,
 @inserted bigint=0, @lock_result int, @started datetime2(7)=SYSDATETIME();
BEGIN TRY
 BEGIN TRANSACTION;
 EXEC @lock_result=sys.sp_getapplock @Resource={qs(resource)},@LockMode='Exclusive',
      @LockOwner='Transaction',@LockTimeout=0;
 IF @lock_result<0 THROW 51101,'Outra sessão controla esta execução/tabela.',1;
 IF EXISTS(SELECT 1 FROM {names['block']} WITH(UPDLOCK,HOLDLOCK)
           WHERE execution_id=@exec AND dataset_id=@dataset AND table_id=@table_id AND block_id=@block)
 BEGIN
   COMMIT;
   RETURN;
 END;
 IF NOT EXISTS(SELECT 1 FROM {names['execution']} WITH(UPDLOCK,HOLDLOCK)
               WHERE execution_id=@exec AND dataset_id=@dataset AND structural_hash={qs(manifest.dataset_id)})
   THROW 51102,'Execução SQL ausente ou estruturalmente incompatível.',1;
 IF NOT EXISTS(SELECT 1 FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
               WHERE execution_id=@exec AND dataset_id=@dataset AND table_id=@table_id
                 AND layout_hash={qs(manifest.layout_hash)} AND projection_hash={qs(manifest.projection_hash)}
                 AND final_limit_json=@ceiling
                 AND destination_schema={qs(target_schema)} AND destination_table={qs(target_table)})
   THROW 51103,'Tabela de controle ausente ou layout/projeção incompatível.',1;
 IF (SELECT last_imported_block FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
     WHERE execution_id=@exec AND dataset_id=@dataset AND table_id=@table_id)<>@seq-1
   THROW 51104,'Bloco fora de sequência; reconcilie o controle antes de reenviar.',1;
 IF NOT EXISTS(SELECT 1 FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
               WHERE execution_id=@exec AND dataset_id=@dataset AND table_id=@table_id
                 AND (import_cursor_json=@lower OR (import_cursor_json IS NULL AND @lower IS NULL)))
   THROW 51106,'Limite inferior diverge do checkpoint importado; gap/overlap bloqueado.',1;
 {bookmark_guard}
 {insert_sql}
 IF @inserted<>@expected
   THROW 51105,'ROWCOUNT_BIG diverge do manifesto; rollback integral.',1;
 INSERT {names['block']}
 (execution_id,dataset_id,table_id,block_id,block_number,lower_bound_json,
  upper_bound_json,manifest_rows,imported_rows,file_bytes,
  data_sha256,format_sha256,data_file_name,format_file_name,
  import_started_at,commit_recorded_at)
 VALUES(@exec,@dataset,@table_id,@block,@seq,@lower,@upper,@expected,@inserted,@bytes,
        @data_sha,@fmt_sha,{qs(manifest.data_file) if manifest.data_file else 'NULL'},
        {qs(manifest.format_file)},@started,SYSDATETIME());
 UPDATE {names['table']} SET last_exported_block=@seq,last_imported_block=@seq,import_cursor_json=@upper,
   imported_rows=imported_rows+@inserted,state='RUNNING',updated_at=SYSDATETIME()
 WHERE execution_id=@exec AND dataset_id=@dataset AND table_id=@table_id;
 UPDATE {names['execution']} SET destination_area={qs(area)},state='RUNNING',
   updated_at=SYSDATETIME() WHERE execution_id=@exec AND dataset_id=@dataset;
 COMMIT;
END TRY
BEGIN CATCH
 IF XACT_STATE()<>0 ROLLBACK;
 THROW;
END CATCH;"""


def import_parameters(manifest: BlockManifest) -> tuple[Any, ...]:
    return (
        manifest.execution_id,
        manifest.dataset_id,
        manifest.table_id,
        manifest.block_id,
        manifest.block_number,
        None if manifest.lower_bound is None else __import__("json").dumps(manifest.lower_bound, ensure_ascii=False, separators=(",", ":")),
        __import__("json").dumps(manifest.upper_bound, ensure_ascii=False, separators=(",", ":")),
        __import__("json").dumps(manifest.final_limit, ensure_ascii=False, separators=(",", ":")),
        manifest.rows_exported,
        manifest.file_bytes,
        manifest.data_sha256,
        manifest.format_sha256,
    )


class DestinationImporter:
    def __init__(self, connection: Any, control_schema: str = "dbo") -> None:
        control_object_names(control_schema)
        self.connection = connection
        self.control_schema = control_schema

    def ensure_control(self) -> None:
        execute(self.connection, control_ddl(self.control_schema))

    def validate_control(self) -> None:
        """Validate the pre-existing SQL control contract without DDL."""

        names = control_object_names(self.control_schema)
        execute(
            self.connection,
            "SET NOCOUNT ON;\n" + _control_validation_sql(names),
        )

    def finish_table(
        self,
        execution_id: str,
        dataset_id: str,
        table_id: str,
        *,
        state: str,
        index_state: str,
    ) -> None:
        if state not in {item.value for item in TableStatus}:
            raise ValueError("state da tabela não pertence ao contrato V2")
        if index_state not in {item.value for item in IndexState}:
            raise ValueError("index_state não pertence ao contrato V2")
        table = control_object_names(self.control_schema)["table"]
        execute(
            self.connection,
            f"""SET XACT_ABORT ON;
UPDATE {table}
SET state=?, index_state=?, updated_at=SYSDATETIME()
WHERE execution_id=? AND dataset_id=? AND table_id=?;
IF @@ROWCOUNT<>1 THROW 51120,'Controle SQL da tabela ausente ao finalizar.',1;""",
            (state, index_state, execution_id, dataset_id, table_id),
        )

    def finish_execution(self, execution_id: str, dataset_id: str, *, state: str) -> None:
        if state not in {item.value for item in ExecutionState}:
            raise ValueError("state da execução não pertence ao contrato V2")
        execution = control_object_names(self.control_schema)["execution"]
        execute(
            self.connection,
            f"""SET XACT_ABORT ON;
UPDATE {execution} SET state=?,updated_at=SYSDATETIME()
WHERE execution_id=? AND dataset_id=?;
IF @@ROWCOUNT<>1 THROW 51121,'Controle SQL da execucao ausente ao finalizar.',1;""",
            (state, execution_id, dataset_id),
        )

    def register_execution_table(
        self,
        *,
        execution_id: str,
        dataset_id: str,
        structural_hash: str,
        area: str,
        table_id: str,
        target_schema: str,
        target_table: str,
        layout_hash: str,
        projection_hash: str,
        final_limit_json: str | None,
    ) -> None:
        """Vincula destino vazio/retomável sem autorizar append arbitrário."""

        names = control_object_names(self.control_schema)
        target = qi(target_schema) + "." + qi(target_table)
        resource = target_lock_resource(target_schema, target_table)
        sql = f"""SET XACT_ABORT ON;
BEGIN TRY
 BEGIN TRANSACTION;
 DECLARE @lock int;
 EXEC @lock=sys.sp_getapplock @Resource={qs(resource)},@LockMode='Exclusive',
      @LockOwner='Transaction',@LockTimeout=0;
 IF @lock<0 THROW 51110,'Outra sessão está vinculando o mesmo destino.',1;
 IF EXISTS(SELECT 1 FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
           WHERE destination_schema=? AND destination_table=?
             AND NOT (execution_id=? AND dataset_id=? AND table_id=?))
   THROW 51114,'Destino fisico ja esta vinculado a outra execucao/dataset/tabela.',1;
 IF EXISTS(SELECT 1 FROM {names['execution']} WITH(UPDLOCK,HOLDLOCK)
           WHERE execution_id=? AND dataset_id=? AND structural_hash<>?)
   THROW 51111,'Execução existente possui hash estrutural divergente.',1;
 IF NOT EXISTS(SELECT 1 FROM {names['execution']} WITH(UPDLOCK,HOLDLOCK)
               WHERE execution_id=? AND dataset_id=?)
   INSERT {names['execution']}
   (execution_id,dataset_id,structural_hash,destination_area,started_at,updated_at,state)
   VALUES(?,?,?, ?,SYSDATETIME(),SYSDATETIME(),'RUNNING');
 IF EXISTS(SELECT 1 FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
           WHERE execution_id=? AND dataset_id=? AND table_id=?
             AND (layout_hash<>? OR projection_hash<>? OR destination_schema<>? OR destination_table<>?
                  OR ISNULL(final_limit_json,N'<NULL>')<>ISNULL(?,N'<NULL>')))
   THROW 51112,'Controle da tabela possui layout/projeção/destino divergente.',1;
 IF NOT EXISTS(SELECT 1 FROM {names['table']} WITH(UPDLOCK,HOLDLOCK)
               WHERE execution_id=? AND dataset_id=? AND table_id=?)
 BEGIN
   IF EXISTS(SELECT TOP(1) 1 FROM {target} WITH(TABLOCKX,HOLDLOCK))
     THROW 51113,'Destino não vazio sem vínculo compatível de retomada.',1;
   INSERT {names['table']}
   (execution_id,dataset_id,table_id,destination_schema,destination_table,layout_hash,
    projection_hash,final_limit_json,state,index_state,updated_at)
   VALUES(?,?,?,?,?,?,?,?,'RUNNING','BASE_READY',SYSDATETIME());
 END;
 COMMIT;
END TRY
BEGIN CATCH
 IF XACT_STATE()<>0 ROLLBACK;
 THROW;
END CATCH;"""
        params = (
            target_schema, target_table, execution_id, dataset_id, table_id,
            execution_id, dataset_id, structural_hash,
            execution_id, dataset_id,
            execution_id, dataset_id, structural_hash, area,
            execution_id, dataset_id, table_id, layout_hash, projection_hash,
            target_schema, target_table, final_limit_json,
            execution_id, dataset_id, table_id,
            execution_id, dataset_id, table_id, target_schema, target_table,
            layout_hash, projection_hash, final_limit_json,
        )
        execute(self.connection, sql, params)

    def compatible_table_binding_exists(
        self,
        *,
        execution_id: str,
        dataset_id: str,
        structural_hash: str,
        area: str,
        table_id: str,
        target_schema: str,
        target_table: str,
        layout_hash: str,
        projection_hash: str,
        final_limit_json: str | None,
    ) -> bool:
        """Confirma, sem criar ou alterar objetos, um vinculo SQL retomavel."""

        names = control_object_names(self.control_schema)
        if not scalar(
            self.connection,
            "SELECT OBJECT_ID(?,N'U')",
            (names["execution"],),
        ) or not scalar(
            self.connection,
            "SELECT OBJECT_ID(?,N'U')",
            (names["table"],),
        ):
            return False
        return bool(
            scalar(
                self.connection,
                f"""SELECT COUNT_BIG(*)
FROM {names['execution']} AS e
INNER JOIN {names['table']} AS t
  ON t.execution_id=e.execution_id AND t.dataset_id=e.dataset_id
WHERE e.execution_id=? AND e.dataset_id=?
  AND e.structural_hash=? AND e.destination_area=?
  AND t.table_id=? AND t.destination_schema=? AND t.destination_table=?
  AND t.layout_hash=? AND t.projection_hash=?
  AND ISNULL(t.final_limit_json,N'<NULL>')=ISNULL(?,N'<NULL>');""",
                (
                    execution_id,
                    dataset_id,
                    structural_hash,
                    area,
                    table_id,
                    target_schema,
                    target_table,
                    layout_hash,
                    projection_hash,
                    final_limit_json,
                ),
            )
        )

    def block_is_confirmed(self, manifest: BlockManifest) -> bool:
        table = control_object_names(self.control_schema)["block"]
        if not scalar(self.connection, "SELECT OBJECT_ID(?,N'U')", (table,)):
            return False
        lower = (
            None
            if manifest.lower_bound is None
            else __import__("json").dumps(
                manifest.lower_bound, ensure_ascii=False, separators=(",", ":")
            )
        )
        upper = __import__("json").dumps(
            manifest.upper_bound, ensure_ascii=False, separators=(",", ":")
        )
        return bool(scalar(
            self.connection,
            f"""SELECT COUNT_BIG(*) FROM {table} WHERE execution_id=? AND dataset_id=?
            AND table_id=? AND block_id=? AND block_number=?
            AND (lower_bound_json=? OR (lower_bound_json IS NULL AND ? IS NULL))
            AND upper_bound_json=?
            AND manifest_rows=? AND imported_rows=? AND file_bytes=?
            AND (data_sha256=? OR (data_sha256 IS NULL AND ? IS NULL))
            AND format_sha256=?
            AND (data_file_name=? OR (data_file_name IS NULL AND ? IS NULL))
            AND format_file_name=?""",
            (manifest.execution_id, manifest.dataset_id, manifest.table_id,
             manifest.block_id, manifest.block_number,
             lower, lower, upper,
             manifest.rows_exported, manifest.rows_exported, manifest.file_bytes,
             manifest.data_sha256, manifest.data_sha256, manifest.format_sha256,
             manifest.data_file, manifest.data_file, manifest.format_file),
        ))

    def table_is_complete(self, manifest: BlockManifest) -> bool:
        table = control_object_names(self.control_schema)["table"]
        ceiling = __import__("json").dumps(
            manifest.final_limit, ensure_ascii=False, separators=(",", ":")
        )
        return bool(
            scalar(
                self.connection,
                f"""SELECT COUNT_BIG(*) FROM {table}
WHERE execution_id=? AND dataset_id=? AND table_id=?
  AND final_limit_json=? AND import_cursor_json=final_limit_json""",
                (
                    manifest.execution_id,
                    manifest.dataset_id,
                    manifest.table_id,
                    ceiling,
                ),
            )
        )

    def verify_destination_cardinality(
        self,
        *,
        execution_id: str,
        dataset_id: str,
        table_id: str,
        target_schema: str,
        target_table: str,
    ) -> int:
        """Confirma que o alvo físico contém exatamente as linhas confirmadas.

        O vínculo de destino exige tabela vazia no primeiro registro e impede
        que outro dataset do motor a reutilize. Esta prova final também detecta
        triggers ou escritas externas que alterem a cardinalidade física.
        """

        block = control_object_names(self.control_schema)["block"]
        expected = int(
            scalar(
                self.connection,
                f"""SELECT COALESCE(SUM(imported_rows),0) FROM {block}
WHERE execution_id=? AND dataset_id=? AND table_id=?""",
                (execution_id, dataset_id, table_id),
            )
            or 0
        )
        target = qi(target_schema) + "." + qi(target_table)
        actual = int(
            scalar(
                self.connection,
                f"SELECT COUNT_BIG(*) FROM {target} WITH(TABLOCK,HOLDLOCK)",
            )
            or 0
        )
        if actual != expected:
            raise RuntimeError(
                "DESTINATION_ROW_COUNT_MISMATCH: "
                f"destino {target_schema}.{target_table} possui {actual} linhas; "
                f"controle SQL confirma {expected}."
            )
        return actual

    def import_manifest(
        self,
        store: ArtifactStore,
        manifest_path: Path | str,
        import_plan: Mapping[str, Any],
        executor_root: Path | str,
        sql_root: str,
    ) -> BlockManifest:
        manifest = store.load_for_reconciliation(manifest_path)
        if self.block_is_confirmed(manifest):
            return manifest
        # Um arquivo removido so e aceitavel quando o controle acima comprova
        # o commit exato. Bloco ainda pendente exige novamente todos os bytes.
        with store.hold_verified_artifacts(manifest_path) as verified:
            manifest = verified.manifest
            fmt_sql = sql_artifact_path(
                executor_root, sql_root, verified.format_path
            )
            data_sql = None
            if verified.data_path is not None:
                data_sql = sql_artifact_path(
                    executor_root, sql_root, verified.data_path
                )
            sql = build_import_sql(manifest, import_plan, data_sql, fmt_sql, self.control_schema)
            try:
                execute(self.connection, sql, import_parameters(manifest))
            except Exception:
                # Uma resposta de COMMIT perdida não autoriza reenvio cego.
                if self.block_is_confirmed(manifest):
                    return manifest
                raise
            if not self.block_is_confirmed(manifest):
                raise RuntimeError("SQL não confirmou o bloco após retorno da transação")
            return manifest
