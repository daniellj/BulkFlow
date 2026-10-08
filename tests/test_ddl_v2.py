from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BRASILIA_TIMESTAMP_SQL = (
    "CONVERT(DATETIME2(7), ((SYSUTCDATETIME() AT TIME ZONE 'UTC') "
    "AT TIME ZONE 'E. South America Standard Time'))"
)

from bcp_engine.ddl import (  # noqa: E402
    CatalogState,
    DdlStage,
    IndexBuildMoment,
    build_apply_batches,
    build_ddl_plan,
    compare_catalog,
    create_table_statement,
    partition_boundary_values,
)
from bcp_engine.profiles import ProfileError, load_profile, resolve_profile  # noqa: E402
from bcp_engine.projection import build_import_plan  # noqa: E402
from bcp_engine.source import validate_source_columns  # noqa: E402


def source_column(
    name: str,
    kind: str = "int",
    *,
    max_length: int = 4,
    precision: int = 10,
    scale: int = 0,
    nullable: bool = False,
    collation: str | None = None,
) -> dict:
    return {
        "name": name,
        "type_name": kind,
        "max_length": max_length,
        "precision": precision,
        "scale": scale,
        "is_nullable": nullable,
        "collation_name": collation,
        "is_identity": False,
        "is_computed": False,
    }


def bronze_layout(*, partitioned: bool = False):
    return resolve_profile(
        "bronze",
        [
            source_column("codigo"),
            source_column(
                "descricao",
                "nvarchar",
                max_length=200,
                nullable=True,
                collation="Latin1_General_100_CI_AS",
            ),
        ],
        source_database="BD_ORIGEM",
        source_table="TABELA_ORIGEM_01",
        destination_table="bd_origem_tabela_origem_01",
        destination_schema="s344",
        partition_column="dh_carga" if partitioned else None,
    )


def landing_layout(*, partitioned: bool = False):
    return resolve_profile(
        "landing",
        [source_column("codigo")],
        source_database="BD_ORIGEM",
        source_table="TABELA_ORIGEM_01",
        destination_table="bd_origem_tabela_origem_01",
        destination_schema="s344",
        partition_column="dh_carga" if partitioned else None,
    )


def catalog_for(layout) -> dict:
    columns = []
    for column in layout.columns:
        columns.append(
            {
                "name": column.name,
                "sql_type": column.sql_type,
                "nullable": column.nullable,
                "identity": column.identity,
                "identity_seed": column.identity_seed,
                "identity_increment": column.identity_increment,
                "computed": column.computed_expression is not None,
                "computed_expression": column.computed_expression,
                "persisted": column.persisted,
                "default_expression": column.default_expression,
                "default_name": column.default_name,
                "collation": column.collation,
            }
        )

    def index_record(index):
        record = {
            "name": index.name,
            "keys": [
                {"name": key.name, "direction": key.direction} for key in index.keys
            ],
            "unique": index.unique,
            "clustered": index.clustered,
            "includes": list(index.includes),
            "filter_predicate": index.filter_predicate,
            "disabled": index.disabled,
            "primary_key": index.primary_key,
        }
        if layout.partition is not None:
            record["data_space_name"] = (
                layout.partition.scheme_name
                if index.clustered
                else layout.partition.filegroup
            )
            record["partition_column"] = (
                layout.partition.column if index.clustered else None
            )
        return record

    sequence = None
    if layout.sequence:
        sequence = {
            "name": layout.sequence.name,
            "sql_type": layout.sequence.sql_type,
            "start": layout.sequence.start,
            "increment": layout.sequence.increment,
            "cycle": layout.sequence.cycle,
        }
    result = {
        "exists": True,
        "object_type": "U",
        "schema": layout.schema,
        "table": layout.table,
        "row_count": 0,
        "columns": columns,
        "primary_key": index_record(layout.primary_key),
        "indexes": [index_record(index) for index in layout.indexes],
        "sequence": sequence,
    }
    if layout.partition is not None:
        result["partition"] = {
            "scheme_name": layout.partition.scheme_name,
            "function_name": layout.partition.function_name,
            "partition_column": layout.partition.column,
            "boundary_on_right": True,
            "type_name": "datetime2",
            "scale": 7,
            "filegroups": ["PRIMARY"],
            "boundaries": list(partition_boundary_values(layout.partition)),
        }
    return result


class ProfileTests(unittest.TestCase):
    def test_landing_source_column_mapping_is_validated_and_projected(self):
        columns = [source_column("codigo")]
        mapping = {
            "aud_ccid": {"source_column": "codigo"},
            "aud_cntrrn": {"constant": 0, "sql_type": "INT"},
            "aud_enttyp": {"constant": "PT", "sql_type": "VARCHAR(30)"},
        }
        validate_source_columns(columns, metadata_mapping=mapping)
        plan = build_import_plan(landing_layout(), metadata_mapping=mapping)
        expression = plan["select_expressions"][
            plan["insert_columns"].index("aud_ccid")
        ]
        self.assertEqual(expression, "b.[codigo]")

        invalid = {**mapping, "aud_ccid": {"source_column": "missing_column"}}
        with self.assertRaisesRegex(RuntimeError, "source_column.*missing_column"):
            validate_source_columns(columns, metadata_mapping=invalid)

    def test_configured_source_names_use_exact_catalog_case_in_generated_sql(self):
        landing = resolve_profile(
            "landing",
            [source_column("CustomerID")],
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="bd_origem_tabela_origem_01",
            destination_schema="s344",
        )
        mapping = {
            "aud_ccid": {"source_column": "customerid"},
            "aud_cntrrn": {"constant": 0, "sql_type": "INT"},
            "aud_enttyp": {"constant": "PT", "sql_type": "VARCHAR(30)"},
        }
        plan = build_import_plan(landing, metadata_mapping=mapping)
        self.assertIn("b.[CustomerID]", plan["select_expressions"])

        bronze = resolve_profile(
            "bronze",
            [source_column("EventID", "bigint", max_length=8)],
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="bd_origem_tabela_origem_01",
            destination_schema="s344",
            bronze_event_id={
                "strategy": "source_column",
                "source_column": "eventid",
            },
        )
        self.assertEqual(bronze.technical_id_source_column, "EventID")
        bronze_plan = build_import_plan(bronze)
        self.assertEqual(bronze_plan["select_expressions"][0], "b.[EventID]")

    def test_profile_contract_uses_only_english_keys_and_placeholders(self):
        profile = load_profile("bronze")
        self.assertEqual(
            set(profile),
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
            },
        )
        self.assertEqual(profile["table_name"], "{destination_table}")
        self.assertEqual(profile["technical_column"]["generation"], "sequence")
        self.assertEqual(profile["indexes"][0]["phase"], "secondary")

    def test_legacy_profile_keys_and_placeholders_are_rejected(self):
        profile_with_legacy_key = load_profile("bronze")
        profile_with_legacy_key["versao_perfil"] = 1
        with self.assertRaisesRegex(ProfileError, "campos desconhecidos"):
            load_profile(profile_with_legacy_key)

        profile_with_legacy_placeholder = load_profile("bronze")
        profile_with_legacy_placeholder["table_name"] = "{tabela_destino}"
        with self.assertRaisesRegex(ProfileError, "Placeholders desconhecidos"):
            load_profile(profile_with_legacy_placeholder)

    def test_profile_can_reference_the_resolved_technical_column_placeholder(self):
        profile = load_profile("bronze")
        profile["primary_key"]["columns"][0]["name"] = "{technical_column}"

        layout = resolve_profile(
            profile,
            [source_column("codigo")],
            source_database="db",
            source_table="t",
            destination_schema="s",
        )

        self.assertEqual(layout.primary_key.keys[0].name, layout.columns[0].name)

    def test_profile_rejects_coerced_boolean_and_integer_values(self):
        mutations = []

        version = load_profile("bronze")
        version["profile_version"] = True
        mutations.append((version, "profile_version"))

        nullable = load_profile("bronze")
        nullable["technical_column"]["nullable"] = "false"
        mutations.append((nullable, "nullable"))

        clustered = load_profile("bronze")
        clustered["primary_key"]["clustered"] = "false"
        mutations.append((clustered, "clustered"))

        unique = load_profile("bronze")
        unique["indexes"][0]["unique"] = "false"
        mutations.append((unique, "unique"))

        sequence = load_profile("bronze")
        sequence["sequence"]["increment"] = True
        mutations.append((sequence, "increment"))

        for profile, field in mutations:
            with self.subTest(field=field), self.assertRaisesRegex(ProfileError, field):
                load_profile(profile)

    def test_destination_identifiers_are_lowercase_and_source_names_are_preserved(self):
        source = [source_column("CustomerID")]
        bronze = resolve_profile(
            "bronze",
            source,
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="BD_ORIGEM_TABELA_ORIGEM_01",
            destination_schema="S344",
        )
        landing = resolve_profile(
            "landing",
            source,
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="BD_ORIGEM_TABELA_ORIGEM_01",
            destination_schema="S344",
        )

        for layout in (bronze, landing):
            self.assertEqual(layout.source_database, "BD_ORIGEM")
            self.assertEqual(layout.source_table, "TABELA_ORIGEM_01")
            self.assertEqual(layout.schema, "s344")
            self.assertEqual(layout.table, "bd_origem_tabela_origem_01")
            self.assertTrue(all(column.name == column.name.lower() for column in layout.columns))
            self.assertEqual(layout.business_columns[0].name, "customerid")
            self.assertEqual(layout.business_columns[0].source_name, "CustomerID")
            self.assertEqual(layout.primary_key.name, layout.primary_key.name.lower())
            self.assertTrue(
                all(index.name == index.name.lower() for index in layout.indexes)
            )
            self.assertTrue(
                all(
                    key.name == key.name.lower()
                    for index in (layout.primary_key,) + layout.indexes
                    for key in index.keys
                )
            )

        self.assertEqual(bronze.sequence.name, bronze.sequence.name.lower())
        self.assertEqual(
            landing.columns[8].default_name,
            landing.columns[8].default_name.lower(),
        )

    def test_bronze_contract_is_exact(self):
        layout = bronze_layout()
        self.assertEqual(layout.profile_name, "bronze")
        self.assertEqual(layout.profile_version, 1)
        self.assertEqual(
            [column.name for column in layout.columns],
            [
                "id_bd_origem_tabela_origem_01",
                "codigo",
                "descricao",
                "bi_lsn_evento",
                "bi_sequencia_evento",
                "cd_operacao",
                "de_operacao",
                "dh_carga",
                "dh_atualizacao",
            ],
        )
        technical = layout.columns[0]
        self.assertEqual(technical.sql_type, "BIGINT")
        self.assertFalse(technical.identity)
        self.assertFalse(technical.nullable)
        self.assertEqual(layout.sequence.name, "seq_bd_origem_tabela_origem_01")
        self.assertFalse(layout.sequence.cycle)
        self.assertTrue(layout.primary_key.clustered)
        self.assertEqual(layout.primary_key.keys[0].direction, "ASC")
        self.assertEqual(len(layout.indexes), 5)
        self.assertEqual(layout.indexes[1].keys[0].direction, "DESC")
        self.assertEqual(layout.indexes[2].keys[0].direction, "DESC")
        self.assertTrue(layout.columns[6].persisted)
        self.assertFalse(layout.columns[6].nullable)
        for name in ("bi_lsn_evento", "bi_sequencia_evento"):
            event_column = next(column for column in layout.columns if column.name == name)
            self.assertEqual(event_column.sql_type.upper(), "BINARY(10)")
            self.assertFalse(event_column.nullable)
            self.assertIsNone(event_column.computed_expression)
        fills = {rule.column: rule.value for rule in layout.fill_rules}
        self.assertEqual(fills["dh_carga"], BRASILIA_TIMESTAMP_SQL)

    def test_custom_profile_keeps_its_own_timestamp_semantics(self):
        profile = load_profile("bronze")
        custom_fill = "CONVERT(DATETIME2(7),'2000-01-01T00:00:00.0000000')"
        for column in profile["additional_columns"]:
            if column["name"] == "dh_carga":
                column["fill"] = custom_fill

        layout = resolve_profile(
            profile,
            [source_column("codigo")],
            source_database="db",
            source_table="t",
            destination_schema="s",
        )

        fills = {rule.column: rule.value for rule in layout.fill_rules}
        self.assertEqual(fills["dh_carga"], custom_fill)

    def test_landing_contract_is_exact(self):
        layout = landing_layout()
        self.assertEqual(
            [column.name for column in layout.columns],
            [
                "id_tabela_origem_01",
                "codigo",
                "aud_ccid",
                "aud_cntrrn",
                "aud_enttyp",
                "bi_lsn_evento",
                "bi_sequencia_evento",
                "cd_operacao",
                "dh_carga",
                "dh_atualizacao",
            ],
        )
        self.assertTrue(layout.columns[0].identity)
        self.assertIsNone(layout.sequence)
        self.assertFalse(layout.primary_key.clustered)
        self.assertTrue(layout.indexes[0].clustered)
        self.assertEqual(layout.indexes[0].phase, "base")
        self.assertEqual(layout.columns[8].default_name, "df_bd_origem_tabela_origem_01_dh_carga")
        self.assertEqual(
            layout.columns[8].default_expression,
            f"({BRASILIA_TIMESTAMP_SQL})",
        )
        self.assertNotIn("de_operacao", [column.name for column in layout.columns])
        self.assertEqual(len(layout.indexes), 5)

    def test_business_collision_with_profile_metadata_is_rejected(self):
        with self.assertRaisesRegex(ProfileError, "Colisão de coluna"):
            resolve_profile(
                "bronze",
                [source_column("cd_operacao")],
                source_database="db",
                source_table="t",
                destination_table="db_t",
                destination_schema="bronze",
            )

    def test_bronze_event_id_can_use_configured_sequence_or_source_column(self):
        source = [source_column("evento_id", "bigint", max_length=8)]
        sequence_layout = resolve_profile(
            "bronze",
            source,
            source_database="db",
            source_table="evento",
            destination_table="db_evento",
            destination_schema="bronze",
            bronze_event_id={
                "strategy": "sequence",
                "sequence_name": "sq_custom_{destination_table}",
            },
        )
        self.assertEqual(sequence_layout.sequence.name, "sq_custom_db_evento")
        source_layout = resolve_profile(
            "bronze",
            source,
            source_database="db",
            source_table="evento",
            destination_table="db_evento",
            destination_schema="bronze",
            bronze_event_id={
                "strategy": "source_column",
                "source_column": "evento_id",
            },
        )
        self.assertIsNone(source_layout.sequence)
        self.assertEqual(source_layout.technical_id_strategy, "source_column")
        self.assertEqual(source_layout.technical_id_source_column, "evento_id")
        self.assertEqual(source_layout.fill_rules[0].value, "source:evento_id")

    def test_optional_partition_rewrites_both_profiles_without_touching_legacy_layouts(self):
        legacy = bronze_layout()
        self.assertIsNone(legacy.partition)
        self.assertTrue(legacy.primary_key.clustered)

        for layout in (bronze_layout(partitioned=True), landing_layout(partitioned=True)):
            with self.subTest(profile=layout.profile_name):
                self.assertIsNotNone(layout.partition)
                self.assertEqual(layout.partition.column, "dh_carga")
                self.assertEqual(layout.partition.function_name, "pf_dh_carga_mensal")
                self.assertEqual(layout.partition.scheme_name, "ps_dh_carga_mensal")
                self.assertFalse(layout.primary_key.clustered)
                clustered = [index for index in layout.indexes if index.clustered]
                self.assertEqual(len(clustered), 1)
                self.assertEqual(clustered[0].keys[0].name, "dh_carga")
                simple = [
                    index
                    for index in layout.indexes
                    if not index.clustered
                    and len(index.keys) == 1
                    and index.keys[0].name == "dh_carga"
                ]
                self.assertEqual(simple, [])

    def test_partition_column_must_exist_and_be_datetime2_7(self):
        with self.assertRaisesRegex(ProfileError, "ausente no destino"):
            resolve_profile(
                "bronze",
                [source_column("codigo")],
                source_database="db",
                source_table="t",
                destination_schema="s",
                partition_column="nao_existe",
            )
        with self.assertRaisesRegex(ProfileError, r"deve ser DATETIME2\(7\)"):
            resolve_profile(
                "bronze",
                [source_column("codigo")],
                source_database="db",
                source_table="t",
                destination_schema="s",
                partition_column="codigo",
            )

    def test_non_default_datetime_partition_uses_attr_descriptor(self):
        layout = resolve_profile(
            "bronze",
            [
                source_column(
                    "event_at",
                    "datetime2",
                    max_length=8,
                    precision=27,
                    scale=7,
                )
            ],
            source_database="db",
            source_table="t",
            destination_schema="s",
            partition_column="EVENT_AT",
        )
        self.assertEqual(layout.partition.column, "event_at")
        self.assertEqual(layout.partition.descriptor, "attr")
        self.assertEqual(layout.partition.function_name, "pf_event_at_attr")


class DdlGenerationTests(unittest.TestCase):
    def test_partition_boundaries_cover_current_month_through_december_year_plus_six(self):
        layout = bronze_layout(partitioned=True)
        values = partition_boundary_values(
            layout.partition, reference_date=date(2026, 10, 7)
        )
        self.assertEqual(values[0], "2026-10-01T00:00:00.0000000")
        self.assertEqual(values[-1], "2032-12-01T00:00:00.0000000")
        self.assertEqual(len(values), 75)

    def test_partitioned_ddl_uses_nonaligned_pk_and_one_aligned_clustered_index(self):
        for layout in (bronze_layout(partitioned=True), landing_layout(partitioned=True)):
            with self.subTest(profile=layout.profile_name):
                table = create_table_statement(layout)
                self.assertIn(
                    "ON [ps_dh_carga_mensal] ([dh_carga]);", table
                )
                self.assertNotIn("PRIMARY KEY", table)
                plan = build_ddl_plan(
                    layout, partition_reference_date=date(2026, 10, 7)
                )
                sql = plan.ssms_script
                self.assertIn(
                    "CREATE PARTITION FUNCTION [pf_dh_carga_mensal] (DATETIME2(7)) AS RANGE RIGHT",
                    sql,
                )
                self.assertIn("2026-10-01T00:00:00.0000000", sql)
                self.assertIn("2032-12-01T00:00:00.0000000", sql)
                self.assertIn(
                    "CREATE PARTITION SCHEME [ps_dh_carga_mensal] AS PARTITION [pf_dh_carga_mensal] ALL TO ([PRIMARY])",
                    sql,
                )
                self.assertIn("PRIMARY KEY NONCLUSTERED", sql)
                self.assertIn(") ON [PRIMARY];", sql)
                self.assertIn(
                    f"CREATE CLUSTERED INDEX [{layout.partition.clustered_index_name}]",
                    sql,
                )
                self.assertIn(
                    "ON [ps_dh_carga_mensal] ([dh_carga]);", sql
                )
                self.assertIn("sys.partition_range_values", sql)
                self.assertIn("THROW 51050", sql)
                self.assertNotRegex(sql, r"(?im)^\s*DROP\s")

    def test_partitioned_profiles_do_not_create_redundant_dh_carga_index(self):
        bronze_sql = build_ddl_plan(bronze_layout(partitioned=True)).ssms_script
        landing_sql = build_ddl_plan(landing_layout(partitioned=True)).ssms_script
        self.assertNotIn("[ix_bd_origem_tabela_origem_01_02]", bronze_sql)
        self.assertNotIn("[ix_tabela_origem_01_05]", landing_sql)

    def test_secondary_stage_can_extend_existing_partition_horizon(self):
        sql = build_ddl_plan(
            bronze_layout(partitioned=True),
            stage=DdlStage.SECONDARY_INDEXES,
            partition_reference_date=date(2026, 10, 7),
        ).ssms_script

        self.assertIn("Partition function requerida esta ausente", sql)
        self.assertIn("ALTER PARTITION FUNCTION [pf_dh_carga_mensal]()", sql)
        self.assertIn("SPLIT RANGE", sql)
        self.assertNotIn("CREATE PARTITION FUNCTION [pf_dh_carga_mensal]", sql)

    def test_bronze_table_sequence_pk_and_indexes(self):
        layout = bronze_layout()
        table = create_table_statement(layout)
        self.assertIn("[id_bd_origem_tabela_origem_01] BIGINT NOT NULL", table)
        self.assertNotIn("IDENTITY", table)
        self.assertIn("PRIMARY KEY CLUSTERED ([id_bd_origem_tabela_origem_01] ASC)", table)
        self.assertIn("[de_operacao] AS (CASE [cd_operacao]", table)
        self.assertIn("PERSISTED NOT NULL", table)

        plan = build_ddl_plan(layout)
        sql = plan.ssms_script
        self.assertIn("CREATE SEQUENCE [s344].[seq_bd_origem_tabela_origem_01] AS BIGINT", sql)
        self.assertIn("NO CYCLE", sql)
        self.assertIn("seq.start_value", sql)
        self.assertIn("seq.increment", sql)
        self.assertIn("seq.is_cycling", sql)
        for number in range(1, 6):
            self.assertIn(f"[ix_bd_origem_tabela_origem_01_{number:02d}]", sql)
        self.assertEqual(len(plan.included_indexes), 5)

    def test_landing_ddl_has_identity_computed_default_and_clustered_base(self):
        layout = landing_layout()
        table = create_table_statement(layout)
        sql = build_ddl_plan(layout).ssms_script
        self.assertIn("[id_tabela_origem_01] BIGINT IDENTITY(1,1) NOT NULL", sql)
        self.assertIn("CAST([aud_ccid] AS BINARY(10))", sql)
        self.assertIn("[cd_operacao] AS (CASE [aud_enttyp]", sql)
        self.assertIn(
            "CONSTRAINT [df_bd_origem_tabela_origem_01_dh_carga] "
            f"DEFAULT ({BRASILIA_TIMESTAMP_SQL}) NOT NULL",
            table,
        )
        self.assertIn("PRIMARY KEY NONCLUSTERED ([id_tabela_origem_01] ASC)", sql)
        self.assertIn("CREATE CLUSTERED INDEX [ix_tabela_origem_01_01]", sql)
        self.assertNotIn("[de_operacao]", sql)

    def test_apply_batch_is_transactional_idempotent_and_has_no_go(self):
        batch = build_apply_batches(bronze_layout())[0]
        for setting in (
            "SET ANSI_NULLS ON",
            "SET ANSI_PADDING ON",
            "SET ANSI_WARNINGS ON",
            "SET ARITHABORT ON",
            "SET CONCAT_NULL_YIELDS_NULL ON",
            "SET QUOTED_IDENTIFIER ON",
            "SET NUMERIC_ROUNDABORT OFF",
            "SET XACT_ABORT ON",
        ):
            self.assertIn(setting, batch)
        self.assertIn("BEGIN TRY", batch)
        self.assertIn("BEGIN TRANSACTION", batch)
        self.assertIn("sys.sp_getapplock", batch)
        self.assertIn("ROLLBACK TRANSACTION", batch)
        self.assertIn("IF SCHEMA_ID", batch)
        self.assertIn("IF OBJECT_ID", batch)
        self.assertIn("IF NOT EXISTS", batch)
        self.assertIn("is_hypothetical = 1", batch)
        self.assertNotRegex(batch, r"(?im)^\s*GO\s*$")
        self.assertNotRegex(batch, r"(?i)\b(?:DROP|TRUNCATE)\b")

    def test_saved_ddl_embeds_complete_fail_closed_catalog_proof(self):
        sql = build_ddl_plan(landing_layout()).ssms_script

        # Exact physical names are compared as bytes so a case-insensitive
        # destination collation cannot make uppercase objects acceptable.
        self.assertIn("CONVERT(varbinary(256), s.name)", sql)
        self.assertIn("CONVERT(varbinary(256), t.name)", sql)
        self.assertIn("CONVERT(varbinary(256), ac.name)", sql)

        # SQL Server itself canonicalizes a temporary reference table.  The
        # saved script then checks every contract attribute against it.
        self.assertIn("CREATE TABLE #bcp_expected_layout", sql)
        self.assertIn("tempdb.sys.columns", sql)
        self.assertIn("ac.max_length <> ec.max_length", sql)
        self.assertIn("ac.precision <> ec.precision", sql)
        self.assertIn("ac.scale <> ec.scale", sql)
        self.assertIn("ac.is_nullable <> ec.is_nullable", sql)
        self.assertIn("ac.is_identity <> ec.is_identity", sql)
        self.assertIn("aic.seed_value", sql)
        self.assertIn("acc.definition", sql)
        self.assertIn("acc.is_persisted", sql)
        self.assertIn("adc.definition", sql)
        self.assertIn("expected_default.default_name", sql)
        self.assertIn("ac.collation_name", sql)

        # PK and every selected index are checked by properties and by a
        # bidirectional EXCEPT over keys, directions and INCLUDE columns.
        self.assertIn("ai.is_primary_key <> ei.is_primary_key", sql)
        self.assertIn("ai.is_unique <> ei.is_unique", sql)
        self.assertIn("ai.has_filter <> ei.has_filter", sql)
        self.assertGreaterEqual(sql.count("EXCEPT"), 2 * 6)
        self.assertIn("THROW 51037", sql)

    def test_saved_and_apply_ddl_share_the_same_fail_closed_guard(self):
        layout = bronze_layout()
        saved = build_ddl_plan(layout, include_go=False).ssms_script.rstrip()
        applied = build_apply_batches(layout)[0].rstrip()

        self.assertEqual(saved, applied)
        self.assertIn("SQL_VARIANT_PROPERTY", saved)
        self.assertIn("seq.precision", saved)
        self.assertIn("seq.scale", saved)
        self.assertIn("CONVERT(varbinary(256), seq.name)", saved)
        self.assertIn("seq.start_value", saved)
        self.assertIn("seq.increment", saved)
        self.assertIn("seq.is_cycling", saved)

    def test_secondary_only_ddl_validates_existing_base_contract(self):
        sql = build_ddl_plan(
            landing_layout(), stage=DdlStage.SECONDARY_INDEXES
        ).ssms_script

        self.assertIn("CREATE TABLE #bcp_expected_layout", sql)
        self.assertIn("pk_bd_origem_tabela_origem_01", sql)
        self.assertIn("ix_tabela_origem_01_01", sql)
        for number in range(2, 6):
            self.assertIn(f"ix_tabela_origem_01_{number:02d}", sql)

    def test_index_moment_separates_base_and_secondary(self):
        landing = landing_layout()
        initial = build_ddl_plan(
            landing,
            stage=DdlStage.INITIAL,
            index_moment=IndexBuildMoment.AFTER_TABLE_LOAD,
        )
        self.assertEqual(initial.included_indexes, ("ix_tabela_origem_01_01",))
        completion = build_ddl_plan(landing, stage=DdlStage.SECONDARY_INDEXES)
        self.assertEqual(
            completion.included_indexes,
            ("ix_tabela_origem_01_02", "ix_tabela_origem_01_03", "ix_tabela_origem_01_04", "ix_tabela_origem_01_05"),
        )
        before = build_ddl_plan(
            landing,
            stage=DdlStage.INITIAL,
            index_moment=IndexBuildMoment.BEFORE_LOAD,
        )
        self.assertEqual(len(before.included_indexes), 5)


class CatalogComparisonTests(unittest.TestCase):
    def test_brasilia_timestamp_matches_sql_server_catalog_parentheses(self):
        from bcp_engine.ddl import normalize_sql_expression

        catalog = (
            "(CONVERT([datetime2](7),((sysutcdatetime() AT TIME ZONE 'UTC') "
            "AT TIME ZONE 'E. South America Standard Time')))"
        )
        self.assertEqual(
            normalize_sql_expression(BRASILIA_TIMESTAMP_SQL),
            normalize_sql_expression(catalog),
        )

    def test_sql_server_parentheses_around_case_constants_are_equivalent(self):
        from bcp_engine.ddl import normalize_sql_expression

        declared = "CASE [x] WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END"
        catalog = "(case [x] when (1) then 'a' when (2) then 'b' else 'c' end)"
        self.assertEqual(
            normalize_sql_expression(declared),
            normalize_sql_expression(catalog),
        )

    def test_sql_server_cast_catalog_rewrite_is_equivalent(self):
        from bcp_engine.ddl import normalize_sql_expression

        self.assertEqual(
            normalize_sql_expression("CAST([aud_ccid] AS BINARY(10))"),
            normalize_sql_expression("(CONVERT([binary](10),[aud_ccid]))"),
        )

    def test_exact_catalog_is_compatible(self):
        layout = bronze_layout()
        comparison = compare_catalog(layout, catalog_for(layout))
        self.assertEqual(comparison.state, CatalogState.COMPATIBLE)
        self.assertTrue(comparison.compatible)

    def test_exact_partitioned_catalog_is_compatible(self):
        for layout in (
            bronze_layout(partitioned=True),
            landing_layout(partitioned=True),
        ):
            with self.subTest(profile=layout.profile_name):
                comparison = compare_catalog(layout, catalog_for(layout))
                self.assertEqual(comparison.state, CatalogState.COMPATIBLE)
                self.assertTrue(comparison.compatible)

    def test_partitioned_contract_refuses_implicit_migration(self):
        layout = bronze_layout(partitioned=True)
        catalog = catalog_for(layout)
        catalog["partition"] = None

        comparison = compare_catalog(layout, catalog)

        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertFalse(comparison.provisionable)
        self.assertTrue(
            any("migracao automatica destrutiva recusada" in error for error in comparison.errors),
            comparison.errors,
        )

    def test_nonpartitioned_contract_refuses_existing_partitioned_table(self):
        layout = bronze_layout()
        catalog = catalog_for(layout)
        partitioned = bronze_layout(partitioned=True)
        catalog["partition"] = catalog_for(partitioned)["partition"]

        comparison = compare_catalog(layout, catalog)

        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(
            any("contrato nao esta" in error for error in comparison.errors),
            comparison.errors,
        )

    def test_partition_catalog_and_index_placement_are_fail_closed(self):
        layout = bronze_layout(partitioned=True)
        mutations = (
            ("partition", "scheme_name", "ps_incorreto", "partition scheme"),
            ("partition", "function_name", "pf_incorreta", "partition function"),
            ("partition", "partition_column", "dh_atualizacao", "coluna de particionamento"),
            ("partition", "scale", 6, "DATETIME2(7)"),
            ("partition", "boundary_on_right", False, "RANGE RIGHT"),
            ("partition", "filegroups", ["SECONDARY"], "PRIMARY"),
            ("primary_key", "data_space_name", layout.partition.scheme_name, "nao alinhado"),
            ("clustered", "partition_column", "dh_atualizacao", "coluna de particionamento"),
        )
        clustered_name = layout.partition.clustered_index_name
        for target, field, value, diagnostic in mutations:
            with self.subTest(target=target, field=field):
                catalog = catalog_for(layout)
                if target == "partition":
                    catalog["partition"][field] = value
                elif target == "primary_key":
                    catalog["primary_key"][field] = value
                else:
                    clustered = next(
                        index
                        for index in catalog["indexes"]
                        if index["name"] == clustered_name
                    )
                    clustered[field] = value
                comparison = compare_catalog(layout, catalog)
                self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
                self.assertTrue(
                    any(diagnostic in error for error in comparison.errors),
                    comparison.errors,
                )

    def test_partition_catalog_requires_every_month_in_current_horizon(self):
        layout = bronze_layout(partitioned=True)
        catalog = catalog_for(layout)
        first = catalog["partition"]["boundaries"].pop(0)

        comparison = compare_catalog(layout, catalog)

        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(
            any(first[:10] in error for error in comparison.errors),
            comparison.errors,
        )

    def test_trailing_partition_horizon_is_idempotently_provisionable(self):
        layout = bronze_layout(partitioned=True)
        catalog = catalog_for(layout)
        last = catalog["partition"]["boundaries"].pop()

        comparison = compare_catalog(layout, catalog)

        self.assertEqual(comparison.state, CatalogState.OBJECTS_PENDING)
        self.assertTrue(comparison.provisionable)
        self.assertEqual(comparison.errors, ())
        self.assertIn(
            f"PARTITION_BOUNDARY:{layout.partition.function_name}:{last[:19]}",
            comparison.missing_objects,
        )

    def test_schema_and_table_case_must_be_canonical_lowercase(self):
        layout = bronze_layout()
        catalog = catalog_for(layout)
        catalog["schema"] = "S344"
        catalog["table"] = "BD_ORIGEM_TABELA_ORIGEM_01"
        comparison = compare_catalog(layout, catalog)
        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(any("schema do destino" in error for error in comparison.errors))
        self.assertTrue(any("tabela do destino" in error for error in comparison.errors))

    def test_index_case_must_be_canonical_lowercase(self):
        layout = landing_layout()
        catalog = catalog_for(layout)
        catalog["indexes"][1]["name"] = catalog["indexes"][1]["name"].upper()

        comparison = compare_catalog(layout, catalog)

        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(
            any("fora do padrao lowercase" in error for error in comparison.errors)
        )
        self.assertFalse(
            any(
                item == f"INDEX:{layout.indexes[1].name}"
                for item in comparison.missing_objects
            )
        )

    def test_column_default_and_index_direction_divergences_fail(self):
        layout = landing_layout()
        catalog = catalog_for(layout)
        catalog["columns"][8]["default_expression"] = "(GETDATE())"
        catalog["indexes"][4]["keys"][0]["direction"] = "ASC"
        comparison = compare_catalog(layout, catalog)
        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(any("DEFAULT" in error for error in comparison.errors))
        self.assertTrue(any("direções" in error for error in comparison.errors))

    def test_missing_post_load_indexes_has_resume_state(self):
        layout = landing_layout()
        catalog = catalog_for(layout)
        catalog["indexes"] = catalog["indexes"][:1]
        comparison = compare_catalog(layout, catalog, data_completed=True)
        self.assertEqual(comparison.state, CatalogState.DATA_COMPLETE_INDEXES_PENDING)
        self.assertEqual(len(comparison.missing_objects), 4)

    def test_other_clustered_index_is_never_replaced(self):
        layout = landing_layout()
        catalog = catalog_for(layout)
        catalog["indexes"] = [
            index for index in catalog["indexes"] if index["name"] != "ix_tabela_origem_01_01"
        ]
        catalog["indexes"].append(
            {
                "name": "clustered_externo",
                "keys": [{"name": "codigo", "direction": "ASC"}],
                "unique": False,
                "clustered": True,
                "includes": [],
                "filter_predicate": None,
                "disabled": False,
                "primary_key": False,
            }
        )
        comparison = compare_catalog(layout, catalog)
        self.assertEqual(comparison.state, CatalogState.CLUSTERED_CONFLICT)
        self.assertFalse(comparison.provisionable)

    def test_missing_sequence_on_populated_bronze_requires_migration(self):
        layout = bronze_layout()
        catalog = catalog_for(layout)
        catalog["sequence"] = None
        catalog["row_count"] = 10
        comparison = compare_catalog(layout, catalog)
        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertIn("migração explícita", comparison.errors[0])


    def test_hypothetical_index_never_satisfies_physical_contract(self):
        layout = landing_layout()
        catalog = catalog_for(layout)
        catalog["indexes"][0]["is_hypothetical"] = True
        comparison = compare_catalog(layout, catalog)
        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(any("hipotetico" in error for error in comparison.errors))

    def test_sequence_requires_complete_start_increment_and_cycle_contract(self):
        layout = bronze_layout()
        mutations = (
            ("start", None, "valor inicial"),
            ("start", 2, "valor inicial"),
            ("increment", None, "incremento"),
            ("cycle", None, "CYCLE/NO CYCLE"),
        )
        for field, value, diagnostic in mutations:
            with self.subTest(field=field, value=value):
                catalog = catalog_for(layout)
                if value is None:
                    del catalog["sequence"][field]
                else:
                    catalog["sequence"][field] = value
                comparison = compare_catalog(layout, catalog)
                self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
                self.assertTrue(
                    any(diagnostic in error for error in comparison.errors),
                    comparison.errors,
                )


if __name__ == "__main__":
    unittest.main()
