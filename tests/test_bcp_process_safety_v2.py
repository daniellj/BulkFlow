import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bcp_engine.auth import AuthError, BcpInvocation, SecretValue, WindowsExecutionContext
from bcp_engine.bcp import (
    BCP_OUTPUT_TAIL_BYTES,
    BcpRunner,
    BoundedOutputTail,
    WinPtyConsole,
    parse_rows_copied,
)
from bcp_engine.connections import WindowsCredentialAdapter


class OutputLimitTests(unittest.TestCase):
    def test_row_count_parser_ignores_terminal_cursor_sequences(self):
        output = "\x1b[?25lStarting copy...\x1b[6;1H3 rows copied.\r\n"
        self.assertEqual(parse_rows_copied(output), 3)

    def test_row_count_parser_does_not_concatenate_progress_and_final_count(self):
        output = (
            "1000 rows successfully bulk-copied to host-file. "
            "Total received: 1000\x1b[2K\x1b[1G1500 rows copied.\r\n"
        )
        self.assertEqual(parse_rows_copied(output), 1500)

    def test_row_count_parser_recovers_when_control_splits_the_final_label(self):
        output = (
            "1000 rows successfully bulk-copied to host-file. "
            "Total received: 1000\x1b[2K1500 ro\x1b[1Gws copied.\r\n"
        )
        self.assertEqual(parse_rows_copied(output), 1500)

    def test_row_count_parser_prefers_visible_number_over_raw_zero_suffix(self):
        output = (
            "1000 rows successfully bulk-copied to host-file. "
            "Total received: 1000\x1b[2K150\x1b[1G0 rows copied.\r\n"
        )
        self.assertEqual(parse_rows_copied(output), 1500)

    def test_tail_is_bounded_and_truncation_is_explicit(self):
        capture = BoundedOutputTail(16)
        capture.append(b"0123456789")
        capture.append(b"abcdefghijklmnop")

        rendered = capture.render()
        self.assertIn("SAIDA BCP TRUNCADA", rendered)
        self.assertIn("10 bytes iniciais omitidos", rendered)
        self.assertTrue(rendered.endswith("abcdefghijklmnop"))
        self.assertEqual(capture.discarded_bytes, 10)

    def test_standard_runner_keeps_bounded_tail_and_still_parses_rows(self):
        payload = BCP_OUTPUT_TAIL_BYTES + 1024
        script = (
            "import sys; "
            f"sys.stdout.write('x'*{payload}); "
            "sys.stdout.write('\\n42 rows copied.\\n')"
        )
        invocation = BcpInvocation(
            endpoint_name="test",
            executable=sys.executable,
            arguments=("-c", script),
            authentication_type="windows_integrated",
        )
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "bcp.log"
            result = BcpRunner().run(invocation, log)
            persisted = log.read_bytes().decode("utf-8")

        self.assertEqual(result.rows_copied, 42)
        self.assertIn("SAIDA BCP TRUNCADA", result.output)
        self.assertLessEqual(len(result.output.encode("utf-8")), BCP_OUTPUT_TAIL_BYTES + 160)
        self.assertEqual(persisted, result.output)

    def test_runner_rechecks_final_file_size_after_child_exit(self):
        invocation = BcpInvocation(
            endpoint_name="test",
            executable=sys.executable,
            arguments=("-c", "pass"),
            authentication_type="windows_integrated",
        )
        runner = BcpRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = root / "block.partial"
            log = root / "bcp.log"

            def finish_with_late_write(*_args, **_kwargs):
                monitor.write_bytes(b"x" * 11)
                return 0, "1 rows copied."

            with patch.object(runner, "_run_standard", side_effect=finish_with_late_write):
                with self.assertRaisesRegex(RuntimeError, "max_file_bytes"):
                    runner.run(
                        invocation,
                        log,
                        monitor_path=monitor,
                        maximum_bytes=10,
                        minimum_free=0,
                    )

    def test_runner_rechecks_final_free_space_after_child_exit(self):
        invocation = BcpInvocation(
            endpoint_name="test",
            executable=sys.executable,
            arguments=("-c", "pass"),
            authentication_type="windows_integrated",
        )
        runner = BcpRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = root / "block.partial"
            monitor.write_bytes(b"x")
            log = root / "bcp.log"
            disk = SimpleNamespace(free=4)
            with (
                patch.object(runner, "_run_standard", return_value=(0, "1 rows copied.")),
                patch("bcp_engine.bcp.shutil.disk_usage", return_value=disk),
            ):
                with self.assertRaisesRegex(RuntimeError, "apos o BCP"):
                    runner.run(
                        invocation,
                        log,
                        monitor_path=monitor,
                        maximum_bytes=10,
                        minimum_free=5,
                    )

    def test_runner_rejects_zero_rows_when_artifact_is_not_empty(self):
        invocation = BcpInvocation(
            endpoint_name="test",
            executable=sys.executable,
            arguments=("-c", "pass"),
            authentication_type="windows_integrated",
        )
        runner = BcpRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor = root / "block.partial"
            monitor.write_bytes(b"native-bcp-payload")
            log = root / "bcp.log"
            with patch.object(runner, "_run_standard", return_value=(0, "0 rows copied.")):
                with self.assertRaisesRegex(RuntimeError, "checkpoint foi bloqueado"):
                    runner.run(
                        invocation,
                        log,
                        monitor_path=monitor,
                        maximum_bytes=1024,
                        minimum_free=0,
                    )


class _BlockingPtyProcess:
    def __init__(self):
        self.alive = True
        self.released = threading.Event()
        self.terminated = 0
        self.exitstatus = 1
        self.writes = []

    def read(self, _size):
        self.released.wait(30)
        raise EOFError

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.terminated += 1
        self.alive = False
        self.released.set()

    def write(self, value):
        self.writes.append(value)


class _AnsiPromptPtyProcess:
    def __init__(self):
        self.alive = True
        self.exitstatus = 0
        self.writes = []
        self._chunks = iter(
            ["P\x1b[?25hassword: ", "\r\n3 rows copied.\r\n"]
        )

    def read(self, _size):
        try:
            return next(self._chunks)
        except StopIteration:
            self.alive = False
            raise EOFError

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False

    def write(self, value):
        self.writes.append(value)


class _ExitDrainRacePtyProcess:
    """Models pywinpty exposing the final screen buffer after transient EOF."""

    def __init__(self):
        self.alive = True
        self.exitstatus = 0
        self.writes = []
        self._read_number = 0

    def read(self, _size):
        self._read_number += 1
        if self._read_number == 1:
            return "Password:\r\n"
        if self._read_number == 2:
            return "\r\nStarting copy...\r\n"
        if self._read_number == 3:
            # The copy itself can run much longer than the post-exit drain
            # grace. Old logic incorrectly measured quiet time from this
            # pre-exit output and therefore trusted the first EOF immediately.
            time.sleep(0.03)
            self.alive = False
            raise EOFError
        if self._read_number == 4:
            return "\r\n1500 rows copied.\r\n"
        raise EOFError

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False

    def write(self, value):
        self.writes.append(value)


class _DelayedExitTailPtyProcess:
    """Models repeated EOFs before ConPTY exposes the final screen tail."""

    def __init__(self, tail_delay=0.7):
        self.alive = True
        self.exitstatus = 0
        self.writes = []
        self._read_number = 0
        self._exited_at = None
        self._tail_delay = tail_delay
        self._tail_sent = False

    def read(self, _size):
        self._read_number += 1
        if self._read_number == 1:
            return "Password:\r\n"
        if self._read_number == 2:
            self.alive = False
            self._exited_at = time.monotonic()
            return (
                "1000 rows successfully bulk-copied to host-file. "
                "Total received: 1000\r\n"
            )
        assert self._exited_at is not None
        if (
            self._tail_delay is not None
            and not self._tail_sent
            and time.monotonic() - self._exited_at >= self._tail_delay
        ):
            self._tail_sent = True
            return "1500 rows copied.\r\n"
        raise EOFError

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False

    def write(self, value):
        self.writes.append(value)


class _BlockedReadAfterExitPtyProcess:
    """Models pywinpty 2.0.15 leaving fileobj.recv blocked after exit."""

    def __init__(self, output="Format file generated.\r\n", exitstatus=0):
        self.alive = True
        self.closed = False
        self.exitstatus = exitstatus
        self.writes = []
        self.close_calls = 0
        self._read_number = 0
        self._password_received = threading.Event()
        self._transport_released = threading.Event()
        self._output = output

    def read(self, _size):
        self._read_number += 1
        if self._read_number == 1:
            return "Password:\r\n"
        if self._read_number == 2:
            self._password_received.wait(2)
            self.alive = False
            return self._output
        self._transport_released.wait(10)
        raise OSError("transport closed")

    def isalive(self):
        # This is the pywinpty 2.0.15 behavior at the root of the regression:
        # process death marks the object closed before its reader socket closes.
        if not self.alive:
            self.closed = True
        return self.alive

    def close(self, force=False):
        self.close_calls += 1
        # PtyProcess.close() is a no-op when isalive() has set closed=True.
        if not self.closed:
            self.closed = True
            self._transport_released.set()

    def terminate(self, force=False):
        self.alive = False
        self._transport_released.set()

    def write(self, value):
        self.writes.append(value)
        self._password_received.set()


class _ReaderFailurePtyProcess:
    def __init__(self):
        self.alive = True
        self.closed = False
        self.exitstatus = 1
        self._read_number = 0

    def read(self, _size):
        self._read_number += 1
        if self._read_number == 1:
            return "Password:\r\n"
        raise ValueError("reader failure before controlled close")

    def isalive(self):
        return self.alive

    def close(self, force=False):
        self.closed = True

    def terminate(self, force=False):
        self.alive = False

    def write(self, _value):
        pass


class WinPtyMonitorTests(unittest.TestCase):
    def _invocation(self):
        return BcpInvocation(
            endpoint_name="origem",
            executable="bcp.exe",
            arguments=("queryout", "arquivo.dat", "-U", "u684"),
            authentication_type="sql",
            password_channel=SecretValue("u684-secret"),
        )

    def test_blocked_pty_read_does_not_block_timeout_and_child_is_terminated(self):
        process = _BlockingPtyProcess()
        spawned = {}

        class PtyProcess:
            @staticmethod
            def spawn(command, **kwargs):
                spawned["command"] = command
                spawned["environment"] = kwargs["env"]
                return process

        started = time.monotonic()
        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            with self.assertRaisesRegex(RuntimeError, "bcp_timeout_seconds"):
                WinPtyConsole().run(self._invocation(), timeout=0.1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2)
        self.assertEqual(process.terminated, 1)
        self.assertNotIn("u684-secret", spawned["command"])
        self.assertIsInstance(spawned["command"], list)
        self.assertNotIn("u684-secret", repr(spawned["environment"]))

    def test_pty_receives_argv_list_so_executable_paths_with_spaces_are_preserved(self):
        process = _BlockingPtyProcess()
        process.alive = False
        process.released.set()
        captured = {}

        class PtyProcess:
            @staticmethod
            def spawn(argv, **_kwargs):
                captured["argv"] = argv
                return process

        invocation = BcpInvocation(
            endpoint_name="origem",
            executable=r"C:\Program Files\Microsoft SQL Server\bcp.exe",
            arguments=("queryout", "arquivo.dat", "-U", "login"),
            authentication_type="sql",
            password_channel=SecretValue("senha-distinta"),
        )
        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            with self.assertRaisesRegex(AuthError, "prompt"):
                WinPtyConsole().run(invocation, timeout=1)

        self.assertEqual(captured["argv"][0], invocation.executable)
        self.assertIsInstance(captured["argv"], list)

    def test_ansi_control_inside_password_prompt_is_ignored(self):
        process = _AnsiPromptPtyProcess()

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            code, output = WinPtyConsole().run(self._invocation(), timeout=1)

        self.assertEqual(code, 0)
        self.assertEqual(process.writes, ["u684-secret\r\n"])
        self.assertIn("3 rows copied", output)

    def test_exit_drain_retries_after_transient_eof_and_keeps_final_row_count(self):
        process = _ExitDrainRacePtyProcess()

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch("bcp_engine.bcp.WINPTY_EXIT_DRAIN_GRACE_SECONDS", 0.01),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            code, output = WinPtyConsole().run(self._invocation(), timeout=1)

        self.assertEqual(code, 0)
        self.assertEqual(process.writes, ["u684-secret\r\n"])
        self.assertIn("Starting copy", output)
        self.assertEqual(parse_rows_copied(output), 1500)
        self.assertNotIn("u684-secret", output)

    def test_exit_drain_survives_repeated_eofs_longer_than_old_grace(self):
        process = _DelayedExitTailPtyProcess(tail_delay=0.7)

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            code, output = WinPtyConsole().run(self._invocation(), timeout=2)

        self.assertEqual(code, 0)
        self.assertGreater(process._read_number, 4)
        self.assertEqual(parse_rows_copied(output), 1500)

    def test_exit_drain_without_final_tail_is_bounded_and_fails_closed(self):
        process = _DelayedExitTailPtyProcess(tail_delay=None)

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        started = time.monotonic()
        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch("bcp_engine.bcp.WINPTY_EXIT_DRAIN_GRACE_SECONDS", 0.05),
            patch("bcp_engine.bcp.WINPTY_EXIT_DRAIN_HARD_TIMEOUT_SECONDS", 0.5),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            code, output = WinPtyConsole().run(self._invocation(), timeout=1)
        elapsed = time.monotonic() - started

        self.assertEqual(code, 0)
        self.assertLess(elapsed, 1)
        with self.assertRaisesRegex(RuntimeError, "contagem real"):
            parse_rows_copied(output)

    def test_blocked_read_after_format_exit_is_closed_and_output_is_drained(self):
        process = _BlockedReadAfterExitPtyProcess()

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        started = time.monotonic()
        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch("bcp_engine.bcp.WINPTY_EXIT_DRAIN_HARD_TIMEOUT_SECONDS", 0.05),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            code, output = WinPtyConsole().run(self._invocation(), timeout=1)
        elapsed = time.monotonic() - started

        self.assertEqual(code, 0)
        self.assertLess(elapsed, 1)
        self.assertEqual(process.close_calls, 1)
        self.assertTrue(process._transport_released.is_set())
        self.assertIn("Format file generated", output)

    def test_format_without_final_row_count_succeeds_but_export_fails_closed(self):
        class PtyProcess:
            processes = []

            @classmethod
            def spawn(cls, _argv, **_kwargs):
                process = _BlockedReadAfterExitPtyProcess()
                cls.processes.append(process)
                return process

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(WinPtyConsole, "available", return_value=True),
                patch("bcp_engine.bcp.WINPTY_EXIT_DRAIN_HARD_TIMEOUT_SECONDS", 0.05),
                patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
            ):
                root = Path(temporary)
                runner = BcpRunner(private_console=WinPtyConsole())
                format_result = runner.run(
                    self._invocation(),
                    root / "format.log",
                    timeout=1,
                    minimum_free=0,
                    expect_row_count=False,
                )
                with self.assertRaisesRegex(RuntimeError, "contagem real"):
                    runner.run(
                        self._invocation(),
                        root / "export.log",
                        timeout=1,
                        minimum_free=0,
                    )

        self.assertIsNone(format_result.rows_copied)
        self.assertEqual(len(PtyProcess.processes), 2)
        self.assertTrue(all(process.close_calls == 1 for process in PtyProcess.processes))

    def test_real_reader_error_before_controlled_close_is_not_hidden(self):
        process = _ReaderFailurePtyProcess()

        class PtyProcess:
            @staticmethod
            def spawn(_argv, **_kwargs):
                return process

        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            with self.assertRaisesRegex(RuntimeError, "Falha ao ler") as raised:
                WinPtyConsole().run(self._invocation(), timeout=1)

        self.assertIsInstance(raised.exception.__cause__, ValueError)

    def test_monitor_stat_error_terminates_child(self):
        process = _BlockingPtyProcess()

        class PtyProcess:
            @staticmethod
            def spawn(_command, **_kwargs):
                return process

        monitor = SimpleNamespace(
            exists=lambda: True,
            stat=lambda: (_ for _ in ()).throw(OSError("falha stat")),
            parent=Path("."),
        )
        with (
            patch.object(WinPtyConsole, "available", return_value=True),
            patch.dict(sys.modules, {"winpty": SimpleNamespace(PtyProcess=PtyProcess)}),
        ):
            with self.assertRaisesRegex(RuntimeError, "revalidar o espaco livre"):
                WinPtyConsole().run(self._invocation(), timeout=2, monitor_path=monitor)
        self.assertEqual(process.terminated, 1)


@unittest.skipUnless(os.name == "nt", "launcher CreateProcessWithLogonW é Windows")
class WindowsLauncherCleanupTests(unittest.TestCase):
    class Api:
        def __init__(self, implementation):
            self.implementation = implementation

        def __call__(self, *args):
            return self.implementation(*args)

    def test_error_after_creation_terminates_child_and_password_is_not_in_command(self):
        state = {"terminated": 0, "command": None, "password": None}

        def create_pipe(read, write, _attributes, _size):
            read._obj.value = 101
            write._obj.value = 102
            return True

        def create_process(
            _username, _domain, password, _logon_flags, _application, command,
            _creation_flags, _environment, _directory, _startup, information,
        ):
            state["password"] = password
            state["command"] = command.value
            information._obj.hProcess = 201
            information._obj.hThread = 202
            information._obj.dwProcessId = 203
            return True

        def wait(_handle, _milliseconds):
            return 0 if state["terminated"] else 258

        def terminate(_handle, _code):
            state["terminated"] += 1
            return True

        kernel = SimpleNamespace(
            CreatePipe=self.Api(create_pipe),
            SetHandleInformation=self.Api(lambda *_args: True),
            CreateFileW=self.Api(lambda *_args: 103),
            CloseHandle=self.Api(lambda *_args: True),
            WaitForSingleObject=self.Api(wait),
            TerminateProcess=self.Api(terminate),
            GetExitCodeProcess=self.Api(lambda *_args: True),
        )
        advapi = SimpleNamespace(CreateProcessWithLogonW=self.Api(create_process))
        invocation = BcpInvocation(
            endpoint_name="origem",
            executable="bcp.exe",
            arguments=("queryout", "arquivo.dat", "-T"),
            authentication_type="windows_credentials",
            windows_context=WindowsExecutionContext("DOM", "u684", SecretValue("secret-684")),
        )

        with (
            patch("ctypes.WinDLL", side_effect=[kernel, advapi]),
            patch("msvcrt.open_osfhandle", side_effect=OSError("falha controlada")),
        ):
            with self.assertRaisesRegex(OSError, "falha controlada"):
                WindowsCredentialAdapter().start_bcp(invocation)

        self.assertEqual(state["terminated"], 1)
        self.assertEqual(state["password"], "secret-684")
        self.assertNotIn("secret-684", state["command"])


if __name__ == "__main__":
    unittest.main()
