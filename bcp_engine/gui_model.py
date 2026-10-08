"""Pure helpers used by the ttkbootstrap adapter.

This module deliberately contains no Tk imports.  Configuration assembly and
validation can therefore be exercised in headless test environments while the
desktop adapter remains a thin client of the same V2 contract used by the CLI.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
from typing import Any, Mapping, Sequence

from .config import PERIMETER_DEFAULT_USERNAMES, validate_config
from .runtime import ensure_runtime_directories
from .watermark_validation import (
    normalize_watermark_columns,
    render_watermark_validation_sql,
)


AUTHENTICATION_LABELS: dict[str, str] = {
    "Windows integrada": "windows_integrated",
    "SQL Server": "sql",
    "Credencial Windows": "windows_credentials",
}
SECRET_PROVIDER_LABELS: dict[str, str] = {
    "Solicitar ao executar": "prompt",
    "Variável de ambiente": "env",
    "Gerenciador de Credenciais do Windows": "windows_credential_manager",
}
DESTINATION_LABELS: dict[str, str] = {"Bronze": "bronze"}
PERIMETER_VALUES: tuple[str, ...] = tuple(PERIMETER_DEFAULT_USERNAMES)
ROW_COUNT_LABELS: dict[str, str] = {
    "Metadados (aproximado)": "metadata",
}
INDEX_PHASE_LABELS: dict[str, str] = {
    "Após a carga da tabela": "after_table_load",
    "Antes da carga": "before_load",
}

ENDPOINT_DISPLAY_ORDER: tuple[tuple[str, str], ...] = (
    ("source", "ORIGEM"),
    ("landing_destination", "LANDING"),
    ("bronze_destination", "BRONZE"),
)

_LANDING_TYPES = {
    "aud_ccid": "BIGINT",
    "aud_cntrrn": "INT",
    "aud_enttyp": "VARCHAR(30)",
}


def default_username_for_perimeter(perimeter: str) -> str:
    """Return the editable SQL-login suggestion for an exact perimeter."""

    try:
        return PERIMETER_DEFAULT_USERNAMES[perimeter]
    except KeyError as error:
        raise ValueError("Perímetro inválido") from error


def update_default_username_for_perimeter(
    perimeter: str,
    current_username: str,
    previous_perimeter: str | None,
) -> str:
    """Apply a new suggestion without overwriting a custom endpoint login."""

    new_default = default_username_for_perimeter(perimeter)
    current = current_username.strip()
    previous_default = (
        PERIMETER_DEFAULT_USERNAMES.get(previous_perimeter)
        if previous_perimeter is not None
        else None
    )
    if not current or (previous_default is not None and current == previous_default):
        return new_default
    return current


def code_for_label(labels: Mapping[str, str], displayed: str, field_name: str) -> str:
    """Resolve a localized display label into its stable contract value."""

    try:
        return labels[displayed]
    except KeyError as error:
        raise ValueError(f"{field_name}: seleção inválida") from error


def label_for_code(labels: Mapping[str, str], code: str, fallback: str | None = None) -> str:
    """Resolve a stable contract value into a localized display label."""

    for label, candidate in labels.items():
        if candidate == code:
            return label
    if fallback is not None:
        return fallback
    raise ValueError(f"Valor de contrato não suportado pela interface: {code}")


def parse_required_integer(
    value: str,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Parse an integer without accepting signs, decimals, or locale ambiguity."""

    text = value.strip()
    if not text or not text.isdecimal():
        raise ValueError(f"{field_name} deve ser um número inteiro")
    result = int(text)
    if result < minimum or (maximum is not None and result > maximum):
        suffix = f" e no máximo {maximum}" if maximum is not None else ""
        raise ValueError(f"{field_name} deve ser no mínimo {minimum}{suffix}")
    return result


def parse_optional_integer(
    value: str,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int | None:
    text = value.strip()
    if not text:
        return None
    return parse_required_integer(
        text, field_name, minimum=minimum, maximum=maximum
    )


def parse_required_number(value: str, field_name: str, *, minimum: float) -> float:
    text = value.strip().replace(",", ".")
    try:
        result = float(text)
    except ValueError as error:
        raise ValueError(f"{field_name} deve ser numérico") from error
    if result < minimum:
        raise ValueError(f"{field_name} deve ser no mínimo {minimum}")
    return result


def format_bytes_summary(value: str | int) -> str:
    """Return a compact pt-BR MB/GB rendering using the configured binary sizes."""

    text = str(value).strip()
    if not text or not text.isdecimal():
        return "Valor inválido"
    number = int(text)
    megabytes = number / (1024**2)
    gigabytes = number / (1024**3)
    rendered = f"{megabytes:,.2f} MB | {gigabytes:,.2f} GB"
    return rendered.replace(",", "#").replace(".", ",").replace("#", ".")


def _projected_bytes(value: Any) -> int | None:
    """Normalize a planner byte value without treating unknown as zero."""

    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _free_bytes_for_path(
    value: str,
    disk_usage_provider: Any,
) -> tuple[int | None, str | None]:
    """Read free space from the executor, preserving inaccessible SQL paths.

    A destination path is the view used by SQL Server. It may be a path on a
    different operating system and therefore deliberately remains unavailable
    instead of being reinterpreted as a local path by :class:`pathlib.Path`.
    For a local path that is not created yet, the nearest existing parent is a
    valid representation of the volume on which it will be created.
    """

    text = str(value).strip()
    if not text:
        return None, "diretÃ³rio nÃ£o informado"
    if os.name == "nt" and PurePosixPath(text).is_absolute():
        return None, "caminho do SQL Server nÃ£o Ã© visÃ­vel neste executor Windows"
    if os.name != "nt" and (PureWindowsPath(text).drive or "\\" in text):
        return None, "caminho do SQL Server nÃ£o Ã© visÃ­vel neste executor Linux"

    candidate = Path(text).expanduser()
    attempted: set[str] = set()
    last_error = "espaÃ§o em disco indisponÃ­vel"
    while True:
        marker = os.path.normcase(os.path.abspath(str(candidate)))
        if marker not in attempted:
            attempted.add(marker)
            try:
                usage = disk_usage_provider(candidate)
                free = getattr(usage, "free", usage)
                return int(free), None
            except Exception as error:
                last_error = str(error) or type(error).__name__
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None, last_error


def build_disk_projection_summary(
    config: Mapping[str, Any],
    plans: Sequence[Mapping[str, Any]],
    *,
    disk_usage_provider: Any = shutil.disk_usage,
) -> dict[str, Any]:
    """Consolidate BCP projections and assess both configured path views.

    This function is intentionally headless. The GUI invokes it from its
    planning worker, never while rendering widgets. Export and import paths
    are two views of the same BCP artifacts, so their requirements are
    assessed independently and are never added together.
    """

    safety_factor = Decimal(str(config.get("estimates", {}).get("safety_factor", 1)))
    minimum_free_bytes = max(0, int(config.get("minimum_free_space_bytes", 0)))
    table_rows: list[dict[str, Any]] = []
    included_rows: list[dict[str, Any]] = []

    for position, plan in enumerate(plans, start=1):
        source = plan.get("source") if isinstance(plan.get("source"), Mapping) else {}
        qualified_source = ".".join(
            str(part)
            for part in (
                source.get("database"),
                source.get("schema"),
                source.get("table"),
            )
            if part
        ) or f"Tabela {position}"
        status = str(plan.get("status") or "PENDING")
        # Every configured table remains visible in the consolidated contract.
        # A failed/skipped planning row without an estimate makes the total
        # explicitly incomplete instead of silently reducing the requirement.
        included = True
        raw_bytes = _projected_bytes(plan.get("estimated_bcp_total_bytes"))
        protected_bytes = _projected_bytes(plan.get("planning_reserve_bytes"))
        if protected_bytes is None and raw_bytes is not None:
            protected_bytes = int(
                (Decimal(raw_bytes) * safety_factor).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        peak_bytes = _projected_bytes(plan.get("predicted_peak_bytes"))
        # The per-table cost is complete as soon as raw/protected bytes are
        # known.  Peak completeness is tracked separately: in retained-file
        # mode an earlier unknown table can make a later cumulative peak
        # unknown without erasing that later table's own size estimate.
        projection_state = (
            "available"
            if raw_bytes is not None and protected_bytes is not None
            else "unavailable"
        )
        peak_state = "available" if peak_bytes is not None else "unavailable"
        row = {
            "position": position,
            "source": qualified_source,
            "status": status,
            "included": included,
            "estimated_rows": _projected_bytes(plan.get("estimated_rows")),
            "raw_bytes": raw_bytes,
            "protected_bytes": protected_bytes,
            "margin_bytes": (
                None
                if raw_bytes is None or protected_bytes is None
                else max(0, protected_bytes - raw_bytes)
            ),
            "peak_bytes": peak_bytes,
            "projection_state": projection_state,
            "peak_state": peak_state,
        }
        table_rows.append(row)
        if included:
            included_rows.append(row)

    unknown_table_count = sum(
        row["projection_state"] == "unavailable" for row in included_rows
    )
    unknown_peak_table_count = sum(
        row["peak_state"] == "unavailable" for row in included_rows
    )
    known_raw_bytes = sum(
        int(row["raw_bytes"])
        for row in included_rows
        if row["raw_bytes"] is not None
    )
    known_protected_bytes = sum(
        int(row["protected_bytes"])
        for row in included_rows
        if row["protected_bytes"] is not None
    )
    totals_available = bool(included_rows) and unknown_table_count == 0
    total_raw_bytes = known_raw_bytes if totals_available else None
    total_protected_bytes = known_protected_bytes if totals_available else None
    predicted_peak_bytes = (
        max((int(row["peak_bytes"]) for row in included_rows), default=0)
        if included_rows and unknown_peak_table_count == 0
        else None
    )

    export_path = str(config.get("executor_directory", "")).strip()
    import_path = str(config.get("destination_sql_directory", "")).strip()
    export_free, export_error = _free_bytes_for_path(export_path, disk_usage_provider)
    normalized_export = os.path.normcase(os.path.normpath(export_path))
    normalized_import = os.path.normcase(os.path.normpath(import_path))
    same_path_view = bool(export_path and import_path and normalized_export == normalized_import)
    execute_import = bool(config.get("execute_import", True))
    if not execute_import:
        import_free, import_error = None, None
    elif same_path_view:
        import_free, import_error = export_free, export_error
    else:
        import_free, import_error = _free_bytes_for_path(
            import_path, disk_usage_provider
        )

    def assessment(
        role: str,
        path: str,
        free_bytes: int | None,
        error: str | None,
        *,
        applicable: bool = True,
    ) -> dict[str, Any]:
        if not applicable:
            state = "not_applicable"
            peak_balance = None
        elif free_bytes is None or predicted_peak_bytes is None:
            state = "unavailable"
            peak_balance = None
        else:
            peak_balance = free_bytes - minimum_free_bytes - predicted_peak_bytes
            state = "sufficient" if peak_balance >= 0 else "insufficient"
        total_balance = (
            None
            if not applicable or free_bytes is None or total_protected_bytes is None
            else free_bytes - minimum_free_bytes - total_protected_bytes
        )
        return {
            "role": role,
            "path": path,
            "free_bytes": free_bytes,
            "minimum_free_bytes": minimum_free_bytes,
            "required_peak_bytes": predicted_peak_bytes,
            "balance_after_peak_bytes": peak_balance,
            "balance_after_total_protected_bytes": total_balance,
            "total_protected_fits": (
                None if total_balance is None else total_balance >= 0
            ),
            "state": state,
            "error": error,
            "measurement_scope": "executor_filesystem",
        }

    return {
        "safety_factor": float(safety_factor),
        "minimum_free_bytes": minimum_free_bytes,
        "included_table_count": len(included_rows),
        "unknown_table_count": unknown_table_count,
        "unknown_peak_table_count": unknown_peak_table_count,
        "known_raw_bytes": known_raw_bytes,
        "known_protected_bytes": known_protected_bytes,
        "total_raw_bytes": total_raw_bytes,
        "total_protected_bytes": total_protected_bytes,
        "total_margin_bytes": (
            None
            if total_raw_bytes is None or total_protected_bytes is None
            else total_protected_bytes - total_raw_bytes
        ),
        "predicted_peak_bytes": predicted_peak_bytes,
        "same_path_view": same_path_view,
        "paths_are_artifact_views": True,
        "directories": {
            "export": assessment("export", export_path, export_free, export_error),
            "import": assessment(
                "import",
                import_path,
                import_free,
                import_error,
                applicable=execute_import,
            ),
        },
        "tables": table_rows,
    }


def split_instance_and_port(instance: str, port: Any = "") -> tuple[str, str]:
    """Project the endpoint contract into the two GUI fields.

    ``instance`` historically accepted ``host,port``.  Keeping this small
    compatibility adapter lets the editor open those files while new files use
    the explicit ``port`` contract field.
    """

    instance_text = str(instance).strip()
    port_text = str(port).strip()
    if port_text:
        return instance_text, port_text
    host, separator, legacy_port = instance_text.rpartition(",")
    if separator and legacy_port.strip().isdecimal():
        return host.strip(), legacy_port.strip()
    return instance_text, ""


def authentication_display_name(authentication: Mapping[str, Any]) -> str:
    """Return a safe configured principal for the operations summary."""

    authentication_type = str(authentication.get("type", ""))
    if authentication_type == "windows_integrated":
        return "Identidade Windows do processo"
    username = str(authentication.get("username", "")).strip()
    if authentication_type == "windows_credentials":
        domain = str(authentication.get("domain", "")).strip()
        return f"{domain}\\{username}" if domain else username
    return username


def connection_summary_rows(config: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Build presentation-only endpoint rows without secret material."""

    rows: list[tuple[str, ...]] = []
    for key, label in ENDPOINT_DISPLAY_ORDER:
        endpoint = config.get(key)
        if not isinstance(endpoint, Mapping):
            continue
        instance, legacy_port = split_instance_and_port(
            str(endpoint.get("instance", "")), endpoint.get("port", "")
        )
        rows.append(
            (
                label,
                authentication_display_name(endpoint.get("authentication", {})),
                instance,
                str(endpoint.get("port", legacy_port)),
                str(endpoint.get("database", "")),
            )
        )
    return rows


def parse_watermark(value: str) -> dict[str, Any] | None:
    """Parse ascending column names separated exclusively by commas.

    The desktop contract intentionally does not expose a direction selector:
    every explicit watermark is traversed in ascending order.  Keeping the
    input as plain column names also prevents SQL fragments from being pasted
    into a field that is later treated as catalog metadata.
    """

    text = value.strip()
    if not text:
        return None
    if "\r" in text or "\n" in text:
        raise ValueError(
            "Marca d'água inválida. Informe os nomes das colunas em uma única "
            "linha, separados por vírgula."
        )
    raw_items = text.split(",")
    for position, raw_item in enumerate(raw_items, start=1):
        name = raw_item.strip()
        if not name:
            raise ValueError(
                "Marca d'água inválida no item "
                f"{position}. Informe um nome de coluna entre as vírgulas."
            )
        upper_name = name.upper()
        if ":" in name or upper_name.endswith(" ASC") or upper_name.endswith(" DESC"):
            raise ValueError(
                "Marca d'água aceita somente nomes de colunas. Não informe "
                "ASC ou DESC: a ordenação é sempre ascendente."
            )
    names = normalize_watermark_columns(raw_items)
    return {"columns": [{"name": name, "direction": "ASC"} for name in names]}


def format_watermark(value: Mapping[str, Any] | None) -> str:
    if not value:
        return ""
    columns = value.get("columns", [])
    return ", ".join(
        str(item["name"])
        for item in columns
        if isinstance(item, Mapping) and item.get("name")
    )


def generate_watermark_validation_script(
    *,
    source_database: str,
    source_schema: str,
    source_table: str,
    watermark: str,
) -> str:
    """Compatibility wrapper around the specialized SQL renderer."""

    parsed = parse_watermark(watermark)
    if parsed is None:
        raise ValueError(
            "Informe ao menos uma coluna em Marca d'água antes de gerar o script"
        )
    return render_watermark_validation_sql(
        source_database=source_database,
        source_schema=source_schema,
        source_table=source_table,
        columns=[item["name"] for item in parsed["columns"]],
    )


def build_authentication(
    *,
    authentication_type: str,
    username: str = "",
    domain: str = "",
    secret_provider: str = "env",
    secret_reference: str = "",
) -> dict[str, Any]:
    """Build an authentication block without ever receiving a secret value."""

    if authentication_type == "windows_integrated":
        return {"type": authentication_type}
    if authentication_type not in {"sql", "windows_credentials"}:
        raise ValueError("Tipo de autenticação inválido")
    if not username.strip():
        raise ValueError("Usuário é obrigatório para a autenticação selecionada")
    if secret_provider not in {"prompt", "env", "windows_credential_manager"}:
        raise ValueError("Provedor de senha inválido")
    if secret_provider != "prompt" and not secret_reference.strip():
        raise ValueError("Referência do segredo é obrigatória para o provedor selecionado")
    result: dict[str, Any] = {
        "type": authentication_type,
        "username": username.strip(),
        "password": {
            "provider": secret_provider,
            "reference": None if secret_provider == "prompt" else secret_reference.strip(),
        },
    }
    if authentication_type == "windows_credentials":
        if not domain.strip():
            raise ValueError("Domínio é obrigatório para credencial Windows")
        result["domain"] = domain.strip()
    return result


def build_endpoint(
    values: Mapping[str, Any],
    *,
    source: bool,
) -> dict[str, Any]:
    """Build one source/destination endpoint from form-safe scalar values."""

    port = parse_required_integer(
        str(values.get("port", "")),
        "Porta do SQL Server",
        minimum=1,
        maximum=65_535,
    )
    result: dict[str, Any] = {
        "instance": str(values.get("instance", "")).strip(),
        "port": port,
        "database": str(values.get("database", "")).strip(),
        "schema": str(values.get("schema", "")).strip(),
        "authentication": build_authentication(
            authentication_type=str(values.get("authentication_type", "")),
            username=str(values.get("username", "")),
            domain=str(values.get("domain", "")),
            secret_provider=str(values.get("secret_provider", "env")),
            secret_reference=str(values.get("secret_reference", "")),
        ),
    }
    if not source:
        result["structure_profile"] = str(values.get("structure_profile", "")).strip()
    dsn = str(values.get("odbc_dsn", "")).strip()
    if dsn:
        result["odbc_dsn"] = dsn
    if values.get("tls_override", True):
        result["tls"] = {
            "encrypt": bool(values.get("encrypt", True)),
            "trust_server_certificate": bool(values.get("trust_server_certificate", False)),
        }
    return result


def _parse_landing_constant(name: str, value: str) -> Any:
    text = value.strip()
    if not text:
        raise ValueError(f"{name}: informe o valor constante")
    if name == "aud_enttyp":
        # Operators normally type PT rather than the JSON string literal "PT".
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        if not isinstance(parsed, str):
            raise ValueError(f"{name}: a constante deve ser texto")
        return parsed
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name}: a constante deve ser um inteiro JSON") from error
    if type(parsed) is not int:
        raise ValueError(f"{name}: a constante deve ser um inteiro")
    return parsed


def build_landing_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    """Build the three explicit Landing mappings required by the engine."""

    result: dict[str, Any] = {}
    for name, sql_type in _LANDING_TYPES.items():
        mode = str(values.get(f"{name}_mode", "constant"))
        raw_value = str(values.get(f"{name}_value", ""))
        if mode == "source_column":
            if not raw_value.strip():
                raise ValueError(f"{name}: informe a coluna da origem")
            result[name] = {"source_column": raw_value.strip()}
        elif mode == "constant":
            result[name] = {
                "constant": _parse_landing_constant(name, raw_value),
                "sql_type": sql_type,
            }
        else:
            raise ValueError(f"{name}: modo de mapeamento inválido")
    return result


def automatic_destination_table(source_database: Any, source_table: Any) -> str:
    """Return the editable destination-table suggestion used by the GUI."""

    database = str(source_database).strip()
    table = str(source_table).strip()
    if not database or not table:
        return ""
    return f"{database}_{table}".lower()


def build_table(
    values: Mapping[str, Any],
    *,
    destination_area: str,
    default_rows_per_block: Any = "",
    default_structure_profile: Any = "",
) -> dict[str, Any]:
    """Build one table entry and keep optional overrides absent when blank."""

    source_database = str(values.get("source_database", "")).strip()
    if not source_database:
        raise ValueError("Banco de dados de origem é obrigatório")
    source_table = str(values.get("source_table", "")).strip()
    if not source_table:
        raise ValueError("Tabela de origem é obrigatória")
    source_schema = str(values.get("source_schema", "")).strip()
    if not source_schema:
        raise ValueError("Esquema de origem é obrigatório")
    destination_schema = str(values.get("destination_schema", "")).strip()
    if not destination_schema:
        raise ValueError("Esquema de destino é obrigatório")
    destination_database = str(values.get("destination_database", "")).strip()
    if not destination_database:
        raise ValueError("Banco de dados de destino é obrigatório")
    result: dict[str, Any] = {
        "source_database": source_database,
        "source_table": source_table,
        "source_schema": source_schema,
        "destination_database": destination_database,
        "destination_schema": destination_schema,
        "enable_cdc": bool(values.get("enable_cdc", False)),
        "watermark": parse_watermark(str(values.get("watermark", ""))),
    }
    destination_table = str(values.get("destination_table", "")).strip()
    if destination_table:
        result["destination_table"] = destination_table

    structure_profile = str(values.get("structure_profile", "")).strip()
    inherited_profile = str(default_structure_profile).strip()
    if structure_profile and structure_profile.casefold() != inherited_profile.casefold():
        result["structure_profile"] = structure_profile
    rows_per_block = parse_optional_integer(
        str(values.get("rows_per_block", "")),
        "Linhas por bloco da tabela",
        minimum=1,
        maximum=5_000_000,
    )
    inherited_rows = parse_optional_integer(
        str(default_rows_per_block),
        "Linhas por bloco globais",
        minimum=1,
        maximum=5_000_000,
    )
    if rows_per_block is not None and rows_per_block != inherited_rows:
        result["rows_per_block"] = rows_per_block
    if bool(values.get("partition_enabled", False)):
        partition_column = str(values.get("partition_column", "")).strip()
        if not partition_column:
            raise ValueError("Coluna de particionamento é obrigatória quando ativada")
        result["partition_column"] = partition_column
    if destination_area == "landing":
        result["metadata_mapping"] = build_landing_metadata(values)
    return result


def table_to_form(
    value: Mapping[str, Any],
    *,
    default_source_database: Any = "",
    default_source_schema: Any = "",
    default_destination_database: Any = "",
    default_destination_schema: Any = "",
    default_rows_per_block: Any = "",
    default_structure_profile: Any = "",
) -> dict[str, Any]:
    """Convert a validated table object into edit-dialog scalar values."""

    is_new_table = not value
    source_database = value.get("source_database", default_source_database)
    source_table = value.get("source_table", "")
    destination_table = value.get(
        "destination_table",
        automatic_destination_table(source_database, source_table),
    )
    result: dict[str, Any] = {
        "source_database": source_database,
        "source_schema": value.get("source_schema", default_source_schema),
        "source_table": source_table,
        "destination_database": value.get(
            "destination_database", default_destination_database
        ),
        "destination_schema": value.get(
            "destination_schema", default_destination_schema
        ),
        "destination_table": destination_table,
        "rows_per_block": value.get("rows_per_block", default_rows_per_block),
        "structure_profile": value.get(
            "structure_profile", default_structure_profile
        ),
        "enable_cdc": bool(value.get("enable_cdc", False)),
        "watermark": format_watermark(value.get("watermark")),
        "partition_enabled": is_new_table or bool(value.get("partition_column")),
        "partition_column": value.get("partition_column", "dh_carga"),
        "aud_ccid_mode": "constant",
        "aud_ccid_value": "1",
        "aud_cntrrn_mode": "constant",
        "aud_cntrrn_value": "1",
        "aud_enttyp_mode": "constant",
        "aud_enttyp_value": "PT",
    }
    mappings = value.get("metadata_mapping", {})
    if isinstance(mappings, Mapping):
        for name in _LANDING_TYPES:
            mapping = mappings.get(name)
            if not isinstance(mapping, Mapping):
                continue
            if "source_column" in mapping:
                result[f"{name}_mode"] = "source_column"
                result[f"{name}_value"] = str(mapping["source_column"])
            elif "constant" in mapping:
                result[f"{name}_mode"] = "constant"
                constant = mapping["constant"]
                result[f"{name}_value"] = (
                    constant if isinstance(constant, str) else json.dumps(constant)
                )
    return result


def validate_table_in_context(
    base_config: Mapping[str, Any],
    table: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a table with the exact runtime contract, not a GUI shadow schema."""

    candidate = deepcopy(dict(base_config))
    candidate["tables"] = [deepcopy(dict(table))]
    return validate_config(candidate)["tables"][0]


def write_config_atomic(path: Path, config: Mapping[str, Any]) -> None:
    """Persist a validated config without exposing partial JSON to another process."""

    normalized = validate_config(config)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    try:
        partial.unlink(missing_ok=True)
        with partial.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(normalized, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def resolve_profile_paths_for_editor(
    config: Mapping[str, Any],
    config_path: Path | None,
) -> dict[str, Any]:
    """Anchor relative custom profiles before an edited config is saved elsewhere.

    Built-in profile aliases remain aliases.  File-backed profiles become
    absolute using the directory of the file that was opened, which prevents
    ``Save as`` from silently changing their meaning.
    """

    result = deepcopy(dict(config))
    if config_path is None:
        return result
    base = config_path.resolve().parent

    def anchored(value: Any) -> Any:
        if not isinstance(value, str) or value.casefold() in {"bronze", "landing"}:
            return value
        if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
            return value
        return str((base / value).resolve())

    for endpoint_name in ("bronze_destination", "landing_destination"):
        endpoint = result.get(endpoint_name)
        if isinstance(endpoint, dict) and "structure_profile" in endpoint:
            endpoint["structure_profile"] = anchored(endpoint["structure_profile"])
    tables = result.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if isinstance(table, dict) and "structure_profile" in table:
                table["structure_profile"] = anchored(table["structure_profile"])
    return result


def default_gui_config(project_root: Path | None = None) -> dict[str, Any]:
    """Return an infrastructure-neutral, validated starter configuration."""

    control_path, export_path, _ddl_path = ensure_runtime_directories(project_root)
    executor_directory = str(export_path)
    control_directory = str(control_path)
    raw: dict[str, Any] = {
        "config_version": 2,
        "perimeter": "DESENVOLVIMENTO",
        "source": {
            "instance": "SERVIDOR_ORIGEM",
            "port": 1433,
            "database": "BANCO_ORIGEM",
            "schema": "dbo",
            "authentication": {
                "type": "sql",
                "username": "u684",
                "password": {"provider": "prompt", "reference": None},
            },
            "tls": {"encrypt": True, "trust_server_certificate": False},
        },
        "bronze_destination": {
            "instance": "SERVIDOR_BRONZE",
            "port": 1433,
            "database": "DBRO684",
            "schema": "dbo",
            "structure_profile": "bronze",
            "authentication": {
                "type": "sql",
                "username": "u684",
                "password": {"provider": "prompt", "reference": None},
            },
            "tls": {"encrypt": True, "trust_server_certificate": False},
        },
        "landing_destination": {
            "instance": "SERVIDOR_LANDING",
            "port": 1433,
            "database": "DLAN684",
            "schema": "dbo",
            "structure_profile": "landing",
            "authentication": {
                "type": "sql",
                "username": "u684",
                "password": {"provider": "prompt", "reference": None},
            },
            "tls": {"encrypt": True, "trust_server_certificate": False},
        },
        "active_destination": "bronze",
        "execute_import": True,
        "create_structure_if_needed": True,
        "allow_schema_evolution": False,
        "keyless_direct_load_max_rows": 5_000_000,
        "executor_directory": executor_directory,
        "destination_sql_directory": executor_directory,
        "local_control_directory": control_directory,
        "rows_per_block": 200_000,
        "max_file_bytes": 157_286_400,
        "minimum_free_space_bytes": 10_737_418_240,
        "delete_confirmed_files": True,
        "continue_after_table_error": True,
        "cdc_retention_minutes": 262_800,
        "control_schema": "dbo",
        "estimates": {
            "row_count_method": "metadata",
            "maximum_sample_rows": 10_000,
            "safety_factor": 1.25,
            "on_unavailable": "stop",
        },
        "structure": {"secondary_indexes_phase": "before_load"},
        "tables": [
            {
                "source_table": "TABELA_ORIGEM",
                "destination_table": "banco_origem_tabela_origem",
                "enable_cdc": False,
                "watermark": None,
                "partition_column": "dh_carga",
            },
        ],
    }
    return validate_config(raw)


def table_rows_summary(tables: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """Create presentation-only rows without mutating table contracts."""

    rows: list[tuple[str, ...]] = []
    for position, table in enumerate(tables, start=1):
        source_database = str(table.get("source_database", ""))
        source_schema = str(table.get("source_schema", ""))
        destination_database = str(table.get("destination_database", ""))
        destination_schema = str(table.get("destination_schema", ""))
        source_table = str(table.get("source_table", ""))
        destination_table = str(table.get("destination_table", "(automático)"))
        source_name = ".".join(
            part for part in (source_database, source_schema, source_table) if part
        )
        destination_name = ".".join(
            part
            for part in (
                destination_database,
                destination_schema,
                destination_table,
            )
            if part
        )
        rows.append(
            (
                str(position),
                source_name,
                destination_name,
                "Sim" if table.get("enable_cdc") else "Não",
                format_watermark(table.get("watermark")) or "Automática / carga direta",
                str(table.get("rows_per_block", "Global")),
                str(table.get("partition_column", "Sem particionamento")),
            )
        )
    return rows


__all__ = [
    "AUTHENTICATION_LABELS",
    "DESTINATION_LABELS",
    "ENDPOINT_DISPLAY_ORDER",
    "INDEX_PHASE_LABELS",
    "PERIMETER_VALUES",
    "ROW_COUNT_LABELS",
    "SECRET_PROVIDER_LABELS",
    "build_authentication",
    "build_disk_projection_summary",
    "build_endpoint",
    "build_landing_metadata",
    "build_table",
    "automatic_destination_table",
    "code_for_label",
    "connection_summary_rows",
    "default_gui_config",
    "default_username_for_perimeter",
    "format_watermark",
    "format_bytes_summary",
    "generate_watermark_validation_script",
    "label_for_code",
    "parse_optional_integer",
    "parse_required_integer",
    "parse_required_number",
    "parse_watermark",
    "resolve_profile_paths_for_editor",
    "table_rows_summary",
    "table_to_form",
    "split_instance_and_port",
    "update_default_username_for_perimeter",
    "validate_table_in_context",
    "write_config_atomic",
]
