from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from bcp_engine.artifacts import ArtifactStore
from bcp_engine.batching import BatchWindow
from bcp_engine.catalog import WatermarkColumn, WatermarkSelection
from bcp_engine.config import read_config
from bcp_engine.engine import BcpEngine, PreparedTable, _table_id
from bcp_engine.importer import build_import_sql
from bcp_engine.models import TableResult, TableStatus
from bcp_engine.profiles import resolve_profile
from bcp_engine.projection import (
    build_import_plan,
    layout_contract,
    layout_from_contract,
    projection_contract,
)
from bcp_engine.state import LocalState, StructuralResumeError
from bcp_engine.util import sha256_json, stable_json


ROOT = Path(__file__).resolve().parents[1]


def source_column(name: str = "codigo") -> dict:
    return {
        "name": name,
        "type_name": "int",
        "max_length": 4,
        "precision": 10,
        "scale": 0,
        "is_nullable": False,
        "collation_name": None,
        "is_identity": False,
        "is_computed": False,
    }


def portable_components(*, schema: str = "s344", partitioned: bool = False):
    columns = [source_column()]
    layout = resolve_profile(
        "bronze",
        columns,
        source_database="BD_ORIGEM",
        source_table="TABELA_ORIGEM_01",
        destination_schema=schema,
        destination_table="bd_origem_tabela_origem_01",
        partition_column="dh_carga" if partitioned else None,
    )
    projection = projection_contract(layout, columns)
    physical = layout_contract(layout)
    effective = {
        "index": 0,
        "rows_per_block": 100,
        "source": {
            "instance": r"SQL\ORIGEM",
            "database": "BD_ORIGEM",
            "read_database": "BD_ORIGEM",
            "schema": "ESQUEMA_ORIGEM",
            "table": "TABELA_ORIGEM_01",
        },
        "destination": {
            "area": "bronze",
            "instance": r"SQL\DESTINO",
            "database": "DBRO684",
            "schema": schema,
            "table": "bd_origem_tabela_origem_01",
            "structure_profile": "templates/bronze.json",
        },
    }
    watermark = SimpleNamespace(
        columns=(WatermarkColumn(name="codigo", type_name="int"),), warnings=()
    )
    prepared = PreparedTable(
        effective=effective,
        table_id=_table_id(effective),
        columns=columns,
        watermark=watermark,
        layout=layout,
        projection=projection,
        layout_contract=physical,
        table_structural_hash="a" * 64,
        final_limit=("100",),
    )
    engine = BcpEngine.__new__(BcpEngine)
    engine.config = {
        "source": {"authentication": {"type": "windows_integrated"}},
        "execute_import": False,
        "keyless_direct_load_max_rows": 5_000_000,
    }
    engine.config_directory = ROOT
    return engine, prepared


def draft_manifest(
    engine: BcpEngine,
    prepared: PreparedTable,
    *,
    execution_id: str,
    block_id: str,
    block_number: int = 1,
    lower: tuple[str, ...] | None = None,
    upper: tuple[str, ...] = ("10",),
    ceiling: tuple[str, ...] = ("100",),
    dataset_id: str = "d" * 64,
    empty: bool = False,
):
    return engine._manifest_for(
        prepared,
        execution_id,
        dataset_id,
        {"block_id": block_id, "block_number": block_number},
        1,
        BatchWindow(lower, upper, ceiling, 0 if empty else 1, 0, 100),
        0 if empty else 1,
        "2026-10-06T12:00:00+00:00",
        {"login": "u684"},
        None,
        empty,
    )


def publish(
    store: ArtifactStore,
    manifest,
    *,
    payload: bytes = b"row",
):
    paths = store.block_paths(
        manifest.table_id, manifest.block_number, manifest.block_id
    )
    paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
    if not manifest.empty_range:
        paths.partial.write_bytes(payload)
    return paths, store.publish(paths, manifest)


class ManifestLayoutSnapshotTests(unittest.TestCase):
    def test_partition_contract_round_trips_and_legacy_hash_shape_is_preserved(self):
        _engine, legacy = portable_components()
        _engine, prepared = portable_components(partitioned=True)

        self.assertNotIn("partition", legacy.layout_contract)
        self.assertIn("partition", prepared.layout_contract)
        self.assertEqual(
            prepared.layout_contract["partition"]["column"], "dh_carga"
        )
        rebuilt = layout_from_contract(
            prepared.layout_contract,
            source_database=prepared.layout.source_database,
            source_table=prepared.layout.source_table,
        )
        self.assertEqual(rebuilt.partition, prepared.layout.partition)
        self.assertEqual(
            layout_contract(rebuilt)["layout_hash"],
            prepared.layout_contract["layout_hash"],
        )

    def test_partition_contract_tampering_is_rejected_by_manifest_hash(self):
        engine, prepared = portable_components(partitioned=True)
        draft = draft_manifest(
            engine,
            prepared,
            execution_id=str(uuid.uuid4()),
            block_id=str(uuid.uuid4()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary).resolve(), draft.execution_id)
            _paths, manifest = publish(store, draft)
            manifest.layout_contract["partition"]["scheme_name"] = "ps_adulterado"

            with self.assertRaisesRegex(ValueError, "layout_hash"):
                manifest.validate()

    def test_manifest_carries_full_layout_but_sql_uses_trusted_local_profile(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        draft = draft_manifest(
            engine, prepared, execution_id=execution_id, block_id=block_id
        )

        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary).resolve(), execution_id)
            _paths, manifest = publish(store, draft)

            snapshot = manifest.layout_contract
            self.assertTrue(
                {
                    "version",
                    "profile",
                    "profile_version",
                    "schema",
                    "table",
                    "columns",
                    "primary_key",
                    "indexes",
                    "sequence",
                    "technical_id_strategy",
                    "technical_id_source_column",
                    "fill_rules",
                    "layout_hash",
                }.issubset(snapshot),
            )
            self.assertEqual(stable_json(snapshot), stable_json(prepared.layout_contract))
            self.assertEqual(snapshot["layout_hash"], manifest.layout_hash)
            self.assertTrue(snapshot["columns"])
            self.assertTrue(snapshot["indexes"])
            self.assertIsNotNone(snapshot["sequence"])

            configured = {
                "destination": {
                    "area": "bronze",
                    "schema": "s_portavel",
                    "table": "bd_origem_tabela_origem_01",
                    "structure_profile": "bronze",
                }
            }
            with patch(
                "bcp_engine.engine.resolve_profile",
                wraps=resolve_profile,
            ) as profile_loader:
                rebuilt = engine._resolve_manifest_layout(manifest, configured)

        profile_loader.assert_called_once()
        self.assertEqual(rebuilt.schema, "s_portavel")
        self.assertEqual(rebuilt.columns, prepared.layout.columns)
        self.assertEqual(rebuilt.primary_key, prepared.layout.primary_key)
        self.assertEqual(rebuilt.indexes, prepared.layout.indexes)
        self.assertEqual(rebuilt.sequence, prepared.layout.sequence)
        self.assertEqual(
            layout_contract(rebuilt)["layout_hash"], manifest.layout_hash
        )

    def test_tampered_embedded_expressions_are_never_accepted_as_runtime_layout(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        draft = draft_manifest(
            engine,
            prepared,
            execution_id=execution_id,
            block_id=str(uuid.uuid4()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary).resolve(), execution_id)
            _paths, manifest = publish(store, draft)

            manifest.layout_contract["fill_rules"][-1]["value"] = (
                "0); DROP TABLE dbo.victim;--"
            )
            hash_value = {
                key: value
                for key, value in manifest.layout_contract.items()
                if key not in {"schema", "layout_hash"}
            }
            manifest.layout_hash = sha256_json(hash_value)
            manifest.layout_contract["layout_hash"] = manifest.layout_hash
            manifest.validate()

            configured = {
                "destination": {
                    "area": "bronze",
                    "schema": "s344",
                    "table": "bd_origem_tabela_origem_01",
                    "structure_profile": "bronze",
                }
            }
            with patch(
                "bcp_engine.engine.resolve_profile", wraps=resolve_profile
            ) as profile_loader:
                with self.assertRaisesRegex(RuntimeError, "layout/projeção|perfil local"):
                    engine._resolve_manifest_layout(manifest, configured)

        profile_loader.assert_called_once()

    def test_custom_profile_remains_portable_when_trusted_copy_exists_locally(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        draft = draft_manifest(
            engine,
            prepared,
            execution_id=execution_id,
            block_id=str(uuid.uuid4()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = ArtifactStore(root, execution_id)
            _paths, manifest = publish(store, draft)
            custom_profile = root / "trusted_bronze.json"
            custom_profile.write_text(
                (ROOT / "templates" / "bronze.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            engine.config_directory = root
            configured = {
                "destination": {
                    "area": "bronze",
                    "schema": "s_portavel",
                    "table": "bd_origem_tabela_origem_01",
                    "structure_profile": custom_profile.name,
                }
            }
            rebuilt = engine._resolve_manifest_layout(manifest, configured)

        self.assertEqual(rebuilt.schema, "s_portavel")
        self.assertEqual(layout_contract(rebuilt)["layout_hash"], manifest.layout_hash)

    def test_initially_empty_table_manifest_survives_transfer_without_data_file(self):
        engine, prepared = portable_components(schema="schema_exportador")
        execution_id = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        draft = draft_manifest(
            engine,
            prepared,
            execution_id=execution_id,
            block_id=block_id,
            upper=(),
            ceiling=(),
            empty=True,
        )

        with tempfile.TemporaryDirectory() as export_dir, tempfile.TemporaryDirectory() as import_dir:
            export_store = ArtifactStore(Path(export_dir).resolve(), execution_id)
            paths, published = publish(export_store, draft)
            shutil.copytree(
                export_store.execution_root,
                Path(import_dir).resolve() / execution_id,
            )
            import_store = ArtifactStore(Path(import_dir).resolve(), execution_id)
            relative_manifest = paths.manifest.relative_to(export_store.execution_root)
            transferred = import_store.load_and_verify(
                import_store.execution_root / relative_manifest
            )

        self.assertTrue(published.table_empty)
        self.assertTrue(transferred.table_empty)
        self.assertTrue(transferred.empty_range)
        self.assertIsNone(transferred.lower_bound)
        self.assertEqual(transferred.upper_bound, [])
        self.assertEqual(transferred.final_limit, [])
        self.assertEqual(transferred.rows_exported, 0)
        self.assertEqual(transferred.file_bytes, 0)
        self.assertIsNone(transferred.data_file)
        self.assertIsNone(transferred.data_sha256)
        self.assertEqual(
            stable_json(transferred.layout_contract),
            stable_json(prepared.layout_contract),
        )
        transferred.validate()
        sql = build_import_sql(
            transferred,
            build_import_plan(prepared.layout),
            None,
            r"D:\BCP\bloco.xml",
            "dbo",
        )
        self.assertNotIn(
            "(SELECT COUNT_BIG(*) FROM OPENJSON(@upper))<>1", sql
        )
        self.assertNotIn(
            "(SELECT COUNT_BIG(*) FROM OPENJSON(@ceiling))<>1", sql
        )


class ImportSetValidationTests(unittest.TestCase):
    class Destination:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class PartialImporter:
        def __init__(self):
            self.imported: list[str] = []
            self.finished_tables: list[tuple[tuple, dict]] = []
            self.finished_executions: list[tuple[tuple, dict]] = []

        def ensure_control(self):
            pass

        def register_execution_table(self, **_kwargs):
            pass

        def compatible_table_binding_exists(self, **_kwargs):
            return False

        def block_is_confirmed(self, _manifest):
            return False

        def import_manifest(self, store, path, _plan, _executor_root, _sql_root):
            manifest = store.load_and_verify(path)
            self.imported.append(manifest.block_id)
            return manifest

        def table_is_complete(self, _manifest):
            return False

        def finish_table(self, *args, **kwargs):
            self.finished_tables.append((args, kwargs))

        def finish_execution(self, *args, **kwargs):
            self.finished_executions.append((args, kwargs))

    @staticmethod
    def _import_engine(root: Path):
        config = read_config(ROOT / "examples" / "config.full.json")
        config["executor_directory"] = str(root)
        config["local_control_directory"] = str(root / "control")
        config["destination_sql_directory"] = r"D:\BCP"
        engine = BcpEngine(config, config_path=ROOT / "config.runtime.json")
        destination = ImportSetValidationTests.Destination()
        engine._connect_source = Mock(
            side_effect=AssertionError("importacao nao pode abrir origem")
        )
        engine._connect_destination = Mock(return_value=(destination, object()))
        engine._destination_table_exists = Mock(return_value=False)
        engine._provision_initial = Mock()
        engine._finish_indexes = Mock()
        engine._sql_path_probe = Mock()
        return engine, destination

    def test_entire_manifest_set_is_prevalidated_before_destination_connection(self):
        exporter, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = ArtifactStore(root, execution_id)
            first = draft_manifest(
                exporter,
                prepared,
                execution_id=execution_id,
                block_id=str(uuid.uuid4()),
            )
            second = draft_manifest(
                exporter,
                prepared,
                execution_id=execution_id,
                block_id=str(uuid.uuid4()),
                block_number=2,
                lower=("10",),
                upper=("20",),
                dataset_id="e" * 64,
            )
            publish(store, first, payload=b"first")
            publish(store, second, payload=b"second")
            engine, destination = self._import_engine(root)

            with patch("bcp_engine.engine.LocalState") as local_state:
                with self.assertRaisesRegex(
                    RuntimeError, "mistura datasets estruturais"
                ):
                    engine.import_manifests(store.execution_root)

        engine._connect_source.assert_not_called()
        engine._connect_destination.assert_not_called()
        local_state.assert_not_called()
        self.assertFalse(destination.closed)

    def test_partial_subset_does_not_finalize_secondary_indexes(self):
        exporter, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = ArtifactStore(root, execution_id)
            draft = draft_manifest(
                exporter,
                prepared,
                execution_id=execution_id,
                block_id=str(uuid.uuid4()),
                upper=("10",),
                ceiling=("100",),
            )
            paths, _manifest = publish(store, draft)
            engine, destination = self._import_engine(root)
            importer = self.PartialImporter()

            with patch(
                "bcp_engine.engine.DestinationImporter", return_value=importer
            ):
                report = engine.import_manifests(paths.manifest)

        self.assertEqual(report.exit_code(), 2)
        self.assertEqual(
            report.tables[0].status, TableStatus.PARTIALLY_IMPORTED.value
        )
        self.assertEqual(report.tables[0].index_state, "DEFERRED")
        self.assertEqual(report.tables[0].rows_imported, 1)
        engine._finish_indexes.assert_not_called()
        self.assertEqual(importer.finished_tables[-1][1]["state"], TableStatus.PARTIALLY_IMPORTED.value)
        self.assertEqual(importer.finished_tables[-1][1]["index_state"], "DEFERRED")
        self.assertEqual(
            importer.finished_executions[-1][1]["state"],
            "COMPLETED_WITH_PENDING_ITEMS",
        )
        self.assertTrue(destination.closed)


class PublishedBlockReconciliationTests(unittest.TestCase):
    def test_direct_keyless_process_is_one_idempotent_block_with_exact_count(self):
        engine, prepared = portable_components()
        prepared.watermark = WatermarkSelection(
            columns=(),
            source="direct_keyless",
            index_name=None,
            is_unique=False,
            transfer_mode="DIRECT_KEYLESS",
            captured_row_count=3,
            direct_reason="sem chave elegível",
        )
        prepared.final_limit = ()
        engine.config.update(
            {
                "bcp_timeout_seconds": 0,
                "max_file_bytes": 10_000_000,
                "minimum_free_space_bytes": 0,
                "delete_confirmed_files": False,
            }
        )
        execution_id = str(uuid.uuid4())

        class Invocation:
            def __init__(self):
                self.query = None
                self.path = None

            def with_operation(self, query, _operation, path):
                child = Invocation()
                child.query = query
                child.path = path
                return child

        class Runner:
            def __init__(self):
                self.queries = []

            def run(self, invocation, _log, **_kwargs):
                self.queries.append(invocation.query)
                Path(invocation.path).write_bytes(b"three-native-rows")
                return SimpleNamespace(rows_copied=3)

        runner = Runner()
        engine.bcp_runner = runner
        result = TableResult("TABELA_ORIGEM_01", None, TableStatus.RUNNING.value)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = ArtifactStore(root, execution_id)
            with LocalState(root / "control") as state:
                state.start_execution(
                execution_id, "d" * 64, "run", "a" * 64, "b" * 64, False
                )
                state.register_table(
                    execution_id,
                    prepared.table_id,
                    0,
                    "dbo",
                    "TABELA_ORIGEM_01",
                    None,
                    None,
                    None,
                    prepared.table_structural_hash,
                    {
                        "transfer_mode": "DIRECT_KEYLESS",
                        "source_rows_at_capture": 3,
                        "watermark": [],
                    },
                )
                state.update_table(
                    execution_id,
                    prepared.table_id,
                    final_limit_json="[]",
                    status=TableStatus.RUNNING.value,
                )

                def write_format(_runner, _base, _source, _columns, path, _log, **_kwargs):
                    Path(path).write_text("<BCPFORMAT />", encoding="utf-8")

                with patch("bcp_engine.engine.make_format_file", side_effect=write_format):
                    engine._process_blocks(
                        object(),
                        None,
                        None,
                        state,
                        store,
                        Invocation(),
                        prepared,
                        execution_id,
                        "d" * 64,
                        {"login": "u684"},
                        None,
                        result,
                    )
                    replay = TableResult(
                        "TABELA_ORIGEM_01", None, TableStatus.RUNNING.value
                    )
                    engine._process_blocks(
                        object(),
                        None,
                        None,
                        state,
                        store,
                        Invocation(),
                        prepared,
                        execution_id,
                        "d" * 64,
                        {"login": "u684"},
                        None,
                        replay,
                    )

            manifests = list(root.rglob("*.manifest.json"))
            self.assertEqual(len(manifests), 1)
            loaded = store.load_and_verify(manifests[0])

        self.assertEqual(len(runner.queries), 1)
        self.assertIn("WITH (HOLDLOCK, TABLOCK)", runner.queries[0])
        self.assertNotIn("ORDER BY", runner.queries[0].upper())
        self.assertEqual(result.rows_exported, 3)
        self.assertEqual(replay.rows_exported, 3)
        self.assertEqual(result.bytes_exported_this_invocation, len(b"three-native-rows"))
        self.assertEqual(replay.bytes_exported_this_invocation, 0)
        self.assertEqual(replay.last_exported, [])
        self.assertIsNone(replay.last_imported)
        self.assertEqual(replay.retained_files, 1)
        replay.duration_seconds = 0.01
        self.assertIsNone(replay.throughput_bytes_per_second)
        self.assertEqual(loaded.transfer_mode, "DIRECT_KEYLESS")
        self.assertEqual(loaded.watermark, [])
        self.assertEqual(loaded.source_rows_at_capture, 3)

    def test_serialized_snapshot_reconciles_with_the_same_planned_block(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        block = {"block_id": block_id, "block_number": 1}
        window = BatchWindow(None, ("10",), ("100",), 1, 0, 100)
        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary).resolve(), execution_id)
            _paths, loaded = publish(
                store,
                draft_manifest(
                    engine,
                    prepared,
                    execution_id=execution_id,
                    block_id=block_id,
                ),
            )

            engine._validate_manifest_for_planned_block(
                loaded,
                prepared,
                execution_id=execution_id,
                dataset_id="d" * 64,
                block=block,
                window=window,
            )

    def test_retrying_identical_layout_snapshot_publication_is_idempotent(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            store = ArtifactStore(Path(temporary).resolve(), execution_id)
            paths, first = publish(
                store,
                draft_manifest(
                    engine,
                    prepared,
                    execution_id=execution_id,
                    block_id=block_id,
                ),
            )
            retried = store.publish(
                paths,
                draft_manifest(
                    engine,
                    prepared,
                    execution_id=execution_id,
                    block_id=block_id,
                ),
            )

        self.assertEqual(retried.block_id, first.block_id)
        self.assertEqual(retried.layout_hash, first.layout_hash)

    def test_valid_manifest_shifted_to_another_durable_block_is_rejected(self):
        engine, prepared = portable_components()
        execution_id = str(uuid.uuid4())
        expected_block_id = str(uuid.uuid4())
        displaced = draft_manifest(
            engine,
            prepared,
            execution_id=execution_id,
            block_id=str(uuid.uuid4()),
            lower=("10",),
            upper=("20",),
        )
        engine.config.update(
            {
                "execute_import": True,
                "bronze_event_id": {"strategy": "sequence"},
                "executor_directory": r"C:\BCP",
                "destination_sql_directory": r"D:\BCP",
                "delete_confirmed_files": False,
            }
        )
        displaced.complete = True
        displaced.data_file = "block_000000000001.bcp"
        displaced.data_sha256 = "a" * 64
        displaced.file_bytes = 3
        displaced.format_file = "block_000000000001.xml"
        displaced.format_sha256 = "f" * 64
        displaced.validate()

        local_block = {
            "status": "EXPORTED",
            "block_id": expected_block_id,
            "block_number": 1,
            "manifest_path": r"C:\BCP\bloco.manifest.json",
            "lower_bound_json": None,
            "upper_bound_json": json.dumps(["10"]),
            "final_limit_json": json.dumps(["100"]),
            "rows_exported": 1,
            "rows_imported": 0,
            "file_bytes": 3,
        }

        class State:
            def blocks(self, _execution_id, _table_id):
                return [local_block]

            def table(self, _execution_id, _table_id):
                return {"export_cursor_json": json.dumps(["100"])}

            def mark_imported(self, *_args):
                raise AssertionError("bloco deslocado nao pode avancar o checkpoint")

        importer = Mock()
        importer.import_manifest.return_value = displaced
        store = Mock()
        store.load_for_reconciliation.return_value = displaced
        store.load_and_verify.return_value = displaced
        result = TableResult("TABELA_ORIGEM_01", "bd_origem_tabela_origem_01", TableStatus.RUNNING.value)

        with self.assertRaisesRegex(StructuralResumeError, "bloco|Manifesto"):
            engine._process_blocks(
                object(),
                object(),
                importer,
                State(),
                store,
                object(),
                prepared,
                execution_id,
                "d" * 64,
                {"login": "u684"},
                {"login": "u684"},
                result,
            )
        importer.import_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
