from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import uuid

from bcp_engine.artifacts import ArtifactStore, _open_posix_regular
from bcp_engine.auth import AuthError, BcpInvocation, SecretValue
from bcp_engine.bcp import BcpRunner, PosixPtyConsole
from bcp_engine.config import ConfigError, read_config, validate_config


ROOT = Path(__file__).resolve().parents[1]
LINUX_CONFIG = ROOT / "examples" / "config.linux-sql.json"
SHELL_LAUNCHER = ROOT / "scripts" / "launchers" / "invoke-bcp.sh"
FIXED_ID = "12345678-1234-5678-9234-567812345678"


class PosixConfigurationContractTests(unittest.TestCase):
    def test_linux_example_uses_posix_paths_and_secret_references(self):
        config = read_config(LINUX_CONFIG)

        self.assertEqual(config["executor_directory"], "/var/lib/bcp-engine/artifacts")
        self.assertEqual(config["local_control_directory"], "/var/lib/bcp-engine/control")
        self.assertEqual(config["destination_sql_directory"], "/var/opt/mssql/bcp")
        rendered = json.dumps(config, ensure_ascii=False)
        self.assertIn("BCP_SOURCE_SQL_PASSWORD", rendered)
        self.assertIn("BCP_BRONZE_SQL_PASSWORD", rendered)
        self.assertIn("BCP_LANDING_SQL_PASSWORD", rendered)
        self.assertEqual(
            {
                config[name]["authentication"]["password"]["reference"]
                for name in ("source", "bronze_destination", "landing_destination")
            },
            {
                "BCP_SOURCE_SQL_PASSWORD",
                "BCP_BRONZE_SQL_PASSWORD",
                "BCP_LANDING_SQL_PASSWORD",
            },
        )
        self.assertNotIn('"-P"', rendered)

    def test_executor_and_control_paths_still_reject_relative_values(self):
        base = read_config(LINUX_CONFIG)
        for field in ("executor_directory", "local_control_directory"):
            invalid = copy.deepcopy(base)
            invalid[field] = "relative/path"
            with self.subTest(field=field), self.assertRaisesRegex(
                ConfigError, "caminho absoluto"
            ):
                validate_config(invalid)


@unittest.skipUnless(os.name == "posix", "runtime PTY POSIX exige host POSIX")
class PosixRuntimeTests(unittest.TestCase):
    def test_artifact_store_operates_on_posix_absolute_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "artifacts"
            store = ArtifactStore(root, str(uuid.uuid4()))
            store.probe(require_delete=True)
            self.assertTrue(store.execution_root.is_dir())

    def test_artifact_directories_preserve_setgid_and_remove_group_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "artifacts"
            root.mkdir()
            root.chmod(0o2770)
            store = ArtifactStore(root, str(uuid.uuid4()))
            table = store.table_directory("a" * 64)
            block = store.block_paths("a" * 64, 1, str(uuid.uuid4())).directory

            for directory in (root, store.execution_root, table, block):
                mode = directory.stat().st_mode
                self.assertTrue(mode & stat.S_ISGID)
                self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))

            self.assertEqual(block.stat().st_mode & 0o777, 0o700)

    def test_artifact_store_rejects_nonsticky_writable_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            unsafe = Path(temporary) / "unsafe"
            unsafe.mkdir()
            unsafe.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError, "Ancestral gravável e não sticky"):
                ArtifactStore(unsafe / "artifacts", str(uuid.uuid4()))

    def test_manifest_is_private_before_validation_even_with_permissive_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary) / "artifacts", str(uuid.uuid4()))
            table_id = "a" * 64
            block_id = str(uuid.uuid4())
            paths = store.block_paths(table_id, 1, block_id)
            previous_umask = os.umask(0)
            try:
                store._atomic_json(paths.manifest, {"incomplete": True})
            finally:
                os.umask(previous_umask)

            self.assertEqual(paths.manifest.stat().st_mode & 0o777, 0o600)
            resumed = store.block_paths(table_id, 1, block_id)
            self.assertEqual(resumed.directory.stat().st_mode & 0o777, 0o700)

    def test_posix_publication_open_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")
            symbolic = root / "symbolic.bin"
            symbolic.symlink_to(outside)
            with self.assertRaises(OSError):
                _open_posix_regular(symbolic)

            linked = root / "linked.bin"
            os.link(outside, linked)
            with self.assertRaisesRegex(RuntimeError, "Hard link"):
                _open_posix_regular(linked)

    def test_sql_password_uses_pty_and_is_redacted_from_output_log_argv_and_env(self):
        secret = "segredo-sintético ; ç []"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "fake_bcp.py"
            capture = root / "child-contract.json"
            log = root / "bcp.log"
            helper.write_text(
                textwrap.dedent(
                    """\
                    import json
                    import os
                    from pathlib import Path
                    import sys
                    import termios

                    capture = Path(sys.argv[1])
                    capture.write_text(
                        json.dumps({"argv": sys.argv, "environment": dict(os.environ)}),
                        encoding="utf-8",
                    )
                    descriptor = sys.stdin.fileno()
                    original = termios.tcgetattr(descriptor)
                    masked = termios.tcgetattr(descriptor)
                    masked[3] &= ~termios.ECHO
                    termios.tcsetattr(descriptor, termios.TCSANOW, masked)
                    try:
                        print("Password: ", end="", flush=True)
                        password = sys.stdin.readline().rstrip("\\r\\n")
                    finally:
                        termios.tcsetattr(descriptor, termios.TCSANOW, original)
                    if not password:
                        raise SystemExit(9)
                    print("\\nentrada recebida=" + password)
                    print("7 rows copied.")
                    """
                ),
                encoding="utf-8",
            )
            invocation = BcpInvocation(
                endpoint_name="source",
                executable=sys.executable,
                arguments=(str(helper), str(capture), "-U", "login_leitura"),
                authentication_type="sql",
                password_channel=SecretValue(secret),
            )
            runner = BcpRunner()
            self.assertIsInstance(runner.private_console, PosixPtyConsole)

            with patch.dict(os.environ, {"BCP_TEST_SECRET": secret}):
                result = runner.run(invocation, log, timeout=5, minimum_free=0)

            child_contract = json.loads(capture.read_text(encoding="utf-8"))
            persisted = log.read_text(encoding="utf-8")

        self.assertEqual(result.rows_copied, 7)
        self.assertNotIn(secret, result.output)
        self.assertNotIn(secret, persisted)
        self.assertIn("********", result.output)
        self.assertNotIn(secret, repr(child_contract["argv"]))
        self.assertNotIn(secret, repr(child_contract["environment"]))
        self.assertNotIn("BCP_TEST_SECRET", child_contract["environment"])

    def test_private_channel_rejects_line_break_before_starting_child(self):
        invocation = BcpInvocation(
            endpoint_name="source",
            executable="executable-must-not-run",
            arguments=("queryout", "data.bcp", "-U", "login"),
            authentication_type="sql",
            password_channel=SecretValue("linha-1\nlinha-2"),
        )

        with self.assertRaisesRegex(AuthError, "incompatível"):
            BcpRunner().preflight(invocation)

    def test_pty_timeout_terminates_child_without_waiting_for_prompt(self):
        invocation = BcpInvocation(
            endpoint_name="source",
            executable=sys.executable,
            arguments=("-c", "import time; time.sleep(30)"),
            authentication_type="sql",
            password_channel=SecretValue("segredo-temporário"),
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "bcp_timeout_seconds"):
                BcpRunner().run(
                    invocation,
                    Path(temporary) / "bcp.log",
                    timeout=0.1,
                    minimum_free=0,
                )
        self.assertLess(time.monotonic() - started, 3)

    def test_shell_launcher_executes_status_with_posix_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = read_config(LINUX_CONFIG)
            config["executor_directory"] = str(root / "artifacts")
            config["local_control_directory"] = str(root / "control")
            config["destination_sql_directory"] = str(root / "sql-visible")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["BCP_PYTHON"] = sys.executable

            result = subprocess.run(
                [
                    str(SHELL_LAUNCHER),
                    "status",
                    "--config",
                    str(config_path),
                    "--execution-id",
                    FIXED_ID,
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )

            control_file = root / "control" / "controle_transferencia.sqlite3"
            self.assertTrue(control_file.is_file())

        self.assertEqual(result.returncode, 1)
        self.assertIn("Execução não encontrada", result.stderr)


if __name__ == "__main__":
    unittest.main()
