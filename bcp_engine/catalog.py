"""Descoberta de catalogo e selecao de marca d'agua para o motor V2.

As funcoes deste modulo mantem a leitura do catalogo separada das regras puras
de selecao. Isso permite testar prioridade, elegibilidade e politicas de indice
sem uma instancia SQL Server, preservando consultas reais para a integracao.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .sql import fetch_dicts
from .util import qi


COMPARABLE_TYPES = frozenset(
    {
        "bigint",
        "int",
        "smallint",
        "tinyint",
        "bit",
        "decimal",
        "numeric",
        "money",
        "smallmoney",
        "date",
        "datetime",
        "smalldatetime",
        "datetime2",
        "datetimeoffset",
        "time",
        "char",
        "varchar",
        "nchar",
        "nvarchar",
        "binary",
        "varbinary",
        "uniqueidentifier",
    }
)

WARNING_NO_COMPATIBLE_INDEX = "WATERMARK_WITHOUT_COMPATIBLE_INDEX"
WARNING_DIRECT_KEYLESS = "KEYLESS_DIRECT_LOAD_SINGLE_BLOCK"


class CatalogError(RuntimeError):
    """Erro de descoberta ou validacao de catalogo."""


class InvalidWatermarkError(CatalogError):
    """A marca explicitamente configurada nao e valida."""


class NoEligibleWatermarkError(CatalogError):
    """A tabela nao possui PK ou indice UNIQUE elegivel."""


class WatermarkContainsNullError(CatalogError):
    """Ao menos uma coluna da marca contem SQL NULL."""


class WatermarkIndexRequiredError(CatalogError):
    """A politica exige indice compativel para a marca explicita."""


class WatermarkNotUniqueError(InvalidWatermarkError):
    """A marca explicita nao distingue os registros atuais."""


class WatermarkEmptyTableError(InvalidWatermarkError):
    """Uma tabela sem PK/UNIQUE esta vazia e nao permite provar a marca."""


class DirectKeylessError(CatalogError):
    """A excecao de carga direta sem chave nao pode ser usada com seguranca."""


class DirectKeylessCountUnavailableError(DirectKeylessError):
    """A estimativa de metadados da tabela sem chave nao pode ser obtida."""


class DirectKeylessLimitExceededError(DirectKeylessError):
    """A tabela sem chave excede o limite global da carga direta."""


@dataclass(frozen=True)
class WatermarkColumn:
    """Coluna ordenada da marca, com metadados necessarios ao bookmark."""

    name: str
    type_name: str
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    nullable: bool = False
    collation_name: str | None = None
    descending: bool = False

    @classmethod
    def from_catalog(
        cls, column: Mapping[str, Any], descending: bool = False
    ) -> "WatermarkColumn":
        return cls(
            name=str(column["name"]),
            type_name=str(column["type_name"]).casefold(),
            max_length=_optional_int(column.get("max_length")),
            precision=_optional_int(column.get("precision")),
            scale=_optional_int(column.get("scale")),
            nullable=bool(column.get("is_nullable", column.get("nullable", False))),
            collation_name=column.get("collation_name"),
            descending=descending,
        )

    def as_catalog_dict(self) -> dict[str, Any]:
        """Representacao aceita por ``type_sql`` e pelos geradores legados."""

        return {
            "name": self.name,
            "type_name": self.type_name,
            "max_length": self.max_length,
            "precision": self.precision,
            "scale": self.scale,
            "is_nullable": self.nullable,
            "collation_name": self.collation_name,
        }


@dataclass(frozen=True)
class IndexCandidate:
    """Indice ordenado conforme definido no catalogo."""

    name: str
    index_id: int
    columns: tuple[WatermarkColumn, ...]
    is_primary_key: bool = False
    is_unique: bool = False
    has_filter: bool = False
    is_disabled: bool = False
    is_hypothetical: bool = False

    @property
    def usable(self) -> bool:
        return not (self.has_filter or self.is_disabled or self.is_hypothetical)


@dataclass(frozen=True)
class WatermarkSelection:
    """Resultado versionavel da escolha da marca d'agua."""

    columns: tuple[WatermarkColumn, ...]
    source: str
    index_name: str | None
    is_unique: bool
    warnings: tuple[str, ...] = ()
    transfer_mode: str = "KEYSET"
    captured_row_count: int | None = None
    direct_reason: str | None = None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def is_direct_keyless(self) -> bool:
        return self.transfer_mode == "DIRECT_KEYLESS"


@dataclass(frozen=True)
class WatermarkDataEvidence:
    """Evidencia dos dados atuais para uma marca explicitamente informada.

    Essa evidencia nunca e usada para procurar colunas candidatas. Ela somente
    comprova, para a lista recebida na configuracao, que a tabela nao esta
    vazia e que a combinacao nao contem NULL nem duplicidade.
    """

    has_rows: bool
    has_nulls: bool
    has_duplicates: bool


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def qualified_table(schema: str, table: str) -> str:
    return f"{qi(schema)}.{qi(table)}"


def query_rows(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Executa uma consulta pequena de metadados e devolve dicionarios."""

    return fetch_dicts(connection, sql, params)


def discover_columns(connection: Any, schema: str, table: str) -> list[dict[str, Any]]:
    """Le colunas na ordem fisica sem materializar dados de negocio."""

    rows = query_rows(
        connection,
        """
SELECT c.column_id, c.name, COALESCE(st.name,ut.name) AS type_name, c.max_length,
       c.precision, c.scale, c.collation_name, c.is_nullable,
       c.is_identity, c.is_computed, c.is_filestream, c.is_sparse,
       c.is_column_set, c.is_hidden, c.generated_always_type,
       c.encryption_type, c.xml_collection_id,
       CONVERT(bit, COLUMNPROPERTY(c.object_id,c.name,'IsDeterministic')) AS is_deterministic,
       CONVERT(bit, COLUMNPROPERTY(c.object_id,c.name,'IsPersisted')) AS is_persisted,
       CONVERT(bit, CASE WHEN mc.column_id IS NULL THEN 0 ELSE 1 END) AS is_masked,
       ut.name AS declared_type_name, ut.is_user_defined, ut.is_assembly_type
FROM sys.columns AS c
LEFT JOIN sys.types AS st
  ON st.system_type_id = c.system_type_id
 AND st.user_type_id = st.system_type_id
JOIN sys.types AS ut
  ON ut.user_type_id = c.user_type_id
LEFT JOIN sys.masked_columns AS mc
  ON mc.object_id = c.object_id AND mc.column_id = c.column_id
WHERE c.object_id = OBJECT_ID(?, N'U')
ORDER BY c.column_id;
""",
        (qualified_table(schema, table),),
    )
    if not rows:
        raise CatalogError(f"Tabela ausente ou sem VIEW DEFINITION: {schema}.{table}")
    return rows


def discover_indexes(connection: Any, schema: str, table: str) -> list[IndexCandidate]:
    """Le indices rowstore; inclui nao unicos para avaliar desempenho."""

    rows = query_rows(
        connection,
        """
SELECT i.index_id, i.name AS index_name, i.is_primary_key, i.is_unique,
       i.has_filter, i.is_disabled, i.is_hypothetical,
       ic.key_ordinal, ic.is_descending_key,
       c.name AS column_name, st.name AS type_name, c.max_length,
       c.precision, c.scale, c.collation_name, c.is_nullable
FROM sys.indexes AS i
JOIN sys.index_columns AS ic
  ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c
  ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN sys.types AS st
  ON st.system_type_id = c.system_type_id
 AND st.user_type_id = st.system_type_id
WHERE i.object_id = OBJECT_ID(?, N'U')
  AND i.type IN (1, 2)
  AND ic.key_ordinal > 0
ORDER BY CASE WHEN i.is_primary_key = 1 THEN 0 ELSE 1 END,
         CASE WHEN i.is_unique = 1 THEN 0 ELSE 1 END,
         i.index_id, ic.key_ordinal;
""",
        (qualified_table(schema, table),),
    )
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(int(row["index_id"]), []).append(row)

    result: list[IndexCandidate] = []
    for index_rows in groups.values():
        first = index_rows[0]
        columns = tuple(
            WatermarkColumn(
                name=str(row["column_name"]),
                type_name=str(row["type_name"]).casefold(),
                max_length=_optional_int(row.get("max_length")),
                precision=_optional_int(row.get("precision")),
                scale=_optional_int(row.get("scale")),
                nullable=bool(row.get("is_nullable", False)),
                collation_name=row.get("collation_name"),
                descending=bool(row.get("is_descending_key", False)),
            )
            for row in sorted(index_rows, key=lambda item: int(item["key_ordinal"]))
        )
        result.append(
            IndexCandidate(
                name=str(first["index_name"]),
                index_id=int(first["index_id"]),
                columns=columns,
                is_primary_key=bool(first["is_primary_key"]),
                is_unique=bool(first["is_unique"]),
                has_filter=bool(first["has_filter"]),
                is_disabled=bool(first["is_disabled"]),
                is_hypothetical=bool(first["is_hypothetical"]),
            )
        )
    return sorted(result, key=_index_priority)


def _index_priority(index: IndexCandidate) -> tuple[int, int, int]:
    return (
        0 if index.is_primary_key else 1,
        0 if index.is_unique else 1,
        index.index_id,
    )


def _is_comparable(column: WatermarkColumn) -> bool:
    if column.type_name not in COMPARABLE_TYPES:
        return False
    if column.type_name in {"varchar", "nvarchar", "varbinary"} and column.max_length == -1:
        return False
    return True


def _resolve_column(
    columns: Sequence[Mapping[str, Any]], requested_name: str
) -> Mapping[str, Any]:
    exact = [column for column in columns if column["name"] == requested_name]
    if exact:
        return exact[0]
    folded = [
        column
        for column in columns
        if str(column["name"]).casefold() == requested_name.casefold()
    ]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise InvalidWatermarkError(
            f"Nome de coluna ambiguo na collation sensivel a caixa: {requested_name}"
        )
    raise InvalidWatermarkError(f"Coluna de marca d'agua inexistente: {requested_name}")


def parse_explicit_watermark(
    columns: Sequence[Mapping[str, Any]], explicit: Mapping[str, Any]
) -> tuple[WatermarkColumn, ...]:
    """Valida a forma V2 ``watermark.columns`` sem procurar fallback."""

    configured = explicit.get("columns") if isinstance(explicit, Mapping) else None
    if not isinstance(configured, list) or not configured:
        raise InvalidWatermarkError("watermark.columns deve ser uma lista nao vazia")

    result: list[WatermarkColumn] = []
    seen: set[str] = set()
    for item in configured:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise InvalidWatermarkError("Cada coluna da marca exige nome e ordem ASC")
        order = str(item.get("direction", "ASC")).upper()
        if order != "ASC":
            raise InvalidWatermarkError(
                f"Ordem invalida para {item['name']}: {order}; use sempre ASC"
            )
        source = _resolve_column(columns, str(item["name"]))
        key = str(source["name"]).casefold()
        if key in seen:
            raise InvalidWatermarkError(f"Coluna repetida na marca d'agua: {source['name']}")
        seen.add(key)
        selected = WatermarkColumn.from_catalog(source)
        if not _is_comparable(selected):
            raise InvalidWatermarkError(
                f"Tipo nao comparavel para marca d'agua: {selected.name} ({selected.type_name})"
            )
        result.append(selected)
    return tuple(result)


def _same_key(left: Sequence[WatermarkColumn], right: Sequence[WatermarkColumn]) -> bool:
    return len(left) == len(right) and all(
        a.name.casefold() == b.name.casefold() for a, b in zip(left, right)
    )


def _index_supports_order(
    index: IndexCandidate, columns: Sequence[WatermarkColumn]
) -> bool:
    if not index.usable or len(index.columns) < len(columns):
        return False
    prefix = index.columns[: len(columns)]
    if any(a.name.casefold() != b.name.casefold() for a, b in zip(prefix, columns)):
        return False
    directions_equal = all(a.descending == b.descending for a, b in zip(prefix, columns))
    directions_reversed = all(a.descending != b.descending for a, b in zip(prefix, columns))
    return directions_equal or directions_reversed


def _automatic_selection(
    columns: Sequence[Mapping[str, Any]],
    indexes: Sequence[IndexCandidate],
    null_probe: Callable[[tuple[WatermarkColumn, ...]], bool],
) -> WatermarkSelection:
    """Seleciona exclusivamente uma PK/UNIQUE descoberta no catalogo.

    Os dados sao consultados apenas para validar cobertura de NULL de uma
    candidata ja fornecida pelos metadados. Nenhuma combinacao de colunas e
    inferida por contagem ou agrupamento.
    """

    by_name = {str(column["name"]).casefold(): column for column in columns}
    rejected_for_null = False
    for index in sorted(indexes, key=_index_priority):
        if not index.usable or not index.is_unique:
            continue
        resolved: list[WatermarkColumn] = []
        eligible = True
        for indexed in index.columns:
            source = by_name.get(indexed.name.casefold())
            if source is None:
                eligible = False
                break
            # A direcao fisica do indice continua disponivel em ``indexed``
            # para avaliar compatibilidade, mas o cursor funcional do motor e
            # invariavelmente ascendente. SQL Server pode percorrer um indice
            # inteiramente DESC no sentido inverso.
            candidate = WatermarkColumn.from_catalog(source)
            if not _is_comparable(candidate):
                eligible = False
                break
            resolved.append(candidate)
        if not eligible or not resolved:
            continue
        selected = tuple(resolved)
        if null_probe(selected):
            rejected_for_null = True
            continue
        return WatermarkSelection(
            columns=selected,
            source="primary_key" if index.is_primary_key else "unique",
            index_name=index.name,
            is_unique=True,
        )

    suffix = "; chaves candidatas continham NULL" if rejected_for_null else ""
    raise NoEligibleWatermarkError(
        "Tabela sem PK/UNIQUE completa, ativa, nao filtrada e comparavel" + suffix
    )


def select_watermark(
    columns: Sequence[Mapping[str, Any]],
    indexes: Sequence[IndexCandidate],
    explicit: Mapping[str, Any] | None,
    *,
    require_index: bool = False,
    has_nulls: Callable[[tuple[WatermarkColumn, ...]], bool] | None = None,
    data_evidence: Callable[
        [tuple[WatermarkColumn, ...]], WatermarkDataEvidence
    ]
    | None = None,
) -> WatermarkSelection:
    """Seleciona marca explicita, ou PK e depois UNIQUE elegivel.

    ``has_nulls`` e uma fronteira injetavel para validar uma chave ja indicada
    pelo catalogo. ``data_evidence`` recebe somente as colunas explicitamente
    configuradas; jamais procura uma combinacao candidata nos dados.

    Quando nao existe PK/UNIQUE estrutural elegivel, a marca explicita so e
    aceita com evidencia positiva de tabela nao vazia, ausencia de NULL e
    unicidade nos dados atuais. Uma marca explicita invalida nunca cai
    silenciosamente para uma chave automatica.
    """

    # Sem acesso aos dados, nulabilidade catalogada deve falhar de forma
    # conservadora. ``discover_watermark`` sempre injeta a prova real.
    null_probe = has_nulls or (
        lambda selected: any(column.nullable for column in selected)
    )
    ordered_indexes = sorted(indexes, key=_index_priority)

    if explicit is not None:
        selected = parse_explicit_watermark(columns, explicit)
        proven_unique = False
        compatible = [
            index for index in ordered_indexes if _index_supports_order(index, selected)
        ]
        if not compatible:
            if require_index:
                raise WatermarkIndexRequiredError(
                    "Marca d'agua explicita nao possui indice compativel"
                )
        # Uma tabela com PK/UNIQUE elegivel ja tem uma chave estrutural aceita
        # pelo contrato. Sem essa garantia, a combinacao recebida do operador
        # precisa ser comprovada nos dados atuais; o motor nao tenta outras.
        try:
            _automatic_selection(columns, ordered_indexes, null_probe)
            has_structural_key = True
        except NoEligibleWatermarkError:
            has_structural_key = False

        if has_structural_key:
            if null_probe(selected):
                raise WatermarkContainsNullError(
                    "Marca d'agua explicita contem NULL; politica rejeitar_tabela aplicada"
                )
        else:
            if data_evidence is None:
                raise InvalidWatermarkError(
                    "Tabela sem PK/UNIQUE exige prova da marca d'agua explicita; "
                    "a validacao de dados nao foi fornecida"
                )
            evidence = data_evidence(selected)
            if evidence.has_nulls:
                raise WatermarkContainsNullError(
                    "Marca d'agua explicita contem NULL; politica rejeitar_tabela aplicada"
                )
            if not evidence.has_rows:
                raise WatermarkEmptyTableError(
                    "Tabela sem PK/UNIQUE esta vazia; nao e possivel comprovar "
                    "a unicidade da marca d'agua explicita"
                )
            if evidence.has_duplicates:
                raise WatermarkNotUniqueError(
                    "Marca d'agua explicita nao distingue os registros atuais; "
                    "existem combinacoes duplicadas"
                )
            proven_unique = True

        chosen = compatible[0] if compatible else None
        unique = proven_unique or bool(
            chosen is not None
            and chosen.is_unique
            and _same_key(chosen.columns, selected)
        )
        return WatermarkSelection(
            columns=selected,
            source="explicit",
            index_name=chosen.name if chosen else None,
            is_unique=unique,
            warnings=() if chosen else (WARNING_NO_COMPATIBLE_INDEX,),
        )

    return _automatic_selection(columns, ordered_indexes, null_probe)


def watermark_has_nulls(
    connection: Any,
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
) -> bool:
    """Verifica cobertura de NULL apenas quando o catalogo permite NULL."""

    nullable = [column for column in columns if column.nullable]
    if not nullable:
        return False
    predicate = " OR ".join(f"{qi(column.name)} IS NULL" for column in nullable)
    rows = query_rows(
        connection,
        f"SELECT TOP (1) 1 AS found_null FROM {qualified_table(schema, table)} WHERE {predicate};",
    )
    return bool(rows)


def explicit_watermark_data_evidence(
    connection: Any,
    schema: str,
    table: str,
    columns: Sequence[WatermarkColumn],
) -> WatermarkDataEvidence:
    """Comprova a lista explicita sem descobrir ou testar outras colunas.

    A consulta e uma unica instrucao, de modo que as tres evidencias observam
    a mesma consistencia por instrucao oferecida pelo SQL Server. O agrupamento
    pode varrer a tabela e por isso so e executado para a excecao contratual:
    tabela sem PK/UNIQUE com marca explicitamente informada.
    """

    if not columns:
        raise InvalidWatermarkError("Marca d'agua explicita vazia")
    source = qualified_table(schema, table)
    names = ", ".join(qi(column.name) for column in columns)
    nullable = [column for column in columns if column.nullable]
    null_test = (
        "EXISTS (SELECT 1 FROM "
        + source
        + " WHERE "
        + " OR ".join(f"{qi(column.name)} IS NULL" for column in nullable)
        + ")"
        if nullable
        else "1 = 0"
    )
    rows = query_rows(
        connection,
        f"""
SELECT
 CONVERT(bit, CASE WHEN EXISTS (SELECT 1 FROM {source}) THEN 1 ELSE 0 END) AS has_rows,
 CONVERT(bit, CASE WHEN {null_test} THEN 1 ELSE 0 END) AS has_nulls,
 CONVERT(bit, CASE WHEN EXISTS (
   SELECT 1 FROM {source}
   GROUP BY {names}
   HAVING COUNT_BIG(*) > 1
 ) THEN 1 ELSE 0 END) AS has_duplicates;
""",
    )
    if not rows:
        raise InvalidWatermarkError(
            "Nao foi possivel obter evidencia da marca d'agua explicita"
        )
    row = rows[0]
    return WatermarkDataEvidence(
        has_rows=bool(row.get("has_rows")),
        has_nulls=bool(row.get("has_nulls")),
        has_duplicates=bool(row.get("has_duplicates")),
    )


def metadata_table_row_count(connection: Any, schema: str, table: str) -> int:
    """Return the fast catalog estimate used to pre-admit DIRECT_KEYLESS.

    This query reads partition metadata and never scans or locks the business
    table.  Because it is intentionally approximate, the hard limit is checked
    again against the row count reported by BCP before publishing/importing.
    """

    try:
        rows = query_rows(
            connection,
            """
SELECT COALESCE(SUM(CONVERT(bigint, p.[rows])), 0) AS row_count
FROM sys.partitions AS p
INNER JOIN sys.tables AS t ON t.object_id = p.object_id
INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ? AND p.index_id IN (0, 1);
""",
            (schema, table),
        )
        if len(rows) != 1 or rows[0].get("row_count") is None:
            raise ValueError("metadados nao retornaram exatamente um valor")
        count = int(rows[0]["row_count"])
        if count < 0:
            raise ValueError("metadados retornaram valor negativo")
        return count
    except Exception as exc:
        raise DirectKeylessCountUnavailableError(
            "Carga direta sem chave recusada: contagem aproximada por metadados indisponivel"
        ) from exc


# Compatibility alias for integrations that imported the old helper.  The
# semantics intentionally changed: no COUNT_BIG/table scan is issued.
exact_table_row_count = metadata_table_row_count


def _direct_keyless_selection(
    connection: Any,
    schema: str,
    table: str,
    *,
    row_limit: int,
    reason: Exception,
    captured_row_count: int | None = None,
) -> WatermarkSelection:
    count = (
        metadata_table_row_count(connection, schema, table)
        if captured_row_count is None
        else captured_row_count
    )
    if type(count) is not int or count < 0:
        raise DirectKeylessCountUnavailableError(
            "Carga direta sem chave recusada: contagem persistida invalida"
        ) from reason
    if count > row_limit:
        raise DirectKeylessLimitExceededError(
            "Carga direta sem chave recusada: estimativa por metadados "
            f"({count}) excede o limite global ({row_limit})"
        ) from reason
    return WatermarkSelection(
        columns=(),
        source="direct_keyless",
        index_name=None,
        is_unique=False,
        warnings=(
            WARNING_DIRECT_KEYLESS,
            f"METADATA_ROW_COUNT_ESTIMATE={count};GLOBAL_LIMIT={row_limit}",
            *(
                ("ROW_COUNT_REUSED_FROM_DURABLE_CONTROL",)
                if captured_row_count is not None
                else ()
            ),
        ),
        transfer_mode="DIRECT_KEYLESS",
        captured_row_count=count,
        direct_reason=str(reason),
    )


def discover_watermark(
    connection: Any,
    schema: str,
    table: str,
    explicit: Mapping[str, Any] | None = None,
    *,
    require_index: bool = False,
    columns: Sequence[Mapping[str, Any]] | None = None,
    indexes: Sequence[IndexCandidate] | None = None,
    direct_keyless_row_limit: int = 0,
    direct_keyless_captured_row_count: int | None = None,
) -> WatermarkSelection:
    """Orquestra a estrategia keyset ou a excecao direta sem chave.

    A carga direta so e considerada para ausencia de chave automatica ou para
    uma marca explicitamente bem formada que nao foi comprovada nos dados por
    NULL, duplicidade ou tabela vazia. Erros estruturais da marca explicita
    (coluna/tipo/ordem/indice obrigatorio) nunca recebem fallback silencioso.
    """

    table_columns = list(columns) if columns is not None else discover_columns(connection, schema, table)
    table_indexes = list(indexes) if indexes is not None else discover_indexes(connection, schema, table)
    if direct_keyless_captured_row_count is not None:
        # Retomada de uma decisao direta ja persistida: revalida somente a
        # estrutura explicitamente configurada, sem reclassificar a tabela a
        # partir de dados que podem ter mudado depois da publicacao do bloco.
        if direct_keyless_row_limit <= 0:
            raise DirectKeylessLimitExceededError(
                "Carga direta sem chave persistida, mas o modo esta desabilitado"
            )
        if explicit is not None:
            selected = parse_explicit_watermark(table_columns, explicit)
            compatible = [
                candidate
                for candidate in table_indexes
                if _index_supports_order(candidate, selected)
            ]
            if require_index and not compatible:
                raise WatermarkIndexRequiredError(
                    "Marca d'agua explicita nao possui indice compativel"
                )
        return _direct_keyless_selection(
            connection,
            schema,
            table,
            row_limit=direct_keyless_row_limit,
            captured_row_count=direct_keyless_captured_row_count,
            reason=NoEligibleWatermarkError(
                "Decisao de carga direta restaurada do controle duravel"
            ),
        )
    try:
        return select_watermark(
            table_columns,
            table_indexes,
            explicit,
            require_index=require_index,
            has_nulls=lambda selected: watermark_has_nulls(
                connection, schema, table, selected
            ),
            data_evidence=lambda selected: explicit_watermark_data_evidence(
                connection, schema, table, selected
            ),
        )
    except (
        NoEligibleWatermarkError,
        WatermarkContainsNullError,
        WatermarkNotUniqueError,
        WatermarkEmptyTableError,
    ) as exc:
        if direct_keyless_row_limit <= 0:
            raise
        return _direct_keyless_selection(
            connection,
            schema,
            table,
            row_limit=direct_keyless_row_limit,
            reason=exc,
        )


__all__ = [
    "COMPARABLE_TYPES",
    "WARNING_NO_COMPATIBLE_INDEX",
    "WARNING_DIRECT_KEYLESS",
    "CatalogError",
    "InvalidWatermarkError",
    "NoEligibleWatermarkError",
    "WatermarkContainsNullError",
    "WatermarkIndexRequiredError",
    "WatermarkNotUniqueError",
    "WatermarkEmptyTableError",
    "DirectKeylessError",
    "DirectKeylessCountUnavailableError",
    "DirectKeylessLimitExceededError",
    "WatermarkColumn",
    "IndexCandidate",
    "WatermarkSelection",
    "WatermarkDataEvidence",
    "qualified_table",
    "query_rows",
    "discover_columns",
    "discover_indexes",
    "parse_explicit_watermark",
    "select_watermark",
    "watermark_has_nulls",
    "explicit_watermark_data_evidence",
    "exact_table_row_count",
    "metadata_table_row_count",
    "discover_watermark",
]
