"""Conexões ODBC e launcher Windows para credenciais explícitas."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping

from .auth import (
    AuthError,
    BcpInvocation,
    OdbcConnectionRequest,
    SecretResolver,
    WindowsContextAdapter,
    assert_bcp_has_no_password,
    build_odbc_request,
    minimal_subprocess_environment,
)
from .bcp import BoundedOutputTail
from .sql import configure_session, fetch_dicts
from .util import DEFAULT_REDACTOR


@dataclass(frozen=True)
class ConnectionIdentity:
    endpoint_name: str
    authentication_type: str
    system_user: str
    original_login: str
    suser_sname: str

    def as_dict(self) -> dict[str, str]:
        return {
            "endpoint": self.endpoint_name,
            "authentication_type": self.authentication_type,
            "system_user": self.system_user,
            "original_login": self.original_login,
            "suser_sname": self.suser_sname,
        }


def verify_connection_identity(connection: Any, request: OdbcConnectionRequest) -> ConnectionIdentity:
    row = fetch_dicts(connection, """
SELECT CONVERT(nvarchar(256),SYSTEM_USER) AS [system_user],
       CONVERT(nvarchar(256),ORIGINAL_LOGIN()) AS [original_login],
       CONVERT(nvarchar(256),SUSER_SNAME()) AS [suser_sname];""")[0]
    identity = ConnectionIdentity(
        request.endpoint_name,
        request.authentication_type,
        str(row["system_user"]),
        str(row["original_login"]),
        str(row["suser_sname"]),
    )
    if request.windows_context:
        expected = request.windows_context.principal.casefold()
        observed = {identity.system_user.casefold(), identity.original_login.casefold(), identity.suser_sname.casefold()}
        if expected not in observed:
            raise AuthError(
                f"Identidade Windows efetiva divergente em {request.endpoint_name}: "
                f"esperado={request.windows_context.principal}, observado={identity.as_dict()}"
            )
    elif request.authentication_type == "sql" and request.configured_principal:
        expected = request.configured_principal.casefold()
        observed = {
            identity.system_user.casefold(), identity.original_login.casefold(),
            identity.suser_sname.casefold(),
        }
        if expected not in observed:
            raise AuthError(
                f"Identidade SQL efetiva divergente em {request.endpoint_name}: "
                f"esperado={request.configured_principal}, observado={identity.as_dict()}"
            )
    return identity


def _pyodbc_connect(request: OdbcConnectionRequest, config: Mapping[str, Any]) -> Any:
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("Instale as dependências com: python -m pip install -r requirements.txt") from exc
    try:
        connection = pyodbc.connect(
            request.connection_string,
            autocommit=True,
            timeout=request.connection_timeout_seconds,
        )
        connection.timeout = request.sql_timeout_seconds
        configure_session(connection)
        return connection
    except Exception as exc:
        raise AuthError(DEFAULT_REDACTOR.exception_text(exc)) from None


class WindowsCredentialAdapter(WindowsContextAdapter):
    """Implementação Windows usando LogonUser/impersonação e CreateProcessWithLogonW.

    ``LOGON_NEW_CREDENTIALS``/``LOGON_NETCREDENTIALS_ONLY`` cria um contexto de
    rede separado. Criar o processo, sozinho, não comprova a autenticação; por
    isso a conexão ODBC consulta e valida a identidade efetiva.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise AuthError("windows_credentials exige executor Windows")

    def connect_odbc(self, request: OdbcConnectionRequest, **kwargs: Any) -> Any:
        if request.windows_context is None:
            return _pyodbc_connect(request, kwargs.get("config", {}))
        import ctypes
        from ctypes import wintypes

        try:
            import pyodbc
            pyodbc.pooling = False
        except ImportError as exc:
            raise RuntimeError("Instale pyodbc conforme requirements.txt") from exc
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        logon = advapi.LogonUserW
        logon.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        logon.restype = wintypes.BOOL
        impersonate = advapi.ImpersonateLoggedOnUser
        impersonate.argtypes = [wintypes.HANDLE]
        impersonate.restype = wintypes.BOOL
        revert = advapi.RevertToSelf
        revert.argtypes = []
        revert.restype = wintypes.BOOL
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        context = request.windows_context
        if not logon(context.username, context.domain, context.password.reveal(), 9, 3, ctypes.byref(token)):
            raise AuthError(f"LogonUserW falhou (erro Win32 {ctypes.get_last_error()})")
        try:
            if not impersonate(token):
                raise AuthError(f"ImpersonateLoggedOnUser falhou (erro Win32 {ctypes.get_last_error()})")
            try:
                connection = _pyodbc_connect(request, kwargs.get("config", {}))
                verify_connection_identity(connection, request)
                return connection
            finally:
                if not revert():
                    raise AuthError(f"RevertToSelf falhou (erro Win32 {ctypes.get_last_error()})")
        finally:
            close_handle(token)

    def start_bcp(self, invocation: BcpInvocation, **kwargs: Any) -> tuple[int, str]:
        if invocation.windows_context is None:
            raise AuthError("Launcher Windows foi chamado sem contexto explícito")
        assert_bcp_has_no_password(
            invocation, invocation.windows_context.password.reveal()
        )
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        create_pipe = kernel.CreatePipe
        create_pipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
        ]
        create_pipe.restype = wintypes.BOOL
        set_handle_information = kernel.SetHandleInformation
        set_handle_information.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        set_handle_information.restype = wintypes.BOOL
        create_file = kernel.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        wait_for_single = kernel.WaitForSingleObject
        wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single.restype = wintypes.DWORD
        terminate_process = kernel.TerminateProcess
        terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate_process.restype = wintypes.BOOL
        get_exit_code = kernel.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        create_process = advapi.CreateProcessWithLogonW
        create_process.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        create_process.restype = wintypes.BOOL
        read_handle, write_handle = wintypes.HANDLE(), wintypes.HANDLE()
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        if not create_pipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(sa), 0):
            raise AuthError(f"CreatePipe falhou (erro Win32 {ctypes.get_last_error()})")
        HANDLE_FLAG_INHERIT = 1
        if not set_handle_information(read_handle, HANDLE_FLAG_INHERIT, 0):
            close_handle(read_handle)
            close_handle(write_handle)
            raise AuthError(f"SetHandleInformation falhou (erro Win32 {ctypes.get_last_error()})")
        GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 1, 3
        nul = create_file("NUL", GENERIC_READ, FILE_SHARE_READ, ctypes.byref(sa),
                          OPEN_EXISTING, 0, None)
        if nul == wintypes.HANDLE(-1).value:
            close_handle(read_handle)
            close_handle(write_handle)
            raise AuthError(f"CreateFileW(NUL) falhou (erro Win32 {ctypes.get_last_error()})")
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = nul, write_handle, write_handle
        info = PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(invocation.argv)))
        environment = minimal_subprocess_environment()
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0"
        )
        context = invocation.windows_context
        LOGON_NETCREDENTIALS_ONLY = 0x2
        CREATE_UNICODE_ENVIRONMENT, CREATE_NO_WINDOW = 0x400, 0x08000000
        captured = BoundedOutputTail()
        reader_errors: list[BaseException] = []
        reader: threading.Thread | None = None
        fd: int | None = None
        reader_owns_fd = False
        timeout = int(kwargs.get("timeout", 0))
        monitor_value = kwargs.get("monitor_path")
        monitor_path = Path(monitor_value) if monitor_value is not None else None
        maximum = int(kwargs.get("maximum_bytes", 2_147_483_648))
        minimum = int(kwargs.get("minimum_free", 10_737_418_240))
        started = time.monotonic()
        WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED, STILL_ACTIVE = 0, 258, 0xFFFFFFFF, 259

        def terminate_child() -> None:
            """Encerra e espera o filho; idempotente se ele já terminou."""

            wait_state = int(wait_for_single(info.hProcess, 0))
            if wait_state == WAIT_OBJECT_0:
                return
            if not terminate_process(info.hProcess, 1):
                # Uma corrida de saída é aceitável somente se agora terminou.
                if int(wait_for_single(info.hProcess, 0)) != WAIT_OBJECT_0:
                    raise RuntimeError(
                        f"TerminateProcess falhou (erro Win32 {ctypes.get_last_error()})"
                    )
                return
            if int(wait_for_single(info.hProcess, 10_000)) != WAIT_OBJECT_0:
                raise RuntimeError("Processo BCP não encerrou após TerminateProcess")

        def consume() -> None:
            assert fd is not None
            try:
                with os.fdopen(fd, "rb", closefd=True) as stream:
                    while True:
                        data = stream.read(65536)
                        if not data:
                            break
                        captured.append(data)
            except BaseException as exc:
                reader_errors.append(exc)

        created = create_process(
            context.username, context.domain, context.password.reveal(), LOGON_NETCREDENTIALS_ONLY,
            None, command, CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            ctypes.cast(environment_block, ctypes.c_void_p), None,
            ctypes.byref(startup), ctypes.byref(info),
        )
        if not created:
            close_handle(write_handle)
            close_handle(nul)
            close_handle(read_handle)
            raise AuthError(f"CreateProcessWithLogonW falhou (erro Win32 {ctypes.get_last_error()})")
        write_parent_open = True
        nul_parent_open = True
        try:
            if not close_handle(write_handle):
                raise RuntimeError(
                    f"CloseHandle(pipe escrita) falhou (erro Win32 {ctypes.get_last_error()})"
                )
            write_parent_open = False
            if not close_handle(nul):
                raise RuntimeError(
                    f"CloseHandle(NUL) falhou (erro Win32 {ctypes.get_last_error()})"
                )
            nul_parent_open = False
            # open_osfhandle transfere a propriedade do HANDLE para o descritor.
            # A thread/stream passa a ser a única responsável por fechá-lo.
            fd = msvcrt.open_osfhandle(int(read_handle.value), os.O_RDONLY)
            reader = threading.Thread(target=consume, name="bcp-windows-output", daemon=True)
            reader.start()
            reader_owns_fd = True

            while True:
                wait_state = int(wait_for_single(info.hProcess, 500))
                if wait_state == WAIT_OBJECT_0:
                    break
                if wait_state == WAIT_FAILED:
                    raise RuntimeError(
                        f"WaitForSingleObject falhou (erro Win32 {ctypes.get_last_error()})"
                    )
                if wait_state != WAIT_TIMEOUT:
                    raise RuntimeError(f"WaitForSingleObject retornou estado inesperado {wait_state}")
                if reader_errors:
                    raise RuntimeError("Falha ao ler a saída BCP") from reader_errors[0]
                if timeout and time.monotonic() - started > timeout:
                    raise RuntimeError("BCP excedeu bcp_timeout_seconds")
                if monitor_path is not None:
                    try:
                        if monitor_path.exists() and monitor_path.stat().st_size > maximum:
                            raise RuntimeError("Arquivo BCP excedeu max_file_bytes")
                        free = shutil.disk_usage(monitor_path.parent).free
                    except OSError as exc:
                        raise RuntimeError(
                            "Não foi possível revalidar o espaço livre durante o BCP"
                        ) from exc
                    if free < minimum:
                        raise RuntimeError("Espaço livre abaixo do mínimo durante o BCP")
            code = wintypes.DWORD(STILL_ACTIVE)
            if not get_exit_code(info.hProcess, ctypes.byref(code)):
                raise RuntimeError(
                    f"GetExitCodeProcess falhou (erro Win32 {ctypes.get_last_error()})"
                )
            if int(code.value) == STILL_ACTIVE:
                raise RuntimeError("Processo BCP sinalizou saída, mas ainda está ativo")
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("Leitura da saída BCP não encerrou após o processo")
            if reader_errors:
                raise RuntimeError("Falha ao ler a saída BCP") from reader_errors[0]
            return int(code.value), captured.render()
        except BaseException as exc:
            try:
                terminate_child()
            except BaseException as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Falha adicional ao encerrar BCP: {cleanup_error}")
            raise
        finally:
            if write_parent_open:
                close_handle(write_handle)
            if nul_parent_open:
                close_handle(nul)
            if reader is not None and reader.is_alive():
                reader.join(timeout=5)
            if fd is not None and not reader_owns_fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
            elif fd is None:
                close_handle(read_handle)
            close_handle(info.hThread)
            close_handle(info.hProcess)


class ConnectionFactory:
    def __init__(self, resolver: SecretResolver, windows_adapter: WindowsContextAdapter | None = None) -> None:
        self.resolver = resolver
        self.windows_adapter = windows_adapter
        if self.windows_adapter is None and os.name == "nt":
            self.windows_adapter = WindowsCredentialAdapter()

    def connect(
        self,
        endpoint: Mapping[str, Any],
        config: Mapping[str, Any],
        *,
        endpoint_name: str,
    ) -> tuple[Any, ConnectionIdentity]:
        request = build_odbc_request(endpoint, config, self.resolver, endpoint_name=endpoint_name)
        if request.requires_windows_context:
            if self.windows_adapter is None:
                raise AuthError("Nenhum WindowsContextAdapter disponível")
            connection = self.windows_adapter.connect_odbc(request, config=config)
        else:
            connection = _pyodbc_connect(request, config)
        identity = verify_connection_identity(connection, request)
        return connection, identity
