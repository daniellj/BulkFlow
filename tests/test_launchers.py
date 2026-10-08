from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_LAUNCHER = ROOT / "scripts" / "launchers" / "invoke-bcp.ps1"
SHELL_LAUNCHER = ROOT / "scripts" / "launchers" / "invoke-bcp.sh"
SENSITIVE_SENTINEL = "SENSITIVE_SENTINEL_MUST_NOT_BE_LOGGED_7419"


def find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def find_usable_bash() -> str | None:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
            ]
        )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "exit 0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def process_environment(**values: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(values)
    return environment


class ControlledInterpreter:
    def __init__(self, directory: Path, exit_code: int):
        self.capture_path = directory / "captured-arguments.json"
        self.helper_path = directory / "capture_arguments.py"
        self.helper_path.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path
                import sys

                Path(os.environ["BCP_TEST_CAPTURE"]).write_text(
                    json.dumps(sys.argv[1:], ensure_ascii=False),
                    encoding="utf-8",
                )
                print("saida controlada")
                print("erro controlado", file=sys.stderr)
                raise SystemExit(int(os.environ["BCP_TEST_EXIT"]))
                """
            ),
            encoding="utf-8",
        )
        self.shell_command_path = directory / "controlled-python.sh"
        self.shell_command_path.write_text(
            "#!/usr/bin/env sh\n"
            'exec "$BCP_TEST_REAL_PYTHON" "$BCP_TEST_HELPER" "$@"\n',
            encoding="utf-8",
        )
        self.shell_command_path.chmod(
            self.shell_command_path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        if os.name == "nt":
            self.command_path = directory / "controlled-python.cmd"
            self.command_path.write_text(
                "@echo off\r\n"
                '"%BCP_TEST_REAL_PYTHON%" "%BCP_TEST_HELPER%" %*\r\n'
                "exit /b %errorlevel%\r\n",
                encoding="ascii",
            )
        else:
            self.command_path = self.shell_command_path
        self.environment = process_environment(
            BCP_PYTHON=str(self.command_path),
            BCP_TEST_CAPTURE=str(self.capture_path),
            BCP_TEST_REAL_PYTHON=sys.executable,
            BCP_TEST_HELPER=str(self.helper_path),
            BCP_TEST_EXIT=str(exit_code),
        )

    def captured_arguments(self) -> list[str]:
        return json.loads(self.capture_path.read_text(encoding="utf-8"))

    def shell_environment(self) -> dict[str, str]:
        environment = self.environment.copy()
        environment["BCP_PYTHON"] = str(self.shell_command_path)
        return environment


@unittest.skipUnless(find_powershell(), "PowerShell não disponível neste host")
class PowerShellLauncherTests(unittest.TestCase):
    powershell = find_powershell()

    def invoke(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert self.powershell is not None
        command = [self.powershell, "-NoLogo", "-NoProfile", "-NonInteractive"]
        if os.name == "nt":
            command.extend(["-ExecutionPolicy", "Bypass"])
        command.extend(["-File", str(POWERSHELL_LAUNCHER), *arguments])
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def test_forwards_arguments_from_any_directory_and_preserves_exit_code(self):
        forwarded = [
            "status",
            "--config",
            "configuração com espaço.json",
            "--execution-id",
            SENSITIVE_SENTINEL,
            "",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            controlled = ControlledInterpreter(temporary_path, exit_code=23)
            result = self.invoke(
                forwarded,
                environment=controlled.environment,
                cwd=temporary_path,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            captured = controlled.captured_arguments()

        self.assertEqual(Path(captured[0]).resolve(), (ROOT / "bcp_bronze.py").resolve())
        self.assertEqual(captured[1:], forwarded)
        self.assertIn("saida controlada", result.stdout)
        self.assertIn("erro controlado", result.stderr)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stdout)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stderr)

    def test_invalid_configured_interpreter_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["--help"],
                environment=process_environment(BCP_PYTHON=SENSITIVE_SENTINEL),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 127)
        self.assertIn("Não foi possível", result.stderr)
        self.assertIn("interpretador Python", result.stderr)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stderr)

    def test_real_cli_help_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["--help"],
                environment=process_environment(BCP_PYTHON=sys.executable),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_real_cli_parser_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["not-a-command"],
                environment=process_environment(BCP_PYTHON=sys.executable),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 2)


@unittest.skipUnless(find_usable_bash(), "bash funcional não disponível neste host")
class ShellLauncherTests(unittest.TestCase):
    bash = find_usable_bash()

    @staticmethod
    def shell_path(path: Path) -> str:
        return path.as_posix()

    def invoke(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert self.bash is not None
        adjusted_environment = environment.copy()
        if os.name == "nt":
            for name in (
                "BCP_PYTHON",
                "BCP_TEST_CAPTURE",
                "BCP_TEST_REAL_PYTHON",
                "BCP_TEST_HELPER",
            ):
                if name in adjusted_environment:
                    adjusted_environment[name] = Path(adjusted_environment[name]).as_posix()
        return subprocess.run(
            [self.bash, self.shell_path(SHELL_LAUNCHER), *arguments],
            cwd=cwd,
            env=adjusted_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def test_forwards_arguments_from_any_directory_and_preserves_exit_code(self):
        forwarded = [
            "status",
            "--config",
            "configuração com espaço.json",
            "--execution-id",
            SENSITIVE_SENTINEL,
            "",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            controlled = ControlledInterpreter(temporary_path, exit_code=23)
            result = self.invoke(
                forwarded,
                environment=controlled.shell_environment(),
                cwd=temporary_path,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            captured = controlled.captured_arguments()

        self.assertEqual(Path(captured[0]).resolve(), (ROOT / "bcp_bronze.py").resolve())
        self.assertEqual(captured[1:], forwarded)
        self.assertIn("saida controlada", result.stdout)
        self.assertIn("erro controlado", result.stderr)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stdout)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stderr)

    def test_invalid_configured_interpreter_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["--help"],
                environment=process_environment(BCP_PYTHON=SENSITIVE_SENTINEL),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 127)
        self.assertIn("Não foi possível", result.stderr)
        self.assertIn("interpretador Python", result.stderr)
        self.assertNotIn(SENSITIVE_SENTINEL, result.stderr)

    def test_real_cli_help_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["--help"],
                environment=process_environment(BCP_PYTHON=sys.executable),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_real_cli_parser_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.invoke(
                ["not-a-command"],
                environment=process_environment(BCP_PYTHON=sys.executable),
                cwd=Path(temporary),
            )

        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
