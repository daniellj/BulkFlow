"""Contrato único entre projeção BCP, layout físico e INSERT de destino."""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .profiles import (
    ColumnDefinition,
    FillRule,
    IndexDefinition,
    IndexKey,
    PartitionDefinition,
    SequenceDefinition,
    TableLayout,
)
from .util import qi, qs, sha256_json


class ProjectionError(ValueError):
    pass


def apply_bronze_id_strategy(
    layout: TableLayout,
    strategy: Mapping[str, Any],
) -> TableLayout:
    if layout.profile_name.casefold() != "bronze":
        return layout
    kind = strategy.get("strategy", "sequence")
    if kind == "sequence":
        pattern = str(strategy.get("sequence_name", "seq_{destination_table}"))
        name = pattern.format(destination_table=layout.table)
        return replace(
            layout,
            sequence=SequenceDefinition(name=name),
            technical_id_strategy="sequence",
            technical_id_source_column=None,
        )
    if kind == "source_column":
        source = strategy.get("source_column")
        if not source:
            raise ProjectionError("source_column é obrigatória para a estratégia Bronze source_column")
        return replace(
            layout,
            sequence=None,
            technical_id_strategy="source_column",
            technical_id_source_column=str(source),
        )
    raise ProjectionError(f"Estratégia de ID Bronze inválida: {kind}")


def projection_contract(layout: TableLayout, source_columns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source_by_name = {str(column["name"]).casefold(): dict(column) for column in source_columns}
    projection: list[dict[str, Any]] = []
    for business in layout.business_columns:
        if business.source_name is None or business.source_name.casefold() not in source_by_name:
            raise ProjectionError(f"Coluna de negócio sem origem: {business.name}")
        projection.append(source_by_name[business.source_name.casefold()])
    value = {
        "version": 2,
        "profile": layout.profile_name,
        "source_database": layout.source_database,
        "source_table": layout.source_table,
        "target_schema": layout.schema,
        "target_table": layout.table,
        "columns": projection,
    }
    hash_value = {key: child for key, child in value.items() if key != "target_schema"}
    return {**value, "projection_hash": sha256_json(hash_value)}


def layout_contract(layout: TableLayout) -> dict[str, Any]:
    value = {
        "version": 2,
        "profile": layout.profile_name,
        "profile_version": layout.profile_version,
        "schema": layout.schema,
        "table": layout.table,
        "columns": [asdict(column) for column in layout.columns],
        "primary_key": asdict(layout.primary_key),
        "indexes": [asdict(index) for index in layout.indexes],
        "sequence": asdict(layout.sequence) if layout.sequence else None,
        "technical_id_strategy": layout.technical_id_strategy,
        "technical_id_source_column": layout.technical_id_source_column,
        "fill_rules": [asdict(rule) for rule in layout.fill_rules],
    }
    # Preserve byte-for-byte hashes for legacy, non-partitioned manifests.
    # Partition metadata is present only when the optional feature is active.
    if layout.partition is not None:
        value["partition"] = asdict(layout.partition)
    # O esquema e roteamento operacional: os mesmos bytes exportados podem ser
    # importados depois em outro endpoint/esquema com o mesmo perfil exato.
    hash_value = {key: child for key, child in value.items() if key != "schema"}
    return {**value, "layout_hash": sha256_json(hash_value)}


def layout_from_contract(
    value: Mapping[str, Any],
    *,
    schema: str | None = None,
    source_database: str,
    source_table: str,
) -> TableLayout:
    """Reconstrói o layout materializado preservado em um manifesto."""

    contract = dict(value)
    expected_hash = str(contract.get("layout_hash", ""))
    hash_value = {
        key: child
        for key, child in contract.items()
        if key not in {"schema", "layout_hash"}
    }
    if not expected_hash or sha256_json(hash_value) != expected_hash:
        raise ProjectionError("Snapshot de layout inválido ou adulterado")
    if int(contract.get("version", 0)) != 2:
        raise ProjectionError("Versão do snapshot de layout não suportada")

    def index(raw: Mapping[str, Any]) -> IndexDefinition:
        payload = dict(raw)
        payload["keys"] = tuple(IndexKey(**dict(item)) for item in payload.get("keys", ()))
        payload["includes"] = tuple(payload.get("includes", ()))
        return IndexDefinition(**payload)

    try:
        columns = tuple(ColumnDefinition(**dict(item)) for item in contract["columns"])
        primary_key = index(contract["primary_key"])
        indexes = tuple(index(item) for item in contract["indexes"])
        sequence_raw = contract.get("sequence")
        sequence = SequenceDefinition(**dict(sequence_raw)) if sequence_raw else None
        fill_rules = tuple(FillRule(**dict(item)) for item in contract["fill_rules"])
        partition_raw = contract.get("partition")
        partition = (
            PartitionDefinition(**dict(partition_raw)) if partition_raw is not None else None
        )
        layout = TableLayout(
            profile_name=str(contract["profile"]),
            profile_version=int(contract["profile_version"]),
            schema=str(schema if schema is not None else contract["schema"]),
            table=str(contract["table"]),
            # ``resolve_profile`` materializa estes identificadores pelo
            # normalizador padrao (casefold). A reconstrucao deve aplicar a
            # mesma regra para reproduzir exatamente o layout_hash.
            source_database=str(source_database),
            source_table=str(source_table),
            columns=columns,
            primary_key=primary_key,
            indexes=indexes,
            sequence=sequence,
            technical_id_strategy=str(contract["technical_id_strategy"]),
            technical_id_source_column=contract.get("technical_id_source_column"),
            fill_rules=fill_rules,
            partition=partition,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionError("Snapshot de layout incompleto ou inválido") from exc
    if layout_contract(layout)["layout_hash"] != expected_hash:
        raise ProjectionError("Snapshot de layout não reproduz o contrato original")
    return layout


def _constant_sql(value: Any, expected_type: str) -> str:
    if value is None:
        return f"CONVERT({expected_type},NULL)"
    if isinstance(value, bool):
        raw = "1" if value else "0"
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        raw = str(value)
    elif isinstance(value, str):
        raw = qs(value)
    else:
        raise ProjectionError("Constante Landing deve ser escalar JSON")
    return f"CONVERT({expected_type},{raw})"


def build_import_plan(
    layout: TableLayout,
    *,
    id_strategy: Mapping[str, Any] | None = None,
    metadata_mapping: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produz colunas/expressões do INSERT, omitindo IDENTITY/computed/default."""

    rules = {rule.column.casefold(): rule.value for rule in layout.fill_rules}
    insert_columns: list[str] = []
    expressions: list[str] = []
    id_strategy = id_strategy or {"strategy": "sequence"}
    metadata_mapping = metadata_mapping or {}
    source_names = {
        str(column.source_name).casefold(): str(column.source_name)
        for column in layout.business_columns
        if column.source_name is not None
    }

    for column in layout.columns:
        if not column.insertable:
            continue
        role = column.role
        expression: str
        if role == "technical":
            if column.identity:
                continue
            if layout.profile_name.casefold() != "bronze":
                raise ProjectionError("Coluna técnica não gerada no perfil")
            strategy = layout.technical_id_strategy or id_strategy.get("strategy", "sequence")
            if strategy == "sequence":
                if layout.sequence is None:
                    raise ProjectionError("Layout Bronze sem sequence")
                expression = f"NEXT VALUE FOR {qi(layout.schema)}.{qi(layout.sequence.name)}"
            elif strategy == "source_column":
                source = layout.technical_id_source_column or id_strategy.get("source_column")
                if not source:
                    raise ProjectionError("source_column não configurada")
                expression = "b." + qi(str(source))
            else:
                raise ProjectionError(f"Estratégia de ID desconhecida: {strategy}")
        elif role == "business":
            expression = "b." + qi(column.source_name or column.name)
        else:
            rule = rules.get(column.name.casefold(), "")
            if rule == "required_mapping":
                mapping = metadata_mapping.get(column.name)
                if not isinstance(mapping, Mapping):
                    raise ProjectionError(
                        f"Carga Landing exige metadata_mapping.{column.name}; "
                        "DDL pode ser gerado sem esse mapeamento."
                    )
                if "source_column" in mapping:
                    configured_source = str(mapping["source_column"])
                    canonical_source = source_names.get(configured_source.casefold())
                    if canonical_source is None:
                        raise ProjectionError(
                            f"metadata_mapping.{column.name}.source_column referencia coluna ausente: "
                            + configured_source
                        )
                    expression = "b." + qi(canonical_source)
                elif "constant" in mapping:
                    expected = {"aud_ccid": "BIGINT", "aud_cntrrn": "INT", "aud_enttyp": "VARCHAR(30)"}[column.name]
                    configured = str(mapping.get("sql_type", expected)).upper().replace(" ", "")
                    if configured != expected:
                        raise ProjectionError(
                            f"Tipo da constante {column.name} deve ser exatamente {expected}"
                        )
                    expression = _constant_sql(mapping["constant"], expected)
                else:
                    raise ProjectionError(f"Mapeamento inválido de {column.name}")
            elif rule in {"destination_computed", "destination_default"}:
                continue
            elif rule == "NULL":
                expression = "NULL"
            elif rule:
                expression = rule
            else:
                raise ProjectionError(f"Sem regra de preenchimento para {column.name}")
        insert_columns.append(column.name)
        expressions.append(expression)

    return {
        "target_schema": layout.schema,
        "target_table": layout.table,
        "insert_columns": insert_columns,
        "select_expressions": expressions,
    }
