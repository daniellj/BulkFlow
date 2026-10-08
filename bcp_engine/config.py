"""Leitura, normalização e validação nativa do contrato de configuração V2.

O módulo não depende de ``jsonschema`` em runtime. O schema publicado em
``schemas/config-v2.schema.json`` serve a editores, pipelines e documentação;
as mesmas invariantes críticas são verificadas aqui.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .util import (
    deep_copy_json,
    normalize_sql_identifier_lower,
    sha256_json,
    validate_sql_identifier,
)


CONFIG_VERSION = 2
DEFAULT_CDC_RETENTION_MINUTES = 262_800
MAX_CDC_RETENTION_MINUTES = 52_494_800
DESTINATION_KEYS = {"bronze": "bronze_destination", "landing": "landing_destination"}
DEFAULT_PROFILES = {"bronze": "templates/bronze.json", "landing": "templates/landing.json"}
PERIMETER_DEFAULT_USERNAMES = {
    "DESENVOLVIMENTO": "u684",
    "HOMOLOGAÇÃO": "h684",
    "PRODUÇÃO": "s684",
}

ROOT_KEYS = {
    "config_version", "scope", "perimeter", "source", "bronze_destination", "landing_destination",
    "active_destination", "execute_import", "create_structure_if_needed",
    "executor_directory", "destination_sql_directory", "local_control_directory",
    "rows_per_block", "max_file_bytes", "minimum_free_space_bytes",
    "keyless_direct_load_max_rows", "allow_schema_evolution",
    "delete_confirmed_files", "continue_after_table_error", "estimates",
    "batching", "structure", "bronze_event_id", "tables", "odbc_driver",
    "bcp_executable", "connection_timeout_seconds", "sql_timeout_seconds",
    "bcp_timeout_seconds", "cdc_retention_minutes", "control_schema", "tls",
    "artifact_reader_sids", "artifact_writer_sids",
}
ENDPOINT_KEYS = {
    "instance", "port", "database", "read_database", "schema", "structure_profile",
    "authentication", "odbc_driver", "odbc_dsn", "connection_timeout_seconds",
    "sql_timeout_seconds", "tls",
}
AUTH_KEYS = {"type", "username", "domain", "password"}
SECRET_KEYS = {"provider", "reference"}
TABLE_KEYS = {
    "source_table", "destination_table", "source_database", "destination_database",
    "source_schema", "destination_schema", "watermark",
    "rows_per_block", "structure_profile", "destination_area", "metadata_mapping",
    "enable_cdc", "partition_column",
}

DEFAULTS: dict[str, Any] = {
    # Infrastructure-neutral contract default. The exact uppercase enum only
    # supplies a login when a SQL endpoint omits it.
    "perimeter": "DESENVOLVIMENTO",
    "active_destination": "bronze",
    "execute_import": True,
    "create_structure_if_needed": True,
    "rows_per_block": 200_000,
    # Tabelas sem PK, UNIQUE elegivel ou marca d'agua comprovada nao podem ser
    # paginadas com seguranca. Ate este limite elas podem seguir em um unico
    # bloco direto. A pre-admissao usa metadados (sem COUNT_BIG) e a contagem
    # real produzida pelo BCP e validada antes da importacao. Zero desativa a
    # excecao sem alterar o restante do contrato V2.
    "keyless_direct_load_max_rows": 5_000_000,
    # Operational authorization only.  A source-layout change still changes
    # the structural fingerprint independently of this switch.
    "allow_schema_evolution": False,
    # Optional Windows principals that need read/traverse access to the BCP
    # files (for example a local SQL Server service SID), plus exceptional
    # writer identities for SMB where the local token SID differs from the
    # effective remote identity. Broad built-in groups are always rejected;
    # the executor, SYSTEM and Administrators are implicit writers.
    "artifact_reader_sids": [],
    "artifact_writer_sids": [],
    "max_file_bytes": 157_286_400,
    "minimum_free_space_bytes": 10_737_418_240,
    "delete_confirmed_files": True,
    "continue_after_table_error": True,
    "odbc_driver": "ODBC Driver 18 for SQL Server",
    "bcp_executable": "bcp",
    "connection_timeout_seconds": 30,
    "sql_timeout_seconds": 0,
    "bcp_timeout_seconds": 0,
    # SQL Server CDC cleanup-job retention. The requested operational default
    # is 262,800 minutes (about 182.5 days, treated here as six months).
    "cdc_retention_minutes": DEFAULT_CDC_RETENTION_MINUTES,
    "control_schema": "dbo",
    "tls": {
        "encrypt": True,
        "trust_server_certificate": False,
        "bcp_switch": "-Ym",
    },
    "estimates": {
        "row_count_method": "metadata",
        "maximum_sample_rows": 10_000,
        "safety_factor": 1.25,
        "on_unavailable": "stop",
    },
    "batching": {
        "null_policy": "reject_table",
        "tie_policy": "complete_group",
        "upper_bound_policy": "capture_at_table_start",
        "require_watermark_index": False,
    },
    "structure": {"secondary_indexes_phase": "before_load"},
    "bronze_event_id": {
        "strategy": "sequence",
        "sequence_name": "seq_{destination_table}",
        "source_column": None,
    },
}


class ConfigError(ValueError):
    """Configuração V2 inválida, antes de qualquer conexão ou mutação."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field} deve ser um objeto JSON")
    return dict(value)


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{field} contém parâmetros desconhecidos: {', '.join(unknown)}")


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{field} deve ser booleano")
    return value


def _integer(value: Any, field: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        suffix = f" e no máximo {maximum}" if maximum is not None else ""
        raise ConfigError(f"{field} deve ser inteiro de no mínimo {minimum}{suffix}")
    return value


def _number(value: Any, field: str, minimum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise ConfigError(f"{field} deve ser numérico e no mínimo {minimum}")
    return float(value)


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{field} deve ser texto não vazio")
    return value


def _artifact_sids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} deve ser uma lista JSON")
    prohibited = {
        "S-1-1-0",       # Everyone
        "S-1-5-7",       # Anonymous
        "S-1-5-11",      # Authenticated Users
        "S-1-5-32-545",  # BUILTIN\\Users
    }
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not re.fullmatch(
            r"S-1-[0-9]+(?:-[0-9]+)+", item, flags=re.IGNORECASE
        ):
            raise ConfigError(
                f"{field}[{index}] deve conter um SID Windows válido"
            )
        normalized = item.upper()
        if normalized in prohibited:
            raise ConfigError(
                f"{field}[{index}] não pode conceder acesso a um grupo amplo"
            )
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _identifier(value: Any, field: str) -> str:
    try:
        return validate_sql_identifier(value, field)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def _lower_identifier(value: Any, field: str) -> str:
    try:
        return normalize_sql_identifier_lower(value, field)
    except ValueError as error:
        raise ConfigError(str(error)) from error


def _executor_path(value: Any, field: str, *, allow_unc: bool = True) -> str:
    """Aceita caminhos absolutos nativos de executores Windows ou POSIX.

    A validação é deliberadamente independente do host para permitir validar e
    preparar configurações Linux em uma estação Windows (e vice-versa). Os
    adaptadores de artefatos/SQLite ainda confirmam a semântica do host no uso.
    """

    text = _nonempty(value, field)
    windows_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    if not windows_path.is_absolute() and not posix_path.is_absolute():
        raise ConfigError(f"{field} deve ser caminho absoluto Windows, UNC ou POSIX")
    if not allow_unc and (
        text.startswith("\\\\") or windows_path.drive.startswith("\\\\")
    ):
        raise ConfigError(f"{field} deve ficar em disco local; SQLite/WAL em UNC não é suportado")
    return text


def _sql_visible_path(value: Any, field: str) -> str:
    """Aceita a visão absoluta do próprio SQL Server (Windows ou Linux).

    O executor e o SQL Server podem usar sintaxes distintas quando enxergam os
    mesmos bytes por compartilhamento ou bind mount.
    """

    text = _nonempty(value, field)
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        return text
    raise ConfigError(f"{field} deve ser caminho absoluto Windows, UNC ou POSIX visível pelo SQL Server")


def _merge(default: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
    result = deep_copy_json(default)
    for key, value in supplied.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deep_copy_json(value)
    return result


def validate_secret_spec(value: Any, field: str) -> dict[str, Any]:
    spec = _mapping(value, field)
    _unknown_keys(spec, SECRET_KEYS, field)
    provider = spec.get("provider")
    if provider not in {"prompt", "env", "windows_credential_manager"}:
        raise ConfigError(
            f"{field}.provider deve ser prompt, env ou windows_credential_manager"
        )
    reference = spec.get("reference")
    if provider == "prompt":
        if reference is not None:
            raise ConfigError(f"{field}.reference deve ser null para provider=prompt")
        reference = None
    else:
        reference = _nonempty(reference, f"{field}.reference")
    return {"provider": provider, "reference": reference}


def validate_auth(
    value: Any,
    field: str,
    *,
    default_sql_username: str | None = None,
) -> dict[str, Any]:
    auth = _mapping(value, field)
    _unknown_keys(auth, AUTH_KEYS, field)
    kind = auth.get("type")
    if kind not in {"windows_integrated", "windows_credentials", "sql"}:
        raise ConfigError(
            f"{field}.type deve ser windows_integrated, windows_credentials ou sql"
        )
    if kind == "windows_integrated":
        conflicts = sorted(set(auth) & {"username", "domain", "password"})
        if conflicts:
            raise ConfigError(
                f"{field}: windows_integrated não aceita {', '.join(conflicts)}"
            )
        return {"type": kind}
    username = auth.get("username")
    if kind == "sql" and (username is None or not str(username).strip()):
        username = default_sql_username
    result: dict[str, Any] = {
        "type": kind,
        "username": _nonempty(username, f"{field}.username"),
        "password": validate_secret_spec(auth.get("password"), f"{field}.password"),
    }
    if kind == "windows_credentials":
        result["domain"] = _nonempty(auth.get("domain"), f"{field}.domain")
    elif "domain" in auth:
        raise ConfigError(f"{field}: autenticação sql não aceita domain")
    return result


def _validate_tls(value: Any, field: str) -> dict[str, Any]:
    tls = _mapping(value, field)
    allowed = {"encrypt", "trust_server_certificate", "hostname_in_certificate", "bcp_switch"}
    _unknown_keys(tls, allowed, field)
    result: dict[str, Any] = {}
    if "encrypt" in tls:
        result["encrypt"] = _bool(tls["encrypt"], f"{field}.encrypt")
    if "trust_server_certificate" in tls:
        result["trust_server_certificate"] = _bool(
            tls["trust_server_certificate"], f"{field}.trust_server_certificate"
        )
    if "hostname_in_certificate" in tls:
        result["hostname_in_certificate"] = _nonempty(
            tls["hostname_in_certificate"], f"{field}.hostname_in_certificate"
        )
    if "bcp_switch" in tls:
        switch = tls["bcp_switch"]
        if switch not in {"-Ys", "-Ym", "-Yo"}:
            raise ConfigError(f"{field}.bcp_switch deve ser -Ys, -Ym ou -Yo")
        result["bcp_switch"] = switch
    return result


def _validate_endpoint(
    value: Any,
    field: str,
    *,
    source: bool,
    default_sql_username: str,
) -> dict[str, Any]:
    endpoint = _mapping(value, field)
    _unknown_keys(endpoint, ENDPOINT_KEYS, field)
    schema = (
        _identifier(endpoint.get("schema"), f"{field}.schema")
        if source
        else _lower_identifier(endpoint.get("schema"), f"{field}.schema")
    )
    result: dict[str, Any] = {
        "instance": _nonempty(endpoint.get("instance"), f"{field}.instance"),
        # ``port`` is explicit in all newly generated configurations.  The
        # fallback preserves older V2 files and normalizes them immediately.
        "port": _integer(endpoint.get("port", 1433), f"{field}.port", 1, 65_535),
        "database": _identifier(endpoint.get("database"), f"{field}.database"),
        "schema": schema,
        "authentication": validate_auth(
            endpoint.get("authentication"),
            f"{field}.authentication",
            default_sql_username=default_sql_username,
        ),
    }
    if source:
        result["read_database"] = _identifier(
            endpoint.get("read_database", result["database"]), f"{field}.read_database"
        )
        if "structure_profile" in endpoint:
            raise ConfigError(f"{field}: origem não aceita structure_profile")
    else:
        if "read_database" in endpoint:
            raise ConfigError(f"{field}: destino não aceita read_database")
        result["structure_profile"] = _nonempty(
            endpoint.get("structure_profile"), f"{field}.structure_profile"
        )
    if "odbc_driver" in endpoint:
        result["odbc_driver"] = _nonempty(endpoint["odbc_driver"], f"{field}.odbc_driver")
    if "odbc_dsn" in endpoint:
        dsn = _nonempty(endpoint["odbc_dsn"], f"{field}.odbc_dsn")
        if len(dsn) > 32 or any(character in dsn for character in ";{}"):
            raise ConfigError(
                f"{field}.odbc_dsn deve ter no máximo 32 caracteres e não conter ; ou chaves"
            )
        result["odbc_dsn"] = dsn
    if "connection_timeout_seconds" in endpoint:
        result["connection_timeout_seconds"] = _integer(
            endpoint["connection_timeout_seconds"], f"{field}.connection_timeout_seconds"
        )
    if "sql_timeout_seconds" in endpoint:
        result["sql_timeout_seconds"] = _integer(
            endpoint["sql_timeout_seconds"], f"{field}.sql_timeout_seconds"
        )
    if "tls" in endpoint:
        result["tls"] = _validate_tls(endpoint["tls"], f"{field}.tls")
    return result


def _validate_watermark(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    mark = _mapping(value, field)
    _unknown_keys(mark, {"columns"}, field)
    columns = mark.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ConfigError(f"{field}.columns deve ser uma lista não vazia")
    result = []
    seen: set[str] = set()
    for index, raw in enumerate(columns):
        item_field = f"{field}.columns[{index}]"
        item = _mapping(raw, item_field)
        _unknown_keys(item, {"name", "direction"}, item_field)
        name = _identifier(item.get("name"), f"{item_field}.name")
        order = item.get("direction", "ASC")
        if order != "ASC":
            raise ConfigError(f"{item_field}.direction deve ser ASC")
        folded = name.casefold()
        if folded in seen:
            raise ConfigError(f"{field} repete a coluna {name}")
        seen.add(folded)
        result.append({"name": name, "direction": "ASC"})
    return {"columns": result}


def _validate_metadata_mapping(value: Any, field: str) -> dict[str, Any]:
    mapping = _mapping(value, field)
    allowed_names = {"aud_ccid", "aud_cntrrn", "aud_enttyp"}
    unknown = sorted(set(mapping) - allowed_names)
    if unknown:
        raise ConfigError(f"{field} contém campos não suportados: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    expected_constant_types = {
        "aud_ccid": "BIGINT",
        "aud_cntrrn": "INT",
        "aud_enttyp": "VARCHAR(30)",
    }
    for name, raw in mapping.items():
        item_field = f"{field}.{name}"
        item = _mapping(raw, item_field)
        _unknown_keys(item, {"source_column", "constant", "sql_type"}, item_field)
        has_source = "source_column" in item
        has_constant = "constant" in item
        if has_source == has_constant:
            raise ConfigError(f"{item_field} deve informar exatamente source_column ou constant")
        if has_source:
            if set(item) != {"source_column"}:
                raise ConfigError(
                    f"{item_field} com source_column não aceita outros campos"
                )
            result[name] = {
                "source_column": _identifier(
                    item["source_column"], f"{item_field}.source_column"
                )
            }
        else:
            constant = item["constant"]
            if isinstance(constant, (Mapping, list)):
                raise ConfigError(
                    f"{item_field}.constant deve ser um escalar JSON, nunca SQL executável"
                )
            normalized = {"constant": deep_copy_json(constant)}
            if "sql_type" in item:
                configured_type = _nonempty(
                    item["sql_type"], f"{item_field}.sql_type"
                ).upper().replace(" ", "")
                expected_type = expected_constant_types[name]
                if configured_type != expected_type:
                    raise ConfigError(
                        f"{item_field}.sql_type deve ser exatamente {expected_type}"
                    )
                normalized["sql_type"] = expected_type
            result[name] = normalized
    return result


def _validate_table(value: Any, index: int, cfg: Mapping[str, Any]) -> dict[str, Any]:
    field = f"tables[{index}]"
    item = _mapping(value, field)
    _unknown_keys(item, TABLE_KEYS, field)
    source_name = _identifier(item.get("source_table"), f"{field}.source_table")
    source_database = _identifier(
        item.get("source_database", cfg["source"]["database"]),
        f"{field}.source_database",
    )
    if source_database.casefold() != str(cfg["source"]["database"]).casefold():
        raise ConfigError(
            f"{field}.source_database deve corresponder a source.database; "
            "o banco por tabela ainda nao define uma conexao independente"
        )

    destination_endpoint = cfg.get("bronze_destination")
    destination_database = item.get("destination_database")
    if destination_database is not None:
        destination_database = _identifier(
            destination_database, f"{field}.destination_database"
        )
        if destination_endpoint is None:
            raise ConfigError(
                f"{field}.destination_database exige bronze_destination configurado"
            )
        if destination_database.casefold() != str(
            destination_endpoint["database"]
        ).casefold():
            raise ConfigError(
                f"{field}.destination_database deve corresponder a "
                "bronze_destination.database; o banco por tabela ainda nao "
                "define uma conexao independente"
            )

    default_target = f"{source_database}_{source_name}".lower()
    target_name = _lower_identifier(
        item.get("destination_table", default_target), f"{field}.destination_table"
    )
    result: dict[str, Any] = {
        "source_table": source_name,
        "destination_table": target_name,
        "enable_cdc": _bool(item.get("enable_cdc", False), f"{field}.enable_cdc"),
    }
    if "source_database" in item:
        result["source_database"] = source_database
    if destination_database is not None:
        result["destination_database"] = destination_database
    if "source_schema" in item:
        result["source_schema"] = _identifier(
            item["source_schema"], f"{field}.source_schema"
        )
    if "destination_schema" in item:
        result["destination_schema"] = _lower_identifier(
            item["destination_schema"], f"{field}.destination_schema"
        )
    if "partition_column" in item:
        result["partition_column"] = _lower_identifier(
            item["partition_column"], f"{field}.partition_column"
        )
    result["watermark"] = _validate_watermark(
        item.get("watermark"), f"{field}.watermark"
    )
    if "rows_per_block" in item:
        result["rows_per_block"] = _integer(
            item["rows_per_block"], f"{field}.rows_per_block", 1, 5_000_000
        )
    if "structure_profile" in item:
        result["structure_profile"] = _nonempty(
            item["structure_profile"], f"{field}.structure_profile"
        )
    area = item.get("destination_area", cfg["active_destination"])
    if area not in DESTINATION_KEYS:
        raise ConfigError(f"{field}.destination_area deve ser bronze")
    if area != "bronze":
        raise ConfigError(
            f"{field}.destination_area deve ser bronze; Landing é somente estrutura"
        )
    if "destination_area" in item:
        result["destination_area"] = area
    if "metadata_mapping" in item:
        if area != "landing":
            raise ConfigError(f"{field}.metadata_mapping só se aplica ao perfil Landing")
        result["metadata_mapping"] = _validate_metadata_mapping(
            item["metadata_mapping"], f"{field}.metadata_mapping"
        )
    if area == "landing":
        missing = {"aud_ccid", "aud_cntrrn", "aud_enttyp"} - set(
            result.get("metadata_mapping", {})
        )
        if missing:
            raise ConfigError(
                f"{field}: carga Landing exige mapeamento explícito de {', '.join(sorted(missing))}"
            )
    return result


def _validate_sections(cfg: dict[str, Any]) -> None:
    estimates = _mapping(cfg["estimates"], "estimates")
    _unknown_keys(
        estimates,
        {"row_count_method", "maximum_sample_rows", "safety_factor", "on_unavailable"},
        "estimates",
    )
    if estimates.get("row_count_method") != "metadata":
        raise ConfigError("estimates.row_count_method deve ser metadata")
    estimates["maximum_sample_rows"] = _integer(
        estimates.get("maximum_sample_rows"),
        "estimates.maximum_sample_rows",
        1,
        1_000_000,
    )
    estimates["safety_factor"] = _number(
        estimates.get("safety_factor"), "estimates.safety_factor", 1.0
    )
    if estimates.get("on_unavailable") not in {"stop", "warn"}:
        raise ConfigError("estimates.on_unavailable deve ser stop ou warn")
    cfg["estimates"] = estimates

    batching = _mapping(cfg["batching"], "batching")
    _unknown_keys(
        batching,
        {"null_policy", "tie_policy", "upper_bound_policy", "require_watermark_index"},
        "batching",
    )
    if batching.get("null_policy") != "reject_table":
        raise ConfigError("batching.null_policy suportada nesta versão: reject_table")
    if batching.get("tie_policy") != "complete_group":
        raise ConfigError("batching.tie_policy suportada nesta versão: complete_group")
    if batching.get("upper_bound_policy") != "capture_at_table_start":
        raise ConfigError(
            "batching.upper_bound_policy suportada nesta versão: capture_at_table_start"
        )
    batching["require_watermark_index"] = _bool(
        batching.get("require_watermark_index"), "batching.require_watermark_index"
    )
    cfg["batching"] = batching

    structure = _mapping(cfg["structure"], "structure")
    _unknown_keys(structure, {"secondary_indexes_phase"}, "structure")
    if structure.get("secondary_indexes_phase") not in {"before_load", "after_table_load"}:
        raise ConfigError(
            "structure.secondary_indexes_phase deve ser before_load ou after_table_load"
        )
    cfg["structure"] = structure

    event_id = _mapping(cfg["bronze_event_id"], "bronze_event_id")
    _unknown_keys(event_id, {"strategy", "sequence_name", "source_column"}, "bronze_event_id")
    strategy = event_id.get("strategy")
    if strategy == "sequence":
        name = _nonempty(event_id.get("sequence_name"), "bronze_event_id.sequence_name")
        if "{destination_table}" not in name:
            raise ConfigError(
                "bronze_event_id.sequence_name deve conter {destination_table} para evitar colisões"
            )
        if event_id.get("source_column") not in (None, ""):
            raise ConfigError("bronze_event_id.source_column conflita com strategy=sequence")
        cfg["bronze_event_id"] = {
            "strategy": strategy, "sequence_name": name, "source_column": None
        }
    elif strategy == "source_column":
        column = _identifier(event_id.get("source_column"), "bronze_event_id.source_column")
        if event_id.get("sequence_name") not in (None, ""):
            raise ConfigError(
                "bronze_event_id.sequence_name conflita com strategy=source_column"
            )
        cfg["bronze_event_id"] = {
            "strategy": strategy, "sequence_name": None, "source_column": column
        }
    else:
        raise ConfigError("bronze_event_id.strategy deve ser sequence ou source_column")


def validate_config(value: Any) -> dict[str, Any]:
    """Valida e retorna uma cópia normalizada, nunca o objeto recebido."""

    supplied = _mapping(value, "configuração")
    _unknown_keys(supplied, ROOT_KEYS, "configuração")
    if supplied.get("config_version") != CONFIG_VERSION:
        raise ConfigError(f"config_version deve ser {CONFIG_VERSION}")
    cfg = _merge(DEFAULTS, supplied)
    supplied_event = supplied.get("bronze_event_id")
    if isinstance(supplied_event, Mapping):
        if (
            supplied_event.get("strategy") == "source_column"
            and "sequence_name" not in supplied_event
        ):
            cfg["bronze_event_id"]["sequence_name"] = None
        if supplied_event.get("strategy") == "sequence" and "source_column" not in supplied_event:
            cfg["bronze_event_id"]["source_column"] = None
    cfg["config_version"] = CONFIG_VERSION
    if "scope" in cfg:
        cfg["scope"] = _nonempty(cfg["scope"], "scope")
    cfg["perimeter"] = _nonempty(cfg.get("perimeter"), "perimeter")
    if cfg["perimeter"] not in PERIMETER_DEFAULT_USERNAMES:
        raise ConfigError(
            "perimeter deve ser exatamente DESENVOLVIMENTO, HOMOLOGAÇÃO ou PRODUÇÃO"
        )
    cfg["active_destination"] = cfg.get("active_destination")
    if cfg["active_destination"] != "bronze":
        raise ConfigError(
            "active_destination deve ser bronze; Landing é somente estrutura"
        )
    for key in (
        "execute_import", "create_structure_if_needed",
        "delete_confirmed_files", "continue_after_table_error",
        "allow_schema_evolution",
    ):
        cfg[key] = _bool(cfg[key], key)
    cfg["rows_per_block"] = _integer(
        cfg["rows_per_block"], "rows_per_block", 1, 5_000_000
    )
    cfg["keyless_direct_load_max_rows"] = _integer(
        cfg["keyless_direct_load_max_rows"],
        "keyless_direct_load_max_rows",
        0,
        9_223_372_036_854_775_807,
    )
    cfg["max_file_bytes"] = _integer(
        cfg["max_file_bytes"], "max_file_bytes", 1
    )
    cfg["minimum_free_space_bytes"] = _integer(
        cfg["minimum_free_space_bytes"], "minimum_free_space_bytes", 0
    )
    cfg["connection_timeout_seconds"] = _integer(
        cfg["connection_timeout_seconds"], "connection_timeout_seconds", 0
    )
    cfg["sql_timeout_seconds"] = _integer(
        cfg["sql_timeout_seconds"], "sql_timeout_seconds", 0
    )
    cfg["bcp_timeout_seconds"] = _integer(
        cfg["bcp_timeout_seconds"], "bcp_timeout_seconds", 0
    )
    cfg["cdc_retention_minutes"] = _integer(
        cfg["cdc_retention_minutes"],
        "cdc_retention_minutes",
        1,
        MAX_CDC_RETENTION_MINUTES,
    )
    cfg["odbc_driver"] = _nonempty(cfg["odbc_driver"], "odbc_driver")
    cfg["bcp_executable"] = _nonempty(cfg["bcp_executable"], "bcp_executable")
    cfg["artifact_reader_sids"] = _artifact_sids(
        cfg["artifact_reader_sids"], "artifact_reader_sids"
    )
    cfg["artifact_writer_sids"] = _artifact_sids(
        cfg["artifact_writer_sids"], "artifact_writer_sids"
    )
    cfg["control_schema"] = _lower_identifier(
        cfg["control_schema"], "control_schema"
    )
    if cfg["control_schema"] != "dbo":
        raise ConfigError(
            "control_schema deve ser dbo; o controle SQL persistente possui "
            "contrato fixo no schema dbo do destino Bronze"
        )
    cfg["tls"] = _merge(DEFAULTS["tls"], _validate_tls(cfg["tls"], "tls"))
    default_username = PERIMETER_DEFAULT_USERNAMES[cfg["perimeter"]]
    cfg["source"] = _validate_endpoint(
        cfg.get("source"),
        "source",
        source=True,
        default_sql_username=default_username,
    )

    for area, key in DESTINATION_KEYS.items():
        if key in supplied:
            cfg[key] = _validate_endpoint(
                cfg[key],
                key,
                source=False,
                default_sql_username=default_username,
            )
        else:
            cfg.pop(key, None)
    active_key = DESTINATION_KEYS[cfg["active_destination"]]
    if cfg["execute_import"] and active_key not in cfg:
        raise ConfigError(
            f"{active_key} é obrigatório quando execute_import=true"
        )

    cfg["executor_directory"] = _executor_path(
        cfg.get("executor_directory"), "executor_directory"
    )
    cfg["local_control_directory"] = _executor_path(
        cfg.get("local_control_directory"), "local_control_directory", allow_unc=False
    )
    if "destination_sql_directory" in supplied:
        cfg["destination_sql_directory"] = _sql_visible_path(
            cfg["destination_sql_directory"], "destination_sql_directory"
        )
    else:
        cfg.pop("destination_sql_directory", None)
    if cfg["execute_import"] and "destination_sql_directory" not in cfg:
        raise ConfigError(
            "destination_sql_directory é obrigatório quando execute_import=true"
        )
    _validate_sections(cfg)
    raw_tables = cfg.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise ConfigError("tables deve ser uma lista não vazia")
    cfg["tables"] = [_validate_table(item, index, cfg) for index, item in enumerate(raw_tables)]
    if cfg["bronze_event_id"]["strategy"] == "sequence":
        pattern = cfg["bronze_event_id"]["sequence_name"]
        for index, item in enumerate(cfg["tables"]):
            _identifier(
                pattern.replace("{destination_table}", item["destination_table"]),
                f"tables[{index}].expanded_sequence",
            )
    _validate_collisions(cfg)
    return cfg


def read_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Não foi possível ler {config_path}: {error}") from error
    return validate_config(value)


def active_destination(config: Mapping[str, Any], *, required: bool | None = None) -> dict[str, Any] | None:
    area = config["active_destination"]
    endpoint = config.get(DESTINATION_KEYS[area])
    must_exist = config.get("execute_import", True) if required is None else required
    if must_exist and endpoint is None:
        raise ConfigError(f"Destino {area} é obrigatório para esta operação")
    return deep_copy_json(endpoint) if endpoint is not None else None


def effective_delete_confirmed_files(config: Mapping[str, Any]) -> bool:
    """Exclusão só é efetiva depois de uma importação confirmada."""

    return bool(config["execute_import"] and config["delete_confirmed_files"])


def effective_tables(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expande defaults/overrides sem misturar origem e destino."""

    result: list[dict[str, Any]] = []
    for index, item in enumerate(config["tables"]):
        area = item.get("destination_area", config["active_destination"])
        endpoint = config.get(DESTINATION_KEYS[area])
        target_schema = item.get("destination_schema")
        if target_schema is None and endpoint is not None:
            target_schema = endpoint["schema"]
        profile = item.get("structure_profile")
        if profile is None and endpoint is not None:
            profile = endpoint["structure_profile"]
        if profile is None:
            profile = DEFAULT_PROFILES[area]
        result.append({
            "index": index,
            "source": {
                "instance": config["source"]["instance"],
                "port": config["source"].get("port", 1433),
                "database": item.get(
                    "source_database", config["source"]["database"]
                ),
                "read_database": config["source"].get(
                    "read_database", config["source"]["database"]
                ),
                "schema": item.get("source_schema", config["source"]["schema"]),
                "table": item["source_table"],
            },
            "destination": {
                "area": area,
                "instance": endpoint.get("instance") if endpoint else None,
                "port": endpoint.get("port") if endpoint else None,
                "database": item.get(
                    "destination_database",
                    endpoint.get("database") if endpoint else None,
                ),
                "schema": target_schema,
                "table": item["destination_table"],
                "structure_profile": profile,
            },
            "watermark": deep_copy_json(item.get("watermark")),
            "enable_cdc": item.get("enable_cdc", False),
            "rows_per_block": item.get("rows_per_block", config["rows_per_block"]),
            "metadata_mapping": deep_copy_json(item.get("metadata_mapping", {})),
            "partition_column": item.get("partition_column"),
        })
    return result


def _validate_collisions(config: Mapping[str, Any]) -> None:
    used: dict[tuple[str, str, str, str], int] = {}
    for table in effective_tables(config):
        target = table["destination"]
        key = (
            target["area"].casefold(),
            (target["database"] or "<sem-banco>").casefold(),
            (target["schema"] or "<sem-esquema>").casefold(),
            target["table"].casefold(),
        )
        if key in used:
            raise ConfigError(
                f"tables[{used[key]}] e tables[{table['index']}] gravariam no mesmo objeto "
                f"{target['area']}:{target['schema'] or '<sem-esquema>'}.{target['table']}"
            )
        used[key] = table["index"]


def _select_table(config: Mapping[str, Any], selector: Any) -> list[dict[str, Any]]:
    tables = effective_tables(config)
    if selector is None:
        return tables
    if type(selector) is int:
        try:
            return [tables[selector]]
        except IndexError as error:
            raise ConfigError(f"Índice de tabela inexistente: {selector}") from error
    matches = [
        table for table in tables
        if table["source"]["table"] == selector or table["destination"]["table"] == selector
    ]
    if len(matches) != 1:
        raise ConfigError(f"Seletor de tabela deve identificar exatamente uma tabela: {selector!r}")
    return matches


def structural_fingerprint(config: Mapping[str, Any], table: Any = None) -> str:
    """Hash do que define origem, intervalos, projeção e layout dos artefatos.

    Credenciais, caminhos, ativação da importação e o endereço físico do destino
    ficam deliberadamente fora; alterá-los não força reexportação por si só.
    """

    selected = _select_table(config, table)
    payload = {
        "config_version": CONFIG_VERSION,
        "logical_source": [
            {
                "instance": entry["source"]["instance"],
                "port": entry["source"]["port"],
                "database": entry["source"]["database"],
                "read_database": entry["source"]["read_database"],
                "schema": entry["source"]["schema"],
                "table": entry["source"]["table"],
                "watermark": entry["watermark"],
                "rows_per_block": entry["rows_per_block"],
                "layout_area": entry["destination"]["area"],
                "layout_table": entry["destination"]["table"],
                "structure_profile": entry["destination"]["structure_profile"],
                "metadata_mapping": entry["metadata_mapping"],
                "partition_column": entry.get("partition_column"),
            }
            for entry in selected
        ],
        "batching": config["batching"],
        "keyless_direct_load_max_rows": config["keyless_direct_load_max_rows"],
        "bronze_event_id": (
            config["bronze_event_id"]
            if any(entry["destination"]["area"] == "bronze" for entry in selected)
            else None
        ),
    }
    return sha256_json(payload)


def operational_fingerprint(config: Mapping[str, Any]) -> str:
    """Hash de parâmetros operacionais, sem qualquer segredo resolvido."""

    endpoint_view: dict[str, Any] = {}
    # Landing is an independent DDL/evolution endpoint.  It must not affect a
    # Bronze data execution or make a valid resume fail merely because its
    # address or credential reference changed.
    for key in ("source", "bronze_destination"):
        endpoint = config.get(key)
        if endpoint is None:
            continue
        endpoint_view[key] = {
            name: deep_copy_json(value)
            for name, value in endpoint.items()
            if name != "structure_profile"
        }
    payload = {
        "perimeter": config["perimeter"],
        "active_destination": config["active_destination"],
        "execute_import": config["execute_import"],
        "create_structure_if_needed": config["create_structure_if_needed"],
        "allow_schema_evolution": config.get("allow_schema_evolution", False),
        "executor_directory": config["executor_directory"],
        "destination_sql_directory": config.get("destination_sql_directory"),
        "local_control_directory": config["local_control_directory"],
        "control_schema": config["control_schema"],
        "max_file_bytes": config["max_file_bytes"],
        "minimum_free_space_bytes": config["minimum_free_space_bytes"],
        "delete_confirmed_files": config["delete_confirmed_files"],
        "continue_after_table_error": config["continue_after_table_error"],
        "estimates": config["estimates"],
        "structure": config["structure"],
        "odbc_driver": config["odbc_driver"],
        "bcp_executable": config["bcp_executable"],
        "connection_timeout_seconds": config["connection_timeout_seconds"],
        "sql_timeout_seconds": config["sql_timeout_seconds"],
        "bcp_timeout_seconds": config["bcp_timeout_seconds"],
        "cdc_retention_minutes": config["cdc_retention_minutes"],
        "tls": config["tls"],
        "endpoints": endpoint_view,
        "destination_routing": [
            {
                "area": item["destination"]["area"],
                "instance": item["destination"]["instance"],
                "port": item["destination"]["port"],
                "database": item["destination"]["database"],
                "schema": item["destination"]["schema"],
                "table": item["destination"]["table"],
            }
            for item in effective_tables(config)
        ],
        "cdc_activation": [
            {
                "source_database": item["source"]["database"],
                "source_schema": item["source"]["schema"],
                "source_table": item["source"]["table"],
                "enable_cdc": item["enable_cdc"],
            }
            for item in effective_tables(config)
        ],
    }
    # Preserve the fingerprint of executions created before this optional ACL
    # field existed. A non-empty allowlist is operationally significant.
    if config.get("artifact_reader_sids"):
        payload["artifact_reader_sids"] = config["artifact_reader_sids"]
    if config.get("artifact_writer_sids"):
        payload["artifact_writer_sids"] = config["artifact_writer_sids"]
    return sha256_json(payload)


def fingerprints(config: Mapping[str, Any], table: Any = None) -> dict[str, str]:
    return {
        "structural": structural_fingerprint(config, table),
        "operational": operational_fingerprint(config),
    }
