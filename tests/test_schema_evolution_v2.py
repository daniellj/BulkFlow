from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.config import (  # noqa: E402
    ConfigError,
    operational_fingerprint,
    structural_fingerprint,
    validate_config,
)
from bcp_engine.ddl import (  # noqa: E402
    CatalogState,
    SchemaEvolutionRequiredError,
    SchemaEvolutionSafetyError,
    analyze_schema_evolution,
    build_apply_batches,
    build_schema_evolution_batches,
    compare_catalog,
)
from bcp_engine.engine import BcpEngine  # noqa: E402
from bcp_engine.profiles import (  # noqa: E402
    ColumnDefinition,
    FillRule,
    IndexDefinition,
    IndexKey,
    PartitionDefinition,
    TableLayout,
)
from bcp_engine.util import target_lock_resource  # noqa: E402


def layout(*, new_nullable: bool = True) -> TableLayout:
    columns = (
        ColumnDefinition("id_target", "BIGINT", False, role="technical"),
        ColumnDefinition(
            "code", "INT", False, role="business", source_name="code"
        ),
        ColumnDefinition(
            "new_note",
            "VARCHAR(50)",
            new_nullable,
            collation="Latin1_General_100_CI_AS",
            role="business",
            source_name="new_note",
        ),
        ColumnDefinition("loaded_at", "DATETIME2(7)", True, role="metadata"),
    )
    return TableLayout(
        profile_name="bronze",
        profile_version=1,
        schema="s344",
        table="bd_origem_sample",
        source_database="bd_origem",
        source_table="sample",
        columns=columns,
        primary_key=IndexDefinition(
            "pk_bd_origem_sample",
            (IndexKey("id_target", "ASC"),),
            unique=True,
            clustered=True,
            primary_key=True,
            phase="base",
        ),
        indexes=(),
        sequence=None,
        technical_id_strategy="source_column",
        technical_id_source_column="code",
        fill_rules=tuple(
            FillRule(column.name, "test") for column in columns
        ),
    )


def partitioned_layout(*, new_nullable: bool = True) -> TableLayout:
    base = layout(new_nullable=new_nullable)
    partition = PartitionDefinition(
        column="loaded_at",
        function_name="pf_loaded_at_attr",
        scheme_name="ps_loaded_at_attr",
        clustered_index_name="ix_bd_origem_sample_loaded_at_clustered",
        descriptor="attr",
    )
    return replace(
        base,
        primary_key=replace(base.primary_key, clustered=False),
        indexes=(
            IndexDefinition(
                partition.clustered_index_name,
                (IndexKey(partition.column, "ASC"),),
                clustered=True,
                phase="base",
            ),
        ),
        partition=partition,
    )
def _column_record(column: ColumnDefinition, ordinal: int) -> dict:
    return {
        "column_id": ordinal,
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


def catalog_for(
    value: TableLayout,
    names: list[str] | None = None,
    *,
    row_count: int | None = 0,
) -> dict:
    by_name = {column.name: column for column in value.columns}
    selected = names or [column.name for column in value.columns]
    primary = {
        "name": value.primary_key.name,
        "keys": [
            {"name": key.name, "direction": key.direction}
            for key in value.primary_key.keys
        ],
        "unique": True,
        "clustered": True,
        "includes": [],
        "filter_predicate": None,
        "disabled": False,
        "primary_key": True,
    }
    return {
        "exists": True,
        "object_type": "U",
        "row_count": row_count,
        "columns": [
            _column_record(by_name[name], ordinal)
            for ordinal, name in enumerate(selected, start=1)
        ],
        "primary_key": primary,
        "indexes": [],
        "sequence": None,
    }


class SchemaEvolutionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(
            (ROOT / "examples" / "config.full.json").read_text(encoding="utf-8-sig")
        )

    def test_default_false_boolean_validation_and_fingerprints(self):
        self.raw.pop("allow_schema_evolution", None)
        base = validate_config(self.raw)
        self.assertIs(base["allow_schema_evolution"], False)

        enabled_raw = dict(self.raw)
        enabled_raw["allow_schema_evolution"] = True
        enabled = validate_config(enabled_raw)
        self.assertIs(enabled["allow_schema_evolution"], True)
        self.assertEqual(structural_fingerprint(base), structural_fingerprint(enabled))
        self.assertNotEqual(
            operational_fingerprint(base), operational_fingerprint(enabled)
        )

        invalid = dict(self.raw)
        invalid["allow_schema_evolution"] = "true"
        with self.assertRaises(ConfigError):
            validate_config(invalid)

    def test_published_schema_declares_the_safe_default(self):
        schema = json.loads(
            (ROOT / "schemas" / "config-v2.schema.json").read_text(encoding="utf-8")
        )
        definition = schema["properties"]["allow_schema_evolution"]
        self.assertEqual(definition, {
            "type": "boolean",
            "default": False,
            "description": definition["description"],
        })


class SchemaEvolutionDdlTests(unittest.TestCase):
    def test_partitioned_evolution_refuses_implicit_repartitioning(self):
        sql = build_schema_evolution_batches(partitioned_layout())[0]

        self.assertIn("THROW 51050", sql)
        self.assertIn("ps_loaded_at_attr", sql)
        self.assertIn("partition_ordinal = 1", sql)
        self.assertIn("ALTER TABLE [s344].[bd_origem_sample] ADD [new_note]", sql)
        self.assertNotRegex(sql, r"(?i)\b(?:DROP|SWITCH|ALTER\s+PARTITION)\b")

    def test_only_missing_business_columns_are_evolution_candidates(self):
        expected = layout()
        snapshot = catalog_for(expected, ["id_target", "code", "loaded_at"])
        comparison = compare_catalog(expected, snapshot)
        analysis = analyze_schema_evolution(expected, snapshot)

        self.assertEqual(comparison.state, CatalogState.SCHEMA_EVOLUTION_PENDING)
        self.assertEqual(comparison.missing_business_columns, ("new_note",))
        self.assertEqual(
            [item.column.name for item in analysis.missing_columns], ["new_note"]
        )
        self.assertEqual(analysis.missing_columns[0].logical_ordinal, 3)
        self.assertTrue(analysis.safe)

        without_technical = catalog_for(
            expected, ["code", "new_note", "loaded_at"]
        )
        self.assertEqual(
            compare_catalog(expected, without_technical).state,
            CatalogState.INCOMPATIBLE,
        )

    def test_not_null_is_rejected_for_populated_or_unknown_table(self):
        expected = layout(new_nullable=False)
        populated = catalog_for(
            expected, ["id_target", "code", "loaded_at"], row_count=3
        )
        unknown = catalog_for(
            expected, ["id_target", "code", "loaded_at"], row_count=None
        )
        self.assertEqual(
            analyze_schema_evolution(expected, populated).unsafe_not_null_columns,
            ("new_note",),
        )
        self.assertEqual(
            analyze_schema_evolution(expected, unknown).unsafe_not_null_columns,
            ("new_note",),
        )

    def test_source_spelling_is_mapped_case_insensitively_to_lowercase_target(self):
        expected = layout()
        uppercase_business = replace(
            expected.columns[2], name="NEW_NOTE", source_name="NEW_NOTE"
        )
        expected = replace(
            expected,
            columns=(
                expected.columns[0],
                expected.columns[1],
                uppercase_business,
                expected.columns[3],
            ),
        )
        snapshot = catalog_for(
            layout(), ["id_target", "code", "loaded_at"]
        )
        comparison = compare_catalog(expected, snapshot)
        analysis = analyze_schema_evolution(expected, snapshot)
        self.assertEqual(comparison.missing_business_columns, ("new_note",))
        self.assertEqual(analysis.missing_columns[0].column.name, "new_note")
        sql = build_schema_evolution_batches(expected)[0]
        self.assertIn("ADD [new_note] VARCHAR(50)", sql)
        self.assertNotIn("ADD [NEW_NOTE]", sql)

    def test_existing_case_variant_is_not_added_and_is_reported_noncanonical(self):
        expected = layout()
        snapshot = catalog_for(expected)
        snapshot["columns"][2]["name"] = "NEW_NOTE"

        analysis = analyze_schema_evolution(expected, snapshot)
        comparison = compare_catalog(expected, snapshot)

        self.assertFalse(analysis.required)
        self.assertEqual(comparison.state, CatalogState.INCOMPATIBLE)
        self.assertTrue(
            any("lowercase" in error for error in comparison.errors),
            comparison.errors,
        )

    def test_physical_ordinal_drift_is_audited_but_named_layout_is_compatible(self):
        expected = layout()
        appended = catalog_for(
            expected, ["id_target", "code", "loaded_at", "new_note"]
        )
        comparison = compare_catalog(expected, appended)
        self.assertEqual(comparison.state, CatalogState.COMPATIBLE)
        self.assertTrue(any("Ordem fisica" in item for item in comparison.warnings))

        appended["columns"][-1]["sql_type"] = "VARCHAR(49)"
        self.assertEqual(
            compare_catalog(expected, appended).state,
            CatalogState.INCOMPATIBLE,
        )

    def test_offline_and_apply_batches_are_additive_atomic_and_share_import_lock(self):
        expected = layout(new_nullable=False)
        disabled = build_apply_batches(expected)[0]
        enabled = build_apply_batches(
            expected, allow_schema_evolution=True
        )[0]
        evolution = build_schema_evolution_batches(expected)[0]
        resource = target_lock_resource(expected.schema, expected.table)

        self.assertNotIn(
            "ALTER TABLE [s344].[bd_origem_sample] ADD [new_note]", disabled
        )
        self.assertIn("allow_schema_evolution esta desabilitado", disabled)
        for sql in (enabled, evolution):
            self.assertIn(resource, sql)
            self.assertIn("BEGIN TRANSACTION", sql)
            self.assertIn("ROLLBACK TRANSACTION", sql)
            self.assertIn("TABLOCKX,HOLDLOCK", sql)
            self.assertIn(
                "ALTER TABLE [s344].[bd_origem_sample] ADD [new_note] VARCHAR(50)",
                sql,
            )
            self.assertNotRegex(sql, r"(?i)\b(?:DROP|RENAME|ALTER\s+COLUMN)\b")
        self.assertNotIn("ADD [id_target]", evolution)
        self.assertNotIn("ADD [loaded_at]", evolution)


class SchemaEvolutionEngineTests(unittest.TestCase):
    def engine(self, *, allow: bool, events: list[tuple[str, dict]]) -> BcpEngine:
        return BcpEngine(
            {
                "allow_schema_evolution": allow,
                "bronze_destination": {"database": "DBRO684"},
            },
            event_sink=lambda name, payload: events.append((name, dict(payload))),
        )

    def test_disabled_and_unsafe_paths_emit_structured_block_events(self):
        events: list[tuple[str, dict]] = []
        expected = layout()
        snapshot = catalog_for(expected, ["id_target", "code", "loaded_at"])
        comparison = compare_catalog(expected, snapshot)
        with self.assertRaises(SchemaEvolutionRequiredError):
            self.engine(allow=False, events=events)._preflight_schema_evolution(
                area="bronze",
                layout=expected,
                catalog=snapshot,
                comparison=comparison,
            )
        self.assertEqual(
            [name for name, _payload in events],
            ["schema_evolution_detected", "schema_evolution_blocked"],
        )
        self.assertEqual(events[-1][1]["reason"], "allow_schema_evolution_disabled")
        self.assertEqual(events[-1][1]["columns"][0]["name"], "new_note")

        events.clear()
        required = layout(new_nullable=False)
        populated = catalog_for(
            required, ["id_target", "code", "loaded_at"], row_count=2
        )
        with self.assertRaises(SchemaEvolutionSafetyError):
            self.engine(allow=True, events=events)._preflight_schema_evolution(
                area="bronze",
                layout=required,
                catalog=populated,
                comparison=compare_catalog(required, populated),
            )
        self.assertEqual(
            events[-1][1]["reason"],
            "not_null_column_on_populated_or_unknown_table",
        )

    def test_success_reinspects_and_audits_physical_ordinal(self):
        events: list[tuple[str, dict]] = []
        expected = layout()
        before = catalog_for(expected, ["id_target", "code", "loaded_at"])
        after = catalog_for(
            expected, ["id_target", "code", "loaded_at", "new_note"]
        )
        engine = self.engine(allow=True, events=events)
        with (
            patch("bcp_engine.engine.execute") as execute_mock,
            patch(
                "bcp_engine.inspection.inspect_layout_catalog",
                return_value=after,
            ),
        ):
            _catalog, comparison = engine._apply_schema_evolution(
                object(),
                area="bronze",
                layout=expected,
                catalog=before,
                comparison=compare_catalog(expected, before),
            )
        self.assertEqual(comparison.state, CatalogState.COMPATIBLE)
        execute_mock.assert_called_once()
        applied = [payload for name, payload in events if name == "schema_evolution_applied"]
        self.assertEqual(applied[0]["columns"][0]["physical_ordinal_after"], 4)
        self.assertTrue(applied[0]["physical_order_differs"])


if __name__ == "__main__":
    unittest.main()
