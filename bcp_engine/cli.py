"""CLI do BulkFlow - SQL Server Data Export & Load."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .config import read_config, validate_config
from .engine import BcpEngine
from .models import SUCCESS_STATUSES, TableStatus
from .prerequisites import inspect_prerequisites
from .reporting import render_execution, render_plan_rows
from .state_migration import migrate_local_state
from .util import RedactingFormatter, deep_copy_json, redact_structure, redacted_exception, stable_json


LOG = logging.getLogger("bcp_engine")


def configure_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="BulkFlowCLI",
        description=(
            "BulkFlow: exportação e carga de dados na Bronze; "
            "a Landing recebe somente DDL e evolução estrutural."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Log técnico adicional, sempre redigido")
    sub = parser.add_subparsers(dest="command", required=True)

    prerequisites = sub.add_parser(
        "prerequisites",
        help="Verificar Driver ODBC e BCP sem abrir conexões",
    )
    prerequisites.add_argument("--config", type=Path, required=True)

    plan = sub.add_parser("plan", help="Inspecionar origem e estimar volume, sem DDL/DML")
    plan.add_argument("--config", type=Path, required=True)

    ddl = sub.add_parser("ddl", help="Gerar ou aplicar DDL idempotente")
    ddl.add_argument("--config", type=Path, required=True)
    ddl.add_argument("--area", choices=["bronze", "landing", "both"], default="both")
    ddl.add_argument("--output", type=Path, default=Path("ddl"))
    ddl.add_argument("--apply", action="store_true")
    ddl.add_argument("--confirm", action="store_true")

    run = sub.add_parser("run", help="Iniciar uma nova execução")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--confirm-load", action="store_true")
    run.add_argument("--export-only", action="store_true")

    resume = sub.add_parser("resume", help="Retomar exatamente uma execução existente")
    resume.add_argument("--config", type=Path, required=True)
    resume.add_argument("--execution-id", required=True)
    resume.add_argument("--confirm-load", action="store_true")

    import_cmd = sub.add_parser("import", help="Importar manifestos sem consultar a origem")
    import_cmd.add_argument("--config", type=Path, required=True)
    import_cmd.add_argument("--manifest", type=Path, required=True)
    import_cmd.add_argument("--confirm-load", action="store_true")

    status = sub.add_parser("status", help="Consultar apenas o controle local")
    status.add_argument("--config", type=Path, required=True)
    status.add_argument("--execution-id", required=True)

    migration = sub.add_parser(
        "migrate-control",
        help="Migrar explicitamente o controle SQLite legado para a versão atual",
    )
    migration.add_argument("--control-directory", type=Path, required=True)
    migration.add_argument(
        "--backup-path",
        type=Path,
        help="Caminho opcional do backup do controle legado; nunca é sobrescrito",
    )
    return parser


def _event(name: str, payload: Mapping[str, Any]) -> None:
    if name == "table_plan" and payload.get("text"):
        LOG.info("Panorama imediatamente antes da tabela:\n%s", payload["text"])
    elif name == "identity":
        LOG.info("Identidade SQL efetiva: %s", stable_json(redact_structure(payload)))
    elif name == "connection_identity":
        LOG.info("Conexão validada: %s", stable_json(redact_structure(payload)))
    elif name == "destination_space":
        logger = LOG.info if payload.get("sufficient") is not False else LOG.error
        logger("Espaço do destino Bronze: %s", stable_json(redact_structure(payload)))
    elif name == "schema_evolution_detected":
        LOG.info(
            "Evolução de schema detectada: %s",
            stable_json(redact_structure(payload)),
        )
    elif name == "schema_evolution_applied":
        LOG.info(
            "Evolução de schema aplicada: %s",
            stable_json(redact_structure(payload)),
        )
    elif name == "schema_evolution_blocked":
        LOG.warning(
            "Evolução de schema bloqueada sem alteração parcial: %s",
            stable_json(redact_structure(payload)),
        )
    elif name == "cdc_database":
        logger = LOG.info if payload.get("success") else LOG.error
        logger("CDC do banco de origem: %s", stable_json(redact_structure(payload)))
    elif name == "cdc_table":
        logger = LOG.info if payload.get("success") else LOG.error
        logger("CDC da tabela de origem: %s", stable_json(redact_structure(payload)))
    elif name == "cdc_retention":
        logger = LOG.info if payload.get("success") else LOG.error
        logger("Retenção do CDC: %s", stable_json(redact_structure(payload)))
    else:
        LOG.debug("%s %s", name, stable_json(redact_structure(payload)))


def _engine(path: Path, *, export_only: bool = False) -> BcpEngine:
    config = read_config(path)
    if export_only:
        value = deep_copy_json(config)
        value["execute_import"] = False
        config = validate_config(value)
        if config.get("delete_confirmed_files"):
            LOG.info(
                "Somente exportar: delete_confirmed_files efetivo=false; "
                "nenhum arquivo válido será excluído."
            )
    return BcpEngine(config, config_path=path.resolve(), event_sink=_event)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.command == "prerequisites":
            config = read_config(args.config)
            report = inspect_prerequisites(config)
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0 if report.ready else 2

        if args.command == "migrate-control":
            report = migrate_local_state(
                args.control_directory,
                backup_path=args.backup_path,
            )
            if report.migrated:
                LOG.info(
                    "Controle SQLite migrado de v%d para v%d; legado/backup preservado em %s; "
                    "%d estado(s) legado(s) de índice arquivado(s).",
                    report.from_version,
                    report.to_version,
                    report.backup_path,
                    report.archived_index_state_rows,
                )
            else:
                LOG.info(
                    "Controle SQLite já está na versão %d; nenhuma alteração foi necessária.",
                    report.to_version,
                )
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "plan":
            engine = _engine(args.config)
            plans, results = engine.plan()
            print(render_plan_rows(plans))
            for result in results:
                if result.status != TableStatus.PENDING.value:
                    LOG.error("%s: %s — %s", result.source_table, result.status, result.reason)
            return 2 if any(
                result.status != TableStatus.PENDING.value for result in results
            ) else 0

        if args.command == "ddl":
            if args.apply and not args.confirm:
                raise RuntimeError("Para aplicar DDL, informe --apply --confirm")
            engine = _engine(args.config)
            areas = ("bronze", "landing") if args.area == "both" else (args.area,)
            files = engine.generate_ddl(
                areas=areas, output_directory=args.output, apply=args.apply
            )
            for path in files:
                print(path)
            LOG.info(
                "DDL %s para %s arquivo(s).",
                "aplicado e validado" if args.apply else "gerado",
                len(files),
            )
            return 0

        if args.command == "run":
            engine = _engine(args.config, export_only=args.export_only)
            if engine.config["execute_import"] and not args.confirm_load:
                raise RuntimeError("Para executar carga no destino, informe --confirm-load")
            report = engine.run()
            print(f"execution_id={report.execution_id}")
            print(render_execution(report))
            return report.exit_code()

        if args.command == "resume":
            engine = _engine(args.config)
            if engine.config["execute_import"] and not args.confirm_load:
                raise RuntimeError("Para retomar carga no destino, informe --confirm-load")
            report = engine.run(execution_id=args.execution_id, resume=True)
            print(render_execution(report))
            return report.exit_code()

        if args.command == "import":
            if not args.confirm_load:
                raise RuntimeError("Para importar no destino, informe --confirm-load")
            engine = _engine(args.config)
            if not engine.config["execute_import"]:
                raise RuntimeError("O comando import exige execute_import=true e o destino Bronze")
            report = engine.import_manifests(args.manifest.resolve())
            print(render_execution(report))
            return report.exit_code()

        if args.command == "status":
            engine = _engine(args.config)
            status = engine.status(args.execution_id)
            if status is None:
                LOG.error(
                    "Execução não encontrada no controle local: %s", args.execution_id
                )
                return 1
            print(json.dumps(redact_structure(status), ensure_ascii=False, indent=2, default=str))
            statuses = {row["status"] for row in status.get("tables", [])}
            return 0 if statuses and statuses.issubset(SUCCESS_STATUSES) else 2

        parser.error("Comando não tratado")
        return 1
    except KeyboardInterrupt:
        LOG.error("Operação interrompida pelo operador; checkpoints duráveis foram preservados.")
        return 1
    except Exception as exc:
        LOG.error("Falha global: %s", redacted_exception(exc))
        return 1


__all__ = ["build_parser", "configure_logging", "main"]
