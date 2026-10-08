from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import uuid

from bcp_engine.cli import main
from bcp_engine.state import LocalState, StateVersionError
from bcp_engine.state_migration import migrate_local_state_v3_to_v4


LEGACY_INDEX_STATES_DDL = """
CREATE TABLE index_states (
  execution_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  index_name TEXT NOT NULL,
  state TEXT NOT NULL,
  error_message TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (execution_id, table_id, index_name),
  FOREIGN KEY (execution_id, table_id) REFERENCES table_runs(execution_id, table_id)
)
"""


class LocalStateMigrationTests(unittest.TestCase):
    def _build_v3(self, directory: Path) -> tuple[Path, str, str, str]:
        execution_id = str(uuid.uuid4())
        table_id = "a" * 64
        block_id: str
        with LocalState(directory) as state:
            state.start_execution(
                execution_id,
                "b" * 64,
                "run",
                "c" * 64,
                "d" * 64,
                True,
                "config.json",
            )
            state.register_table(
                execution_id,
                table_id,
                1,
                "dbo",
                "T_SOURCE",
                "bronze",
                "source",
                "db_t_source",
                "e" * 64,
                [{"name": "id", "direction": "ASC"}],
            )
            block = state.plan_block(
                execution_id,
                table_id,
                1,
                None,
                ["1500"],
                ["3000"],
            )
            block_id = str(block["block_id"])
            state.begin_attempt(
                execution_id,
                table_id,
                block_id,
                "attempt-1.log",
            )

        current_path = directory / "controle_transferencia.sqlite3"
        path = directory / "bcp_control_v2.sqlite3"
        current_path.replace(path)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DROP INDEX ux_execucao_tabela_destino")
            for current_name, legacy_name in (
                ("metadados", "meta"),
                ("execucao", "executions"),
                ("execucao_tabela", "table_runs"),
                ("execucao_lote", "blocks"),
                ("tentativa_lote", "attempts"),
            ):
                connection.execute(
                    f'ALTER TABLE "{current_name}" RENAME TO "{legacy_name}"'
                )
            connection.execute(
                """CREATE UNIQUE INDEX ux_table_destination
                ON table_runs(execution_id,destination_area,destination_schema,destination_table)
                WHERE destination_table IS NOT NULL"""
            )
            connection.execute(LEGACY_INDEX_STATES_DDL)
            connection.execute(
                """INSERT INTO index_states
                (execution_id,table_id,index_name,state,error_message,updated_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    execution_id,
                    table_id,
                    "ix_t_source_01",
                    "PENDING",
                    "diagnóstico legado",
                    "2026-10-06T12:00:00.000000+00:00",
                ),
            )
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        return path, execution_id, table_id, block_id

    @staticmethod
    def _current_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
        primary_keys = {
            "metadados": "key",
            "execucao": "execution_id",
            "execucao_tabela": "execution_id,table_id",
            "execucao_lote": "execution_id,table_id,block_id",
            "tentativa_lote": "execution_id,table_id,block_id,attempt",
        }
        with closing(sqlite3.connect(path)) as connection:
            return {
                table: list(
                    connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
                )
                for table, order in primary_keys.items()
            }

    @staticmethod
    def _core_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
        primary_keys = {
            "meta": "key",
            "executions": "execution_id",
            "table_runs": "execution_id,table_id",
            "blocks": "execution_id,table_id,block_id",
            "attempts": "execution_id,table_id,block_id,attempt",
        }
        with closing(sqlite3.connect(path)) as connection:
            return {
                table: list(
                    connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY {order}'
                    )
                )
                for table, order in primary_keys.items()
            }

    def test_explicit_v3_to_current_preserves_core_and_archives_legacy_rows(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            path, execution_id, table_id, block_id = self._build_v3(directory)
            before = self._core_snapshot(path)

            with self.assertRaisesRegex(StateVersionError, "vers.o 3.*migra"):
                LocalState(directory)

            report = migrate_local_state_v3_to_v4(directory)
            self.assertTrue(report.migrated)
            self.assertEqual((report.from_version, report.to_version), (3, 5))
            self.assertEqual(report.archived_index_state_rows, 1)
            self.assertIsNotNone(report.backup_path)

            backup = Path(str(report.backup_path))
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                archived = connection.execute(
                    "SELECT execution_id,table_id,index_name,state,error_message "
                    "FROM index_states"
                ).fetchone()
            self.assertEqual(
                archived,
                (
                    execution_id,
                    table_id,
                    "ix_t_source_01",
                    "PENDING",
                    "diagnóstico legado",
                ),
            )

            self.assertEqual(self._core_snapshot(path), before)
            with LocalState(directory) as migrated:
                self.assertEqual(
                    migrated.connection.execute("PRAGMA user_version").fetchone()[0],
                    5,
                )
                self.assertIsNotNone(migrated.execution(execution_id))
                self.assertIsNotNone(migrated.table(execution_id, table_id))
                blocks = migrated.blocks(execution_id, table_id)
                self.assertEqual([row["block_id"] for row in blocks], [block_id])
                self.assertIsNone(
                    migrated.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='index_states'"
                    ).fetchone()
                )

    def test_migration_is_idempotent_for_valid_current_control(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            path, *_ = self._build_v3(directory)
            first = migrate_local_state_v3_to_v4(directory)
            current = directory / "controle_transferencia.sqlite3"
            before = self._current_snapshot(current)
            second = migrate_local_state_v3_to_v4(directory)
            self.assertTrue(first.migrated)
            self.assertFalse(second.migrated)
            self.assertEqual((second.from_version, second.to_version), (5, 5))
            self.assertIsNone(second.backup_path)
            self.assertEqual(self._current_snapshot(current), before)

    def test_opening_exact_v4_legacy_control_copies_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            legacy, execution_id, table_id, _block_id = self._build_v3(directory)
            with closing(sqlite3.connect(legacy)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("DROP TABLE index_states")
                connection.execute("PRAGMA user_version=4")
                connection.commit()
            legacy_before = self._core_snapshot(legacy)

            with LocalState(directory) as migrated:
                self.assertEqual(migrated.path.name, "controle_transferencia.sqlite3")
                self.assertEqual(
                    migrated.connection.execute("PRAGMA user_version").fetchone()[0],
                    5,
                )
                self.assertIsNotNone(migrated.execution(execution_id))
                self.assertIsNotNone(migrated.table(execution_id, table_id))

            self.assertTrue(legacy.is_file())
            self.assertEqual(self._core_snapshot(legacy), legacy_before)
            with closing(sqlite3.connect(legacy)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_existing_identical_backup_is_reused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            path, *_ = self._build_v3(directory)
            backup = directory / "operator-selected-v3.sqlite3"
            shutil.copy2(path, backup)
            original_backup = backup.read_bytes()

            report = migrate_local_state_v3_to_v4(directory, backup_path=backup)
            self.assertTrue(report.migrated)
            self.assertEqual(Path(str(report.backup_path)), backup)
            self.assertEqual(backup.read_bytes(), original_backup)

    def test_v2_and_unversioned_controls_remain_fail_closed(self):
        for version in (0, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as raw_directory:
                directory = Path(raw_directory).resolve()
                path = directory / "bcp_control_v2.sqlite3"
                if version == 0:
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("CREATE TABLE legacy(value TEXT)")
                        connection.execute("INSERT INTO legacy VALUES('keep-me')")
                        connection.commit()
                else:
                    path, *_ = self._build_v3(directory)
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("PRAGMA user_version=2")
                        connection.commit()

                with self.assertRaisesRegex(StateVersionError, "vers.es 3, 4 ou 5"):
                    migrate_local_state_v3_to_v4(directory)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        version,
                    )
                self.assertFalse(
                    path.with_name(path.name + ".v3.backup").exists()
                )

    def test_structurally_modified_v3_is_rejected_before_backup_or_publish(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            path, *_ = self._build_v3(directory)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE VIEW unexpected_view AS SELECT execution_id FROM executions"
                )
                connection.commit()
            before = self._core_snapshot(path)

            with self.assertRaisesRegex(StateVersionError, "objetos nao versionados"):
                migrate_local_state_v3_to_v4(directory)

            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='unexpected_view'"
                    ).fetchone()
                )
            self.assertEqual(self._core_snapshot(path), before)
            self.assertFalse(path.with_name(path.name + ".v3.backup").exists())

    def test_cli_exposes_explicit_migration_without_config(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory).resolve()
            self._build_v3(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["migrate-control", "--control-directory", str(directory)]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["migrated"])
            self.assertEqual(payload["from_version"], 3)
            self.assertEqual(payload["to_version"], 5)


if __name__ == "__main__":
    unittest.main()
