from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bcp_engine.engine as engine_module  # noqa: E402
from bcp_engine.ddl import CatalogState  # noqa: E402
from bcp_engine.engine import BcpEngine  # noqa: E402


def comparison(state: CatalogState, *missing: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        errors=(),
        missing_objects=tuple(missing),
    )


def role_engine(role: str = "data_only", area: str = "landing") -> BcpEngine:
    engine = BcpEngine.__new__(BcpEngine)
    engine.config = {
        "active_destination": area,
        "execute_import": True,
        "bronze_destination": {"role": "structure_only"},
        "landing_destination": {"role": "structure_only"},
        "structure": {"secondary_indexes_phase": "after_table_load"},
        "create_structure_if_needed": True,
        "allow_schema_evolution": True,
    }
    engine.config[area + "_destination"]["role"] = role
    engine.config_directory = ROOT
    engine.event = lambda *_args: None
    return engine


class DestinationRoleEngineTests(unittest.TestCase):
    def test_destination_control_is_read_only_for_data_only_role(self):
        engine = role_engine()
        importer = Mock()

        engine._prepare_destination_control(importer, "landing")

        importer.validate_control.assert_called_once_with()
        importer.ensure_control.assert_not_called()

    def test_destination_control_is_ensured_when_structure_is_authorized(self):
        engine = role_engine("structure_and_data", "bronze")
        importer = Mock()

        engine._prepare_destination_control(importer, "bronze")

        importer.ensure_control.assert_called_once_with()
        importer.validate_control.assert_not_called()

    def test_data_only_initial_provision_accepts_only_complete_layout_without_ddl(self):
        engine = role_engine()
        layout = SimpleNamespace(profile_name="landing")
        engine._apply_schema_evolution = Mock(
            side_effect=AssertionError("schema evolution is forbidden")
        )

        with (
            patch(
                "bcp_engine.inspection.inspect_layout_catalog",
                return_value={"exists": True},
            ),
            patch.object(
                engine_module,
                "compare_catalog",
                return_value=comparison(CatalogState.COMPATIBLE),
            ) as compare,
            patch.object(engine_module, "build_apply_batches") as batches,
            patch.object(engine_module, "execute") as execute,
        ):
            engine._provision_initial(object(), layout, area="landing")

        self.assertTrue(compare.call_args.kwargs["include_secondary"])
        engine._apply_schema_evolution.assert_not_called()
        batches.assert_not_called()
        execute.assert_not_called()

    def test_data_only_initial_provision_rejects_incomplete_layout_without_ddl(self):
        engine = role_engine()
        layout = SimpleNamespace(profile_name="landing")
        engine._apply_schema_evolution = Mock(
            side_effect=AssertionError("schema evolution is forbidden")
        )

        with (
            patch(
                "bcp_engine.inspection.inspect_layout_catalog",
                return_value={"exists": True},
            ),
            patch.object(
                engine_module,
                "compare_catalog",
                return_value=comparison(
                    CatalogState.INDEXES_PENDING, "INDEX:ix_required"
                ),
            ),
            patch.object(engine_module, "build_apply_batches") as batches,
            patch.object(engine_module, "execute") as execute,
            self.assertRaisesRegex(RuntimeError, "incluindo índices"),
        ):
            engine._provision_initial(object(), layout, area="landing")

        engine._apply_schema_evolution.assert_not_called()
        batches.assert_not_called()
        execute.assert_not_called()

    def test_data_only_finalization_never_creates_missing_indexes(self):
        engine = role_engine()
        layout = SimpleNamespace(profile_name="landing")

        with (
            patch(
                "bcp_engine.inspection.inspect_layout_catalog",
                return_value={"exists": True},
            ),
            patch.object(
                engine_module,
                "compare_catalog",
                return_value=comparison(
                    CatalogState.DATA_COMPLETE_INDEXES_PENDING,
                    "INDEX:ix_required",
                ),
            ),
            patch.object(engine_module, "build_apply_batches") as batches,
            patch.object(engine_module, "execute") as execute,
            self.assertRaisesRegex(RuntimeError, "não permite criar índices"),
        ):
            engine._finish_indexes(object(), layout, area="landing")

        batches.assert_not_called()
        execute.assert_not_called()

    def test_data_only_binding_validates_before_sql_control_registration(self):
        engine = role_engine()
        engine._provision_initial = Mock(side_effect=RuntimeError("layout incomplete"))
        engine._destination_table_exists = Mock(
            side_effect=AssertionError("existence shortcut must not be used")
        )
        importer = Mock()

        with self.assertRaisesRegex(RuntimeError, "layout incomplete"):
            engine._bind_and_provision(
                object(),
                importer,
                execution_id="execution",
                dataset_id="dataset",
                structural_hash="structural",
                area="landing",
                table_id="table",
                layout=SimpleNamespace(schema="dst", table="target"),
                layout_hash="layout",
                projection_hash="projection",
                final_limit_json="[]",
            )

        importer.register_execution_table.assert_not_called()
        engine._destination_table_exists.assert_not_called()

    def test_generate_ddl_rejects_data_only_before_files_or_connections(self):
        engine = role_engine()
        engine._connect_source = Mock(
            side_effect=AssertionError("source connection is forbidden")
        )
        engine._connect_destination = Mock(
            side_effect=AssertionError("destination connection is forbidden")
        )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "not-created"
            with self.assertRaisesRegex(RuntimeError, "não autoriza operações de estrutura"):
                engine.generate_ddl(
                    areas=["landing"],
                    output_directory=output,
                    apply=True,
                )
            self.assertFalse(output.exists())

        engine._connect_source.assert_not_called()
        engine._connect_destination.assert_not_called()


if __name__ == "__main__":
    unittest.main()
