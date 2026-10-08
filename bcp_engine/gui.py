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
import threading
from typing import Any, Callable, Mapping

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

import ttkbootstrap as ttk
from ttkbootstrap.widgets import ToolTip

from .auth import AuthError, SecretResolver
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
from .prerequisites import inspect_prerequisites
from .reporting import format_bytes, render_execution, render_plan_rows
from .runtime import (
    ensure_runtime_directories,
    runtime_config_directory,
    runtime_data_root,
    runtime_ddl_directory,
)
from .util import redact_structure, redact_text, redacted_exception, stable_json
from .watermark_validation import (
    WATERMARK_VALIDATION_GUIDANCE_PT_BR,
    render_watermark_validation_sql,
    watermark_validation_filename,
)


RUNTIME_DATA_ROOT = runtime_data_root()
RUNTIME_DDL_DIRECTORY = runtime_ddl_directory()
RUNTIME_CONFIG_DIRECTORY = runtime_config_directory()
PRODUCT_NAME = "BulkFlow - SQL Server Data Export & Load"
WINDOW_TITLE = PRODUCT_NAME
DEFAULT_VALUE_COLOR = "#e8f3ff"
_NO_DEFAULT = object()


FIELD_HELP: dict[str, str] = {
    "perimeter": "Perímetro operacional. Sugere o usuário SQL, mas cada conexão continua independente.",
    "execute_import": "Quando habilitado, exporta da Origem e importa os arquivos confirmados na Bronze.",
    "create_structure_if_needed": (
        "Controla o provisionamento automático da Bronze durante executar, retomar "
        "ou importar manifestos. Desmarcado, exige estrutura compatível já existente. "
        "O botão explícito Aplicar DDL continua aplicando Bronze e Landing "
        "independentemente desta opção. O padrão é habilitado."
    ),
    "allow_schema_evolution": "Permite somente a adição segura de novas colunas encontradas na Origem.",
    "delete_confirmed_files": "Remove arquivos exportados apenas depois da confirmação durável da importação.",
    "continue_after_table_error": "Registra a falha da tabela e continua o processamento das tabelas seguintes.",
    "require_watermark_index": "Exige índice compatível com a marca d'água antes de autorizar a extração.",
    "executor_directory": "Diretório local ou compartilhado em que o programa exporta os arquivos BCP.",
    "destination_sql_directory": "Diretório pelo qual o SQL Server da Bronze acessa os arquivos durante a importação.",
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
        "Esquema fixo dbo da Bronze que armazena o controle persistente das "
        "execuções. Este valor não pode ser alterado."
    ),
    "endpoint_enabled": "Inclui este destino na configuração e nas operações estruturais.",
    "instance": "Nome DNS, host ou instância SQL Server, sem o número da porta.",
    "port": "Porta TCP obrigatória do serviço SQL Server, entre 1 e 65535.",
    "database": "Banco de dados usado por esta conexão.",
    "schema": "Esquema SQL. Nos destinos, o nome é normalizado para minúsculas.",
    "odbc_dsn": "DSN ODBC opcional. Quando preenchido, ele define a rota efetiva da conexão.",
    "structure_profile": "Perfil JSON que define metadados, índices e estrutura técnica do destino.",
    "authentication_type": "Método de autenticação usado exclusivamente por este ambiente.",
    "username": "Usuário da conexão. Pode ser diferente em Origem, Landing e Bronze.",
    "domain": "Domínio Windows; editável somente para o tipo Credencial Windows.",
    "encrypt": "Solicita conexão criptografada ao SQL Server.",
    "trust_server_certificate": "Aceita o certificado apresentado sem validar sua cadeia de confiança.",
    "source_database": (
        "Banco de dados da tabela na Origem. Nesta versão, deve coincidir com o "
        "banco configurado na conexão de Origem; altere primeiro a aba Conexões."
    ),
    "source_table": "Nome da tabela existente no banco de Origem.",
    "destination_database": (
        "Banco de dados da tabela na Bronze. Nesta versão, deve coincidir com o "
        "banco configurado na conexão Bronze; altere primeiro a aba Conexões."
    ),
    "destination_table": (
        "Nome da tabela nos destinos. O padrão é banco_origem_tabela_origem em "
        "minúsculas e acompanha a origem enquanto não for personalizado."
    ),
    "source_schema": "Esquema obrigatório da tabela na Origem.",
    "destination_schema": "Esquema obrigatório da tabela na Bronze e na Landing.",
    "table_rows_per_block": (
        "Exibe o tamanho global do lote. O valor só é salvo como override desta "
        "tabela quando for diferente do valor global."
    ),
    "table_structure_profile": (
        "Exibe o perfil da Bronze. O valor só é salvo como override desta tabela "
        "quando for diferente do perfil do destino."
    ),
    "watermark": (
        "Nome da coluna, ou nomes separados por vírgula, usados na paginação. "
        "A ordem é sempre ascendente; não informe ASC ou DESC."
    ),
    "watermark_validation_script": WATERMARK_VALIDATION_GUIDANCE_PT_BR,
    "enable_cdc": "Solicita ativação e confirmação do CDC antes de exportar esta tabela.",
    "partition_enabled": "Habilita particionamento da tabela de destino pela coluna informada.",
    "partition_column": "Coluna datetime2 usada no particionamento mensal; sugestão: dh_carga.",
    "ddl_area": "Seleciona quais destinos recebem os scripts de estrutura.",
    "ddl_output": "Diretório em que os scripts DDL gerados serão gravados.",
    "execution_id": "UUID usado para consultar ou retomar uma execução existente.",
    "manifest_path": "Manifesto, índice ou diretório para uma importação independente na Bronze.",
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


@dataclass
class _SecretPromptRequest:
    label: str
    ready: threading.Event
    value: str | None = None
    error: BaseException | None = None


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
    ) -> None:
        self.destination_area = destination_area
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
        row = self._entry(
            body, row, "Perfil de estrutura (opcional; usa o destino)", "structure_profile",
            browse=True,
            help_key="table_structure_profile",
            default_value=self.default_structure_profile,
        )
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
            text="Ativar CDC nesta tabela",
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
            landing = ttk.Labelframe(
                body,
                text="Mapeamento Landing (obrigatório)",
                padding=12,
            )
            landing.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(12, 8))
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
            ttk.Button(
                parent,
                text="Selecionar…",
                command=lambda: self._browse_profile(name),
                bootstyle="outline-secondary",
            ).grid(row=row, column=2, padx=(8, 0), pady=6)
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
        self.partition_entry.configure(state="normal" if enabled else "disabled")
        self._update_partition_style()

    def _browse_profile(self, name: str) -> None:
        selected = filedialog.askopenfilename(
            parent=self.window,
            title="Selecionar perfil de estrutura",
            filetypes=[("Arquivos JSON", "*.json"), ("Todos os arquivos", "*.*")],
        )
        if selected:
            self.variables[name].set(selected)

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
        self.root.geometry("1240x860")
        self.root.minsize(1040, 720)
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
        self.action_buttons: list[ttk.Button] = []
        self._default_widgets: list[
            tuple[tk.Misc, tk.Variable, Any | Callable[[], Any], str, bool]
        ] = []
        self._domain_entries: dict[str, ttk.Entry] = {}
        self._suspend_schema_sync = False
        self._previous_bronze_schema = ""
        self._suspend_directory_sync = False
        self._previous_executor_directory = ""
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
        self.endpoint_enabled: dict[str, tk.BooleanVar] = {
            "source": tk.BooleanVar(value=True),
            "bronze_destination": tk.BooleanVar(value=True),
            "landing_destination": tk.BooleanVar(value=True),
        }
        self.endpoint_variables: dict[str, dict[str, tk.Variable]] = {}
        for key in self.endpoint_enabled:
            self.endpoint_variables[key] = {
                "instance": tk.StringVar(),
                "port": tk.StringVar(),
                "database": tk.StringVar(),
                "schema": tk.StringVar(),
                "structure_profile": tk.StringVar(),
                "odbc_dsn": tk.StringVar(),
                "authentication_type": tk.StringVar(),
                "username": tk.StringVar(),
                "domain": tk.StringVar(),
                "secret_provider": tk.StringVar(),
                "secret_reference": tk.StringVar(),
                "encrypt": tk.BooleanVar(value=True),
                "trust_server_certificate": tk.BooleanVar(value=False),
            }
        self.ddl_area = tk.StringVar(value="Ambos")
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
        self.export_disk_assessment = tk.StringVar(
            value="Exportação: aguardando planejamento."
        )
        self.import_disk_assessment = tk.StringVar(
            value="Importação: aguardando planejamento."
        )

        for name in ("max_file_bytes", "minimum_free_space_bytes"):
            self.global_variables[name].trace_add(
                "write", lambda *_args, field=name: self._update_byte_display(field)
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
        for name in (
            "destination_sql_directory",
            "minimum_free_space_bytes",
            "safety_factor",
            "execute_import",
            "delete_confirmed_files",
            "rows_per_block",
            "maximum_sample_rows",
            "row_count_method",
            "keyless_direct_load_max_rows",
        ):
            self.global_variables[name].trace_add(
                "write", lambda *_args: self._invalidate_disk_projection()
            )
        self.endpoint_variables["bronze_destination"]["schema"].trace_add(
            "write", self._on_bronze_schema_changed
        )
        for endpoint in self.endpoint_variables.values():
            for name in (
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

    def _build_window(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(12, 10))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text=PRODUCT_NAME, font=("Segoe UI", 16, "bold")).pack(
            side="left", padx=(0, 18)
        )
        ttk.Button(toolbar, text="Nova", command=self._new_configuration, bootstyle="secondary").pack(
            side="left", padx=3
        )
        ttk.Button(toolbar, text="Abrir…", command=self._open_configuration, bootstyle="secondary").pack(
            side="left", padx=3
        )
        ttk.Button(
            toolbar,
            text="Salvar para GUI/CLI",
            command=self._save_configuration,
            bootstyle="primary",
        ).pack(
            side="left", padx=3
        )
        ttk.Button(
            toolbar,
            text="Salvar como…",
            command=lambda: self._save_configuration(save_as=True),
            bootstyle="outline-primary",
        ).pack(side="left", padx=3)
        ttk.Button(
            toolbar,
            text="Validar configuração",
            command=self._validate_button,
            bootstyle="outline-success",
        ).pack(side="left", padx=(16, 3))
        ttk.Label(toolbar, textvariable=self.config_path_label, bootstyle="secondary").pack(
            side="right", padx=8
        )

        self.notebook = ttk.Notebook(self.root, padding=(10, 0, 10, 8))
        self.notebook.pack(fill="both", expand=True)
        self.general_tab = ttk.Frame(self.notebook, padding=14)
        self.connections_tab = ttk.Frame(self.notebook, padding=14)
        self.tables_tab = ttk.Frame(self.notebook, padding=14)
        self.operations_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.general_tab, text="1. Geral")
        self.notebook.add(self.connections_tab, text="2. Conexões")
        self.notebook.add(self.tables_tab, text="3. Tabelas")
        self.notebook.add(self.operations_tab, text="4. Executar e acompanhar")
        self._build_general_tab()
        self._build_connections_tab()
        self._build_tables_tab()
        self._build_operations_tab()

        footer = ttk.Frame(self.root, padding=(12, 4, 12, 10))
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=180)
        self.progress.pack(side="left")
        ttk.Label(footer, textvariable=self.operation_status).pack(side="left", padx=10)

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
            perimeter, self.global_variables["perimeter"], "DREADS", "combobox"
        )
        perimeter.bind("<<ComboboxSelected>>", self._on_perimeter_selected)
        row += 1
        ttk.Label(flow, text="Fluxo de dados").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Label(
            flow,
            text="Origem → Bronze (Landing recebe somente DDL/evolução)",
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
            ("continue_after_table_error", "Continuar após erro de tabela"),
            ("require_watermark_index", "Exigir índice compatível com a marca d'água"),
        ):
            container = ttk.Frame(flow)
            container.grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
            control = ttk.Checkbutton(
                container,
                text=label,
                variable=self.global_variables[name],
                bootstyle="round-toggle",
            )
            control.pack(side="left")
            _attach_help(control, name)
            help_marker = ttk.Label(container, text=" ? ", bootstyle="info", cursor="hand2")
            help_marker.pack(side="left", padx=(3, 0))
            _attach_help(help_marker, name)
            row += 1

        row = 0
        row = self._form_entry(
            storage,
            row,
            "Diretório de exportação dos arquivos *",
            self.global_variables["executor_directory"],
            browse="directory",
            help_key="executor_directory",
        )
        row = self._form_entry(
            storage,
            row,
            "Diretório de importação dos arquivos *",
            self.global_variables["destination_sql_directory"],
            help_key="destination_sql_directory",
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
            entry = ttk.Entry(advanced, textvariable=self.global_variables[name])
            entry.grid(
                row=row, column=column + 1, sticky="ew", padx=(8, 20 if column == 0 else 0), pady=5
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
    ) -> int:
        _help_label(parent, label, help_key).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(
            row=row, column=1, sticky="ew", padx=(8, 0), pady=5
        )
        _attach_help(entry, help_key)
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

    def _select_directory(self, variable: tk.Variable) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="Selecionar diretório")
        if selected:
            variable.set(selected)

    def _build_connections_tab(self) -> None:
        notebook = ttk.Notebook(self.connections_tab)
        notebook.pack(fill="both", expand=True)
        for key, title, source in (
            ("source", "Origem (obrigatória)", True),
            ("bronze_destination", "Destino Bronze (dados e estrutura)", False),
            ("landing_destination", "Destino Landing (somente estrutura)", False),
        ):
            frame = ttk.Frame(notebook, padding=14)
            notebook.add(frame, text=title)
            self._build_endpoint_frame(frame, key=key, source=source)

    def _build_endpoint_frame(self, frame: ttk.Frame, *, key: str, source: bool) -> None:
        variables = self.endpoint_variables[key]
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
            _attach_help(enabled, "endpoint_enabled")
            enabled_help = ttk.Label(
                enabled_container, text=" ? ", bootstyle="info", cursor="hand2"
            )
            enabled_help.pack(side="left", padx=(3, 0))
            _attach_help(enabled_help, "endpoint_enabled")
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
                "structure_profile": "landing",
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
            entry = ttk.Entry(frame, textvariable=variables[name])
            entry.grid(
                row=current_row,
                column=column + 1,
                sticky="ew",
                padx=(8, 20 if column == 0 else 0),
                pady=6,
            )
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
                    entry, variables[name], defaults[name], "entry"
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
        _attach_help(username, "username")
        self._register_default_widget(
            username,
            variables["username"],
            self._current_username_default,
            "entry",
        )
        _help_label(auth, "Domínio (credencial Windows)", "domain").grid(
            row=1, column=0, sticky="w", pady=5
        )
        domain = ttk.Entry(auth, textvariable=variables["domain"])
        domain.grid(
            row=1, column=1, sticky="ew", padx=(8, 20), pady=5
        )
        _attach_help(domain, "domain")
        self._domain_entries[key] = domain
        ttk.Label(
            auth,
            text=(
                "A senha é solicitada de forma mascarada ao planejar e nunca é gravada "
                "no arquivo de configuração. Provedores definidos em um JSON existente são preservados."
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
        _attach_help(trust, "trust_server_certificate")
        trust_help = ttk.Label(tls, text=" ? ", bootstyle="info", cursor="hand2")
        trust_help.pack(side="left", padx=(3, 0))
        _attach_help(trust_help, "trust_server_certificate")
        ttk.Label(frame, text="* campo obrigatório", bootstyle="secondary").grid(
            row=row + 2, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        self._update_auth_state(key)

    def _update_auth_state(self, key: str) -> None:
        displayed = str(self.endpoint_variables[key]["authentication_type"].get())
        authentication_type = AUTHENTICATION_LABELS.get(displayed, "")
        state = "normal" if authentication_type == "windows_credentials" else "disabled"
        entry = self._domain_entries.get(key)
        if entry is not None:
            entry.configure(state=state)

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
            "destination": "Destino Bronze (banco.esquema.tabela)",
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
            self.table_tree.heading(name, text=headings[name])
            anchor = "center" if name in {"order", "cdc", "batch"} else "w"
            self.table_tree.column(
                name,
                width=widths[name],
                minwidth=widths[name],
                anchor=anchor,
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
        return "bronze"

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

    def _on_bronze_schema_changed(self, *_args: object) -> None:
        if self._suspend_schema_sync:
            return
        bronze = str(self.endpoint_variables["bronze_destination"]["schema"].get())
        landing_variable = self.endpoint_variables["landing_destination"]["schema"]
        landing = str(landing_variable.get())
        if not landing.strip() or landing == self._previous_bronze_schema:
            self._suspend_schema_sync = True
            try:
                landing_variable.set(bronze)
            finally:
                self._suspend_schema_sync = False
        self._previous_bronze_schema = bronze
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

    def _table_dialog_defaults(self) -> dict[str, str]:
        return {
            "default_source_database": str(
                self.endpoint_variables["source"]["database"].get()
            ),
            "default_source_schema": str(
                self.endpoint_variables["source"]["schema"].get()
            ),
            "default_destination_database": str(
                self.endpoint_variables["bronze_destination"]["database"].get()
            ),
            "default_destination_schema": str(
                self.endpoint_variables["bronze_destination"]["schema"].get()
            ),
            "default_rows_per_block": str(
                self.global_variables["rows_per_block"].get()
            ),
            "default_structure_profile": str(
                self.endpoint_variables["bronze_destination"][
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
        tab.rowconfigure(5, weight=1)

        prerequisites = ttk.Labelframe(
            tab, text="1. Atender aos pré-requisitos do executor", padding=10
        )
        prerequisites.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        prerequisites.columnconfigure(0, weight=1)
        ttk.Label(prerequisites, textvariable=self.odbc_requirement).grid(
            row=0, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Label(prerequisites, textvariable=self.bcp_requirement).grid(
            row=1, column=0, sticky="w", padx=4, pady=2
        )
        verify = ttk.Button(
            prerequisites,
            text="Verificar pré-requisitos",
            command=self._verify_prerequisites,
            bootstyle="info",
        )
        verify.grid(row=0, column=1, rowspan=2, padx=8, pady=4)
        ttk.Label(
            prerequisites, textvariable=self.prerequisite_status, bootstyle="secondary"
        ).grid(row=0, column=2, rowspan=2, sticky="w", padx=8)

        ttk.Separator(prerequisites).grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=(8, 6)
        )
        ttk.Label(
            prerequisites,
            textvariable=self.disk_projection_overview,
            wraplength=1130,
            bootstyle="primary",
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        ttk.Label(
            prerequisites,
            textvariable=self.export_disk_assessment,
            wraplength=1130,
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        ttk.Label(
            prerequisites,
            textvariable=self.import_disk_assessment,
            wraplength=1130,
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        ttk.Label(
            prerequisites,
            text=(
                "Exportação e importação são duas visões dos mesmos arquivos BCP; "
                "o espaço nunca é somado duas vezes."
            ),
            bootstyle="secondary",
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=4, pady=(2, 4))

        projection_columns = (
            "source",
            "estimated_rows",
            "raw_bytes",
            "protected_bytes",
            "projection_state",
        )
        projection_frame = ttk.Frame(prerequisites)
        projection_frame.columnconfigure(0, weight=1)
        projection_frame.grid(
            row=7, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 4)
        )
        self.disk_projection_tree = ttk.Treeview(
            projection_frame,
            columns=projection_columns,
            show="headings",
            height=4,
        )
        projection_headings = {
            "source": "Tabela de origem",
            "estimated_rows": "Linhas estimadas",
            "raw_bytes": "BCP bruto",
            "protected_bytes": "BCP + margem",
            "projection_state": "Estimativa",
        }
        projection_widths = {
            "source": 360,
            "estimated_rows": 120,
            "raw_bytes": 140,
            "protected_bytes": 140,
            "projection_state": 210,
        }
        for name in projection_columns:
            self.disk_projection_tree.heading(name, text=projection_headings[name])
            self.disk_projection_tree.column(
                name,
                width=projection_widths[name],
                anchor="w" if name in {"source", "projection_state"} else "e",
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
            tab, text="2. Informar credenciais e planejar", padding=10
        )
        planning.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        planning.columnconfigure(1, weight=1)
        plan = ttk.Button(planning, text="Planejar", command=self._plan, bootstyle="primary")
        plan.grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(
            planning,
            text=(
                "Senhas necessárias serão solicitadas separadamente e mascaradas "
                "para Origem, Landing e Bronze."
            ),
            bootstyle="secondary",
        ).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(planning, textvariable=self.plan_status, bootstyle="secondary").grid(
            row=0, column=2, sticky="e", padx=8
        )
        summary_columns = ("environment", "username", "instance", "port", "database")
        self.connection_tree = ttk.Treeview(
            planning, columns=summary_columns, show="headings", height=3
        )
        summary_headings = {
            "environment": "Ambiente",
            "username": "Usuário",
            "instance": "Instância SQL Server",
            "port": "Porta",
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
            self.connection_tree.heading(name, text=summary_headings[name])
            self.connection_tree.column(
                name,
                width=summary_widths[name],
                anchor="center" if name in {"environment", "port"} else "w",
            )
        self.connection_tree.grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 2)
        )

        ddl = ttk.Labelframe(tab, text="3. Gerar e aplicar a estrutura", padding=10)
        ddl.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ddl.columnconfigure(3, weight=1)
        _help_label(ddl, "Área do DDL", "ddl_area").grid(
            row=0, column=0, padx=4, pady=4, sticky="w"
        )
        ddl_area = ttk.Combobox(
            ddl,
            textvariable=self.ddl_area,
            values=["Bronze", "Landing", "Ambos"],
            state="readonly",
            width=12,
        )
        ddl_area.grid(row=0, column=1, padx=4, pady=4)
        _attach_help(ddl_area, "ddl_area")
        self._register_default_widget(ddl_area, self.ddl_area, "Ambos", "combobox")
        _help_label(ddl, "Diretório dos scripts", "ddl_output").grid(
            row=0, column=2, padx=(18, 4), pady=4, sticky="w"
        )
        ddl_output = ttk.Entry(ddl, textvariable=self.ddl_output, width=36)
        ddl_output.grid(row=0, column=3, padx=4, pady=4, sticky="ew")
        _attach_help(ddl_output, "ddl_output")
        self._register_default_widget(
            ddl_output, self.ddl_output, str(RUNTIME_DDL_DIRECTORY), "entry"
        )
        generate = ttk.Button(
            ddl, text="Gerar DDL", command=lambda: self._ddl(False), bootstyle="secondary"
        )
        generate.grid(
            row=0, column=4, padx=4, pady=4
        )
        apply_ddl = ttk.Button(
            ddl, text="Aplicar DDL", command=lambda: self._ddl(True), bootstyle="warning"
        )
        apply_ddl.grid(
            row=0, column=5, padx=4, pady=4
        )

        transfer = ttk.Labelframe(
            tab, text="4. Exportar da Origem e importar na Bronze", padding=10
        )
        transfer.grid(row=3, column=0, sticky="ew", pady=(0, 8))
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

        notice = ttk.Label(
            tab,
            text=(
                "Operações destrutivas exigem confirmação. A janela permanece responsiva, "
                "mas uma operação em andamento não pode ser cancelada pela interface."
            ),
            bootstyle="secondary",
            wraplength=1100,
        )
        notice.grid(row=4, column=0, sticky="w", pady=(2, 6))
        log_frame = ttk.Labelframe(tab, text="Acompanhamento", padding=8)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(
            log_frame,
            wrap="word",
            font=("Cascadia Mono", 10),
            state="disabled",
            background="#111827",
            foreground="#e5e7eb",
            insertbackground="#e5e7eb",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ttk.Button(
            log_frame,
            text="Limpar acompanhamento",
            command=self._clear_log,
            bootstyle="outline-secondary",
        ).grid(row=1, column=0, sticky="e", pady=(8, 0))

        self.action_buttons.extend(
            [verify, plan, generate, apply_ddl, run, resume, status, import_manifests]
        )
        self._update_prerequisite_descriptions()

    def _invalidate_disk_projection(self) -> None:
        """Discard stale sizing evidence without touching disk or SQL."""

        overview = getattr(self, "disk_projection_overview", None)
        export = getattr(self, "export_disk_assessment", None)
        import_ = getattr(self, "import_disk_assessment", None)
        if overview is not None:
            overview.set(
                "Projeção de disco: execute Planejar para recalcular todas as tabelas."
            )
        if export is not None:
            export.set("Exportação: aguardando planejamento.")
        if import_ is not None:
            import_.set("Importação: aguardando planejamento.")
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
        peak = summary.get("predicted_peak_bytes")
        factor = summary.get("safety_factor")
        minimum = summary.get("minimum_free_bytes")
        if raw is None or protected is None:
            self.disk_projection_overview.set(
                "Projeção consolidada INDISPONÍVEL: "
                f"{unknown} tabela(s) sem estimativa; subtotal conhecido "
                f"{format_bytes(summary.get('known_raw_bytes'))} bruto / "
                f"{format_bytes(summary.get('known_protected_bytes'))} com margem."
            )
        else:
            peak_text = format_bytes(peak)
            if peak is None:
                peak_text += " (incompleto por dependência anterior)"
            self.disk_projection_overview.set(
                "Projeção consolidada: "
                f"bruto {format_bytes(raw)} | com fator {factor:g}: "
                f"{format_bytes(protected)} | pico operacional {peak_text} | "
                f"reserva mínima {format_bytes(minimum)}."
            )

        state_labels = {
            "sufficient": "SUFICIENTE",
            "insufficient": "INSUFICIENTE",
            "unavailable": "INDISPONÍVEL",
            "not_applicable": "NÃO APLICÁVEL",
        }

        def render_directory(label: str, value: Mapping[str, Any]) -> str:
            state = str(value.get("state", "unavailable"))
            total_fits = value.get("total_protected_fits")
            retention_state = (
                "NÃO APLICÁVEL"
                if state == "not_applicable"
                else "INDISPONÍVEL"
                if total_fits is None
                else "SUFICIENTE"
                if total_fits
                else "INSUFICIENTE"
            )
            text = (
                f"{label} (visão do executor): "
                f"{value.get('path') or '<não informado>'} | "
                f"livre {format_bytes(value.get('free_bytes'))} | "
                f"saldo operacional após pico e reserva "
                f"{format_bytes(value.get('balance_after_peak_bytes'))} | "
                f"saldo se todos os arquivos protegidos forem retidos "
                f"{format_bytes(value.get('balance_after_total_protected_bytes'))} | "
                f"retenção integral: {retention_state} | operação: "
                f"{state_labels.get(state, state.upper())}"
            )
            if value.get("error"):
                text += " — " + redact_text(str(value["error"]))
            return text

        directories = summary.get("directories") or {}
        self.export_disk_assessment.set(
            render_directory("Exportação", directories.get("export") or {})
        )
        self.import_disk_assessment.set(
            render_directory("Importação", directories.get("import") or {})
        )

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
            state_text = (
                "Calculada"
                if state == "available"
                else f"Indisponível ({row.get('status') or 'sem estado'})"
            )
            tree.insert(
                "",
                "end",
                iid=f"disk-projection-{index}",
                values=(
                    row.get("source") or "indisponível",
                    self._format_projection_rows(row.get("estimated_rows")),
                    format_bytes(row.get("raw_bytes")),
                    protected_text,
                    state_text,
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
                }
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

    def _endpoint_form_values(self, key: str) -> dict[str, Any]:
        variables = self.endpoint_variables[key]
        result = {name: variable.get() for name, variable in variables.items()}
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
        return result

    def _collect_config(self) -> dict[str, Any]:
        candidate = deepcopy(self.base_config)
        candidate["config_version"] = 2
        candidate["perimeter"] = str(self.global_variables["perimeter"].get())
        destination_area = "bronze"
        candidate["active_destination"] = "bronze"
        for name in (
            "execute_import",
            "create_structure_if_needed",
            "allow_schema_evolution",
            "delete_confirmed_files",
            "continue_after_table_error",
        ):
            candidate[name] = bool(self.global_variables[name].get())
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
        # O controle SQL é um contrato físico fixo da Bronze. Reafirmar o
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
            if self.endpoint_enabled[key].get():
                candidate[key] = build_endpoint(
                    self._endpoint_form_values(key), source=False
                )
            else:
                candidate.pop(key, None)
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
            "delete_confirmed_files",
            "continue_after_table_error",
        ):
            self.global_variables[name].set(bool(normalized[name]))
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
        self._previous_bronze_schema = str(
            self.endpoint_variables["bronze_destination"]["schema"].get()
        )
        self._last_perimeter = str(normalized["perimeter"])
        self.table_forms = []
        if path is None:
            # A nova configuração é um rascunho visual: destinos sem
            # esquema e nenhuma linha fictícia na grade. A validação oficial
            # continua sendo executada antes de salvar ou operar.
            self._suspend_schema_sync = True
            try:
                self.endpoint_variables["bronze_destination"]["schema"].set("")
                self.endpoint_variables["landing_destination"]["schema"].set("")
            finally:
                self._suspend_schema_sync = False
            self._previous_bronze_schema = ""
        else:
            source_endpoint = normalized["source"]
            bronze_endpoint = normalized.get("bronze_destination", {})
            for index, table in enumerate(normalized["tables"]):
                form = table_to_form(
                    table,
                    default_source_database=source_endpoint["database"],
                    default_source_schema=source_endpoint["schema"],
                    default_destination_database=bronze_endpoint.get("database", ""),
                    default_destination_schema=bronze_endpoint.get("schema", ""),
                    default_rows_per_block=normalized["rows_per_block"],
                    default_structure_profile=bronze_endpoint.get(
                        "structure_profile", ""
                    ),
                )
                if "metadata_mapping" not in table:
                    form["aud_ccid_value"] = str(index + 1)
                self.table_forms.append(form)
        self._refresh_table_tree()
        self._refresh_connection_summary([])
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
            "domain": auth.get("domain", ""),
            "secret_provider": label_for_code(
                SECRET_PROVIDER_LABELS, str(secret.get("provider", "env"))
            ),
            "secret_reference": secret.get("reference") or "",
            "encrypt": data.get("tls", {}).get("encrypt", True),
            "trust_server_certificate": data.get("tls", {}).get(
                "trust_server_certificate", False
            ),
        }
        for name, value in values.items():
            variables[name].set(value)
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
        path = self.current_config_path
        if save_as or path is None:
            selected = filedialog.asksaveasfilename(
                parent=self.root,
                title=f"Salvar configuração para GUI/CLI — {PRODUCT_NAME}",
                initialdir=str(RUNTIME_CONFIG_DIRECTORY),
                initialfile="config.transferencia.json",
                defaultextension=".json",
                filetypes=[("Configuração JSON", "*.json")],
            )
            if not selected:
                return False
            path = Path(selected)
        try:
            write_config_atomic(path, config)
            self._load_config_into_form(config, path=path)
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
            return self._collect_config()
        except Exception as error:
            messagebox.showerror(
                "Configuração inválida", redacted_exception(error), parent=self.root
            )
            self.notebook.select(self.general_tab)
            return None

    def _plan(self) -> None:
        config = self._prepare_operation()
        if config is None:
            return
        self._invalidate_disk_projection()
        self.secret_resolver.clear_cache()

        def operation() -> str:
            # Resolve all configured credentials up front. In a one-file,
            # no-console executable this callback remains entirely in Tk; it
            # never falls back to getpass/stdin.
            for endpoint_name in (
                "source",
                "landing_destination",
                "bronze_destination",
            ):
                endpoint = config.get(endpoint_name)
                if endpoint is None:
                    continue
                self.secret_resolver.resolve_auth(
                    endpoint["authentication"], endpoint_name=endpoint_name
                )
            plans, results = self._engine(config).plan()
            disk_projection = build_disk_projection_summary(config, plans)
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
            "Bronze": ("bronze",),
            "Landing": ("landing",),
            "Ambos": ("bronze", "landing"),
        }[area_label]
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
        action = "exportar e importar os dados" if config["execute_import"] else "exportar os dados"
        if not messagebox.askyesno(
            "Confirmar nova execução",
            f"Deseja {action} em uma nova execução?",
            icon="warning",
            parent=self.root,
        ):
            return

        def operation() -> str:
            report = self._engine(config).run()
            self.worker_queue.put(("execution_id", report.execution_id))
            rendered = render_execution(report)
            if report.exit_code() != 0:
                raise RuntimeError(rendered)
            return rendered

        self._start_worker("Executando carga", operation)

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

        def operation() -> str:
            report = self._engine(config).run(execution_id=execution_id, resume=True)
            rendered = render_execution(report)
            if report.exit_code() != 0:
                raise RuntimeError(rendered)
            return rendered

        self._start_worker("Retomando execução", operation)

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
            "Importar os manifestos validados na Bronze? A origem não será consultada.",
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

    def _start_worker(self, label: str, operation: Callable[[], str]) -> None:
        self.busy = True
        self.operation_status.set(label + "…")
        self.progress.start(12)
        for button in self.action_buttons:
            button.configure(state="disabled")
        self._append_log(f"\n=== {label} ===")

        def target() -> None:
            try:
                result = operation()
            except BaseException as error:
                self.worker_queue.put(("failure", redacted_exception(error)))
            else:
                self.worker_queue.put(("success", result))

        threading.Thread(target=target, name="bcp-gui-worker", daemon=True).start()

    def _prompt_secret_from_worker(self, label: str) -> str:
        localized_label = label
        for internal_name, displayed_name in (
            ("source", "Origem"),
            ("landing_destination", "Landing"),
            ("bronze_destination", "Bronze"),
        ):
            localized_label = localized_label.replace(
                f" para {internal_name}: ", f" para {displayed_name}: "
            )
        request = _SecretPromptRequest(label=localized_label, ready=threading.Event())
        self.worker_queue.put(("secret_prompt", request))
        request.ready.wait()
        if request.error is not None:
            raise AuthError("A entrada da senha foi cancelada") from request.error
        if request.value is None:
            raise AuthError("A entrada da senha foi cancelada")
        return request.value

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "event":
                    name, event_payload = payload
                    self._append_log(self._format_event(name, event_payload))
                elif kind == "prerequisites":
                    self.prerequisite_status.set(str(payload))
                elif kind == "connection_summary":
                    self._refresh_connection_summary(list(payload))
                    self.plan_status.set("Planejamento concluído")
                elif kind == "disk_projection":
                    self._refresh_disk_projection(payload)
                elif kind == "secret_prompt":
                    self._handle_secret_prompt(payload)
                elif kind == "execution_id":
                    self.execution_id.set(str(payload))
                elif kind == "success":
                    self._append_log(str(payload))
                    self._finish_worker(success=True)
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
        if name == "table_plan" and safe.get("text"):
            return "Panorama antes da tabela:\n" + str(safe["text"])
        labels = {
            "identity": "Identidade efetiva da origem",
            "destination_identity": "Identidade efetiva do destino",
            "schema_evolution_detected": "Evolução de esquema detectada",
            "schema_evolution_applied": "Evolução de esquema aplicada",
            "schema_evolution_blocked": "Evolução de esquema bloqueada",
            "cdc_database": "CDC do banco de origem",
            "cdc_table": "CDC da tabela de origem",
            "cdc_retention": "Retenção do CDC",
        }
        return f"{labels.get(name, name)}: {stable_json(safe)}"

    def _finish_worker(self, *, success: bool, error: str | None = None) -> None:
        self.busy = False
        self.progress.stop()
        for button in self.action_buttons:
            button.configure(state="normal")
        if success:
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
        self.log_text.configure(state="normal")
        self.log_text.insert("end", redact_text(text).rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

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
