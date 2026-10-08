"""Adaptador BCP seguro: argv sem senha, console privado e contagem real."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Protocol

from .auth import (
    AuthError,
    BcpInvocation,
    WindowsContextAdapter,
    assert_bcp_has_no_password,
    minimal_subprocess_environment,
)
from .util import DEFAULT_REDACTOR, SecretRedactor


NS = "http://schemas.microsoft.com/sqlserver/2004/bulkload/format"

# O ConPTY pode inserir sequencias de controle no meio de uma palavra ao
# redesenhar o prompt (por exemplo ``P<CSI>assword:``). Remova CSI/OSC apenas
# da janela usada para detectar o prompt; a saida original continua capturada.
_TERMINAL_CONTROL = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)

_PASSWORD_PROMPT = re.compile(r"(?i)(?:password|senha|contrase(?:ñ|n)a)\s*:")

# pywinpty can report the child as exited (and even raise one transient EOF)
# before ConPTY exposes the last screen update containing ``rows copied``.
# A short, bounded quiet period catches that tail without adding latency to
# normal successful runs, whose final counter lets the reader finish at once.
WINPTY_EXIT_DRAIN_GRACE_SECONDS = 2.0
WINPTY_EXIT_DRAIN_HARD_TIMEOUT_SECONDS = 5.0
WINPTY_READ_RETRY_SECONDS = 0.02
_BCP_FINAL_COUNT_MARKER = re.compile(
    r"(?i)\b[0-9][0-9., ]*\s+(?:rows? copied|linhas? copiadas?|filas? copiadas?)\.?"
)


def _terminal_visible_text(value: str) -> str:
    return _TERMINAL_CONTROL.sub("", value)

# O BCP normalmente escreve poucas linhas, mas mensagens repetidas do driver ou
# de rede não podem transformar o processo do motor em um acumulador sem limite.
# Quatro MiB preservam, com ampla folga, o resumo final usado pelo parser e um
# trecho diagnóstico útil do log. Quando o limite é ultrapassado, mantemos a
# cauda (onde o BCP informa a contagem/erro final) e tornamos o truncamento
# explícito no próprio texto retornado.
BCP_OUTPUT_TAIL_BYTES = 4 * 1024 * 1024


class BoundedOutputTail:
    """Captura binária thread-safe que retém somente a cauda da saída."""

    def __init__(self, limit: int = BCP_OUTPUT_TAIL_BYTES) -> None:
        if limit <= 0:
            raise ValueError("O limite da captura BCP deve ser positivo")
        self.limit = int(limit)
        self._tail = bytearray()
        self._discarded = 0
        self._lock = threading.Lock()

    def append(self, value: bytes | bytearray | memoryview | str) -> None:
        data = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
        if not data:
            return
        with self._lock:
            overflow = len(self._tail) + len(data) - self.limit
            if overflow > 0:
                removed = min(overflow, len(self._tail))
                if removed:
                    del self._tail[:removed]
                    self._discarded += removed
                remaining = overflow - removed
                if remaining:
                    data = data[remaining:]
                    self._discarded += remaining
            self._tail.extend(data)

    @property
    def discarded_bytes(self) -> int:
        with self._lock:
            return self._discarded

    def render(self) -> str:
        with self._lock:
            tail = bytes(self._tail)
            discarded = self._discarded
        text = tail.decode("utf-8", errors="replace")
        if not discarded:
            return text
        marker = (
            "[SAIDA BCP TRUNCADA: "
            f"{discarded} bytes iniciais omitidos; "
            f"ultimos {self.limit} bytes preservados]\n"
        )
        return marker + text


@dataclass(frozen=True)
class BcpResult:
    return_code: int
    rows_copied: int | None
    duration_seconds: float
    output: str


class PrivateConsole(Protocol):
    def available(self) -> bool: ...

    def run(
        self,
        invocation: BcpInvocation,
        timeout: int,
        *,
        monitor_path: Path | None = None,
        maximum_bytes: int = 2_147_483_648,
        minimum_free: int = 10_737_418_240,
    ) -> tuple[int, str]: ...


class WinPtyConsole:
    """Canal ConPTY/WinPTY opcional para responder ao prompt mascarado do BCP.

    A senha é escrita no pipe do pseudoconsole e não participa de argv, ambiente,
    arquivo temporário ou preview de comando.
    """

    PROMPT = _PASSWORD_PROMPT

    def available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import winpty  # noqa: F401
            return True
        except ImportError:
            return False

    def run(
        self,
        invocation: BcpInvocation,
        timeout: int,
        *,
        monitor_path: Path | None = None,
        maximum_bytes: int = 2_147_483_648,
        minimum_free: int = 10_737_418_240,
    ) -> tuple[int, str]:
        password = _private_console_password(invocation)
        if not self.available():
            raise AuthError(
                "Autenticação SQL no BCP exige o adaptador de pseudoconsole pywinpty; "
                "nenhum fallback inseguro por stdin/argv será usado."
            )
        import winpty

        assert_bcp_has_no_password(invocation, password)
        process = winpty.PtyProcess.spawn(
            list(invocation.argv),
            env=minimal_subprocess_environment(),
            dimensions=(80, 200),
        )
        started = time.monotonic()
        captured = BoundedOutputTail()
        # A leitura de PtyProcess.read pode bloquear indefinidamente enquanto o
        # processo aguarda entrada. Ela fica isolada nesta thread; a thread de
        # monitoramento continua aplicando timeout, quota e espaço em disco.
        chunks: queue.Queue[str] = queue.Queue(maxsize=128)
        reader_done = threading.Event()
        stop_reader = threading.Event()
        final_count_seen = threading.Event()
        controlled_close = threading.Event()
        reader_errors: list[BaseException] = []

        def close_exited_pseudoconsole() -> None:
            """Close the PTY transport after the child has already exited.

            pywinpty 2.0.15 sets ``PtyProcess.closed`` when ``isalive()``
            observes child termination, although ``read()`` can still be
            blocked in the transport's ``recv()``. Its public ``close()`` then
            becomes a no-op. Reopening only that bookkeeping flag lets the
            public method close the transport without reviving or terminating
            the already-dead child.
            """

            close = getattr(process, "close", None)
            if not callable(close):
                return
            controlled_close.set()
            if getattr(process, "closed", False):
                process.closed = False
            close(force=False)

        def consume() -> None:
            exit_drain_started: float | None = None
            last_exit_output_at: float | None = None
            output_window = ""
            try:
                while not stop_reader.is_set():
                    try:
                        chunk = process.read(4096)
                    except EOFError:
                        chunk = ""
                    except BaseException:
                        # Closing the transport from the monitor is the only
                        # supported way to release pywinpty 2.0.15 from a
                        # blocking recv() after child exit. Suppress only the
                        # exception produced after that deliberate close.
                        if controlled_close.is_set():
                            break
                        raise
                    now = time.monotonic()
                    if stop_reader.is_set() and not chunk:
                        break
                    if chunk:
                        if not process.isalive():
                            exit_drain_started = exit_drain_started or now
                            last_exit_output_at = now
                        output_window = (output_window + chunk)[-8192:]
                        has_final_count = bool(_BCP_FINAL_COUNT_MARKER.search(
                            _terminal_visible_text(output_window)
                        ))
                        while not stop_reader.is_set():
                            try:
                                chunks.put(chunk, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                        else:
                            break
                        # Publish completion only after the chunk containing
                        # the final counter is safely queued for the monitor.
                        if has_final_count:
                            final_count_seen.set()
                            if not process.isalive():
                                break
                        continue

                    if process.isalive():
                        # Do not trust an EOF while the child is still alive.
                        # The monitor retains authority over the hard timeout.
                        stop_reader.wait(WINPTY_READ_RETRY_SECONDS)
                        continue

                    exit_drain_started = exit_drain_started or now
                    if final_count_seen.is_set():
                        break
                    quiet_since = last_exit_output_at or exit_drain_started
                    if now - quiet_since >= WINPTY_EXIT_DRAIN_GRACE_SECONDS:
                        break
                    # This retry is the important race fix: ConPTY may expose
                    # the final screen buffer only after pywinpty reports one
                    # EOF immediately following child termination.
                    if stop_reader.wait(WINPTY_READ_RETRY_SECONDS):
                        break
            except BaseException as exc:  # propagado no monitor, nunca perdido na thread
                reader_errors.append(exc)
            finally:
                reader_done.set()

        reader = threading.Thread(target=consume, name="bcp-winpty-output", daemon=True)
        reader.start()
        password_sent = False
        prompt_window = ""
        failure: BaseException | None = None
        exit_seen_at: float | None = None
        transport_closed_at: float | None = None
        try:
            while True:
                try:
                    chunk = chunks.get(timeout=0.05)
                except queue.Empty:
                    chunk = ""
                if chunk:
                    captured.append(chunk)
                    prompt_window = (prompt_window + chunk)[-8192:]
                    if (
                        not password_sent
                        and self.PROMPT.search(_terminal_visible_text(prompt_window))
                    ):
                        process.write(password + "\r\n")
                        password_sent = True

                if reader_errors:
                    raise RuntimeError("Falha ao ler a saída do pseudoconsole BCP") from reader_errors[0]
                process_alive = process.isalive()
                if timeout and process_alive and time.monotonic() - started > timeout:
                    raise RuntimeError("BCP excedeu bcp_timeout_seconds")
                if monitor_path is not None:
                    try:
                        if monitor_path.exists() and monitor_path.stat().st_size > maximum_bytes:
                            raise RuntimeError("Arquivo BCP excedeu max_file_bytes")
                        free = shutil.disk_usage(monitor_path.parent).free
                    except OSError as exc:
                        raise RuntimeError(
                            "Nao foi possivel revalidar o espaco livre durante o BCP"
                        ) from exc
                    if free < minimum_free:
                        raise RuntimeError("Espaco livre abaixo do minimo durante a exportacao")

                if not process_alive:
                    exit_seen_at = exit_seen_at or time.monotonic()
                    if reader_done.is_set() and chunks.empty():
                        break
                    # Preserve a bounded drain window after child exit. If
                    # pywinpty remains blocked, close only the PTY transport,
                    # then keep draining every chunk already queued.
                    if (
                        (
                            final_count_seen.is_set()
                            or time.monotonic() - exit_seen_at
                            > WINPTY_EXIT_DRAIN_HARD_TIMEOUT_SECONDS
                        )
                        and not controlled_close.is_set()
                    ):
                        try:
                            close_exited_pseudoconsole()
                            transport_closed_at = time.monotonic()
                        except BaseException as exc:
                            raise RuntimeError(
                                "Falha ao fechar o pseudoconsole BCP após o processo"
                            ) from exc
                    if (
                        transport_closed_at is not None
                        and not reader_done.is_set()
                        and time.monotonic() - transport_closed_at > 5
                    ):
                        raise RuntimeError(
                            "Leitura do pseudoconsole BCP não encerrou após o fechamento controlado"
                        )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            stop_reader.set()
            if process.isalive():
                try:
                    process.terminate(force=True)
                    deadline = time.monotonic() + 5
                    while process.isalive() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if process.isalive():
                        raise RuntimeError("Não foi possível encerrar o processo BCP no pseudoconsole")
                except BaseException as cleanup_error:
                    if failure is None:
                        raise
                    if hasattr(failure, "add_note"):
                        failure.add_note(
                            f"Falha adicional ao encerrar BCP no pseudoconsole: {cleanup_error}"
                        )
            if not controlled_close.is_set():
                try:
                    close_exited_pseudoconsole()
                except BaseException as cleanup_error:
                    if failure is None:
                        raise
                    if hasattr(failure, "add_note"):
                        failure.add_note(
                            "Falha adicional ao fechar o pseudoconsole BCP: "
                            + str(cleanup_error)
                        )
            reader.join(timeout=5)
            if reader.is_alive() and failure is None:
                raise RuntimeError("Leitura do pseudoconsole BCP não encerrou após o processo")
            if reader.is_alive() and failure is not None and hasattr(failure, "add_note"):
                failure.add_note("Thread de leitura WinPTY não encerrou em 5 segundos")
        if not password_sent:
            raise AuthError(
                "O BCP não apresentou um prompt de senha reconhecido; versão/adaptador não homologado."
            )
        return int(process.exitstatus or 0), captured.render()


def _private_console_password(invocation: BcpInvocation) -> str:
    if not invocation.password_channel:
        raise AuthError("Canal privado foi chamado sem senha SQL")
    password = invocation.password_channel.reveal()
    if not password:
        raise AuthError("Senha SQL vazia não é aceita pelo canal privado")
    if any(character in password for character in ("\x00", "\r", "\n")):
        raise AuthError(
            "Senha SQL contém caractere incompatível com o prompt do canal privado"
        )
    return password


class PosixPtyConsole:
    """Canal PTY POSIX para responder ao prompt mascarado do BCP.

    O segredo é escrito exclusivamente no descritor mestre do pseudoterminal.
    Ele não entra em argv, ambiente ou arquivo temporário. A saída ainda passa
    pela redação centralizada do :class:`BcpRunner`, inclusive se uma versão
    defeituosa do utilitário ecoar a entrada.
    """

    PROMPT = _PASSWORD_PROMPT

    def available(self) -> bool:
        if os.name != "posix":
            return False
        try:
            import pty  # noqa: F401
            import select  # noqa: F401
            return True
        except ImportError:
            return False

    def run(
        self,
        invocation: BcpInvocation,
        timeout: int,
        *,
        monitor_path: Path | None = None,
        maximum_bytes: int = 2_147_483_648,
        minimum_free: int = 10_737_418_240,
    ) -> tuple[int, str]:
        password = _private_console_password(invocation)
        if not self.available():
            raise AuthError(
                "Autenticação SQL no BCP exige pseudoterminal POSIX disponível; "
                "nenhum fallback inseguro por stdin/argv será usado."
            )
        import pty
        import select

        assert_bcp_has_no_password(invocation, password)
        master_fd, slave_fd = pty.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(invocation.argv),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                shell=False,
                env=minimal_subprocess_environment(),
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        finally:
            if process is not None:
                os.close(slave_fd)

        captured = BoundedOutputTail()
        prompt_window = ""
        password_sent = False
        eof = False
        exit_seen_at: float | None = None
        started = time.monotonic()
        failure: BaseException | None = None
        try:
            while True:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if readable:
                    try:
                        block = os.read(master_fd, 65_536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        block = b""
                    if not block:
                        eof = True
                    else:
                        captured.append(block)
                        prompt_window = (
                            prompt_window + block.decode("utf-8", errors="replace")
                        )[-8192:]
                        if (
                            not password_sent
                            and self.PROMPT.search(_terminal_visible_text(prompt_window))
                        ):
                            payload = bytearray((password + "\n").encode("utf-8"))
                            try:
                                view = memoryview(payload)
                                while view:
                                    written = os.write(master_fd, view)
                                    if written <= 0:
                                        raise RuntimeError(
                                            "Falha ao escrever no pseudoterminal BCP"
                                        )
                                    view = view[written:]
                            finally:
                                payload[:] = b"\x00" * len(payload)
                            password_sent = True

                return_code = process.poll()
                if return_code is not None:
                    exit_seen_at = exit_seen_at or time.monotonic()
                    if eof:
                        break
                    if time.monotonic() - exit_seen_at > 2:
                        raise RuntimeError(
                            "Leitura do pseudoterminal BCP não sinalizou EOF após o processo"
                        )
                if timeout and time.monotonic() - started > timeout:
                    raise RuntimeError("BCP excedeu bcp_timeout_seconds")
                if monitor_path is not None:
                    try:
                        if (
                            monitor_path.exists()
                            and monitor_path.stat().st_size > maximum_bytes
                        ):
                            raise RuntimeError("Arquivo BCP excedeu max_file_bytes")
                        free = shutil.disk_usage(monitor_path.parent).free
                    except OSError as exc:
                        raise RuntimeError(
                            "Não foi possível revalidar o espaço livre durante o BCP"
                        ) from exc
                    if free < minimum_free:
                        raise RuntimeError(
                            "Espaço livre abaixo do mínimo durante a exportação"
                        )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                except BaseException as cleanup_error:
                    if failure is None:
                        raise
                    if hasattr(failure, "add_note"):
                        failure.add_note(
                            "Falha adicional ao encerrar BCP no pseudoterminal: "
                            + str(cleanup_error)
                        )
            os.close(master_fd)
        if not password_sent:
            raise AuthError(
                "O BCP não apresentou um prompt de senha reconhecido; "
                "versão/adaptador não homologado."
            )
        return int(process.returncode or 0), captured.render()


_ROWS_PATTERNS = (
    re.compile(r"(?i)\b([0-9][0-9., ]*)\s+rows? copied\.?"),
    re.compile(r"(?i)\b([0-9][0-9., ]*)\s+linhas? copiadas?\.?"),
    re.compile(r"(?i)\b([0-9][0-9., ]*)\s+filas? copiadas?\.?"),
)


def parse_rows_copied(output: str) -> int:
    def counts(value: str) -> list[int]:
        found: list[int] = []
        for pattern in _ROWS_PATTERNS:
            for match in pattern.finditer(value):
                digits = re.sub(r"\D", "", match.group(1))
                if digits:
                    found.append(int(digits))
        return found

    # WinPTY can place a cursor-control sequence between a progress counter
    # ("Total received: 1000") and the final counter ("1500 rows copied").
    # Removing that sequence with an empty string would fabricate 10001500.
    # Treat controls as record boundaries for numeric parsing instead.
    raw_output = output
    visible = _terminal_visible_text(raw_output)
    progress_chunks: list[int] = []
    for item in re.findall(
            r"(?i)\b([0-9][0-9., ]*)\s+rows?\s+successfully\s+bulk-copied",
            visible,
    ):
        digits = re.sub(r"\D", "", item)
        if digits:
            progress_chunks.append(int(digits))
    progress_total = sum(progress_chunks)

    def without_progress_prefix(candidate: int) -> int:
        combined = str(candidate)
        prefix = str(progress_total)
        if progress_total and combined.startswith(prefix) and len(combined) > len(prefix):
            final_count = int(combined[len(prefix):])
            if final_count >= progress_total:
                return final_count
        return candidate

    # A redraw can split either the number or the final label itself. Parse
    # the reconstructed terminal view first: replacing every control with a
    # newline may leave only a suffix such as ``0 rows copied`` from the real
    # ``1500 rows copied``. ``without_progress_prefix`` also removes the
    # progress counter that ConPTY can concatenate with the final counter.
    visible_matches = counts(visible)
    if visible_matches:
        return without_progress_prefix(visible_matches[-1])

    # Keep a boundary-based fallback for output produced by unusual terminals
    # whose cursor controls intentionally separate otherwise unrelated text.
    matches = counts(_TERMINAL_CONTROL.sub("\n", raw_output))
    if matches:
        return without_progress_prefix(matches[-1])
    raise RuntimeError(
        "Não foi possível obter a contagem real do BCP. O parser deve ser homologado "
        "para a versão/idioma instalado antes de avançar o checkpoint."
    )


class BcpRunner:
    def __init__(
        self,
        *,
        private_console: PrivateConsole | None = None,
        windows_context_adapter: WindowsContextAdapter | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if private_console is not None:
            self.private_console = private_console
        elif os.name == "nt":
            self.private_console = WinPtyConsole()
        else:
            self.private_console = PosixPtyConsole()
        self.windows_context_adapter = windows_context_adapter
        self.redactor = redactor or DEFAULT_REDACTOR

    def preflight(self, invocation: BcpInvocation) -> None:
        assert_bcp_has_no_password(
            invocation,
            invocation.password_channel.reveal() if invocation.password_channel else None,
        )
        if invocation.requires_private_password_channel:
            _private_console_password(invocation)
            if not self.private_console.available():
                hint = (
                    "instale pywinpty e homologue o prompt da versão instalada"
                    if os.name == "nt"
                    else "use um executor POSIX com suporte nativo a PTY"
                )
                raise AuthError(
                    "BCP SQL requer pseudoconsole privado antes da execução; " + hint + "."
                )
        if invocation.requires_windows_context and self.windows_context_adapter is None:
            raise AuthError(
                "windows_credentials requer WindowsContextAdapter homologado para iniciar o BCP"
            )

    @staticmethod
    def require_version(executable: str) -> str:
        process = subprocess.Popen(
            [executable, "-v"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False, env=minimal_subprocess_environment(),
        )
        assert process.stdout is not None
        captured = BoundedOutputTail()
        reader_errors: list[BaseException] = []

        def consume() -> None:
            try:
                with process.stdout as stream:
                    for block in iter(lambda: stream.read(65536), b""):
                        captured.append(block)
            except BaseException as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=consume, name="bcp-version-output", daemon=True)
        reader.start()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("Consulta de versão do BCP excedeu 20 segundos") from None
        finally:
            reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError("Leitura da versão do BCP não encerrou")
        if reader_errors:
            raise RuntimeError("Falha ao ler a versão do BCP") from reader_errors[0]
        output = captured.render()
        versions = re.findall(r"\b(\d{1,2})\.\d+(?:\.\d+)?", output)
        if process.returncode or not versions or int(versions[0]) < 17:
            raise RuntimeError(
                "BCP 17+ com controles TLS é obrigatório. Verifique 'bcp -v'; "
                "não desative certificado/TLS como contorno."
            )
        if int(versions[0]) == 17:
            # Algumas revisoes atuais das Command Line Utilities continuam
            # identificadas como 17.x, mas ja oferecem os controles TLS -Y/-u.
            # Homologue a capacidade real em vez de confiar apenas no major.
            try:
                help_process = subprocess.run(
                    [executable, "-?"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    env=minimal_subprocess_environment(),
                    timeout=20,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("Consulta de capacidade do BCP excedeu 20 segundos") from None
            help_output = help_process.stdout.decode("utf-8", errors="replace")
            has_tls_mode = re.search(r"-Y(?:\[|[smo]|\s)", help_output)
            has_trust_switch = re.search(r"-u(?:\s|$)", help_output)
            if not has_tls_mode or not has_trust_switch:
                raise RuntimeError(
                    "BCP 17 detectado sem os switches TLS -Y e -u exigidos; "
                    "atualize as Command Line Utilities."
                )
        return versions[0]

    def _run_standard(self, invocation: BcpInvocation, timeout: int, monitor_path: Path | None,
                      maximum_bytes: int, minimum_free: int) -> tuple[int, str]:
        process = subprocess.Popen(
            list(invocation.argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False, env=minimal_subprocess_environment(),
        )
        assert process.stdout is not None
        captured = BoundedOutputTail()
        reader_errors: list[BaseException] = []

        def consume() -> None:
            try:
                with process.stdout as stream:
                    while True:
                        block = stream.read(65536)
                        if not block:
                            return
                        captured.append(block)
            except BaseException as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=consume, name="bcp-output", daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                if timeout and time.monotonic() - started > timeout:
                    raise RuntimeError("BCP excedeu bcp_timeout_seconds")
                if monitor_path is not None:
                    if monitor_path.exists() and monitor_path.stat().st_size > maximum_bytes:
                        raise RuntimeError("Arquivo BCP excedeu max_file_bytes")
                    try:
                        free = shutil.disk_usage(monitor_path.parent).free
                    except OSError as exc:
                        raise RuntimeError("Não foi possível revalidar o espaço livre durante o BCP") from exc
                    if free < minimum_free:
                        raise RuntimeError("Espaço livre abaixo do mínimo durante a exportação")
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError("Leitura da saída BCP não encerrou após o processo")
        if reader_errors:
            raise RuntimeError("Falha ao ler a saída BCP") from reader_errors[0]
        output = captured.render()
        return int(process.returncode or 0), output

    def run(
        self,
        invocation: BcpInvocation,
        log_path: Path,
        *,
        monitor_path: Path | None = None,
        timeout: int = 0,
        maximum_bytes: int = 2_147_483_648,
        minimum_free: int = 10_737_418_240,
        expect_row_count: bool = True,
    ) -> BcpResult:
        self.preflight(invocation)
        if len(subprocess.list2cmdline(list(invocation.argv))) > 30_000:
            raise RuntimeError("Comando BCP excede o limite seguro da linha de comando Windows")
        started = time.perf_counter()
        if invocation.requires_windows_context:
            assert self.windows_context_adapter is not None
            result = self.windows_context_adapter.start_bcp(
                invocation,
                timeout=timeout,
                monitor_path=monitor_path,
                maximum_bytes=maximum_bytes,
                minimum_free=minimum_free,
            )
            if isinstance(result, tuple):
                code, output = result
            else:
                code, output = result.return_code, result.output
        elif invocation.requires_private_password_channel:
            code, output = self.private_console.run(
                invocation,
                timeout,
                monitor_path=monitor_path,
                maximum_bytes=maximum_bytes,
                minimum_free=minimum_free,
            )
        else:
            code, output = self._run_standard(
                invocation, timeout, monitor_path, maximum_bytes, minimum_free
            )
        # A child can append its last buffer between the final polling cycle
        # and process termination. Recheck both hard limits before the caller
        # is allowed to publish or import the artifact.
        if monitor_path is not None:
            try:
                if monitor_path.exists() and monitor_path.stat().st_size > maximum_bytes:
                    raise RuntimeError("Arquivo BCP excedeu max_file_bytes")
                free = shutil.disk_usage(monitor_path.parent).free
            except OSError as exc:
                raise RuntimeError(
                    "Nao foi possivel revalidar tamanho e espaco livre apos o BCP"
                ) from exc
            if free < minimum_free:
                raise RuntimeError("Espaco livre abaixo do minimo apos o BCP")
        duration = time.perf_counter() - started
        safe_output = self.redactor.redact(_terminal_visible_text(output))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(safe_output, encoding="utf-8", newline="")
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        if code:
            raise RuntimeError(f"BCP retornou {code}; consulte o log protegido: {log_path}")
        rows = parse_rows_copied(output) if expect_row_count else None
        if expect_row_count and rows == 0 and monitor_path is not None:
            try:
                artifact_bytes = monitor_path.stat().st_size if monitor_path.exists() else 0
            except OSError as exc:
                raise RuntimeError(
                    "Não foi possível validar o artefato após obter zero linhas do BCP"
                ) from exc
            if artifact_bytes > 0:
                raise RuntimeError(
                    "BCP informou zero linhas, mas produziu um artefato não vazio; "
                    "o checkpoint foi bloqueado para evitar perda silenciosa de dados."
                )
        return BcpResult(code, rows, duration, safe_output)


def make_format_file(
    runner: BcpRunner,
    base_invocation: BcpInvocation,
    source_name: str,
    columns: list[dict[str, Any]],
    path: Path,
    log_path: Path,
    *,
    timeout: int = 0,
) -> None:
    # ``nul`` is a reserved device only on Windows.  On POSIX it is an ordinary
    # relative filename, so bcp may fail while trying to open it (and before it
    # even reaches the password prompt).  ``os.devnull`` provides the native
    # spelling on both platforms: ``nul`` on Windows and ``/dev/null`` on POSIX.
    invocation = base_invocation.with_operation(
        source_name, "format", os.devnull, "-x", "-f", str(path)
    )
    runner.run(invocation, log_path, timeout=timeout, minimum_free=0, expect_row_count=False)
    ET.register_namespace("", NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    tree = ET.parse(path)
    root = tree.getroot()
    row = root.find("{" + NS + "}ROW")
    record = root.find("{" + NS + "}RECORD")
    if row is None or record is None:
        raise RuntimeError("Format file XML BCP inválido")
    projected_names = [str(column["name"]) for column in columns]
    elements = list(row)
    if [element.attrib.get("NAME") for element in elements] != projected_names:
        raise RuntimeError("Format file não corresponde à projeção exportada")
    fields = {element.attrib["ID"]: element for element in list(record)}
    for column, element in zip(columns, elements):
        if int(column.get("max_length", 0)) == -1 or column.get("type_name") in {"text", "ntext", "image", "xml"}:
            fields[element.attrib["SOURCE"]].set("MAX_LENGTH", "2147483647")
    partial = path.with_suffix(path.suffix + ".partial")
    tree.write(partial, encoding="utf-8", xml_declaration=True)
    os.replace(partial, path)
