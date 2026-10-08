"""Autenticação, resolução de segredos e planos seguros de conexão/processo.

Este módulo monta pedidos; ele não executa silenciosamente credenciais Windows
explícitas no contexto do processo atual. Esse modo exige um
``WindowsContextAdapter`` homologado pelo chamador.
"""

from __future__ import annotations

import getpass
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .runtime import resolve_bcp_executable
from .util import DEFAULT_REDACTOR, REDACTED, SecretRedactor, ensure_no_secret_arguments


class AuthError(RuntimeError):
    pass


class ExplicitWindowsContextRequired(AuthError):
    pass


class SecretValue:
    """Valor em memória cuja representação nunca revela o conteúdo."""

    __slots__ = ("__value",)

    def __init__(self, value: str, redactor: SecretRedactor | None = None) -> None:
        if not isinstance(value, str):
            raise TypeError("Segredo resolvido deve ser texto")
        self.__value = value
        DEFAULT_REDACTOR.register(value)
        if redactor is not None and redactor is not DEFAULT_REDACTOR:
            redactor.register(value)

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "SecretValue('********')"

    def __str__(self) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self.__value)


@dataclass(frozen=True)
class WindowsExecutionContext:
    domain: str
    username: str
    password: SecretValue = field(repr=False)

    @property
    def principal(self) -> str:
        return f"{self.domain}\\{self.username}"


class WindowsContextAdapter(ABC):
    """Fronteira para helper/launcher Windows auditado.

    Uma implementação deve criar tanto conexões ODBC quanto processos BCP sob
    o token indicado e validar a identidade efetiva. A classe abstrata impede o
    motor de alegar suporte usando ``UID=DOMINIO\\usuario``.
    """

    @abstractmethod
    def connect_odbc(self, request: "OdbcConnectionRequest", **kwargs: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def start_bcp(self, invocation: "BcpInvocation", **kwargs: Any) -> Any:
        raise NotImplementedError


@dataclass(frozen=True, repr=False)
class OdbcConnectionRequest:
    endpoint_name: str
    authentication_type: str
    connection_string: str = field(repr=False)
    windows_context: WindowsExecutionContext | None = field(default=None, repr=False)
    configured_principal: str | None = None
    connection_timeout_seconds: int = 30
    sql_timeout_seconds: int = 0

    @property
    def requires_windows_context(self) -> bool:
        return self.windows_context is not None

    @property
    def redacted_connection_string(self) -> str:
        return DEFAULT_REDACTOR.redact(self.connection_string)

    def __repr__(self) -> str:
        context = self.windows_context.principal if self.windows_context else None
        return (
            "OdbcConnectionRequest("
            f"endpoint_name={self.endpoint_name!r}, "
            f"authentication_type={self.authentication_type!r}, "
            f"connection_string={self.redacted_connection_string!r}, "
            f"configured_principal={self.configured_principal!r}, "
            f"connection_timeout_seconds={self.connection_timeout_seconds!r}, "
            f"sql_timeout_seconds={self.sql_timeout_seconds!r}, "
            f"windows_context={context!r})"
        )


@dataclass(frozen=True, repr=False)
class BcpInvocation:
    endpoint_name: str
    executable: str
    arguments: tuple[str, ...]
    authentication_type: str
    password_channel: SecretValue | None = field(default=None, repr=False)
    windows_context: WindowsExecutionContext | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        ensure_no_secret_arguments(self.arguments)

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.executable, *self.arguments)

    @property
    def requires_private_password_channel(self) -> bool:
        return self.password_channel is not None

    @property
    def requires_windows_context(self) -> bool:
        return self.windows_context is not None

    def with_operation(self, *arguments: str) -> "BcpInvocation":
        complete = tuple(arguments) + self.arguments
        ensure_no_secret_arguments(complete)
        return BcpInvocation(
            endpoint_name=self.endpoint_name,
            executable=self.executable,
            arguments=complete,
            authentication_type=self.authentication_type,
            password_channel=self.password_channel,
            windows_context=self.windows_context,
        )

    def __repr__(self) -> str:
        return (
            "BcpInvocation("
            f"endpoint_name={self.endpoint_name!r}, executable={self.executable!r}, "
            f"arguments={self.arguments!r}, authentication_type={self.authentication_type!r}, "
            f"private_password_channel={self.requires_private_password_channel!r}, "
            f"windows_context={self.windows_context.principal if self.windows_context else None!r})"
        )


def _read_windows_credential(target: str) -> str:
    """Lê uma credencial Generic do Windows Credential Manager por CredReadW."""

    if os.name != "nt":
        raise AuthError("Windows Credential Manager só está disponível em executor Windows")
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        pointer = ctypes.POINTER(CREDENTIALW)()
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        cred_read = advapi.CredReadW
        cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        cred_read.restype = wintypes.BOOL
        cred_free = advapi.CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = None
        CRED_TYPE_GENERIC = 1
        if not cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            raise AuthError(
                f"Credencial Windows não encontrada ou inacessível para a referência {target!r} "
                f"(erro Win32 {code})"
            )
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            cred_free(pointer)
    except AuthError:
        raise
    except Exception as error:
        raise AuthError(f"Falha ao acessar Windows Credential Manager: {error}") from None
    try:
        return blob.decode("utf-16-le")
    except UnicodeDecodeError:
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuthError("CredentialBlob não contém texto UTF-16LE/UTF-8 suportado") from error


class SecretResolver:
    """Resolve cada referência uma vez por execução e registra-a no redator."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        prompt: Callable[[str], str] | None = None,
        credential_reader: Callable[[str], str] | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.environment = environment if environment is not None else os.environ
        self.prompt = prompt or getpass.getpass
        self.credential_reader = credential_reader or _read_windows_credential
        self.redactor = redactor or DEFAULT_REDACTOR
        self._cache: dict[tuple[str, str], SecretValue] = {}

    def resolve(
        self,
        spec: Mapping[str, Any],
        *,
        credential_key: str,
        prompt_label: str | None = None,
    ) -> SecretValue:
        provider = spec.get("provider")
        reference = spec.get("reference")
        if provider == "prompt":
            key = ("prompt", credential_key)
        elif provider in {"env", "windows_credential_manager"} and isinstance(reference, str) and reference:
            # A mesma referência em dois endpoints é compartilhamento explícito,
            # nunca inferência por usuário/host.
            key = (provider, reference)
        else:
            raise AuthError("Descritor de segredo inválido ou não validado")
        if key in self._cache:
            return self._cache[key]
        if provider == "prompt":
            value = self.prompt(prompt_label or f"Senha para {credential_key}: ")
        elif provider == "env":
            if reference not in self.environment:
                raise AuthError(f"Variável de ambiente de segredo não definida: {reference}")
            value = self.environment[reference]
        else:
            value = self.credential_reader(reference)
        if not isinstance(value, str):
            raise AuthError("Provedor de segredo retornou valor que não é texto")
        secret = SecretValue(value, self.redactor)
        self._cache[key] = secret
        return secret

    def resolve_auth(self, auth: Mapping[str, Any], *, endpoint_name: str) -> SecretValue | None:
        kind = auth["type"]
        if kind == "windows_integrated":
            return None
        principal = (
            f"{auth['domain']}\\{auth['username']}"
            if kind == "windows_credentials" else auth["username"]
        )
        return self.resolve(
            auth["password"],
            credential_key=f"{endpoint_name}:{kind}:{principal}",
            prompt_label=f"Senha de {principal} para {endpoint_name}: ",
        )

    def clear_cache(self) -> None:
        # Strings Python não oferecem zeroização confiável; remover referências é
        # a garantia honesta disponível neste processo.
        self._cache.clear()


def _brace(value: Any) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def _odbc_values(values: Mapping[str, Any]) -> str:
    # O Driver Manager do Windows resolve DSN sem chaves; os demais valores
    # continuam sempre protegidos contra separadores da connection string.
    return ";".join(
        f"{key}={value if key == 'DSN' else _brace(value)}"
        for key, value in values.items()
    )


def _tls_options(endpoint: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    tls = dict(config.get("tls", {}))
    tls.update(endpoint.get("tls", {}))
    return tls


def sql_server_target(endpoint: Mapping[str, Any]) -> str:
    """Return the explicit SQL Server network target used by BCP and non-DSN ODBC.

    Instance and TCP port are separate configuration values so operators can
    review routing without parsing a vendor-specific server string.  A DSN may
    override this value for pyodbc connections, but BCP always uses this target.
    """

    instance = str(endpoint["instance"])
    port = int(endpoint.get("port", 1433))
    return f"{instance},{port}"


def _odbc_base(endpoint: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    database = endpoint.get("read_database") or endpoint["database"]
    tls = _tls_options(endpoint, config)
    values: dict[str, Any] = {
        "DATABASE": database,
        "Encrypt": "yes" if tls.get("encrypt", True) else "no",
        "TrustServerCertificate": "yes" if tls.get("trust_server_certificate", False) else "no",
        "APP": "Data_Export_Import_Engine",
    }
    if endpoint.get("odbc_dsn"):
        values = {"DSN": endpoint["odbc_dsn"], **values}
    else:
        values = {
            "DRIVER": endpoint.get(
                "odbc_driver",
                config.get("odbc_driver", "ODBC Driver 18 for SQL Server"),
            ),
            "SERVER": sql_server_target(endpoint),
            **values,
        }
    if tls.get("hostname_in_certificate"):
        values["HostNameInCertificate"] = tls["hostname_in_certificate"]
    return values


def build_odbc_connection_string(
    endpoint: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    password: SecretValue | str | None = None,
) -> str:
    """Monta string ODBC integrada/SQL; recusa domínio explícito no processo atual."""

    auth = endpoint["authentication"]
    kind = auth["type"]
    if kind == "windows_credentials":
        raise ExplicitWindowsContextRequired(
            "windows_credentials exige WindowsContextAdapter; UID=DOMINIO\\usuario não implementa SSPI"
        )
    values = _odbc_base(endpoint, config)
    if kind == "windows_integrated":
        values["Trusted_Connection"] = "yes"
    elif kind == "sql":
        if password is None:
            raise AuthError("Senha SQL deve ser resolvida antes de montar a conexão")
        raw_password = password.reveal() if isinstance(password, SecretValue) else str(password)
        DEFAULT_REDACTOR.register(raw_password)
        values["UID"] = auth["username"]
        values["PWD"] = raw_password
    else:
        raise AuthError(f"Tipo de autenticação não suportado: {kind}")
    return _odbc_values(values)


def build_odbc_request(
    endpoint: Mapping[str, Any],
    config: Mapping[str, Any],
    resolver: SecretResolver,
    *,
    endpoint_name: str,
) -> OdbcConnectionRequest:
    auth = endpoint["authentication"]
    kind = auth["type"]
    secret = resolver.resolve_auth(auth, endpoint_name=endpoint_name)
    connection_timeout = int(
        endpoint.get(
            "connection_timeout_seconds",
            config.get("connection_timeout_seconds", 30),
        )
    )
    sql_timeout = int(
        endpoint.get("sql_timeout_seconds", config.get("sql_timeout_seconds", 0))
    )
    if kind == "windows_credentials":
        assert secret is not None
        context = WindowsExecutionContext(auth["domain"], auth["username"], secret)
        values = _odbc_base(endpoint, config)
        values["Trusted_Connection"] = "yes"
        connection_string = _odbc_values(values)
        return OdbcConnectionRequest(
            endpoint_name,
            kind,
            connection_string,
            context,
            context.principal,
            connection_timeout,
            sql_timeout,
        )
    return OdbcConnectionRequest(
        endpoint_name,
        kind,
        build_odbc_connection_string(endpoint, config, password=secret),
        None,
        str(auth["username"]) if kind == "sql" else None,
        connection_timeout,
        sql_timeout,
    )


def build_bcp_invocation(
    endpoint: Mapping[str, Any],
    config: Mapping[str, Any],
    resolver: SecretResolver,
    *,
    endpoint_name: str,
) -> BcpInvocation:
    """Monta os argumentos-base BCP sem jamais incluir ``-P`` ou a senha."""

    auth = endpoint["authentication"]
    kind = auth["type"]
    database = endpoint.get("read_database") or endpoint["database"]
    arguments = ["-S", sql_server_target(endpoint), "-d", database]
    secret = resolver.resolve_auth(auth, endpoint_name=endpoint_name)
    context = None
    password_channel = None
    if kind == "windows_integrated":
        arguments.append("-T")
    elif kind == "windows_credentials":
        assert secret is not None
        context = WindowsExecutionContext(auth["domain"], auth["username"], secret)
        arguments.append("-T")
    elif kind == "sql":
        assert secret is not None
        arguments.extend(["-U", auth["username"]])
        password_channel = secret
    else:
        raise AuthError(f"Tipo de autenticação não suportado: {kind}")
    arguments.extend(["-n", "-C", "RAW"])
    tls = _tls_options(endpoint, config)
    switch = tls.get("bcp_switch", "-Ym")
    if switch:
        arguments.append(switch)
    if tls.get("trust_server_certificate", False):
        arguments.append("-u")
    connection_timeout = endpoint.get(
        "connection_timeout_seconds", config.get("connection_timeout_seconds", 30)
    )
    arguments.extend(["-l", str(int(connection_timeout))])
    ensure_no_secret_arguments(arguments)
    requested_executable = str(config.get("bcp_executable", "bcp"))
    resolved_executable = resolve_bcp_executable(requested_executable)
    return BcpInvocation(
        endpoint_name=endpoint_name,
        # Request construction remains available before native prerequisites
        # are installed. Execution still performs the existing version
        # preflight; an installed deployment gets the absolute path above and
        # therefore does not depend on a stale process PATH.
        executable=resolved_executable or requested_executable,
        arguments=tuple(arguments),
        authentication_type=kind,
        password_channel=password_channel,
        windows_context=context,
    )


def bcp_base_args(
    endpoint: Mapping[str, Any],
    config: Mapping[str, Any],
    resolver: SecretResolver,
    *,
    endpoint_name: str,
) -> tuple[str, ...]:
    return build_bcp_invocation(
        endpoint, config, resolver, endpoint_name=endpoint_name
    ).arguments


def minimal_subprocess_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Ambiente mínimo para BCP/helpers; não herda variáveis de segredo arbitrárias."""

    source = environment if environment is not None else os.environ
    allowed = {
        "COMSPEC", "LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMDRIVE",
        "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
    }
    return {key: value for key, value in source.items() if key.upper() in allowed}


def assert_bcp_has_no_password(invocation: BcpInvocation, known_secret: str | None = None) -> None:
    ensure_no_secret_arguments(invocation.argv)
    if not known_secret:
        return
    # O contrato do laboratório usa propositalmente login e senha iguais. O
    # valor logo após -U é um identificador público obrigatório e não pode ser
    # confundido com o transporte da senha. Qualquer outra ocorrência continua
    # sendo recusada, além da proibição categórica de -P acima.
    username_positions = {
        index + 1
        for index, argument in enumerate(invocation.argv[:-1])
        if argument == "-U"
    }
    leaked = any(
        known_secret in argument
        and not (index in username_positions and argument == known_secret)
        for index, argument in enumerate(invocation.argv)
    )
    if leaked:
        raise AuthError("Senha encontrada nos argumentos BCP")


def authentication_identity(auth: Mapping[str, Any]) -> dict[str, str]:
    """Descrição segura para manifestos/relatórios, sem referência nem senha."""

    kind = str(auth["type"])
    result = {"type": kind}
    if kind == "windows_integrated":
        result["configured_principal"] = "<identidade_windows_do_processo>"
    elif kind == "windows_credentials":
        result["configured_principal"] = f"{auth['domain']}\\{auth['username']}"
        result["secret_provider"] = str(auth["password"]["provider"])
    elif kind == "sql":
        result["configured_principal"] = str(auth["username"])
        result["secret_provider"] = str(auth["password"]["provider"])
    else:
        raise AuthError(f"Tipo de autenticação não suportado: {kind}")
    return result
