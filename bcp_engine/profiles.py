"""Perfis versionados e resolução do layout físico Bronze/Landing.

Este módulo não acessa SQL Server nem o filesystem durante a resolução de um
perfil já carregado. A mesma :class:`TableLayout` deve alimentar geração de DDL,
projeção de transporte e importação, evitando contratos físicos divergentes.
"""

from __future__ import annotations

import json
import string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .util import qi, type_sql


PROFILE_VERSION = 1
DEFAULT_PROFILE_DIRECTORY = Path(__file__).resolve().parents[1] / "templates"
ALLOWED_PLACEHOLDERS = frozenset(
    {
        "source_database",
        "source_table",
        "destination_table",
        "destination_schema",
        "technical_column",
        "partition_column",
        "partition_descriptor",
    }
)

PROFILE_ROOT_KEYS = frozenset(
    {
        "profile_version",
        "name",
        "table_name",
        "technical_column",
        "business_columns",
        "additional_columns",
        "sequence",
        "partitioning",
        "primary_key",
        "indexes",
    }
)
PROFILE_COLUMN_KEYS = frozenset(
    {
        "name",
        "sql_type",
        "result_sql_type",
        "nullable",
        "identity",
        "identity_seed",
        "identity_increment",
        "computed",
        "persisted",
        "default",
        "default_name",
        "collation",
        "generation",
        "fill",
    }
)
PROFILE_BUSINESS_COLUMN_KEYS = frozenset(
    {"mode", "preserve_order", "materialize_identity", "materialize_computed"}
)
PROFILE_INDEX_KEYS = frozenset(
    {"name", "clustered", "unique", "phase", "columns", "includes", "filter_predicate"}
)
PROFILE_INDEX_COLUMN_KEYS = frozenset({"name", "direction"})
PROFILE_SEQUENCE_KEYS = frozenset({"name", "sql_type", "start", "increment", "cycle"})
PROFILE_PARTITION_KEYS = frozenset(
    {
        "sql_type",
        "range_direction",
        "interval",
        "future_years",
        "filegroup",
        "function_name",
        "scheme_name",
        "clustered_index_name",
    }
)


class ProfileError(ValueError):
    """Perfil inválido ou impossível de materializar."""


@dataclass(frozen=True)
class ColumnDefinition:
    name: str
    sql_type: str | None
    nullable: bool
    identity: bool = False
    identity_seed: int = 1
    identity_increment: int = 1
    computed_expression: str | None = None
    persisted: bool = False
    default_expression: str | None = None
    default_name: str | None = None
    collation: str | None = None
    role: str = "business"
    source_name: str | None = None

    @property
    def insertable(self) -> bool:
        return not self.identity and self.computed_expression is None and self.default_expression is None


@dataclass(frozen=True)
class IndexKey:
    name: str
    direction: str = "ASC"


@dataclass(frozen=True)
class IndexDefinition:
    name: str
    keys: tuple[IndexKey, ...]
    unique: bool = False
    clustered: bool = False
    includes: tuple[str, ...] = ()
    filter_predicate: str | None = None
    disabled: bool = False
    primary_key: bool = False
    phase: str = "secondary"


@dataclass(frozen=True)
class SequenceDefinition:
    name: str
    sql_type: str = "BIGINT"
    start: int = 1
    increment: int = 1
    cycle: bool = False


@dataclass(frozen=True)
class PartitionDefinition:
    column: str
    function_name: str
    scheme_name: str
    clustered_index_name: str
    descriptor: str
    sql_type: str = "DATETIME2(7)"
    range_direction: str = "RIGHT"
    interval: str = "month"
    future_years: int = 6
    filegroup: str = "PRIMARY"


@dataclass(frozen=True)
class FillRule:
    column: str
    value: str


@dataclass(frozen=True)
class TableLayout:
    profile_name: str
    profile_version: int
    schema: str
    table: str
    source_database: str
    source_table: str
    columns: tuple[ColumnDefinition, ...]
    primary_key: IndexDefinition
    indexes: tuple[IndexDefinition, ...]
    sequence: SequenceDefinition | None
    technical_id_strategy: str
    technical_id_source_column: str | None
    fill_rules: tuple[FillRule, ...]
    partition: PartitionDefinition | None = None

    @property
    def qualified_name(self) -> str:
        return f"{qi(self.schema)}.{qi(self.table)}"

    @property
    def business_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(column for column in self.columns if column.role == "business")

    @property
    def insertable_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(column for column in self.columns if column.insertable)

    @property
    def base_indexes(self) -> tuple[IndexDefinition, ...]:
        return tuple(index for index in self.indexes if index.phase == "base")

    @property
    def secondary_indexes(self) -> tuple[IndexDefinition, ...]:
        return tuple(index for index in self.indexes if index.phase == "secondary")


def _validate_identifier(value: str, field: str) -> str:
    try:
        qi(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{field} inválido: {value!r}") from exc
    return value


def _normalize_destination_identifier(value: str, field: str) -> str:
    return _validate_identifier(value.lower(), field)


def _iter_placeholders(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, str):
        try:
            parsed = string.Formatter().parse(value)
            for _literal, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if format_spec or conversion or "." in field_name or "[" in field_name:
                    raise ProfileError(f"Placeholder não suportado em {value!r}")
                result.add(field_name)
        except ValueError as exc:
            raise ProfileError(f"Template inválido: {value!r}") from exc
    elif isinstance(value, Mapping):
        for child in value.values():
            result.update(_iter_placeholders(child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            result.update(_iter_placeholders(child))
    return result


def _render(value: Any, context: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        placeholders = _iter_placeholders(value)
        unknown = placeholders - ALLOWED_PLACEHOLDERS
        if unknown:
            raise ProfileError("Placeholders desconhecidos: " + ", ".join(sorted(unknown)))
        missing = placeholders - context.keys()
        if missing:
            raise ProfileError("Contexto sem placeholders: " + ", ".join(sorted(missing)))
        return value.format_map(context)
    if isinstance(value, Mapping):
        return {key: _render(child, context) for key, child in value.items()}
    if isinstance(value, list):
        return [_render(child, context) for child in value]
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: frozenset[str], field: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProfileError(
            f"{field} contém campos desconhecidos: {', '.join(unknown)}"
        )


def _require_bool_if_present(
    value: Mapping[str, Any], field: str, name: str
) -> None:
    if name in value and type(value[name]) is not bool:
        raise ProfileError(f"{field}.{name} deve ser booleano JSON")


def _require_int_if_present(
    value: Mapping[str, Any], field: str, name: str, *, nonzero: bool = False
) -> None:
    if name not in value:
        return
    item = value[name]
    if type(item) is not int or (nonzero and item == 0):
        suffix = " inteiro não zero" if nonzero else " inteiro"
        raise ProfileError(f"{field}.{name} deve ser{suffix} JSON")


def _validate_profile_column_types(value: Mapping[str, Any], field: str) -> None:
    for name in ("nullable", "identity", "persisted"):
        _require_bool_if_present(value, field, name)
    _require_int_if_present(value, field, "identity_seed")
    _require_int_if_present(value, field, "identity_increment", nonzero=True)


def _validate_profile_index_types(value: Mapping[str, Any], field: str) -> None:
    for name in ("clustered", "unique"):
        _require_bool_if_present(value, field, name)
    if "includes" in value and not isinstance(value["includes"], list):
        raise ProfileError(f"{field}.includes deve ser uma lista")


def validate_profile(profile: Mapping[str, Any]) -> None:
    required = {
        "profile_version",
        "name",
        "table_name",
        "technical_column",
        "business_columns",
        "additional_columns",
        "primary_key",
        "indexes",
    }
    missing = required - profile.keys()
    if missing:
        raise ProfileError("Perfil sem campos obrigatórios: " + ", ".join(sorted(missing)))
    _reject_unknown_keys(profile, PROFILE_ROOT_KEYS, "profile")
    if type(profile["profile_version"]) is not int or profile["profile_version"] != PROFILE_VERSION:
        raise ProfileError(
            f"profile_version={profile['profile_version']!r} não suportada; "
            f"esperado {PROFILE_VERSION}"
        )
    if not isinstance(profile["name"], str) or not profile["name"]:
        raise ProfileError("name do perfil é obrigatório")
    if not isinstance(profile["table_name"], str) or not profile["table_name"]:
        raise ProfileError("table_name do perfil é obrigatório")
    business = profile["business_columns"]
    if not isinstance(business, Mapping):
        raise ProfileError("business_columns deve ser um objeto")
    _reject_unknown_keys(
        business, PROFILE_BUSINESS_COLUMN_KEYS, "business_columns"
    )
    if business.get("mode") != "auto_discovery":
        raise ProfileError("business_columns.mode deve ser auto_discovery")
    if business.get("preserve_order") is not True:
        raise ProfileError("o perfil deve preservar a ordem das colunas de negócio")
    for field in ("technical_column", "primary_key"):
        if not isinstance(profile[field], Mapping):
            raise ProfileError(f"{field} deve ser um objeto")
    _reject_unknown_keys(
        profile["technical_column"], PROFILE_COLUMN_KEYS, "technical_column"
    )
    _validate_profile_column_types(profile["technical_column"], "technical_column")
    _reject_unknown_keys(profile["primary_key"], PROFILE_INDEX_KEYS, "primary_key")
    _validate_profile_index_types(profile["primary_key"], "primary_key")
    for field in ("additional_columns", "indexes"):
        if not isinstance(profile[field], list):
            raise ProfileError(f"{field} deve ser uma lista")
    for index, column in enumerate(profile["additional_columns"]):
        if not isinstance(column, Mapping):
            raise ProfileError(f"additional_columns[{index}] deve ser um objeto")
        _reject_unknown_keys(
            column, PROFILE_COLUMN_KEYS, f"additional_columns[{index}]"
        )
        _validate_profile_column_types(column, f"additional_columns[{index}]")
    for field in ("primary_key",):
        raw_columns = profile[field].get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise ProfileError(f"{field}.columns deve ser uma lista não vazia")
        for index, column in enumerate(raw_columns):
            if not isinstance(column, Mapping):
                raise ProfileError(f"{field}.columns[{index}] deve ser um objeto")
            _reject_unknown_keys(
                column,
                PROFILE_INDEX_COLUMN_KEYS,
                f"{field}.columns[{index}]",
            )
    for index, raw_index in enumerate(profile["indexes"]):
        if not isinstance(raw_index, Mapping):
            raise ProfileError(f"indexes[{index}] deve ser um objeto")
        _reject_unknown_keys(raw_index, PROFILE_INDEX_KEYS, f"indexes[{index}]")
        _validate_profile_index_types(raw_index, f"indexes[{index}]")
        raw_columns = raw_index.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise ProfileError(f"indexes[{index}].columns deve ser uma lista não vazia")
        for key_index, column in enumerate(raw_columns):
            if not isinstance(column, Mapping):
                raise ProfileError(
                    f"indexes[{index}].columns[{key_index}] deve ser um objeto"
                )
            _reject_unknown_keys(
                column,
                PROFILE_INDEX_COLUMN_KEYS,
                f"indexes[{index}].columns[{key_index}]",
            )
    sequence = profile.get("sequence")
    if sequence is not None:
        if not isinstance(sequence, Mapping):
            raise ProfileError("sequence deve ser um objeto ou null")
        _reject_unknown_keys(sequence, PROFILE_SEQUENCE_KEYS, "sequence")
        _require_int_if_present(sequence, "sequence", "start")
        _require_int_if_present(sequence, "sequence", "increment", nonzero=True)
        _require_bool_if_present(sequence, "sequence", "cycle")
    partitioning = profile.get("partitioning")
    if partitioning is not None:
        if not isinstance(partitioning, Mapping):
            raise ProfileError("partitioning deve ser um objeto ou null")
        _reject_unknown_keys(partitioning, PROFILE_PARTITION_KEYS, "partitioning")
        required_partitioning = PROFILE_PARTITION_KEYS
        missing_partitioning = required_partitioning - partitioning.keys()
        if missing_partitioning:
            raise ProfileError(
                "partitioning sem campos obrigatorios: "
                + ", ".join(sorted(missing_partitioning))
            )
        for name in (
            "sql_type",
            "range_direction",
            "interval",
            "filegroup",
            "function_name",
            "scheme_name",
            "clustered_index_name",
        ):
            if not isinstance(partitioning[name], str) or not partitioning[name].strip():
                raise ProfileError(f"partitioning.{name} deve ser uma string nao vazia")
        if str(partitioning["sql_type"]).upper().replace(" ", "") != "DATETIME2(7)":
            raise ProfileError("partitioning.sql_type deve ser DATETIME2(7)")
        if str(partitioning["range_direction"]).upper() != "RIGHT":
            raise ProfileError("partitioning.range_direction deve ser RIGHT")
        if str(partitioning["interval"]).casefold() != "month":
            raise ProfileError("partitioning.interval deve ser month")
        if str(partitioning["filegroup"]).upper() != "PRIMARY":
            raise ProfileError("partitioning.filegroup deve ser PRIMARY")
        future_years = partitioning["future_years"]
        if type(future_years) is not int or future_years < 1:
            raise ProfileError("partitioning.future_years deve ser um inteiro positivo")
    for name in ("preserve_order", "materialize_identity", "materialize_computed"):
        _require_bool_if_present(business, "business_columns", name)
    unknown = _iter_placeholders(profile) - ALLOWED_PLACEHOLDERS
    if unknown:
        raise ProfileError("Placeholders desconhecidos: " + ", ".join(sorted(unknown)))


def load_profile(
    profile: str | Path | Mapping[str, Any],
    *,
    directory: Path | None = None,
) -> dict[str, Any]:
    """Carrega um perfil pelo nome, caminho ou mapping e valida sua versão."""

    if isinstance(profile, Mapping):
        loaded = json.loads(json.dumps(profile, ensure_ascii=False))
    else:
        path = Path(profile)
        if not path.suffix and not path.is_absolute() and path.parent == Path("."):
            configured_candidate = (
                directory / f"{path.name}.json" if directory is not None else None
            )
            path = (
                configured_candidate
                if configured_candidate is not None and configured_candidate.is_file()
                else DEFAULT_PROFILE_DIRECTORY / f"{path.name}.json"
            )
        elif not path.is_absolute() and directory is not None:
            path = directory / path
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ProfileError("A raiz do perfil deve ser um objeto JSON")
    validate_profile(loaded)
    return loaded


def build_context(
    *,
    source_database: str,
    source_table: str,
    destination_schema: str,
    destination_table: str | None = None,
    partition_column: str | None = None,
    normalizer: Callable[[str], str] | None = str.casefold,
) -> dict[str, str]:
    """Cria o contexto preservando a origem e normalizando o destino em lowercase."""

    normalize = normalizer or (lambda value: value)
    source_database_name = _validate_identifier(source_database, "source_database")
    source_table_name = _validate_identifier(source_table, "source_table")
    destination = destination_table or f"{source_database_name}_{source_table_name}"
    normalized_partition = (
        _normalize_destination_identifier(partition_column, "partition_column")
        if partition_column is not None
        else "partition_disabled"
    )
    context = {
        "source_database": source_database_name,
        "source_table": source_table_name,
        "destination_table": normalize(destination).lower(),
        "destination_schema": normalize(destination_schema).lower(),
        "partition_column": normalized_partition,
        "partition_descriptor": (
            "mensal" if normalized_partition.casefold() == "dh_carga" else "attr"
        ),
    }
    for name, value in context.items():
        _validate_identifier(value, name)
    return context


def _column_from_source(source: Mapping[str, Any]) -> ColumnDefinition:
    if "name" not in source:
        raise ProfileError("Coluna autodescoberta sem name")
    source_name = _validate_identifier(str(source["name"]), "coluna de origem")
    name = _normalize_destination_identifier(source_name, "coluna de negócio")
    try:
        declaration = type_sql(dict(source))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f"Tipo inválido para a coluna de negócio {name}") from exc
    nullable = bool(source.get("is_nullable", source.get("nullable", False)))
    collation = source.get("collation_name", source.get("collation"))
    if collation is not None:
        _validate_identifier(str(collation), f"collation de {name}")
    return ColumnDefinition(
        name=name,
        sql_type=declaration,
        nullable=nullable,
        collation=str(collation) if collation is not None else None,
        role="business",
        source_name=source_name,
    )


def _column_from_profile(raw: Mapping[str, Any], role: str) -> ColumnDefinition:
    name = _normalize_destination_identifier(
        str(raw.get("name", "")), f"coluna {role}"
    )
    computed = raw.get("computed")
    declaration = raw.get("sql_type")
    result_declaration = raw.get("result_sql_type")
    if computed is None and (not isinstance(declaration, str) or not declaration.strip()):
        raise ProfileError(f"Coluna {name} deve declarar tipo ou expressão computed")
    if computed is not None and declaration is not None:
        raise ProfileError(f"Coluna computed {name} não deve declarar tipo")
    if computed is None and result_declaration is not None:
        raise ProfileError(f"Coluna não computed {name} não aceita result_sql_type")
    identity = bool(raw.get("identity", False))
    if identity and computed is not None:
        raise ProfileError(f"Coluna {name} não pode ser IDENTITY e computed")
    default_name = raw.get("default_name")
    default_expression = raw.get("default")
    if bool(default_name) != bool(default_expression):
        raise ProfileError(f"Coluna {name}: default e default_name devem ser definidos juntos")
    if default_name:
        default_name = _normalize_destination_identifier(
            str(default_name), f"default de {name}"
        )
    return ColumnDefinition(
        name=name,
        sql_type=(
            str(result_declaration)
            if computed is not None and result_declaration is not None
            else str(declaration)
            if declaration is not None
            else None
        ),
        nullable=bool(raw.get("nullable", False)),
        identity=identity,
        identity_seed=int(raw.get("identity_seed", 1)),
        identity_increment=int(raw.get("identity_increment", 1)),
        computed_expression=str(computed) if computed is not None else None,
        persisted=bool(raw.get("persisted", False)),
        default_expression=str(default_expression) if default_expression is not None else None,
        default_name=str(default_name) if default_name is not None else None,
        collation=str(raw["collation"]) if raw.get("collation") else None,
        role=role,
    )


def _index_from_profile(raw: Mapping[str, Any], *, primary_key: bool = False) -> IndexDefinition:
    name = _normalize_destination_identifier(str(raw.get("name", "")), "índice")
    raw_keys = raw.get("columns")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ProfileError(f"Índice {name} deve ter ao menos uma coluna-chave")
    keys: list[IndexKey] = []
    for raw_key in raw_keys:
        if not isinstance(raw_key, Mapping):
            raise ProfileError(f"Chave inválida no índice {name}")
        key_name = _normalize_destination_identifier(
            str(raw_key.get("name", "")), f"chave de {name}"
        )
        direction = str(raw_key.get("direction", "ASC")).upper()
        if direction not in {"ASC", "DESC"}:
            raise ProfileError(f"Direção inválida em {name}.{key_name}: {direction}")
        keys.append(IndexKey(key_name, direction))
    includes = tuple(
        _normalize_destination_identifier(str(column), f"INCLUDE de {name}")
        for column in raw.get("includes", [])
    )
    phase = "base" if primary_key else str(raw.get("phase", "secondary"))
    if phase not in {"base", "secondary"}:
        raise ProfileError(f"phase inválido no índice {name}: {phase}")
    return IndexDefinition(
        name=name,
        keys=tuple(keys),
        unique=True if primary_key else bool(raw.get("unique", False)),
        clustered=bool(raw.get("clustered", False)),
        includes=includes,
        filter_predicate=(
            str(raw["filter_predicate"]) if raw.get("filter_predicate") else None
        ),
        disabled=False,
        primary_key=primary_key,
        phase=phase,
    )


def resolve_profile(
    profile: str | Path | Mapping[str, Any],
    source_columns: Sequence[Mapping[str, Any]],
    *,
    source_database: str,
    source_table: str,
    destination_schema: str,
    destination_table: str | None = None,
    normalizer: Callable[[str], str] | None = str.casefold,
    directory: Path | None = None,
    bronze_event_id: Mapping[str, Any] | None = None,
    partition_column: str | None = None,
) -> TableLayout:
    """Resolve placeholders e intercala colunas autodescobertas na ordem exata."""

    raw_profile = load_profile(profile, directory=directory)
    context = build_context(
        source_database=source_database,
        source_table=source_table,
        destination_schema=destination_schema,
        destination_table=destination_table,
        partition_column=partition_column,
        normalizer=normalizer,
    )
    technical_template = raw_profile.get("technical_column", {}).get("name")
    if not isinstance(technical_template, str) or not technical_template:
        raise ProfileError("technical_column.name do perfil e obrigatorio")
    technical_name = _normalize_destination_identifier(
        str(_render(technical_template, context)), "technical_column.name"
    )
    context["technical_column"] = technical_name
    rendered = _render(raw_profile, context)
    table = _normalize_destination_identifier(
        str(rendered["table_name"]), "table_name"
    )
    technical = _column_from_profile(rendered["technical_column"], "technical")
    business = tuple(_column_from_source(column) for column in source_columns)
    additional = tuple(
        _column_from_profile(column, "metadata") for column in rendered["additional_columns"]
    )
    columns = (technical,) + business + additional

    folded_names: dict[str, str] = {}
    for column in columns:
        folded = column.name.casefold()
        if folded in folded_names:
            raise ProfileError(
                f"Colisão de coluna entre {folded_names[folded]!r} e {column.name!r}"
            )
        folded_names[folded] = column.name

    primary_key = _index_from_profile(rendered["primary_key"], primary_key=True)
    indexes = tuple(_index_from_profile(index) for index in rendered["indexes"])
    partition: PartitionDefinition | None = None
    if partition_column is not None:
        partition_raw = rendered.get("partitioning")
        if not isinstance(partition_raw, Mapping):
            raise ProfileError(
                f"O perfil {rendered['name']!r} nao oferece contrato de particionamento"
            )
        partition_name = context["partition_column"]
        matching_columns = [
            column for column in columns if column.name.casefold() == partition_name.casefold()
        ]
        if not matching_columns:
            raise ProfileError(
                f"Coluna de particionamento ausente no destino: {partition_name}"
            )
        partition_source = matching_columns[0]
        if partition_source.computed_expression is not None:
            raise ProfileError(
                f"Coluna de particionamento nao pode ser computed: {partition_name}"
            )
        normalized_partition_type = "".join(
            str(partition_source.sql_type or "").upper().split()
        )
        expected_partition_type = "".join(
            str(partition_raw["sql_type"]).upper().split()
        )
        if normalized_partition_type != expected_partition_type:
            raise ProfileError(
                f"Coluna de particionamento {partition_name} deve ser "
                f"{partition_raw['sql_type']}; encontrado {partition_source.sql_type}"
            )
        partition = PartitionDefinition(
            column=partition_name,
            function_name=_normalize_destination_identifier(
                str(partition_raw["function_name"]), "partitioning.function_name"
            ),
            scheme_name=_normalize_destination_identifier(
                str(partition_raw["scheme_name"]), "partitioning.scheme_name"
            ),
            clustered_index_name=_normalize_destination_identifier(
                str(partition_raw["clustered_index_name"]),
                "partitioning.clustered_index_name",
            ),
            descriptor=context["partition_descriptor"],
            sql_type=str(partition_raw["sql_type"]).upper(),
            range_direction=str(partition_raw["range_direction"]).upper(),
            interval=str(partition_raw["interval"]).casefold(),
            future_years=int(partition_raw["future_years"]),
            filegroup=str(partition_raw["filegroup"]).upper(),
        )
        primary_key = replace(primary_key, clustered=False)
        transformed_indexes: list[IndexDefinition] = []
        for candidate in indexes:
            transformed = (
                replace(candidate, clustered=False)
                if candidate.clustered
                else candidate
            )
            is_redundant_simple_partition_index = (
                not transformed.unique
                and not transformed.clustered
                and len(transformed.keys) == 1
                and transformed.keys[0].name.casefold() == partition_name.casefold()
                and not transformed.includes
                and transformed.filter_predicate is None
            )
            if not is_redundant_simple_partition_index:
                transformed_indexes.append(transformed)
        transformed_indexes.append(
            IndexDefinition(
                name=partition.clustered_index_name,
                keys=(IndexKey(partition.column, "ASC"),),
                unique=False,
                clustered=True,
                phase="base",
            )
        )
        indexes = tuple(transformed_indexes)
    all_index_names: set[str] = set()
    available = set(folded_names)
    for index in (primary_key,) + indexes:
        folded_index = index.name.casefold()
        if folded_index in all_index_names:
            raise ProfileError(f"Nome de índice duplicado: {index.name}")
        all_index_names.add(folded_index)
        for key in index.keys:
            if key.name.casefold() not in available:
                raise ProfileError(f"Índice {index.name} referencia coluna ausente: {key.name}")
        for include in index.includes:
            if include.casefold() not in available:
                raise ProfileError(f"Índice {index.name} inclui coluna ausente: {include}")
        if set(name.casefold() for name in index.includes) & {
            key.name.casefold() for key in index.keys
        }:
            raise ProfileError(f"Índice {index.name} repete chave em INCLUDE")

    clustered = [index.name for index in (primary_key,) + indexes if index.clustered]
    if len(clustered) != 1:
        raise ProfileError(
            "O perfil resolvido deve definir exatamente uma organização clustered; encontradas: "
            + ", ".join(clustered)
        )

    sequence_raw = rendered.get("sequence")
    technical_strategy = str(rendered["technical_column"].get("generation", ""))
    technical_source: str | None = None
    if str(rendered["name"]).casefold() == "bronze" and bronze_event_id is not None:
        configured_strategy = str(bronze_event_id.get("strategy", "sequence"))
        if configured_strategy == "sequence":
            configured_name = bronze_event_id.get("sequence_name")
            if configured_name:
                if sequence_raw is None:
                    raise ProfileError("Perfil Bronze não define uma sequence substituível")
                sequence_raw = dict(sequence_raw)
                sequence_raw["name"] = _render(str(configured_name), context)
            technical_strategy = "sequence"
        elif configured_strategy == "source_column":
            technical_source = _validate_identifier(
                str(bronze_event_id.get("source_column", "")),
                "bronze_event_id.source_column",
            )
            matches = [
                column for column in business if column.name.casefold() == technical_source.casefold()
            ]
            if not matches:
                raise ProfileError(
                    f"Coluna de ID de evento ausente na origem: {technical_source}"
                )
            if matches[0].sql_type is None or matches[0].sql_type.casefold() != "bigint":
                raise ProfileError("bronze_event_id.source_column deve ser BIGINT")
            if matches[0].nullable:
                raise ProfileError("bronze_event_id.source_column não pode aceitar NULL")
            # Preserve the exact catalog spelling for case-sensitive source
            # databases. Configuration matching remains case-insensitive, but
            # generated SQL must reference the real identifier.
            technical_source = matches[0].source_name
            sequence_raw = None
            technical_strategy = "source_column"
        else:
            raise ProfileError(
                "bronze_event_id.strategy deve ser sequence ou source_column"
            )
    sequence = None
    if sequence_raw is not None:
        if not isinstance(sequence_raw, Mapping):
            raise ProfileError("sequence deve ser objeto ou null")
        sequence = SequenceDefinition(
            name=_normalize_destination_identifier(
                str(sequence_raw.get("name", "")), "sequence"
            ),
            sql_type=str(sequence_raw.get("sql_type", "BIGINT")),
            start=int(sequence_raw.get("start", 1)),
            increment=int(sequence_raw.get("increment", 1)),
            cycle=bool(sequence_raw.get("cycle", False)),
        )
    if technical.identity and sequence is not None:
        raise ProfileError("Perfil IDENTITY não pode também gerar ID por sequence")
    if not technical.identity and technical_strategy == "sequence" and sequence is None:
        raise ProfileError("Perfil com geração sequence deve definir sequence")

    fill_rules: list[FillRule] = [
        FillRule(
            technical.name,
            f"source:{technical_source}" if technical_source else technical_strategy,
        )
    ]
    fill_rules.extend(FillRule(column.name, f"source:{column.source_name}") for column in business)
    fill_rules.extend(
        FillRule(column.name, str(raw_column.get("fill", "")))
        for column, raw_column in zip(additional, rendered["additional_columns"])
    )
    return TableLayout(
        profile_name=str(rendered["name"]),
        profile_version=int(rendered["profile_version"]),
        schema=context["destination_schema"],
        table=table,
        source_database=context["source_database"],
        source_table=context["source_table"],
        columns=columns,
        primary_key=primary_key,
        indexes=indexes,
        sequence=sequence,
        technical_id_strategy=technical_strategy,
        technical_id_source_column=technical_source,
        fill_rules=tuple(fill_rules),
        partition=partition,
    )


build_layout = resolve_profile


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "ColumnDefinition",
    "DEFAULT_PROFILE_DIRECTORY",
    "FillRule",
    "IndexDefinition",
    "IndexKey",
    "PartitionDefinition",
    "PROFILE_VERSION",
    "ProfileError",
    "SequenceDefinition",
    "TableLayout",
    "build_context",
    "build_layout",
    "load_profile",
    "resolve_profile",
    "validate_profile",
]
