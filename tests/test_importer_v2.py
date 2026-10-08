from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from bcp_engine.artifacts import (
    MAX_JSON_ARTIFACT_BYTES,
    ArtifactStore,
    _ArtifactReadLease,
    read_bounded_json,
)
from bcp_engine.config import effective_tables, read_config
from bcp_engine.engine import BcpEngine, _table_id
from bcp_engine.importer import (
    DestinationImporter,
    build_import_sql,
    control_ddl,
    control_object_names,
    import_parameters,
    sql_artifact_path,
)
from bcp_engine.models import BlockManifest
from bcp_engine.planner import DestinationSpaceAssessment
from bcp_engine.profiles import resolve_profile
from bcp_engine.projection import build_import_plan, layout_contract, projection_contract
from bcp_engine.state import LocalState


ROOT = Path(__file__).resolve().parents[1]

BRASILIA_TIMESTAMP_SQL = (
    "CONVERT(DATETIME2(7), ((SYSUTCDATETIME() AT TIME ZONE 'UTC') "
    "AT TIME ZONE 'E. South America Standard Time'))"
)


def source_column(name: str, kind: str = "int") -> dict:
    return {
        "name": name,
        "type_name": kind,
        "max_length": 4,
        "precision": 10,
        "scale": 0,
        "is_nullable": False,
        "collation_name": None,
        "is_identity": False,
        "is_computed": False,
    }


def complete_manifest(
    *,
    execution_id: str | None = None,
    block_id: str | None = None,
    rows: int = 5,
    file_bytes: int = 100,
    empty: bool = False,
    destination: dict | None = None,
    projection: list[dict] | None = None,
    projection_hash: str = "1" * 64,
    layout_hash: str = "2" * 64,
) -> BlockManifest:
    return BlockManifest(
        manifest_version=2,
        complete=True,
        execution_id=execution_id or str(uuid.uuid4()),
        dataset_id="d" * 64,
        table_id="e" * 64,
        block_id=block_id or str(uuid.uuid4()),
        block_number=1,
        attempt=1,
        source={
            "instance": r"SQL\ORIGEM",
            "database": "BD_ORIGEM",
            "read_database": "BD_ORIGEM",
            "schema": "dbo",
            "table": "TABELA_ORIGEM_01",
        },
        destination=destination if destination is not None else {
            "area": "bronze",
            "instance": r"SQL\DESTINO",
            "database": "DBRO684",
            "schema": "s344",
            "table": "bd_origem_tabela_origem_01",
        },
        authentication={"source": "descritor-sem-segredo", "destination": "descritor-sem-segredo"},
        profile="bronze",
        projection=projection or [source_column("codigo")],
        projection_hash=projection_hash,
        layout_hash=layout_hash,
        watermark=[{"name": "codigo", "direction": "ASC", "type_name": "int", "collation": None}],
        lower_bound=None,
        upper_bound=["10"],
        final_limit=["100"],
        rows_exported=0 if empty else rows,
        file_bytes=0 if empty else file_bytes,
        data_file=None if empty else "block_000000000001.bcp",
        data_sha256=None if empty else "a" * 64,
        format_file="block_000000000001.xml",
        format_sha256="f" * 64,
        exported_at="2026-10-06T12:00:00+00:00",
        empty_range=empty,
    )


def bronze_plan() -> dict:
    return {
        "target_schema": "s344",
        "target_table": "bd_origem_tabela_origem_01",
        "insert_columns": [
            "id_bd_origem_tabela_origem_01", "codigo", "bi_lsn_evento",
            "bi_sequencia_evento", "cd_operacao", "dh_carga", "updated_at",
        ],
        "select_expressions": [
            "NEXT VALUE FOR [s344].[seq_bd_origem_tabela_origem_01]", "b.[codigo]",
            "CONVERT(BINARY(10),0)", "CONVERT(BINARY(10),0)", "2",
            BRASILIA_TIMESTAMP_SQL, "NULL",
        ],
    }


class ImportSqlTests(unittest.TestCase):
    def test_data_control_and_checkpoint_are_one_idempotent_transaction(self):
        manifest = complete_manifest()
        sql = build_import_sql(
            manifest,
            bronze_plan(),
            r"D:\BCP\exec\bloco.bcp",
            r"D:\BCP\exec\bloco.xml",
            "dbo",
        )
        begin = sql.index("BEGIN TRANSACTION")
        duplicate_guard = sql.index("IF EXISTS(SELECT 1 FROM [dbo].[execucao_lote]")
        data_insert = sql.index("INSERT INTO [s344].[bd_origem_tabela_origem_01]")
        row_count = sql.index("SET @inserted=ROWCOUNT_BIG()")
        block_control = sql.index("INSERT [dbo].[execucao_lote]")
        checkpoint = sql.index("UPDATE [dbo].[execucao_tabela]")
        commit = sql.rindex("COMMIT;")
        self.assertLess(begin, duplicate_guard)
        self.assertLess(duplicate_guard, data_insert)
        self.assertLess(data_insert, row_count)
        self.assertLess(row_count, block_control)
        self.assertLess(block_control, checkpoint)
        self.assertIn("last_exported_block=@seq,last_imported_block=@seq", sql)
        self.assertLess(checkpoint, commit)
        self.assertIn("WITH(UPDLOCK,HOLDLOCK)", sql)
        self.assertIn("sys.sp_getapplock", sql)
        self.assertIn("IF @inserted<>@expected", sql)
        self.assertIn("IF XACT_STATE()<>0 ROLLBACK", sql)

    def test_gap_and_overlap_are_rejected_by_exact_checkpoint_match(self):
        manifest = complete_manifest()
        manifest.lower_bound = ["5"]
        sql = build_import_sql(
            manifest, bronze_plan(), r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertIn("import_cursor_json=@lower", sql)
        self.assertIn("import_cursor_json IS NULL AND @lower IS NULL", sql)
        self.assertIn("THROW 51106", sql)
        self.assertIn("gap/overlap bloqueado", sql)

    def test_upper_bound_is_type_aware_and_cannot_pass_final_ceiling(self):
        manifest = complete_manifest()
        manifest.lower_bound = ["5"]
        manifest.upper_bound = ["10"]
        manifest.final_limit = ["100"]
        sql = build_import_sql(
            manifest, bronze_plan(), r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertIn("OPENJSON(@upper)", sql)
        self.assertIn("OPENJSON(@ceiling)", sql)
        self.assertIn("TRY_CONVERT(int", sql)
        self.assertIn("THROW 51108", sql)
        self.assertIn("ultrapassa o teto final", sql)
        self.assertIn("THROW 51107", sql)

    def test_sql_control_binding_includes_the_physical_target(self):
        sql = build_import_sql(
            complete_manifest(), bronze_plan(), r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertIn("destination_schema=N's344'", sql)
        self.assertIn("destination_table=N'bd_origem_tabela_origem_01'", sql)
        self.assertIn("DATA_TRANSFER_TARGET:", sql)

    def test_bronze_sequence_and_zero_event_metadata_are_generated_in_insert(self):
        columns = [source_column("codigo")]
        layout = resolve_profile(
            "bronze",
            columns,
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="bd_origem_tabela_origem_01",
            destination_schema="s344",
        )
        plan = build_import_plan(layout)
        expressions_by_column = dict(
            zip(plan["insert_columns"], plan["select_expressions"], strict=True)
        )
        self.assertEqual(
            expressions_by_column["bi_lsn_evento"],
            "CONVERT(BINARY(10),0)",
        )
        self.assertEqual(
            expressions_by_column["bi_sequencia_evento"],
            "CONVERT(BINARY(10),0)",
        )
        sql = build_import_sql(
            complete_manifest(), plan, r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertIn("NEXT VALUE FOR [s344].[seq_bd_origem_tabela_origem_01]", sql)
        self.assertEqual(
            sql.count("CONVERT(BINARY(10),0)"),
            2,
        )
        self.assertNotIn("CONVERT(BINARY(10),NEXT VALUE FOR", sql)
        self.assertNotIn("UPDATE [s344].[bd_origem_tabela_origem_01]", sql)
        self.assertNotIn("IDENTITY_INSERT", sql)
        self.assertNotIn("MAX([id_", sql)
        self.assertNotIn("[de_operacao]", sql.split("FROM OPENROWSET", 1)[0])
        self.assertIn(BRASILIA_TIMESTAMP_SQL, plan["select_expressions"])

    def test_landing_omits_identity_computed_and_default_columns(self):
        columns = [source_column("codigo")]
        layout = resolve_profile(
            "landing",
            columns,
            source_database="BD_ORIGEM",
            source_table="TABELA_ORIGEM_01",
            destination_table="bd_origem_tabela_origem_01",
            destination_schema="s344",
        )
        plan = build_import_plan(
            layout,
            metadata_mapping={
                "aud_ccid": {"constant": 0, "sql_type": "bigint"},
                "aud_cntrrn": {"constant": 0, "sql_type": "int"},
                "aud_enttyp": {"constant": "PT", "sql_type": "varchar(30)"},
            },
        )
        forbidden = {
            "id_tabela_origem_01", "bi_lsn_evento", "bi_sequencia_evento",
            "cd_operacao", "dh_carga",
        }
        self.assertTrue(forbidden.isdisjoint(plan["insert_columns"]))
        self.assertTrue({"codigo", "aud_ccid", "aud_cntrrn", "aud_enttyp"}.issubset(plan["insert_columns"]))
        sql = build_import_sql(
            complete_manifest(), plan, r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertNotIn("IDENTITY_INSERT", sql)
        for name in forbidden:
            self.assertNotIn(f"[{name}]", sql.split("SELECT", 1)[0])

    def test_empty_range_advances_sql_control_without_bulk_read(self):
        manifest = complete_manifest(empty=True)
        sql = build_import_sql(manifest, bronze_plan(), None, r"D:\f.xml", "dbo")
        self.assertNotIn("OPENROWSET", sql)
        self.assertNotIn("INSERT INTO [s344]", sql)
        self.assertIn("SET @inserted=CONVERT(bigint,0)", sql)
        self.assertIn("INSERT [dbo].[execucao_lote]", sql)
        self.assertIn("import_cursor_json=@upper", sql)

    def test_direct_keyless_import_uses_empty_bookmarks_and_bcp_count_guard(self):
        manifest = complete_manifest(rows=7)
        manifest.watermark = []
        manifest.lower_bound = None
        manifest.upper_bound = []
        manifest.final_limit = []
        manifest.transfer_mode = "DIRECT_KEYLESS"
        manifest.source_rows_at_capture = 7
        manifest.validate()
        sql = build_import_sql(
            manifest, bronze_plan(), r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertIn("Contrato da carga direta sem chave invalido", sql)
        self.assertNotIn("@expected<>CONVERT(bigint,7)", sql)
        self.assertIn("IF @inserted<>@expected", sql)
        self.assertNotIn("TRY_CONVERT(int", sql)
        parameters = import_parameters(manifest)
        self.assertIsNone(parameters[5])
        self.assertEqual(parameters[6], "[]")
        self.assertEqual(parameters[7], "[]")

    def test_bigint_values_are_not_truncated_in_parameters_or_control(self):
        huge_rows = 2**31 + 987_654
        huge_bytes = 2**31 + 456_789
        manifest = complete_manifest(rows=huge_rows, file_bytes=huge_bytes)
        parameters = import_parameters(manifest)
        self.assertEqual(parameters[8], huge_rows)
        self.assertEqual(parameters[9], huge_bytes)
        ddl = control_ddl("dbo")
        self.assertIn("manifest_rows bigint", ddl)
        self.assertIn("imported_rows bigint", ddl)
        self.assertIn("file_bytes bigint", ddl)
        self.assertIn("last_exported_block bigint", ddl)
        self.assertIn("last_imported_block bigint", ddl)

    def test_control_ddl_is_side_by_side_idempotent_and_non_destructive(self):
        ddl = control_ddl("dbo")
        self.assertIn("execucao", ddl)
        self.assertIn("execucao_tabela", ddl)
        self.assertIn("execucao_lote", ddl)
        self.assertIn("OBJECT_ID", ddl)
        self.assertIn("BEGIN TRANSACTION", ddl)
        self.assertIn("ROLLBACK", ddl)
        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("(destination_schema,destination_table)", ddl)
        self.assertIn("IGNORE_DUP_KEY=OFF", ddl)
        self.assertNotIn("DROP ", ddl.upper())
        self.assertNotIn("TRUNCATE ", ddl.upper())
        self.assertIn("@control_object_count<>0", ddl)
        self.assertIn("@expected_columns", ddl)
        self.assertIn("@expected_indexes", ddl)
        self.assertIn("@expected_defaults", ddl)
        self.assertIn("@expected_foreign_key_columns", ddl)
        self.assertIn("is_not_trusted<>0", ddl)
        self.assertIn("quantidade de colunas divergente", ddl)
        self.assertIn("trigger nao versionado", ddl)

    def test_default_control_contract_rejects_silent_split_from_legacy_objects(self):
        ddl = control_ddl("dbo")
        for object_name in (
            "versao_esquema",
            "execucao",
            "execucao_tabela",
            "execucao_lote",
        ):
            self.assertIn(f"[dbo].[{object_name}]", ddl)
            self.assertIn(f"[controle_transferencia].[{object_name}]", ddl)
        self.assertIn("[bcp_control_v2].[bcp_schema_version]", ddl)
        self.assertIn("Controle SQL legado detectado fora de dbo", ddl)
        self.assertIn("THROW 51106", ddl)
        self.assertNotIn("DROP ", ddl.upper())

    def test_destination_importer_defaults_to_canonical_dbo_control(self):
        importer = DestinationImporter(object())
        self.assertEqual(importer.control_schema, "dbo")
        with patch("bcp_engine.importer.execute") as execute_sql:
            importer.ensure_control()
        ddl = execute_sql.call_args.args[1]
        self.assertIn("CREATE TABLE [dbo].[versao_esquema]", ddl)
        self.assertIn("CREATE TABLE [dbo].[execucao]", ddl)
        self.assertIn("CREATE TABLE [dbo].[execucao_tabela]", ddl)
        self.assertIn("CREATE TABLE [dbo].[execucao_lote]", ddl)

    def test_destination_importer_can_validate_existing_control_without_ddl(self):
        importer = DestinationImporter(object())
        with patch("bcp_engine.importer.execute") as execute_sql:
            importer.validate_control()
        sql = execute_sql.call_args.args[1]
        self.assertIn("Controle SQL incompativel", sql)
        self.assertIn("[dbo].[versao_esquema]", sql)
        self.assertNotIn("CREATE TABLE", sql.upper())
        self.assertNotIn("CREATE SCHEMA", sql.upper())
        self.assertNotIn("ALTER TABLE", sql.upper())
        self.assertNotIn("DROP ", sql.upper())

    def test_control_schema_is_fixed_to_dbo_at_every_importer_boundary(self):
        invalid_schemas = ("controle_transferencia", "DBO", "outro_schema")
        for schema in invalid_schemas:
            with self.subTest(schema=schema, boundary="object_names"):
                with self.assertRaisesRegex(ValueError, "deve ser dbo"):
                    control_object_names(schema)
            with self.subTest(schema=schema, boundary="ddl"):
                with self.assertRaisesRegex(ValueError, "deve ser dbo"):
                    control_ddl(schema)
            with self.subTest(schema=schema, boundary="import_sql"):
                with self.assertRaisesRegex(ValueError, "deve ser dbo"):
                    build_import_sql(
                        complete_manifest(),
                        bronze_plan(),
                        r"D:\d.bcp",
                        r"D:\f.xml",
                        schema,
                    )
            with self.subTest(schema=schema, boundary="destination_importer"):
                with self.assertRaisesRegex(ValueError, "deve ser dbo"):
                    DestinationImporter(object(), schema)

    def test_authentication_or_password_text_never_enters_import_sql(self):
        manifest = complete_manifest()
        marker = "SENHA_ULTRASSECRETA_ç;{}"
        manifest.authentication = {"source": marker, "destination": marker}
        sql = build_import_sql(
            manifest, bronze_plan(), r"D:\d.bcp", r"D:\f.xml", "dbo"
        )
        self.assertNotIn(marker, sql)
        self.assertNotIn(marker, repr(import_parameters(manifest)))


class PathMappingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.file = self.root / "exec" / "table" / "block.bcp"
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"x")

    def tearDown(self):
        self.temporary.cleanup()

    def test_maps_only_relative_suffix_to_sql_root(self):
        mapped = sql_artifact_path(self.root, r"\\server\share\BCP", self.file)
        self.assertEqual(
            PureWindowsPath(mapped),
            PureWindowsPath(r"\\server\share\BCP\exec\table\block.bcp"),
        )

    def test_local_artifact_outside_executor_root_is_rejected(self):
        outside = self.root.parent / "outside.bcp"
        with self.assertRaisesRegex(ValueError, "não pertence"):
            sql_artifact_path(self.root, r"D:\BCP", outside)

    def test_sql_root_must_be_absolute_and_cannot_contain_parent_traversal(self):
        for sql_root in (r"relative\root", r"D:\seguro\..\escape"):
            with self.subTest(sql_root=sql_root):
                with self.assertRaisesRegex(ValueError, "raiz|absoluto|inválido"):
                    sql_artifact_path(self.root, sql_root, self.file)


class DestinationImporterTests(unittest.TestCase):
    def test_sql_control_rejects_states_outside_the_english_enum_contract(self):
        importer = DestinationImporter(object())
        with patch("bcp_engine.importer.execute") as execute_sql:
            with self.assertRaisesRegex(ValueError, "state da tabela"):
                importer.finish_table(
                    str(uuid.uuid4()),
                    "d" * 64,
                    "e" * 64,
                    state="CONCLUIDA",
                    index_state="COMPLETED",
                )
            with self.assertRaisesRegex(ValueError, "index_state"):
                importer.finish_table(
                    str(uuid.uuid4()),
                    "d" * 64,
                    "e" * 64,
                    state="COMPLETED",
                    index_state="CONCLUIDO",
                )
            with self.assertRaisesRegex(ValueError, "state da execução"):
                importer.finish_execution(
                    str(uuid.uuid4()), "d" * 64, state="CONCLUIDA"
                )
            execute_sql.assert_not_called()
    def test_final_cardinality_compares_sql_control_with_physical_target(self):
        importer = DestinationImporter(object())
        with patch("bcp_engine.importer.scalar", side_effect=[18, 18]) as query:
            observed = importer.verify_destination_cardinality(
                execution_id=str(uuid.uuid4()),
                dataset_id="d" * 64,
                table_id="e" * 64,
                target_schema="s344",
                target_table="bd_origem_tabela_origem_01",
            )
        self.assertEqual(observed, 18)
        self.assertIn("SUM(imported_rows)", query.call_args_list[0].args[1])
        self.assertIn(
            "COUNT_BIG(*) FROM [s344].[bd_origem_tabela_origem_01]",
            query.call_args_list[1].args[1],
        )

        with patch("bcp_engine.importer.scalar", side_effect=[18, 19]):
            with self.assertRaisesRegex(
                RuntimeError, "DESTINATION_ROW_COUNT_MISMATCH"
            ):
                importer.verify_destination_cardinality(
                    execution_id=str(uuid.uuid4()),
                    dataset_id="d" * 64,
                    table_id="e" * 64,
                    target_schema="s344",
                    target_table="bd_origem_tabela_origem_01",
                )
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.execution_id = str(uuid.uuid4())
        self.block_id = str(uuid.uuid4())
        self.store = ArtifactStore(self.root, self.execution_id)
        self.paths = self.store.block_paths("c" * 64, 1, self.block_id)
        self.paths.partial.write_bytes(b"dados")
        self.paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
        draft = complete_manifest(execution_id=self.execution_id, block_id=self.block_id)
        draft.complete = False
        draft.data_file = None
        draft.data_sha256 = None
        draft.format_file = "pending"
        draft.format_sha256 = "pending"
        draft.file_bytes = 0
        self.manifest = self.store.publish(self.paths, draft)

    def tearDown(self):
        self.temporary.cleanup()

    def test_hash_is_checked_before_any_sql_call(self):
        self.paths.data.write_bytes(b"adulterado")
        importer = DestinationImporter(object())
        with patch("bcp_engine.importer.scalar") as scalar_mock, patch(
            "bcp_engine.importer.execute"
        ) as execute_mock:
            with self.assertRaisesRegex(RuntimeError, "modificado"):
                importer.import_manifest(
                    self.store,
                    self.paths.manifest,
                    bronze_plan(),
                    self.root,
                    r"D:\BCP",
                )
        scalar_mock.assert_not_called()
        execute_mock.assert_not_called()

    def test_already_confirmed_block_is_not_reinserted(self):
        importer = DestinationImporter(object())
        with patch.object(importer, "block_is_confirmed", return_value=True), patch(
            "bcp_engine.importer.execute"
        ) as execute_mock:
            returned = importer.import_manifest(
                self.store, self.paths.manifest, bronze_plan(), self.root, r"D:\BCP"
            )
        self.assertEqual(returned.block_id, self.manifest.block_id)
        execute_mock.assert_not_called()

    def test_lost_commit_response_reconciles_control_instead_of_reinserting(self):
        importer = DestinationImporter(object())
        with patch.object(importer, "block_is_confirmed", side_effect=[False, True]), patch(
            "bcp_engine.importer.execute", side_effect=RuntimeError("conexão perdida após COMMIT")
        ) as execute_mock:
            returned = importer.import_manifest(
                self.store, self.paths.manifest, bronze_plan(), self.root, r"D:\BCP"
            )
        self.assertEqual(returned.block_id, self.manifest.block_id)
        execute_mock.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "share lease obrigatório é específico do Windows")
    def test_artifact_cannot_be_reopened_for_write_during_sql_bulk_read(self):
        importer = DestinationImporter(object())
        original = self.paths.data.read_bytes()

        def attempt_mutation(*_args, **_kwargs):
            with self.assertRaises(OSError):
                self.paths.data.write_bytes(b"adulterado durante bulk")
            self.assertEqual(self.paths.data.read_bytes(), original)

        with (
            patch.object(importer, "block_is_confirmed", side_effect=[False, True]),
            patch("bcp_engine.importer.execute", side_effect=attempt_mutation),
        ):
            returned = importer.import_manifest(
                self.store,
                self.paths.manifest,
                bronze_plan(),
                self.root,
                r"D:\BCP",
            )

        self.assertEqual(returned.block_id, self.manifest.block_id)
        # Closing the lease restores the owner's normal ability to manage the
        # artifact; retention/deletion policy remains operational.
        self.paths.data.write_bytes(original)

    def test_data_hash_is_streamed_without_materializing_bcp_bytes(self):
        importer = DestinationImporter(object())
        original_read_bytes = _ArtifactReadLease.read_bytes
        materialized: list[Path] = []

        def observe_read_bytes(lease, path, **kwargs):
            materialized.append(Path(path))
            return original_read_bytes(lease, path, **kwargs)

        with (
            patch.object(_ArtifactReadLease, "read_bytes", new=observe_read_bytes),
            patch.object(importer, "block_is_confirmed", side_effect=[False, True]),
            patch("bcp_engine.importer.execute"),
        ):
            importer.import_manifest(
                self.store,
                self.paths.manifest,
                bronze_plan(),
                self.root,
                r"D:\BCP",
            )

        self.assertIn(self.paths.manifest, materialized)
        self.assertNotIn(self.paths.data, materialized)

    def test_verified_bundle_exposes_only_canonical_leased_paths(self):
        aliased = self.paths.directory / ".." / self.paths.directory.name / self.paths.manifest.name
        with self.store.hold_verified_artifacts(aliased) as verified:
            self.assertEqual(verified.manifest_path, self.paths.manifest.resolve())
            self.assertEqual(verified.format_path, self.paths.format.resolve())
            self.assertEqual(verified.data_path, self.paths.data.resolve())

    def test_json_artifact_read_is_bounded(self):
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * (MAX_JSON_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(RuntimeError, "limite de leitura segura"):
            read_bounded_json(oversized)

    @unittest.skipUnless(os.name == "nt", "DACL de artefatos é específica do Windows")
    def test_preexisting_everyone_ace_is_removed_from_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            execution_id = str(uuid.uuid4())
            execution_root = root / execution_id
            execution_root.mkdir()
            child = execution_root / "child.bin"
            child.write_bytes(b"content")
            for path, grant in (
                (execution_root, "*S-1-1-0:(OI)(CI)F"),
                (child, "*S-1-1-0:F"),
            ):
                subprocess.run(
                    ["icacls.exe", str(path), "/grant", grant, "/Q"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            ArtifactStore(root, execution_id)
            environment = os.environ.copy()
            environment["BCP_TEST_PATHS"] = f"{execution_root}|{child}"
            script = (
                "$env:BCP_TEST_PATHS.Split('|') | ForEach-Object { "
                "(Get-Acl -LiteralPath $_).Access | ForEach-Object { "
                "$_.IdentityReference.Translate("
                "[System.Security.Principal.SecurityIdentifier]).Value } }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotIn("S-1-1-0", result.stdout.splitlines())

    def test_confirmed_lookup_compares_hashes_and_bounds_not_only_row_count(self):
        importer = DestinationImporter(object())
        calls: list[tuple[str, tuple]] = []

        def fake_scalar(_connection, sql, params=()):
            calls.append((sql, tuple(params)))
            return 1

        with patch("bcp_engine.importer.scalar", side_effect=fake_scalar):
            self.assertTrue(importer.block_is_confirmed(self.manifest))
        lookup_sql, lookup_params = calls[-1]
        self.assertIn("data_sha256", lookup_sql)
        self.assertIn("format_sha256", lookup_sql)
        self.assertIn("lower_bound_json", lookup_sql)
        self.assertIn("upper_bound_json", lookup_sql)
        self.assertIn(self.manifest.data_sha256, lookup_params)
        self.assertIn(self.manifest.format_sha256, lookup_params)

    def test_compatible_binding_probe_is_read_only_and_checks_exact_contract(self):
        importer = DestinationImporter(object())
        calls: list[tuple[str, tuple]] = []

        def fake_scalar(_connection, sql, params=()):
            calls.append((sql, tuple(params)))
            return 1

        final_limit_json = '["100"]'
        with patch("bcp_engine.importer.scalar", side_effect=fake_scalar):
            exists = importer.compatible_table_binding_exists(
                execution_id=self.execution_id,
                dataset_id="d" * 64,
                structural_hash="d" * 64,
                area="bronze",
                table_id="e" * 64,
                target_schema="s344",
                target_table="bd_origem_tabela_origem_01",
                layout_hash="2" * 64,
                projection_hash="1" * 64,
                final_limit_json=final_limit_json,
            )

        self.assertTrue(exists)
        self.assertEqual(len(calls), 3)
        sql, params = calls[-1]
        self.assertTrue(sql.lstrip().upper().startswith("SELECT COUNT_BIG"))
        self.assertNotIn("INSERT ", sql.upper())
        self.assertNotIn("UPDATE ", sql.upper())
        self.assertNotIn("DELETE ", sql.upper())
        self.assertEqual(
            params,
            (
                self.execution_id,
                "d" * 64,
                "d" * 64,
                "bronze",
                "e" * 64,
                "s344",
                "bd_origem_tabela_origem_01",
                "2" * 64,
                "1" * 64,
                final_limit_json,
            ),
        )

    def test_registration_serializes_and_persists_physical_target_ownership(self):
        importer = DestinationImporter(object())
        arguments = {
            "execution_id": self.execution_id,
            "dataset_id": "d" * 64,
            "structural_hash": "d" * 64,
            "area": "bronze",
            "table_id": "t" * 64,
            "target_schema": "s344",
            "target_table": "bd_origem_tabela_origem_01",
            "layout_hash": "2" * 64,
            "projection_hash": "1" * 64,
            "final_limit_json": '["100"]',
        }
        with patch("bcp_engine.importer.execute") as execute_mock:
            importer.register_execution_table(**arguments)
        _connection, sql, params = execute_mock.call_args.args
        self.assertIn("DATA_TRANSFER_TARGET:", sql)
        self.assertIn("WHERE destination_schema=? AND destination_table=?", sql)
        self.assertIn("AND NOT (execution_id=? AND dataset_id=? AND table_id=?)", sql)
        self.assertIn("THROW 51114", sql)
        self.assertEqual(params[:5], (
            "s344", "bd_origem_tabela_origem_01", self.execution_id, "d" * 64, "t" * 64,
        ))
        self.assertEqual(sql.count("?"), len(params))

    def test_same_physical_target_uses_same_lock_across_datasets_and_allows_exact_resume(self):
        importer = DestinationImporter(object())

        def register(execution_id: str, dataset_id: str, table_id: str) -> str:
            with patch("bcp_engine.importer.execute") as execute_mock:
                importer.register_execution_table(
                    execution_id=execution_id,
                    dataset_id=dataset_id,
                    structural_hash=dataset_id,
                    area="bronze",
                    table_id=table_id,
                    target_schema="s344",
                    target_table="bd_origem_tabela_origem_01",
                    layout_hash="2" * 64,
                    projection_hash="1" * 64,
                    final_limit_json='["100"]',
                )
            return execute_mock.call_args.args[1]

        first_sql = register(str(uuid.uuid4()), "a" * 64, "1" * 64)
        competing_sql = register(str(uuid.uuid4()), "b" * 64, "2" * 64)
        first_resource = first_sql.split("@Resource=", 1)[1].split(",@LockMode", 1)[0]
        competing_resource = competing_sql.split("@Resource=", 1)[1].split(",@LockMode", 1)[0]
        self.assertEqual(first_resource, competing_resource)
        self.assertIn(
            "IF NOT EXISTS(SELECT 1 FROM [dbo].[execucao_tabela] WITH(UPDLOCK,HOLDLOCK)",
            first_sql,
        )
        self.assertIn("execution_id=? AND dataset_id=? AND table_id=?", first_sql)


class ImportWithoutSourceTests(unittest.TestCase):
    class _Destination:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _Importer:
        def __init__(self):
            self.imported = []
            self.ensure_control_calls = 0
            self.validate_control_calls = 0
            self.registered = []
            self.confirmed = set()
            self.bindings = set()
            self.finished_tables = []
            self.finished_executions = []

        def ensure_control(self):
            self.ensure_control_calls += 1

        def validate_control(self):
            self.validate_control_calls += 1

        def register_execution_table(self, **kwargs):
            self.registered.append(kwargs)
            self.bindings.add(
                (kwargs["execution_id"], kwargs["dataset_id"], kwargs["table_id"])
            )

        def compatible_table_binding_exists(self, **kwargs):
            return (
                kwargs["execution_id"],
                kwargs["dataset_id"],
                kwargs["table_id"],
            ) in self.bindings

        def block_is_confirmed(self, manifest):
            return manifest.block_id in self.confirmed

        def import_manifest(self, store, path, _plan, _executor_root, _sql_root):
            manifest = store.load_and_verify(path)
            if manifest.block_id not in self.confirmed:
                self.imported.append(manifest.block_id)
                self.confirmed.add(manifest.block_id)
            return manifest

        def table_is_complete(self, _manifest):
            return True

        def verify_destination_cardinality(self, **_kwargs):
            return 7

        def finish_table(self, *args, **kwargs):
            self.finished_tables.append((args, kwargs))

        def finish_execution(self, *args, **kwargs):
            self.finished_executions.append((args, kwargs))

    def _published_manifest(
        self,
        root: Path,
        *,
        destination_present: bool,
        table_index: int = 0,
        execution_id: str | None = None,
        block_number: int = 1,
        lower_bound: list[str] | None = None,
        upper_bound: list[str] | None = None,
        final_limit: list[str] | None = None,
        data: bytes = b"dados",
    ):
        execution_id = execution_id or str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        configured = effective_tables(
            read_config(ROOT / "examples" / "config.full.json")
        )[table_index]
        source_columns = [source_column("codigo")]
        layout = resolve_profile(
            "bronze",
            source_columns,
            source_database=configured["source"]["database"],
            source_table=configured["source"]["table"],
            destination_table=configured["destination"]["table"],
            destination_schema=configured["destination"]["schema"],
        )
        physical = layout_contract(layout)
        projection = projection_contract(layout, source_columns)
        destination = (
            {
                "area": "bronze", "instance": r"SQL\DESTINO", "database": "DBRO684",
                "schema": configured["destination"]["schema"],
                "table": configured["destination"]["table"],
            }
            if destination_present else None
        )
        manifest = complete_manifest(
            execution_id=execution_id,
            block_id=block_id,
            destination=destination,
            projection=projection["columns"],
            projection_hash=projection["projection_hash"],
            layout_hash=physical["layout_hash"],
        )
        manifest.source = dict(configured["source"])
        manifest.table_id = _table_id(configured)
        manifest.block_number = block_number
        manifest.lower_bound = lower_bound
        manifest.upper_bound = upper_bound or ["10"]
        manifest.final_limit = final_limit or ["100"]
        if not destination_present:
            manifest.destination = None
            manifest.authentication.pop("destination", None)
        store = ArtifactStore(root, execution_id)
        paths = store.block_paths(manifest.table_id, block_number, block_id)
        paths.partial.write_bytes(data)
        paths.format.write_text("<BCPFORMAT />", encoding="utf-8")
        manifest.complete = False
        manifest.data_file = None
        manifest.data_sha256 = None
        manifest.format_file = "pending"
        manifest.format_sha256 = "pending"
        manifest.file_bytes = 0
        store.publish(paths, manifest)
        return paths.manifest

    def _engine(self, root: Path):
        config = read_config(ROOT / "examples" / "config.full.json")
        config["executor_directory"] = str(root)
        config["local_control_directory"] = str(root / "control")
        config["destination_sql_directory"] = r"D:\BCP"
        engine = BcpEngine(config, config_path=ROOT / "config.runtime.json")
        destination = self._Destination()
        engine._connect_source = Mock(side_effect=AssertionError("import não pode abrir origem"))
        engine._connect_destination = Mock(return_value=(destination, object()))
        engine._destination_table_exists = Mock(return_value=False)
        engine._provision_initial = Mock()
        engine._finish_indexes = Mock()
        engine._sql_path_probe = Mock()
        return engine, destination

    def test_manifest_import_does_not_open_source_and_finishes_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            engine, destination = self._engine(root)
            fake_importer = self._Importer()
            with patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer):
                report = engine.import_manifests(path)
            engine._connect_source.assert_not_called()
            self.assertEqual(report.exit_code(), 0)
            self.assertEqual(len(fake_importer.imported), 1)
            engine._finish_indexes.assert_called_once()
            self.assertTrue(destination.closed)

    def test_manifest_data_only_validates_control_without_ensuring_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            engine, destination = self._engine(root)
            engine.config["bronze_destination"]["role"] = "data_only"
            fake_importer = self._Importer()
            with patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer):
                report = engine.import_manifests(path)

            self.assertEqual(report.exit_code(), 0)
            self.assertEqual(fake_importer.ensure_control_calls, 0)
            self.assertEqual(fake_importer.validate_control_calls, 1)
            self.assertTrue(destination.closed)

    def test_manifest_import_materializes_fresh_local_control_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            expected = json.loads(path.read_text(encoding="utf-8"))
            engine, _destination = self._engine(root)
            engine.config["delete_confirmed_files"] = False
            fake_importer = self._Importer()

            with patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer):
                first = engine.import_manifests(path)
                second = engine.import_manifests(path)

            self.assertEqual(first.exit_code(), 0)
            self.assertEqual(second.exit_code(), 0)
            engine._connect_source.assert_not_called()

            with LocalState(root / "control", create=False) as state:
                table = state.table(expected["execution_id"], expected["table_id"])
                blocks = state.blocks(expected["execution_id"], expected["table_id"])

            self.assertIsNotNone(table)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["block_id"], expected["block_id"])
            self.assertEqual(blocks[0]["status"], "IMPORTED")
            self.assertEqual(blocks[0]["rows_exported"], expected["rows_exported"])
            self.assertEqual(blocks[0]["rows_imported"], expected["rows_exported"])
            self.assertEqual(table["rows_exported"], expected["rows_exported"])
            self.assertEqual(table["rows_imported"], expected["rows_exported"])
            self.assertEqual(table["export_cursor_json"], '["10"]')
            self.assertEqual(table["import_cursor_json"], '["10"]')

    def test_export_only_manifest_without_destination_uses_configured_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=False)
            engine, destination = self._engine(root)
            fake_importer = self._Importer()
            with patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer):
                report = engine.import_manifests(path)
            engine._connect_source.assert_not_called()
            self.assertEqual(report.exit_code(), 0)
            self.assertEqual(report.tables[0].destination_table, "bd_origem_tabela_origem_01")
            self.assertTrue(destination.closed)

    def test_index_failure_is_reported_pending_and_connection_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            engine, destination = self._engine(root)
            engine._finish_indexes.side_effect = RuntimeError("índice indisponível")
            with patch(
                "bcp_engine.engine.DestinationImporter", return_value=self._Importer()
            ):
                report = engine.import_manifests(path)
            self.assertEqual(report.exit_code(), 2)
            self.assertEqual(
                report.tables[0].status,
                "DATA_COMPLETE_INDEXES_PENDING",
            )
            self.assertEqual(report.tables[0].index_state, "PENDING")
            self.assertIn("índice indisponível", report.tables[0].reason)
            self.assertTrue(destination.closed)

    def test_manifest_import_skips_before_sql_control_when_space_is_insufficient(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            expected = json.loads(path.read_text(encoding="utf-8"))
            engine, _destination = self._engine(root)
            fake_importer = self._Importer()
            events = []
            engine.event = lambda name, payload: events.append((name, payload))

            def insufficient(_connection, required_bytes):
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=required_bytes - 1,
                    sufficient=False,
                    volumes=({"volume_mount_point": "D:\\", "available_bytes": 1},),
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch(
                    "bcp_engine.engine.inspect_destination_space",
                    side_effect=insufficient,
                ) as inspect_mock,
            ):
                report = engine.import_manifests(path)

            expected_required = 7  # ceil(5 bytes publicados * fator 1,25)
            self.assertEqual(inspect_mock.call_args.args[1], expected_required)
            self.assertEqual(
                report.tables[0].status,
                "SKIPPED_DESTINATION_INSUFFICIENT_SPACE",
            )
            self.assertIn(
                "Carga no destino nao iniciada; nenhuma linha desta tabela foi importada.",
                report.tables[0].warnings,
            )
            self.assertEqual(fake_importer.ensure_control_calls, 0)
            self.assertEqual(fake_importer.registered, [])
            self.assertEqual(fake_importer.imported, [])
            self.assertEqual(fake_importer.finished_tables, [])
            self.assertEqual(fake_importer.finished_executions, [])
            engine._provision_initial.assert_not_called()
            engine._sql_path_probe.assert_not_called()
            self.assertEqual(events[0][0], "destination_space")
            self.assertFalse(events[0][1]["sufficient"])

            with LocalState(root / "control", create=False) as state:
                local = state.table(expected["execution_id"], expected["table_id"])
            self.assertIsNotNone(local)
            self.assertEqual(
                local["status"], "SKIPPED_DESTINATION_INSUFFICIENT_SPACE"
            )

    def test_manifest_import_warns_and_proceeds_when_space_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            engine, _destination = self._engine(root)
            fake_importer = self._Importer()
            events = []
            engine.event = lambda name, payload: events.append((name, payload))

            def unavailable(_connection, required_bytes):
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=None,
                    sufficient=None,
                    error="sem permissao para medir o volume",
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch(
                    "bcp_engine.engine.inspect_destination_space",
                    side_effect=unavailable,
                ),
            ):
                report = engine.import_manifests(path)

            self.assertEqual(report.tables[0].status, "COMPLETED")
            self.assertTrue(
                any(
                    warning.startswith("DESTINATION_SPACE_UNAVAILABLE:")
                    for warning in report.tables[0].warnings
                )
            )
            self.assertEqual(fake_importer.ensure_control_calls, 1)
            self.assertEqual(len(fake_importer.registered), 1)
            self.assertEqual(len(fake_importer.imported), 1)
            self.assertEqual(events[0][0], "destination_space")
            self.assertIsNone(events[0][1]["sufficient"])

    def test_second_import_counts_no_space_for_blocks_already_confirmed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            expected = json.loads(path.read_text(encoding="utf-8"))
            engine, _destination = self._engine(root)
            engine.config["delete_confirmed_files"] = False
            fake_importer = self._Importer()
            required_values = []

            def assess(_connection, required_bytes):
                required_values.append(required_bytes)
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=required_bytes,
                    sufficient=required_bytes == 0 or len(required_values) == 1,
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch("bcp_engine.engine.inspect_destination_space", side_effect=assess),
            ):
                first = engine.import_manifests(path)
                second = engine.import_manifests(path)

            self.assertEqual(first.tables[0].status, "COMPLETED")
            self.assertEqual(second.tables[0].status, "COMPLETED")
            self.assertEqual(required_values, [7, 0])
            self.assertEqual(fake_importer.imported, [expected["block_id"]])
            with LocalState(root / "control", create=False) as state:
                local = state.table(expected["execution_id"], expected["table_id"])
            self.assertEqual(local["status"], "COMPLETED")

    def test_manifest_import_counts_only_unconfirmed_block_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            execution_id = str(uuid.uuid4())
            first_path = self._published_manifest(
                root,
                destination_present=True,
                execution_id=execution_id,
                block_number=1,
                upper_bound=["10"],
                final_limit=["20"],
                data=b"already-confirmed",
            )
            second_path = self._published_manifest(
                root,
                destination_present=True,
                execution_id=execution_id,
                block_number=2,
                lower_bound=["10"],
                upper_bound=["20"],
                final_limit=["20"],
                data=b"pending-block",
            )
            first_manifest = json.loads(first_path.read_text(encoding="utf-8"))
            second_manifest = json.loads(second_path.read_text(encoding="utf-8"))
            engine, _destination = self._engine(root)
            fake_importer = self._Importer()
            fake_importer.confirmed.add(first_manifest["block_id"])
            fake_importer.bindings.add(
                (
                    execution_id,
                    first_manifest["dataset_id"],
                    first_manifest["table_id"],
                )
            )
            required_values = []

            def sufficient(_connection, required_bytes):
                required_values.append(required_bytes)
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=required_bytes,
                    sufficient=True,
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch(
                    "bcp_engine.engine.inspect_destination_space",
                    side_effect=sufficient,
                ),
            ):
                report = engine.import_manifests(root / execution_id)

            expected_required = 17  # ceil(13 bytes pendentes * 1,25)
            self.assertEqual(required_values, [expected_required])
            self.assertEqual(report.tables[0].status, "COMPLETED")
            self.assertEqual(fake_importer.imported, [second_manifest["block_id"]])

    def test_space_skip_terminalizes_compatible_preexisting_sql_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            expected = json.loads(path.read_text(encoding="utf-8"))
            engine, _destination = self._engine(root)
            fake_importer = self._Importer()
            fake_importer.bindings.add(
                (
                    expected["execution_id"],
                    expected["dataset_id"],
                    expected["table_id"],
                )
            )

            def insufficient(_connection, required_bytes):
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=required_bytes - 1,
                    sufficient=False,
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch(
                    "bcp_engine.engine.inspect_destination_space",
                    side_effect=insufficient,
                ),
            ):
                report = engine.import_manifests(path)

            self.assertEqual(
                report.tables[0].status,
                "SKIPPED_DESTINATION_INSUFFICIENT_SPACE",
            )
            self.assertEqual(fake_importer.ensure_control_calls, 0)
            self.assertEqual(fake_importer.registered, [])
            self.assertEqual(fake_importer.imported, [])
            engine._provision_initial.assert_not_called()
            engine._sql_path_probe.assert_not_called()
            self.assertEqual(
                fake_importer.finished_tables[-1][1]["state"],
                "SKIPPED_DESTINATION_INSUFFICIENT_SPACE",
            )
            self.assertEqual(
                fake_importer.finished_executions[-1][1]["state"],
                "COMPLETED_WITH_PENDING_ITEMS",
            )

    def test_space_skip_continues_with_next_viable_table_even_when_fail_fast(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            execution_id = str(uuid.uuid4())
            self._published_manifest(
                root,
                destination_present=True,
                table_index=0,
                execution_id=execution_id,
            )
            self._published_manifest(
                root,
                destination_present=True,
                table_index=1,
                execution_id=execution_id,
            )
            engine, _destination = self._engine(root)
            engine.config["continue_after_table_error"] = False
            fake_importer = self._Importer()
            assessments = iter((False, True))

            def assess(_connection, required_bytes):
                sufficient = next(assessments)
                return DestinationSpaceAssessment(
                    required_bytes=required_bytes,
                    available_bytes=0 if not sufficient else required_bytes,
                    sufficient=sufficient,
                )

            with (
                patch("bcp_engine.engine.DestinationImporter", return_value=fake_importer),
                patch("bcp_engine.engine.inspect_destination_space", side_effect=assess),
            ):
                report = engine.import_manifests(root / execution_id)

            self.assertEqual(len(report.tables), 2)
            self.assertEqual(
                report.tables[0].status,
                "SKIPPED_DESTINATION_INSUFFICIENT_SPACE",
            )
            self.assertEqual(report.tables[1].status, "COMPLETED")
            self.assertEqual(fake_importer.ensure_control_calls, 1)
            self.assertEqual(len(fake_importer.registered), 1)
            self.assertEqual(
                fake_importer.registered[0]["target_table"],
                "bd_origem_tabela_origem_02",
            )
            self.assertEqual(len(fake_importer.imported), 1)

    def test_invalid_manifest_mapping_is_rejected_before_destination_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self._published_manifest(root, destination_present=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["destination"]["table"] = "destino_nao_configurado"
            path.write_text(json.dumps(payload), encoding="utf-8")
            engine, destination = self._engine(root)
            with patch(
                "bcp_engine.engine.DestinationImporter", return_value=self._Importer()
            ):
                with self.assertRaisesRegex(RuntimeError, "Destino do manifesto diverge"):
                    engine.import_manifests(path)
            engine._connect_source.assert_not_called()
            engine._connect_destination.assert_not_called()
            self.assertFalse(destination.closed)


if __name__ == "__main__":
    unittest.main()
