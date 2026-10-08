from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.gui_model import build_disk_projection_summary  # noqa: E402


def _config(
    export_path: str = "artifact-view",
    import_path: str = "artifact-view",
    *,
    minimum_free_bytes: int = 100,
    safety_factor: float = 1.25,
) -> dict:
    return {
        "executor_directory": export_path,
        "destination_sql_directory": import_path,
        "minimum_free_space_bytes": minimum_free_bytes,
        "estimates": {"safety_factor": safety_factor},
    }


def _plan(
    table: str,
    raw: int | None,
    protected: int | None,
    peak: int | None,
    *,
    status: str = "PENDING",
) -> dict:
    return {
        "source": {"database": "BD_ORIGEM", "schema": "dbo", "table": table},
        "status": status,
        "estimated_rows": 10,
        "estimated_bcp_total_bytes": raw,
        "planning_reserve_bytes": protected,
        "predicted_peak_bytes": peak,
    }


class GuiDiskProjectionTests(unittest.TestCase):
    def test_consolidates_every_configured_table_and_keeps_status_detail(self):
        provider_calls: list[str] = []

        def disk_usage(path):
            provider_calls.append(str(path))
            return SimpleNamespace(free=5_000)

        summary = build_disk_projection_summary(
            _config(),
            [
                _plan("T1", 1_000, 1_250, 300),
                _plan("T2", 2_000, 2_500, 800),
                _plan("IGNORADA", 9_000, 11_250, 9_000, status="SKIPPED_NO_KEY"),
            ],
            disk_usage_provider=disk_usage,
        )

        self.assertEqual(summary["included_table_count"], 3)
        self.assertEqual(summary["unknown_table_count"], 0)
        self.assertEqual(summary["total_raw_bytes"], 12_000)
        self.assertEqual(summary["total_protected_bytes"], 15_000)
        self.assertEqual(summary["total_margin_bytes"], 3_000)
        self.assertEqual(summary["predicted_peak_bytes"], 9_000)
        self.assertEqual(summary["known_raw_bytes"], 12_000)
        self.assertEqual(summary["tables"][0]["source"], "BD_ORIGEM.dbo.T1")
        self.assertEqual(summary["tables"][1]["margin_bytes"], 500)
        self.assertEqual(summary["tables"][2]["projection_state"], "available")
        self.assertEqual(summary["tables"][2]["status"], "SKIPPED_NO_KEY")
        self.assertTrue(summary["same_path_view"])
        self.assertEqual(len(provider_calls), 1)
        for role in ("export", "import"):
            assessment = summary["directories"][role]
            self.assertEqual(assessment["state"], "insufficient")
            self.assertEqual(assessment["required_peak_bytes"], 9_000)
            self.assertEqual(assessment["balance_after_peak_bytes"], -4_100)
            self.assertEqual(
                assessment["balance_after_total_protected_bytes"], -10_100
            )
            self.assertFalse(assessment["total_protected_fits"])

    def test_unknown_pending_table_never_becomes_a_false_zero_or_approval(self):
        summary = build_disk_projection_summary(
            _config(),
            [
                _plan("CONHECIDA", 1_000, 1_250, 400),
                _plan("DESCONHECIDA", None, None, None),
            ],
            disk_usage_provider=lambda _path: SimpleNamespace(free=50_000),
        )

        self.assertEqual(summary["unknown_table_count"], 1)
        self.assertEqual(summary["known_raw_bytes"], 1_000)
        self.assertEqual(summary["known_protected_bytes"], 1_250)
        self.assertIsNone(summary["total_raw_bytes"])
        self.assertIsNone(summary["total_protected_bytes"])
        self.assertIsNone(summary["total_margin_bytes"])
        self.assertIsNone(summary["predicted_peak_bytes"])
        self.assertEqual(summary["directories"]["export"]["state"], "unavailable")
        self.assertIsNone(
            summary["directories"]["export"]["balance_after_peak_bytes"]
        )

    def test_assesses_distinct_export_and_import_views_independently(self):
        free_by_path = {"export-view": 900, "import-view": 400}

        summary = build_disk_projection_summary(
            _config("export-view", "import-view"),
            [_plan("T1", 1_000, 1_250, 500)],
            disk_usage_provider=lambda path: SimpleNamespace(
                free=free_by_path[str(path)]
            ),
        )

        self.assertFalse(summary["same_path_view"])
        export = summary["directories"]["export"]
        imported = summary["directories"]["import"]
        self.assertEqual(export["state"], "sufficient")
        self.assertEqual(export["balance_after_peak_bytes"], 300)
        self.assertEqual(export["balance_after_total_protected_bytes"], -450)
        self.assertFalse(export["total_protected_fits"])
        self.assertEqual(imported["state"], "insufficient")
        self.assertEqual(imported["balance_after_peak_bytes"], -200)
        self.assertEqual(imported["balance_after_total_protected_bytes"], -950)
        self.assertTrue(summary["paths_are_artifact_views"])

    def test_inaccessible_path_is_unavailable_instead_of_zero_bytes(self):
        summary = build_disk_projection_summary(
            _config("export-view", "sql-only-view"),
            [_plan("T1", 100, 125, 100)],
            disk_usage_provider=lambda path: (
                SimpleNamespace(free=1_000)
                if str(path) == "export-view"
                else (_ for _ in ()).throw(OSError("not mounted"))
            ),
        )

        imported = summary["directories"]["import"]
        self.assertIsNone(imported["free_bytes"])
        self.assertEqual(imported["state"], "unavailable")
        self.assertIsNone(imported["balance_after_peak_bytes"])
        self.assertIn("not mounted", imported["error"])

    def test_missing_table_reserve_uses_per_table_ceiling(self):
        summary = build_disk_projection_summary(
            _config(minimum_free_bytes=0, safety_factor=1.25),
            [
                _plan("T1", 1, None, 2),
                _plan("T2", 1, None, 2),
            ],
            disk_usage_provider=lambda _path: SimpleNamespace(free=100),
        )

        # Cada parcela recebe CEILING(1 * 1,25) antes da soma: 2 + 2, não 3.
        self.assertEqual(summary["total_raw_bytes"], 2)
        self.assertEqual(summary["total_protected_bytes"], 4)
        self.assertEqual(summary["total_margin_bytes"], 2)

    def test_known_table_cost_is_preserved_when_only_operational_peak_is_unknown(self):
        summary = build_disk_projection_summary(
            _config(),
            [_plan("T1", 1_000, 1_250, None)],
            disk_usage_provider=lambda _path: SimpleNamespace(free=10_000),
        )

        self.assertEqual(summary["unknown_table_count"], 0)
        self.assertEqual(summary["unknown_peak_table_count"], 1)
        self.assertEqual(summary["total_raw_bytes"], 1_000)
        self.assertEqual(summary["total_protected_bytes"], 1_250)
        self.assertIsNone(summary["predicted_peak_bytes"])
        self.assertEqual(summary["tables"][0]["projection_state"], "available")
        self.assertEqual(summary["tables"][0]["peak_state"], "unavailable")
        self.assertEqual(summary["directories"]["export"]["state"], "unavailable")

    def test_export_only_does_not_measure_or_assess_import_directory(self):
        calls: list[str] = []
        config = _config("export-view", "sql-only-view")
        config["execute_import"] = False

        summary = build_disk_projection_summary(
            config,
            [_plan("T1", 100, 125, 100)],
            disk_usage_provider=lambda path: (
                calls.append(str(path)) or SimpleNamespace(free=1_000)
            ),
        )

        self.assertEqual(calls, ["export-view"])
        imported = summary["directories"]["import"]
        self.assertEqual(imported["state"], "not_applicable")
        self.assertIsNone(imported["free_bytes"])
        self.assertIsNone(imported["total_protected_fits"])


if __name__ == "__main__":
    unittest.main()
