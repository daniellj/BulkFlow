from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from bcp_engine.artifacts import ArtifactStore
from bcp_engine.models import BlockManifest, ExecutionReport, TableResult
from bcp_engine.reporting import write_reports
from bcp_engine.state import LocalState, StateVersionError, StructuralResumeError


def manifest_draft(
    *,
    execution_id: str,
    table_id: str,
    block_id: str,
    block_number: int = 1,
    rows: int = 2,
    empty: bool = False,
) -> BlockManifest:
    return BlockManifest(
        manifest_version=2,
        complete=False,
        execution_id=execution_id,
        dataset_id="d" * 64,
        table_id=table_id,
        block_id=block_id,
        block_number=block_number,
        attempt=1,
        source={
            "instance": r"SQL\ORIGEM",
            "database": "DB_ORIGEM",
            "read_database": "DB_ORIGEM",
            "schema": "dbo",
            "table": "TABELA",
        },
        destination={
            "area": "bronze",
            "instance": r"SQL\DESTINO",
            "database": "DB_BRONZE",
            "schema": "fonte",
            "table": "db_origem_tabela",
        },
        authentication={"source": "windows_integrated", "destination": "windows_integrated"},
        profile="bronze",
        projection=[
            {
                "name": "codigo",
                "type_name": "int",
                "max_length": 4,
                "precision": 10,
                "scale": 0,
                "is_nullable": False,
                "collation_name": None,
            }
        ],
        projection_hash="1" * 64,
        layout_hash="2" * 64,
        watermark=[
            {
                "name": "codigo",
                "direction": "ASC",
                "type_name": "int",
                "collation": None,
            }
        ],
        lower_bound=None if block_number == 1 else [str((block_number - 1) * 10)],
        upper_bound=[str(block_number * 10)],
        final_limit=["100"],
        rows_exported=0 if empty else rows,
        file_bytes=0,
        data_file=None,
        data_sha256=None,
        format_file="pending",
        format_sha256="pending",
        exported_at="2026-10-06T12:00:00+00:00",
        empty_range=empty,
    )


class ArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.execution_id = str(uuid.uuid4())
        self.table_id = "e" * 64
        self.block_id = str(uuid.uuid4())
        self.store = ArtifactStore(self.root, self.execution_id)
        self.paths = self.store.block_paths(self.table_id, 1, self.block_id)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_inputs(self, data: bytes = b"native-bcp-data") -> BlockManifest:
        self.paths.partial.write_bytes(data)
        self.paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
        return manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
        )

    def test_publish_orders_partial_data_before_complete_manifest(self):
        published = self.store.publish(self.paths, self._write_inputs())
        self.assertLessEqual(len(self.paths.directory.parent.name), 45)
        self.assertTrue(self.paths.directory.name.startswith("block_000000000001_"))
        self.assertEqual(self.paths.data.name, "block_000000000001.bcp")
        self.assertTrue(published.complete)
        self.assertFalse(self.paths.partial.exists())
        self.assertTrue(self.paths.data.is_file())
        self.assertTrue(self.paths.manifest.is_file())
        reloaded = self.store.load_and_verify(self.paths.manifest)
        self.assertEqual(reloaded.data_sha256, published.data_sha256)
        self.assertEqual(reloaded.file_bytes, len(b"native-bcp-data"))

    def test_partial_or_missing_manifest_never_becomes_durable(self):
        draft = self._write_inputs()
        partial_manifest = self.paths.manifest.with_suffix(self.paths.manifest.suffix + ".partial")
        partial_manifest.write_text(json.dumps({"complete": False}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "final ausente ou parcial"):
            self.store.load_and_verify(partial_manifest)
        self.assertFalse(draft.complete)
        self.assertFalse(self.paths.manifest.exists())

    def test_stale_manifest_partial_from_crash_is_reconciled_on_retry(self):
        draft = self._write_inputs()
        partial_manifest = self.paths.manifest.with_suffix(self.paths.manifest.suffix + ".partial")
        partial_manifest.write_text('{"truncado":', encoding="utf-8")
        # Uma queda depois de fsync e antes de os.replace não pode bloquear para
        # sempre o mesmo bloco; o fragmento não é evidência de conclusão.
        published = self.store.publish(self.paths, draft)
        self.assertTrue(published.complete)
        self.assertFalse(partial_manifest.exists())

    def test_hash_tampering_is_detected_before_use(self):
        self.store.publish(self.paths, self._write_inputs())
        self.paths.data.write_bytes(b"dados-adulterados")
        with self.assertRaisesRegex(RuntimeError, "modificado"):
            self.store.load_and_verify(self.paths.manifest)

    def test_format_hash_tampering_is_detected(self):
        self.store.publish(self.paths, self._write_inputs())
        self.paths.format.write_text("<adulterado />", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "format file diverge"):
            self.store.load_and_verify(self.paths.manifest)

    def test_removing_confirmed_data_is_idempotent_after_commit(self):
        self.store.publish(self.paths, self._write_inputs())
        self.assertTrue(self.store.retained_data_exists(self.paths.manifest))
        self.store.remove_confirmed_data(self.paths.manifest)
        self.assertFalse(self.paths.data.exists())
        self.assertFalse(self.store.retained_data_exists(self.paths.manifest))

        # Simula retomada depois de queda exatamente apos o unlink.
        self.store.remove_confirmed_data(self.paths.manifest)
        reconciled = self.store.load_for_reconciliation(self.paths.manifest)
        self.assertEqual(reconciled.block_id, self.block_id)

    def test_manifest_data_path_cannot_escape_block_directory(self):
        self.store.publish(self.paths, self._write_inputs())
        payload = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        payload["data_file"] = "../fora.bcp"
        self.paths.manifest.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nome do arquivo|escapa"):
            self.store.load_and_verify(self.paths.manifest)

    def test_manifest_itself_cannot_be_loaded_outside_execution_root(self):
        outside = self.root / "outside.manifest.json"
        outside.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapa"):
            self.store.load_and_verify(outside)

    def test_existing_manifest_must_match_requested_block_identity(self):
        self.store.publish(self.paths, self._write_inputs())
        different = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=str(uuid.uuid4()),
        )
        with self.assertRaisesRegex(RuntimeError, "identidade|bloco"):
            self.store.publish(self.paths, different)

    def test_manifest_rejects_password_fields(self):
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
        )
        draft.complete = True
        draft.empty_range = True
        draft.rows_exported = 0
        draft.format_file = "block.xml"
        draft.format_sha256 = "f" * 64
        draft.authentication = {"source": {"senha": "não pode persistir"}}
        with self.assertRaisesRegex(ValueError, "secreto"):
            draft.validate()

    def test_empty_manifest_rejects_nonzero_bytes_or_data_hash(self):
        for file_bytes, data_hash in ((1, None), (0, "a" * 64)):
            with self.subTest(file_bytes=file_bytes, data_hash=data_hash):
                draft = manifest_draft(
                    execution_id=self.execution_id,
                    table_id=self.table_id,
                    block_id=self.block_id,
                    empty=True,
                    rows=0,
                )
                draft.complete = True
                draft.file_bytes = file_bytes
                draft.data_sha256 = data_hash
                draft.format_file = "block_000000000001.xml"
                draft.format_sha256 = "f" * 64
                with self.assertRaisesRegex(ValueError, "Faixa vazia"):
                    draft.validate()

    def test_manifest_rejects_noncanonical_uuid_and_uppercase_hash(self):
        draft = manifest_draft(
            execution_id=self.execution_id.upper(),
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        with self.assertRaisesRegex(ValueError, "execution_id"):
            draft.validate()

        draft.execution_id = self.execution_id
        draft.dataset_id = ("d" * 64).upper()
        with self.assertRaisesRegex(ValueError, "dataset_id"):
            draft.validate()

        draft.authentication = {
            "source": '{"type":"sql","password":"também não pode persistir"}',
            "destination": "windows_integrated",
        }
        with self.assertRaisesRegex(ValueError, "secreto"):
            draft.validate()

    def test_manifest_rejects_legacy_or_unknown_nested_contract_fields(self):
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block.xml"
        draft.format_sha256 = "f" * 64
        draft.source = {
            "instancia": r"SQL\ORIGEM",
            "banco": "DB_ORIGEM",
            "banco_leitura": "DB_ORIGEM",
            "esquema": "dbo",
            "tabela": "TABELA",
        }
        with self.assertRaisesRegex(ValueError, "desconhecidos|obrigatórios"):
            draft.validate()

        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block.xml"
        draft.format_sha256 = "f" * 64
        draft.watermark[0]["legacy_order"] = "ASC"
        with self.assertRaisesRegex(ValueError, "desconhecidos"):
            draft.validate()

        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block_000000000001.xml"
        draft.format_sha256 = "f" * 64
        assert draft.destination is not None
        draft.destination["schema"] = "S344"
        with self.assertRaisesRegex(ValueError, "minúsculas"):
            draft.validate()

    def test_manifest_rejects_unsupported_watermark_type_before_import(self):
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block_000000000001.xml"
        draft.format_sha256 = "f" * 64
        draft.watermark[0]["type_name"] = "sql_variant"
        with self.assertRaisesRegex(ValueError, "type_name.*suportado"):
            draft.validate()

    def test_manifest_requires_exact_landing_metadata_mapping_contract(self):
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block_000000000001.xml"
        draft.format_sha256 = "f" * 64
        draft.profile = "landing"
        assert draft.destination is not None
        draft.destination["area"] = "landing"
        draft.metadata_mapping = {
            "aud_ccid": {"constant": 1, "sql_type": "BIGINT"},
            "aud_cntrrn": {"constant": 1, "sql_type": "INT"},
        }
        with self.assertRaisesRegex(ValueError, "completo.*aud_enttyp"):
            draft.validate()

        draft.metadata_mapping["aud_enttyp"] = {
            "constant": "PT",
            "sql_type": "VARCHAR(30)",
        }
        draft.validate()

        draft.metadata_mapping["aud_ccid"]["sql_type"] = "INT"
        with self.assertRaisesRegex(ValueError, "exatamente BIGINT"):
            draft.validate()

    def test_manifest_rejects_metadata_mapping_outside_landing(self):
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            empty=True,
            rows=0,
        )
        draft.complete = True
        draft.format_file = "block_000000000001.xml"
        draft.format_sha256 = "f" * 64
        draft.metadata_mapping = {
            "aud_ccid": {"constant": 1},
            "aud_cntrrn": {"constant": 1},
            "aud_enttyp": {"constant": "PT"},
        }
        with self.assertRaisesRegex(ValueError, "somente se aplica"):
            draft.validate()

    def test_direct_keyless_manifest_has_no_fake_watermark_and_persists_count(self):
        self.paths.partial.write_bytes(b"duas-linhas")
        self.paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            rows=2,
        )
        draft.watermark = []
        draft.lower_bound = None
        draft.upper_bound = []
        draft.final_limit = []
        draft.transfer_mode = "DIRECT_KEYLESS"
        draft.source_rows_at_capture = 2
        published = self.store.publish(self.paths, draft)
        self.assertEqual(published.watermark, [])
        self.assertEqual(published.transfer_mode, "DIRECT_KEYLESS")
        self.assertEqual(published.source_rows_at_capture, 2)

        # source_rows_at_capture is only the fast metadata estimate for a
        # DIRECT_KEYLESS transfer.  The BCP result is the authoritative count
        # and may legitimately differ when the source changes concurrently.
        published.rows_exported = 1
        published.validate()

    def test_empty_direct_keyless_manifest_persists_zero_count_without_data_file(self):
        self.paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
        draft = manifest_draft(
            execution_id=self.execution_id,
            table_id=self.table_id,
            block_id=self.block_id,
            rows=0,
            empty=True,
        )
        draft.watermark = []
        draft.lower_bound = None
        draft.upper_bound = []
        draft.final_limit = []
        draft.transfer_mode = "DIRECT_KEYLESS"
        draft.source_rows_at_capture = 0
        draft.table_empty = True
        published = self.store.publish(self.paths, draft)
        self.assertTrue(published.empty_range)
        self.assertTrue(published.table_empty)
        self.assertEqual(published.source_rows_at_capture, 0)
        self.assertIsNone(published.data_file)


class ReportingArtifactTests(unittest.TestCase):
    def test_report_file_names_and_columns_use_the_english_contract(self):
        execution_id = str(uuid.uuid4())
        report = ExecutionReport(
            execution_id=execution_id,
            command="run",
            started_at="2026-10-06T12:00:00+00:00",
            cdc_database={
                "success": True,
                "stage": "retention_verification",
                "retention_minutes": 262_800,
                "retention_changed": False,
                "retention_restarted": False,
            },
            tables=[
                TableResult(
                    source_table="TABELA_ORIGEM_01",
                    destination_table="bd_origem_tabela_origem_01",
                    status="COMPLETED",
                    transfer_mode="DIRECT_KEYLESS",
                    source_rows_at_capture=18,
                    destination_rows_verified=18,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            json_path, csv_path = write_reports(report, temporary)
            self.assertEqual(json_path.name, f"report_{execution_id}.json")
            self.assertEqual(csv_path.name, f"report_{execution_id}.csv")
            json_report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                json_report["cdc_database"]["retention_minutes"], 262_800
            )
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            header = lines[0]
        self.assertIn("index_state", header)
        self.assertIn("transfer_mode", header)
        self.assertIn("source_rows_at_capture", header)
        self.assertIn("destination_rows_verified", header)
        self.assertIn("bytes_exported_this_invocation", header)
        self.assertIn("DIRECT_KEYLESS", lines[1])
        self.assertIn(",18,", lines[1])
        self.assertNotIn("indexes_state", header)


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.execution_id = str(uuid.uuid4())
        self.table_id = "c" * 64
        self.state = LocalState(self.directory)
        self.state.start_execution(
            self.execution_id,
            "d" * 64,
            "run",
            "a" * 64,
            "b" * 64,
            True,
        )
        self.state.register_table(
            self.execution_id,
            self.table_id,
            0,
            "dbo",
            "TABELA",
            "bronze",
            "fonte",
            "db_tabela",
            "d" * 64,
            [{"name": "codigo", "direction": "ASC"}],
        )

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def _plan(self, *, final_limit: list[str] | None = None):
        return self.state.plan_block(
            self.execution_id,
            self.table_id,
            1,
            None,
            ["10"],
            final_limit or ["100"],
        )

    def test_block_id_is_stable_across_retries_and_reopen(self):
        self.assertEqual(self.state.path.name, "controle_transferencia.sqlite3")
        self.assertEqual(
            self.state.connection.execute("PRAGMA user_version").fetchone()[0], 5
        )
        self.assertIsNone(
            self.state.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_states'"
            ).fetchone()
        )
        with self.state.execution_lease(self.execution_id):
            lock_path = self.directory / f"execution_{self.execution_id}.lock"
            self.assertTrue(lock_path.is_file())
        first = self._plan()
        self.state.begin_attempt(
            self.execution_id, self.table_id, first["block_id"], "tentativa-1.log"
        )
        second_attempt = self.state.begin_attempt(
            self.execution_id, self.table_id, first["block_id"], "tentativa-2.log"
        )
        self.assertEqual(second_attempt, 2)
        self.state.close()
        self.state = LocalState(self.directory)
        same = self._plan()
        self.assertEqual(same["block_id"], first["block_id"])

    def test_retry_closes_previous_interrupted_attempt(self):
        block = self._plan()
        self.state.begin_attempt(self.execution_id, self.table_id, block["block_id"], "one.log")
        self.state.begin_attempt(self.execution_id, self.table_id, block["block_id"], "two.log")
        attempts = self.state.connection.execute(
            "SELECT attempt,status,finished_at FROM tentativa_lote WHERE execution_id=? AND table_id=? ORDER BY attempt",
            (self.execution_id, self.table_id),
        ).fetchall()
        self.assertNotEqual(attempts[0]["status"], "RUNNING")
        self.assertIsNotNone(attempts[0]["finished_at"])
        self.assertEqual(attempts[1]["status"], "RUNNING")

    def test_existing_block_rejects_changed_bounds_and_final_limit(self):
        self._plan(final_limit=["100"])
        with self.assertRaises(StructuralResumeError):
            self.state.plan_block(
                self.execution_id, self.table_id, 1, None, ["11"], ["100"]
            )
        with self.assertRaises(StructuralResumeError):
            self.state.plan_block(
                self.execution_id, self.table_id, 1, None, ["10"], ["101"]
            )

    def test_export_and_import_checkpoints_are_separate_and_support_bigint(self):
        block = self._plan()
        self.state.begin_attempt(self.execution_id, self.table_id, block["block_id"], "one.log")
        rows = 2**31 + 123_456
        file_bytes = 2**31 + 654_321
        self.state.mark_exported(
            self.execution_id,
            self.table_id,
            block["block_id"],
            "manifest.json",
            rows,
            file_bytes,
            "a" * 64,
            "f" * 64,
        )
        after_export = self.state.table(self.execution_id, self.table_id)
        self.assertEqual(after_export["export_cursor_json"], '["10"]')
        self.assertIsNone(after_export["import_cursor_json"])
        self.assertEqual(after_export["rows_exported"], rows)
        self.assertEqual(after_export["rows_imported"], 0)
        self.assertEqual(after_export["bytes_exported"], file_bytes)

        self.state.mark_imported(self.execution_id, self.table_id, block["block_id"], rows)
        after_import = self.state.table(self.execution_id, self.table_id)
        self.assertEqual(after_import["import_cursor_json"], '["10"]')
        self.assertEqual(after_import["rows_imported"], rows)
        stored = self.state.blocks(self.execution_id, self.table_id)[0]
        self.assertEqual(stored["rows_exported"], rows)
        self.assertEqual(stored["rows_imported"], rows)

    def test_structural_change_blocks_resume_but_operational_change_is_recorded(self):
        resumed = self.state.resume_execution(self.execution_id, "a" * 64, "c" * 64)
        self.assertEqual(resumed["operational_hash"], "b" * 64)
        current = self.state.execution(self.execution_id)
        self.assertEqual(current["operational_hash"], "c" * 64)
        with self.assertRaises(StructuralResumeError):
            self.state.resume_execution(self.execution_id, "d" * 64, "e" * 64)

    def test_local_state_rejects_values_outside_the_english_state_contract(self):
        with self.assertRaisesRegex(ValueError, "status"):
            self.state.update_table(
                self.execution_id, self.table_id, status="PENDENTE"
            )
        with self.assertRaisesRegex(ValueError, "index_state"):
            self.state.update_table(
                self.execution_id, self.table_id, index_state="NAO_INICIADO"
            )
        with self.assertRaisesRegex(ValueError, "execução"):
            self.state.finish_execution(self.execution_id, "CONCLUIDA")

    def test_local_state_rejects_noncanonical_execution_identity(self):
        with self.assertRaisesRegex(ValueError, "execution_id"):
            self.state.start_execution(
                self.execution_id.upper(),
                "d" * 64,
                "run",
                "a" * 64,
                "b" * 64,
                False,
            )

    def test_fail_attempt_is_atomic_diagnostic_and_retriable(self):
        block = self._plan()
        first_attempt = self.state.begin_attempt(
            self.execution_id, self.table_id, block["block_id"], "one.log"
        )
        self.state.fail_attempt(
            self.execution_id,
            self.table_id,
            block["block_id"],
            "BCP_EXIT_1",
            "falha controlada",
        )
        failed = self.state.connection.execute(
            """SELECT status,finished_at,error_code,error_message FROM tentativa_lote
            WHERE execution_id=? AND table_id=? AND block_id=? AND attempt=?""",
            (self.execution_id, self.table_id, block["block_id"], first_attempt),
        ).fetchone()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIsNotNone(failed["finished_at"])
        self.assertEqual(failed["error_code"], "BCP_EXIT_1")
        planned = self.state.blocks(self.execution_id, self.table_id)[0]
        self.assertEqual(planned["status"], "PLANNED")
        self.assertEqual(planned["error_message"], "falha controlada")
        self.assertEqual(
            self.state.begin_attempt(
                self.execution_id, self.table_id, block["block_id"], "two.log"
            ),
            2,
        )

    def test_refresh_unstarted_table_updates_provisional_contract_only(self):
        self.state.update_table(
            self.execution_id,
            self.table_id,
            status="SKIPPED_NO_KEY",
            reason="provisorio",
        )
        self.state.refresh_unstarted_table(
            self.execution_id,
            self.table_id,
            destination_area="landing",
            destination_schema="s344",
            destination_table="destino_final",
            structural_hash="e" * 64,
            watermark=[{"name": "id", "direction": "ASC"}],
        )
        refreshed = self.state.table(self.execution_id, self.table_id)
        self.assertEqual(refreshed["status"], "PENDING")
        self.assertEqual(refreshed["destination_area"], "landing")
        self.assertEqual(refreshed["destination_table"], "destino_final")
        self.assertEqual(refreshed["structural_hash"], "e" * 64)
        self.assertEqual(refreshed["watermark_json"], '[{"direction":"ASC","name":"id"}]')
        self.assertIsNone(refreshed["reason"])

        self._plan()
        with self.assertRaises(StructuralResumeError):
            self.state.refresh_unstarted_table(
                self.execution_id,
                self.table_id,
                destination_area="bronze",
                destination_schema="outra",
                destination_table="outra",
                structural_hash="f" * 64,
            )

    def test_v2_rejects_column_drift_without_mutating_file(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            state = LocalState(directory)
            state.close()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "ALTER TABLE execucao RENAME COLUMN command TO legacy_command"
                )
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "colunas divergentes"):
                LocalState(directory)
            with closing(sqlite3.connect(path)) as connection:
                names = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(execucao)")
                ]
            self.assertIn("legacy_command", names)
            self.assertNotIn("command", names)

    def test_v2_rejects_missing_required_index(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            state = LocalState(directory)
            state.close()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP INDEX ux_execucao_tabela_destino")
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "indice requerido"):
                LocalState(directory)

    def test_v4_rejects_unversioned_indexes_views_and_triggers(self):
        mutations = (
            "CREATE INDEX ix_unversioned ON execucao(status)",
            "CREATE VIEW view_unversioned AS SELECT execution_id FROM execucao",
            """CREATE TRIGGER trigger_unversioned AFTER UPDATE ON execucao
            BEGIN SELECT 1; END""",
        )
        for statement in mutations:
            with self.subTest(statement=statement), tempfile.TemporaryDirectory() as raw_directory:
                directory = Path(raw_directory).resolve()
                state = LocalState(directory)
                state.close()
                path = directory / "controle_transferencia.sqlite3"
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(statement)
                    connection.commit()
                with self.assertRaisesRegex(StateVersionError, "objetos nao versionados"):
                    LocalState(directory)

    def test_v4_rejects_foreign_key_signature_drift(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            state = LocalState(directory)
            state.close()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA writable_schema=ON")
                connection.execute(
                    """UPDATE sqlite_master
                    SET sql=replace(sql,' REFERENCES execucao(execution_id)','')
                    WHERE type='table' AND name='execucao_tabela'"""
                )
                connection.execute("PRAGMA writable_schema=OFF")
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "chaves estrangeiras"):
                LocalState(directory)

    def test_previous_v3_control_requires_explicit_migration(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            state = LocalState(directory)
            state.close()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version=3")
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "vers.o 3.*migra"):
                LocalState(directory)

    def test_unversioned_existing_database_is_not_silently_migrated(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE legacy_state (value TEXT)")
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "user_version=0"):
                LocalState(directory)
            with closing(sqlite3.connect(path)) as connection:
                objects = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(objects, {"legacy_state"})

    def test_explicit_incompatible_schema_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            state = LocalState(directory)
            state.close()
            path = directory / "controle_transferencia.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version=6")
                connection.commit()
            with self.assertRaisesRegex(StateVersionError, "vers.o 6"):
                LocalState(directory)


if __name__ == "__main__":
    unittest.main()
