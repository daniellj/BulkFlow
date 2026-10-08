from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from bcp_engine.prerequisites import inspect_prerequisites


class PrerequisiteTests(unittest.TestCase):
    def test_ready_report_contains_driver_bcp_path_and_version(self):
        pyodbc = types.SimpleNamespace(
            drivers=lambda: ["SQL Server", "ODBC Driver 18 for SQL Server"]
        )
        with patch.dict(sys.modules, {"pyodbc": pyodbc}), patch(
            "bcp_engine.prerequisites.resolve_bcp_executable",
            return_value=r"C:\tools\bcp.exe",
        ), patch(
            "bcp_engine.prerequisites.BcpRunner.require_version", return_value="18.5.1"
        ):
            report = inspect_prerequisites(
                {
                    "odbc_driver": "ODBC Driver 18 for SQL Server",
                    "bcp_executable": "bcp",
                }
            )
        self.assertTrue(report.ready)
        self.assertEqual(report.bcp_version, "18.5.1")
        self.assertEqual(report.bcp_resolved_path, r"C:\tools\bcp.exe")
        self.assertNotIn("password", str(report.as_dict()).casefold())

    def test_missing_native_components_fail_without_mutating_host(self):
        pyodbc = types.SimpleNamespace(drivers=lambda: ["SQL Server"])
        with patch.dict(sys.modules, {"pyodbc": pyodbc}), patch(
            "bcp_engine.prerequisites.resolve_bcp_executable", return_value=None
        ):
            report = inspect_prerequisites(
                {
                    "odbc_driver": "ODBC Driver 18 for SQL Server",
                    "bcp_executable": "bcp",
                }
            )
        self.assertFalse(report.ready)
        self.assertTrue(any(item.startswith("ODBC_DRIVER_NOT_INSTALLED") for item in report.errors))
        self.assertTrue(any(item.startswith("BCP_NOT_FOUND") for item in report.errors))


if __name__ == "__main__":
    unittest.main()
