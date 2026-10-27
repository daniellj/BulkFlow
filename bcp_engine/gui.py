"""Desktop adapter for the Data Export/Import Engine application services.

The module owns presentation and worker-thread coordination only.  Business
rules, SQL access, BCP execution, resume semantics, and report generation stay
inside :class:`bcp_engine.engine.BcpEngine` and the validated V2 contract.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

import ttkbootstrap as ttk
from ttkbootstrap.widgets import ToolTip

from .auth import (
    AuthError,
    SecretResolver,
    prompt_secret_environment_variable,
)
from .config import MAX_CDC_RETENTION_MINUTES, read_config, validate_config
from .engine import BcpEngine
from .gui_model import (
    AUTHENTICATION_LABELS,
    INDEX_PHASE_LABELS,
    PERIMETER_VALUES,
    ROW_COUNT_LABELS,
    SECRET_PROVIDER_LABELS,
    automatic_destination_table,
    build_disk_projection_summary,
    build_endpoint,
    build_table,
    code_for_label,
    connection_summary_rows,
    default_gui_config,
    format_bytes_summary,
    format_minutes_as_days,
    format_watermark,
    label_for_code,
    parse_required_integer,
    parse_required_number,
    parse_watermark,
    resolve_profile_paths_for_editor,
    split_instance_and_port,
    table_to_form,
    update_default_username_for_perimeter,
    write_config_atomic,
)
from .models import TableStatus
from .profiles import (
    DEFAULT_PROFILE_DIRECTORY,
    ProfileError,
    load_profile,
    profile_matches_area,
    profile_requires_aud_mapping,
)
from .prerequisites import inspect_prerequisites
from .reporting import format_bytes, render_execution, render_plan_rows
from .runtime import (
    ensure_runtime_directories,
    runtime_config_directory,
    runtime_data_root,
    runtime_ddl_directory,
)
from .util import redact_structure, redact_text, redacted_exception
from .watermark_validation import (
    WATERMARK_VALIDATION_GUIDANCE_PT_BR,
    render_watermark_validation_sql,
    watermark_validation_filename,
)
from .windows_dpapi import protect_machine_secret


RUNTIME_DATA_ROOT = runtime_data_root()
RUNTIME_DDL_DIRECTORY = runtime_ddl_directory()
RUNTIME_CONFIG_DIRECTORY = runtime_config_directory()


def _cli_executable_directory() -> Path:
    """Return the directory containing the GUI/CLI release executables."""

    if bool(getattr(sys, "frozen", False)):
        executable_directory = Path(sys.executable).resolve().parent
        project_candidate = executable_directory.parent
        if (
            executable_directory.name.casefold() == "release"
            and (project_candidate / "bcp_bronze.py").is_file()
            and (project_candidate / "templates").is_dir()
        ):
            return executable_directory
        return executable_directory
    project_root = Path(__file__).resolve().parents[1]
    return project_root / "release"


CLI_EXECUTABLE_DIRECTORY = _cli_executable_directory()


class _ScrollableTab(ttk.Frame):
    """Notebook page with viewport-aware horizontal and vertical scrolling."""

    def __init__(self, master: tk.Misc, *, padding: int = 14) -> None:
        super().__init__(master)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        background = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(
            self,
            highlightthickness=0,
            borderwidth=0,
            background=background,
        )
        self.vertical = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview
        )
        self.horizontal = ttk.Scrollbar(
            self, orient="horizontal", command=self.canvas.xview
        )
        self.canvas.configure(
            yscrollcommand=self.vertical.set,
            xscrollcommand=self.horizontal.set,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical.grid(row=0, column=1, sticky="ns")
        self.horizontal.grid(row=1, column=0, sticky="ew")
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.content.bind("<Configure>", self._sync_scroll_region, add="+")
        self.canvas.bind("<Configure>", self._fit_content_to_viewport, add="+")

    def _sync_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _fit_content_to_viewport(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(
            self._window,
            width=max(event.width, self.content.winfo_reqwidth()),
            height=max(event.height, self.content.winfo_reqheight()),
        )
        self._sync_scroll_region()

    def contains(self, widget: tk.Misc) -> bool:
        current: tk.Misc | None = widget
        while current is not None:
            if current is self.content or current is self.canvas:
                return True
            current = getattr(current, "master", None)
        return False

    def scroll_vertical(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")


PRODUCT_NAME = "BulkFlow - SQL Server Data Export & Load"
WINDOW_TITLE = PRODUCT_NAME
TABLE_CDC_LABEL = "Ativar CDC na tabela de origem"
DEFAULT_VALUE_COLOR = "#e8f3ff"
_NO_DEFAULT = object()

CONNECTION_TAB_TITLES: dict[str, str] = {
    "source": "Origem",
    "bronze_destination": "Destino 01",
    "landing_destination": "Destino 02",
}
CONNECTION_SUMMARY_VISIBLE_ROWS = 3
PROCESS_FLOW_CARD_TITLES = (
    "01 · PREPARAR",
    "02 · PLANEJAR LOTE",
    "03 · ESTRUTURA",
    "04 · EXPORTAR BCP",
    "05 · IMPORTAR / VALIDAR",
    "06 · CONFIRMAR E FINALIZAR",
    "07 · HÁ MAIS LOTES?",
)


def build_process_flow_cards(
    *,
    source_database: str,
    destination_databases: list[str],
    export_directory: str,
    import_directory: str,
    create_structure: bool,
    execute_import: bool,
    delete_confirmed_files: bool,
) -> tuple[list[str], str]:
    destinations = " e ".join(destination_databases) or "nenhum destino habilitado"
    structure_step = (
        f"Criar ou validar em {destinations}, quando necessário."
        if create_structure and destination_databases
        else f"Usar estruturas existentes em {destinations}; não criar tabelas."
    )
    if execute_import:
        import_step = f"Carregar o lote no Destino 01 a partir de {import_directory}."
        confirm_step = "Confirmar a carga nos controles local e SQL."
        file_step = (
            "Excluir o BCP confirmado."
            if delete_confirmed_files
            else "Manter o BCP após a importação."
        )
        sequential_note = (
            "O próximo lote só começa após a confirmação da importação anterior. "
            "Se a importação falhar, o BCP é preservado para diagnóstico ou retomada."
        )
    else:
        import_step = "Não importar: operação configurada somente para exportação."
        confirm_step = "Validar e registrar o arquivo BCP exportado."
        file_step = "Manter o arquivo BCP exportado."
        sequential_note = (
            "O próximo lote só começa após a validação e o registro do BCP anterior."
        )
    cards = [
        "Validar configuração, conexões e pré-requisitos do executor.",
        "Definir o próximo lote da tabela de origem.",
        structure_step,
        f"Executar BCP queryout de {source_database} para {export_directory}.",
        import_step,
        f"{confirm_step} {file_step}",
        "Sim: voltar ao próximo lote. Não: avançar para a próxima tabela.",
    ]
    notes = (
        "O BulkFlow processa um lote por vez. "
        + sequential_note
        + " Manifestos, formatos e logs permanecem disponíveis para auditoria. "
        "Tabelas sem chave elegíveis para carga direta usam um único bloco."
    )
    return cards, notes


def build_process_flow_description(
    *,
    source_database: str,
    destination_databases: list[str],
    export_directory: str,
    import_directory: str,
    create_structure: bool,
    execute_import: bool,
    delete_confirmed_files: bool,
) -> str:
    cards, notes = build_process_flow_cards(
        source_database=source_database,
        destination_databases=destination_databases,
        export_directory=export_directory,
        import_directory=import_directory,
        create_structure=create_structure,
        execute_import=execute_import,
        delete_confirmed_files=delete_confirmed_files,
    )
    steps = ["Para cada tabela do fluxo", *cards]
    return "\n↓\n".join(steps) + "\n\n" + notes


def build_cli_workflow_command(
    config_path: Path | None,
    *,
    execute_import: bool,
    ddl_area: str | None,
    ddl_output: str | Path,
    cli_directory: str | Path = CLI_EXECUTABLE_DIRECTORY,
    prompt_credentials: Sequence[tuple[str, str, str]] = (),
    delete_config_on_success: bool = False,
) -> str:
    """Build a fail-fast PowerShell workflow for idempotent DDL then load."""

    if config_path is None:
        return "Salve a configuração para gerar o comando da CLI."
    if ddl_area not in {"bronze", "landing", "both"}:
        return (
            "Nenhum destino habilitado permite criar tabelas. Configure uma função "
            "com estrutura antes de gerar o fluxo da CLI."
        )
    resolved_config = str(config_path.resolve())
    resolved_output = str(Path(ddl_output).expanduser().resolve())
    change_directory = subprocess.list2cmdline(
        ["cd", str(Path(cli_directory).expanduser().resolve())]
    )
    ddl = subprocess.list2cmdline(
        [
            r".\BulkFlowCLI.exe",
            "ddl",
            "--config",
            resolved_config,
            "--area",
            ddl_area,
            "--output",
            resolved_output,
            "--apply",
            "--confirm",
        ]
    )
    run_arguments = [r".\BulkFlowCLI.exe", "run", "--config", resolved_config]
    run_arguments.append("--confirm-load" if execute_import else "--export-only")
    run = subprocess.list2cmdline(run_arguments)

    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    grouped: dict[str, list[tuple[str, str]]] = {}
    for environment_name, label, identity in prompt_credentials:
        grouped.setdefault(identity, []).append((environment_name, label))
    secret_setup: list[str] = []
    secret_cleanup: list[str] = []
    for index, entries in enumerate(grouped.values(), start=1):
        variable = f"$bulkFlowPassword{index}"
        label = entries[0][1]
        secret_setup.append(
            f"{variable} = [System.Net.NetworkCredential]::new('', "
            f"(Read-Host {ps_literal(label)} -AsSecureString)).Password"
        )
        for environment_name, _label in entries:
            secret_setup.append(f"$env:{environment_name} = {variable}")
            secret_cleanup.append(
                f"Remove-Item Env:{environment_name} -ErrorAction SilentlyContinue"
            )
        secret_cleanup.append(f"{variable} = $null")

    run_stage = (
        "Exportar e importar dados, uma tabela por vez"
        if execute_import
        else "Exportar dados, uma tabela por vez"
    )
    workflow = (
        "Write-Host '[BulkFlow] Etapa 1/2 - Criar ou atualizar estruturas com "
        f"DDL idempotente'; {ddl}; "
        "if ($LASTEXITCODE -ne 0) { throw '[BulkFlow] Falha na etapa 1/2; "
        "a carga nao foi iniciada.' }; "
        f"Write-Host '[BulkFlow] Etapa 2/2 - {run_stage}'; {run}; "
        "if ($LASTEXITCODE -ne 0) { throw '[BulkFlow] Falha na etapa 2/2.' }"
    )
    if delete_config_on_success:
        workflow += (
            "; Write-Host '[BulkFlow] Fluxo concluido; removendo o pacote temporario'; "
            f"Remove-Item -LiteralPath {ps_literal(resolved_config)} -Force -ErrorAction Stop; "
            "Write-Host '[BulkFlow] Pacote temporario removido com sucesso'"
        )
    if secret_setup:
        workflow = (
            "; ".join(secret_setup)
            + "; try { "
            + workflow
            + " } finally { "
            + "; ".join(secret_cleanup)
            + " }"
        )
    return f"{change_directory}; {workflow}"


def build_temporary_execution_package(
    config: Mapping[str, Any],
    previous_config: Mapping[str, Any],
    passwords: Mapping[str, str],
    *,
    protector: Callable[[str], str] = protect_machine_secret,
) -> dict[str, Any]:
    """Return a validated one-time config with only DPAPI-encrypted passwords."""

    package = deepcopy(dict(config))
    for endpoint_name in (
        "source",
        "bronze_destination",
        "landing_destination",
    ):
        endpoint = package.get(endpoint_name)
        if endpoint is None or endpoint["authentication"]["type"] == "windows_integrated":
            continue
        password = str(passwords.get(endpoint_name, ""))
        if password:
            endpoint["authentication"]["password"] = {
                "provider": "windows_dpapi",
                "reference": protector(password),
            }
            continue
        previous = previous_config.get(endpoint_name) or {}
        previous_secret = (
            (previous.get("authentication") or {}).get("password") or {}
        )
        if previous_secret.get("provider") != "windows_dpapi":
            raise ValueError(
                "Informe a senha mascarada em Conexões -> "
                + CONNECTION_TAB_TITLES[endpoint_name]
                + "."
            )
        endpoint["authentication"]["password"] = dict(previous_secret)
    package["execution_package"] = {
        "temporary": True,
        "delete_on_success": True,
        "protection": "windows_dpapi_local_machine",
    }
    return validate_config(package)


def build_run_confirmation(*, execute_import: bool, destination_database: str) -> str:
    if execute_import:
        return "Deseja exportar e importar os dados em uma nova execução?"
    database = destination_database.strip() or "<não informado>"
    return (
        f"O processo não importará os dados no Destino 01 ({database}), pois "
        "a configuração '2. Geral -> Importar no destino' está desligada. "
        "A exportação prosseguirá de forma completa, gerando todos os arquivos.\n\n"
        "Deseja iniciar a exportação?"
    )


def build_disk_projection_guidance(
    *,
    destination_instance: str,
    export_directory: str,
    import_directory: str,
    delete_confirmed_files: bool,
    max_file_bytes: str | int,
) -> str:
    """Render operator capacity checks according to BCP retention behavior."""

    if delete_confirmed_files:
        limit_in_mb = format_bytes_summary(max_file_bytes).split(" | ", 1)[0]
        folder_projection = f"Projeção do lote: {limit_in_mb}"
    else:
        folder_projection = "Projeção de disco"

    def normalized(path: str) -> str:
        return path.strip().replace("/", "\\").rstrip("\\").casefold()

    lines = [
        "O operador deve avaliar:",
        "- Espaço disponível na instância de banco de dados de destino "
        f"{destination_instance} X Projeção de disco",
        f"- Espaço disponível em {export_directory} X {folder_projection}",
    ]
    if not (
        bool(normalized(export_directory))
        and normalized(export_directory) == normalized(import_directory)
    ):
        lines.append(
            f"- Espaço disponível em {import_directory} X {folder_projection}"
        )
    return "\n".join(lines)


def validate_structure_profile_selection(
    path: str | Path, *, area: str
) -> tuple[Path, str]:
    """Validate a selected profile file and its destination family."""

    resolved = Path(path).expanduser().resolve(strict=True)
    profile_name = validate_structure_profile_reference(resolved, area=area)
    return resolved, profile_name


def validate_structure_profile_reference(
    profile: str | Path, *, area: str
) -> str:
    """Validate any built-in alias or file-backed profile for an area."""

    loaded = load_profile(profile)
    profile_name = str(loaded["name"])
    if not profile_matches_area(profile_name, area):
        expected = "'bronze'" if area == "bronze" else "um nome iniciado por 'landing_'"
        raise ProfileError(
            f"Perfil {profile_name!r} incompatível com este destino: o campo "
            f"'name' deve ser {expected}."
        )
    return profile_name


SOURCE_ROLE_LABELS: dict[str, str] = {
    "provedor de dados": "data_provider",
}
DESTINATION_ROLE_LABELS: dict[str, str] = {
    "estrutura e dados": "structure_and_data",
    "somente estrutura": "structure_only",
    "somente dados (premissa: já existir a estrutura)": "data_only",
}
DEFAULT_ENDPOINT_ROLES: dict[str, str] = {
    "source": "data_provider",
    "bronze_destination": "structure_and_data",
    "landing_destination": "structure_only",
}
COLUMN_COLLISION_STRATEGY_LABELS: dict[str, str] = {
    "Preservar Origem com sufixo _origem": "rename_source",
    "Manter somente a coluna de destino": "keep_destination",
}
DESTINATION_ENDPOINT_KEYS: dict[str, str] = {
    "bronze": "bronze_destination",
    "landing": "landing_destination",
}
DATA_DESTINATION_ROLES = frozenset({"structure_and_data", "data_only"})


FIELD_HELP: dict[str, str] = {
    "perimeter": "Perímetro operacional. Sugere o usuário SQL, mas cada conexão continua independente.",
    "execute_import": "Quando habilitado, exporta da Origem e importa os arquivos confirmados no destino de dados.",
    "create_structure_if_needed": (
        "Controla o provisionamento automático do destino com função Estrutura e dados durante executar, retomar "
        "ou importar manifestos. Desmarcado, exige estrutura compatível já existente. "
        "O botão explícito Aplicar DDL respeita a função de cada destino. "
        "O padrão é habilitado."
    ),
    "allow_schema_evolution": "Permite somente a adição segura de novas colunas encontradas na Origem.",
    "delete_confirmed_files": "Remove arquivos exportados apenas depois da confirmação durável da importação.",
    "delete_completed_table_artifacts": (
        "Após a importação e a validação integral de cada tabela, remove seus "
        "manifestos, arquivos de formato, logs e diretórios auxiliares. Tabelas "
        "canceladas, incompletas ou com erro são preservadas para retomada. Os "
        "controles local/SQL e os relatórios consolidados permanecem."
    ),
    "continue_after_table_error": "Registra a falha da tabela e continua o processamento das tabelas seguintes.",
    "require_watermark_index": "Exige índice compatível com a marca d'água antes de autorizar a extração.",
    "executor_directory": (
        "Diretório em que o programa exporta os arquivos BCP. A pasta deve ser "
        "acessível tanto no fluxo da Origem quanto no Destino, preferencialmente "
        "por um caminho de rede, com permissões de leitura e escrita para as "
        "identidades que executam o processo."
    ),
    "destination_sql_directory": (
        "Diretório pelo qual o SQL Server do destino de dados acessa os arquivos "
        "durante a importação. A pasta deve ser acessível tanto no fluxo da Origem "
        "quanto no Destino, preferencialmente por um caminho de rede, com permissões "
        "de leitura e escrita para as identidades que executam o processo."
    ),
    "artifact_reader_sids": "SIDs Windows específicos que precisam ler os artefatos; separe por vírgula.",
    "artifact_writer_sids": "SIDs Windows excepcionais autorizados a escrever no compartilhamento SMB.",
    "local_control_directory": "Diretório local do controle SQLite e dos checkpoints de retomada.",
    "rows_per_block": "Quantidade-alvo de linhas em cada lote paginado. Empates podem ampliar o lote.",
    "keyless_direct_load_max_rows": "Limite para carga direta de tabela sem PK, UNIQUE ou marca d'água aceita.",
    "max_file_bytes": "Tamanho máximo permitido para um arquivo BCP individual.",
    "minimum_free_space_bytes": "Reserva mínima que deve permanecer livre no disco do executor.",
    "row_count_method": (
        "A cardinalidade do planejamento é sempre aproximada e obtida por "
        "sys.partitions; a projeção nunca executa COUNT_BIG."
    ),
    "maximum_sample_rows": "Máximo de linhas usadas para estimar o tamanho médio de um registro.",
    "safety_factor": "Margem multiplicadora aplicada à estimativa de espaço; 1,25 acrescenta 25%.",
    "secondary_indexes_phase": "Define se índices secundários são criados antes ou depois da carga.",
    "odbc_driver": "Nome exato do driver registrado no executor, por exemplo ODBC Driver 18 for SQL Server.",
    "bcp_executable": "Nome no PATH ou caminho absoluto do utilitário bcp instalado no executor.",
    "connection_timeout_seconds": "Tempo máximo, em segundos, para abrir uma conexão SQL.",
    "sql_timeout_seconds": "Tempo máximo de uma operação SQL; zero significa sem limite.",
    "bcp_timeout_seconds": "Tempo máximo de um processo BCP; zero significa sem limite.",
    "cdc_retention_minutes": (
        "Prazo global de retenção das alterações do CDC, em minutos. "
        "O padrão de 262.800 minutos equivale a aproximadamente 182,5 dias "
        "(seis meses) e só é "
        "aplicado quando ao menos uma tabela solicita CDC."
    ),
    "control_schema": (
        "Esquema fixo dbo do destino de dados que armazena o controle persistente das "
        "execuções. Este valor não pode ser alterado."
    ),
    "endpoint_enabled": "Inclui este destino na configuração e nas operações estruturais.",
    "role": (
        "Define a função da conexão no fluxo. A Origem fornece os dados. Cada "
        "destino pode receber estrutura e dados, somente estrutura ou somente dados; "
        "neste último caso, a estrutura compatível deve existir previamente."
    ),
    "instance": "Nome DNS, host ou instância SQL Server, sem o número da porta.",
    "port": "Porta TCP obrigatória do serviço SQL Server, entre 1 e 65535.",
    "database": "Banco de dados usado por esta conexão.",
    "schema": "Esquema SQL. Nos destinos, o nome é normalizado para minúsculas.",
    "odbc_dsn": "DSN ODBC opcional. Quando preenchido, ele define a rota efetiva da conexão.",
    "structure_profile": "Perfil JSON que define metadados, índices e estrutura técnica do destino.",
    "structure_profile_import": (
        "Carrega e valida imediatamente um perfil JSON personalizado. No Destino 01, "
        "o campo 'name' deve ser 'bronze'; no Destino 02, deve começar com 'landing_'. "
        "Use como exemplos/esqueletos os arquivos templates\\bronze.json, "
        "templates\\landing_with_aud.json e templates\\landing_without_aud.json. "
        "O caminho absoluto do arquivo validado será salvo na configuração para uso "
        "idêntico pelo GUI e pelo BulkFlowCLI.exe."
    ),
    "authentication_type": "Método de autenticação usado exclusivamente por este ambiente.",
    "username": "Usuário da conexão. Pode ser diferente na Origem, no Destino 01 e no Destino 02.",
    "password": "Senha mantida somente em memória nesta sessão. Não é gravada no JSON nem exibida nos logs.",
    "domain": "Domínio Windows; editável somente para o tipo Credencial Windows.",
    "encrypt": "Solicita conexão criptografada ao SQL Server.",
    "trust_server_certificate": "Aceita o certificado apresentado sem validar sua cadeia de confiança.",
    "source_database": (
        "Banco de dados da tabela na Origem. Nesta versão, deve coincidir com o "
        "banco configurado na conexão de Origem; altere primeiro a aba Conexões."
    ),
    "source_table": "Nome da tabela existente no banco de Origem.",
    "destination_database": (
        "Banco de dados da tabela no destino de dados. Deve coincidir com o "
        "banco configurado nessa conexão; altere primeiro a aba Conexões."
    ),
    "destination_table": (
        "Nome da tabela nos destinos. O padrão é banco_origem_tabela_origem em "
        "minúsculas e acompanha a origem enquanto não for personalizado."
    ),
    "source_schema": "Esquema obrigatório da tabela na Origem.",
    "destination_schema": "Esquema obrigatório da tabela no destino de dados.",
    "table_rows_per_block": (
        "Exibe o tamanho global do lote. O valor só é salvo como override desta "
        "tabela quando for diferente do valor global."
    ),
    "table_structure_profile": (
        "Exibe o perfil do destino de dados. O valor só é salvo como override desta tabela "
        "quando for diferente do perfil do destino."
    ),
    "watermark": (
        "Usada principalmente em tabelas sem chave primária (PK) ou chave UNIQUE. "
        "Informe uma coluna, ou uma combinação de colunas separadas por vírgula, "
        "capaz de distinguir e ordenar as linhas. A marca d'água controla os lotes, "
        "registra onde o processamento parou e permite sua retomada segura. A ordem "
        "é sempre ascendente; não informe ASC ou DESC."
    ),
    "watermark_validation_script": WATERMARK_VALIDATION_GUIDANCE_PT_BR,
    "enable_cdc": (
        "Ativa e confirma o CDC em banco_origem.esquema_origem.tabela_origem "
        "antes da exportação."
    ),
    "partition_enabled": "Habilita particionamento da tabela de destino pela coluna informada.",
    "partition_column": "Coluna datetime2 usada no particionamento mensal; sugestão: dh_carga.",
    "ddl_area": "Seleciona quais destinos recebem os scripts de estrutura.",
    "resolve_column_collisions": (
        "Quando marcado, uma coluna da Origem que colide com uma coluna técnica ou de "
        "metadados do perfil é preservada no destino com o sufixo '_origem'. Exemplo: "
        "Origem.schema.table.dh_carga e <Destino 01 ou Destino 02>.schema.table.dh_carga. Cada resolução "
        "é exibida no acompanhamento e deve ser revisada pelo operador. Desmarcado, a "
        "colisão interrompe a geração do DDL com diagnóstico detalhado."
    ),
    "column_collision_strategy": (
        "Define como resolver cada homônimo. '_origem' preserva os dois valores, "
        "renomeando a coluna da Origem. 'Manter somente a coluna de destino' não "
        "materializa a homônima da Origem e mantém apenas a coluna técnica ou de "
        "metadados definida pelo perfil do destino."
    ),
    "ddl_output": (
        "Diretório em que os scripts DDL idempotentes serão gravados. O padrão é "
        "Local\\BulkFlow\\ddl; use o botão ... para escolher outra pasta. O caminho "
        "absoluto selecionado será incluído no fluxo sugerido da CLI."
    ),
    "execution_id": "UUID usado para consultar ou retomar uma execução existente.",
    "manifest_path": "Manifesto, índice ou diretório para uma importação independente no destino de dados.",
}


def _attach_help(widget: tk.Misc, help_key: str) -> None:
    text = FIELD_HELP[help_key]
    tooltip = ToolTip(widget, text=text, wraplength=420, delay=250)
    setattr(widget, "_field_tooltip", tooltip)


def _help_label(parent: tk.Misc, text: str, help_key: str) -> ttk.Frame:
    container = ttk.Frame(parent)
    ttk.Label(container, text=text).pack(side="left")
    marker = ttk.Label(container, text=" ? ", bootstyle="info", cursor="hand2")
    marker.pack(side="left", padx=(3, 0))
    _attach_help(marker, help_key)
    marker.bind(
        "<Button-1>",
        lambda _event: messagebox.showinfo("Ajuda do parâmetro", FIELD_HELP[help_key], parent=parent),
    )
    return container


def _configuration_filename(config: Mapping[str, Any]) -> str:
    """Build the GUI/CLI configuration filename from Destino 01's schema."""

    destination_schema = str(
        config.get("bronze_destination", {}).get("schema", "")
    ).strip()
    safe_schema = re.sub(r"[^0-9A-Za-z_.-]+", "_", destination_schema).strip("._")
    return f"config_transferencia_{safe_schema or 'sem_esquema'}.json"


@dataclass
class _SecretPromptRequest:
    label: str
    ready: threading.Event
    value: str | None = None
    error: BaseException | None = None


class _WorkerCancelled(RuntimeError):
    """Signals a completed cooperative cancellation to the Tk worker bridge."""


class TableDialog:
    """Modal editor that returns GUI scalar fields, never database rows."""

    _MAPPING_LABELS = {
        "Constante": "constant",
        "Coluna da origem": "source_column",
    }

    def __init__(
        self,
        parent: tk.Misc,
        *,
        destination_area: str,
        initial: Mapping[str, Any] | None = None,
        default_source_database: str = "",
        default_source_schema: str = "",
        default_destination_database: str = "",
        default_destination_schema: str = "",
        default_rows_per_block: str = "",
        default_structure_profile: str = "",
        destination_fields_enabled: bool = True,
    ) -> None:
        self.destination_area = destination_area
        self.destination_fields_enabled = destination_fields_enabled
        self._field_widgets: dict[str, list[tk.Misc]] = {}
        self.result: dict[str, Any] | None = None
        values = dict(
            initial
            or table_to_form(
                {},
                default_source_database=default_source_database,
                default_source_schema=default_source_schema,
                default_destination_database=default_destination_database,
                default_destination_schema=default_destination_schema,
                default_rows_per_block=default_rows_per_block,
                default_structure_profile=default_structure_profile,
            )
        )
        inherited_values = {
            "source_database": default_source_database,
            "source_schema": default_source_schema,
            "destination_database": default_destination_database,
            "destination_schema": default_destination_schema,
            "rows_per_block": default_rows_per_block,
            "structure_profile": default_structure_profile,
        }
        for name, inherited in inherited_values.items():
            if not str(values.get(name, "")).strip():
                values[name] = inherited
        self.default_source_database = default_source_database
        self.default_source_schema = default_source_schema
        self.default_destination_database = default_destination_database
        self.default_destination_schema = default_destination_schema
        self.default_rows_per_block = default_rows_per_block
        self.default_structure_profile = default_structure_profile
        self._updating_automatic_destination = False
        self._previous_automatic_destination = automatic_destination_table(
            values.get("source_database", ""), values.get("source_table", "")
        )
        current_destination = str(values.get("destination_table", "")).strip()
        self._destination_is_automatic = (
            not current_destination
            or current_destination.casefold()
            == self._previous_automatic_destination.casefold()
        )
        if not current_destination:
            values["destination_table"] = self._previous_automatic_destination
        self.window = ttk.Toplevel(parent)
        self.window.title("Configurar tabela")
        self.window.geometry("850x900" if destination_area == "landing" else "850x800")
        self.window.minsize(700, 440)
        self.window.transient(parent)
        self.window.grab_set()

        self.variables: dict[str, tk.Variable] = {
            "source_database": tk.StringVar(
                value=str(values.get("source_database", ""))
            ),
            "source_table": tk.StringVar(value=str(values.get("source_table", ""))),
            "destination_database": tk.StringVar(
                value=str(values.get("destination_database", ""))
            ),
            "destination_table": tk.StringVar(value=str(values.get("destination_table", ""))),
            "source_schema": tk.StringVar(value=str(values.get("source_schema", ""))),
            "destination_schema": tk.StringVar(value=str(values.get("destination_schema", ""))),
            "rows_per_block": tk.StringVar(value=str(values.get("rows_per_block", ""))),
            "structure_profile": tk.StringVar(value=str(values.get("structure_profile", ""))),
            "enable_cdc": tk.BooleanVar(value=bool(values.get("enable_cdc", False))),
            "watermark": tk.StringVar(value=str(values.get("watermark", ""))),
            "partition_enabled": tk.BooleanVar(
                value=bool(values.get("partition_enabled", False))
            ),
            "partition_column": tk.StringVar(
                value=str(values.get("partition_column", "dh_carga"))
            ),
        }
        for name in ("aud_ccid", "aud_cntrrn", "aud_enttyp"):
            mode = str(values.get(f"{name}_mode", "constant"))
            self.variables[f"{name}_mode"] = tk.StringVar(
                value=label_for_code(self._MAPPING_LABELS, mode, "Constante")
            )
            self.variables[f"{name}_value"] = tk.StringVar(
                value=str(values.get(f"{name}_value", ""))
            )

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        row = 0
        row = self._entry(
            body,
            row,
            "Banco de dados de origem *",
            "source_database",
            help_key="source_database",
            default_value=self.default_source_database,
        )
        row = self._entry(
            body,
            row,
            "Esquema de origem *",
            "source_schema",
            help_key="source_schema",
            default_value=self.default_source_schema,
        )
        row = self._entry(
            body, row, "Tabela de origem *", "source_table", help_key="source_table"
        )
        row = self._entry(
            body,
            row,
            "Banco de dados de destino *",
            "destination_database",
            help_key="destination_database",
            default_value=self.default_destination_database,
        )
        row = self._entry(
            body,
            row,
            "Esquema de destino *",
            "destination_schema",
            help_key="destination_schema",
            default_value=self.default_destination_schema,
        )
        row = self._entry(
            body,
            row,
            "Tabela de destino (opcional; gerada automaticamente)",
            "destination_table",
            help_key="destination_table",
            default_value=self._current_automatic_destination,
        )
        row = self._entry(
            body,
            row,
            "Linhas por bloco (opcional; usa o valor global)",
            "rows_per_block",
            help_key="table_rows_per_block",
            default_value=self.default_rows_per_block,
        )
        row = self._profile_selector(body, row)
        _help_label(body, "Marca d'água (opcional)", "watermark").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=6
        )
        watermark_entry = ttk.Entry(body, textvariable=self.variables["watermark"])
        watermark_entry.grid(
            row=row, column=1, sticky="ew", pady=6
        )
        _attach_help(watermark_entry, "watermark")
        watermark_script_button = ttk.Button(
            body,
            text="Baixar script de validação…",
            command=self._save_watermark_validation_script,
            bootstyle="outline-info",
        )
        watermark_script_button.grid(
            row=row, column=2, sticky="ew", padx=(8, 0), pady=6
        )
        _attach_help(watermark_script_button, "watermark_validation_script")
        row += 1
        ttk.Label(
            body,
            text=(
                "Informe somente os nomes, separados por vírgula; a ordem é sempre ASC. "
                "Para comprovar a candidata, baixe o SQL e execute-o na Origem com "
                "permissão de leitura. Em branco: PK, UNIQUE ou carga direta limitada."
            ),
            bootstyle="secondary",
            wraplength=790,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
        row += 1
        cdc_container = ttk.Frame(body)
        cdc_container.grid(row=row, column=0, columnspan=3, sticky="w", pady=8)
        cdc = ttk.Checkbutton(
            cdc_container,
            text=TABLE_CDC_LABEL,
            variable=self.variables["enable_cdc"],
            bootstyle="round-toggle",
        )
        cdc.pack(side="left")
        _attach_help(cdc, "enable_cdc")
        cdc_help = ttk.Label(cdc_container, text=" ? ", bootstyle="info", cursor="hand2")
        cdc_help.pack(side="left", padx=(3, 0))
        _attach_help(cdc_help, "enable_cdc")
        row += 1

        partition_container = ttk.Frame(body)
        partition_container.grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        partition_container.columnconfigure(2, weight=1)
        partition = ttk.Checkbutton(
            partition_container,
            text="Particionar tabela de destino (dh_carga)",
            variable=self.variables["partition_enabled"],
            command=self._update_partition_state,
            bootstyle="round-toggle",
        )
        partition.grid(row=0, column=0, sticky="w")
        self.partition_toggle = partition
        _attach_help(partition, "partition_enabled")
        partition_help = ttk.Label(
            partition_container, text=" ? ", bootstyle="info", cursor="hand2"
        )
        partition_help.grid(row=0, column=1, sticky="w", padx=(3, 12))
        _attach_help(partition_help, "partition_enabled")
        self.partition_entry = ttk.Entry(
            partition_container,
            textvariable=self.variables["partition_column"],
            style="DefaultValue.TEntry",
        )
        self.partition_entry.grid(row=0, column=2, sticky="ew")
        _attach_help(self.partition_entry, "partition_column")
        self.variables["partition_column"].trace_add(
            "write", lambda *_args: self._update_partition_style()
        )
        self._update_partition_state()
        self._update_destination_fields_state()
        row += 1

        self.variables["source_database"].trace_add(
            "write", self._on_source_identity_changed
        )
        self.variables["source_table"].trace_add(
            "write", self._on_source_identity_changed
        )
        self.variables["destination_table"].trace_add(
            "write", self._on_destination_table_changed
        )

        if destination_area == "landing":
            self.landing_mapping_frame = ttk.Labelframe(
                body, text="Mapeamento do Destino 02 (obrigatório)", padding=12
            )
            landing = self.landing_mapping_frame
            self._landing_mapping_grid = {
                "row": row,
                "column": 0,
                "columnspan": 3,
                "sticky": "ew",
                "pady": (12, 8),
            }
            landing.grid(**self._landing_mapping_grid)
            landing.columnconfigure(2, weight=1)
            ttk.Label(landing, text="Campo").grid(row=0, column=0, sticky="w", padx=4)
            ttk.Label(landing, text="Origem do valor").grid(row=0, column=1, sticky="w", padx=4)
            ttk.Label(landing, text="Valor / coluna").grid(row=0, column=2, sticky="w", padx=4)
            for index, name in enumerate(("aud_ccid", "aud_cntrrn", "aud_enttyp"), start=1):
                ttk.Label(landing, text=f"{name} *").grid(
                    row=index, column=0, sticky="w", padx=4, pady=5
                )
                ttk.Combobox(
                    landing,
                    textvariable=self.variables[f"{name}_mode"],
                    values=list(self._MAPPING_LABELS),
                    state="readonly",
                    width=20,
                ).grid(row=index, column=1, sticky="ew", padx=4, pady=5)
                ttk.Entry(
                    landing, textvariable=self.variables[f"{name}_value"]
                ).grid(row=index, column=2, sticky="ew", padx=4, pady=5)
            self.variables["structure_profile"].trace_add(
                "write", lambda *_args: self._update_landing_mapping_visibility()
            )
            self._update_landing_mapping_visibility()
            row += 1

        ttk.Label(
            body,
            text="* campo obrigatório",
            bootstyle="secondary",
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        actions = ttk.Frame(body)
        actions.grid(row=row + 1, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(actions, text="Cancelar", command=self.window.destroy, bootstyle="secondary").pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="Salvar tabela", command=self._save, bootstyle="primary").pack(
            side="left", padx=4
        )
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.wait_visibility()
        self.window.focus_force()

    def _entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        name: str,
        *,
        browse: bool = False,
        help_key: str,
        default_value: Any = _NO_DEFAULT,
    ) -> int:
        _help_label(parent, label, help_key).grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=6
        )
        entry = ttk.Entry(parent, textvariable=self.variables[name])
        self._field_widgets.setdefault(name, []).append(entry)
        entry.grid(
            row=row, column=1, sticky="ew", pady=6
        )
        _attach_help(entry, help_key)
        if default_value is not _NO_DEFAULT:
            refresh = self._register_default_entry(
                entry, self.variables[name], default_value
            )
            if name == "destination_table":
                self._destination_style_refresh = refresh
        if browse:
            browse_button = ttk.Button(
                parent,
                text="Selecionar…",
                command=lambda: self._browse_profile(name),
                bootstyle="outline-secondary",
            )
            browse_button.grid(row=row, column=2, padx=(8, 0), pady=6)
            self._field_widgets[name].append(browse_button)
        return row + 1

    def _profile_selector(self, parent: ttk.Frame, row: int) -> int:
        _help_label(
            parent,
            "Perfil de estrutura (opcional; usa o destino)",
            "table_structure_profile",
        ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        built_in = (
            ["bronze"]
            if self.destination_area == "bronze"
            else ["landing_without_aud", "landing_with_aud"]
        )
        current = str(self.variables["structure_profile"].get()).strip()
        choices = list(built_in)
        for value in (self.default_structure_profile, current):
            if value and value not in choices:
                choices.append(value)
        self.structure_profile_choices = choices
        combobox = ttk.Combobox(
            parent,
            textvariable=self.variables["structure_profile"],
            values=choices,
            state="readonly",
        )
        combobox.grid(row=row, column=1, sticky="ew", pady=6)
        _attach_help(combobox, "table_structure_profile")
        self.structure_profile_combobox = combobox
        self._field_widgets.setdefault("structure_profile", []).append(combobox)
        self._register_default_combobox(
            combobox,
            self.variables["structure_profile"],
            self.default_structure_profile,
        )
        browse_button = ttk.Button(
            parent,
            text="...",
            width=4,
            command=self._browse_structure_profile,
            bootstyle="outline-secondary",
        )
        browse_button.grid(row=row, column=2, padx=(8, 0), pady=6)
        _attach_help(browse_button, "structure_profile_import")
        self._field_widgets["structure_profile"].append(browse_button)
        return row + 1

    @staticmethod
    def _comparable_default(value: Any) -> str:
        return str(value).strip().replace(",", ".").casefold()

    def _register_default_entry(
        self,
        entry: ttk.Entry,
        variable: tk.Variable,
        default_value: Any | Callable[[], Any],
    ) -> Callable[..., None]:
        def update(*_args: object) -> None:
            expected = default_value() if callable(default_value) else default_value
            style = (
                "DefaultValue.TEntry"
                if self._comparable_default(variable.get())
                == self._comparable_default(expected)
                else "TEntry"
            )
            entry.configure(style=style)

        variable.trace_add("write", update)
        update()
        return update

    def _register_default_combobox(
        self,
        combobox: ttk.Combobox,
        variable: tk.Variable,
        default_value: Any,
    ) -> None:
        def update(*_args: object) -> None:
            style = (
                "DefaultValue.TCombobox"
                if self._comparable_default(variable.get())
                == self._comparable_default(default_value)
                else "TCombobox"
            )
            combobox.configure(style=style)

        variable.trace_add("write", update)
        update()

    def _current_automatic_destination(self) -> str:
        return automatic_destination_table(
            self.variables["source_database"].get(),
            self.variables["source_table"].get(),
        )

    def _on_source_identity_changed(self, *_args: object) -> None:
        automatic = self._current_automatic_destination()
        current = str(self.variables["destination_table"].get()).strip()
        if (
            self._destination_is_automatic
            or not current
            or current.casefold() == self._previous_automatic_destination.casefold()
        ):
            self._updating_automatic_destination = True
            try:
                self.variables["destination_table"].set(automatic)
            finally:
                self._updating_automatic_destination = False
            self._destination_is_automatic = True
        self._previous_automatic_destination = automatic
        refresh = getattr(self, "_destination_style_refresh", None)
        if refresh is not None:
            refresh()

    def _on_destination_table_changed(self, *_args: object) -> None:
        if self._updating_automatic_destination:
            return
        current = str(self.variables["destination_table"].get()).strip()
        automatic = self._current_automatic_destination()
        self._destination_is_automatic = (
            not current or current.casefold() == automatic.casefold()
        )

    def _update_partition_style(self) -> None:
        value = str(self.variables["partition_column"].get()).strip().casefold()
        self.partition_entry.configure(
            style="DefaultValue.TEntry" if value == "dh_carga" else "TEntry"
        )

    def _update_partition_state(self) -> None:
        enabled = bool(self.variables["partition_enabled"].get())
        destination_fields_enabled = getattr(
            self, "destination_fields_enabled", True
        )
        self.partition_entry.configure(
            state=(
                "normal"
                if enabled and destination_fields_enabled
                else "disabled"
            )
        )
        self._update_partition_style()

    def _update_destination_fields_state(self) -> None:
        state = "normal" if self.destination_fields_enabled else "disabled"
        for name in (
            "destination_database",
            "destination_schema",
            "destination_table",
            "structure_profile",
        ):
            for widget in self._field_widgets.get(name, []):
                active_state = (
                    "readonly"
                    if name == "structure_profile"
                    and widget is getattr(self, "structure_profile_combobox", None)
                    else state
                )
                widget.configure(
                    state=active_state if self.destination_fields_enabled else "disabled"
                )
        self.partition_toggle.configure(state=state)
        self._update_partition_state()

    def _browse_structure_profile(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.window,
            title="Carregar perfil de estrutura JSON",
            initialdir=str(DEFAULT_PROFILE_DIRECTORY.resolve()),
            filetypes=[("Arquivos JSON", "*.json"), ("Todos os arquivos", "*.*")],
        )
        if not selected:
            return
        try:
            path, profile_name = validate_structure_profile_selection(
                selected, area=self.destination_area
            )
        except Exception as error:
            messagebox.showerror(
                "Perfil de estrutura inválido",
                "O arquivo não foi carregado.\n\n" + redacted_exception(error),
                parent=self.window,
            )
            return
        value = str(path)
        if value not in self.structure_profile_choices:
            self.structure_profile_choices.append(value)
            self.structure_profile_combobox.configure(
                values=self.structure_profile_choices
            )
        self.variables["structure_profile"].set(value)
        messagebox.showinfo(
            "Perfil de estrutura validado",
            f"Perfil {profile_name!r} carregado e selecionado.\n\n{path}",
            parent=self.window,
        )

    def _update_landing_mapping_visibility(self) -> None:
        frame = getattr(self, "landing_mapping_frame", None)
        if frame is None:
            return
        try:
            required = profile_requires_aud_mapping(
                str(self.variables["structure_profile"].get())
            )
        except Exception:
            required = False
        if required:
            frame.grid(**self._landing_mapping_grid)
        else:
            frame.grid_remove()

    def _save_watermark_validation_script(self) -> None:
        try:
            source_database = str(self.variables["source_database"].get())
            source_schema = str(self.variables["source_schema"].get())
            source_table = str(self.variables["source_table"].get())
            parsed_watermark = parse_watermark(
                str(self.variables["watermark"].get())
            )
            if parsed_watermark is None:
                raise ValueError(
                    "Informe ao menos uma coluna em Marca d'água antes de gerar o script"
                )
            script = render_watermark_validation_sql(
                source_database=source_database,
                source_schema=source_schema,
                source_table=source_table,
                columns=[item["name"] for item in parsed_watermark["columns"]],
            )
            suggested_filename = watermark_validation_filename(
                source_database, source_schema, source_table
            )
        except Exception as error:
            messagebox.showerror(
                "Não foi possível gerar o script",
                redacted_exception(error),
                parent=self.window,
            )
            return

        selected = filedialog.asksaveasfilename(
            parent=self.window,
            title="Salvar script de validação da marca d'água",
            defaultextension=".sql",
            initialfile=suggested_filename,
            filetypes=[("Script SQL", "*.sql"), ("Todos os arquivos", "*.*")],
        )
        if not selected:
            return
        try:
            Path(selected).write_text(script, encoding="utf-8-sig", newline="\n")
        except OSError as error:
            messagebox.showerror(
                "Falha ao salvar o script",
                redacted_exception(error),
                parent=self.window,
            )
            return
        messagebox.showinfo(
            "Script salvo",
            WATERMARK_VALIDATION_GUIDANCE_PT_BR,
            parent=self.window,
        )

    def _save(self) -> None:
        try:
            raw: dict[str, Any] = {
                name: variable.get() for name, variable in self.variables.items()
            }
            for name, expected, label in (
                (
                    "source_database",
                    self.default_source_database,
                    "Banco de dados de origem",
                ),
                (
                    "destination_database",
                    self.default_destination_database,
                    "Banco de dados de destino",
                ),
            ):
                if expected and (
                    str(raw.get(name, "")).strip().casefold()
                    != str(expected).strip().casefold()
                ):
                    raise ValueError(
                        f"{label} deve coincidir com a conexão correspondente nesta versão; "
                        "altere primeiro o banco na aba Conexões"
                    )
            if not str(raw.get("destination_table", "")).strip():
                raw["destination_table"] = self._current_automatic_destination()
            for name in ("aud_ccid", "aud_cntrrn", "aud_enttyp"):
                raw[f"{name}_mode"] = code_for_label(
                    self._MAPPING_LABELS,
                    str(raw[f"{name}_mode"]),
                    name,
                )
            # Parse now for immediate, field-specific feedback.  The full
            # runtime contract is validated again by the parent window.
            build_table(
                raw,
                destination_area=self.destination_area,
                default_rows_per_block=self.default_rows_per_block,
                default_structure_profile=self.default_structure_profile,
            )
        except Exception as error:
            messagebox.showerror(
                "Tabela inválida", redacted_exception(error), parent=self.window
            )
            return
        if (
            self._comparable_default(raw.get("rows_per_block", ""))
            == self._comparable_default(self.default_rows_per_block)
        ):
            # Keep the contract override absent while showing the inherited
            # global value in the editor. A later global change then continues
            # to flow naturally to this table.
            raw["rows_per_block"] = ""
        if (
            self._comparable_default(raw.get("structure_profile", ""))
            == self._comparable_default(self.default_structure_profile)
        ):
            # Preserve inheritance instead of freezing the endpoint profile as
            # a per-table override merely because it was displayed in the form.
            raw["structure_profile"] = ""
        self.result = raw
        self.window.destroy()

    def show(self) -> dict[str, Any] | None:
        self.window.wait_window()
        return self.result


class BcpGuiApplication:
    """ttkbootstrap presentation adapter for ``BcpEngine``."""

    def __init__(
        self,
        root: ttk.Window,
        *,
        initial_config_path: Path | None = None,
    ) -> None:
        self.root = root
        # File dialogs and default operational paths must exist before their
        # first use. A frozen build resolves them below LocalAppData, never
        # below Program Files.
        ensure_runtime_directories()
        RUNTIME_CONFIG_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.root.title(WINDOW_TITLE)
        # Reserve enough vertical space for the complete operations workflow,
        # including a useful Acompanhamento pane, on the first render.
        self.root.geometry("1280x980")
        # Smaller monitors remain usable because every notebook page owns a
        # viewport with horizontal and vertical scrolling.
        self.root.minsize(760, 520)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.style = ttk.Style()
        self.style.configure("DefaultValue.TEntry", fieldbackground=DEFAULT_VALUE_COLOR)
        self.style.map(
            "DefaultValue.TEntry",
            fieldbackground=[
                ("disabled", DEFAULT_VALUE_COLOR),
                ("readonly", DEFAULT_VALUE_COLOR),
            ],
        )
        self.style.configure("DefaultValue.TCombobox", fieldbackground=DEFAULT_VALUE_COLOR)
        self.style.map(
            "DefaultValue.TCombobox",
            fieldbackground=[("readonly", DEFAULT_VALUE_COLOR)],
        )

        self.current_config_path: Path | None = None
        self._last_perimeter: str | None = None
        initial_config = (
            read_config(initial_config_path)
            if initial_config_path is not None
            else default_gui_config()
        )
        self.base_config = initial_config
        self.table_forms: list[dict[str, Any]] = []
        self.worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.active_execution_engine: BcpEngine | None = None
        self.action_buttons: list[ttk.Button] = []
        self._default_widgets: list[
            tuple[tk.Misc, tk.Variable, Any | Callable[[], Any], str, bool]
        ] = []
        self._domain_entries: dict[str, ttk.Entry] = {}
        self._password_entries: dict[str, ttk.Entry] = {}
        self._operation_passwords: dict[str, str] = {}
        self._role_comboboxes: dict[str, ttk.Combobox] = {}
        self._structure_profile_comboboxes: dict[str, ttk.Combobox] = {}
        self._structure_profile_choices: dict[str, list[str]] = {
            "bronze_destination": ["bronze"],
            "landing_destination": [
                "landing_without_aud",
                "landing_with_aud",
            ],
        }
        self._suspend_schema_sync = False
        self._previous_source_schema = ""
        self._suspend_directory_sync = False
        self._previous_executor_directory = ""
        self._last_execute_import_state: bool | None = None
        self._delete_confirmed_files_preference: bool | None = None
        self._endpoint_controls: dict[str, list[tuple[tk.Misc, str]]] = {}
        self._endpoint_enabled_widgets: dict[str, ttk.Checkbutton] = {}
        self.secret_resolver = SecretResolver(prompt=self._prompt_secret_from_worker)

        self._create_variables()
        self._build_window()
        self._load_config_into_form(initial_config, path=initial_config_path)
        self.root.after(100, self._poll_worker_queue)

    def _create_variables(self) -> None:
        self.global_variables: dict[str, tk.Variable] = {
            "perimeter": tk.StringVar(),
            "execute_import": tk.BooleanVar(),
            "create_structure_if_needed": tk.BooleanVar(),
            "allow_schema_evolution": tk.BooleanVar(),
            "resolve_column_collisions": tk.BooleanVar(value=True),
            "column_collision_strategy": tk.StringVar(
                value="Manter somente a coluna de destino"
            ),
            "keyless_direct_load_max_rows": tk.StringVar(),
            "executor_directory": tk.StringVar(),
            "destination_sql_directory": tk.StringVar(),
            "artifact_reader_sids": tk.StringVar(),
            "artifact_writer_sids": tk.StringVar(),
            "local_control_directory": tk.StringVar(),
            "rows_per_block": tk.StringVar(),
            "max_file_bytes": tk.StringVar(),
            "minimum_free_space_bytes": tk.StringVar(),
            "delete_confirmed_files": tk.BooleanVar(),
            "delete_completed_table_artifacts": tk.BooleanVar(value=True),
            "continue_after_table_error": tk.BooleanVar(),
            "odbc_driver": tk.StringVar(),
            "bcp_executable": tk.StringVar(),
            "connection_timeout_seconds": tk.StringVar(),
            "sql_timeout_seconds": tk.StringVar(),
            "bcp_timeout_seconds": tk.StringVar(),
            "cdc_retention_minutes": tk.StringVar(),
            "control_schema": tk.StringVar(),
            "row_count_method": tk.StringVar(),
            "maximum_sample_rows": tk.StringVar(),
            "safety_factor": tk.StringVar(),
            "require_watermark_index": tk.BooleanVar(),
            "secondary_indexes_phase": tk.StringVar(),
        }
        self.byte_display_variables: dict[str, tk.StringVar] = {
            "max_file_bytes": tk.StringVar(),
            "minimum_free_space_bytes": tk.StringVar(),
        }
        self.cdc_retention_days = tk.StringVar()
        self.endpoint_enabled: dict[str, tk.BooleanVar] = {
            "source": tk.BooleanVar(value=True),
            "bronze_destination": tk.BooleanVar(value=True),
            "landing_destination": tk.BooleanVar(value=True),
        }
        self.endpoint_variables: dict[str, dict[str, tk.Variable]] = {}
        for key in self.endpoint_enabled:
            self.endpoint_variables[key] = {
                "role": tk.StringVar(),
                "instance": tk.StringVar(),
                "port": tk.StringVar(),
                "database": tk.StringVar(),
                "schema": tk.StringVar(),
                "structure_profile": tk.StringVar(),
                "odbc_dsn": tk.StringVar(),
                "authentication_type": tk.StringVar(),
                "username": tk.StringVar(),
                "password": tk.StringVar(),
                "domain": tk.StringVar(),
                "secret_provider": tk.StringVar(),
                "secret_reference": tk.StringVar(),
                "encrypt": tk.BooleanVar(value=True),
                "trust_server_certificate": tk.BooleanVar(value=True),
            }
        self.ddl_area = tk.StringVar(value="Ambas")
        self.ddl_output = tk.StringVar(value=str(RUNTIME_DDL_DIRECTORY))
        self.execution_id = tk.StringVar()
        self.manifest_path = tk.StringVar()
        self.config_path_label = tk.StringVar(value="Configuração ainda não salva")
        self.operation_status = tk.StringVar(value="Pronto")
        self.prerequisite_status = tk.StringVar(value="Ainda não verificado")
        self.plan_status = tk.StringVar(value="Planejamento ainda não executado")
        self.odbc_requirement = tk.StringVar()
        self.bcp_requirement = tk.StringVar()
        self.disk_projection_overview = tk.StringVar(
            value="Projeção de disco: execute Planejar para calcular todas as tabelas."
        )
        self.destination_disk_assessment = tk.StringVar(
            value="Destino 01: aguardando planejamento."
        )
        self.disk_operator_warning = tk.StringVar()
        self.disk_projection_guidance = tk.StringVar()
        self.credential_permissions_guidance = tk.StringVar()
        self.process_flow_description = tk.StringVar()
        self.process_flow_cards = [tk.StringVar() for _ in PROCESS_FLOW_CARD_TITLES]
        self.process_flow_note = tk.StringVar()
        self.process_flow_context = tk.StringVar(
            value="Tabela: configuração | Lote: aguardando | Próxima ação: validar e planejar"
        )
        self.ddl_none_warning = tk.StringVar()
        self.cli_command_proposal = tk.StringVar()
        self.log_follow_status = tk.StringVar(
            value="Acompanhamento automático: ativo"
        )
        self.follow_log_tail = True
        self.verbose_logging = tk.BooleanVar(value=False)
        self.execution_progress_value = tk.DoubleVar(value=0)
        self.execution_progress_percent = tk.StringVar(value="0%")
        self.planning_progress_window: ttk.Toplevel | None = None
        self.planning_progress_value = tk.DoubleVar(value=0)
        self.planning_progress_percent = tk.StringVar(value="0%")
        self.planning_progress_message = tk.StringVar(
            value="Preparando a checagem de planejamento…"
        )

        for name in ("max_file_bytes", "minimum_free_space_bytes"):
            self.global_variables[name].trace_add(
                "write", lambda *_args, field=name: self._update_byte_display(field)
            )
        self.global_variables["cdc_retention_minutes"].trace_add(
            "write", lambda *_args: self._update_cdc_retention_days()
        )
        self.global_variables["odbc_driver"].trace_add(
            "write", lambda *_args: self._update_prerequisite_descriptions()
        )
        self.global_variables["bcp_executable"].trace_add(
            "write", lambda *_args: self._update_prerequisite_descriptions()
        )
        self.global_variables["executor_directory"].trace_add(
            "write", self._on_executor_directory_changed
        )
        self.global_variables["create_structure_if_needed"].trace_add(
            "write", lambda *_args: self._on_create_structure_changed()
        )
        self.global_variables["execute_import"].trace_add(
            "write", lambda *_args: self._on_execute_import_changed()
        )
        for name in (
            "destination_sql_directory",
            "max_file_bytes",
            "minimum_free_space_bytes",
            "safety_factor",
            "delete_confirmed_files",
            "rows_per_block",
            "maximum_sample_rows",
            "row_count_method",
            "keyless_direct_load_max_rows",
        ):
            self.global_variables[name].trace_add(
                "write", lambda *_args: self._on_planning_context_changed()
            )
        self.endpoint_variables["source"]["schema"].trace_add(
            "write", self._on_source_schema_changed
        )
        for endpoint in self.endpoint_variables.values():
            for name in (
                "role",
                "instance",
                "port",
                "database",
                "authentication_type",
                "username",
                "domain",
                "secret_provider",
                "secret_reference",
                "odbc_dsn",
                "schema",
                "structure_profile",
                "encrypt",
                "trust_server_certificate",
            ):
                endpoint[name].trace_add(
                    "write", lambda *_args: self._on_credential_context_changed()
                )
        for key in ("bronze_destination", "landing_destination"):
            self.endpoint_enabled[key].trace_add(
                "write", lambda *_args, endpoint=key: self._on_endpoint_enabled_changed(endpoint)
            )
        self.global_variables["resolve_column_collisions"].trace_add(
            "write", lambda *_args: self._update_collision_strategy_state()
        )
        self.ddl_area.trace_add("write", lambda *_args: self._update_ddl_section_state())
        self.ddl_output.trace_add(
            "write", lambda *_args: self._update_contextual_guidance()
        )

    def _update_ddl_area_default(self) -> None:
        create_variable = getattr(self, "global_variables", {}).get(
            "create_structure_if_needed"
        )
        if create_variable is not None and not bool(create_variable.get()):
            self.ddl_area.set("Destino 02")
            return
        bronze = bool(self.endpoint_enabled["bronze_destination"].get())
        landing = bool(self.endpoint_enabled["landing_destination"].get())
        value = (
            "Ambas"
            if bronze and landing
            else "Destino 01"
            if bronze
            else "Destino 02"
            if landing
            else "Nenhum"
        )
        self.ddl_area.set(value)

    def _on_endpoint_enabled_changed(self, key: str) -> None:
        self._update_endpoint_state(key)
        self._update_ddl_area_choices()
        self._update_ddl_area_default()
        self._update_contextual_guidance()

    def _on_execute_import_changed(self) -> None:
        enabled = bool(self.global_variables["execute_import"].get())
        previous = self._last_execute_import_state
        delete_variable = self.global_variables["delete_confirmed_files"]
        if not enabled and previous is True:
            self._delete_confirmed_files_preference = bool(delete_variable.get())
        elif (
            enabled
            and previous is False
            and self._delete_confirmed_files_preference is not None
        ):
            delete_variable.set(self._delete_confirmed_files_preference)
        self._last_execute_import_state = enabled
        self.endpoint_enabled["bronze_destination"].set(enabled)
        if not enabled:
            delete_variable.set(False)
        delete_widget = getattr(self, "delete_confirmed_files_widget", None)
        if delete_widget is not None:
            delete_widget.configure(state="normal" if enabled else "disabled")
            delete_widget.state(["!disabled"] if enabled else ["disabled"])
        self._update_endpoint_state("bronze_destination")
        self._update_ddl_area_choices()
        self._update_ddl_area_default()
        self._on_planning_context_changed()

    def _on_execute_import_toggled_by_user(self) -> None:
        # Reaplicar explicitamente no callback do controle evita que o estado
        # visual fique defasado em relação ao BooleanVar em alguns ciclos do Tk.
        self._on_execute_import_changed()
        if not bool(self.global_variables["execute_import"].get()):
            database = str(
                self.endpoint_variables["bronze_destination"]["database"].get()
            ).strip() or "<não informado>"
            messagebox.showinfo(
                "Importação desabilitada",
                f"A importação de dados no Destino 01 ({database}) está desabilitada.",
                parent=self.root,
            )

    def _update_ddl_area_choices(self) -> None:
        widget = getattr(self, "ddl_area_widget", None)
        execute_import = bool(self.global_variables["execute_import"].get())
        values = (
            ["Nenhum", "Destino 01", "Destino 02", "Ambas"]
            if execute_import
            else ["Nenhum", "Destino 02"]
        )
        if widget is not None:
            widget.configure(values=values)
        if str(self.ddl_area.get()) not in values:
            self.ddl_area.set(
                "Destino 02"
                if self.endpoint_enabled["landing_destination"].get()
                else "Nenhum"
            )

    def _on_create_structure_changed(self) -> None:
        enabled = bool(self.global_variables["create_structure_if_needed"].get())
        role = "structure_and_data" if enabled else "data_only"
        self.endpoint_variables["bronze_destination"]["role"].set(
            label_for_code(DESTINATION_ROLE_LABELS, role)
        )
        self._update_ddl_area_default()
        self._update_contextual_guidance()

    def _on_planning_context_changed(self) -> None:
        self._invalidate_disk_projection()
        self._update_contextual_guidance()

    @staticmethod
    def _same_display_path(first: str, second: str) -> bool:
        def normalized(value: str) -> str:
            return value.strip().replace("/", "\\").rstrip("\\").casefold()

        return bool(normalized(first)) and normalized(first) == normalized(second)

    @staticmethod
    def _same_server_login(
        first_instance: str,
        first_username: str,
        second_instance: str,
        second_username: str,
    ) -> bool:
        values = tuple(
            str(value).strip().casefold()
            for value in (
                first_instance,
                first_username,
                second_instance,
                second_username,
            )
        )
        return all(values) and values[0] == values[2] and values[1] == values[3]

    def _update_contextual_guidance(self) -> None:
        def endpoint_value(key: str, field: str) -> str:
            value = str(self.endpoint_variables[key][field].get()).strip()
            return value or "<não informado>"

        source_database = endpoint_value("source", "database")
        destination_1_database = endpoint_value("bronze_destination", "database")
        destination_2_database = endpoint_value("landing_destination", "database")
        export_directory = str(self.global_variables["executor_directory"].get()).strip() or "<não informado>"
        import_directory = str(self.global_variables["destination_sql_directory"].get()).strip() or "<não informado>"

        enabled_destination_databases = [
            database
            for key, database in (
                ("bronze_destination", destination_1_database),
                ("landing_destination", destination_2_database),
            )
            if bool(self.endpoint_enabled[key].get())
        ]
        flow_options = {
            "source_database": source_database,
            "destination_databases": enabled_destination_databases,
            "export_directory": export_directory,
            "import_directory": import_directory,
            "create_structure": bool(
                self.global_variables["create_structure_if_needed"].get()
            ),
            "execute_import": bool(self.global_variables["execute_import"].get()),
            "delete_confirmed_files": bool(
                self.global_variables["delete_confirmed_files"].get()
            ),
        }
        self.process_flow_description.set(build_process_flow_description(**flow_options))
        flow_cards, flow_note = build_process_flow_cards(**flow_options)
        for variable, text in zip(self.process_flow_cards, flow_cards, strict=True):
            variable.set(text)
        self.process_flow_note.set(flow_note)
        mode = "exportação e importação" if flow_options["execute_import"] else "somente exportação"
        self.process_flow_context.set(
            "Tabela: cada tabela configurada   |   Lote: um por vez   |   "
            f"Próxima ação: validar e planejar ({mode})"
        )
        self._update_process_flow_card_styles(**flow_options)
        ddl_areas = [
            area
            for key, area in (
                ("bronze_destination", "bronze"),
                ("landing_destination", "landing"),
            )
            if bool(self.endpoint_enabled[key].get())
            and DESTINATION_ROLE_LABELS.get(
                str(self.endpoint_variables[key]["role"].get())
            )
            in {"structure_and_data", "structure_only"}
        ]
        cli_ddl_area = (
            "both"
            if len(ddl_areas) == 2
            else ddl_areas[0]
            if ddl_areas
            else None
        )
        prompt_credentials: list[tuple[str, str, str]] = []
        credential_groups: dict[str, list[tuple[str, str]]] = {}
        for endpoint_name in (
            "source",
            "bronze_destination",
            "landing_destination",
        ):
            if endpoint_name != "source" and not bool(
                self.endpoint_enabled[endpoint_name].get()
            ):
                continue
            variables = self.endpoint_variables[endpoint_name]
            authentication_type = AUTHENTICATION_LABELS.get(
                str(variables["authentication_type"].get()), ""
            )
            secret_provider = SECRET_PROVIDER_LABELS.get(
                str(variables["secret_provider"].get()), ""
            )
            environment_name = prompt_secret_environment_variable(endpoint_name)
            if (
                authentication_type not in {"sql", "windows_credentials"}
                or secret_provider != "prompt"
                or environment_name is None
            ):
                continue
            username = str(variables["username"].get()).strip()
            identity = "|".join(
                (
                    str(variables["instance"].get()).strip().casefold(),
                    str(variables["port"].get()).strip(),
                    authentication_type,
                    str(variables["domain"].get()).strip().casefold(),
                    username.casefold(),
                )
            )
            credential_groups.setdefault(identity, []).append(
                (environment_name, CONNECTION_TAB_TITLES[endpoint_name])
            )
        for identity, entries in credential_groups.items():
            endpoint_names = " / ".join(label for _environment, label in entries)
            username = identity.rsplit("|", 1)[-1] or "usuário configurado"
            prompt_label = f"Senha de {username} para {endpoint_names}"
            prompt_credentials.extend(
                (environment_name, prompt_label, identity)
                for environment_name, _label in entries
            )
        self.cli_command_proposal.set(
            build_cli_workflow_command(
                self.current_config_path,
                execute_import=bool(self.global_variables["execute_import"].get()),
                ddl_area=cli_ddl_area,
                ddl_output=self.ddl_output.get(),
                prompt_credentials=prompt_credentials,
                delete_config_on_success=bool(
                    (self.base_config.get("execution_package") or {}).get(
                        "delete_on_success"
                    )
                ),
            )
        )

        self.disk_projection_guidance.set(
            build_disk_projection_guidance(
                destination_instance=endpoint_value(
                    "bronze_destination", "instance"
                ),
                export_directory=export_directory,
                import_directory=import_directory,
                delete_confirmed_files=bool(
                    self.global_variables["delete_confirmed_files"].get()
                ),
                max_file_bytes=self.global_variables["max_file_bytes"].get(),
            )
        )

        permission_lines = [
            "É premissa ter o usuário criado nas instâncias conforme permissionamento abaixo:",
            "",
        ]
        destination_2_permissions = "leitura e escrita; ddl"
        if self._same_server_login(
            self.endpoint_variables["bronze_destination"]["instance"].get(),
            self.endpoint_variables["bronze_destination"]["username"].get(),
            self.endpoint_variables["landing_destination"]["instance"].get(),
            self.endpoint_variables["landing_destination"]["username"].get(),
        ):
            destination_2_permissions += "; função de servidor: bulkadmin"
        for title, key, permissions in (
            ("ORIGEM", "source", "somente leitura"),
            (
                "DESTINO 01",
                "bronze_destination",
                "leitura e escrita; ddl; função de servidor: bulkadmin",
            ),
            (
                "DESTINO 02",
                "landing_destination",
                destination_2_permissions,
            ),
        ):
            permission_lines.append(
                f"- {title} | Instância: {endpoint_value(key, 'instance')} | "
                f"Banco: {endpoint_value(key, 'database')} | Usuário: {endpoint_value(key, 'username')} | "
                f"Permissões necessárias: {permissions}"
            )
        self.credential_permissions_guidance.set("\n".join(permission_lines))

        ddl_warnings: list[str] = []
        if not bool(self.global_variables["execute_import"].get()):
            ddl_warnings.append(
                f"Cenário configurado não cria tabelas em {destination_1_database}."
            )
        if str(self.ddl_area.get()) == "Nenhum":
            inactive_databases = [destination_2_database]
            if bool(self.global_variables["execute_import"].get()):
                inactive_databases.insert(0, destination_1_database)
            ddl_warnings.append(
                "Cenário configurado não cria tabelas em "
                + " e ".join(inactive_databases)
                + "."
            )
        self.ddl_none_warning.set("\n".join(ddl_warnings))

    def _update_process_flow_card_styles(self, **options: Any) -> None:
        headers = getattr(self, "process_flow_card_headers", [])
        if len(headers) != len(PROCESS_FLOW_CARD_TITLES):
            return
        structure_active = bool(
            options.get("create_structure") and options.get("destination_databases")
        )
        import_active = bool(options.get("execute_import"))
        delete_files = bool(options.get("delete_confirmed_files"))
        styles = (
            "primary",
            "info",
            "primary" if structure_active else "secondary",
            "success",
            "warning" if import_active else "secondary",
            "danger" if delete_files and import_active else "info",
            "primary",
        )
        for header, style in zip(headers, styles, strict=True):
            header.configure(bootstyle=f"{style}-inverse")

    def _update_ddl_section_state(self) -> None:
        inactive = str(self.ddl_area.get()) == "Nenhum"
        for widget in getattr(self, "ddl_non_area_controls", []):
            widget.configure(state="disabled" if inactive else "normal")
        self._update_collision_strategy_state()
        self._update_contextual_guidance()

    def _update_collision_strategy_state(self) -> None:
        widget = getattr(self, "column_collision_strategy_widget", None)
        if widget is not None:
            ddl_area = getattr(self, "ddl_area", None)
            area_is_active = ddl_area is None or str(ddl_area.get()) != "Nenhum"
            widget.configure(
                state=(
                    "readonly"
                    if self.global_variables["resolve_column_collisions"].get()
                    and area_is_active
                    else "disabled"
                )
            )

    def _build_window(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(12, 10))
        toolbar.pack(fill="x")
        toolbar_header = ttk.Frame(toolbar)
        toolbar_header.pack(fill="x")
        toolbar_actions = ttk.Frame(toolbar)
        toolbar_actions.pack(fill="x", pady=(8, 0))
        ttk.Label(
            toolbar_header, text=PRODUCT_NAME, font=("Segoe UI", 16, "bold")
        ).pack(side="left", padx=(0, 18))
        ttk.Button(
            toolbar_actions,
            text="Nova",
            command=self._new_configuration,
            bootstyle="secondary",
        ).pack(side="left", padx=3)
        ttk.Button(
            toolbar_actions,
            text="Abrir…",
            command=self._open_configuration,
            bootstyle="secondary",
        ).pack(side="left", padx=3)
        ttk.Button(
            toolbar_actions,
            text="Salvar para GUI/CLI",
            command=self._save_configuration,
            bootstyle="primary",
        ).pack(
            side="left", padx=3
        )
        ttk.Button(
            toolbar_actions,
            text="Salvar como…",
            command=lambda: self._save_configuration(save_as=True),
            bootstyle="outline-primary",
        ).pack(side="left", padx=3)
        ttk.Button(
            toolbar_actions,
            text="Validar configuração",
            command=self._validate_button,
            bootstyle="outline-success",
        ).pack(side="left", padx=(16, 3))
        ttk.Label(
            toolbar_header,
            textvariable=self.config_path_label,
            bootstyle="secondary",
        ).pack(side="right", padx=8)

        self.notebook = ttk.Notebook(self.root, padding=(10, 0, 10, 8))
        self.notebook.pack(fill="both", expand=True)
        self._scrollable_tabs: list[_ScrollableTab] = []

        def scrollable_page() -> tuple[_ScrollableTab, ttk.Frame]:
            page = _ScrollableTab(self.notebook)
            self._scrollable_tabs.append(page)
            return page, page.content

        self.process_flow_page, self.process_flow_tab = scrollable_page()
        self.general_page, self.general_tab = scrollable_page()
        self.connections_page, self.connections_tab = scrollable_page()
        self.tables_page, self.tables_tab = scrollable_page()
        self.operations_page, self.operations_tab = scrollable_page()
        self.tracking_page, self.tracking_tab = scrollable_page()
        self.notebook.add(self.process_flow_page, text="1. Fluxo Processo")
        self.notebook.add(self.general_page, text="2. Geral")
        self.notebook.add(self.connections_page, text="3. Conexões")
        self.notebook.add(self.tables_page, text="4. Tabelas")
        self.notebook.add(self.operations_page, text="5. Executar")
        self.notebook.add(self.tracking_page, text="6. Acompanhamento")
        self.root.bind_all("<MouseWheel>", self._scroll_active_tab, add="+")
        self.root.bind_all("<Button-4>", self._scroll_active_tab, add="+")
        self.root.bind_all("<Button-5>", self._scroll_active_tab, add="+")
        self._build_process_flow_tab()
        self._build_general_tab()
        self._build_connections_tab()
        self._build_tables_tab()
        self._build_operations_tab()
        self._build_tracking_tab()

        footer = ttk.Frame(self.root, padding=(12, 4, 12, 10))
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(
            footer,
            mode="determinate",
            maximum=100,
            variable=self.execution_progress_value,
            length=240,
            bootstyle="success-striped",
        )
        self.progress.pack(side="left")
        ttk.Label(
            footer,
            textvariable=self.execution_progress_percent,
            width=6,
            anchor="e",
        ).pack(side="left", padx=(8, 0))
        ttk.Label(footer, textvariable=self.operation_status).pack(side="left", padx=10)

    def _scroll_active_tab(self, event: tk.Event) -> str | None:
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview)):
            return None
        selected = self.notebook.select()
        for page in self._scrollable_tabs:
            if str(page) != selected or not page.contains(widget):
                continue
            if getattr(event, "num", None) == 4:
                units = -3
            elif getattr(event, "num", None) == 5:
                units = 3
            else:
                delta = int(getattr(event, "delta", 0))
                if delta == 0:
                    return None
                magnitude = max(1, abs(delta) // 120)
                units = -magnitude if delta > 0 else magnitude
            page.scroll_vertical(units)
            return "break"
        return None

    def _build_process_flow_tab(self) -> None:
        tab = self.process_flow_tab
        tab.columnconfigure(0, weight=1)
        flow_diagram = ttk.Frame(tab, padding=12, relief="solid", borderwidth=1)
        flow_diagram.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            flow_diagram,
            textvariable=self.process_flow_context,
            bootstyle="primary-inverse",
            anchor="center",
            padding=(12, 8),
            font=("Segoe UI Semibold", 10),
        ).grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 10))
        ttk.Label(
            flow_diagram,
            text="PIPELINE POR TABELA",
            bootstyle="secondary",
            font=("Segoe UI Semibold", 9),
        ).grid(row=1, column=0, columnspan=5, pady=(0, 5))
        for column in (0, 2, 4):
            flow_diagram.columnconfigure(column, weight=1, uniform="flow-card")
        card_positions = ((2, 0), (2, 2), (2, 4), (4, 4), (4, 2), (4, 0))
        card_styles = ("primary", "info", "primary", "success", "warning", "info")
        self.process_flow_card_headers: list[ttk.Label] = []
        for index, ((row, column), style) in enumerate(
            zip(card_positions, card_styles, strict=True)
        ):
            card = ttk.Frame(
                flow_diagram,
                padding=0,
                relief="solid",
                borderwidth=1,
            )
            card.grid(row=row, column=column, sticky="nsew", padx=5, pady=4)
            header = ttk.Label(
                card,
                text=PROCESS_FLOW_CARD_TITLES[index],
                bootstyle=f"{style}-inverse",
                anchor="w",
                padding=(10, 6),
                font=("Segoe UI Semibold", 9),
            )
            header.pack(fill="x")
            self.process_flow_card_headers.append(header)
            body = ttk.Label(
                card,
                textvariable=self.process_flow_cards[index],
                justify="left",
                anchor="nw",
                wraplength=290,
                padding=(10, 9),
            )
            body.pack(fill="both", expand=True)
            tooltip = ToolTip(
                body, text=self.process_flow_cards[index].get(), wraplength=650
            )
            body.bind(
                "<Enter>",
                lambda _event, tip=tooltip, variable=self.process_flow_cards[index]: setattr(
                    tip, "text", variable.get()
                ),
                add="+",
            )
        for row, column, arrow in (
            (2, 1, "→"),
            (2, 3, "→"),
            (3, 4, "↓"),
            (4, 3, "←"),
            (4, 1, "←"),
            (5, 0, "↓"),
        ):
            ttk.Label(
                flow_diagram,
                text=arrow,
                bootstyle="primary",
                font=("Segoe UI Semibold", 18),
            ).grid(row=row, column=column, padx=3, pady=1)
        decision = ttk.Frame(
            flow_diagram,
            padding=0,
            relief="solid",
            borderwidth=1,
        )
        decision.grid(row=6, column=0, columnspan=5, sticky="ew", padx=5, pady=4)
        decision_header = ttk.Label(
            decision,
            text=PROCESS_FLOW_CARD_TITLES[6],
            bootstyle="primary-inverse",
            anchor="center",
            padding=(10, 6),
            font=("Segoe UI Semibold", 9),
        )
        decision_header.pack(fill="x")
        self.process_flow_card_headers.append(decision_header)
        decision_body = ttk.Label(
            decision,
            textvariable=self.process_flow_cards[6],
            justify="center",
            anchor="center",
            wraplength=1000,
            padding=(10, 8),
        )
        decision_body.pack(fill="x")
        ToolTip(
            decision_body,
            text=(
                "Sim: retorna ao planejamento do próximo lote. "
                "Não: avança para a próxima tabela."
            ),
        )
        ttk.Label(
            flow_diagram,
            textvariable=self.process_flow_note,
            justify="left",
            anchor="w",
            wraplength=1120,
            bootstyle="secondary",
        ).grid(row=7, column=0, columnspan=5, sticky="ew", padx=5, pady=(8, 4))
        legend = ttk.Frame(flow_diagram)
        legend.grid(row=8, column=0, columnspan=5, sticky="ew", padx=5, pady=(5, 0))
        ttk.Label(legend, text="LEGENDA", font=("Segoe UI Semibold", 9)).pack(
            side="left", padx=(0, 10)
        )
        for text, style in (
            ("Etapa obrigatória", "primary-inverse"),
            ("Processamento de dados", "success-inverse"),
            ("Etapa condicional", "warning-inverse"),
            ("Etapa não executada", "secondary-inverse"),
            ("Ponto de retomada", "info-inverse"),
        ):
            ttk.Label(legend, text=text, bootstyle=style, padding=(7, 3)).pack(
                side="left", padx=3
            )
        self._update_contextual_guidance()

    def _build_general_tab(self) -> None:
        tab = self.general_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        flow = ttk.Labelframe(tab, text="Fluxo", padding=12)
        flow.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 10))
        storage = ttk.Labelframe(tab, text="Arquivos e limites", padding=12)
        storage.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 10))
        advanced = ttk.Labelframe(tab, text="Planejamento e ferramentas", padding=12)
        advanced.grid(row=1, column=0, columnspan=2, sticky="nsew")
        flow.columnconfigure(1, weight=1)
        storage.columnconfigure(1, weight=1)
        advanced.columnconfigure(1, weight=1)
        advanced.columnconfigure(3, weight=1)

        row = 0
        _help_label(flow, "Perímetro *", "perimeter").grid(
            row=row, column=0, sticky="w", pady=5
        )
        perimeter = ttk.Combobox(
            flow,
            textvariable=self.global_variables["perimeter"],
            values=PERIMETER_VALUES,
            state="readonly",
        )
        perimeter.grid(row=row, column=1, sticky="ew", pady=5)
        _attach_help(perimeter, "perimeter")
        self._register_default_widget(
            perimeter,
            self.global_variables["perimeter"],
            "DESENVOLVIMENTO",
            "combobox",
        )
        perimeter.bind("<<ComboboxSelected>>", self._on_perimeter_selected)
        row += 1
        ttk.Label(flow, text="Fluxo de dados").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Label(
            flow,
            text="Origem → destino com função de dados; os demais recebem somente as operações permitidas por sua função",
            bootstyle="secondary",
            wraplength=360,
        ).grid(row=row, column=1, sticky="w", pady=5)
        row += 1
        for name, label in (
            ("execute_import", "Importar no destino"),
            ("create_structure_if_needed", "Criar estrutura se necessário"),
            ("allow_schema_evolution", "Permitir evolução aditiva de esquema"),
            (
                "delete_confirmed_files",
                "Apagar arquivos exportados após confirmação",
            ),
            (
                "delete_completed_table_artifacts",
                "Excluir artefatos auxiliares após concluir cada tabela",
            ),
            ("continue_after_table_error", "Continuar após erro de tabela"),
            ("require_watermark_index", "Exigir índice compatível com a marca d'água"),
        ):
            container = ttk.Frame(flow)
            container.grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
            control = ttk.Checkbutton(
                container,
                text=label,
                variable=self.global_variables[name],
                command=(
                    self._on_execute_import_toggled_by_user
                    if name == "execute_import"
                    else None
                ),
                bootstyle="round-toggle",
            )
            control.pack(side="left")
            if name == "execute_import":
                self.execute_import_widget = control
            if name == "delete_confirmed_files":
                self.delete_confirmed_files_widget = control
            _attach_help(control, name)
            help_marker = ttk.Label(container, text=" ? ", bootstyle="info", cursor="hand2")
            help_marker.pack(side="left", padx=(3, 0))
            _attach_help(help_marker, name)
            row += 1
        self._on_execute_import_changed()

        row = 0
        row = self._form_entry(
            storage,
            row,
            "Diretório de exportação dos arquivos *",
            self.global_variables["executor_directory"],
            browse="directory",
            help_key="executor_directory",
            entry_width=58,
            entry_help=False,
            value_tooltip=True,
        )
        row = self._form_entry(
            storage,
            row,
            "Diretório de importação dos arquivos *",
            self.global_variables["destination_sql_directory"],
            browse="directory",
            help_key="destination_sql_directory",
            entry_width=58,
            entry_help=False,
            value_tooltip=True,
        )
        row = self._form_entry(
            storage,
            row,
            "SIDs leitores dos artefatos (Windows; opcional)",
            self.global_variables["artifact_reader_sids"],
            help_key="artifact_reader_sids",
            default_value="",
            highlight_empty_default=True,
        )
        row = self._form_entry(
            storage,
            row,
            "SIDs escritores SMB dos artefatos (Windows; opcional)",
            self.global_variables["artifact_writer_sids"],
            help_key="artifact_writer_sids",
            default_value="",
            highlight_empty_default=True,
        )
        row = self._form_entry(
            storage,
            row,
            "Diretório de controle local *",
            self.global_variables["local_control_directory"],
            browse="directory",
            help_key="local_control_directory",
            default_value=str(RUNTIME_DATA_ROOT / ".bcp-control"),
            entry_width=58,
            entry_help=False,
            value_tooltip=True,
        )
        row = self._form_entry(
            storage,
            row,
            "Linhas por bloco *",
            self.global_variables["rows_per_block"],
            help_key="rows_per_block",
            default_value="200000",
        )
        row = self._form_entry(
            storage,
            row,
            "Limite para tabela sem chave *",
            self.global_variables["keyless_direct_load_max_rows"],
            help_key="keyless_direct_load_max_rows",
            default_value="5000000",
        )
        row = self._form_entry(
            storage,
            row,
            "Arquivo máximo em bytes *",
            self.global_variables["max_file_bytes"],
            help_key="max_file_bytes",
            default_value="157286400",
            companion=self.byte_display_variables["max_file_bytes"],
        )
        self._form_entry(
            storage,
            row,
            "Espaço livre mínimo em bytes *",
            self.global_variables["minimum_free_space_bytes"],
            help_key="minimum_free_space_bytes",
            default_value="10737418240",
            companion=self.byte_display_variables["minimum_free_space_bytes"],
        )

        _help_label(advanced, "Contagem de linhas *", "row_count_method").grid(
            row=0, column=0, sticky="w", pady=5
        )
        row_count = ttk.Combobox(
            advanced,
            textvariable=self.global_variables["row_count_method"],
            values=list(ROW_COUNT_LABELS),
            state="readonly",
        )
        row_count.grid(row=0, column=1, sticky="ew", padx=(8, 20), pady=5)
        _attach_help(row_count, "row_count_method")
        self._register_default_widget(
            row_count,
            self.global_variables["row_count_method"],
            "Metadados (aproximado)",
            "combobox",
        )
        _help_label(advanced, "Amostra máxima *", "maximum_sample_rows").grid(
            row=0, column=2, sticky="w", pady=5
        )
        sample = ttk.Entry(advanced, textvariable=self.global_variables["maximum_sample_rows"])
        sample.grid(
            row=0, column=3, sticky="ew", padx=(8, 0), pady=5
        )
        _attach_help(sample, "maximum_sample_rows")
        self._register_default_widget(
            sample, self.global_variables["maximum_sample_rows"], "10000", "entry"
        )
        _help_label(advanced, "Fator de segurança *", "safety_factor").grid(
            row=1, column=0, sticky="w", pady=5
        )
        safety = ttk.Entry(advanced, textvariable=self.global_variables["safety_factor"])
        safety.grid(
            row=1, column=1, sticky="ew", padx=(8, 20), pady=5
        )
        _attach_help(safety, "safety_factor")
        self._register_default_widget(
            safety, self.global_variables["safety_factor"], "1.25", "entry"
        )
        _help_label(advanced, "Índices secundários *", "secondary_indexes_phase").grid(
            row=1, column=2, sticky="w", pady=5
        )
        index_phase = ttk.Combobox(
            advanced,
            textvariable=self.global_variables["secondary_indexes_phase"],
            values=list(INDEX_PHASE_LABELS),
            state="readonly",
        )
        index_phase.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=5)
        _attach_help(index_phase, "secondary_indexes_phase")
        self._register_default_widget(
            index_phase,
            self.global_variables["secondary_indexes_phase"],
            "Antes da carga",
            "combobox",
        )
        fields = (
            ("Retenção do CDC (minutos) *", "cdc_retention_minutes", None),
            ("Driver ODBC *", "odbc_driver", "ODBC Driver 18 for SQL Server"),
            ("Executável BCP *", "bcp_executable", "bcp"),
            ("Timeout de conexão (s) *", "connection_timeout_seconds", "30"),
            ("Timeout SQL (s; 0 sem limite) *", "sql_timeout_seconds", "0"),
            ("Timeout BCP (s; 0 sem limite) *", "bcp_timeout_seconds", "0"),
            ("Esquema de controle (fixo)", "control_schema", "dbo"),
        )
        for index, (label, name, default) in enumerate(fields, start=2):
            column = 0 if index % 2 == 0 else 2
            row = 2 + (index - 2) // 2
            _help_label(advanced, label, name).grid(
                row=row, column=column, sticky="w", pady=5
            )
            entry_parent = advanced
            if name == "cdc_retention_minutes":
                entry_parent = ttk.Frame(advanced)
                entry_parent.grid(
                    row=row,
                    column=column + 1,
                    sticky="ew",
                    padx=(8, 20 if column == 0 else 0),
                    pady=5,
                )
                entry_parent.columnconfigure(0, weight=1)
            entry = ttk.Entry(entry_parent, textvariable=self.global_variables[name])
            if name == "cdc_retention_minutes":
                entry.grid(row=0, column=0, sticky="ew")
                days_display = ttk.Entry(
                    entry_parent,
                    textvariable=self.cdc_retention_days,
                    state="readonly",
                    width=18,
                    bootstyle="secondary",
                )
                days_display.grid(row=0, column=1, sticky="ew", padx=(6, 0))
                _attach_help(days_display, name)
            else:
                entry.grid(
                    row=row,
                    column=column + 1,
                    sticky="ew",
                    padx=(8, 20 if column == 0 else 0),
                    pady=5,
                )
            _attach_help(entry, name)
            if name == "control_schema":
                # ``dbo`` is a fixed product default (and not one of the
                # operator-review fields that must stay white).  Keep the
                # default-value colour even though editing is intentionally
                # disabled.
                self._register_default_widget(
                    entry, self.global_variables[name], "dbo", "entry"
                )
                entry.configure(state="disabled")
                self._control_schema_entry = entry
            elif name == "cdc_retention_minutes":
                self._register_default_widget(
                    entry, self.global_variables[name], "262800", "entry"
                )
            elif default is not None:
                self._register_default_widget(
                    entry, self.global_variables[name], default, "entry"
                )
        ttk.Label(
            tab,
            text=(
                "* campo obrigatório    |    "
                "Fundo azul-claro: valor padrão editável"
            ),
            bootstyle="secondary",
        ).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self._update_contextual_guidance()

    def _form_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.Variable,
        *,
        browse: str | None = None,
        help_key: str,
        default_value: Any | None = None,
        companion: tk.StringVar | None = None,
        highlight_empty_default: bool = False,
        entry_width: int | None = None,
        entry_help: bool = True,
        value_tooltip: bool = False,
    ) -> int:
        _help_label(parent, label, help_key).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable, width=entry_width)
        entry.grid(
            row=row, column=1, sticky="ew", padx=(8, 0), pady=5
        )
        if entry_help:
            _attach_help(entry, help_key)
        if value_tooltip:
            tooltip = ToolTip(entry, text=str(variable.get()), wraplength=700, delay=250)
            entry.bind(
                "<Enter>",
                lambda _event, tip=tooltip, value=variable: setattr(
                    tip, "text", str(value.get()) or "<não informado>"
                ),
                add="+",
            )
            setattr(entry, "_value_tooltip", tooltip)
        if default_value is not None:
            self._register_default_widget(
                entry,
                variable,
                default_value,
                "entry",
                highlight_empty_default=highlight_empty_default,
            )
        if browse == "directory":
            ttk.Button(
                parent,
                text="…",
                width=3,
                command=lambda: self._select_directory(variable),
                bootstyle="outline-secondary",
            ).grid(row=row, column=2, padx=(6, 0), pady=5)
        elif companion is not None:
            display = ttk.Entry(
                parent,
                textvariable=companion,
                state="readonly",
                width=25,
                bootstyle="secondary",
            )
            display.grid(row=row, column=2, sticky="ew", padx=(6, 0), pady=5)
            _attach_help(display, help_key)
        return row + 1

    @staticmethod
    def _comparable_value(value: Any) -> str:
        return str(value).strip().replace(",", ".").casefold()

    def _current_username_default(self) -> str:
        perimeter = str(self.global_variables["perimeter"].get())
        if perimeter not in PERIMETER_VALUES:
            return ""
        return update_default_username_for_perimeter(perimeter, "", None)

    def _register_default_widget(
        self,
        widget: tk.Misc,
        variable: tk.Variable,
        default_value: Any | Callable[[], Any],
        widget_kind: str,
        *,
        highlight_empty_default: bool = False,
    ) -> None:
        record = (
            widget,
            variable,
            default_value,
            widget_kind,
            highlight_empty_default,
        )
        self._default_widgets.append(record)
        variable.trace_add("write", lambda *_args, item=record: self._apply_default_style(item))
        self._apply_default_style(record)

    def _apply_default_style(
        self,
        record: tuple[tk.Misc, tk.Variable, Any | Callable[[], Any], str, bool],
    ) -> None:
        widget, variable, default_value, widget_kind, highlight_empty_default = record
        expected = default_value() if callable(default_value) else default_value
        expected_value = self._comparable_value(expected)
        is_default = (highlight_empty_default or bool(expected_value)) and (
            self._comparable_value(variable.get()) == expected_value
        )
        style = (
            f"DefaultValue.T{widget_kind.title()}"
            if is_default
            else f"T{widget_kind.title()}"
        )
        try:
            widget.configure(style=style)
        except tk.TclError:
            pass

    def _update_byte_display(self, field: str) -> None:
        value = self.global_variables[field].get()
        self.byte_display_variables[field].set(format_bytes_summary(str(value)))

    def _update_cdc_retention_days(self) -> None:
        value = self.global_variables["cdc_retention_minutes"].get()
        self.cdc_retention_days.set(format_minutes_as_days(value))

    def _select_directory(self, variable: tk.Variable) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Selecionar diretório")
        if selected:
            variable.set(selected)

    def _select_ddl_output_directory(self) -> None:
        current = Path(str(self.ddl_output.get())).expanduser()
        initial = current if current.is_dir() else RUNTIME_DDL_DIRECTORY
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Selecionar diretório para os scripts DDL",
            initialdir=str(initial.resolve()),
            mustexist=False,
        )
        if selected:
            self.ddl_output.set(str(Path(selected).resolve()))

    def _build_connections_tab(self) -> None:
        notebook = ttk.Notebook(self.connections_tab)
        notebook.pack(fill="both", expand=True)
        self._connections_notebook = notebook
        for key, source in (
            ("source", True),
            ("bronze_destination", False),
            ("landing_destination", False),
        ):
            frame = ttk.Frame(notebook, padding=14)
            notebook.add(frame, text=CONNECTION_TAB_TITLES[key])
            self._build_endpoint_frame(frame, key=key, source=source)

    def _build_endpoint_frame(self, frame: ttk.Frame, *, key: str, source: bool) -> None:
        variables = self.endpoint_variables[key]
        self._endpoint_controls[key] = []
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        row = 0
        if not source:
            enabled_container = ttk.Frame(frame)
            enabled_container.grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(0, 12)
            )
            enabled = ttk.Checkbutton(
                enabled_container,
                text="Incluir este destino na configuração",
                variable=self.endpoint_enabled[key],
                bootstyle="round-toggle",
            )
            enabled.pack(side="left")
            self._endpoint_enabled_widgets[key] = enabled
            if key == "bronze_destination":
                enabled.configure(state="disabled")
            _attach_help(enabled, "endpoint_enabled")
            enabled_help = ttk.Label(
                enabled_container, text=" ? ", bootstyle="info", cursor="hand2"
            )
            enabled_help.pack(side="left", padx=(3, 0))
            _attach_help(enabled_help, "endpoint_enabled")
            row += 1

        role_labels = SOURCE_ROLE_LABELS if source else DESTINATION_ROLE_LABELS
        default_role = DEFAULT_ENDPOINT_ROLES[key]
        default_role_label = label_for_code(role_labels, default_role)
        _help_label(frame, "Função *", "role").grid(
            row=row, column=0, sticky="w", pady=6
        )
        role = ttk.Combobox(
            frame,
            textvariable=variables["role"],
            values=list(role_labels),
            state="readonly",
        )
        role.grid(
            row=row,
            column=1,
            sticky="ew",
            padx=(8, 20),
            pady=6,
        )
        _attach_help(role, "role")
        self._role_comboboxes[key] = role
        self._endpoint_controls[key].append((role, "readonly"))
        self._register_default_widget(
            role,
            variables["role"],
            default_role_label,
            "combobox",
        )
        row += 1

        defaults = {
            "source": {
                "instance": "SERVIDOR_ORIGEM",
                "port": "1433",
                "database": "BANCO_ORIGEM",
                "schema": "dbo",
            },
            "bronze_destination": {
                "instance": "SERVIDOR_BRONZE",
                "port": "1433",
                "database": "DBRO684",
                "structure_profile": "bronze",
            },
            "landing_destination": {
                "instance": "SERVIDOR_LANDING",
                "port": "1433",
                "database": "DLAN684",
                "structure_profile": "landing_without_aud",
            },
        }[key]

        fields: list[tuple[str, str, int, int]] = [
            ("Instância *", "instance", row, 0),
            ("Porta *", "port", row, 2),
            ("Banco de dados *", "database", row + 1, 0),
            ("Esquema *", "schema", row + 1, 2),
            ("DSN ODBC (opcional)", "odbc_dsn", row + 2, 0),
        ]
        if not source:
            fields.append(("Perfil de estrutura *", "structure_profile", row + 2, 2))
        for label, name, current_row, column in fields:
            _help_label(frame, label, name).grid(
                row=current_row, column=column, sticky="w", pady=6
            )
            if name == "structure_profile":
                profile_container = ttk.Frame(frame)
                profile_container.grid(
                    row=current_row,
                    column=column + 1,
                    sticky="ew",
                    padx=(8, 20 if column == 0 else 0),
                    pady=6,
                )
                profile_container.columnconfigure(0, weight=1)
                entry = ttk.Combobox(
                    profile_container,
                    textvariable=variables[name],
                    values=self._structure_profile_choices[key],
                    state="readonly",
                )
                entry.grid(row=0, column=0, sticky="ew")
                load_profile_button = ttk.Button(
                    profile_container,
                    text="...",
                    width=4,
                    bootstyle="secondary-outline",
                    command=lambda endpoint=key: self._select_structure_profile(
                        endpoint
                    ),
                )
                load_profile_button.grid(row=0, column=1, padx=(6, 0))
                _attach_help(load_profile_button, "structure_profile_import")
                self._structure_profile_comboboxes[key] = entry
                self._endpoint_controls[key].append((entry, "readonly"))
                self._endpoint_controls[key].append((load_profile_button, "normal"))
            else:
                entry = ttk.Entry(frame, textvariable=variables[name])
                entry.grid(
                    row=current_row,
                    column=column + 1,
                    sticky="ew",
                    padx=(8, 20 if column == 0 else 0),
                    pady=6,
                )
                self._endpoint_controls[key].append((entry, "normal"))
            _attach_help(entry, name)
            if name == "odbc_dsn":
                self._register_default_widget(
                    entry,
                    variables[name],
                    "",
                    "entry",
                    highlight_empty_default=True,
                )
            elif name == "structure_profile" and name in defaults:
                self._register_default_widget(
                    entry, variables[name], defaults[name], "combobox"
                )
        row += 3

        auth = ttk.Labelframe(frame, text="Autenticação", padding=12)
        auth.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(16, 8))
        auth.columnconfigure(1, weight=1)
        auth.columnconfigure(3, weight=1)
        _help_label(auth, "Tipo *", "authentication_type").grid(
            row=0, column=0, sticky="w", pady=5
        )
        auth_type = ttk.Combobox(
            auth,
            textvariable=variables["authentication_type"],
            values=list(AUTHENTICATION_LABELS),
            state="readonly",
        )
        auth_type.grid(row=0, column=1, sticky="ew", padx=(8, 20), pady=5)
        self._endpoint_controls[key].append((auth_type, "readonly"))
        _attach_help(auth_type, "authentication_type")
        self._register_default_widget(
            auth_type, variables["authentication_type"], "SQL Server", "combobox"
        )
        auth_type.bind(
            "<<ComboboxSelected>>", lambda _event, endpoint=key: self._update_auth_state(endpoint)
        )
        _help_label(auth, "Usuário (quando aplicável)", "username").grid(
            row=0, column=2, sticky="w", pady=5
        )
        username = ttk.Entry(auth, textvariable=variables["username"])
        username.grid(
            row=0, column=3, sticky="ew", padx=(8, 0), pady=5
        )
        self._endpoint_controls[key].append((username, "normal"))
        _attach_help(username, "username")
        _help_label(auth, "Domínio (credencial Windows)", "domain").grid(
            row=1, column=0, sticky="w", pady=5
        )
        domain = ttk.Entry(auth, textvariable=variables["domain"])
        domain.grid(
            row=1, column=1, sticky="ew", padx=(8, 20), pady=5
        )
        _attach_help(domain, "domain")
        self._domain_entries[key] = domain
        self._endpoint_controls[key].append((domain, "normal"))
        _help_label(auth, "Senha (quando aplicável)", "password").grid(
            row=1, column=2, sticky="w", pady=5
        )
        password = ttk.Entry(auth, textvariable=variables["password"], show="•")
        password.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=5)
        _attach_help(password, "password")
        self._password_entries[key] = password
        self._endpoint_controls[key].append((password, "normal"))
        ttk.Label(
            auth,
            text=(
                "A senha permanece mascarada e somente em memória durante esta sessão; "
                "nunca é gravada no arquivo de configuração."
            ),
            bootstyle="secondary",
            wraplength=900,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        tls = ttk.Labelframe(frame, text="TLS", padding=12)
        tls.grid(row=row + 1, column=0, columnspan=4, sticky="ew", pady=8)
        encrypt = ttk.Checkbutton(
            tls,
            text="Criptografar conexão",
            variable=variables["encrypt"],
            bootstyle="round-toggle",
        )
        encrypt.pack(side="left", padx=(0, 4))
        self._endpoint_controls[key].append((encrypt, "normal"))
        _attach_help(encrypt, "encrypt")
        encrypt_help = ttk.Label(tls, text=" ? ", bootstyle="info", cursor="hand2")
        encrypt_help.pack(side="left", padx=(0, 24))
        _attach_help(encrypt_help, "encrypt")
        trust = ttk.Checkbutton(
            tls,
            text="Confiar no certificado do servidor",
            variable=variables["trust_server_certificate"],
            bootstyle="round-toggle",
        )
        trust.pack(side="left")
        self._endpoint_controls[key].append((trust, "normal"))
        _attach_help(trust, "trust_server_certificate")
        trust_help = ttk.Label(tls, text=" ? ", bootstyle="info", cursor="hand2")
        trust_help.pack(side="left", padx=(3, 0))
        _attach_help(trust_help, "trust_server_certificate")
        ttk.Label(frame, text="* campo obrigatório", bootstyle="secondary").grid(
            row=row + 2, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        self._update_endpoint_state(key)

    def _add_structure_profile_choice(self, key: str, value: str) -> None:
        if key not in self._structure_profile_choices:
            return
        choices = self._structure_profile_choices[key]
        if value and value not in choices:
            choices.append(value)
        combobox = self._structure_profile_comboboxes.get(key)
        if combobox is not None:
            combobox.configure(values=choices)

    def _select_structure_profile(self, key: str) -> None:
        area = "bronze" if key == "bronze_destination" else "landing"
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Carregar perfil de estrutura JSON",
            initialdir=str(DEFAULT_PROFILE_DIRECTORY.resolve()),
            filetypes=[("Perfil JSON", "*.json"), ("Todos os arquivos", "*.*")],
        )
        if not selected:
            return
        try:
            path, profile_name = validate_structure_profile_selection(
                selected, area=area
            )
        except Exception as error:
            messagebox.showerror(
                "Perfil de estrutura inválido",
                "O arquivo não foi carregado.\n\n" + redacted_exception(error),
                parent=self.root,
            )
            return
        value = str(path)
        self._add_structure_profile_choice(key, value)
        self.endpoint_variables[key]["structure_profile"].set(value)
        self._on_credential_context_changed()
        messagebox.showinfo(
            "Perfil de estrutura validado",
            f"Perfil {profile_name!r} carregado e selecionado para "
            f"{'Destino 01' if area == 'bronze' else 'Destino 02'}.\n\n{path}",
            parent=self.root,
        )

    def _update_endpoint_state(self, key: str) -> None:
        enabled = key == "source" or bool(self.endpoint_enabled[key].get())
        for widget, active_state in self._endpoint_controls.get(key, []):
            widget.configure(state=active_state if enabled else "disabled")
        toggle = self._endpoint_enabled_widgets.get(key)
        if toggle is not None:
            toggle.configure(
                state="disabled" if key == "bronze_destination" else "normal"
            )
        self._update_auth_state(key)

    def _update_auth_state(self, key: str) -> None:
        endpoint_is_enabled = key == "source" or bool(self.endpoint_enabled[key].get())
        displayed = str(self.endpoint_variables[key]["authentication_type"].get())
        authentication_type = AUTHENTICATION_LABELS.get(displayed, "")
        state = (
            "normal"
            if endpoint_is_enabled and authentication_type == "windows_credentials"
            else "disabled"
        )
        entry = self._domain_entries.get(key)
        if entry is not None:
            entry.configure(state=state)
        password_entry = self._password_entries.get(key)
        if password_entry is not None:
            password_entry.configure(
                state=(
                    "normal"
                    if endpoint_is_enabled
                    and authentication_type in {"sql", "windows_credentials"}
                    else "disabled"
                )
            )

    def _build_tables_tab(self) -> None:
        tab = self.tables_tab
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        columns = (
            "order",
            "source",
            "destination",
            "cdc",
            "watermark",
            "batch",
            "partition",
        )
        self.table_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headings = {
            "order": "Ordem",
            "source": "Origem (banco.esquema.tabela)",
            "destination": "Destino de dados (banco.esquema.tabela)",
            "cdc": "CDC",
            "watermark": "Marca d'água",
            "batch": "Linhas/bloco",
            "partition": "Particionamento",
        }
        widths = {
            "order": 64,
            "source": 220,
            "destination": 260,
            "cdc": 60,
            "watermark": 260,
            "batch": 110,
            "partition": 150,
        }
        for name in columns:
            heading = headings[name] + ("  |" if name != columns[-1] else "")
            self.table_tree.heading(name, text=heading, anchor="w")
            self.table_tree.column(
                name,
                width=widths[name],
                minwidth=widths[name],
                anchor="w",
                stretch=name in {"source", "destination", "watermark"},
            )
        self.table_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.table_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(tab, orient="horizontal", command=self.table_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table_tree.configure(
            yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set
        )
        self.table_tree.tag_configure("even", background="#f4f8fc")
        self.table_tree.bind("<Double-1>", lambda _event: self._edit_table())
        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(actions, text="Adicionar tabela", command=self._add_table, bootstyle="success").pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(actions, text="Editar", command=self._edit_table, bootstyle="primary").pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="Remover", command=self._remove_table, bootstyle="danger").pack(
            side="left", padx=6
        )
        ttk.Button(actions, text="Mover para cima", command=lambda: self._move_table(-1), bootstyle="secondary").pack(
            side="left", padx=(20, 6)
        )
        ttk.Button(actions, text="Mover para baixo", command=lambda: self._move_table(1), bootstyle="secondary").pack(
            side="left", padx=6
        )
        ttk.Label(
            tab,
            text="A ordem acima é a ordem de processamento. A marca d'água explícita é validada; o motor nunca a inventa.",
            bootstyle="secondary",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _current_destination_area(self) -> str:
        areas = self._selected_data_destination_areas()
        if len(areas) == 1:
            return areas[0]
        configured = str(self.base_config.get("active_destination", "bronze"))
        return configured if configured in DESTINATION_ENDPOINT_KEYS else "bronze"

    def _selected_data_destination_areas(self) -> list[str]:
        areas: list[str] = []
        for area, key in DESTINATION_ENDPOINT_KEYS.items():
            if not bool(self.endpoint_enabled[key].get()):
                continue
            displayed = str(self.endpoint_variables[key]["role"].get())
            role = DESTINATION_ROLE_LABELS.get(displayed)
            if role in DATA_DESTINATION_ROLES:
                areas.append(area)
        return areas

    def _required_data_destination_area(self) -> str:
        areas = self._selected_data_destination_areas()
        if len(areas) != 1:
            raise ValueError(
                "Configure exatamente um destino com função 'estrutura e dados' "
                "ou 'somente dados'."
            )
        return areas[0]

    def _on_perimeter_selected(self, _event: object | None = None) -> None:
        perimeter = str(self.global_variables["perimeter"].get())
        for endpoint in self.endpoint_variables.values():
            current = str(endpoint["username"].get())
            endpoint["username"].set(
                update_default_username_for_perimeter(
                    perimeter, current, self._last_perimeter
                )
            )
        self._last_perimeter = perimeter

    def _on_credential_context_changed(self) -> None:
        """Discard session secrets and stale connection evidence after an edit."""

        self.secret_resolver.clear_cache()
        self.plan_status.set("Planejamento ainda não executado")
        self._refresh_connection_summary([])
        self._invalidate_disk_projection()
        self._update_contextual_guidance()

    def _on_source_schema_changed(self, *_args: object) -> None:
        if self._suspend_schema_sync:
            return
        source_schema = str(self.endpoint_variables["source"]["schema"].get())
        self._suspend_schema_sync = True
        try:
            for key in ("bronze_destination", "landing_destination"):
                destination_schema = self.endpoint_variables[key]["schema"]
                current = str(destination_schema.get())
                if not current.strip() or current == self._previous_source_schema:
                    destination_schema.set(source_schema)
        finally:
            self._suspend_schema_sync = False
        self._previous_source_schema = source_schema
        for record in self._default_widgets:
            self._apply_default_style(record)

    def _on_executor_directory_changed(self, *_args: object) -> None:
        if self._suspend_directory_sync:
            return
        executor = str(self.global_variables["executor_directory"].get())
        destination_variable = self.global_variables["destination_sql_directory"]
        destination = str(destination_variable.get())
        if not destination.strip() or destination == self._previous_executor_directory:
            self._suspend_directory_sync = True
            try:
                destination_variable.set(executor)
            finally:
                self._suspend_directory_sync = False
        self._previous_executor_directory = executor
        self._invalidate_disk_projection()
        self._update_contextual_guidance()

    def _table_dialog_defaults(self) -> dict[str, str]:
        destination_key = DESTINATION_ENDPOINT_KEYS[self._current_destination_area()]
        return {
            "default_source_database": str(
                self.endpoint_variables["source"]["database"].get()
            ),
            "default_source_schema": str(
                self.endpoint_variables["source"]["schema"].get()
            ),
            "default_destination_database": str(
                self.endpoint_variables[destination_key]["database"].get()
            ),
            "default_destination_schema": str(
                self.endpoint_variables[destination_key]["schema"].get()
            ),
            "default_rows_per_block": str(
                self.global_variables["rows_per_block"].get()
            ),
            "default_structure_profile": str(
                self.endpoint_variables[destination_key][
                    "structure_profile"
                ].get()
            ),
        }

    def _add_table(self) -> None:
        defaults = self._table_dialog_defaults()
        initial = table_to_form({}, **defaults)
        initial["aud_ccid_value"] = str(len(self.table_forms) + 1)
        dialog = TableDialog(
            self.root,
            destination_area=self._current_destination_area(),
            initial=initial,
            destination_fields_enabled=bool(
                self.global_variables["execute_import"].get()
            ),
            **defaults,
        )
        value = dialog.show()
        if value is not None:
            self.table_forms.append(value)
            self._refresh_table_tree(select=len(self.table_forms) - 1)

    def _selected_table_index(self) -> int | None:
        selection = self.table_tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _edit_table(self) -> None:
        index = self._selected_table_index()
        if index is None:
            messagebox.showinfo("Tabelas", "Selecione uma tabela para editar.", parent=self.root)
            return
        defaults = self._table_dialog_defaults()
        dialog = TableDialog(
            self.root,
            destination_area=self._current_destination_area(),
            initial=self.table_forms[index],
            destination_fields_enabled=bool(
                self.global_variables["execute_import"].get()
            ),
            **defaults,
        )
        value = dialog.show()
        if value is not None:
            self.table_forms[index] = value
            self._refresh_table_tree(select=index)

    def _remove_table(self) -> None:
        index = self._selected_table_index()
        if index is None:
            messagebox.showinfo("Tabelas", "Selecione uma tabela para remover.", parent=self.root)
            return
        source = self.table_forms[index].get("source_table", "")
        if not messagebox.askyesno(
            "Remover tabela",
            f"Remover {source} apenas desta configuração?",
            parent=self.root,
        ):
            return
        self.table_forms.pop(index)
        self._refresh_table_tree(select=min(index, len(self.table_forms) - 1))

    def _move_table(self, offset: int) -> None:
        index = self._selected_table_index()
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self.table_forms):
            return
        self.table_forms[index], self.table_forms[target] = (
            self.table_forms[target], self.table_forms[index]
        )
        self._refresh_table_tree(select=target)

    def _refresh_table_tree(self, *, select: int | None = None) -> None:
        for item in self.table_tree.get_children():
            self.table_tree.delete(item)
        for index, form in enumerate(self.table_forms):
            destination = str(form.get("destination_table", "")).strip() or "(automático)"
            watermark = str(form.get("watermark", "")).strip() or "Automática / carga direta"
            source_database = str(form.get("source_database", "")).strip()
            source_schema = str(form.get("source_schema", "")).strip()
            destination_database = str(form.get("destination_database", "")).strip()
            destination_schema = str(form.get("destination_schema", "")).strip()
            source = str(form.get("source_table", "")).strip()
            source_parts = [part for part in (source_database, source_schema, source) if part]
            destination_parts = [
                part
                for part in (destination_database, destination_schema, destination)
                if part
            ]
            partition = (
                str(form.get("partition_column", "dh_carga")).strip()
                if form.get("partition_enabled")
                else "Sem particionamento"
            )
            self.table_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    index + 1,
                    ".".join(source_parts),
                    ".".join(destination_parts),
                    "Sim" if form.get("enable_cdc") else "Não",
                    watermark,
                    form.get("rows_per_block", "") or "Global",
                    partition,
                ),
                tags=("even",) if index % 2 == 0 else (),
            )
        if select is not None and 0 <= select < len(self.table_forms):
            self.table_tree.selection_set(str(select))
            self.table_tree.focus(str(select))
        self._invalidate_disk_projection()

    def _build_operations_tab(self) -> None:
        tab = self.operations_tab
        tab.columnconfigure(0, weight=1)

        prerequisites = ttk.Labelframe(
            tab, text="1. Atender aos pré-requisitos do executor", padding=6
        )
        prerequisites.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        prerequisites.columnconfigure(0, weight=1)
        ttk.Label(prerequisites, textvariable=self.odbc_requirement).grid(
            row=0, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Label(prerequisites, textvariable=self.bcp_requirement).grid(
            row=1, column=0, sticky="w", padx=4, pady=2
        )
        install = ttk.Button(
            prerequisites,
            text="Instalar pré-requisitos",
            command=self._install_prerequisites,
            bootstyle="outline-info",
        )
        install.grid(row=0, column=1, rowspan=2, padx=(8, 4), pady=4)
        verify = ttk.Button(
            prerequisites,
            text="Verificar pré-requisitos",
            command=self._verify_prerequisites,
            bootstyle="info",
        )
        verify.grid(row=0, column=2, rowspan=2, padx=4, pady=4)
        ttk.Label(
            prerequisites, textvariable=self.prerequisite_status, bootstyle="secondary"
        ).grid(row=0, column=3, rowspan=2, sticky="w", padx=8)

        ttk.Separator(prerequisites).grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=(8, 6)
        )
        ttk.Label(
            prerequisites,
            textvariable=self.disk_projection_overview,
            wraplength=1130,
            bootstyle="primary",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=4, pady=2)
        disk_guidance_label = ttk.Label(
            prerequisites,
            textvariable=self.disk_projection_guidance,
            wraplength=1800,
            bootstyle="danger",
            justify="left",
        )
        disk_guidance_label.grid(
            row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(2, 5)
        )
        prerequisites.bind(
            "<Configure>",
            lambda event, label=disk_guidance_label: label.configure(
                wraplength=max(500, event.width - 40)
            ),
            add="+",
        )
        ttk.Label(
            prerequisites,
            textvariable=self.destination_disk_assessment,
            wraplength=1130,
        ).grid(row=5, column=0, columnspan=4, sticky="w", padx=4, pady=2)

        projection_columns = (
            "source",
            "estimated_rows",
            "raw_bytes",
            "protected_bytes",
        )
        projection_frame = ttk.Frame(prerequisites)
        projection_frame.columnconfigure(0, weight=1)
        projection_frame.grid(
            row=6, column=0, columnspan=4, sticky="ew", padx=4, pady=(2, 4)
        )
        self.disk_projection_tree = ttk.Treeview(
            projection_frame,
            columns=projection_columns,
            show="headings",
            height=2,
        )
        projection_headings = {
            "source": "Tabela de origem  |",
            "estimated_rows": "Linhas estimadas  |",
            "raw_bytes": "BCP bruto  |",
            "protected_bytes": "BCP + margem",
        }
        projection_widths = {
            "source": 360,
            "estimated_rows": 120,
            "raw_bytes": 140,
            "protected_bytes": 140,
        }
        for name in projection_columns:
            self.disk_projection_tree.heading(
                name, text=projection_headings[name], anchor="w"
            )
            self.disk_projection_tree.column(
                name,
                width=projection_widths[name],
                anchor="w",
            )
        self.disk_projection_tree.tag_configure("unavailable", foreground="#9a6700")
        self.disk_projection_tree.grid(row=0, column=0, sticky="ew")
        projection_scroll = ttk.Scrollbar(
            projection_frame,
            orient="vertical",
            command=self.disk_projection_tree.yview,
        )
        projection_scroll.grid(row=0, column=1, sticky="ns")
        self.disk_projection_tree.configure(yscrollcommand=projection_scroll.set)
        planning = ttk.Labelframe(
            tab, text="2. Informar credenciais e planejar", padding=6
        )
        planning.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        planning.columnconfigure(1, weight=1)
        permission_guidance_label = ttk.Label(
            planning,
            textvariable=self.credential_permissions_guidance,
            bootstyle="danger",
            justify="left",
            wraplength=1800,
        )
        permission_guidance_label.grid(
            row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(2, 6)
        )
        planning.bind(
            "<Configure>",
            lambda event, label=permission_guidance_label: label.configure(
                wraplength=max(500, event.width - 40)
            ),
            add="+",
        )
        plan = ttk.Button(planning, text="Planejar", command=self._plan, bootstyle="primary")
        plan.grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(
            planning,
            text=(
                "Senhas necessárias serão solicitadas separadamente e mascaradas "
                "para Origem, Destino 01 e Destino 02."
            ),
            bootstyle="secondary",
        ).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(planning, textvariable=self.plan_status, bootstyle="secondary").grid(
            row=1, column=2, sticky="e", padx=8
        )
        summary_columns = ("environment", "username", "instance", "port", "database")
        self.connection_tree = ttk.Treeview(
            planning,
            columns=summary_columns,
            show="headings",
            height=CONNECTION_SUMMARY_VISIBLE_ROWS,
        )
        summary_headings = {
            "environment": "Ambiente  |",
            "username": "Usuário  |",
            "instance": "Instância SQL Server  |",
            "port": "Porta  |",
            "database": "Banco de dados",
        }
        summary_widths = {
            "environment": 100,
            "username": 220,
            "instance": 260,
            "port": 80,
            "database": 180,
        }
        for name in summary_columns:
            self.connection_tree.heading(
                name, text=summary_headings[name], anchor="w"
            )
            self.connection_tree.column(
                name,
                width=summary_widths[name],
                anchor="w",
            )
        self.connection_tree.grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 2)
        )

        ddl = ttk.Labelframe(tab, text="3. Gerar e aplicar a estrutura", padding=6)
        ddl.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        ddl.columnconfigure(4, weight=1)
        collision_container = ttk.Frame(ddl)
        collision_container.grid(row=0, column=0, padx=4, pady=4, sticky="w")
        collision_toggle = ttk.Frame(collision_container)
        collision_toggle.grid(row=0, column=0, sticky="w")
        self.ddl_collision_checkbutton = ttk.Checkbutton(
            collision_toggle,
            text="Resolver colisão de colunas",
            variable=self.global_variables["resolve_column_collisions"],
            bootstyle="round-toggle",
        )
        self.ddl_collision_checkbutton.pack(side="left")
        collision_help = ttk.Label(
            collision_toggle, text=" ? ", bootstyle="info", cursor="hand2"
        )
        collision_help.pack(side="left", padx=(3, 0))
        _attach_help(collision_help, "resolve_column_collisions")
        strategy_label = _help_label(
            collision_container, "Estratégia", "column_collision_strategy"
        )
        strategy_label.grid(row=1, column=0, sticky="w", pady=(4, 1))
        self.column_collision_strategy_widget = ttk.Combobox(
            collision_container,
            textvariable=self.global_variables["column_collision_strategy"],
            values=list(COLUMN_COLLISION_STRATEGY_LABELS),
            state="readonly",
            width=39,
        )
        self.column_collision_strategy_widget.grid(row=2, column=0, sticky="w")
        _attach_help(
            self.column_collision_strategy_widget, "column_collision_strategy"
        )
        self._register_default_widget(
            self.column_collision_strategy_widget,
            self.global_variables["column_collision_strategy"],
            "Manter somente a coluna de destino",
            "combobox",
        )
        self._update_collision_strategy_state()
        _help_label(ddl, "Área do DDL", "ddl_area").grid(
            row=0, column=1, padx=(12, 4), pady=4, sticky="w"
        )
        ddl_area = ttk.Combobox(
            ddl,
            textvariable=self.ddl_area,
            values=["Nenhum", "Destino 01", "Destino 02", "Ambas"],
            state="readonly",
            width=12,
        )
        ddl_area.grid(row=0, column=2, padx=4, pady=4)
        self.ddl_area_widget = ddl_area
        self._update_ddl_area_choices()
        _attach_help(ddl_area, "ddl_area")
        self._register_default_widget(ddl_area, self.ddl_area, "Ambas", "combobox")
        _help_label(ddl, "Diretório dos scripts", "ddl_output").grid(
            row=0, column=3, padx=(18, 4), pady=4, sticky="w"
        )
        ddl_output = ttk.Entry(ddl, textvariable=self.ddl_output, width=36)
        ddl_output.grid(row=0, column=4, padx=4, pady=4, sticky="ew")
        ddl_output_tooltip = ToolTip(
            ddl_output, text=str(self.ddl_output.get()), wraplength=700, delay=250
        )
        ddl_output.bind(
            "<Enter>",
            lambda _event, tip=ddl_output_tooltip: setattr(
                tip, "text", str(self.ddl_output.get()) or "<não informado>"
            ),
            add="+",
        )
        setattr(ddl_output, "_value_tooltip", ddl_output_tooltip)
        self._register_default_widget(
            ddl_output, self.ddl_output, str(RUNTIME_DDL_DIRECTORY), "entry"
        )
        select_ddl_output = ttk.Button(
            ddl,
            text="...",
            width=4,
            command=self._select_ddl_output_directory,
            bootstyle="secondary-outline",
        )
        select_ddl_output.grid(row=0, column=5, padx=(0, 4), pady=4)
        ToolTip(
            select_ddl_output,
            text=(
                "Escolha a pasta dos scripts DDL. O seletor abre por padrão em "
                f"{RUNTIME_DDL_DIRECTORY}."
            ),
            wraplength=600,
        )
        generate = ttk.Button(
            ddl, text="Gerar DDL", command=lambda: self._ddl(False), bootstyle="secondary"
        )
        generate.grid(
            row=0, column=6, padx=4, pady=4
        )
        apply_ddl = ttk.Button(
            ddl, text="Aplicar DDL", command=lambda: self._ddl(True), bootstyle="warning"
        )
        apply_ddl.grid(
            row=0, column=7, padx=4, pady=4
        )
        self.ddl_non_area_controls = [
            self.ddl_collision_checkbutton,
            ddl_output,
            select_ddl_output,
            generate,
            apply_ddl,
        ]
        ttk.Label(
            ddl,
            textvariable=self.ddl_none_warning,
            bootstyle="danger",
            wraplength=1130,
        ).grid(row=1, column=0, columnspan=8, sticky="w", padx=4, pady=(2, 0))
        self._update_ddl_section_state()

        transfer = ttk.Labelframe(
            tab, text="4. Exportar da Origem e importar no destino de dados", padding=6
        )
        transfer.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        transfer.columnconfigure(3, weight=1)
        run = ttk.Button(
            transfer,
            text="Exportar e importar dados",
            command=self._run,
            bootstyle="success",
        )
        run.grid(row=0, column=0, padx=4, pady=4)
        _help_label(transfer, "ID da execução", "execution_id").grid(
            row=0, column=1, padx=(18, 4), pady=4
        )
        execution_id = ttk.Entry(transfer, textvariable=self.execution_id, width=40)
        execution_id.grid(row=0, column=2, columnspan=2, sticky="ew", padx=4, pady=4)
        _attach_help(execution_id, "execution_id")
        resume = ttk.Button(transfer, text="Retomar", command=self._resume, bootstyle="primary")
        resume.grid(row=0, column=4, padx=4, pady=4)
        status = ttk.Button(
            transfer,
            text="Consultar status",
            command=self._status,
            bootstyle="outline-primary",
        )
        status.grid(row=0, column=5, padx=4, pady=4)
        self.cancel_execution_button = ttk.Button(
            transfer,
            text="Cancelar",
            command=self._cancel_execution,
            bootstyle="danger",
            state="disabled",
        )
        self.cancel_execution_button.grid(row=0, column=6, padx=4, pady=4)

        recovery = ttk.Frame(transfer)
        recovery.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 0))
        recovery.columnconfigure(2, weight=1)
        _help_label(recovery, "Manifesto / diretório", "manifest_path").grid(
            row=0, column=0, padx=4, pady=4
        )
        manifest = ttk.Entry(recovery, textvariable=self.manifest_path, width=54)
        manifest.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        _attach_help(manifest, "manifest_path")
        ttk.Button(
            recovery,
            text="Selecionar…",
            command=self._select_manifest,
            bootstyle="secondary",
        ).grid(row=0, column=3, padx=4, pady=4)
        import_manifests = ttk.Button(
            recovery,
            text="Importar manifestos",
            command=self._import_manifests,
            bootstyle="warning",
        )
        import_manifests.grid(row=0, column=4, padx=4, pady=4)
        ttk.Button(
            recovery,
            text="Salvar configuração para GUI/CLI",
            command=self._save_configuration,
            bootstyle="outline-success",
        ).grid(row=0, column=5, padx=(18, 4), pady=4)

        cli_hint = ttk.Frame(transfer)
        cli_hint.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(2, 4))
        cli_hint.columnconfigure(1, weight=1)
        ttk.Label(cli_hint, text="Fluxo completo sugerido para CLI").grid(
            row=0, column=0, sticky="w", padx=4
        )
        cli_command = ttk.Entry(
            cli_hint,
            textvariable=self.cli_command_proposal,
            state="readonly",
        )
        cli_command.grid(row=0, column=1, sticky="ew", padx=4)
        cli_tooltip = ToolTip(
            cli_command,
            text=(
                "Aplica primeiro o DDL idempotente. A carga sequencial, uma tabela "
                "por vez, começa somente quando o comando DDL termina com código zero."
            ),
            wraplength=700,
        )
        cli_command.bind(
            "<Enter>",
            lambda _event, tip=cli_tooltip: setattr(
                tip,
                "text",
                "DDL idempotente seguido da carga tabela por tabela:\n"
                + self.cli_command_proposal.get(),
            ),
            add="+",
        )
        ttk.Button(
            cli_hint,
            text="Copiar fluxo CLI",
            command=self._copy_cli_command,
            bootstyle="outline-secondary",
        ).grid(row=0, column=2, padx=4)

        notice = ttk.Label(
            tab,
            text=(
                "Operações destrutivas exigem confirmação. Ao cancelar uma carga, o lote "
                "corrente termina e os checkpoints são preservados para retomada."
            ),
            bootstyle="secondary",
            wraplength=1100,
        )
        notice.grid(row=4, column=0, sticky="w", pady=(0, 2))

        self.action_buttons.extend(
            [
                install,
                verify,
                plan,
                generate,
                apply_ddl,
                run,
                resume,
                status,
                import_manifests,
            ]
        )
        self._update_prerequisite_descriptions()

    def _build_tracking_tab(self) -> None:
        tab = self.tracking_tab
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        log_frame = ttk.Labelframe(tab, text="Acompanhamento", padding=8)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(
            log_frame,
            height=24,
            wrap="word",
            font=("Cascadia Mono", 10),
            state="disabled",
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.vbar.configure(command=self._on_log_scrollbar)
        for sequence in (
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
            "<KeyRelease-Up>",
            "<KeyRelease-Down>",
            "<KeyRelease-Prior>",
            "<KeyRelease-Next>",
            "<KeyRelease-Home>",
            "<KeyRelease-End>",
        ):
            self.log_text.bind(sequence, self._on_log_view_changed, add="+")
        footer = ttk.Frame(log_frame)
        footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            textvariable=self.log_follow_status,
            bootstyle="secondary",
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            footer,
            text="Modo verbose",
            variable=self.verbose_logging,
            bootstyle="round-toggle",
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            footer,
            text="Ir para o mais recente",
            command=self._follow_latest_log,
            bootstyle="outline-primary",
        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            footer,
            text="Limpar acompanhamento",
            command=self._clear_log,
            bootstyle="outline-secondary",
        ).grid(row=0, column=3, padx=(8, 0))


    def _invalidate_disk_projection(self) -> None:
        """Discard stale sizing evidence without touching disk or SQL."""

        overview = getattr(self, "disk_projection_overview", None)
        destination = getattr(self, "destination_disk_assessment", None)
        warning = getattr(self, "disk_operator_warning", None)
        if overview is not None:
            overview.set(
                "Projeção de disco: execute Planejar para recalcular todas as tabelas."
            )
        if destination is not None:
            destination.set("Destino 01: aguardando planejamento.")
        if warning is not None:
            warning.set("")
        tree = getattr(self, "disk_projection_tree", None)
        if tree is not None:
            for item in tree.get_children():
                tree.delete(item)

    @staticmethod
    def _format_projection_rows(value: int | None) -> str:
        if value is None:
            return "indisponível"
        return f"{value:,}".replace(",", ".")

    def _refresh_disk_projection(self, summary: Mapping[str, Any]) -> None:
        """Render a summary calculated by the background planning worker."""

        unknown = int(summary.get("unknown_table_count", 0))
        raw = summary.get("total_raw_bytes")
        protected = summary.get("total_protected_bytes")
        factor = summary.get("safety_factor")
        if raw is None or protected is None:
            self.disk_projection_overview.set(
                "Projeção consolidada INDISPONÍVEL: "
                f"{unknown} tabela(s) sem estimativa; subtotal conhecido "
                f"{format_bytes(summary.get('known_raw_bytes'))} bruto / "
                f"{format_bytes(summary.get('known_protected_bytes'))} com margem."
            )
        else:
            self.disk_projection_overview.set(
                "Projeção consolidada: "
                f"bruto {format_bytes(raw)} | com fator {factor:g}: "
                f"{format_bytes(protected)}."
            )

        directories = summary.get("directories") or {}
        destination = summary.get("destination_os") or {}
        capacity = destination.get("capacity_ok")
        capacity_text = (
            "SUPORTA"
            if capacity is True
            else "NÃO SUPORTA"
            if capacity is False
            else "NÃO FOI POSSÍVEL CAPTURAR"
        )
        destination_text = (
            f"Destino 01 ({self.endpoint_variables['bronze_destination']['instance'].get() or '<não informada>'}) "
            "(discos do SQL Server): livre mínimo "
            f"{format_bytes(destination.get('free_bytes'))} | espaço total das tabelas "
            f"de origem {format_bytes(destination.get('required_bytes'))} | "
            f"avaliação: {capacity_text}"
        )
        if destination.get("error"):
            destination_text += " — " + redact_text(str(destination["error"]))
        destination_variable = getattr(self, "destination_disk_assessment", None)
        if destination_variable is not None:
            destination_variable.set(destination_text)
        warning_variable = getattr(self, "disk_operator_warning", None)
        if warning_variable is not None:
            warning_variable.set("")

        tree = getattr(self, "disk_projection_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        for index, row in enumerate(summary.get("tables") or []):
            protected_text = format_bytes(row.get("protected_bytes"))
            if row.get("margin_bytes") is not None:
                protected_text += f" (+{format_bytes(row.get('margin_bytes'))})"
            state = str(row.get("projection_state", "unavailable"))
            tree.insert(
                "",
                "end",
                iid=f"disk-projection-{index}",
                values=(
                    row.get("source") or "indisponível",
                    self._format_projection_rows(row.get("estimated_rows")),
                    format_bytes(row.get("raw_bytes")),
                    protected_text,
                ),
                tags=("unavailable",) if state != "available" else (),
            )

    def _update_prerequisite_descriptions(self) -> None:
        driver = str(self.global_variables["odbc_driver"].get()).strip()
        executable = str(self.global_variables["bcp_executable"].get()).strip()
        self.odbc_requirement.set(
            f"[Microsoft][ODBC Driver Manager]: {driver or '<informe o driver>'}"
        )
        self.bcp_requirement.set(
            f"BCP (utilitário): {executable or '<informe o executável>'}"
        )
        self.prerequisite_status.set("Ainda não verificado")

    def _verify_prerequisites(self) -> None:
        if self.busy:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde a conclusão da operação atual.",
                parent=self.root,
            )
            return
        driver = str(self.global_variables["odbc_driver"].get()).strip()
        executable = str(self.global_variables["bcp_executable"].get()).strip()
        if not driver or not executable:
            messagebox.showerror(
                "Pré-requisitos incompletos",
                "Informe o Driver ODBC e o executável BCP na aba Geral.",
                parent=self.root,
            )
            return

        def operation() -> str:
            report = inspect_prerequisites(
                {
                    "odbc_driver": driver,
                    "bcp_executable": executable,
                },
                hide_window=True,
            )
            if not report.ready:
                raise RuntimeError("; ".join(report.errors) or "Pré-requisitos indisponíveis.")
            details = report.as_dict()
            self.worker_queue.put(
                (
                    "prerequisites",
                    f"OK — BCP {details['bcp_version']}",
                )
            )
            return (
                "Pré-requisitos confirmados no executor.\n"
                f"Driver ODBC: {driver}\n"
                f"BCP: {details['bcp_resolved_path']} (versão {details['bcp_version']})"
            )

        self._start_worker("Verificando pré-requisitos", operation)

    def _install_prerequisites(self) -> None:
        """Open Setup-BulkFlow.exe from the packaged or development layout."""

        if self.busy:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde a conclusão da operação atual.",
                parent=self.root,
            )
            return
        executable_directory = Path(sys.executable).resolve().parent
        project_directory = Path(__file__).resolve().parents[1]
        candidates = (
            executable_directory / "Setup-BulkFlow.exe",
            project_directory / "release" / "Setup-BulkFlow.exe",
            Path.cwd().resolve() / "release" / "Setup-BulkFlow.exe",
            Path.cwd().resolve() / "Setup-BulkFlow.exe",
        )
        setup = next((path for path in candidates if path.is_file()), None)
        if setup is None:
            messagebox.showerror(
                "Instalador não encontrado",
                "Não foi possível localizar Setup-BulkFlow.exe. "
                "Mantenha o instalador na mesma pasta da GUI ou na pasta release.",
                parent=self.root,
            )
            return
        try:
            subprocess.Popen([str(setup)], cwd=str(setup.parent))
        except OSError as error:
            messagebox.showerror(
                "Não foi possível abrir o instalador",
                redacted_exception(error),
                parent=self.root,
            )
            return
        self._append_log(f"Instalador de pré-requisitos iniciado: {setup}")

    def _refresh_connection_summary(self, rows: list[tuple[str, ...]]) -> None:
        tree = getattr(self, "connection_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        for index, row in enumerate(rows):
            tree.insert("", "end", iid=f"connection-{index}", values=row)

    def _select_manifest(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Selecionar manifesto ou índice",
            filetypes=[("Manifestos JSON", "*.json"), ("Todos os arquivos", "*.*")],
        )
        if not selected:
            selected = filedialog.askdirectory(
                parent=self.root, title="Ou selecionar diretório de manifestos"
            )
        if selected:
            self.manifest_path.set(selected)

    def _copy_cli_command(self) -> None:
        command = self.cli_command_proposal.get().strip()
        if (
            not command
            or self.current_config_path is None
            or command.startswith("Nenhum destino habilitado")
        ):
            messagebox.showinfo(
                "Fluxo da CLI",
                command
                or "Salve a configuração antes de copiar o fluxo da CLI.",
                parent=self.root,
            )
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.operation_status.set("Fluxo completo da CLI copiado")

    def _endpoint_form_values(self, key: str) -> dict[str, Any]:
        variables = self.endpoint_variables[key]
        result = {name: variable.get() for name, variable in variables.items()}
        role_labels = (
            SOURCE_ROLE_LABELS if key == "source" else DESTINATION_ROLE_LABELS
        )
        result["role"] = code_for_label(
            role_labels,
            str(result["role"]),
            "Função da conexão",
        )
        result["authentication_type"] = code_for_label(
            AUTHENTICATION_LABELS,
            str(result["authentication_type"]),
            "Tipo de autenticação",
        )
        result["secret_provider"] = code_for_label(
            SECRET_PROVIDER_LABELS,
            str(result["secret_provider"]),
            "Provedor da senha",
        )
        if (
            result["authentication_type"] in {"sql", "windows_credentials"}
            and result["secret_provider"] != "windows_dpapi"
        ):
            result["secret_provider"] = "prompt"
            result["secret_reference"] = ""
        result.pop("password", None)
        return result

    def _collect_config(self) -> dict[str, Any]:
        candidate = deepcopy(self.base_config)
        candidate["config_version"] = 2
        candidate["perimeter"] = str(self.global_variables["perimeter"].get())
        for name in (
            "execute_import",
            "create_structure_if_needed",
            "allow_schema_evolution",
            "resolve_column_collisions",
            "delete_confirmed_files",
            "delete_completed_table_artifacts",
            "continue_after_table_error",
        ):
            candidate[name] = bool(self.global_variables[name].get())
        candidate["column_collision_strategy"] = code_for_label(
            COLUMN_COLLISION_STRATEGY_LABELS,
            str(self.global_variables["column_collision_strategy"].get()),
            "Estratégia de colisão de colunas",
        )
        candidate["rows_per_block"] = parse_required_integer(
            str(self.global_variables["rows_per_block"].get()),
            "Linhas por bloco",
            minimum=1,
            maximum=5_000_000,
        )
        candidate["keyless_direct_load_max_rows"] = parse_required_integer(
            str(self.global_variables["keyless_direct_load_max_rows"].get()),
            "Limite para tabela sem chave",
            minimum=0,
        )
        candidate["cdc_retention_minutes"] = parse_required_integer(
            str(self.global_variables["cdc_retention_minutes"].get()),
            "Retenção do CDC em minutos",
            minimum=1,
            maximum=MAX_CDC_RETENTION_MINUTES,
        )
        candidate["max_file_bytes"] = parse_required_integer(
            str(self.global_variables["max_file_bytes"].get()),
            "Arquivo máximo em bytes",
            minimum=1,
        )
        candidate["minimum_free_space_bytes"] = parse_required_integer(
            str(self.global_variables["minimum_free_space_bytes"].get()),
            "Espaço livre mínimo em bytes",
            minimum=0,
        )
        for name in (
            "connection_timeout_seconds",
            "sql_timeout_seconds",
            "bcp_timeout_seconds",
        ):
            candidate[name] = parse_required_integer(
                str(self.global_variables[name].get()), name, minimum=0
            )
        for name in (
            "executor_directory",
            "local_control_directory",
            "odbc_driver",
            "bcp_executable",
        ):
            candidate[name] = str(self.global_variables[name].get()).strip()
        # O controle SQL usa o schema dbo no destino de dados. Reafirmar o
        # valor aqui impede que uma alteração programática da StringVar gere
        # uma configuração que a própria interface não permite editar.
        self.global_variables["control_schema"].set("dbo")
        candidate["control_schema"] = "dbo"
        sql_directory = str(self.global_variables["destination_sql_directory"].get()).strip()
        if sql_directory:
            candidate["destination_sql_directory"] = sql_directory
        else:
            candidate.pop("destination_sql_directory", None)
        reader_sids = str(self.global_variables["artifact_reader_sids"].get()).replace(";", ",")
        candidate["artifact_reader_sids"] = [
            item.strip() for item in reader_sids.split(",") if item.strip()
        ]
        writer_sids = str(self.global_variables["artifact_writer_sids"].get()).replace(";", ",")
        candidate["artifact_writer_sids"] = [
            item.strip() for item in writer_sids.split(",") if item.strip()
        ]
        candidate["estimates"]["row_count_method"] = code_for_label(
            ROW_COUNT_LABELS,
            str(self.global_variables["row_count_method"].get()),
            "Contagem de linhas",
        )
        candidate["estimates"]["maximum_sample_rows"] = parse_required_integer(
            str(self.global_variables["maximum_sample_rows"].get()),
            "Amostra máxima",
            minimum=1,
            maximum=1_000_000,
        )
        candidate["estimates"]["safety_factor"] = parse_required_number(
            str(self.global_variables["safety_factor"].get()),
            "Fator de segurança",
            minimum=1.0,
        )
        candidate["batching"]["require_watermark_index"] = bool(
            self.global_variables["require_watermark_index"].get()
        )
        candidate["structure"]["secondary_indexes_phase"] = code_for_label(
            INDEX_PHASE_LABELS,
            str(self.global_variables["secondary_indexes_phase"].get()),
            "Momento dos índices secundários",
        )
        candidate["source"] = build_endpoint(
            self._endpoint_form_values("source"), source=True
        )
        for key in ("bronze_destination", "landing_destination"):
            keep_export_layout = (
                key == "bronze_destination" and not candidate["execute_import"]
            )
            if self.endpoint_enabled[key].get() or keep_export_layout:
                candidate[key] = build_endpoint(
                    self._endpoint_form_values(key), source=False
                )
            else:
                candidate.pop(key, None)
        data_areas = self._selected_data_destination_areas()
        if candidate["execute_import"]:
            destination_area = self._required_data_destination_area()
        elif len(data_areas) > 1:
            raise ValueError(
                "Configure no máximo um destino com função 'estrutura e dados' "
                "ou 'somente dados'."
            )
        elif data_areas:
            destination_area = data_areas[0]
        else:
            # Exportações sem destino continuam precisando de uma área lógica
            # para resolver o perfil do artefato. Nenhuma conexão de destino é
            # aberta nesse fluxo.
            destination_area = self._current_destination_area()
        candidate["active_destination"] = destination_area
        if not self.table_forms:
            raise ValueError("Informe pelo menos uma tabela")
        table_defaults = self._table_dialog_defaults()
        candidate["tables"] = [
            build_table(
                form,
                destination_area=destination_area,
                default_rows_per_block=table_defaults["default_rows_per_block"],
                default_structure_profile=table_defaults[
                    "default_structure_profile"
                ],
            )
            for form in self.table_forms
        ]
        return validate_config(candidate)

    def _load_config_into_form(self, config: Mapping[str, Any], *, path: Path | None) -> None:
        normalized = validate_config(resolve_profile_paths_for_editor(config, path))
        for endpoint_key, area in (
            ("bronze_destination", "bronze"),
            ("landing_destination", "landing"),
        ):
            endpoint = normalized.get(endpoint_key)
            if endpoint is not None:
                validate_structure_profile_reference(
                    str(endpoint["structure_profile"]), area=area
                )
        self.secret_resolver.clear_cache()
        self.base_config = deepcopy(normalized)
        self.current_config_path = path.resolve() if path else None
        self.config_path_label.set(
            str(self.current_config_path) if self.current_config_path else "Configuração ainda não salva"
        )
        globals_to_copy = (
            "perimeter",
            "rows_per_block",
            "keyless_direct_load_max_rows",
            "executor_directory",
            "destination_sql_directory",
            "local_control_directory",
            "max_file_bytes",
            "minimum_free_space_bytes",
            "odbc_driver",
            "bcp_executable",
            "connection_timeout_seconds",
            "sql_timeout_seconds",
            "bcp_timeout_seconds",
            "cdc_retention_minutes",
            "control_schema",
        )
        for name in globals_to_copy:
            self.global_variables[name].set(str(normalized.get(name, "")))
        self.global_variables["artifact_reader_sids"].set(
            ", ".join(normalized.get("artifact_reader_sids", []))
        )
        self.global_variables["artifact_writer_sids"].set(
            ", ".join(normalized.get("artifact_writer_sids", []))
        )
        for name in (
            "execute_import",
            "create_structure_if_needed",
            "allow_schema_evolution",
            "resolve_column_collisions",
            "delete_confirmed_files",
            "delete_completed_table_artifacts",
            "continue_after_table_error",
        ):
            self.global_variables[name].set(bool(normalized[name]))
        self.global_variables["column_collision_strategy"].set(
            label_for_code(
                COLUMN_COLLISION_STRATEGY_LABELS,
                str(normalized["column_collision_strategy"]),
            )
        )
        self._update_collision_strategy_state()
        self.global_variables["row_count_method"].set(
            label_for_code(ROW_COUNT_LABELS, str(normalized["estimates"]["row_count_method"]))
        )
        self.global_variables["maximum_sample_rows"].set(
            str(normalized["estimates"]["maximum_sample_rows"])
        )
        self.global_variables["safety_factor"].set(
            str(normalized["estimates"]["safety_factor"])
        )
        self.global_variables["require_watermark_index"].set(
            bool(normalized["batching"]["require_watermark_index"])
        )
        self.global_variables["secondary_indexes_phase"].set(
            label_for_code(
                INDEX_PHASE_LABELS,
                str(normalized["structure"]["secondary_indexes_phase"]),
            )
        )
        self._suspend_schema_sync = True
        try:
            self._load_endpoint("source", normalized.get("source"), source=True)
            self._load_endpoint(
                "bronze_destination", normalized.get("bronze_destination"), source=False
            )
            self._load_endpoint(
                "landing_destination", normalized.get("landing_destination"), source=False
            )
        finally:
            self._suspend_schema_sync = False
        self._previous_source_schema = str(
            self.endpoint_variables["source"]["schema"].get()
        )
        self._last_perimeter = str(normalized["perimeter"])
        self.table_forms = []
        if path is None:
            # A nova configuração herda o esquema da Origem nos dois destinos
            # e começa sem linhas fictícias na grade.
            source_schema = str(self.endpoint_variables["source"]["schema"].get())
            self.endpoint_variables["bronze_destination"]["schema"].set(source_schema)
            self.endpoint_variables["landing_destination"]["schema"].set(source_schema)
        else:
            source_endpoint = normalized["source"]
            destination_area = str(normalized["active_destination"])
            destination_endpoint = normalized.get(
                DESTINATION_ENDPOINT_KEYS[destination_area], {}
            )
            for index, table in enumerate(normalized["tables"]):
                form = table_to_form(
                    table,
                    default_source_database=source_endpoint["database"],
                    default_source_schema=source_endpoint["schema"],
                    default_destination_database=destination_endpoint.get("database", ""),
                    default_destination_schema=destination_endpoint.get("schema", ""),
                    default_rows_per_block=normalized["rows_per_block"],
                    default_structure_profile=destination_endpoint.get(
                        "structure_profile", ""
                    ),
                )
                if "metadata_mapping" not in table:
                    form["aud_ccid_value"] = str(index + 1)
                self.table_forms.append(form)
        self._refresh_table_tree()
        self._refresh_connection_summary([])
        if not bool(self.global_variables["create_structure_if_needed"].get()):
            self.endpoint_variables["bronze_destination"]["role"].set(
                label_for_code(DESTINATION_ROLE_LABELS, "data_only")
            )
        self._on_execute_import_changed()
        self._update_ddl_area_default()
        self._update_contextual_guidance()
        self.plan_status.set("Planejamento ainda não executado")

    def _load_endpoint(
        self,
        key: str,
        endpoint: Mapping[str, Any] | None,
        *,
        source: bool,
    ) -> None:
        variables = self.endpoint_variables[key]
        self.endpoint_enabled[key].set(source or endpoint is not None)
        data = dict(endpoint or {})
        auth = dict(data.get("authentication") or {"type": "windows_integrated"})
        secret = dict(auth.get("password") or {"provider": "prompt", "reference": None})
        instance, port = split_instance_and_port(
            str(data.get("instance", "")), data.get("port", "")
        )
        values: dict[str, Any] = {
            "role": label_for_code(
                SOURCE_ROLE_LABELS if source else DESTINATION_ROLE_LABELS,
                str(data.get("role", DEFAULT_ENDPOINT_ROLES[key])),
            ),
            "instance": instance,
            "port": port,
            "database": data.get("database", ""),
            "schema": data.get("schema", ""),
            "structure_profile": data.get("structure_profile", ""),
            "odbc_dsn": data.get("odbc_dsn", ""),
            "authentication_type": label_for_code(
                AUTHENTICATION_LABELS, str(auth.get("type", "windows_integrated"))
            ),
            "username": auth.get("username", ""),
            "password": "",
            "domain": auth.get("domain", ""),
            "secret_provider": label_for_code(
                SECRET_PROVIDER_LABELS, str(secret.get("provider", "env"))
            ),
            "secret_reference": secret.get("reference") or "",
            "encrypt": data.get("tls", {}).get("encrypt", True),
            "trust_server_certificate": data.get("tls", {}).get(
                "trust_server_certificate", True
            ),
        }
        for name, value in values.items():
            variables[name].set(value)
        if not source:
            self._add_structure_profile_choice(
                key, str(values["structure_profile"])
            )
        self._update_auth_state(key)

    def _new_configuration(self) -> None:
        if self.busy:
            return
        if not messagebox.askyesno(
            "Nova configuração",
            "Restaurar o modelo inicial agnóstico? Alterações não salvas serão perdidas.",
            parent=self.root,
        ):
            return
        self._load_config_into_form(default_gui_config(), path=None)
        self._append_log("Nova configuração criada com o modelo inicial agnóstico.")

    def _open_configuration(self) -> None:
        if self.busy:
            return
        selected = filedialog.askopenfilename(
            parent=self.root,
            title=f"Abrir configuração — {PRODUCT_NAME}",
            initialdir=str(RUNTIME_CONFIG_DIRECTORY),
            filetypes=[("Configuração JSON", "*.json"), ("Todos os arquivos", "*.*")],
        )
        if not selected:
            return
        try:
            path = Path(selected)
            config = read_config(path)
            self._load_config_into_form(config, path=path)
        except Exception as error:
            messagebox.showerror(
                "Não foi possível abrir", redacted_exception(error), parent=self.root
            )
            return
        self._append_log(f"Configuração aberta: {path.resolve()}")

    def _save_configuration(self, *, save_as: bool = False) -> bool:
        if self.busy:
            return False
        try:
            config = self._collect_config()
        except Exception as error:
            messagebox.showerror(
                "Configuração inválida", redacted_exception(error), parent=self.root
            )
            return False
        preserved_passwords = {
            endpoint_name: str(variables["password"].get())
            for endpoint_name, variables in getattr(
                self, "endpoint_variables", {}
            ).items()
        }
        entered_passwords = {
            endpoint_name: password
            for endpoint_name, password in preserved_passwords.items()
            if password
        }
        if entered_passwords:
            encrypted_package = messagebox.askyesnocancel(
                "Configuração para execução CLI",
                "Incluir as credenciais SQL criptografadas com DPAPI para permitir "
                "execução pontual e não interativa?\n\n"
                "Sim: o JSON será um pacote temporário, válido somente nesta máquina, "
                "e o fluxo CLI sugerido o excluirá após sucesso completo. Em falha ou "
                "cancelamento, ele será preservado para retomada.\n\n"
                "Não: nenhuma senha será salva e a CLI solicitará as credenciais.",
                parent=self.root,
            )
            if encrypted_package is None:
                return False
        else:
            encrypted_package = any(
                str(
                    (((self.base_config.get(name) or {}).get("authentication") or {}).get("password") or {}).get("provider")
                )
                == "windows_dpapi"
                for name in getattr(self, "endpoint_variables", {})
            )
        if encrypted_package:
            try:
                config = build_temporary_execution_package(
                    config,
                    self.base_config,
                    preserved_passwords,
                )
            except Exception as error:
                messagebox.showerror(
                    "Não foi possível proteger as credenciais",
                    redacted_exception(error),
                    parent=self.root,
                )
                return False
        else:
            config.pop("execution_package", None)
            for endpoint_name in getattr(self, "endpoint_variables", {}):
                endpoint = config.get(endpoint_name)
                if endpoint is None or endpoint["authentication"]["type"] == "windows_integrated":
                    continue
                endpoint["authentication"]["password"] = {
                    "provider": "prompt",
                    "reference": None,
                }
        path = self.current_config_path
        if save_as or path is None:
            selected = filedialog.asksaveasfilename(
                parent=self.root,
                title=f"Salvar configuração para GUI/CLI — {PRODUCT_NAME}",
                initialdir=str(RUNTIME_CONFIG_DIRECTORY),
                initialfile=_configuration_filename(config),
                defaultextension=".json",
                filetypes=[("Configuração JSON", "*.json")],
            )
            if not selected:
                return False
            path = Path(selected)
        try:
            write_config_atomic(path, config)
            self._load_config_into_form(config, path=path)
            for endpoint_name, password in preserved_passwords.items():
                self.endpoint_variables[endpoint_name]["password"].set(password)
        except Exception as error:
            messagebox.showerror(
                "Não foi possível salvar", redacted_exception(error), parent=self.root
            )
            return False
        resolved_path = path.resolve()
        self._append_log(
            "Configuração validada e salva para uso na GUI ou CLI: "
            f"{resolved_path}\n"
            f"PowerShell: .\\scripts\\launchers\\invoke-bcp.ps1 plan --config \"{resolved_path}\"\n"
            f"Shell Linux: ./scripts/launchers/invoke-bcp.sh plan --config \"{resolved_path}\""
        )
        self.operation_status.set("Configuração salva")
        return True

    def _validate_button(self) -> None:
        try:
            config = self._collect_config()
        except Exception as error:
            messagebox.showerror(
                "Configuração inválida", redacted_exception(error), parent=self.root
            )
            return
        self.base_config = deepcopy(config)
        messagebox.showinfo(
            "Configuração válida",
            "Todos os campos e contratos foram validados. Nenhuma conexão foi aberta.",
            parent=self.root,
        )
        self._append_log("Configuração validada localmente; nenhuma conexão foi aberta.")

    def _engine(self, config: dict[str, Any]) -> BcpEngine:
        context_path = self.current_config_path or (
            RUNTIME_CONFIG_DIRECTORY / "config.gui.unsaved.json"
        )
        config["_hide_subprocess_windows"] = True
        return BcpEngine(
            config,
            config_path=context_path.resolve(),
            resolver=self.secret_resolver,
            event_sink=lambda name, payload: self.worker_queue.put(
                ("event", (name, dict(payload)))
            ),
        )

    def _prepare_operation(self) -> dict[str, Any] | None:
        if self.busy:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde a conclusão da operação atual.",
                parent=self.root,
            )
            return None
        try:
            config = self._collect_config()
            passwords: dict[str, str] = {}
            for endpoint_name in (
                "source",
                "bronze_destination",
                "landing_destination",
            ):
                endpoint = config.get(endpoint_name)
                if endpoint is None:
                    continue
                auth_type = str(endpoint["authentication"]["type"])
                if auth_type not in {"sql", "windows_credentials"}:
                    continue
                password = str(
                    self.endpoint_variables[endpoint_name]["password"].get()
                )
                if not password:
                    raise ValueError(
                        "Informe a senha mascarada em Conexões -> "
                        + CONNECTION_TAB_TITLES[endpoint_name]
                        + "."
                    )
                passwords[endpoint_name] = password
            self._operation_passwords = passwords
            self.secret_resolver.clear_cache()
            config["_bcp_password_argument"] = True
            return config
        except Exception as error:
            messagebox.showerror(
                "Configuração inválida", redacted_exception(error), parent=self.root
            )
            self.notebook.select(self.general_tab)
            return None

    def _open_planning_progress(self) -> None:
        self._close_planning_progress()
        window = ttk.Toplevel(self.root)
        window.title("Planejamento em andamento")
        window.geometry("680x270")
        window.resizable(False, False)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        self.planning_progress_window = window
        self.planning_progress_value.set(0)
        self.planning_progress_percent.set("0%")
        self.planning_progress_message.set(
            "Preparando conexões e verificações somente leitura…"
        )

        body = ttk.Frame(window, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="PLANEJAMENTO DO FLUXO",
            bootstyle="primary",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "O BulkFlow conectará às bases configuradas, analisará tabelas e "
                "catálogos e confrontará a projeção com os espaços disponíveis no "
                "Destino 01 e nas pastas de exportação/importação dos arquivos BCP."
            ),
            justify="left",
            wraplength=625,
        ).pack(anchor="w", pady=(8, 16))
        progress_row = ttk.Frame(body)
        progress_row.pack(fill="x")
        ttk.Progressbar(
            progress_row,
            variable=self.planning_progress_value,
            maximum=100,
            mode="determinate",
            bootstyle="success-striped",
        ).pack(side="left", fill="x", expand=True)
        ttk.Label(
            progress_row,
            textvariable=self.planning_progress_percent,
            width=6,
            anchor="e",
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(10, 0))
        ttk.Label(
            body,
            textvariable=self.planning_progress_message,
            bootstyle="secondary",
            justify="left",
            wraplength=625,
        ).pack(anchor="w", pady=(14, 0))
        window.grab_set()
        window.update_idletasks()

    def _update_planning_progress(self, percent: int, message: str) -> None:
        value = max(0, min(100, int(percent)))
        self.planning_progress_value.set(value)
        self.planning_progress_percent.set(f"{value}%")
        self.planning_progress_message.set(message)
        window = self.planning_progress_window
        if window is not None and window.winfo_exists():
            window.update_idletasks()

    def _close_planning_progress(self) -> None:
        window = getattr(self, "planning_progress_window", None)
        if window is None:
            return
        try:
            if window.winfo_exists():
                window.grab_release()
                window.destroy()
        except tk.TclError:
            pass
        self.planning_progress_window = None

    def _plan(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        self._invalidate_disk_projection()
        self.secret_resolver.clear_cache()
        self._open_planning_progress()

        def operation() -> str:
            # Resolve all configured credentials up front. In a one-file,
            # no-console executable this callback remains entirely in Tk; it
            # never falls back to getpass/stdin.
            endpoints = (
                "source",
                "landing_destination",
                "bronze_destination",
            )
            self.worker_queue.put(
                ("planning_progress", (5, "Validando as credenciais configuradas…"))
            )
            configured_endpoints = [
                endpoint_name
                for endpoint_name in endpoints
                if config.get(endpoint_name) is not None
            ]
            for endpoint_index, endpoint_name in enumerate(configured_endpoints, start=1):
                endpoint = config.get(endpoint_name)
                self.secret_resolver.resolve_auth(
                    endpoint["authentication"], endpoint_name=endpoint_name
                )
                credential_percent = 5 + int(
                    (endpoint_index / max(len(configured_endpoints), 1)) * 10
                )
                self.worker_queue.put(
                    (
                        "planning_progress",
                        (
                            credential_percent,
                            f"Credenciais validadas ({endpoint_index} de "
                            f"{len(configured_endpoints)})…",
                        ),
                    )
                )
            plans, results = self._engine(config).plan(
                progress_callback=lambda percent, message: self.worker_queue.put(
                    ("planning_progress", (percent, message))
                )
            )
            self.worker_queue.put(
                (
                    "planning_progress",
                    (
                        92,
                        "Analisando o espaço livre nas pastas de exportação e "
                        "importação dos arquivos BCP…",
                    ),
                )
            )
            disk_projection = build_disk_projection_summary(config, plans)
            self.worker_queue.put(
                (
                    "planning_progress",
                    (97, "Preparando a projeção consolidada e os resultados…"),
                )
            )
            output = render_plan_rows(plans)
            invalid = [result for result in results if result.status != TableStatus.PENDING.value]
            if invalid:
                output += "\n\nTabelas que exigem ação:\n" + "\n".join(
                    f"- {item.source_table}: {item.status} — {item.reason or 'sem detalhe'}"
                    for item in invalid
                )
            self.worker_queue.put(
                ("connection_summary", connection_summary_rows(config))
            )
            self.worker_queue.put(("disk_projection", disk_projection))
            self.worker_queue.put(
                (
                    "planning_progress",
                    (100, "Planejamento concluído. Finalizando a apresentação…"),
                )
            )
            time.sleep(0.35)
            return output

        self._start_worker("Planejando", operation)

    def _ddl(self, apply: bool) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        if apply and not messagebox.askyesno(
            "Confirmar aplicação de DDL",
            "Aplicar o DDL idempotente nos destinos selecionados?",
            icon="warning",
            parent=self.root,
        ):
            return
        area_label = self.ddl_area.get()
        areas = {
            "Nenhum": (),
            "Destino 01": ("bronze",),
            "Destino 02": ("landing",),
            "Ambas": ("bronze", "landing"),
        }[area_label]
        if not areas:
            messagebox.showinfo(
                "Nenhum destino selecionado",
                "Ative ao menos um destino na aba Conexões para gerar ou aplicar DDL.",
                parent=self.root,
            )
            return
        output_directory = Path(self.ddl_output.get()).expanduser().resolve()

        def operation() -> str:
            files = self._engine(config).generate_ddl(
                areas=areas,
                output_directory=output_directory,
                apply=apply,
            )
            verb = "aplicado e validado" if apply else "gerado"
            return f"DDL {verb}.\n" + "\n".join(str(path) for path in files)

        self._start_worker("Aplicando DDL" if apply else "Gerando DDL", operation)

    def _run(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        confirmation = build_run_confirmation(
            execute_import=bool(config["execute_import"]),
            destination_database=str(
                self.endpoint_variables["bronze_destination"]["database"].get()
            ),
        )
        if not messagebox.askyesno(
            "Confirmar nova execução",
            confirmation,
            icon="warning",
            parent=self.root,
        ):
            return

        engine = self._engine(config)
        self.active_execution_engine = engine

        def operation() -> str:
            report = engine.run()
            self.worker_queue.put(("execution_id", report.execution_id))
            rendered = render_execution(report)
            if report.cancelled:
                raise _WorkerCancelled(rendered)
            if report.exit_code() != 0:
                raise RuntimeError(rendered)
            return rendered

        self._start_worker("Executando carga", operation, cancellable=True)

    def _resume(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        execution_id = self.execution_id.get().strip()
        if not execution_id:
            messagebox.showerror(
                "ID obrigatório", "Informe o ID da execução a retomar.", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "Confirmar retomada",
            "Retomar pelos checkpoints duráveis da execução informada?",
            icon="warning",
            parent=self.root,
        ):
            return

        engine = self._engine(config)
        self.active_execution_engine = engine

        def operation() -> str:
            report = engine.run(execution_id=execution_id, resume=True)
            rendered = render_execution(report)
            if report.cancelled:
                raise _WorkerCancelled(rendered)
            if report.exit_code() != 0:
                raise RuntimeError(rendered)
            return rendered

        self._start_worker("Retomando execução", operation, cancellable=True)

    def _cancel_execution(self) -> None:
        engine = self.active_execution_engine
        if not self.busy or engine is None or engine.cancellation_requested:
            return
        if not messagebox.askyesno(
            "Confirmar cancelamento",
            "Solicitar o cancelamento? O lote corrente será concluído e os "
            "checkpoints serão gravados antes de a execução parar.",
            icon="warning",
            parent=self.root,
        ):
            return
        engine.request_cancel()
        self.cancel_execution_button.configure(state="disabled")
        self.operation_status.set("Cancelando… aguardando o lote corrente")
        self._append_log(
            "Cancelamento solicitado. Aguardando a conclusão consistente do lote corrente."
        )

    def _import_manifests(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        manifest = self.manifest_path.get().strip()
        if not manifest:
            messagebox.showerror(
                "Manifesto obrigatório",
                "Informe um manifesto, índice ou diretório de manifestos.",
                parent=self.root,
            )
            return
        if not config["execute_import"]:
            messagebox.showerror(
                "Importação desabilitada",
                "Ative 'Importar no destino' antes de importar manifestos.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Confirmar importação",
            "Importar os manifestos validados no destino de dados? A origem não será consultada.",
            icon="warning",
            parent=self.root,
        ):
            return

        def operation() -> str:
            report = self._engine(config).import_manifests(Path(manifest).resolve())
            self.worker_queue.put(("execution_id", report.execution_id))
            rendered = render_execution(report)
            if report.exit_code() != 0:
                raise RuntimeError(rendered)
            return rendered

        self._start_worker("Importando manifestos", operation)

    def _status(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        execution_id = self.execution_id.get().strip()
        if not execution_id:
            messagebox.showerror(
                "ID obrigatório", "Informe o ID da execução para consultar.", parent=self.root
            )
            return

        def operation() -> str:
            status = self._engine(config).status(execution_id)
            if status is None:
                raise RuntimeError("Execução não encontrada no controle local.")
            return json.dumps(redact_structure(status), ensure_ascii=False, indent=2, default=str)

        self._start_worker("Consultando status", operation)

    def _start_worker(
        self,
        label: str,
        operation: Callable[[], str],
        *,
        cancellable: bool = False,
    ) -> None:
        self.busy = True
        self.operation_status.set(label + "…")
        self._update_execution_progress(0)
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.cancel_execution_button.configure(
            state="normal" if cancellable else "disabled"
        )
        self._append_log(f"\n=== {label} ===")

        def target() -> None:
            try:
                result = operation()
            except _WorkerCancelled as cancelled:
                self.worker_queue.put(("cancelled", str(cancelled)))
            except BaseException as error:
                self.worker_queue.put(("failure", redacted_exception(error)))
            else:
                self.worker_queue.put(("success", result))

        threading.Thread(target=target, name="bcp-gui-worker", daemon=True).start()

    def _update_execution_progress(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        self.execution_progress_value.set(value)
        self.execution_progress_percent.set(f"{value}%")

    def _prompt_secret_from_worker(self, label: str) -> str:
        for endpoint_name, password in self._operation_passwords.items():
            if f" para {endpoint_name}:" in label:
                return password
        raise AuthError(
            "Senha não informada na aba Conexões para a operação atual."
        )

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "event":
                    name, event_payload = payload
                    formatted = self._format_event(name, event_payload)
                    if formatted:
                        self._append_log(formatted)
                elif kind == "prerequisites":
                    self.prerequisite_status.set(str(payload))
                elif kind == "connection_summary":
                    self._refresh_connection_summary(list(payload))
                    self.plan_status.set("Planejamento concluído")
                elif kind == "disk_projection":
                    self._refresh_disk_projection(payload)
                elif kind == "planning_progress":
                    percent, message = payload
                    self._update_planning_progress(int(percent), str(message))
                elif kind == "secret_prompt":
                    self._handle_secret_prompt(payload)
                elif kind == "execution_id":
                    self.execution_id.set(str(payload))
                elif kind == "success":
                    self._append_log(str(payload))
                    self._finish_worker(success=True)
                elif kind == "cancelled":
                    self._append_log(str(payload))
                    self._finish_worker(success=False, cancelled=True)
                elif kind == "failure":
                    self._append_log("ERRO: " + str(payload))
                    self._finish_worker(success=False, error=str(payload))
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_worker_queue)

    def _handle_secret_prompt(self, request: _SecretPromptRequest) -> None:
        try:
            value = simpledialog.askstring(
                "Senha necessária",
                request.label,
                show="•",
                parent=self.root,
            )
            if value is None:
                request.error = RuntimeError("cancelado pelo operador")
            else:
                request.value = value
        except BaseException as error:
            request.error = error
        finally:
            request.ready.set()

    def _format_event(self, name: str, payload: Mapping[str, Any]) -> str:
        safe = redact_structure(payload)

        def pretty_json(value: Any, *, indent: int = 2) -> str:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            prefix = " " * indent
            return "\n".join(prefix + line for line in rendered.splitlines())

        if name == "bcp_command":
            verbose = getattr(self, "verbose_logging", None)
            if verbose is None or not bool(verbose.get()):
                return ""
            return f"COMANDO [{safe.get('context')}]: {safe.get('command')}"
        if name == "bulk_import_command":
            verbose = getattr(self, "verbose_logging", None)
            if verbose is None or not bool(verbose.get()):
                return ""
            return f"COMANDO [{safe.get('context')}]: {safe.get('command')}"
        if name == "batch_progress":
            self._update_execution_progress(int(safe.get("percent", 0)))
            if safe.get("status") != "started":
                return ""
            phase = (
                "Exportação" if safe.get("phase") == "export" else "Importação"
            )
            return (
                f"{phase} lote {safe.get('block_number')} de "
                f"{safe.get('total_blocks')}.\n"
                f"Tabela: {safe.get('source_table')}."
            )
        if name == "execution_progress":
            self._update_execution_progress(int(safe.get("percent", 0)))
            return ""
        if name == "table_artifacts_removed":
            return (
                "Artefatos auxiliares removidos após a conclusão da tabela: "
                + str(safe.get("source_table", "<não informada>"))
                + "."
            )
        if name == "database_command":
            return ""
        if name == "table_plan" and safe.get("text"):
            return "Panorama antes da tabela:\n" + str(safe["text"])
        labels = {
            "identity": "Identidade efetiva da origem",
            "destination_identity": "Identidade efetiva do destino",
            "schema_evolution_detected": "Evolução de esquema detectada",
            "schema_evolution_applied": "Evolução de esquema aplicada",
            "schema_evolution_blocked": "Evolução de esquema bloqueada",
            "column_collision_resolved": "ATENÇÃO — colisão de coluna resolvida",
            "cdc_database": "CDC do banco de origem",
            "cdc_table": "CDC da tabela de origem",
            "cdc_retention": "Retenção do CDC",
        }
        return f"{labels.get(name, name)}:\n{pretty_json(safe)}"

    def _finish_worker(
        self,
        *,
        success: bool,
        error: str | None = None,
        cancelled: bool = False,
    ) -> None:
        self._close_planning_progress()
        self.busy = False
        self.active_execution_engine = None
        self.cancel_execution_button.configure(state="disabled")
        if success:
            self._update_execution_progress(100)
        for button in self.action_buttons:
            button.configure(state="normal")
        self._update_ddl_section_state()
        if cancelled:
            self.operation_status.set("Execução cancelada — pronta para retomada")
            messagebox.showinfo(
                "Cancelamento concluído",
                "O lote corrente foi concluído e os checkpoints foram gravados. "
                "O operador pode retomar o processo de onde parou usando o ID da execução.",
                parent=self.root,
            )
        elif success:
            self.operation_status.set("Operação concluída")
            messagebox.showinfo(
                "Operação concluída",
                "A operação terminou. Consulte o acompanhamento para os detalhes.",
                parent=self.root,
            )
        else:
            self.operation_status.set("Operação concluída com erro")
            messagebox.showerror(
                "Falha na operação",
                error or "A operação falhou sem detalhe adicional.",
                parent=self.root,
            )

    def _append_log(self, text: str) -> None:
        follow_tail = bool(getattr(self, "follow_log_tail", True))
        self.log_text.configure(state="normal")
        self.log_text.insert("end", redact_text(text).rstrip() + "\n\n")
        if follow_tail:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_log_follow_state(self, enabled: bool) -> None:
        self.follow_log_tail = enabled
        status = getattr(self, "log_follow_status", None)
        if status is not None:
            status.set(
                "Acompanhamento automático: ativo"
                if enabled
                else "Acompanhamento automático: pausado"
            )

    def _sync_log_follow_from_view(self) -> None:
        _first, last = self.log_text.yview()
        self._set_log_follow_state(float(last) >= 0.999)

    def _on_log_view_changed(self, _event: Any = None) -> None:
        self.root.after_idle(self._sync_log_follow_from_view)

    def _on_log_scrollbar(self, *arguments: str) -> None:
        self.log_text.yview(*arguments)
        self._sync_log_follow_from_view()

    def _follow_latest_log(self) -> None:
        self.log_text.see("end")
        self._set_log_follow_state(True)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._set_log_follow_state(True)

    def _close(self) -> None:
        if self.busy:
            messagebox.showwarning(
                "Operação em andamento",
                "Aguarde a operação terminar. Fechar agora poderia interromper o processo BCP.",
                parent=self.root,
            )
            return
        self.secret_resolver.clear_cache()
        self.root.destroy()


def main(config_path: Path | None = None) -> int:
    """Launch the graphical adapter.  Packaging as an executable is deferred."""

    root = ttk.Window(themename="flatly")
    BcpGuiApplication(root, initial_config_path=config_path)
    root.mainloop()
    return 0


__all__ = ["BcpGuiApplication", "TableDialog", "main"]
