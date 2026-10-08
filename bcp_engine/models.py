"""Objetos de domínio serializáveis do BulkFlow.

Os modelos deste módulo não abrem conexões e não escrevem arquivos. Isso permite
que CLI, testes e uma futura interface usem o mesmo contrato sem depender de
``print``/``input``.
"""
from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field
from datetime import datetime
from enum import Enum
import re
from typing import Any

from .util import sha256_json, validate_canonical_uuid, validate_sha256_hex


class TableStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    EXPORTED = "EXPORTED"
    EMPTY = "EMPTY"
    SKIPPED_NO_KEY = "SKIPPED_NO_KEY"
    SKIPPED_DIRECT_LIMIT = "SKIPPED_KEYLESS_DIRECT_LOAD_OVER_LIMIT"
    SKIPPED_DIRECT_COUNT_UNAVAILABLE = "SKIPPED_KEYLESS_DIRECT_LOAD_COUNT_UNAVAILABLE"
    SKIPPED_INVALID_WATERMARK = "SKIPPED_INVALID_WATERMARK"
    SKIPPED_NULL_WATERMARK = "SKIPPED_NULL_WATERMARK"
    SKIPPED_CDC_ACTIVATION_FAILED = "SKIPPED_CDC_ACTIVATION_FAILED"
    SKIPPED_DESTINATION_SPACE = "SKIPPED_DESTINATION_INSUFFICIENT_SPACE"
    LAYOUT_ERROR = "LAYOUT_ERROR"
    SCHEMA_EVOLUTION_PENDING = "SCHEMA_EVOLUTION_PENDING"
    EXPORT_ERROR = "EXPORT_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    TIE_GROUP_TOO_LARGE = "TIE_GROUP_TOO_LARGE"
    DIRECT_SOURCE_DRIFT = "KEYLESS_DIRECT_LOAD_SOURCE_DRIFT"
    PARTIAL_ERROR = "PARTIAL_ERROR"
    PARTIALLY_IMPORTED = "PARTIALLY_IMPORTED"
    DATA_COMPLETE_INDEXES_PENDING = "DATA_COMPLETE_INDEXES_PENDING"


class ExecutionState(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_PENDING_ITEMS = "COMPLETED_WITH_PENDING_ITEMS"
    GLOBAL_ERROR = "GLOBAL_ERROR"


class BlockState(str, Enum):
    PLANNED = "PLANNED"
    EXPORTING = "EXPORTING"
    EXPORTED = "EXPORTED"
    IMPORTING = "IMPORTING"
    IMPORTED = "IMPORTED"
    EMPTY_CONFIRMED = "EMPTY_CONFIRMED"


class AttemptState(str, Enum):
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class IndexState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    BASE_READY = "BASE_READY"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    DEFERRED = "DEFERRED"


SUCCESS_STATUSES = {
    TableStatus.COMPLETED.value,
    TableStatus.EXPORTED.value,
    TableStatus.EMPTY.value,
}


# Tipos cujo texto de bookmark possui uma conversao SQL fechada e validada
# pelo importador. O manifesto e uma fronteira de confianca: qualquer tipo
# fora desta lista deve ser recusado antes de abrir a conexao de destino.
SUPPORTED_WATERMARK_TYPES = frozenset(
    {
        "bigint",
        "int",
        "smallint",
        "tinyint",
        "decimal",
        "numeric",
        "char",
        "varchar",
        "nchar",
        "nvarchar",
        "date",
        "datetime",
        "smalldatetime",
        "datetime2",
        "datetimeoffset",
        "time",
        "bit",
        "money",
        "smallmoney",
        "binary",
        "varbinary",
        "uniqueidentifier",
    }
)

_LANDING_METADATA_COLUMNS = frozenset({"aud_ccid", "aud_cntrrn", "aud_enttyp"})
_LANDING_METADATA_CONSTANT_TYPES = {
    "aud_ccid": "BIGINT",
    "aud_cntrrn": "INT",
    "aud_enttyp": "VARCHAR(30)",
}


_IDENTIFIER_PATTERN = re.compile(r"^[^\x00-\x1f]{1,128}$")


def _strict_object(
    value: Any,
    *,
    field_name: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} deve ser um objeto JSON")
    allowed = required | (optional or set())
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise ValueError(
            f"{field_name} contém campos desconhecidos: {', '.join(unknown)}"
        )
    if missing:
        raise ValueError(
            f"{field_name} não contém campos obrigatórios: {', '.join(missing)}"
        )
    return value


def _nonempty_string(value: Any, field_name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} deve ser uma string não vazia")
    if identifier and not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} deve ser um identificador SQL válido")
    return value


@dataclass(frozen=True)
class ProjectionColumn:
    name: str
    sql_type: str
    nullable: bool
    source_expression: str | None = None
    collation: str | None = None
    role: str = "business"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_endpoint_snapshot(
    value: Any,
    *,
    field_name: str,
    destination: bool,
) -> None:
    required = {"instance", "database", "schema", "table"}
    if destination:
        required.add("area")
    else:
        required.add("read_database")
    endpoint = _strict_object(
        value,
        field_name=field_name,
        required=required,
        optional={"effective_identity", "port"},
    )
    if "port" in endpoint and (
        type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65_535
    ):
        raise ValueError(f"{field_name}.port deve estar entre 1 e 65535")
    for name in required:
        _nonempty_string(
            endpoint[name],
            f"{field_name}.{name}",
            identifier=name in {"database", "read_database", "schema", "table"},
        )
    if destination and endpoint["area"] not in {"bronze", "landing"}:
        raise ValueError(f"{field_name}.area deve ser bronze ou landing")
    if destination:
        for name in ("schema", "table"):
            if endpoint[name] != endpoint[name].lower():
                raise ValueError(f"{field_name}.{name} deve usar somente minúsculas")
    identity = endpoint.get("effective_identity")
    if identity is not None and not isinstance(identity, dict):
        raise ValueError(f"{field_name}.effective_identity deve ser um objeto JSON")


def _validate_projection(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("projection deve ser um array JSON não vazio")
    required = {
        "name",
        "type_name",
        "max_length",
        "precision",
        "scale",
        "collation_name",
        "is_nullable",
    }
    optional = {
        "column_id",
        "is_identity",
        "is_computed",
        "is_filestream",
        "is_sparse",
        "is_column_set",
        "is_hidden",
        "generated_always_type",
        "encryption_type",
        "xml_collection_id",
        "is_deterministic",
        "is_persisted",
        "is_masked",
        "declared_type_name",
        "is_user_defined",
        "is_assembly_type",
    }
    names: set[str] = set()
    for index, raw in enumerate(value):
        field_name = f"projection[{index}]"
        column = _strict_object(
            raw, field_name=field_name, required=required, optional=optional
        )
        name = _nonempty_string(column["name"], f"{field_name}.name", identifier=True)
        folded = name.casefold()
        if folded in names:
            raise ValueError(f"projection contém coluna repetida: {name}")
        names.add(folded)
        _nonempty_string(column["type_name"], f"{field_name}.type_name")
        if type(column["is_nullable"]) is not bool:
            raise ValueError(f"{field_name}.is_nullable deve ser booleano")
        for child in ("max_length", "precision", "scale"):
            if column[child] is not None and type(column[child]) is not int:
                raise ValueError(f"{field_name}.{child} deve ser inteiro ou null")
        if column["collation_name"] is not None and not isinstance(
            column["collation_name"], str
        ):
            raise ValueError(f"{field_name}.collation_name deve ser string ou null")


def _validate_watermark(value: Any, *, transfer_mode: str) -> None:
    if not isinstance(value, list):
        raise ValueError("watermark deve ser um array JSON")
    names: set[str] = set()
    for index, raw in enumerate(value):
        field_name = f"watermark[{index}]"
        item = _strict_object(
            raw,
            field_name=field_name,
            required={"name", "direction", "type_name", "collation"},
        )
        name = _nonempty_string(item["name"], f"{field_name}.name", identifier=True)
        if name.casefold() in names:
            raise ValueError(f"watermark contém coluna repetida: {name}")
        names.add(name.casefold())
        if item["direction"] not in {"ASC", "DESC"}:
            raise ValueError(f"{field_name}.direction deve ser ASC ou DESC")
        type_name = _nonempty_string(
            item["type_name"], f"{field_name}.type_name"
        )
        if type_name.casefold() not in SUPPORTED_WATERMARK_TYPES:
            raise ValueError(
                f"{field_name}.type_name nao e suportado para bookmark"
            )
        if item["collation"] is not None and not isinstance(item["collation"], str):
            raise ValueError(f"{field_name}.collation deve ser string ou null")
    if transfer_mode == "KEYSET" and not value:
        raise ValueError("Manifesto keyset deve descrever watermark não vazio")


def _validate_metadata_mapping(value: Any, *, profile: str) -> None:
    if not isinstance(value, dict):
        raise ValueError("metadata_mapping deve ser um objeto JSON")
    keys = set(value)
    unknown = sorted(keys - _LANDING_METADATA_COLUMNS)
    if unknown:
        raise ValueError(
            "metadata_mapping contem colunas nao suportadas: " + ", ".join(unknown)
        )
    if profile.casefold() == "landing":
        missing = sorted(_LANDING_METADATA_COLUMNS - keys)
        if missing:
            raise ValueError(
                "Manifesto Landing exige metadata_mapping completo: "
                + ", ".join(missing)
            )
    elif keys:
        raise ValueError("metadata_mapping somente se aplica ao perfil Landing")
    for target, raw in value.items():
        _nonempty_string(target, "metadata_mapping.<column>", identifier=True)
        item = _strict_object(
            raw,
            field_name=f"metadata_mapping.{target}",
            required=set(),
            optional={"source_column", "constant", "sql_type"},
        )
        has_source = "source_column" in item
        has_constant = "constant" in item
        if has_source == has_constant:
            raise ValueError(
                f"metadata_mapping.{target} deve conter exatamente source_column ou constant"
            )
        if has_source:
            if set(item) != {"source_column"}:
                raise ValueError(
                    f"metadata_mapping.{target} com source_column não aceita outros campos"
                )
            _nonempty_string(
                item["source_column"],
                f"metadata_mapping.{target}.source_column",
                identifier=True,
            )
        else:
            if isinstance(item["constant"], (dict, list)):
                raise ValueError(
                    f"metadata_mapping.{target}.constant deve ser um escalar JSON"
                )
            if "sql_type" in item:
                sql_type = _nonempty_string(
                    item["sql_type"], f"metadata_mapping.{target}.sql_type"
                ).upper().replace(" ", "")
                expected_type = _LANDING_METADATA_CONSTANT_TYPES[target]
                if sql_type != expected_type:
                    raise ValueError(
                        f"metadata_mapping.{target}.sql_type deve ser exatamente "
                        f"{expected_type}"
                    )


def _validate_layout_contract(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("layout_contract deve ser um objeto JSON")
    if not value:
        return
    contract = _strict_object(
        value,
        field_name="layout_contract",
        required={
            "version",
            "profile",
            "profile_version",
            "schema",
            "table",
            "columns",
            "primary_key",
            "indexes",
            "sequence",
            "technical_id_strategy",
            "technical_id_source_column",
            "fill_rules",
            "layout_hash",
        },
        optional={"partition"},
    )
    if type(contract["version"]) is not int or type(contract["profile_version"]) is not int:
        raise ValueError("Versões de layout_contract devem ser inteiros")
    for name in ("profile", "schema", "table", "technical_id_strategy"):
        _nonempty_string(contract[name], f"layout_contract.{name}")
    for name in ("schema", "table"):
        if contract[name] != contract[name].lower():
            raise ValueError(f"layout_contract.{name} deve usar somente minúsculas")
    if not isinstance(contract["columns"], list) or not isinstance(contract["indexes"], list):
        raise ValueError("layout_contract.columns/indexes devem ser arrays JSON")
    if not isinstance(contract["primary_key"], dict):
        raise ValueError("layout_contract.primary_key deve ser um objeto JSON")
    if contract["sequence"] is not None and not isinstance(contract["sequence"], dict):
        raise ValueError("layout_contract.sequence deve ser objeto JSON ou null")
    if not isinstance(contract["fill_rules"], list):
        raise ValueError("layout_contract.fill_rules deve ser um array JSON")
    partition = contract.get("partition")
    if partition is not None:
        partition_fields = {
            "column",
            "function_name",
            "scheme_name",
            "clustered_index_name",
            "descriptor",
            "sql_type",
            "range_direction",
            "interval",
            "future_years",
            "filegroup",
        }
        partition = _strict_object(
            partition,
            field_name="layout_contract.partition",
            required=partition_fields,
        )
        for name in (
            "column",
            "function_name",
            "scheme_name",
            "clustered_index_name",
            "descriptor",
        ):
            normalized = _nonempty_string(
                partition[name], f"layout_contract.partition.{name}", identifier=True
            )
            if normalized != normalized.lower():
                raise ValueError(
                    f"layout_contract.partition.{name} deve usar somente minusculas"
                )
        if str(partition["sql_type"]).upper().replace(" ", "") != "DATETIME2(7)":
            raise ValueError("layout_contract.partition.sql_type deve ser DATETIME2(7)")
        if partition["range_direction"] != "RIGHT":
            raise ValueError("layout_contract.partition.range_direction deve ser RIGHT")
        if partition["interval"] != "month":
            raise ValueError("layout_contract.partition.interval deve ser month")
        if partition["filegroup"] != "PRIMARY":
            raise ValueError("layout_contract.partition.filegroup deve ser PRIMARY")
        if type(partition["future_years"]) is not int or partition["future_years"] < 1:
            raise ValueError(
                "layout_contract.partition.future_years deve ser um inteiro positivo"
            )
    source_column = contract["technical_id_source_column"]
    if source_column is not None:
        _nonempty_string(
            source_column,
            "layout_contract.technical_id_source_column",
            identifier=True,
        )

    column_fields = {
        "name",
        "sql_type",
        "nullable",
        "identity",
        "identity_seed",
        "identity_increment",
        "computed_expression",
        "persisted",
        "default_expression",
        "default_name",
        "collation",
        "role",
        "source_name",
    }
    for index, raw in enumerate(contract["columns"]):
        field_name = f"layout_contract.columns[{index}]"
        column = _strict_object(
            raw, field_name=field_name, required=column_fields
        )
        column_name = _nonempty_string(
            column["name"], f"{field_name}.name", identifier=True
        )
        if column_name != column_name.lower():
            raise ValueError(f"{field_name}.name deve usar somente minúsculas")
        if column["sql_type"] is not None:
            _nonempty_string(column["sql_type"], f"{field_name}.sql_type")
        for child in ("nullable", "identity", "persisted"):
            if type(column[child]) is not bool:
                raise ValueError(f"{field_name}.{child} deve ser booleano")
        for child in ("identity_seed", "identity_increment"):
            if type(column[child]) is not int:
                raise ValueError(f"{field_name}.{child} deve ser inteiro")
        _nonempty_string(column["role"], f"{field_name}.role")

    index_fields = {
        "name",
        "keys",
        "unique",
        "clustered",
        "includes",
        "filter_predicate",
        "disabled",
        "primary_key",
        "phase",
    }

    def validate_index(raw: Any, field_name: str) -> None:
        index = _strict_object(raw, field_name=field_name, required=index_fields)
        index_name = _nonempty_string(
            index["name"], f"{field_name}.name", identifier=True
        )
        if index_name != index_name.lower():
            raise ValueError(f"{field_name}.name deve usar somente minúsculas")
        if not isinstance(index["keys"], (list, tuple)) or not index["keys"]:
            raise ValueError(f"{field_name}.keys deve ser um array JSON não vazio")
        for key_index, raw_key in enumerate(index["keys"]):
            key_field = f"{field_name}.keys[{key_index}]"
            key = _strict_object(
                raw_key, field_name=key_field, required={"name", "direction"}
            )
            key_name = _nonempty_string(
                key["name"], f"{key_field}.name", identifier=True
            )
            if key_name != key_name.lower():
                raise ValueError(f"{key_field}.name deve usar somente minúsculas")
            if key["direction"] not in {"ASC", "DESC"}:
                raise ValueError(f"{key_field}.direction deve ser ASC ou DESC")
        if not isinstance(index["includes"], (list, tuple)):
            raise ValueError(f"{field_name}.includes deve ser um array JSON")
        for include_position, include in enumerate(index["includes"]):
            include_name = _nonempty_string(
                include,
                f"{field_name}.includes[{include_position}]",
                identifier=True,
            )
            if include_name != include_name.lower():
                raise ValueError(
                    f"{field_name}.includes[{include_position}] deve usar somente minúsculas"
                )
        for child in ("unique", "clustered", "disabled", "primary_key"):
            if type(index[child]) is not bool:
                raise ValueError(f"{field_name}.{child} deve ser booleano")
        _nonempty_string(index["phase"], f"{field_name}.phase")

    validate_index(contract["primary_key"], "layout_contract.primary_key")
    for index, raw in enumerate(contract["indexes"]):
        validate_index(raw, f"layout_contract.indexes[{index}]")

    if contract["sequence"] is not None:
        sequence = _strict_object(
            contract["sequence"],
            field_name="layout_contract.sequence",
            required={"name", "sql_type", "start", "increment", "cycle"},
        )
        sequence_name = _nonempty_string(
            sequence["name"], "layout_contract.sequence.name", identifier=True
        )
        if sequence_name != sequence_name.lower():
            raise ValueError("layout_contract.sequence.name deve usar somente minúsculas")
        _nonempty_string(sequence["sql_type"], "layout_contract.sequence.sql_type")
        for child in ("start", "increment"):
            if type(sequence[child]) is not int:
                raise ValueError(f"layout_contract.sequence.{child} deve ser inteiro")
        if type(sequence["cycle"]) is not bool:
            raise ValueError("layout_contract.sequence.cycle deve ser booleano")

    for index, raw in enumerate(contract["fill_rules"]):
        field_name = f"layout_contract.fill_rules[{index}]"
        rule = _strict_object(
            raw, field_name=field_name, required={"column", "value"}
        )
        rule_column = _nonempty_string(
            rule["column"], f"{field_name}.column", identifier=True
        )
        if rule_column != rule_column.lower():
            raise ValueError(f"{field_name}.column deve usar somente minúsculas")
        _nonempty_string(rule["value"], f"{field_name}.value")


@dataclass
class BlockManifest:
    """Manifesto portátil de um bloco publicado.

    ``complete`` só pode ser ``True`` depois de o arquivo final, seu formato e
    seus hashes terem sido confirmados. O manifesto nunca contém credenciais.
    """

    manifest_version: int
    complete: bool
    execution_id: str
    dataset_id: str
    table_id: str
    block_id: str
    block_number: int
    attempt: int
    source: dict[str, Any]
    destination: dict[str, Any] | None
    authentication: dict[str, str]
    profile: str
    projection: list[dict[str, Any]]
    projection_hash: str
    layout_hash: str
    watermark: list[dict[str, Any]]
    lower_bound: list[str] | None
    upper_bound: list[str]
    final_limit: list[str] | None
    rows_exported: int
    file_bytes: int
    data_file: str | None
    data_sha256: str | None
    format_file: str
    format_sha256: str
    exported_at: str
    consistency: str = "LIVE_BEST_EFFORT"
    empty_range: bool = False
    metadata_mapping: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    layout_contract: dict[str, Any] = field(default_factory=dict)
    table_empty: bool = False
    transfer_mode: str = "KEYSET"
    source_rows_at_capture: int | None = None

    def validate(self) -> None:
        forbidden = {"senha", "password", "pwd", "secret", "segredo"}

        def inspect(value: Any, path: str = "") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).casefold() in forbidden:
                        raise ValueError(f"Manifesto contém campo secreto: {path}{key}")
                    inspect(item, path + str(key) + ".")
            elif isinstance(value, list):
                for item in value:
                    inspect(item, path)
            elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    decoded = __import__("json").loads(value)
                except (TypeError, ValueError):
                    return
                if isinstance(decoded, (dict, list)):
                    inspect(decoded, path)

        inspect(asdict(self))
        if self.manifest_version != 2:
            raise ValueError("Versão de manifesto incompatível")
        if type(self.complete) is not bool or not self.complete:
            raise ValueError("Manifesto ainda não foi concluído")
        if (
            type(self.block_number) is not int
            or type(self.attempt) is not int
            or self.block_number < 1
            or self.attempt < 1
        ):
            raise ValueError("Número de bloco/tentativa inválido")
        if (
            type(self.rows_exported) is not int
            or type(self.file_bytes) is not int
            or self.rows_exported < 0
            or self.file_bytes < 0
        ):
            raise ValueError("Contadores do manifesto não podem ser negativos")
        if type(self.empty_range) is not bool or type(self.table_empty) is not bool:
            raise ValueError("Indicadores booleanos inválidos no manifesto")
        for field_name, value in (
            ("execution_id", self.execution_id),
            ("block_id", self.block_id),
        ):
            try:
                validate_canonical_uuid(value, field_name)
            except ValueError as exc:
                raise ValueError(f"{field_name} inválido no manifesto") from exc
        for field_name, value in (
            ("dataset_id", self.dataset_id),
            ("table_id", self.table_id),
        ):
            try:
                validate_sha256_hex(value, field_name)
            except ValueError as exc:
                raise ValueError(f"{field_name} inválido no manifesto")
        _nonempty_string(self.profile, "profile")
        try:
            datetime.fromisoformat(self.exported_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("exported_at inválido no manifesto") from exc
        if self.consistency not in {"LIVE_BEST_EFFORT"}:
            raise ValueError("consistency inválida no manifesto")
        _validate_endpoint_snapshot(self.source, field_name="source", destination=False)
        if self.destination is not None:
            _validate_endpoint_snapshot(
                self.destination, field_name="destination", destination=True
            )
            if self.profile.casefold() != str(self.destination["area"]).casefold():
                raise ValueError("profile diverge da area de destino do manifesto")
        authentication = _strict_object(
            self.authentication,
            field_name="authentication",
            required={"source"},
            optional={"destination"},
        )
        if (self.destination is None) != ("destination" not in authentication):
            raise ValueError(
                "authentication.destination deve acompanhar o snapshot de destination"
            )
        for name, descriptor in authentication.items():
            _nonempty_string(descriptor, f"authentication.{name}")
        _validate_projection(self.projection)
        if self.transfer_mode not in {"KEYSET", "DIRECT_KEYLESS"}:
            raise ValueError("Modo de transferência inválido no manifesto")
        _validate_watermark(self.watermark, transfer_mode=self.transfer_mode)
        if self.transfer_mode == "DIRECT_KEYLESS":
            if self.watermark != []:
                raise ValueError("Carga direta sem chave não pode fingir marca d'água")
            if self.block_number != 1:
                raise ValueError("Carga direta sem chave deve possuir exatamente o bloco 1")
            if self.lower_bound is not None or self.upper_bound != [] or self.final_limit != []:
                raise ValueError("Carga direta sem chave deve usar limites vazios sem bookmark")
            if (
                type(self.source_rows_at_capture) is not int
                or self.source_rows_at_capture < 0
            ):
                raise ValueError("Carga direta exige estimativa de metadados persistida")
            if self.table_empty != (self.rows_exported == 0):
                raise ValueError("Indicador de tabela vazia diverge da exportacao direta")
        else:
            if self.source_rows_at_capture is not None:
                raise ValueError("Carga keyset não aceita contagem capturada do modo direto")
            width = len(self.watermark)
            if self.table_empty:
                if self.lower_bound is not None or self.upper_bound != [] or self.final_limit != []:
                    raise ValueError("Tabela vazia deve usar limites portáteis vazios")
            else:
                if not isinstance(self.upper_bound, list) or len(self.upper_bound) != width:
                    raise ValueError("Limite superior incompatível com a marca d'água")
                if not isinstance(self.final_limit, list) or len(self.final_limit) != width:
                    raise ValueError("Teto final incompatível com a marca d'água")
                if self.lower_bound is not None and (
                    not isinstance(self.lower_bound, list) or len(self.lower_bound) != width
                ):
                    raise ValueError("Limite inferior incompatível com a marca d'água")
            assert isinstance(self.upper_bound, list)
            assert isinstance(self.final_limit, list)
            if any(value is None for value in [*self.upper_bound, *self.final_limit]):
                raise ValueError("Bookmarks do manifesto não podem conter NULL")
            if self.lower_bound is not None and any(value is None for value in self.lower_bound):
                raise ValueError("Bookmark inferior não pode conter NULL")
        for field_name, values in (
            ("lower_bound", self.lower_bound),
            ("upper_bound", self.upper_bound),
            ("final_limit", self.final_limit),
        ):
            if values is not None and (
                not isinstance(values, list)
                or any(not isinstance(item, str) for item in values)
            ):
                raise ValueError(f"{field_name} deve ser um array JSON de strings")
        if self.table_empty and (not self.empty_range or self.rows_exported != 0):
            raise ValueError("Tabela vazia deve ser uma faixa vazia sem linhas")
        for name, value in (
            ("projection_hash", self.projection_hash),
            ("layout_hash", self.layout_hash),
            ("format_sha256", self.format_sha256),
        ):
            try:
                validate_sha256_hex(value, name)
            except ValueError as exc:
                raise ValueError(f"{name} inválido no manifesto")
        if self.data_sha256 is not None and (
            not isinstance(self.data_sha256, str)
        ):
            raise ValueError("data_sha256 inválido no manifesto")
        if self.data_sha256 is not None:
            try:
                validate_sha256_hex(self.data_sha256, "data_sha256")
            except ValueError as exc:
                raise ValueError("data_sha256 inválido no manifesto") from exc
        _validate_metadata_mapping(self.metadata_mapping, profile=self.profile)
        if not isinstance(self.warnings, list) or not all(
            isinstance(item, str) for item in self.warnings
        ):
            raise ValueError("warnings deve ser um array JSON de strings")
        _validate_layout_contract(self.layout_contract)
        if self.layout_contract:
            embedded_hash = self.layout_contract.get("layout_hash")
            hash_value = {
                key: child
                for key, child in self.layout_contract.items()
                if key not in {"schema", "layout_hash"}
            }
            if embedded_hash != self.layout_hash or sha256_json(hash_value) != self.layout_hash:
                raise ValueError("Snapshot do layout diverge do layout_hash")
        if self.empty_range:
            if (
                self.rows_exported != 0
                or self.file_bytes != 0
                or self.data_file is not None
                or self.data_sha256 is not None
            ):
                raise ValueError(
                    "Faixa vazia não pode conter linhas, bytes, arquivo ou hash de dados"
                )
        elif not all((self.data_file, self.data_sha256, self.format_file, self.format_sha256)):
            raise ValueError("Manifesto de dados está incompleto")
        for field_name, value in (
            ("data_file", self.data_file),
            ("format_file", self.format_file),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value or value != value.replace("\\", "/").split("/")[-1]:
                raise ValueError(f"{field_name} deve conter somente o nome do arquivo")
        expected_stem = f"block_{self.block_number:012d}"
        if self.data_file is not None and self.data_file != expected_stem + ".bcp":
            raise ValueError("data_file diverge do nome canônico do bloco")
        if self.format_file != expected_stem + ".xml":
            raise ValueError("format_file diverge do nome canônico do bloco")

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BlockManifest":
        if not isinstance(value, dict):
            raise ValueError("A raiz do manifesto deve ser um objeto JSON")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("Campos desconhecidos no manifesto: " + ", ".join(sorted(unknown)))
        required = {
            name
            for name, definition in cls.__dataclass_fields__.items()
            if definition.default is MISSING and definition.default_factory is MISSING
        }
        missing = required - set(value)
        if missing:
            raise ValueError(
                "Campos obrigatórios ausentes no manifesto: "
                + ", ".join(sorted(missing))
            )
        result = cls(**value)
        result.validate()
        return result


@dataclass
class TableResult:
    source_table: str
    destination_table: str | None
    status: str
    reason: str | None = None
    estimated_rows: int | None = None
    rows_exported: int = 0
    rows_imported: int = 0
    destination_rows_verified: int | None = None
    bytes_exported: int = 0
    # ``bytes_exported`` is cumulative for the execution and is restored on
    # resume.  Throughput, however, must not divide those historical bytes by
    # the wall time of a later no-op resume.  Keep the bytes actually produced
    # by this invocation as the explicit numerator.
    bytes_exported_this_invocation: int = 0
    retained_files: int = 0
    duration_seconds: float = 0.0
    watermark: list[dict[str, Any]] = field(default_factory=list)
    transfer_mode: str = "KEYSET"
    source_rows_at_capture: int | None = None
    final_limit: list[str] | None = None
    last_exported: list[str] | None = None
    last_imported: list[str] | None = None
    index_state: str = IndexState.NOT_STARTED.value
    warnings: list[str] = field(default_factory=list)
    next_action: str | None = None

    @property
    def throughput_bytes_per_second(self) -> float | None:
        if self.duration_seconds <= 0 or self.bytes_exported_this_invocation <= 0:
            return None
        return self.bytes_exported_this_invocation / self.duration_seconds

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["throughput_bytes_per_second"] = self.throughput_bytes_per_second
        return value


@dataclass
class ExecutionReport:
    execution_id: str
    command: str
    started_at: str
    finished_at: str | None = None
    consistency: str = "LIVE_BEST_EFFORT"
    tables: list[TableResult] = field(default_factory=list)
    cdc_database: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    global_error: str | None = None

    def exit_code(self) -> int:
        if self.global_error:
            return 1
        if any(item.status not in SUCCESS_STATUSES for item in self.tables):
            return 2
        return 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "command": self.command,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "consistency": self.consistency,
            "tables": [item.as_dict() for item in self.tables],
            "cdc_database": dict(self.cdc_database) if self.cdc_database else None,
            "warnings": list(self.warnings),
            "global_error": self.global_error,
            "exit_code": self.exit_code(),
        }
