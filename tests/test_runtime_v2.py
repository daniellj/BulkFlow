from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from bcp_engine.runtime import (
    ensure_runtime_directories,
    resolve_bcp_executable,
    runtime_config_directory,
    runtime_control_directory,
    runtime_data_root,
    runtime_ddl_directory,
    runtime_export_directory,
)


class RuntimePathTests(unittest.TestCase):
    def test_source_execution_preserves_explicit_project_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root.resolve() / "Local" / "BulkFlow"
            self.assertEqual(runtime_data_root(root), expected)
            self.assertEqual(
                runtime_control_directory(root), expected / ".bcp-control"
            )
            self.assertEqual(runtime_export_directory(root), expected / "bcp-data")
            self.assertEqual(runtime_ddl_directory(root), expected / "ddl")
            self.assertEqual(runtime_config_directory(root), root.resolve())

    def test_default_source_directories_are_created_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected_root = root.resolve() / "Local" / "BulkFlow"
            expected = (
                expected_root / ".bcp-control",
                expected_root / "bcp-data",
                expected_root / "ddl",
            )
            self.assertEqual(ensure_runtime_directories(root), expected)
            self.assertEqual(ensure_runtime_directories(root), expected)
            self.assertTrue(all(path.is_dir() for path in expected))

    def test_frozen_execution_uses_local_app_data_for_every_mutable_area(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_app_data = Path(temporary) / "Local"
            with patch.object(sys, "frozen", True, create=True):
                root = runtime_data_root(
                    environment={"LOCALAPPDATA": str(local_app_data)}
                )
                with patch.dict(
                    "os.environ", {"LOCALAPPDATA": str(local_app_data)}, clear=False
                ):
                    ddl = runtime_ddl_directory()
                    config = runtime_config_directory()
        expected = (local_app_data / "BulkFlow").resolve()
        self.assertEqual(root, expected)
        self.assertEqual(ddl, expected / "ddl")
        self.assertEqual(config, expected / "config")

    def test_frozen_release_in_source_tree_uses_exact_project_local_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary) / "BulkFlow"
            release_directory = project_root / "release"
            release_directory.mkdir(parents=True)
            (project_root / "bcp_bronze.py").touch()
            (project_root / "templates").mkdir()
            (project_root / "schemas").mkdir()
            executable = release_directory / "BulkFlowGUI.exe"
            executable.touch()

            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(executable)
            ):
                control, export, ddl = ensure_runtime_directories()
                root = runtime_data_root()
                config = runtime_config_directory()
            expected_root = project_root.resolve() / "Local" / "BulkFlow"
            self.assertEqual(root, expected_root)
            self.assertEqual(control, expected_root / ".bcp-control")
            self.assertEqual(export, expected_root / "bcp-data")
            self.assertEqual(ddl, expected_root / "ddl")
            self.assertEqual(config, expected_root / "config")
            self.assertTrue(all(path.is_dir() for path in (control, export, ddl)))


class BcpResolutionTests(unittest.TestCase):
    def test_explicit_existing_path_has_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            explicit = Path(temporary) / "custom" / "bcp.exe"
            explicit.parent.mkdir()
            explicit.touch()
            with patch(
                "bcp_engine.runtime._read_installer_bcp_path"
            ) as registry_read:
                resolved = resolve_bcp_executable(
                    explicit, environment={"PATH": ""}
                )
        self.assertEqual(resolved, str(explicit.resolve()))
        registry_read.assert_not_called()

    def test_missing_explicit_path_does_not_fall_back_to_another_bcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing" / "bcp.exe"
            with patch("bcp_engine.runtime.shutil.which") as which:
                resolved = resolve_bcp_executable(missing, environment={"PATH": "x"})
        self.assertIsNone(resolved)
        which.assert_not_called()

    def test_windows_installer_registry_precedes_program_files_and_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            registered = Path(temporary) / "registered" / "bcp.exe"
            registered.parent.mkdir()
            registered.touch()
            with patch("bcp_engine.runtime._is_windows", return_value=True), patch(
                "bcp_engine.runtime._read_installer_bcp_path",
                return_value=str(registered.resolve()),
            ), patch("bcp_engine.runtime.shutil.which") as which:
                resolved = resolve_bcp_executable("bcp", environment={"PATH": ""})
        self.assertEqual(resolved, str(registered.resolve()))
        which.assert_not_called()

    def test_windows_official_odbc_180_path_precedes_newer_numeric_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary) / "Program Files"
            base = program_files / "Microsoft SQL Server" / "Client SDK" / "ODBC"
            preferred = base / "180" / "Tools" / "Binn" / "bcp.exe"
            newer = base / "190" / "Tools" / "Binn" / "bcp.exe"
            preferred.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            preferred.touch()
            newer.touch()
            with patch("bcp_engine.runtime._is_windows", return_value=True), patch(
                "bcp_engine.runtime._read_installer_bcp_path", return_value=None
            ):
                resolved = resolve_bcp_executable(
                    "bcp.exe",
                    environment={"ProgramFiles": str(program_files), "PATH": ""},
                )
        self.assertEqual(resolved, str(preferred.resolve()))

    def test_windows_numeric_sdk_fallback_uses_highest_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            program_files = Path(temporary) / "Program Files"
            base = program_files / "Microsoft SQL Server" / "Client SDK" / "ODBC"
            older = base / "170" / "Tools" / "Binn" / "bcp.exe"
            newer = base / "190" / "Tools" / "Binn" / "bcp.exe"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            newer.touch()
            with patch("bcp_engine.runtime._is_windows", return_value=True), patch(
                "bcp_engine.runtime._read_installer_bcp_path", return_value=None
            ):
                resolved = resolve_bcp_executable(
                    "bcp", environment={"ProgramFiles": str(program_files), "PATH": ""}
                )
        self.assertEqual(resolved, str(newer.resolve()))

    def test_posix_and_custom_bare_names_use_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            discovered = Path(temporary) / "bcp-custom"
            discovered.touch()
            with patch("bcp_engine.runtime._is_windows", return_value=False), patch(
                "bcp_engine.runtime.shutil.which", return_value=str(discovered)
            ) as which:
                resolved = resolve_bcp_executable(
                    "bcp-custom", environment={"PATH": str(discovered.parent)}
                )
        self.assertEqual(resolved, str(discovered.resolve()))
        which.assert_called_once_with("bcp-custom", path=str(discovered.parent))


if __name__ == "__main__":
    unittest.main()
