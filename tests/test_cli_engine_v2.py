from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import uuid


import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bcp_engine.cli as cli  # noqa: E402
import bcp_engine.engine as engine_module  # noqa: E402
from bcp_engine.cdc import DatabaseCdcResult, TableCdcResult  # noqa: E402
from bcp_engine.catalog import NoEligibleWatermarkError  # noqa: E402
from bcp_engine.engine import BcpEngine  # noqa: E402
from bcp_engine.models import TableStatus  # noqa: E402


CONFIG_PATH = Path("config.test.json")
FIXED_ID = "12345678-1234-5678-9234-567812345678"


class FakeReport:
    def __init__(self, code=0, execution_id=FIXED_ID):
        self.execution_id = execution_id
        self._code = code

    def exit_code(self):
        return self._code


class ParserTests(unittest.TestCase):
    def test_parser_accepts_all_commands_and_their_required_arguments(self):
        parser = cli.build_parser()
        cases = [
            (["plan", "--config", "cfg.json"], "plan"),
            (["ddl", "--config", "cfg.json", "--area", "both"], "ddl"),
            (["run", "--config", "cfg.json", "--export-only"], "run"),
            (["resume", "--config", "cfg.json", "--execution-id", FIXED_ID], "resume"),
            (["import", "--config", "cfg.json", "--manifest", "m.json"], "import"),
            (["status", "--config", "cfg.json", "--execution-id", FIXED_ID], "status"),
        ]
        for arguments, command in cases:
            with self.subTest(command=command):
                parsed = parser.parse_args(arguments)
                self.assertEqual(parsed.command, command)
                self.assertEqual(parsed.config, Path("cfg.json"))

    def test_ddl_default_output_uses_exact_project_local_bulkflow_directory(self):
        expected = ROOT.resolve() / "Local" / "BulkFlow" / "ddl"
        parsed = cli.build_parser().parse_args(
            ["ddl", "--config", "cfg.json", "--area", "both"]
        )
        self.assertEqual(parsed.output, expected)
        self.assertTrue(expected.is_dir())

        explicit = Path("custom-ddl")
        overridden = cli.build_parser().parse_args(
            [
                "ddl",
                "--config",
                "cfg.json",
                "--area",
                "both",
                "--output",
                str(explicit),
            ]
        )
        self.assertEqual(overridden.output, explicit)

    def test_parser_rejects_removed_portuguese_machine_flags_and_values(self):
        parser = cli.build_parser()
        rejected = [
            ["ddl", "--config", "cfg.json", "--saida", "ddl"],
            ["ddl", "--config", "cfg.json", "--area", "ambas"],
            ["run", "--config", "cfg.json", "--confirmar-carga"],
            ["run", "--config", "cfg.json", "--somente-exportar"],
            ["resume", "--config", "cfg.json", "--execucao", FIXED_ID],
            ["import", "--config", "cfg.json", "--manifesto", "m.json"],
        ]
        for arguments in rejected:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parser.parse_args(arguments)


class CliBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.logging = patch.object(cli, "configure_logging")
        self.render_execution = patch.object(cli, "render_execution", return_value="report")
        self.render_plan = patch.object(cli, "render_plan_rows", return_value="plan")
        self.printing = patch("builtins.print")
        self.logging.start()
        self.render_execution.start()
        self.render_plan.start()
        self.printing.start()
        self.addCleanup(self.logging.stop)
        self.addCleanup(self.render_execution.stop)
        self.addCleanup(self.render_plan.stop)
        self.addCleanup(self.printing.stop)

    @staticmethod
    def fake_engine(*, import_enabled=True, report_code=0):
        engine = Mock()
        engine.config = {"execute_import": import_enabled}
        engine.run.return_value = FakeReport(report_code)
        engine.import_manifests.return_value = FakeReport(report_code)
        engine.generate_ddl.return_value = []
        return engine

    def test_run_and_resume_dispatch_preserve_new_vs_existing_identity_semantics(self):
        run_engine = self.fake_engine(import_enabled=True)
        resume_engine = self.fake_engine(import_enabled=True)
        with patch.object(cli, "_engine", side_effect=[run_engine, resume_engine]):
            self.assertEqual(
                cli.main(["run", "--config", str(CONFIG_PATH), "--confirm-load"]),
                0,
            )
            self.assertEqual(
                cli.main(
                    [
                        "resume", "--config", str(CONFIG_PATH),
                        "--execution-id", FIXED_ID, "--confirm-load",
                    ]
                ),
                0,
            )
        run_engine.run.assert_called_once_with()
        resume_engine.run.assert_called_once_with(execution_id=FIXED_ID, resume=True)

    def test_mutating_commands_require_the_documented_confirmations(self):
        run_engine = self.fake_engine(import_enabled=True)
        with patch.object(cli, "_engine", return_value=run_engine):
            self.assertEqual(cli.main(["run", "--config", str(CONFIG_PATH)]), 1)
        run_engine.run.assert_not_called()

        resume_engine = self.fake_engine(import_enabled=True)
        with patch.object(cli, "_engine", return_value=resume_engine):
            self.assertEqual(
                cli.main(
                    ["resume", "--config", str(CONFIG_PATH), "--execution-id", FIXED_ID]
                ),
                1,
            )
        resume_engine.run.assert_not_called()

        ddl_engine = self.fake_engine()
        with patch.object(cli, "_engine", return_value=ddl_engine):
            self.assertEqual(
                cli.main(["ddl", "--config", str(CONFIG_PATH), "--apply"]), 1
            )
        ddl_engine.generate_ddl.assert_not_called()

        import_engine = self.fake_engine(import_enabled=True)
        with patch.object(cli, "_engine", return_value=import_engine):
            self.assertEqual(
                cli.main(
                    ["import", "--config", str(CONFIG_PATH), "--manifest", "m.json"]
                ),
                1,
            )
        import_engine.import_manifests.assert_not_called()

    def test_confirmed_mutations_and_export_only_override_are_dispatched(self):
        ddl_engine = self.fake_engine()
        import_engine = self.fake_engine(import_enabled=True)
        export_engine = self.fake_engine(import_enabled=False)
        with patch.object(
            cli, "_engine", side_effect=[ddl_engine, import_engine, export_engine]
        ) as factory:
            self.assertEqual(
                cli.main(
                    [
                        "ddl",
                        "--config",
                        str(CONFIG_PATH),
                        "--apply",
                        "--confirm",
                    ]
                ),
                0,
            )
            self.assertEqual(
                cli.main(
                    [
                        "import",
                        "--config",
                        str(CONFIG_PATH),
                        "--manifest",
                        "m.json",
                        "--confirm-load",
                    ]
                ),
                0,
            )
            self.assertEqual(
                cli.main(
                    ["run", "--config", str(CONFIG_PATH), "--export-only"]
                ),
                0,
            )
        ddl_engine.generate_ddl.assert_called_once()
        import_engine.import_manifests.assert_called_once()
        export_engine.run.assert_called_once_with()
        self.assertEqual(
            factory.call_args_list,
            [
                call(CONFIG_PATH),
                call(CONFIG_PATH),
                call(CONFIG_PATH, export_only=True),
            ],
        )

    def test_cli_propagates_success_partial_and_global_exit_codes(self):
        for report_code in (0, 2):
            with self.subTest(report_code=report_code):
                fake = self.fake_engine(import_enabled=True, report_code=report_code)
                with patch.object(cli, "_engine", return_value=fake):
                    self.assertEqual(
                        cli.main(
                            [
                                "run",
                                "--config",
                                str(CONFIG_PATH),
                                "--confirm-load",
                            ]
                        ),
                        report_code,
                    )
        with patch.object(cli, "_engine", side_effect=RuntimeError("global")):
            self.assertEqual(cli.main(["plan", "--config", str(CONFIG_PATH)]), 1)

    def test_plan_and_status_distinguish_partial_missing_and_success(self):
        planned = SimpleNamespace(
            status=TableStatus.PENDING.value, source_table="a", reason=None
        )
        skipped = SimpleNamespace(
            status=TableStatus.SKIPPED_NO_KEY.value, source_table="b", reason="sem chave"
        )
        plan_ok = self.fake_engine()
        plan_ok.plan.return_value = ([], [planned])
        plan_partial = self.fake_engine()
        plan_partial.plan.return_value = ([], [planned, skipped])
        status_ok = self.fake_engine()
        status_ok.status.return_value = {
            "tables": [{"status": TableStatus.COMPLETED.value}]
        }
        status_partial = self.fake_engine()
        status_partial.status.return_value = {
            "tables": [{"status": TableStatus.PARTIAL_ERROR.value}]
        }
        status_missing = self.fake_engine()
        status_missing.status.return_value = None
        with patch.object(
            cli,
            "_engine",
            side_effect=[plan_ok, plan_partial, status_ok, status_partial, status_missing],
        ):
            self.assertEqual(cli.main(["plan", "--config", str(CONFIG_PATH)]), 0)
            self.assertEqual(cli.main(["plan", "--config", str(CONFIG_PATH)]), 2)
            self.assertEqual(
                cli.main(
                    ["status", "--config", str(CONFIG_PATH), "--execution-id", FIXED_ID]
                ),
                0,
            )
            self.assertEqual(
                cli.main(
                    ["status", "--config", str(CONFIG_PATH), "--execution-id", FIXED_ID]
                ),
                2,
            )
            self.assertEqual(
                cli.main(
                    ["status", "--config", str(CONFIG_PATH), "--execution-id", FIXED_ID]
                ),
                1,
            )


class FakeResolver:
    def __init__(self):
        self.cleared = 0

    def clear_cache(self):
        self.cleared += 1


class FakeBcpRunner:
    def __init__(self):
        self.versions = []
        self.preflights = []

    def require_version(self, executable):
        self.versions.append(executable)

    def preflight(self, invocation):
        self.preflights.append(invocation)


class FakeArtifactStore:
    def __init__(self, _root, execution_id, *, reader_sids=(), writer_sids=()):
        self.execution_id = execution_id
        self.reader_sids = tuple(reader_sids)
        self.writer_sids = tuple(writer_sids)
        self.execution_root = Path(".")
        self.probes = []

    def probe(self, require_delete=False):
        self.probes.append(require_delete)


class FakeIdentity:
    def as_dict(self):
        return {"login": "tester"}


class FakeLocalState:
    instances = []

    def __init__(self, _directory):
        self.start_calls = []
        self.resume_calls = []
        self.finish_calls = []
        self.update_calls = []
        self.reconciled_blocks = []
        self.imported_blocks = []
        self.records = {}
        self.closed = False
        type(self).instances.append(self)

    def start_execution(self, *args):
        self.start_calls.append(args)

    def resume_execution(self, *args):
        self.resume_calls.append(args)
        return {}

    def register_table(
        self,
        execution_id,
        table_id,
        ordinal,
        source_schema,
        source_table,
        destination_area,
        destination_schema,
        destination_table,
        structural_hash,
        watermark,
    ):
        self.records.setdefault(
            table_id,
            {
                "execution_id": execution_id,
                "structural_hash": structural_hash,
                "final_limit_json": json.dumps(["9"]),
                "rows_exported": 0,
                "status": "PENDING",
            },
        )

    def table(self, _execution_id, table_id):
        return self.records.get(table_id)

    def update_table(self, execution_id, table_id, **values):
        self.update_calls.append((execution_id, table_id, dict(values)))
        if table_id in self.records:
            self.records[table_id].update(values)

    def reconcile_manifest_block(self, *args, **kwargs):
        self.reconciled_blocks.append((args, kwargs))

    def mark_imported(self, *args):
        self.imported_blocks.append(args)

    def finish_execution(self, *args):
        self.finish_calls.append(args)

    def execution(self, _execution_id):
        return None

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def base_engine_config():
    return {
        "source": {"authentication": {"type": "windows_integrated"}},
        "execute_import": False,
        "delete_confirmed_files": True,
        "local_control_directory": r"C:\control",
        "executor_directory": r"C:\data",
        "continue_after_table_error": True,
    }


def make_engine():
    engine = BcpEngine.__new__(BcpEngine)
    engine.config = base_engine_config()
    engine.config_path = None
    engine.config_directory = ROOT
    engine.resolver = FakeResolver()
    engine.connection_factory = None
    engine.bcp_runner = FakeBcpRunner()
    engine.event = lambda *_args: None
    engine._connect_source = Mock(return_value=(object(), FakeIdentity()))
    engine._connect_destination = Mock(side_effect=AssertionError("destino proibido"))
    engine._sql_path_probe = Mock()
    return engine


class EngineBoundaryTests(unittest.TestCase):
    def run_patches(self, tables):
        stack = ExitStack()
        FakeLocalState.instances = []
        stack.enter_context(patch.object(engine_module, "LocalState", FakeLocalState))
        stack.enter_context(patch.object(engine_module, "ArtifactStore", FakeArtifactStore))
        stack.enter_context(patch.object(engine_module, "effective_tables", return_value=tables))
        stack.enter_context(patch.object(engine_module, "structural_fingerprint", return_value="struct"))
        stack.enter_context(patch.object(engine_module, "operational_fingerprint", return_value="oper"))
        stack.enter_context(patch.object(engine_module, "database_identity", return_value={"db": "id"}))
        stack.enter_context(
            patch.object(
                engine_module,
                "build_bcp_invocation",
                return_value=SimpleNamespace(executable="bcp"),
            )
        )
        stack.enter_context(patch.object(engine_module, "write_reports"))
        stack.enter_context(patch.object(engine_module, "close_all"))
        return stack

    def test_resolve_layout_rejects_profile_for_another_destination_area(self):
        engine = BcpEngine.__new__(BcpEngine)
        engine.config = {
            "execute_import": True,
            "bronze_destination": {
                "schema": "s344",
                "structure_profile": "templates/bronze.json",
            },
            "bronze_event_id": {
                "strategy": "sequence",
                "sequence_name": "seq_{destination_table}",
                "source_column": None,
            },
        }
        engine.config_directory = ROOT
        table = {
            "source": {"database": "BD_ORIGEM", "table": "TABELA_ORIGEM_01"},
            "destination": {
                "area": "bronze",
                "schema": "s344",
                "table": "bd_origem_tabela_origem_01",
                "structure_profile": "templates/landing.json",
            },
        }
        columns = [{
            "name": "COLUNA_MARCA_DAGUA_01",
            "type_name": "int",
            "max_length": 4,
            "precision": 10,
            "scale": 0,
            "is_nullable": False,
            "collation_name": None,
            "is_identity": False,
            "is_computed": False,
        }]
        with self.assertRaisesRegex(RuntimeError, "não corresponde.*destino"):
            engine._resolve_layout(table, columns)

    def test_service_layer_rejects_landing_as_a_data_route(self):
        for operation in ("run", "import"):
            with self.subTest(operation=operation):
                engine = make_engine()
                engine.config["active_destination"] = "landing"
                with self.assertRaisesRegex(RuntimeError, "Landing é somente estrutura"):
                    if operation == "run":
                        engine.run(execution_id=FIXED_ID)
                    else:
                        engine.import_manifests(Path("manifesto-inexistente.json"))
                engine._connect_source.assert_not_called()
                engine._connect_destination.assert_not_called()

        engine = make_engine()
        engine.config["tables"] = [{"destination_area": "landing"}]
        with self.assertRaisesRegex(RuntimeError, "destination_area deve ser bronze"):
            engine.run(execution_id=FIXED_ID)
        engine._connect_source.assert_not_called()
        engine._connect_destination.assert_not_called()

    def test_engine_run_generates_uuid_and_resume_preserves_supplied_uuid(self):
        engine = make_engine()
        generated = uuid.UUID(FIXED_ID)
        with self.run_patches([]), patch.object(
            engine_module.uuid, "uuid4", return_value=generated
        ) as uuid4:
            new_report = engine.run()
            resumed_report = engine.run(execution_id=FIXED_ID, resume=True)
        self.assertEqual(new_report.execution_id, FIXED_ID)
        self.assertEqual(resumed_report.execution_id, FIXED_ID)
        self.assertEqual(uuid4.call_count, 1)
        self.assertEqual(FakeLocalState.instances[0].start_calls[0][0], FIXED_ID)
        self.assertEqual(FakeLocalState.instances[1].resume_calls[0][0], FIXED_ID)
        engine._connect_destination.assert_not_called()

    def test_global_failure_before_control_open_still_writes_final_reports(self):
        engine = make_engine()
        with (
            self.run_patches([]),
            patch.object(
                engine_module,
                "LocalState",
                side_effect=RuntimeError("controle local corrompido"),
            ),
            patch.object(engine_module, "write_reports") as writer,
        ):
            report = engine.run(execution_id=FIXED_ID)

        self.assertEqual(report.exit_code(), 1)
        self.assertIn("controle local corrompido", report.global_error)
        self.assertIsNotNone(report.finished_at)
        writer.assert_called_once_with(report, Path("."))
        engine._connect_source.assert_not_called()

    def test_export_only_run_never_connects_destination_and_tables_are_sequential(self):
        engine = make_engine()
        tables = [
            {
                "index": index,
                "source": {"schema": "dbo", "table": name},
                "destination": {
                    "area": "bronze",
                    "schema": "s",
                    "table": "dst_" + name,
                },
            }
            for index, name in enumerate(("a", "b", "c"))
        ]
        events = []

        def prepare(_source, table, _db):
            name = table["source"]["table"]
            events.append("prepare:" + name)
            return SimpleNamespace(
                effective=table,
                table_id="id_" + name,
                columns=[],
                watermark=SimpleNamespace(columns=(), warnings=()),
                layout=SimpleNamespace(table="dst_" + name),
                projection={},
                layout_contract={},
                table_structural_hash="hash_" + name,
                final_limit=None,
                average_row_bytes=None,
            )

        def process(
            _source,
            _destination,
            _importer,
            state,
            _store,
            _base_bcp,
            prepared,
            _execution_id,
            _dataset_id,
            _source_identity,
            _destination_identity,
            result,
        ):
            name = prepared.effective["source"]["table"]
            events.append("process:" + name)
            if name == "b":
                raise RuntimeError("falha intermediaria")
            result.rows_exported = 1
            state.records[prepared.table_id]["rows_exported"] = 1

        engine.prepare_table = Mock(side_effect=prepare)
        engine._table_plan = Mock(
            return_value={
                "estimated_rows": 1,
                "warnings": [],
                "execution_allowed": True,
            }
        )
        engine._process_blocks = Mock(side_effect=process)
        with self.run_patches(tables):
            report = engine.run(execution_id=FIXED_ID)
        self.assertEqual(
            events,
            [
                "prepare:a",
                "process:a",
                "prepare:b",
                "process:b",
                "prepare:c",
                "process:c",
            ],
        )
        self.assertEqual(
            [result.status for result in report.tables],
            [
                TableStatus.EXPORTED.value,
                TableStatus.EXPORT_ERROR.value,
                TableStatus.EXPORTED.value,
            ],
        )
        engine._connect_destination.assert_not_called()

    def test_failure_before_second_preparation_does_not_corrupt_first_table_state(self):
        engine = make_engine()
        tables = [
            {
                "index": index,
                "source": {"schema": "dbo", "table": name},
                "destination": {
                    "area": "bronze",
                    "schema": "s",
                    "table": "dst_" + name,
                },
            }
            for index, name in enumerate(("a", "b"))
        ]
        prepared_a = SimpleNamespace(
            effective=tables[0],
            table_id="id_a",
            columns=[],
            watermark=SimpleNamespace(columns=(), warnings=()),
            layout=SimpleNamespace(table="dst_a"),
            projection={},
            layout_contract={},
            table_structural_hash="hash_a",
            final_limit=None,
            average_row_bytes=None,
        )
        engine.prepare_table = Mock(side_effect=[prepared_a, RuntimeError("falha B")])
        engine._table_plan = Mock(
            return_value={"estimated_rows": 1, "warnings": [], "execution_allowed": True}
        )

        def process(*args):
            state, prepared, result = args[3], args[6], args[-1]
            result.rows_exported = 1
            state.records[prepared.table_id]["rows_exported"] = 1

        engine._process_blocks = Mock(side_effect=process)
        with self.run_patches(tables):
            report = engine.run(execution_id=FIXED_ID)
        state = FakeLocalState.instances[0]
        self.assertEqual(report.tables[0].status, TableStatus.EXPORTED.value)
        self.assertEqual(report.tables[1].status, TableStatus.EXPORT_ERROR.value)
        self.assertEqual(state.records["id_a"]["status"], TableStatus.EXPORTED.value)
        self.assertFalse(
            any(
                table_id == "id_a" and "falha B" in str(values.get("reason", ""))
                for _execution, table_id, values in state.update_calls
            )
        )

    def test_continue_after_table_error_false_stops_after_typed_table_failure(self):
        engine = make_engine()
        engine.config["continue_after_table_error"] = False
        tables = [
            {
                "index": index,
                "source": {"schema": "dbo", "table": name},
                "destination": {
                    "area": "bronze",
                    "schema": "s",
                    "table": "dst_" + name,
                },
            }
            for index, name in enumerate(("a", "b"))
        ]
        engine.prepare_table = Mock(
            side_effect=NoEligibleWatermarkError("sem chave elegível")
        )

        with self.run_patches(tables):
            report = engine.run(execution_id=FIXED_ID)

        self.assertEqual(len(report.tables), 1)
        self.assertEqual(report.tables[0].status, TableStatus.SKIPPED_NO_KEY.value)
        engine.prepare_table.assert_called_once()

    def test_cdc_failure_skips_only_flagged_table_and_continues(self):
        engine = make_engine()
        engine.config["continue_after_table_error"] = False
        engine.config["cdc_retention_minutes"] = 262_800
        engine.config["source"].update(
            {"database": "BD_ORIGEM", "read_database": "BD_ORIGEM"}
        )
        tables = [
            {
                "index": index,
                "enable_cdc": enable_cdc,
                "source": {
                    "database": "BD_ORIGEM",
                    "read_database": "BD_ORIGEM",
                    "schema": "dbo",
                    "table": name,
                },
                "destination": {
                    "area": "bronze",
                    "schema": "s344",
                    "table": "dst_" + name,
                },
            }
            for index, (name, enable_cdc) in enumerate((("a", True), ("b", False)))
        ]
        coordinator = Mock()
        coordinator.preflight.return_value = DatabaseCdcResult(
            database="BD_ORIGEM",
            success=True,
            enabled=True,
            changed=False,
            state="ONLINE",
            stage="database_verification",
        )
        coordinator.ensure_table.return_value = TableCdcResult(
            database="BD_ORIGEM",
            schema="dbo",
            table="a",
            success=False,
            enabled=False,
            changed=False,
            supports_net_changes=None,
            stage="table_verification",
            error_code="CDC_PERMISSION_DENIED",
            message="CDC não confirmado.",
            permission_hint="Use uma conta autorizada.",
        )
        prepared_b = SimpleNamespace(
            effective=tables[1],
            table_id="id_b",
            columns=[],
            watermark=SimpleNamespace(columns=(), warnings=()),
            layout=SimpleNamespace(table="dst_b"),
            projection={},
            layout_contract={},
            table_structural_hash="hash_b",
            final_limit=None,
            average_row_bytes=None,
        )
        engine.prepare_table = Mock(return_value=prepared_b)
        engine._table_plan = Mock(
            return_value={"estimated_rows": 1, "warnings": [], "execution_allowed": True}
        )

        def process(*args):
            state, prepared, result = args[3], args[6], args[-1]
            result.rows_exported = 1
            state.records[prepared.table_id]["rows_exported"] = 1

        engine._process_blocks = Mock(side_effect=process)
        with self.run_patches(tables), patch.object(
            engine_module, "CdcCoordinator", return_value=coordinator
        ) as coordinator_factory:
            report = engine.run(execution_id=FIXED_ID)

        coordinator_factory.assert_called_once_with(retention_minutes=262_800)
        coordinator.preflight.assert_called_once()
        coordinator.ensure_table.assert_called_once_with(
            unittest.mock.ANY,
            database="BD_ORIGEM",
            schema="dbo",
            table="a",
        )
        self.assertEqual(
            [item.status for item in report.tables],
            [
                TableStatus.SKIPPED_CDC_ACTIVATION_FAILED.value,
                TableStatus.EXPORTED.value,
            ],
        )
        engine.prepare_table.assert_called_once_with(
            unittest.mock.ANY, tables[1], unittest.mock.ANY
        )

    def test_status_reads_only_local_state(self):
        engine = make_engine()

        class StatusState(FakeLocalState):
            def execution(self, execution_id):
                return {"execution_id": execution_id, "tables": []}

        with patch.object(engine_module, "LocalState", StatusState):
            status = engine.status(FIXED_ID)
        self.assertEqual(status["execution_id"], FIXED_ID)
        engine._connect_destination.assert_not_called()
        engine._connect_source.assert_not_called()

    def test_import_manifests_never_connects_or_queries_source(self):
        engine = make_engine()
        engine.config.update(
            {
                "execute_import": True,
                "active_destination": "bronze",
                "source": {
                    "instance": "source",
                    "database": "source_db",
                    "read_database": "source_db",
                    "schema": "dbo",
                    "authentication": {"type": "windows_integrated"},
                },
                "bronze_destination": {
                    "instance": "destination",
                    "database": "destination_db",
                    "schema": "s344",
                    "structure_profile": "bronze",
                },
                "control_schema": "dbo",
                "rows_per_block": 1500,
                "estimates": {"safety_factor": 1.25},
                "destination_sql_directory": r"C:\data",
                "bronze_event_id": {"strategy": "sequence"},
                "delete_confirmed_files": False,
                "tables": [
                    {
                        "source_table": "source_table",
                        "destination_table": "target",
                        "watermark": None,
                        "enable_cdc": False,
                    }
                ],
            }
        )
        engine._connect_source = Mock(side_effect=AssertionError("origem proibida"))
        engine._connect_destination = Mock(return_value=(object(), FakeIdentity()))
        engine._provision_initial = Mock()
        engine._finish_indexes = Mock()
        manifest = SimpleNamespace(
            execution_id=FIXED_ID,
            dataset_id="dataset",
            profile="bronze",
            projection=[],
            destination={"area": "bronze", "schema": "s344", "table": "target"},
            source={
                "database": "source_db",
                "read_database": "source_db",
                "schema": "dbo",
                "table": "source_table",
            },
            layout_hash="layout",
            projection_hash="projection",
            metadata_mapping={},
            layout_contract={},
            table_id=engine_module._table_id(
                engine_module.effective_tables(engine.config)[0]
            ),
            block_id="block-id",
            block_number=1,
            rows_exported=7,
            file_bytes=70,
            watermark=[],
            final_limit=["9"],
            upper_bound=["9"],
            lower_bound=None,
            data_sha256="a" * 64,
            format_sha256="f" * 64,
            empty_range=False,
            exported_at="2026-10-06T12:00:00+00:00",
        )

        class ImportStore(FakeArtifactStore):
            def load_and_verify(self, _path):
                return manifest

        importer = Mock()
        state = FakeLocalState(r"C:\control")
        layout = SimpleNamespace(table="target", schema="s344", profile_name="bronze")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "block.manifest.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch.object(engine_module.BlockManifest, "from_dict", return_value=manifest),
                patch.object(engine_module, "DestinationImporter", return_value=importer),
                patch.object(engine_module, "LocalState", return_value=state),
                patch.object(engine_module, "ArtifactStore", ImportStore),
                patch.object(engine_module, "resolve_profile", return_value=layout),
                patch.object(engine_module, "layout_contract", return_value={"layout_hash": "layout"}),
                patch.object(
                    engine_module,
                    "projection_contract",
                    return_value={"projection_hash": "projection"},
                ),
                patch.object(engine_module, "build_import_plan", return_value=object()),
                patch.object(engine_module, "operational_fingerprint", return_value="operational"),
                patch.object(engine_module, "write_reports"),
                patch.object(engine_module, "close_all"),
                patch.object(engine, "_destination_table_exists", return_value=False),
            ):
                report = engine.import_manifests(path)
        self.assertEqual(report.exit_code(), 0)
        self.assertEqual(report.tables[0].rows_imported, 7)
        engine._connect_source.assert_not_called()
        engine._connect_destination.assert_called_once_with()
        importer.import_manifest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
