"""Utilitários compartilhados do BulkFlow."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


REDACTED = "********"
SCALAR_SQL_TYPES = {
    "bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric",
    "float", "real", "money", "smallmoney", "date", "datetime", "smalldatetime",
    "datetime2", "datetimeoffset", "time", "char", "varchar", "nchar", "nvarchar",
    "binary", "varbinary", "uniqueidentifier", "xml", "timestamp", "rowversion",
    "text", "ntext", "image",
}
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|pwd|senha|secret|segredo|token)(?:$|_)", re.IGNORECASE
)
_ASSIGNMENT_PATTERNS = (
    re.compile(r"(?i)(\b(?:PWD|PASSWORD|SENHA)\s*=\s*)(?:\{(?:[^}]|}})*\}|[^;\s]*)"),
    re.compile(r"(?i)(\b(?:--password|--senha|-P)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"),
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_canonical_uuid(value: Any, field: str = "uuid") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} deve ser UUID canônico em texto")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} deve ser UUID canônico em texto") from exc
    if value != str(parsed):
        raise ValueError(
            f"{field} deve usar a forma UUID canônica lowercase com hífens"
        )
    return value


def validate_sha256_hex(value: Any, field: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} deve ser SHA-256 hexadecimal lowercase")
    return value


def stable_json(value: Any) -> str:
    """Serializa um contrato de forma determinística para hashing/auditoria."""

    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    """Nome compatível com o baseline para SHA-256 de JSON canônico."""

    return sha256_json(value)


def target_lock_resource(target_schema: str, target_table: str) -> str:
    """Return the stable application-lock name for one physical target.

    DDL provisioning, additive schema evolution and block imports must contend
    for the same resource.  The resource deliberately excludes execution and
    dataset identifiers because those logical owners still address the same
    SQL Server object.
    """

    validate_sql_identifier(target_schema, "target_schema")
    validate_sql_identifier(target_table, "target_table")
    physical_name = target_schema.casefold() + "\0" + target_table.casefold()
    suffix = hashlib.sha256(physical_name.encode("utf-8")).hexdigest()
    return "DATA_TRANSFER_TARGET:" + suffix


def file_hash(path: str | Path) -> str:
    value = Path(path)
    hasher = hashlib.sha256()
    with value.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def qi(value: str) -> str:
    """Delimita um identificador SQL Server após validação de comprimento/NUL."""

    validate_sql_identifier(value)
    return "[" + value.replace("]", "]]" ) + "]"


def qs(value: str) -> str:
    """Delimita um literal Unicode SQL; não usar para valores que aceitem parâmetros."""

    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Literal SQL deve ser texto sem NUL")
    return "N'" + value.replace("'", "''") + "'"


def collation_sql(value: str) -> str:
    """Valida uma collation no ponto da gramatica que nao aceita ``[]``."""

    validate_sql_identifier(value, "collation")
    if re.fullmatch(r"[A-Za-z0-9_]+", value) is None:
        raise ValueError("Collation contem caracteres nao permitidos")
    return value


def type_sql(column: Mapping[str, Any]) -> str:
    """Materializa a declaração de tipo nativo usada por catálogo/perfis V2.

    Mantém o contrato do motor V1: ``timestamp/rowversion`` é materializado como
    ``binary(8)`` e comprimentos de tipos Unicode chegam do catálogo em bytes.
    """

    kind = str(column["type_name"]).casefold()
    if kind in {"timestamp", "rowversion"}:
        return "binary(8)"
    if kind in {"char", "varchar", "binary", "varbinary", "nchar", "nvarchar"}:
        length = int(column["max_length"])
        width: str | int = "max" if length == -1 else length // 2 if kind.startswith("n") else length
        if width != "max" and width <= 0:
            raise ValueError(f"Comprimento inválido para {kind}: {length}")
        return f"{kind}({width})"
    if kind in {"decimal", "numeric"}:
        precision, scale = int(column["precision"]), int(column["scale"])
        if not 1 <= precision <= 38 or not 0 <= scale <= precision:
            raise ValueError(f"Precisão/escala inválida para {kind}: ({precision},{scale})")
        return f"{kind}({precision},{scale})"
    if kind in {"datetime2", "datetimeoffset", "time"}:
        scale = int(column["scale"])
        if not 0 <= scale <= 7:
            raise ValueError(f"Escala inválida para {kind}: {scale}")
        return f"{kind}({scale})"
    if kind == "float":
        precision = int(column["precision"])
        if not 1 <= precision <= 53:
            raise ValueError(f"Precisão inválida para float: {precision}")
        return f"float({precision})"
    if kind not in SCALAR_SQL_TYPES:
        raise ValueError(f"Tipo não suportado: {kind}")
    return kind


def deep_copy_json(value: Any) -> Any:
    """Cópia profunda restrita aos tipos que um JSON de configuração aceita."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def validate_sql_identifier(value: Any, field: str = "identificador") -> str:
    """Valida o limite do SQL Server; quoting continua responsabilidade do SQL layer."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-16-le")) > 256
    ):
        raise ValueError(
            f"{field} deve ser um identificador SQL não vazio de até "
            "128 unidades UTF-16"
        )
    return value


def normalize_sql_identifier_lower(value: Any, field: str = "identificador") -> str:
    if not isinstance(value, str):
        return validate_sql_identifier(value, field)
    normalized = value.lower()
    return validate_sql_identifier(normalized, field)


def redact_structure(value: Any) -> Any:
    """Produz uma cópia publicável de uma estrutura potencialmente sensível.

    Descritores de segredo no formato ``{provedor, referencia}`` são metadados e
    permanecem visíveis. Um valor escalar sob uma chave sensível é sempre ocultado.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            text_key = str(key)
            if _SENSITIVE_KEY.search(text_key):
                if isinstance(child, Mapping) and set(child).issubset({"provider", "reference"}):
                    result[text_key] = redact_structure(child)
                else:
                    result[text_key] = REDACTED
            else:
                result[text_key] = redact_structure(child)
        return result
    if isinstance(value, tuple):
        return tuple(redact_structure(item) for item in value)
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value


class SecretRedactor:
    """Registro central de valores secretos e redator de mensagens.

    O registro é intencionalmente por processo. Ele reduz exposição acidental em
    logs/exceções, mas não promete apagar cópias da memória do interpretador.
    """

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, secret: str | bytes | None) -> None:
        if secret is None:
            return
        if isinstance(secret, bytes):
            for encoding in ("utf-8", "utf-16-le"):
                try:
                    decoded = secret.decode(encoding)
                except UnicodeDecodeError:
                    continue
                if decoded:
                    self._secrets.add(decoded)
        else:
            text = str(secret)
            if text:
                self._secrets.add(text)

    def redact(self, value: Any) -> str:
        text = str(value)
        # Substituir primeiro os valores longos evita deixar sufixos de segredos
        # quando um valor é prefixo de outro.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        for pattern in _ASSIGNMENT_PATTERNS:
            text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
        return text

    def exception_text(self, error: BaseException, include_chain: bool = True) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            parts.append(f"{type(current).__name__}: {self.redact(current)}")
            if not include_chain:
                break
            current = current.__cause__ or current.__context__
        return " <- ".join(parts)


DEFAULT_REDACTOR = SecretRedactor()


def redact_text(value: Any) -> str:
    return DEFAULT_REDACTOR.redact(value)


def redacted_exception(error: BaseException) -> str:
    return DEFAULT_REDACTOR.exception_text(error)


class RedactingFormatter(logging.Formatter):
    """Formatter que aplica o redator também a mensagens e tracebacks."""

    def __init__(self, *args: Any, redactor: SecretRedactor | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.redactor = redactor or DEFAULT_REDACTOR

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))

    def formatException(self, ei: tuple[type[BaseException], BaseException, Any]) -> str:  # noqa: N802
        return self.redactor.redact(super().formatException(ei))


def ensure_no_secret_arguments(arguments: Sequence[str], redactor: SecretRedactor | None = None) -> None:
    """Defesa adicional para subprocessos: recusa switches de senha conhecidos."""

    del redactor  # O valor não deve sequer chegar aos argumentos.
    for index, argument in enumerate(arguments):
        lowered = argument.casefold()
        if lowered in {"-p", "--password", "--senha"} or lowered.startswith(("-p=", "--password=", "--senha=")):
            raise ValueError(f"Argumento de segredo proibido na posição {index}: {argument}")
        if lowered.startswith(("pwd=", "password=", "senha=")):
            raise ValueError(f"Segredo embutido em argumento proibido na posição {index}")
