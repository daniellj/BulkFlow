"""Geração idempotente de DDL e comparação estrutural do catálogo.

As funções deste módulo são puras: recebem um ``TableLayout`` e/ou um snapshot
agregado do catálogo e devolvem SQL/diagnósticos. A camada de conexão é quem
deve consultar o catálogo, recusar incompatibilidades e executar cada batch.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Any

from .profiles import (
    ColumnDefinition,
    IndexDefinition,
    IndexKey,
    PartitionDefinition,
    TableLayout,
)
from .util import collation_sql, qi, qs, target_lock_resource, type_sql


class IndexBuildMoment(str, Enum):
    BEFORE_LOAD = "before_load"
    AFTER_TABLE_LOAD = "after_table_load"


class DdlStage(str, Enum):
    FULL = "full_provisioning"
    INITIAL = "initial_structure"
    SECONDARY_INDEXES = "secondary_indexes"


class CatalogState(str, Enum):
    ABSENT = "ABSENT"
    COMPATIBLE = "COMPATIBLE"
    OBJECTS_PENDING = "OBJECTS_PENDING"
    INDEXES_PENDING = "INDEXES_PENDING"
    DATA_COMPLETE_INDEXES_PENDING = "DATA_COMPLETE_INDEXES_PENDING"
    SCHEMA_EVOLUTION_PENDING = "SCHEMA_EVOLUTION_PENDING"
    INCOMPATIBLE = "INCOMPATIBLE"
    CLUSTERED_CONFLICT = "CLUSTERED_CONFLICT"


@dataclass(frozen=True)
class DdlPlan:
    layout: TableLayout
    stage: DdlStage
    index_moment: IndexBuildMoment
    included_indexes: tuple[str, ...]
    batches: tuple[str, ...]
    ssms_script: str


@dataclass(frozen=True)
class CatalogComparison:
    state: CatalogState
    errors: tuple[str, ...] = ()
    missing_objects: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing_business_columns: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        return self.state is CatalogState.COMPATIBLE

    @property
    def provisionable(self) -> bool:
        return not self.errors and self.state in {
            CatalogState.ABSENT,
            CatalogState.OBJECTS_PENDING,
            CatalogState.INDEXES_PENDING,
            CatalogState.DATA_COMPLETE_INDEXES_PENDING,
            CatalogState.SCHEMA_EVOLUTION_PENDING,
        }


class SchemaEvolutionError(RuntimeError):
    """Base class for schema-evolution decisions that must stop a table."""


class SchemaEvolutionRequiredError(SchemaEvolutionError):
    """The destination lacks business columns and mutation is disabled."""


class SchemaEvolutionSafetyError(SchemaEvolutionError):
    """The requested additive change cannot be performed without backfill."""


@dataclass(frozen=True)
class SchemaEvolutionColumn:
    column: ColumnDefinition
    logical_ordinal: int
    physical_ordinal_before: int | None = None


@dataclass(frozen=True)
class SchemaEvolutionAnalysis:
    missing_columns: tuple[SchemaEvolutionColumn, ...] = ()
    unsafe_not_null_columns: tuple[str, ...] = ()
    row_count: int | None = None
    physical_order_differs: bool = False

    @property
    def required(self) -> bool:
        return bool(self.missing_columns)

    @property
    def safe(self) -> bool:
        return not self.unsafe_not_null_columns


SET_OPTIONS = """SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
SET XACT_ABORT ON;"""


def _qualified(schema: str, name: str) -> str:
    return f"{qi(schema)}.{qi(name)}"


def partition_boundary_values(
    partition: PartitionDefinition,
    *,
    reference_date: date | datetime | None = None,
) -> tuple[str, ...]:
    """Return deterministic monthly RANGE RIGHT boundaries for one DDL plan.

    The requested horizon is the first day of the current month through the
    first day of December in ``current year + future_years``.  A caller may
    inject ``reference_date`` for deterministic validation and tests.
    """

    if partition.interval != "month":
        raise ValueError(f"Intervalo de particionamento nao suportado: {partition.interval}")
    current = reference_date or date.today()
    if isinstance(current, datetime):
        current = current.date()
    year = current.year
    month = current.month
    final_year = current.year + partition.future_years
    values: list[str] = []
    while year < final_year or (year == final_year and month <= 12):
        values.append(f"{year:04d}-{month:02d}-01T00:00:00.0000000")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tuple(values)


def _column_sql(column: ColumnDefinition) -> str:
    if column.computed_expression is not None:
        suffix = " PERSISTED" if column.persisted else ""
        suffix += " NULL" if column.nullable else " NOT NULL"
        return f"{qi(column.name)} AS ({column.computed_expression}){suffix}"
    if column.sql_type is None:
        raise ValueError(f"Coluna {column.name} sem tipo SQL")
    parts = [qi(column.name), column.sql_type]
    if column.collation:
        parts.extend(("COLLATE", collation_sql(column.collation)))
    if column.identity:
        parts.append(f"IDENTITY({column.identity_seed},{column.identity_increment})")
    if column.default_expression is not None:
        if not column.default_name:
            raise ValueError(f"Default sem nome na coluna {column.name}")
        parts.extend(("CONSTRAINT", qi(column.default_name), "DEFAULT", column.default_expression))
    parts.append("NULL" if column.nullable else "NOT NULL")
    return " ".join(parts)


def _key_sql(keys: Sequence[IndexKey]) -> str:
    return ", ".join(f"{qi(key.name)} {key.direction}" for key in keys)


def _primary_key_sql(primary_key: IndexDefinition) -> str:
    organization = "CLUSTERED" if primary_key.clustered else "NONCLUSTERED"
    return (
        f"CONSTRAINT {qi(primary_key.name)} PRIMARY KEY {organization} "
        f"({_key_sql(primary_key.keys)})"
    )


def create_table_statement(layout: TableLayout) -> str:
    definitions = [_column_sql(column) for column in layout.columns]
    if layout.partition is None:
        definitions.append(_primary_key_sql(layout.primary_key))
    body = ",\n    ".join(definitions)
    placement = ""
    if layout.partition is not None:
        placement = (
            f" ON {qi(layout.partition.scheme_name)} "
            f"({qi(layout.partition.column)})"
        )
    return f"CREATE TABLE {layout.qualified_name} (\n    {body}\n){placement};"


def create_sequence_statement(layout: TableLayout) -> str | None:
    sequence = layout.sequence
    if sequence is None:
        return None
    cycle = "CYCLE" if sequence.cycle else "NO CYCLE"
    return (
        f"CREATE SEQUENCE {_qualified(layout.schema, sequence.name)} AS {sequence.sql_type} "
        f"START WITH {sequence.start} INCREMENT BY {sequence.increment} {cycle};"
    )


def create_index_statement(layout: TableLayout, index: IndexDefinition) -> str:
    placement = ""
    if layout.partition is not None:
        if index.clustered:
            placement = (
                f" ON {qi(layout.partition.scheme_name)} "
                f"({qi(layout.partition.column)})"
            )
        else:
            # The technical PK remains globally unique.  Keeping it and the
            # remaining nonclustered indexes nonaligned also prevents SQL
            # Server from implicitly adding the partitioning column.
            placement = f" ON {qi(layout.partition.filegroup)}"
    if index.primary_key:
        organization = "CLUSTERED" if index.clustered else "NONCLUSTERED"
        return (
            f"ALTER TABLE {layout.qualified_name} ADD CONSTRAINT {qi(index.name)} "
            f"PRIMARY KEY {organization} ({_key_sql(index.keys)}){placement};"
        )
    uniqueness = "UNIQUE " if index.unique else ""
    organization = "CLUSTERED" if index.clustered else "NONCLUSTERED"
    statement = (
        f"CREATE {uniqueness}{organization} INDEX {qi(index.name)} "
        f"ON {layout.qualified_name} ({_key_sql(index.keys)})"
    )
    if index.includes:
        statement += " INCLUDE (" + ", ".join(qi(name) for name in index.includes) + ")"
    if index.filter_predicate:
        statement += " WHERE " + index.filter_predicate
    return statement + placement + ";"


def analyze_schema_evolution(
    layout: TableLayout,
    catalog: Mapping[str, Any] | None,
) -> SchemaEvolutionAnalysis:
    """Describe the additive business-column delta without mutating SQL Server.

    ``row_count`` is useful for an early, fail-closed diagnostic.  The generated
    SQL repeats the populated-table proof under an exclusive transaction lock;
    catalog metadata alone is never the final safety decision.
    """

    if not catalog or catalog.get("exists", True) is False:
        return SchemaEvolutionAnalysis(row_count=None)
    actual_columns = tuple(catalog.get("columns", ()))
    actual_names = tuple(str(item.get("name", "")) for item in actual_columns)
    # SQL Server may use a case-sensitive catalog collation, while the source
    # contract explicitly treats source identifiers case-insensitively.  Fold
    # only for matching; ``compare_catalog`` separately enforces canonical
    # lowercase spelling on the destination.
    actual_by_name = {
        name.casefold(): item for name, item in zip(actual_names, actual_columns)
    }
    missing = tuple(
        SchemaEvolutionColumn(
            column=replace(column, name=column.name.casefold()),
            logical_ordinal=ordinal,
        )
        for ordinal, column in enumerate(layout.columns, start=1)
        if column.role == "business"
        and column.name.casefold() not in actual_by_name
    )
    row_count_value = catalog.get("row_count")
    row_count = None if row_count_value is None else int(row_count_value)
    populated_or_unknown = row_count is None or row_count > 0
    unsafe = tuple(
        item.column.name
        for item in missing
        if not item.column.nullable and populated_or_unknown
    )
    expected_present = tuple(
        column.name.casefold()
        for column in layout.columns
        if column.name.casefold() in actual_by_name
    )
    return SchemaEvolutionAnalysis(
        missing_columns=missing,
        unsafe_not_null_columns=unsafe,
        row_count=row_count,
        physical_order_differs=(
            tuple(name.casefold() for name in actual_names) != expected_present
        ),
    )


def _schema_guard(layout: TableLayout) -> str:
    create = f"CREATE SCHEMA {qi(layout.schema)};"
    return f"""IF EXISTS (
       SELECT 1
       FROM sys.schemas AS s
       WHERE LOWER(s.name) = {qs(layout.schema.casefold())}
         AND CONVERT(varbinary(256), s.name) <> CONVERT(varbinary(256), {qs(layout.schema)})
   )
    THROW 51031, 'Schema existente diverge do nome lowercase requerido.', 1;
IF SCHEMA_ID({qs(layout.schema)}) IS NOT NULL
   AND NOT EXISTS (
       SELECT 1
       FROM sys.schemas AS s
       WHERE s.schema_id = SCHEMA_ID({qs(layout.schema)})
         AND CONVERT(varbinary(256), s.name) = CONVERT(varbinary(256), {qs(layout.schema)})
   )
    THROW 51031, 'Schema existente diverge do nome lowercase requerido.', 1;
IF SCHEMA_ID({qs(layout.schema)}) IS NULL EXEC({qs(create)});"""


def _partition_guard(
    layout: TableLayout,
    *,
    allow_create: bool,
    reference_date: date | datetime | None = None,
) -> str | None:
    partition = layout.partition
    if partition is None:
        return None

    boundaries = partition_boundary_values(partition, reference_date=reference_date)
    values_sql = ",\n        ".join(qs(value) for value in boundaries)
    create_function = (
        f"CREATE PARTITION FUNCTION {qi(partition.function_name)} "
        f"({partition.sql_type}) AS RANGE {partition.range_direction} FOR VALUES (\n"
        f"        {values_sql}\n    );"
    )
    create_scheme = (
        f"CREATE PARTITION SCHEME {qi(partition.scheme_name)} AS PARTITION "
        f"{qi(partition.function_name)} ALL TO ({qi(partition.filegroup)});"
    )
    if allow_create:
        ensure_function = f"""IF NOT EXISTS (
    SELECT 1 FROM sys.partition_functions WHERE name = {qs(partition.function_name)}
)
    EXEC({qs(create_function)});"""
        ensure_scheme = f"""IF NOT EXISTS (
    SELECT 1 FROM sys.partition_schemes WHERE name = {qs(partition.scheme_name)}
)
    EXEC({qs(create_scheme)});"""
    else:
        ensure_function = f"""IF NOT EXISTS (
    SELECT 1 FROM sys.partition_functions WHERE name = {qs(partition.function_name)}
)
    THROW 51040, 'Partition function requerida esta ausente.', 1;"""
        ensure_scheme = f"""IF NOT EXISTS (
    SELECT 1 FROM sys.partition_schemes WHERE name = {qs(partition.scheme_name)}
)
    THROW 51041, 'Partition scheme requerido esta ausente.', 1;"""

    extensions: list[str] = []
    for boundary in boundaries:
        split = (
            f"ALTER PARTITION SCHEME {qi(partition.scheme_name)} "
            f"NEXT USED {qi(partition.filegroup)}; "
            f"ALTER PARTITION FUNCTION {qi(partition.function_name)}() "
            f"SPLIT RANGE ({qs(boundary)});"
        )
        extensions.append(
            f"""IF NOT EXISTS (
    SELECT 1
    FROM sys.partition_range_values AS prv
    JOIN sys.partition_functions AS pf ON pf.function_id = prv.function_id
    WHERE pf.name = {qs(partition.function_name)}
      AND CONVERT(datetime2(7), prv.value) = CONVERT(datetime2(7), {qs(boundary)})
)
BEGIN
    IF EXISTS (
        SELECT 1
        FROM sys.partition_range_values AS prv
        JOIN sys.partition_functions AS pf ON pf.function_id = prv.function_id
        WHERE pf.name = {qs(partition.function_name)}
          AND CONVERT(datetime2(7), prv.value) > CONVERT(datetime2(7), {qs(boundary)})
    )
        THROW 51045, 'Partition function possui lacuna mensal interna; extensao automatica recusada.', 1;
    EXEC({qs(split)});
END;"""
        )

    return f"""IF EXISTS (
    SELECT 1
    FROM sys.partition_functions
    WHERE LOWER(name) = {qs(partition.function_name.casefold())}
      AND CONVERT(varbinary(256), name) <> CONVERT(varbinary(256), {qs(partition.function_name)})
)
    THROW 51040, 'Nome da partition function diverge do lowercase requerido.', 1;
{ensure_function}
IF NOT EXISTS (
    SELECT 1
    FROM sys.partition_functions AS pf
    JOIN sys.partition_parameters AS pp ON pp.function_id = pf.function_id
    JOIN sys.types AS typ ON typ.user_type_id = pp.user_type_id
    WHERE pf.name = {qs(partition.function_name)}
      AND pf.boundary_value_on_right = 1
      AND typ.name = N'datetime2'
      AND pp.scale = 7
      AND (SELECT COUNT_BIG(*) FROM sys.partition_parameters AS ppc
           WHERE ppc.function_id = pf.function_id) = 1
)
    THROW 51042, 'Partition function diverge em tipo DATETIME2(7) ou RANGE RIGHT.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.partition_range_values AS prv
    JOIN sys.partition_functions AS pf ON pf.function_id = prv.function_id
    WHERE pf.name = {qs(partition.function_name)}
      AND (
          TRY_CONVERT(datetime2(7), prv.value) IS NULL
          OR DAY(CONVERT(datetime2(7), prv.value)) <> 1
          OR CONVERT(time(7), CONVERT(datetime2(7), prv.value)) <> CONVERT(time(7), '00:00:00')
      )
)
    THROW 51043, 'Partition function contem limite que nao representa o inicio de um mes.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.partition_schemes
    WHERE LOWER(name) = {qs(partition.scheme_name.casefold())}
      AND CONVERT(varbinary(256), name) <> CONVERT(varbinary(256), {qs(partition.scheme_name)})
)
    THROW 51041, 'Nome do partition scheme diverge do lowercase requerido.', 1;
{ensure_scheme}
IF NOT EXISTS (
    SELECT 1
    FROM sys.partition_schemes AS ps
    JOIN sys.partition_functions AS pf ON pf.function_id = ps.function_id
    WHERE ps.name = {qs(partition.scheme_name)}
      AND pf.name = {qs(partition.function_name)}
)
    THROW 51044, 'Partition scheme nao referencia a partition function requerida.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.partition_schemes AS ps
    JOIN sys.partition_functions AS pf ON pf.function_id = ps.function_id
    WHERE pf.name = {qs(partition.function_name)}
      AND ps.name <> {qs(partition.scheme_name)}
)
    THROW 51044, 'Partition function compartilhada por scheme externo; extensao automatica recusada.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.partition_schemes AS ps
    JOIN sys.destination_data_spaces AS dds
      ON dds.partition_scheme_id = ps.data_space_id
    JOIN sys.filegroups AS fg ON fg.data_space_id = dds.data_space_id
    WHERE ps.name = {qs(partition.scheme_name)}
      AND fg.name <> {qs(partition.filegroup)}
)
    THROW 51044, 'Partition scheme deve mapear todas as particoes para PRIMARY.', 1;
{' '.join(extensions)}"""


def _sequence_validation_guard(layout: TableLayout) -> str | None:
    """Validate every sequence attribute represented by ``SequenceDefinition``."""

    sequence = layout.sequence
    if sequence is None:
        return None
    name = _qualified(layout.schema, sequence.name)
    expected_cycle = 1 if sequence.cycle else 0
    type_sample = f"CAST(CAST(0 AS {sequence.sql_type}) AS sql_variant)"
    return f"""IF EXISTS (
    SELECT 1
    FROM sys.objects AS obj
    JOIN sys.schemas AS sch ON sch.schema_id = obj.schema_id
    WHERE LOWER(sch.name) = {qs(layout.schema.casefold())}
      AND LOWER(obj.name) = {qs(sequence.name.casefold())}
      AND (
        obj.type <> N'SO'
        OR CONVERT(varbinary(256), sch.name) <> CONVERT(varbinary(256), {qs(layout.schema)})
        OR CONVERT(varbinary(256), obj.name) <> CONVERT(varbinary(256), {qs(sequence.name)})
      )
)
    THROW 51021, 'Existe objeto incompativel com o nome da sequence requerida.', 1;
IF OBJECT_ID({qs(name)}, N'SO') IS NULL
    THROW 51032, 'Sequence requerida esta ausente.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.sequences AS seq
    JOIN sys.schemas AS sch ON sch.schema_id = seq.schema_id
    JOIN sys.types AS typ ON typ.user_type_id = seq.user_type_id
    WHERE seq.object_id = OBJECT_ID({qs(name)}, N'SO')
      AND (
        CONVERT(varbinary(256), sch.name) <> CONVERT(varbinary(256), {qs(layout.schema)})
        OR CONVERT(varbinary(256), seq.name) <> CONVERT(varbinary(256), {qs(sequence.name)})
        OR CONVERT(varbinary(256), typ.name) <>
           CONVERT(varbinary(256), CONVERT(nvarchar(128), SQL_VARIANT_PROPERTY({type_sample}, 'BaseType')))
        OR CONVERT(int, seq.precision) <>
           CONVERT(int, SQL_VARIANT_PROPERTY({type_sample}, 'Precision'))
        OR CONVERT(int, seq.scale) <>
           CONVERT(int, SQL_VARIANT_PROPERTY({type_sample}, 'Scale'))
        OR CONVERT(decimal(38,0), seq.start_value) <> {sequence.start}
        OR CONVERT(decimal(38,0), seq.increment) <> {sequence.increment}
        OR CONVERT(int, seq.is_cycling) <> {expected_cycle}
      )
)
    THROW 51027, 'Sequence existente diverge em nome, tipo, inicio, incremento ou ciclo.', 1;"""


def _sequence_guard(layout: TableLayout) -> str | None:
    statement = create_sequence_statement(layout)
    if statement is None or layout.sequence is None:
        return None
    name = _qualified(layout.schema, layout.sequence.name)
    structure_guard = _sequence_validation_guard(layout)
    probe = (
        f"IF EXISTS (SELECT TOP (1) 1 FROM {layout.qualified_name}) "
        "SET @has_rows = 1;"
    )
    return f"""IF EXISTS (
    SELECT 1
    FROM sys.objects AS obj
    JOIN sys.schemas AS sch ON sch.schema_id = obj.schema_id
    WHERE LOWER(sch.name) = {qs(layout.schema.casefold())}
      AND LOWER(obj.name) = {qs(layout.sequence.name.casefold())}
      AND (
        obj.type <> N'SO'
        OR CONVERT(varbinary(256), sch.name) <> CONVERT(varbinary(256), {qs(layout.schema)})
        OR CONVERT(varbinary(256), obj.name) <> CONVERT(varbinary(256), {qs(layout.sequence.name)})
      )
)
    THROW 51021, 'Existe objeto incompativel com o nome da sequence requerida.', 1;
IF OBJECT_ID({qs(name)}) IS NOT NULL AND OBJECT_ID({qs(name)}, N'SO') IS NULL
    THROW 51021, 'Existe objeto incompatível com o nome da sequence requerida.', 1;
IF OBJECT_ID({qs(name)}, N'SO') IS NULL
BEGIN
    DECLARE @bcp_sequence_has_rows bit = 0;
    EXEC sys.sp_executesql {qs(probe)}, N'@has_rows bit OUTPUT', @bcp_sequence_has_rows OUTPUT;
    IF @bcp_sequence_has_rows = 1
        THROW 51025, 'Sequence ausente em tabela povoada; execute migração explícita.', 1;
    EXEC({qs(statement)});
END;
{structure_guard}"""


def _table_guard(layout: TableLayout) -> str:
    name = layout.qualified_name
    statement = create_table_statement(layout)
    return f"""IF EXISTS (
    SELECT 1
    FROM sys.objects AS obj
    JOIN sys.schemas AS sch ON sch.schema_id = obj.schema_id
    WHERE LOWER(sch.name) = {qs(layout.schema.casefold())}
      AND LOWER(obj.name) = {qs(layout.table.casefold())}
      AND (
        obj.type <> N'U'
        OR CONVERT(varbinary(256), sch.name) <> CONVERT(varbinary(256), {qs(layout.schema)})
        OR CONVERT(varbinary(256), obj.name) <> CONVERT(varbinary(256), {qs(layout.table)})
      )
)
    THROW 51022, 'Existe objeto incompativel com o nome da tabela requerida.', 1;
IF OBJECT_ID({qs(name)}) IS NOT NULL AND OBJECT_ID({qs(name)}, N'U') IS NULL
    THROW 51022, 'Existe objeto incompatível com o nome da tabela requerida.', 1;
IF OBJECT_ID({qs(name)}, N'U') IS NULL EXEC({qs(statement)});"""


def _table_partition_state_guard(layout: TableLayout) -> str:
    """Reject implicit repartitioning of any pre-existing destination table."""

    if layout.partition is None:
        return f"""IF OBJECT_ID({qs(layout.qualified_name)}, N'U') IS NOT NULL
   AND EXISTS (
       SELECT 1
       FROM sys.indexes AS i
       JOIN sys.partition_schemes AS ps ON ps.data_space_id = i.data_space_id
       WHERE i.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
         AND i.type IN (0, 1)
   )
    THROW 51050, 'Tabela existente e particionada, mas o contrato nao solicita particionamento.', 1;"""
    partition = layout.partition
    return f"""IF OBJECT_ID({qs(layout.qualified_name)}, N'U') IS NOT NULL
   AND NOT EXISTS (
       SELECT 1
       FROM sys.indexes AS i
       JOIN sys.partition_schemes AS ps ON ps.data_space_id = i.data_space_id
       JOIN sys.index_columns AS ic
         ON ic.object_id = i.object_id
        AND ic.index_id = i.index_id
        AND ic.partition_ordinal = 1
       JOIN sys.columns AS c
         ON c.object_id = ic.object_id AND c.column_id = ic.column_id
       WHERE i.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
         AND i.type IN (0, 1)
         AND ps.name = {qs(partition.scheme_name)}
         AND CONVERT(varbinary(256), c.name) =
             CONVERT(varbinary(256), {qs(partition.column)})
   )
    THROW 51050, 'Tabela existente exige migracao explicita para o particionamento requerido.', 1;"""


def _supports_collation(sql_type: str | None) -> bool:
    if sql_type is None:
        return False
    base = re.split(r"[\s(]", sql_type.strip(), maxsplit=1)[0].casefold()
    return base in {"char", "varchar", "text", "nchar", "nvarchar", "ntext"}


def _shadow_column_sql(column: ColumnDefinition) -> str:
    """Render one column for SQL Server's temporary canonical reference."""

    if column.computed_expression is not None:
        suffix = " PERSISTED" if column.persisted else ""
        suffix += " NULL" if column.nullable else " NOT NULL"
        return f"{qi(column.name)} AS ({column.computed_expression}){suffix}"
    if column.sql_type is None:
        raise ValueError(f"Coluna {column.name} sem tipo SQL")
    parts = [qi(column.name), column.sql_type]
    if column.collation:
        parts.extend(("COLLATE", collation_sql(column.collation)))
    elif _supports_collation(column.sql_type):
        # Local temp tables otherwise inherit tempdb's collation.
        parts.extend(("COLLATE", "DATABASE_DEFAULT"))
    if column.identity:
        parts.append(f"IDENTITY({column.identity_seed},{column.identity_increment})")
    if column.default_expression is not None:
        # The definition is compared through catalog metadata; the real,
        # contract-owned constraint name is checked separately.
        parts.extend(("DEFAULT", column.default_expression))
    parts.append("NULL" if column.nullable else "NOT NULL")
    return " ".join(parts)


def _shadow_index_statement(
    index: IndexDefinition,
    shadow_name: str,
    shadow_table: str,
) -> str:
    uniqueness = "UNIQUE " if index.unique else ""
    organization = "CLUSTERED" if index.clustered else "NONCLUSTERED"
    statement = (
        f"CREATE {uniqueness}{organization} INDEX {qi(shadow_name)} "
        f"ON {shadow_table} ({_key_sql(index.keys)})"
    )
    if index.includes:
        statement += " INCLUDE (" + ", ".join(qi(name) for name in index.includes) + ")"
    if index.filter_predicate:
        statement += " WHERE " + index.filter_predicate
    return statement + ";"


def _index_contract_sql(
    layout: TableLayout,
    index: IndexDefinition,
    *,
    shadow_table: str,
    shadow_index_name: str | None,
) -> str:
    actual_name = qs(index.name)
    expected_lookup = (
        "ei.is_primary_key = 1"
        if index.primary_key
        else f"ei.name = {qs(str(shadow_index_name))}"
    )
    placement_guard = ""
    if layout.partition is not None:
        if index.clustered:
            expected_space = layout.partition.scheme_name
            expected_partition_column = layout.partition.column
            placement_guard = f"""
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes AS ai
    JOIN sys.data_spaces AS ds ON ds.data_space_id = ai.data_space_id
    JOIN sys.index_columns AS aic
      ON aic.object_id = ai.object_id
     AND aic.index_id = ai.index_id
     AND aic.partition_ordinal = 1
    JOIN sys.columns AS ac
      ON ac.object_id = aic.object_id AND ac.column_id = aic.column_id
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
      AND ds.name = {qs(expected_space)}
      AND CONVERT(varbinary(256), ac.name) =
          CONVERT(varbinary(256), {qs(expected_partition_column)})
)
    THROW 51047, 'Indice clustered nao esta alinhado ao partition scheme/coluna requeridos.', 1;"""
        else:
            placement_guard = f"""
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes AS ai
    JOIN sys.data_spaces AS ds ON ds.data_space_id = ai.data_space_id
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
      AND ds.name = {qs(layout.partition.filegroup)}
      AND NOT EXISTS (
          SELECT 1 FROM sys.index_columns AS aic
          WHERE aic.object_id = ai.object_id
            AND aic.index_id = ai.index_id
            AND aic.partition_ordinal > 0
      )
)
    THROW 51048, 'Indice nonclustered deve permanecer nao alinhado em PRIMARY.', 1;"""
    contract = f"""IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes AS ai
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
)
    THROW 51034, 'Indice ou chave primaria requerida esta ausente.', 1;
IF EXISTS (
    SELECT 1
    FROM sys.indexes AS ai
    CROSS JOIN tempdb.sys.indexes AS ei
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
      AND ei.object_id = OBJECT_ID(N'tempdb..{shadow_table}')
      AND {expected_lookup}
      AND (
        CONVERT(varbinary(256), ai.name) <> CONVERT(varbinary(256), {actual_name})
        OR ai.type <> ei.type
        OR ai.is_unique <> ei.is_unique
        OR ai.is_primary_key <> ei.is_primary_key
        OR ai.is_unique_constraint <> ei.is_unique_constraint
        OR ai.is_disabled <> ei.is_disabled
        OR ai.is_hypothetical <> ei.is_hypothetical
        OR ai.has_filter <> ei.has_filter
        OR ISNULL(CONVERT(varbinary(max), ai.filter_definition), 0x) <>
           ISNULL(CONVERT(varbinary(max), ei.filter_definition), 0x)
      )
)
    THROW 51035, 'Indice ou chave primaria existente diverge do contrato fisico.', 1;
IF EXISTS (
    SELECT
        aic.key_ordinal,
        aic.is_descending_key,
        aic.is_included_column,
        CONVERT(varbinary(256), ac.name) AS column_name
    FROM sys.indexes AS ai
    JOIN sys.index_columns AS aic
      ON aic.object_id = ai.object_id AND aic.index_id = ai.index_id
    JOIN sys.columns AS ac
      ON ac.object_id = aic.object_id AND ac.column_id = aic.column_id
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
    EXCEPT
    SELECT
        eic.key_ordinal,
        eic.is_descending_key,
        eic.is_included_column,
        CONVERT(varbinary(256), ec.name) AS column_name
    FROM tempdb.sys.indexes AS ei
    JOIN tempdb.sys.index_columns AS eic
      ON eic.object_id = ei.object_id AND eic.index_id = ei.index_id
    JOIN tempdb.sys.columns AS ec
      ON ec.object_id = eic.object_id AND ec.column_id = eic.column_id
    WHERE ei.object_id = OBJECT_ID(N'tempdb..{shadow_table}')
      AND {expected_lookup}
)
OR EXISTS (
    SELECT
        eic.key_ordinal,
        eic.is_descending_key,
        eic.is_included_column,
        CONVERT(varbinary(256), ec.name) AS column_name
    FROM tempdb.sys.indexes AS ei
    JOIN tempdb.sys.index_columns AS eic
      ON eic.object_id = ei.object_id AND eic.index_id = ei.index_id
    JOIN tempdb.sys.columns AS ec
      ON ec.object_id = eic.object_id AND ec.column_id = eic.column_id
    WHERE ei.object_id = OBJECT_ID(N'tempdb..{shadow_table}')
      AND {expected_lookup}
    EXCEPT
    SELECT
        aic.key_ordinal,
        aic.is_descending_key,
        aic.is_included_column,
        CONVERT(varbinary(256), ac.name) AS column_name
    FROM sys.indexes AS ai
    JOIN sys.index_columns AS aic
      ON aic.object_id = ai.object_id AND aic.index_id = ai.index_id
    JOIN sys.columns AS ac
      ON ac.object_id = aic.object_id AND ac.column_id = aic.column_id
    WHERE ai.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND ai.name = {actual_name}
)
    THROW 51036, 'Colunas, ordem ou direcao de indice existente divergem do contrato.', 1;"""
    return contract + placement_guard


def _table_contract_guard(
    layout: TableLayout,
    indexes: Sequence[IndexDefinition],
) -> str:
    """Embed a fail-closed catalog proof in both saved and applied DDL."""

    shadow_table = "#bcp_expected_layout"
    definitions = [_shadow_column_sql(column) for column in layout.columns]
    primary = layout.primary_key
    organization = "CLUSTERED" if primary.clustered else "NONCLUSTERED"
    definitions.append(f"PRIMARY KEY {organization} ({_key_sql(primary.keys)})")
    shadow_create = (
        f"CREATE TABLE {shadow_table} (\n        "
        + ",\n        ".join(definitions)
        + "\n    );"
    )

    shadow_indexes: list[str] = []
    contracts: list[str] = [
        _index_contract_sql(
            layout,
            primary,
            shadow_table=shadow_table,
            shadow_index_name=None,
        )
    ]
    for ordinal, index in enumerate(indexes, start=1):
        shadow_name = f"bcp_expected_index_{ordinal:03d}"
        shadow_indexes.append(_shadow_index_statement(index, shadow_name, shadow_table))
        contracts.append(
            _index_contract_sql(
                layout,
                index,
                shadow_table=shadow_table,
                shadow_index_name=shadow_name,
            )
        )

    default_rows = ",\n        ".join(
        "("
        + qs(column.name)
        + ", "
        + (
            qs(column.default_name)
            if column.default_name is not None
            else "CAST(NULL AS sysname)"
        )
        + ")"
        for column in layout.columns
    )
    dynamic_sql = f"""{shadow_create}
    {' '.join(shadow_indexes)}
    IF NOT EXISTS (
        SELECT 1
        FROM sys.tables AS t
        JOIN sys.schemas AS s ON s.schema_id = t.schema_id
        WHERE t.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
          AND CONVERT(varbinary(256), s.name) = CONVERT(varbinary(256), {qs(layout.schema)})
          AND CONVERT(varbinary(256), t.name) = CONVERT(varbinary(256), {qs(layout.table)})
    )
        THROW 51033, 'Tabela existente diverge dos nomes lowercase requeridos.', 1;
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT * FROM sys.columns
            WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
        ) AS ac
        FULL OUTER JOIN (
            SELECT * FROM tempdb.sys.columns
            WHERE object_id = OBJECT_ID(N'tempdb..{shadow_table}')
        ) AS ec
          ON ac.name = ec.name COLLATE DATABASE_DEFAULT
        LEFT JOIN sys.types AS atp ON atp.user_type_id = ac.user_type_id
        LEFT JOIN tempdb.sys.types AS etp ON etp.user_type_id = ec.user_type_id
        LEFT JOIN sys.identity_columns AS aic
          ON aic.object_id = ac.object_id AND aic.column_id = ac.column_id
        LEFT JOIN tempdb.sys.identity_columns AS eic
          ON eic.object_id = ec.object_id AND eic.column_id = ec.column_id
        LEFT JOIN sys.computed_columns AS acc
          ON acc.object_id = ac.object_id AND acc.column_id = ac.column_id
        LEFT JOIN tempdb.sys.computed_columns AS ecc
          ON ecc.object_id = ec.object_id AND ecc.column_id = ec.column_id
        LEFT JOIN sys.default_constraints AS adc ON adc.object_id = ac.default_object_id
        LEFT JOIN tempdb.sys.default_constraints AS edc ON edc.object_id = ec.default_object_id
        LEFT JOIN (VALUES
        {default_rows}
        ) AS expected_default(column_name, default_name)
          ON expected_default.column_name = ec.name COLLATE DATABASE_DEFAULT
        WHERE ac.column_id IS NULL
           OR ec.column_id IS NULL
           OR CONVERT(varbinary(256), ac.name) <> CONVERT(varbinary(256), ec.name)
           OR CONVERT(varbinary(256), atp.name) <> CONVERT(varbinary(256), etp.name)
           OR ac.max_length <> ec.max_length
           OR ac.precision <> ec.precision
           OR ac.scale <> ec.scale
           OR ac.is_nullable <> ec.is_nullable
           OR ac.is_ansi_padded <> ec.is_ansi_padded
           OR ac.is_identity <> ec.is_identity
           OR ac.is_computed <> ec.is_computed
           OR ac.is_rowguidcol <> ec.is_rowguidcol
           OR ac.is_filestream <> ec.is_filestream
           OR ac.is_sparse <> ec.is_sparse
           OR ac.is_column_set <> ec.is_column_set
           OR ac.generated_always_type <> ec.generated_always_type
           OR ISNULL(CONVERT(varbinary(256), ac.collation_name), 0x) <>
              ISNULL(CONVERT(varbinary(256), ec.collation_name), 0x)
           OR ISNULL(CONVERT(decimal(38,0), aic.seed_value), 0) <>
              ISNULL(CONVERT(decimal(38,0), eic.seed_value), 0)
           OR ISNULL(CONVERT(decimal(38,0), aic.increment_value), 0) <>
              ISNULL(CONVERT(decimal(38,0), eic.increment_value), 0)
           OR ISNULL(CONVERT(varbinary(max), acc.definition), 0x) <>
              ISNULL(CONVERT(varbinary(max), ecc.definition), 0x)
           OR ISNULL(acc.is_persisted, 0) <> ISNULL(ecc.is_persisted, 0)
           OR ISNULL(CONVERT(varbinary(max), adc.definition), 0x) <>
              ISNULL(CONVERT(varbinary(max), edc.definition), 0x)
           OR ISNULL(CONVERT(varbinary(256), adc.name), 0x) <>
              ISNULL(CONVERT(varbinary(256), expected_default.default_name), 0x)
    )
        THROW 51037, 'Colunas existentes divergem em nome, tipo, nulabilidade, identity, computed, default ou collation.', 1;
    {' '.join(contracts)}"""
    # sp_executesql owns the local temporary table scope, so no DROP is needed
    # and a second execution on the same connection remains idempotent.
    return f"EXEC sys.sp_executesql {qs(dynamic_sql)};"


def _clustered_conflict_guard(layout: TableLayout, index: IndexDefinition) -> str:
    if not index.clustered:
        return ""
    return f"""IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND type = 1 AND name <> {qs(index.name)}
)
    THROW 51023, 'Outro índice clustered já existe; nenhuma substituição foi executada.', 1;
"""


def _primary_key_guard(layout: TableLayout) -> str:
    primary_key = layout.primary_key
    statement = create_index_statement(layout, primary_key)
    return f"""IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND LOWER(name) = {qs(primary_key.name.casefold())}
      AND CONVERT(varbinary(256), name) <> CONVERT(varbinary(256), {qs(primary_key.name)})
)
    THROW 51038, 'Nome de indice ou constraint diverge do lowercase requerido.', 1;
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U') AND is_primary_key = 1
)
BEGIN
    {_clustered_conflict_guard(layout, primary_key).rstrip()}
    EXEC({qs(statement)});
END;"""


def _index_guard(layout: TableLayout, index: IndexDefinition) -> str:
    statement = create_index_statement(layout, index)
    return f"""IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND LOWER(name) = {qs(index.name.casefold())}
      AND CONVERT(varbinary(256), name) <> CONVERT(varbinary(256), {qs(index.name)})
)
    THROW 51038, 'Nome de indice ou constraint diverge do lowercase requerido.', 1;
IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U')
      AND name = {qs(index.name)} AND is_hypothetical = 1
)
    THROW 51026, 'Indice hipotetico nao satisfaz o contrato fisico requerido.', 1;
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U') AND name = {qs(index.name)}
)
BEGIN
    {_clustered_conflict_guard(layout, index).rstrip()}
    EXEC({qs(statement)});
END;"""


def _missing_column_predicate(layout: TableLayout, column: ColumnDefinition) -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM sys.columns AS c "
        f"WHERE c.object_id = OBJECT_ID({qs(layout.qualified_name)}, N'U') "
        f"AND LOWER(c.name) = {qs(column.name.casefold())})"
    )


def _schema_evolution_guard(layout: TableLayout, *, allow_schema_evolution: bool) -> str:
    """Return an idempotent additive guard for an existing destination table."""

    business = layout.business_columns
    non_business = tuple(column for column in layout.columns if column.role != "business")
    missing_non_business = " OR ".join(
        _missing_column_predicate(layout, column) for column in non_business
    ) or "1=0"
    missing_business = " OR ".join(
        _missing_column_predicate(layout, column) for column in business
    ) or "1=0"
    object_exists = f"OBJECT_ID({qs(layout.qualified_name)}, N'U') IS NOT NULL"
    required_guard = f"""IF {object_exists} AND ({missing_non_business})
    THROW 51029, 'Layout existente nao possui coluna tecnica ou de metadados requerida; evolucao automatica recusada.', 1;"""
    if not allow_schema_evolution:
        return required_guard + f"""
IF {object_exists} AND ({missing_business})
    THROW 51028, 'Evolucao de schema pendente: allow_schema_evolution esta desabilitado.', 1;"""

    non_nullable_missing = " OR ".join(
        _missing_column_predicate(layout, column)
        for column in business
        if not column.nullable
    ) or "1=0"
    probe = (
        f"IF EXISTS (SELECT TOP (1) 1 FROM {layout.qualified_name} "
        "WITH (TABLOCKX,HOLDLOCK)) SET @has_rows = 1;"
    )
    additions: list[str] = []
    for column in business:
        target_column = replace(column, name=column.name.casefold())
        statement = f"ALTER TABLE {layout.qualified_name} ADD {_column_sql(target_column)};"
        additions.append(
            f"IF {_missing_column_predicate(layout, column)}\n"
            f"        EXEC({qs(statement)});"
        )
    indented_additions = "\n    ".join(additions)
    return required_guard + f"""
IF {object_exists}
BEGIN
    IF ({non_nullable_missing})
    BEGIN
        DECLARE @bcp_schema_has_rows bit = 0;
        EXEC sys.sp_executesql {qs(probe)}, N'@has_rows bit OUTPUT', @bcp_schema_has_rows OUTPUT;
        IF @bcp_schema_has_rows = 1
            THROW 51030, 'Evolucao de schema recusada: coluna NOT NULL ausente em tabela povoada e nao ha default/backfill autorizado.', 1;
    END;
    {indented_additions}
END;"""


def _coerce_moment(value: IndexBuildMoment | str) -> IndexBuildMoment:
    if isinstance(value, IndexBuildMoment):
        return value
    try:
        return IndexBuildMoment(value)
    except ValueError as exc:
        raise ValueError(
            "secondary_index_timing deve ser before_load ou after_table_load"
        ) from exc


def _coerce_stage(value: DdlStage | str) -> DdlStage:
    if isinstance(value, DdlStage):
        return value
    try:
        return DdlStage(value)
    except ValueError as exc:
        raise ValueError(f"Estágio DDL inválido: {value}") from exc


def _selected_indexes(
    layout: TableLayout,
    stage: DdlStage,
    moment: IndexBuildMoment,
) -> tuple[IndexDefinition, ...]:
    if stage is DdlStage.FULL:
        return layout.indexes
    if stage is DdlStage.SECONDARY_INDEXES:
        return layout.secondary_indexes
    if moment is IndexBuildMoment.BEFORE_LOAD:
        return layout.indexes
    return layout.base_indexes


def _transaction_batch(
    layout: TableLayout,
    stage: DdlStage,
    indexes: Sequence[IndexDefinition],
    *,
    allow_schema_evolution: bool,
    partition_reference_date: date | datetime | None = None,
) -> str:
    resource = target_lock_resource(layout.schema, layout.table)
    statements: list[str] = []
    partition_guard = _partition_guard(
        layout,
        allow_create=stage is not DdlStage.SECONDARY_INDEXES,
        reference_date=partition_reference_date,
    )
    if partition_guard:
        statements.append(partition_guard)
    statements.append(_table_partition_state_guard(layout))
    if stage is not DdlStage.SECONDARY_INDEXES:
        # This guard runs before any other mutation.  For a new table it is a
        # no-op; for an existing table it either adds only business columns or
        # stops the entire transaction before sequence/index provisioning.
        statements.append(
            _schema_evolution_guard(
                layout,
                allow_schema_evolution=allow_schema_evolution,
            )
        )
        statements.append(_schema_guard(layout))
        # A tabela é criada primeiro para que a proteção da sequence consiga
        # verificar com segurança se um objeto já existente contém dados.
        statements.append(_table_guard(layout))
        sequence_guard = _sequence_guard(layout)
        if sequence_guard:
            statements.append(sequence_guard)
        # A tabela nova já nasce com PK; este guard trata uma tabela existente
        # sem PK sem vincular a verificação à criação da tabela.
        statements.append(_primary_key_guard(layout))
    else:
        statements.append(
            f"IF OBJECT_ID({qs(layout.qualified_name)}, N'U') IS NULL "
            "THROW 51024, 'Tabela ausente para criação dos índices secundários.', 1;"
        )
        sequence_guard = _sequence_validation_guard(layout)
        if sequence_guard:
            statements.append(sequence_guard)
    statements.extend(_index_guard(layout, index) for index in indexes)
    contract_indexes: list[IndexDefinition] = list(layout.base_indexes)
    contract_names = {index.name.casefold() for index in contract_indexes}
    for index in indexes:
        if index.name.casefold() not in contract_names:
            contract_indexes.append(index)
            contract_names.add(index.name.casefold())
    statements.append(_table_contract_guard(layout, tuple(contract_indexes)))
    indented = "\n".join("    " + line.replace("\n", "\n    ") for line in statements)
    partition_lock = ""
    if layout.partition is not None:
        partition_resource = target_lock_resource(
            "partition", layout.partition.function_name
        )
        partition_lock = f"""    DECLARE @bcp_partition_lock int;
    EXEC @bcp_partition_lock = sys.sp_getapplock
        @Resource = {qs(partition_resource)},
        @LockMode = 'Exclusive',
        @LockOwner = 'Transaction',
        @LockTimeout = 0;
    IF @bcp_partition_lock < 0
        THROW 51049, 'Nao foi possivel obter o applock do particionamento.', 1;
"""
    return f"""{SET_OPTIONS}
BEGIN TRY
    BEGIN TRANSACTION;
{partition_lock}    DECLARE @bcp_ddl_lock int;
    EXEC @bcp_ddl_lock = sys.sp_getapplock
        @Resource = {qs(resource)},
        @LockMode = 'Exclusive',
        @LockOwner = 'Transaction',
        @LockTimeout = 0;
    IF @bcp_ddl_lock < 0
        THROW 51020, 'Não foi possível obter o applock do provisionamento.', 1;
{indented}
    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;"""


def build_ddl_plan(
    layout: TableLayout,
    *,
    stage: DdlStage | str = DdlStage.FULL,
    index_moment: IndexBuildMoment | str = IndexBuildMoment.AFTER_TABLE_LOAD,
    allow_schema_evolution: bool = False,
    include_go: bool = True,
    partition_reference_date: date | datetime | None = None,
) -> DdlPlan:
    """Gera um plano offline e batches aplicáveis, sem executar SQL.

    ``FULL`` é usado por provisionamento explícito e sempre inclui todos os
    índices. ``INITIAL`` respeita ``index_moment``. ``SECONDARY_INDEXES`` gera
    apenas os índices adiados, permitindo retomar sem recarregar dados.
    """

    normalized_stage = _coerce_stage(stage)
    normalized_moment = _coerce_moment(index_moment)
    indexes = _selected_indexes(layout, normalized_stage, normalized_moment)
    batch = _transaction_batch(
        layout,
        normalized_stage,
        indexes,
        allow_schema_evolution=allow_schema_evolution,
        partition_reference_date=partition_reference_date,
    )
    script = batch + ("\nGO\n" if include_go else "\n")
    return DdlPlan(
        layout=layout,
        stage=normalized_stage,
        index_moment=normalized_moment,
        included_indexes=tuple(index.name for index in indexes),
        batches=(batch,),
        ssms_script=script,
    )


def generate_ddl_script(
    layout: TableLayout,
    *,
    stage: DdlStage | str = DdlStage.FULL,
    index_moment: IndexBuildMoment | str = IndexBuildMoment.AFTER_TABLE_LOAD,
    allow_schema_evolution: bool = False,
    include_go: bool = True,
) -> str:
    return build_ddl_plan(
        layout,
        stage=stage,
        index_moment=index_moment,
        allow_schema_evolution=allow_schema_evolution,
        include_go=include_go,
    ).ssms_script


def build_apply_batches(
    layout: TableLayout,
    *,
    stage: DdlStage | str = DdlStage.FULL,
    index_moment: IndexBuildMoment | str = IndexBuildMoment.AFTER_TABLE_LOAD,
    allow_schema_evolution: bool = False,
) -> tuple[str, ...]:
    """Retorna batches sem ``GO``, apropriados para ``cursor.execute``."""

    return build_ddl_plan(
        layout,
        stage=stage,
        index_moment=index_moment,
        allow_schema_evolution=allow_schema_evolution,
        include_go=False,
    ).batches


def build_schema_evolution_batches(layout: TableLayout) -> tuple[str, ...]:
    """Build an ALTER-only transaction for an already-existing table."""

    resource = target_lock_resource(layout.schema, layout.table)
    statements = [
        (
            f"IF OBJECT_ID({qs(layout.qualified_name)}, N'U') IS NULL "
            "THROW 51024, 'Tabela ausente para evolucao de schema.', 1;"
        ),
        _table_partition_state_guard(layout),
        _schema_evolution_guard(layout, allow_schema_evolution=True),
    ]
    indented = "\n".join("    " + line.replace("\n", "\n    ") for line in statements)
    batch = f"""{SET_OPTIONS}
BEGIN TRY
    BEGIN TRANSACTION;
    DECLARE @bcp_ddl_lock int;
    EXEC @bcp_ddl_lock = sys.sp_getapplock
        @Resource = {qs(resource)},
        @LockMode = 'Exclusive',
        @LockOwner = 'Transaction',
        @LockTimeout = 0;
    IF @bcp_ddl_lock < 0
        THROW 51020, 'Nao foi possivel obter o applock da evolucao de schema.', 1;
{indented}
    COMMIT TRANSACTION;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;"""
    return (batch,)


generate_table_ddl = generate_ddl_script


def _normalize_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    return text


def normalize_sql_expression(value: Any) -> str | None:
    """Normaliza ruído de catálogo sem apagar agrupamentos semanticamente úteis."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    def fully_wrapped(candidate: str) -> bool:
        depth = 0
        quoted = False
        index = 0
        while index < len(candidate):
            char = candidate[index]
            if char == "'":
                if quoted and index + 1 < len(candidate) and candidate[index + 1] == "'":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(candidate) - 1:
                        return False
                    if depth < 0:
                        return False
            index += 1
        return depth == 0 and not quoted

    while len(text) >= 2 and text[0] == "(" and text[-1] == ")" and fully_wrapped(text):
        text = text[1:-1].strip()

    tokens = re.findall(r"'(?:[^']|'')*'|\[[^]]*(?:]][^]]*)*\]|[^'\[]+", text)
    normalized: list[str] = []
    for token in tokens:
        if token.startswith("'"):
            normalized.append(token)
        elif token.startswith("[") and token.endswith("]"):
            normalized.append(token[1:-1].replace("]]", "]").casefold())
        else:
            plain = re.sub(r"\s+", "", token).casefold()
            # O SQL Server persiste constantes de CASE como ``(1)`` mesmo
            # quando o contrato declarou ``1``. Essa parentizacao nao muda a
            # expressao e deve permanecer idempotente na reinspecao.
            plain = re.sub(
                r"\b(when|then|else)\(([+-]?\d+(?:\.\d+)?)\)",
                r"\1\2",
                plain,
            )
            normalized.append(plain)
    result = "".join(normalized)
    # O catalogo do SQL Server reescreve CAST simples como CONVERT. Preserve o
    # contrato declarativo do perfil e compare a forma canonica equivalente.
    result = re.sub(
        r"cast\(([a-z_][a-z0-9_]*)as([a-z]+(?:\(\d+(?:,\d+)?\))?)\)",
        r"convert(\2,\1)",
        result,
    )
    return result


def _actual_column_type(column: Mapping[str, Any]) -> str:
    direct = column.get("sql_type", column.get("data_type"))
    if direct is not None:
        return str(direct)
    if "type_name" in column:
        return type_sql(dict(column))
    return str(column.get("type", ""))


def _bool(record: Mapping[str, Any], *names: str, default: bool = False) -> bool:
    for name in names:
        if name in record:
            return bool(record[name])
    return default


def _column_differences(expected: ColumnDefinition, actual: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    prefix = f"coluna {expected.name}"
    if expected.sql_type is not None and _normalize_type(_actual_column_type(actual)) != _normalize_type(
        expected.sql_type
    ):
        differences.append(
            f"{prefix}: tipo esperado {expected.sql_type}, encontrado {_actual_column_type(actual)}"
        )
    actual_nullable = _bool(actual, "nullable", "is_nullable")
    if actual_nullable != expected.nullable:
        differences.append(f"{prefix}: nulabilidade divergente")
    actual_identity = _bool(actual, "identity", "is_identity")
    if actual_identity != expected.identity:
        differences.append(f"{prefix}: propriedade IDENTITY divergente")
    if expected.identity and actual_identity:
        if "identity_seed" in actual and int(actual["identity_seed"]) != expected.identity_seed:
            differences.append(f"{prefix}: seed do IDENTITY divergente")
        if "identity_increment" in actual and int(actual["identity_increment"]) != expected.identity_increment:
            differences.append(f"{prefix}: incremento do IDENTITY divergente")
    actual_computed = _bool(actual, "computed", "is_computed")
    if actual_computed != (expected.computed_expression is not None):
        differences.append(f"{prefix}: propriedade computed divergente")
    actual_computed_expression = actual.get(
        "computed_expression", actual.get("computed_definition")
    )
    if actual_computed and actual_computed_expression is None:
        actual_computed_expression = actual.get("definition")
    if normalize_sql_expression(actual_computed_expression) != normalize_sql_expression(
        expected.computed_expression
    ):
        differences.append(f"{prefix}: expressão computed divergente")
    actual_persisted = _bool(actual, "persisted", "is_persisted")
    if actual_persisted != expected.persisted:
        differences.append(f"{prefix}: persistência da computed divergente")
    actual_default = actual.get("default_expression", actual.get("default_definition"))
    if normalize_sql_expression(actual_default) != normalize_sql_expression(expected.default_expression):
        differences.append(f"{prefix}: expressão DEFAULT divergente")
    actual_default_name = actual.get("default_name", actual.get("default_constraint_name"))
    if (str(actual_default_name) if actual_default_name is not None else None) != expected.default_name:
        differences.append(f"{prefix}: nome da constraint DEFAULT divergente")
    if expected.collation is not None and actual.get(
        "collation", actual.get("collation_name")
    ) != expected.collation:
        differences.append(f"{prefix}: collation divergente")
    return differences


def _actual_index_keys(index: Mapping[str, Any]) -> tuple[IndexKey, ...]:
    raw_keys = index.get("keys", index.get("columns", ()))
    result: list[IndexKey] = []
    for key in raw_keys:
        if isinstance(key, str):
            result.append(IndexKey(key, "ASC"))
        else:
            name = key.get("name", key.get("column_name"))
            direction = key.get("direction", key.get("order"))
            if direction is None:
                direction = "DESC" if _bool(key, "descending", "is_descending_key") else "ASC"
            result.append(IndexKey(str(name), str(direction).upper()))
    return tuple(result)


def _actual_index_includes(index: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in index.get("includes", index.get("included_columns", ())):
        if isinstance(value, str):
            result.append(value)
        else:
            result.append(str(value.get("name", value.get("column_name"))))
    return tuple(result)


def _actual_clustered(index: Mapping[str, Any]) -> bool:
    if "clustered" in index:
        return bool(index["clustered"])
    if "type" in index and isinstance(index["type"], int):
        return index["type"] == 1
    return str(index.get("type_desc", "")).upper() == "CLUSTERED"


def _index_differences(expected: IndexDefinition, actual: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    prefix = f"índice {expected.name}"
    if str(actual.get("name", "")) != expected.name:
        differences.append(f"{prefix}: nome divergente")
    if _actual_index_keys(actual) != expected.keys:
        differences.append(f"{prefix}: chaves ou direções divergentes")
    if _bool(actual, "unique", "is_unique") != expected.unique:
        differences.append(f"{prefix}: unicidade divergente")
    if _actual_clustered(actual) != expected.clustered:
        differences.append(f"{prefix}: organização clustered/nonclustered divergente")
    if sorted(_actual_index_includes(actual)) != sorted(expected.includes):
        differences.append(f"{prefix}: colunas INCLUDE divergentes")
    actual_filter = actual.get("filter_predicate", actual.get("filter_definition"))
    actual_has_filter = _bool(actual, "has_filter", default=actual_filter is not None)
    if actual_has_filter != (expected.filter_predicate is not None):
        differences.append(f"{prefix}: presença de filtro divergente")
    if normalize_sql_expression(actual_filter) != normalize_sql_expression(expected.filter_predicate):
        differences.append(f"{prefix}: filtro divergente")
    if _bool(actual, "disabled", "is_disabled") != expected.disabled:
        differences.append(f"{prefix}: estado enabled/disabled divergente")
    if _bool(actual, "hypothetical", "is_hypothetical"):
        differences.append(f"{prefix}: indice hipotetico nao e aceito")
    if _bool(actual, "primary_key", "is_primary_key") != expected.primary_key:
        differences.append(f"{prefix}: propriedade de chave primária divergente")
    return differences


def _sequence_differences(layout: TableLayout, actual: Mapping[str, Any]) -> list[str]:
    expected = layout.sequence
    if expected is None:
        return []
    differences: list[str] = []
    if str(actual.get("name", "")) != expected.name:
        differences.append(f"sequence {expected.name}: nome divergente")
    actual_type = actual.get("sql_type", actual.get("data_type", actual.get("type_name")))
    if _normalize_type(actual_type) != _normalize_type(expected.sql_type):
        differences.append(f"sequence {expected.name}: tipo divergente")

    def integer_differs(value: Any, expected_value: int) -> bool:
        try:
            return value is None or int(value) != expected_value
        except (TypeError, ValueError, OverflowError):
            return True

    start = actual.get("start", actual.get("start_value"))
    if integer_differs(start, expected.start):
        differences.append(f"sequence {expected.name}: valor inicial divergente")
    increment = actual.get("increment", actual.get("increment_by"))
    if integer_differs(increment, expected.increment):
        differences.append(f"sequence {expected.name}: incremento divergente")
    has_cycle_flag = "cycle" in actual or "is_cycling" in actual
    if not has_cycle_flag or _bool(actual, "cycle", "is_cycling") != expected.cycle:
        differences.append(f"sequence {expected.name}: CYCLE/NO CYCLE divergente")
    return differences


def _partition_differences(
    layout: TableLayout,
    actual: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    expected = layout.partition
    if expected is None:
        return (
            ["tabela existente esta particionada, mas o contrato nao esta"]
            if actual
            else [],
            [],
        )
    if not isinstance(actual, Mapping):
        return (
            [
                "tabela existente nao esta particionada; migracao automatica destrutiva recusada"
            ],
            [],
        )

    differences: list[str] = []
    pending: list[str] = []
    exact_fields = (
        ("scheme_name", expected.scheme_name, "partition scheme"),
        ("function_name", expected.function_name, "partition function"),
        ("partition_column", expected.column, "coluna de particionamento"),
    )
    for field, wanted, label in exact_fields:
        if str(actual.get(field, "")) != wanted:
            differences.append(f"{label} divergente: esperado {wanted}")
    if not bool(actual.get("boundary_on_right", False)):
        differences.append("partition function deve usar RANGE RIGHT")
    try:
        actual_scale = int(actual.get("scale", -1))
    except (TypeError, ValueError, OverflowError):
        actual_scale = -1
    if str(actual.get("type_name", "")).casefold() != "datetime2" or actual_scale != 7:
        differences.append("partition function deve usar DATETIME2(7)")
    filegroups = tuple(str(value) for value in actual.get("filegroups", ()))
    if not filegroups or any(value != expected.filegroup for value in filegroups):
        differences.append("partition scheme deve mapear todas as particoes para PRIMARY")

    actual_boundaries: set[datetime] = set()
    try:
        for value in actual.get("boundaries", ()):
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is not None:
                raise ValueError("timezone nao suportado em limite de particao")
            actual_boundaries.add(parsed)
    except (TypeError, ValueError):
        differences.append("partition function contem limite temporal invalido")
    else:
        required_boundaries = {
            datetime.fromisoformat(value)
            for value in partition_boundary_values(expected)
        }
        missing = sorted(required_boundaries - actual_boundaries)
        if missing:
            maximum_actual = max(actual_boundaries) if actual_boundaries else None
            internal = [
                value
                for value in missing
                if maximum_actual is not None and value < maximum_actual
            ]
            if internal:
                differences.append(
                    "partition function possui lacuna mensal interna; primeiro limite ausente: "
                    + internal[0].isoformat(timespec="seconds")
                )
            else:
                pending.extend(
                    f"PARTITION_BOUNDARY:{expected.function_name}:"
                    + value.isoformat(timespec="seconds")
                    for value in missing
                )
        if any(
            value.day != 1
            or value.hour != 0
            or value.minute != 0
            or value.second != 0
            or value.microsecond != 0
            for value in actual_boundaries
        ):
            differences.append(
                "partition function contem limite que nao representa o inicio de um mes"
            )
    return differences, pending


def _index_placement_differences(
    expected: IndexDefinition,
    actual: Mapping[str, Any],
    partition: PartitionDefinition,
) -> list[str]:
    differences: list[str] = []
    prefix = f"indice {expected.name}"
    data_space_name = actual.get("data_space_name")
    partition_column = actual.get("partition_column")
    if expected.clustered:
        if data_space_name != partition.scheme_name:
            differences.append(f"{prefix}: partition scheme divergente")
        if partition_column != partition.column:
            differences.append(f"{prefix}: coluna de particionamento divergente")
    else:
        if data_space_name != partition.filegroup:
            differences.append(f"{prefix}: deve permanecer nao alinhado em PRIMARY")
        if partition_column not in (None, ""):
            differences.append(f"{prefix}: nao deve estar alinhado ao particionamento")
    return differences


def _catalog_indexes(catalog: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    indexes: list[dict[str, Any]] = [dict(item) for item in catalog.get("indexes", ())]
    primary = catalog.get("primary_key")
    if isinstance(primary, Mapping):
        canonical = dict(primary)
        canonical.setdefault("primary_key", True)
        if not any(item.get("name") == canonical.get("name") for item in indexes):
            indexes.append(canonical)
    return tuple(indexes)


def compare_catalog(
    layout: TableLayout,
    catalog: Mapping[str, Any] | None,
    *,
    include_secondary: bool = True,
    data_completed: bool = False,
) -> CatalogComparison:
    """Compara um snapshot agregado do catálogo com o contrato resolvido.

    O snapshot aceita ``columns``, ``primary_key``, ``indexes``, ``sequence`` e
    ``row_count``. Índices extras nonclustered são tolerados; outro clustered é
    sempre conflito e nunca é removido automaticamente.
    """

    if not catalog or catalog.get("exists", True) is False:
        missing = [f"TABLE:{layout.schema}.{layout.table}"]
        if layout.partition:
            missing.extend(
                (
                    f"PARTITION_FUNCTION:{layout.partition.function_name}",
                    f"PARTITION_SCHEME:{layout.partition.scheme_name}",
                )
            )
        if layout.sequence:
            missing.append(f"SEQUENCE:{layout.schema}.{layout.sequence.name}")
        missing.extend(f"INDEX:{index.name}" for index in (layout.primary_key,) + layout.indexes)
        return CatalogComparison(CatalogState.ABSENT, missing_objects=tuple(missing))

    errors: list[str] = []
    missing_base: list[str] = []
    missing_secondary: list[str] = []
    warnings: list[str] = []
    partition_errors, partition_pending = _partition_differences(
        layout, catalog.get("partition")
    )
    errors.extend(partition_errors)
    missing_base.extend(partition_pending)
    object_type = catalog.get("object_type", "U")
    if object_type != "U":
        errors.append(f"{layout.schema}.{layout.table}: tipo de objeto {object_type!r}, esperado 'U'")
    actual_schema = catalog.get("schema")
    if actual_schema is not None and str(actual_schema) != layout.schema:
        errors.append(
            f"schema do destino fora do contrato lowercase: {actual_schema!r}; "
            f"esperado {layout.schema!r}"
        )
    actual_table = catalog.get("table")
    if actual_table is not None and str(actual_table) != layout.table:
        errors.append(
            f"tabela do destino fora do contrato lowercase: {actual_table!r}; "
            f"esperado {layout.table!r}"
        )

    actual_columns = tuple(catalog.get("columns", ()))
    # Destination identifiers are canonical lowercase.  ``source_name`` keeps
    # the original source spelling used by the export projection.
    expected_names = tuple(column.name.casefold() for column in layout.columns)
    actual_names = tuple(str(column.get("name", "")) for column in actual_columns)
    actual_folded_names = tuple(name.casefold() for name in actual_names)
    expected_set = set(expected_names)
    actual_set = set(actual_folded_names)
    missing_names = tuple(name for name in expected_names if name not in actual_set)
    business_names = {column.name.casefold() for column in layout.business_columns}
    missing_business = tuple(name for name in missing_names if name in business_names)
    missing_required = tuple(name for name in missing_names if name not in business_names)
    unexpected = tuple(
        name for name in actual_names if name.casefold() not in expected_set
    )
    noncanonical = tuple(name for name in actual_names if name != name.casefold())
    duplicate_folded = tuple(
        sorted(
            {
                name.casefold()
                for name in actual_names
                if actual_folded_names.count(name.casefold()) > 1
            }
        )
    )
    if missing_required:
        errors.append(
            "colunas tecnicas/metadados ausentes: " + ", ".join(missing_required)
        )
    if unexpected:
        errors.append(
            "colunas inesperadas no destino (nenhuma remocao automatica permitida): "
            + ", ".join(unexpected)
        )
    if noncanonical:
        errors.append(
            "colunas do destino fora do padrao lowercase: "
            + ", ".join(noncanonical)
        )
    if duplicate_folded:
        errors.append(
            "colunas do destino colidem em comparacao case-insensitive: "
            + ", ".join(duplicate_folded)
        )
    expected_present_order = tuple(name for name in expected_names if name in actual_set)
    actual_expected_order = tuple(
        name.casefold() for name in actual_names if name.casefold() in expected_set
    )
    if actual_expected_order != expected_present_order:
        warnings.append(
            "Ordem fisica das colunas diverge da ordem logica do contrato; "
            "a importacao permanece segura por usar listas de colunas nomeadas."
        )
    actual_by_name = {
        str(column.get("name", "")).casefold(): column
        for column in actual_columns
    }
    for expected in layout.columns:
        actual = actual_by_name.get(expected.name.casefold())
        if actual is None:
            continue
        errors.extend(_column_differences(expected, actual))

    actual_indexes = _catalog_indexes(catalog)
    actual_index_names = tuple(str(index.get("name", "")) for index in actual_indexes)
    noncanonical_indexes = tuple(
        name for name in actual_index_names if name != name.casefold()
    )
    duplicate_folded_indexes = tuple(
        sorted(
            {
                name.casefold()
                for name in actual_index_names
                if sum(
                    1
                    for candidate in actual_index_names
                    if candidate.casefold() == name.casefold()
                )
                > 1
            }
        )
    )
    if noncanonical_indexes:
        errors.append(
            "indices/constraints do destino fora do padrao lowercase: "
            + ", ".join(noncanonical_indexes)
        )
    if duplicate_folded_indexes:
        errors.append(
            "indices/constraints do destino colidem em comparacao case-insensitive: "
            + ", ".join(duplicate_folded_indexes)
        )
    by_name = {
        str(index.get("name", "")).casefold(): index for index in actual_indexes
    }
    actual_primary = next(
        (index for index in actual_indexes if _bool(index, "primary_key", "is_primary_key")),
        None,
    )
    if actual_primary is None:
        missing_base.append(f"PRIMARY_KEY:{layout.primary_key.name}")
    else:
        errors.extend(_index_differences(layout.primary_key, actual_primary))
        if layout.partition is not None:
            errors.extend(
                _index_placement_differences(
                    layout.primary_key, actual_primary, layout.partition
                )
            )

    expected_clustered = next(
        index for index in (layout.primary_key,) + layout.indexes if index.clustered
    )
    actual_clustered = [index for index in actual_indexes if _actual_clustered(index)]
    clustered_conflicts = [
        str(index.get("name", ""))
        for index in actual_clustered
        if str(index.get("name", "")).casefold() != expected_clustered.name.casefold()
    ]
    if clustered_conflicts:
        return CatalogComparison(
            CatalogState.CLUSTERED_CONFLICT,
            errors=(
                f"Clustered existente incompatível: {', '.join(clustered_conflicts)}; "
                f"esperado {expected_clustered.name}. Nenhuma substituição é permitida.",
            ),
        )

    expected_indexes = layout.indexes if include_secondary else layout.base_indexes
    for expected in expected_indexes:
        actual = by_name.get(expected.name.casefold())
        if actual is None:
            target = missing_secondary if expected.phase == "secondary" else missing_base
            target.append(f"INDEX:{expected.name}")
        else:
            errors.extend(_index_differences(expected, actual))
            if layout.partition is not None:
                errors.extend(
                    _index_placement_differences(expected, actual, layout.partition)
                )

    if layout.sequence is not None:
        actual_sequence = catalog.get("sequence")
        if not isinstance(actual_sequence, Mapping):
            sequences = catalog.get("sequences", ())
            actual_sequence = next(
                (
                    sequence
                    for sequence in sequences
                    if str(sequence.get("name", "")).casefold()
                    == layout.sequence.name.casefold()
                ),
                None,
            )
        if actual_sequence is None:
            if int(catalog.get("row_count", 0) or 0) > 0:
                errors.append(
                    f"sequence {layout.sequence.name} ausente em tabela povoada; "
                    "é necessária migração explícita"
                )
            else:
                missing_base.append(f"SEQUENCE:{layout.sequence.name}")
        else:
            errors.extend(_sequence_differences(layout, actual_sequence))

    if errors:
        return CatalogComparison(CatalogState.INCOMPATIBLE, tuple(errors), warnings=tuple(warnings))
    if missing_business:
        missing_columns = tuple(f"COLUMN:{name}" for name in missing_business)
        return CatalogComparison(
            CatalogState.SCHEMA_EVOLUTION_PENDING,
            missing_objects=tuple(missing_columns + tuple(missing_base) + tuple(missing_secondary)),
            warnings=tuple(warnings),
            missing_business_columns=missing_business,
        )
    if missing_base:
        return CatalogComparison(
            CatalogState.OBJECTS_PENDING,
            missing_objects=tuple(missing_base + missing_secondary),
            warnings=tuple(warnings),
        )
    if missing_secondary:
        state = (
            CatalogState.DATA_COMPLETE_INDEXES_PENDING
            if data_completed
            else CatalogState.INDEXES_PENDING
        )
        return CatalogComparison(
            state, missing_objects=tuple(missing_secondary), warnings=tuple(warnings)
        )
    extra_indexes = sorted(
        name
        for name in by_name
        if name and name not in {layout.primary_key.name, *(index.name for index in layout.indexes)}
    )
    if extra_indexes:
        warnings.append("Índices nonclustered extras preservados: " + ", ".join(extra_indexes))
    return CatalogComparison(CatalogState.COMPATIBLE, warnings=tuple(warnings))


__all__ = [
    "CatalogComparison",
    "CatalogState",
    "DdlPlan",
    "DdlStage",
    "IndexBuildMoment",
    "SchemaEvolutionAnalysis",
    "SchemaEvolutionColumn",
    "SchemaEvolutionError",
    "SchemaEvolutionRequiredError",
    "SchemaEvolutionSafetyError",
    "SET_OPTIONS",
    "analyze_schema_evolution",
    "build_apply_batches",
    "build_ddl_plan",
    "build_schema_evolution_batches",
    "compare_catalog",
    "create_index_statement",
    "create_sequence_statement",
    "create_table_statement",
    "generate_ddl_script",
    "generate_table_ddl",
    "normalize_sql_expression",
    "partition_boundary_values",
]
