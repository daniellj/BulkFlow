from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.config import ConfigError, read_config, validate_config  # noqa: E402
from bcp_engine.gui_model import (  # noqa: E402
    ROW_COUNT_LABELS,
    automatic_destination_table,
    build_authentication,
    build_disk_projection_summary,
    build_endpoint,
    build_table,
    connection_summary_rows,
    default_gui_config,
    default_username_for_perimeter,
    format_bytes_summary,
    format_minutes_as_days,
    format_watermark,
    parse_optional_integer,
    parse_watermark,
    resolve_profile_paths_for_editor,
    split_instance_and_port,
    table_to_form,
    update_default_username_for_perimeter,
    write_config_atomic,
)


class GuiModelTests(unittest.TestCase):
    def test_disk_projection_consolidates_every_table_and_does_not_double_count_views(self):
        calls: list[Path] = []

        class Usage:
            free = 10_000

        def disk_usage(path):
            calls.append(Path(path))
            return Usage()

        config = {
            "executor_directory": "artifact-share",
            "destination_sql_directory": "artifact-share",
            "minimum_free_space_bytes": 1_000,
            "estimates": {"safety_factor": 1.25},
        }
        plans = [
            {
                "source": {"database": "BD_ORIGEM", "schema": "dbo", "table": "a"},
                "status": "PENDING",
                "estimated_rows": 10,
                "estimated_bcp_total_bytes": 2_000,
                "planning_reserve_bytes": 2_500,
                "predicted_peak_bytes": 700,
            },
            {
                "source": {"database": "BD_ORIGEM", "schema": "dbo", "table": "b"},
                "status": "PENDING",
                "estimated_rows": 20,
                "estimated_bcp_total_bytes": 4_000,
                "planning_reserve_bytes": 5_000,
                "predicted_peak_bytes": 1_500,
            },
        ]

        summary = build_disk_projection_summary(
            config, plans, disk_usage_provider=disk_usage
        )

        self.assertEqual(summary["total_raw_bytes"], 6_000)
        self.assertEqual(summary["total_protected_bytes"], 7_500)
        self.assertEqual(summary["total_margin_bytes"], 1_500)
        self.assertEqual(summary["predicted_peak_bytes"], 1_500)
        self.assertTrue(summary["same_path_view"])
        self.assertEqual(len(calls), 1)
        export = summary["directories"]["export"]
        self.assertEqual(export["balance_after_peak_bytes"], 7_500)
        self.assertEqual(export["balance_after_total_protected_bytes"], 1_500)
        self.assertEqual(export["state"], "sufficient")
        self.assertEqual([row["source"] for row in summary["tables"]], [
            "BD_ORIGEM.dbo.a",
            "BD_ORIGEM.dbo.b",
        ])

    def test_disk_projection_preserves_unknown_table_and_never_coerces_it_to_zero(self):
        config = {
            "executor_directory": ".",
            "destination_sql_directory": ".",
            "minimum_free_space_bytes": 0,
            "estimates": {"safety_factor": 1.25},
        }
        plans = [
            {
                "source": {"database": "D", "schema": "dbo", "table": "known"},
                "status": "PENDING",
                "estimated_bcp_total_bytes": 100,
                "planning_reserve_bytes": 125,
                "predicted_peak_bytes": 50,
            },
            {
                "source": {"database": "D", "schema": "dbo", "table": "unknown"},
                "status": "LAYOUT_ERROR",
                "estimated_bcp_total_bytes": None,
                "planning_reserve_bytes": None,
                "predicted_peak_bytes": None,
            },
        ]
        summary = build_disk_projection_summary(
            config, plans, disk_usage_provider=lambda _path: 1_000
        )

        self.assertEqual(summary["known_raw_bytes"], 100)
        self.assertEqual(summary["unknown_table_count"], 1)
        self.assertIsNone(summary["total_raw_bytes"])
        self.assertIsNone(summary["total_protected_bytes"])
        self.assertIsNone(summary["predicted_peak_bytes"])
        self.assertEqual(summary["directories"]["export"]["state"], "unavailable")

    def test_gui_exposes_only_metadata_row_count_projection(self):
        self.assertEqual(ROW_COUNT_LABELS, {"Metadados (aproximado)": "metadata"})

    def test_cdc_retention_minutes_are_presented_as_days(self):
        self.assertEqual(format_minutes_as_days(262_800), "182,50 dias")
        self.assertEqual(format_minutes_as_days("1440"), "1,00 dia")
        self.assertEqual(format_minutes_as_days("x"), "Valor inválido")
        self.assertEqual(format_minutes_as_days(-1), "Valor inválido")

    def test_default_form_contract_is_infrastructure_neutral(self):
        config = default_gui_config(ROOT)
        self.assertEqual(config["config_version"], 2)
        self.assertEqual(config["perimeter"], "DESENVOLVIMENTO")
        self.assertEqual(config["source"]["database"], "BANCO_ORIGEM")
        self.assertEqual(config["bronze_destination"]["database"], "DBRO684")
        self.assertEqual(config["landing_destination"]["database"], "DLAN684")
        self.assertEqual(config["rows_per_block"], 200_000)
        self.assertEqual(config["max_file_bytes"], 157_286_400)
        self.assertEqual(config["keyless_direct_load_max_rows"], 5_000_000)
        self.assertEqual(config["cdc_retention_minutes"], 262_800)
        self.assertEqual(config["control_schema"], "dbo")
        self.assertEqual(
            config["structure"]["secondary_indexes_phase"], "before_load"
        )
        self.assertEqual(config["estimates"]["safety_factor"], 1.25)
        self.assertEqual(config["source"]["port"], 1433)
        self.assertEqual(config["bronze_destination"]["port"], 1433)
        self.assertEqual(config["landing_destination"]["port"], 1433)
        self.assertEqual(config["bronze_destination"]["structure_profile"], "bronze")
        self.assertEqual(config["landing_destination"]["structure_profile"], "landing")
        self.assertEqual(config["source"]["role"], "data_provider")
        self.assertEqual(
            config["bronze_destination"]["role"], "structure_and_data"
        )
        self.assertEqual(config["landing_destination"]["role"], "structure_only")
        self.assertNotIn("scope", config)
        self.assertFalse(config["allow_schema_evolution"])
        self.assertEqual(len(config["tables"]), 1)
        self.assertFalse(config["tables"][0]["enable_cdc"])
        self.assertEqual(config["tables"][0]["partition_column"], "dh_carga")
        self.assertEqual(config["active_destination"], "bronze")
        self.assertEqual(validate_config(config), config)

    def test_source_defaults_use_and_create_project_local_bulkflow_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            config = default_gui_config(project_root)
            expected_root = project_root.resolve() / "Local" / "BulkFlow"
            control = expected_root / ".bcp-control"
            data = expected_root / "bcp-data"
            ddl = expected_root / "ddl"

            self.assertEqual(config["local_control_directory"], str(control))
            self.assertEqual(config["executor_directory"], str(data))
            self.assertEqual(config["destination_sql_directory"], str(data))
            self.assertTrue(control.is_dir())
            self.assertTrue(data.is_dir())
            self.assertTrue(ddl.is_dir())

    def test_gui_configuration_contract_rejects_non_dbo_control_schema(self):
        config = default_gui_config(ROOT)
        config["control_schema"] = "controle_transferencia"
        with self.assertRaisesRegex(ConfigError, "control_schema deve ser dbo"):
            validate_config(config)

    def test_perimeter_username_suggestions_preserve_custom_values(self):
        self.assertEqual(default_username_for_perimeter("DESENVOLVIMENTO"), "u684")
        self.assertEqual(default_username_for_perimeter("HOMOLOGAÇÃO"), "h684")
        self.assertEqual(default_username_for_perimeter("PRODUÇÃO"), "s684")
        self.assertEqual(
            update_default_username_for_perimeter(
                "PRODUÇÃO", "u684", "DESENVOLVIMENTO"
            ),
            "s684",
        )
        self.assertEqual(
            update_default_username_for_perimeter(
                "PRODUÇÃO", "login_custom", "DESENVOLVIMENTO"
            ),
            "login_custom",
        )
        self.assertEqual(
            update_default_username_for_perimeter("HOMOLOGAÇÃO", "", None),
            "h684",
        )

    def test_watermark_parser_supports_simple_and_composite_keys(self):
        self.assertIsNone(parse_watermark("  "))
        parsed = parse_watermark("codigo, data_evento, sequencia")
        self.assertEqual(
            parsed,
            {
                "columns": [
                    {"name": "codigo", "direction": "ASC"},
                    {"name": "data_evento", "direction": "ASC"},
                    {"name": "sequencia", "direction": "ASC"},
                ]
            },
        )
        self.assertEqual(
            format_watermark(parsed),
            "codigo, data_evento, sequencia",
        )

    def test_watermark_parser_rejects_directions_newlines_empty_items_and_duplicates(self):
        for invalid in ("codigo:ASC", "codigo DESC", "codigo:SIDEWAYS"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "somente nomes de colunas"):
                    parse_watermark(invalid)
        with self.assertRaisesRegex(ValueError, "única linha"):
            parse_watermark("codigo\nsequencia")
        with self.assertRaisesRegex(ValueError, "entre as vírgulas"):
            parse_watermark("codigo,,sequencia")
        with self.assertRaisesRegex(ValueError, "repete a coluna"):
            parse_watermark("Codigo, codigo")
        with self.assertRaisesRegex(ValueError, "128"):
            parse_watermark("😀" * 65)

    def test_authentication_builder_never_receives_or_persists_plaintext_password(self):
        integrated = build_authentication(authentication_type="windows_integrated")
        self.assertEqual(integrated, {"type": "windows_integrated"})
        sql = build_authentication(
            authentication_type="sql",
            username="u684",
            secret_provider="env",
            secret_reference="BCP_SQL_PASSWORD",
        )
        self.assertEqual(
            sql,
            {
                "type": "sql",
                "username": "u684",
                "password": {"provider": "env", "reference": "BCP_SQL_PASSWORD"},
            },
        )
        self.assertNotIn("plaintext", json.dumps(sql))
        with self.assertRaisesRegex(ValueError, "Referência do segredo"):
            build_authentication(
                authentication_type="sql",
                username="u684",
                secret_provider="env",
            )

    def test_endpoint_builder_keeps_source_and_destination_contracts_separate(self):
        shared = {
            "instance": "127.0.0.1",
            "port": "14333",
            "database": "BD_ORIGEM",
            "schema": "dbo",
            "odbc_dsn": "BCP_BD_ORIGEM_LOCAL",
            "authentication_type": "sql",
            "username": "u684",
            "secret_provider": "prompt",
            "encrypt": True,
            "trust_server_certificate": True,
        }
        source = build_endpoint(shared, source=True)
        self.assertEqual(source["port"], 14333)
        self.assertEqual(source["role"], "data_provider")
        self.assertNotIn("read_database", source)
        self.assertNotIn("structure_profile", source)
        destination = build_endpoint(
            {
                **shared,
                "database": "DBRO684",
                "schema": "s344",
                "structure_profile": str(ROOT / "templates" / "bronze.json"),
            },
            source=False,
        )
        self.assertNotIn("read_database", destination)
        self.assertEqual(destination["role"], "structure_and_data")
        self.assertTrue(destination["structure_profile"].endswith("bronze.json"))

        for invalid in ("", "0", "65536", "abc"):
            with self.subTest(port=invalid):
                with self.assertRaisesRegex(ValueError, "Porta do SQL Server"):
                    build_endpoint({**shared, "port": invalid}, source=True)

    def test_table_builder_includes_cdc_watermark_and_optional_overrides(self):
        table = build_table(
            {
                "source_database": "BD_ORIGEM",
                "source_table": "TABELA_ORIGEM_03",
                "destination_table": "bd_origem_tabela_origem_03",
                "destination_database": "DBRO684",
                "source_schema": "dbo",
                "destination_schema": "s344",
                "rows_per_block": "1500",
                "enable_cdc": True,
                "watermark": "COLUNA_MARCA_DAGUA",
                "partition_enabled": True,
                "partition_column": "dh_carga",
            },
            destination_area="bronze",
        )
        self.assertTrue(table["enable_cdc"])
        self.assertEqual(table["rows_per_block"], 1_500)
        self.assertEqual(table["watermark"]["columns"][0]["name"], "COLUNA_MARCA_DAGUA")
        self.assertEqual(table["partition_column"], "dh_carga")
        self.assertNotIn("metadata_mapping", table)

    def test_table_builder_requires_both_schemas_and_omits_disabled_partition(self):
        base = {
            "source_database": "BD_ORIGEM",
            "source_table": "TABELA_ORIGEM_01",
            "destination_database": "DBRO684",
            "source_schema": "dbo",
            "destination_schema": "s344",
            "partition_enabled": False,
            "partition_column": "dh_carga",
        }
        self.assertNotIn(
            "partition_column", build_table(base, destination_area="bronze")
        )
        for missing in ("source_schema", "destination_schema"):
            with self.subTest(missing=missing):
                invalid = dict(base)
                invalid[missing] = ""
                with self.assertRaisesRegex(ValueError, "Esquema"):
                    build_table(invalid, destination_area="bronze")

    def test_new_table_form_inherits_visual_defaults(self):
        form = table_to_form(
            {},
            default_source_database="BD_ORIGEM",
            default_source_schema="dbo",
            default_destination_database="DBRO684",
            default_destination_schema="s344",
            default_rows_per_block="200000",
            default_structure_profile="bronze",
        )
        self.assertEqual(form["source_database"], "BD_ORIGEM")
        self.assertEqual(form["source_schema"], "dbo")
        self.assertEqual(form["destination_database"], "DBRO684")
        self.assertEqual(form["destination_schema"], "s344")
        self.assertEqual(form["rows_per_block"], "200000")
        self.assertEqual(form["structure_profile"], "bronze")
        self.assertTrue(form["partition_enabled"])
        self.assertEqual(form["partition_column"], "dh_carga")

        existing_without_partition = table_to_form(
            {
                "source_database": "BD_ORIGEM",
                "source_table": "origem",
                "destination_database": "DBRO684",
                "source_schema": "dbo",
                "destination_schema": "dbo",
            },
            default_rows_per_block="200000",
        )
        self.assertFalse(existing_without_partition["partition_enabled"])

    def test_automatic_destination_name_and_visual_inheritance_are_deterministic(self):
        self.assertEqual(
            automatic_destination_table("BD_ORIGEM", "TABELA_ORIGEM_01"),
            "bd_origem_tabela_origem_01",
        )
        self.assertEqual(automatic_destination_table("", "TABELA_ORIGEM_01"), "")

        form = table_to_form(
            {
                "source_database": "BD_ORIGEM",
                "source_table": "TABELA_ORIGEM_01",
                "source_schema": "dbo",
                "destination_database": "DBRO684",
                "destination_schema": "s344",
            },
            default_rows_per_block="200000",
            default_structure_profile="bronze",
        )
        self.assertEqual(form["destination_table"], "bd_origem_tabela_origem_01")
        contract = build_table(
            form,
            destination_area="bronze",
            default_rows_per_block="200000",
            default_structure_profile="bronze",
        )
        self.assertNotIn("rows_per_block", contract)
        self.assertNotIn("structure_profile", contract)

    def test_byte_summary_and_legacy_instance_projection(self):
        self.assertEqual(format_bytes_summary(104_857_600), "100,00 MB | 0,10 GB")
        self.assertEqual(format_bytes_summary("x"), "Valor inválido")
        self.assertEqual(
            split_instance_and_port("sql.example,14333"),
            ("sql.example", "14333"),
        )
        self.assertEqual(
            split_instance_and_port("SQL\\INST01", 1444),
            ("SQL\\INST01", "1444"),
        )

    def test_connection_summary_is_complete_and_contains_no_secret(self):
        config = default_gui_config(ROOT)
        rows = connection_summary_rows(config)
        self.assertEqual(
            [row[0] for row in rows], ["ORIGEM", "DESTINO 01", "DESTINO 02"]
        )
        self.assertEqual(rows[0][1:], ("u684", "SERVIDOR_ORIGEM", "1433", "BANCO_ORIGEM"))
        rendered = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("password", rendered.casefold())
        self.assertNotIn("reference", rendered.casefold())

    def test_optional_integer_distinguishes_blank_from_zero(self):
        self.assertIsNone(parse_optional_integer(" ", "Campo", minimum=1))
        with self.assertRaises(ValueError):
            parse_optional_integer("0", "Campo", minimum=1)

    def test_atomic_writer_round_trips_only_a_validated_contract(self):
        config = default_gui_config(ROOT)
        expected_root = ROOT.resolve() / "Local" / "BulkFlow"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            write_config_atomic(path, config)
            self.assertFalse(path.with_suffix(".json.partial").exists())
            loaded = read_config(path)
        self.assertEqual(loaded, config)
        self.assertEqual(
            loaded["local_control_directory"],
            str(expected_root / ".bcp-control"),
        )
        self.assertEqual(loaded["executor_directory"], str(expected_root / "bcp-data"))
        self.assertEqual(
            loaded["destination_sql_directory"], str(expected_root / "bcp-data")
        )

    def test_atomic_writer_does_not_replace_existing_file_with_invalid_config(self):
        config = default_gui_config(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            write_config_atomic(path, config)
            original = path.read_bytes()
            invalid = dict(config)
            invalid["rows_per_block"] = 0
            with self.assertRaises(ValueError):
                write_config_atomic(path, invalid)
            self.assertEqual(path.read_bytes(), original)

    def test_relative_custom_profiles_are_anchored_for_safe_save_as(self):
        config = default_gui_config(ROOT)
        config["bronze_destination"]["structure_profile"] = "../templates/bronze.json"
        config["landing_destination"]["structure_profile"] = "landing"
        config["tables"][0]["structure_profile"] = "profiles/custom.json"
        source_path = ROOT / "docker" / "source-config.json"
        resolved = resolve_profile_paths_for_editor(config, source_path)
        self.assertEqual(
            resolved["bronze_destination"]["structure_profile"],
            str((ROOT / "templates" / "bronze.json").resolve()),
        )
        self.assertEqual(
            resolved["tables"][0]["structure_profile"],
            str((ROOT / "docker" / "profiles" / "custom.json").resolve()),
        )
        self.assertEqual(resolved["landing_destination"]["structure_profile"], "landing")

    def test_frozen_defaults_live_in_local_app_data_not_beside_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_app_data = Path(temporary) / "AppData" / "Local"
            executable = (
                Path(temporary) / "Program Files" / "BulkFlow" / "BulkFlowGUI.exe"
            )
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(executable)
            ), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}, clear=False
            ):
                config = default_gui_config()
            directories_existed = all(
                (local_app_data / "BulkFlow" / name).is_dir()
                for name in (".bcp-control", "bcp-data", "ddl")
            )
        expected_root = (local_app_data / "BulkFlow").resolve()
        self.assertEqual(config["executor_directory"], str(expected_root / "bcp-data"))
        self.assertEqual(
            config["destination_sql_directory"], str(expected_root / "bcp-data")
        )
        self.assertEqual(
            config["local_control_directory"], str(expected_root / ".bcp-control")
        )
        self.assertTrue(directories_existed)
        self.assertEqual(config["bronze_destination"]["structure_profile"], "bronze")


class GuiConstructionTests(unittest.TestCase):
    def test_source_gui_ddl_field_uses_exact_project_local_default(self):
        try:
            from bcp_engine.gui import RUNTIME_DATA_ROOT, RUNTIME_DDL_DIRECTORY
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        expected_root = ROOT.resolve() / "Local" / "BulkFlow"
        self.assertEqual(RUNTIME_DATA_ROOT, expected_root)
        self.assertEqual(RUNTIME_DDL_DIRECTORY, expected_root / "ddl")

    def test_disk_projection_is_rendered_in_prerequisite_area(self):
        try:
            from bcp_engine.gui import BcpGuiApplication
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Tree:
            def __init__(self):
                self.rows = {}

            def get_children(self):
                return tuple(self.rows)

            def delete(self, item):
                self.rows.pop(item)

            def insert(self, _parent, _position, *, iid, values, tags):
                self.rows[iid] = (values, tags)

        application = object.__new__(BcpGuiApplication)
        application.disk_projection_overview = Value()
        application.export_disk_assessment = Value()
        application.import_disk_assessment = Value()
        application.disk_projection_tree = Tree()
        summary = {
            "unknown_table_count": 0,
            "known_raw_bytes": 1_000,
            "known_protected_bytes": 1_250,
            "total_raw_bytes": 1_000,
            "total_protected_bytes": 1_250,
            "predicted_peak_bytes": 500,
            "safety_factor": 1.25,
            "minimum_free_bytes": 100,
            "directories": {
                "export": {
                    "path": r"C:\BCP",
                    "free_bytes": 5_000,
                    "balance_after_peak_bytes": 4_400,
                    "balance_after_total_protected_bytes": 3_650,
                    "total_protected_fits": True,
                    "state": "sufficient",
                    "error": None,
                },
                "import": {
                    "path": r"\\sql\BCP",
                    "free_bytes": None,
                    "balance_after_peak_bytes": None,
                    "balance_after_total_protected_bytes": None,
                    "total_protected_fits": None,
                    "state": "unavailable",
                    "error": "sem acesso",
                },
            },
            "tables": [
                {
                    "source": "D.dbo.t",
                    "estimated_rows": 10,
                    "raw_bytes": 1_000,
                    "protected_bytes": 1_250,
                    "margin_bytes": 250,
                    "projection_state": "available",
                    "status": "PENDING",
                }
            ],
        }

        application._refresh_disk_projection(summary)

        self.assertIn("bruto", application.disk_projection_overview.value)
        self.assertIn("operação: SUFICIENTE", application.export_disk_assessment.value)
        self.assertIn("retenção integral: SUFICIENTE", application.export_disk_assessment.value)
        self.assertIn("INDISPONÍVEL", application.import_disk_assessment.value)
        self.assertEqual(len(application.disk_projection_tree.rows), 1)

    def test_table_dialog_downloads_prefilled_watermark_validation_script(self):
        try:
            from bcp_engine.gui import TableDialog
            from bcp_engine.watermark_validation import (
                WATERMARK_VALIDATION_GUIDANCE_PT_BR,
            )
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        dialog = object.__new__(TableDialog)
        dialog.window = object()
        dialog.variables = {
            "source_database": Value("BD_ORIGEM"),
            "source_schema": Value("dbo"),
            "source_table": Value("TABELA_ORIGEM_01"),
            "watermark": Value("COLUNA_MARCA_DAGUA_01, COLUNA_MARCA_DAGUA_02"),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "marca_dagua.sql"
            with (
                patch(
                    "bcp_engine.gui.filedialog.asksaveasfilename",
                    return_value=str(destination),
                ) as save_dialog,
                patch("bcp_engine.gui.messagebox.showinfo") as show_info,
                patch("bcp_engine.gui.messagebox.showerror") as show_error,
            ):
                dialog._save_watermark_validation_script()

            save_dialog.assert_called_once()
            self.assertEqual(
                save_dialog.call_args.kwargs["initialfile"],
                "validar_marca_dagua_BD_ORIGEM_dbo_TABELA_ORIGEM_01.sql",
            )
            show_error.assert_not_called()
            show_info.assert_called_once()
            self.assertEqual(
                show_info.call_args.args[1], WATERMARK_VALIDATION_GUIDANCE_PT_BR
            )
            script = destination.read_text(encoding="utf-8-sig")
            self.assertIn(
                "DECLARE @SourceDatabase       sysname       = N'BD_ORIGEM';",
                script,
            )
            self.assertIn(
                "DECLARE @WatermarkColumnsJson nvarchar(max) = "
                "N'[\"COLUNA_MARCA_DAGUA_01\",\"COLUNA_MARCA_DAGUA_02\"]';",
                script,
            )
            self.assertNotIn("__SOURCE_", script)
            self.assertNotIn("__WATERMARK_", script)

    def test_table_destination_suggestion_tracks_source_until_customized(self):
        try:
            from bcp_engine.gui import TableDialog
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        dialog = object.__new__(TableDialog)
        dialog.variables = {
            "source_database": Value("BD_ORIGEM"),
            "source_table": Value("TABELA_ORIGEM_01"),
            "destination_table": Value("bd_origem_tabela_origem_01"),
        }
        dialog._updating_automatic_destination = False
        dialog._destination_is_automatic = True
        dialog._previous_automatic_destination = "bd_origem_tabela_origem_01"

        dialog.variables["source_table"].set("TABELA_ORIGEM_04")
        dialog._on_source_identity_changed()
        self.assertEqual(
            dialog.variables["destination_table"].get(), "bd_origem_tabela_origem_04"
        )

        dialog.variables["destination_table"].set("destino_personalizado")
        dialog._on_destination_table_changed()
        dialog.variables["source_table"].set("TABELA_ORIGEM_05")
        dialog._on_source_identity_changed()
        self.assertEqual(
            dialog.variables["destination_table"].get(), "destino_personalizado"
        )

        dialog.variables["destination_table"].set("")
        dialog._on_destination_table_changed()
        dialog.variables["source_table"].set("TABELA_ORIGEM_01")
        dialog._on_source_identity_changed()
        self.assertEqual(
            dialog.variables["destination_table"].get(), "bd_origem_tabela_origem_01"
        )

    def test_table_save_keeps_equal_batch_and_profile_as_inherited(self):
        try:
            from bcp_engine.gui import TableDialog
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Window:
            destroyed = False

            def destroy(self):
                self.destroyed = True

        values = {
            "source_database": "BD_ORIGEM",
            "source_schema": "dbo",
            "source_table": "TABELA_ORIGEM_01",
            "destination_database": "DBRO684",
            "destination_schema": "s344",
            "destination_table": "bd_origem_tabela_origem_01",
            "rows_per_block": "200000",
            "structure_profile": "bronze",
            "enable_cdc": False,
            "watermark": "",
            "partition_enabled": True,
            "partition_column": "dh_carga",
            "aud_ccid_mode": "Constante",
            "aud_ccid_value": "1",
            "aud_cntrrn_mode": "Constante",
            "aud_cntrrn_value": "1",
            "aud_enttyp_mode": "Constante",
            "aud_enttyp_value": "PT",
        }
        dialog = object.__new__(TableDialog)
        dialog.variables = {name: Value(value) for name, value in values.items()}
        dialog.default_source_database = "BD_ORIGEM"
        dialog.default_destination_database = "DBRO684"
        dialog.default_rows_per_block = "200000"
        dialog.default_structure_profile = "bronze"
        dialog.destination_area = "bronze"
        dialog.window = Window()
        dialog.result = None

        dialog._save()

        self.assertEqual(dialog.result["rows_per_block"], "")
        self.assertEqual(dialog.result["structure_profile"], "")
        self.assertTrue(dialog.window.destroyed)

    def test_prerequisite_button_uses_the_shared_inspection_service(self):
        try:
            from bcp_engine.gui import BcpGuiApplication
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class Report:
            ready = True
            errors: tuple[str, ...] = ()

            @staticmethod
            def as_dict():
                return {
                    "bcp_version": "15.0.4298.1",
                    "bcp_resolved_path": r"C:\tools\bcp.exe",
                }

        application = object.__new__(BcpGuiApplication)
        application.busy = False
        application.root = None
        application.worker_queue = __import__("queue").Queue()
        application.global_variables = {
            "odbc_driver": Value("ODBC Driver 18 for SQL Server"),
            "bcp_executable": Value("bcp"),
        }
        completed: dict[str, str] = {}

        def run_now(label, operation):
            completed["label"] = label
            completed["result"] = operation()

        application._start_worker = run_now
        with patch("bcp_engine.gui.inspect_prerequisites", return_value=Report()) as inspect:
            application._verify_prerequisites()

        inspect.assert_called_once_with(
            {
                "odbc_driver": "ODBC Driver 18 for SQL Server",
                "bcp_executable": "bcp",
            }
        )
        self.assertEqual(completed["label"], "Verificando pré-requisitos")
        self.assertIn(r"C:\tools\bcp.exe", completed["result"])
        self.assertEqual(
            application.worker_queue.get_nowait(),
            ("prerequisites", "OK — BCP 15.0.4298.1"),
        )

    def test_gui_engine_always_uses_tk_injected_secret_resolver(self):
        try:
            from bcp_engine.auth import SecretResolver
            from bcp_engine.gui import BcpGuiApplication
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        application = object.__new__(BcpGuiApplication)
        application.current_config_path = None
        application.worker_queue = __import__("queue").Queue()
        prompts: list[str] = []
        application.secret_resolver = SecretResolver(
            prompt=lambda label: prompts.append(label) or "senha-mascarada"
        )
        engine = application._engine(default_gui_config(ROOT))
        secret = engine.resolver.resolve_auth(
            engine.config["source"]["authentication"], endpoint_name="source"
        )
        self.assertIs(engine.resolver, application.secret_resolver)
        self.assertEqual(secret.reveal(), "senha-mascarada")
        self.assertEqual(len(prompts), 1)

    def test_final_gui_log_redacts_registered_secret(self):
        try:
            from bcp_engine.auth import SecretValue
            from bcp_engine.gui import BcpGuiApplication
        except ImportError as error:  # pragma: no cover - Linux CI may omit Tk/GUI extras
            self.skipTest(f"Dependência gráfica indisponível: {error}")

        class LogBuffer:
            def __init__(self):
                self.value = ""

            def configure(self, **_kwargs):
                pass

            def insert(self, _where, value):
                self.value += value

            def see(self, _where):
                pass

        secret = "senha-gui-final-nao-pode-vazar-684"
        SecretValue(secret)
        application = object.__new__(BcpGuiApplication)
        application.log_text = LogBuffer()

        application._append_log(f"falha simulada com {secret}")

        self.assertNotIn(secret, application.log_text.value)
        self.assertIn("********", application.log_text.value)

    def test_window_constructs_and_collects_the_same_validated_contract(self):
        try:
            import ttkbootstrap as ttk
            from bcp_engine.gui import BcpGuiApplication

            root = ttk.Window(themename="flatly")
        except Exception as error:  # pragma: no cover - Linux CI without a display
            self.skipTest(f"Ambiente gráfico indisponível: {error}")
        try:
            root.withdraw()
            application = BcpGuiApplication(root)
            root.update_idletasks()
            self.assertEqual(
                [
                    application._connections_notebook.tab(index, "text")
                    for index in range(application._connections_notebook.index("end"))
                ],
                ["Origem", "Destino 01", "Destino 02"],
            )
            expected_roles = {
                "source": (
                    "provedor de dados",
                    ("provedor de dados",),
                ),
                "bronze_destination": (
                    "estrutura e dados",
                    (
                        "estrutura e dados",
                        "somente estrutura",
                        "somente dados (premissa: já existir a estrutura)",
                    ),
                ),
                "landing_destination": (
                    "somente estrutura",
                    (
                        "estrutura e dados",
                        "somente estrutura",
                        "somente dados (premissa: já existir a estrutura)",
                    ),
                ),
            }
            for endpoint, (default_role, options) in expected_roles.items():
                role_variable = application.endpoint_variables[endpoint]["role"]
                role_widget = application._role_comboboxes[endpoint]
                self.assertEqual(role_variable.get(), default_role)
                self.assertEqual(tuple(role_widget.cget("values")), options)
                self.assertEqual(str(role_widget.cget("state")), "readonly")
                self.assertEqual(
                    str(role_widget.cget("style")), "DefaultValue.TCombobox"
                )
                self.assertTrue(hasattr(role_widget, "_field_tooltip"))
            self.assertEqual(
                application.global_variables["destination_sql_directory"].get(),
                application.global_variables["executor_directory"].get(),
            )
            self.assertEqual(application.cdc_retention_days.get(), "182,50 dias")
            application.global_variables["cdc_retention_minutes"].set("1440")
            self.assertEqual(application.cdc_retention_days.get(), "1,00 dia")
            application.global_variables["cdc_retention_minutes"].set("262800")
            application.global_variables["executor_directory"].set(
                r"C:\transferencia\exportacao-a"
            )
            self.assertEqual(
                application.global_variables["destination_sql_directory"].get(),
                r"C:\transferencia\exportacao-a",
            )
            application.global_variables["destination_sql_directory"].set(
                r"\\sql\share\importacao"
            )
            application.global_variables["executor_directory"].set(
                r"C:\transferencia\exportacao-b"
            )
            self.assertEqual(
                application.global_variables["destination_sql_directory"].get(),
                r"\\sql\share\importacao",
            )
            application.global_variables["destination_sql_directory"].set("")
            application.global_variables["executor_directory"].set(
                r"C:\transferencia\exportacao-c"
            )
            self.assertEqual(
                application.global_variables["destination_sql_directory"].get(),
                r"C:\transferencia\exportacao-c",
            )
            self.assertEqual(application.table_tree.get_children(), ())
            self.assertEqual(
                application.endpoint_variables["bronze_destination"]["schema"].get(), ""
            )
            application.endpoint_variables["bronze_destination"]["schema"].set("s344")
            self.assertEqual(
                application.endpoint_variables["landing_destination"]["schema"].get(),
                "s344",
            )
            defaults = application._table_dialog_defaults()
            self.assertEqual(defaults["default_source_database"], "BANCO_ORIGEM")
            self.assertEqual(defaults["default_source_schema"], "dbo")
            self.assertEqual(defaults["default_destination_database"], "DBRO684")
            self.assertEqual(defaults["default_destination_schema"], "s344")
            self.assertEqual(defaults["default_rows_per_block"], "200000")
            self.assertEqual(defaults["default_structure_profile"], "bronze")
            table = table_to_form(
                default_gui_config(ROOT)["tables"][0],
                default_source_database="BANCO_ORIGEM",
                default_source_schema="dbo",
                default_destination_database="DBRO684",
                default_destination_schema="s344",
                default_rows_per_block="200000",
                default_structure_profile="bronze",
            )
            application.table_forms = [table]
            application._refresh_table_tree()
            self.assertEqual(
                str(application._control_schema_entry.cget("state")), "disabled"
            )
            application.global_variables["control_schema"].set(
                "controle_transferencia"
            )
            config = application._collect_config()
            self.assertEqual(config["rows_per_block"], 200_000)
            self.assertEqual(len(config["tables"]), 1)
            self.assertEqual(config["perimeter"], "DESENVOLVIMENTO")
            self.assertEqual(config["active_destination"], "bronze")
            self.assertEqual(config["source"]["role"], "data_provider")
            self.assertEqual(
                config["bronze_destination"]["role"], "structure_and_data"
            )
            self.assertEqual(
                config["landing_destination"]["role"], "structure_only"
            )
            self.assertTrue(config["create_structure_if_needed"])
            self.assertEqual(config["max_file_bytes"], 157_286_400)
            self.assertEqual(config["control_schema"], "dbo")
            self.assertEqual(
                application.global_variables["control_schema"].get(), "dbo"
            )
            self.assertEqual(
                config["structure"]["secondary_indexes_phase"], "before_load"
            )
            self.assertEqual(config["source"]["port"], 1433)
            self.assertEqual(
                application.endpoint_variables["source"]["domain"].get(), ""
            )
            self.assertEqual(
                str(application._domain_entries["source"].cget("state")), "disabled"
            )
            application.endpoint_variables["source"]["authentication_type"].set(
                "Credencial Windows"
            )
            application._update_auth_state("source")
            self.assertEqual(
                str(application._domain_entries["source"].cget("state")), "normal"
            )
            with patch.object(application.secret_resolver, "clear_cache") as clear_cache:
                application.endpoint_variables["source"]["port"].set("1434")
            clear_cache.assert_called()

            application.endpoint_variables["bronze_destination"]["role"].set(
                "somente estrutura"
            )
            application.endpoint_variables["landing_destination"]["role"].set(
                "estrutura e dados"
            )
            self.assertEqual(application._current_destination_area(), "landing")
            switched_defaults = application._table_dialog_defaults()
            self.assertEqual(
                switched_defaults["default_destination_database"], "DLAN684"
            )
            self.assertEqual(
                switched_defaults["default_structure_profile"], "landing"
            )
            application.endpoint_variables["bronze_destination"]["role"].set(
                "estrutura e dados"
            )
            with self.assertRaisesRegex(ValueError, "exatamente um destino"):
                application._required_data_destination_area()

            application.global_variables["execute_import"].set(False)
            application.endpoint_variables["source"]["authentication_type"].set(
                "Windows integrada"
            )
            application._update_auth_state("source")
            application.endpoint_variables["bronze_destination"]["role"].set(
                "somente estrutura"
            )
            application.endpoint_variables["landing_destination"]["role"].set(
                "somente estrutura"
            )
            structure_only = application._collect_config()
            self.assertFalse(structure_only["execute_import"])
            self.assertEqual(structure_only["active_destination"], "bronze")

            highlighted_variables = {
                id(record[1]) for record in application._default_widgets
            }
            self.assertIn(
                id(application.global_variables["control_schema"]),
                highlighted_variables,
            )
            for name in ("artifact_reader_sids", "artifact_writer_sids"):
                self.assertIn(
                    id(application.global_variables[name]), highlighted_variables
                )
            for name in (
                "executor_directory",
                "destination_sql_directory",
            ):
                self.assertNotIn(
                    id(application.global_variables[name]), highlighted_variables
                )
            self.assertIn(
                id(application.global_variables["cdc_retention_minutes"]),
                highlighted_variables,
            )
            for endpoint in application.endpoint_variables.values():
                self.assertIn(id(endpoint["role"]), highlighted_variables)
                self.assertIn(id(endpoint["odbc_dsn"]), highlighted_variables)
                self.assertNotIn(id(endpoint["username"]), highlighted_variables)
                for name in ("instance", "port", "database", "schema"):
                    self.assertNotIn(id(endpoint[name]), highlighted_variables)
        finally:
            root.destroy()

    def test_product_name_and_tooltip_inventory(self):
        try:
            from bcp_engine.gui import FIELD_HELP, PRODUCT_NAME, TABLE_CDC_LABEL
        except ImportError as error:  # pragma: no cover
            self.skipTest(f"Dependência gráfica indisponível: {error}")
        self.assertEqual(PRODUCT_NAME, "BulkFlow - SQL Server Data Export & Load")
        self.assertEqual(TABLE_CDC_LABEL, "Ativar CDC na tabela de origem")
        required = {
            "perimeter",
            "create_structure_if_needed",
            "rows_per_block",
            "cdc_retention_minutes",
            "max_file_bytes",
            "instance",
            "port",
            "database",
            "schema",
            "role",
            "authentication_type",
            "username",
            "domain",
            "source_database",
            "source_table",
            "destination_database",
            "destination_table",
            "source_schema",
            "destination_schema",
            "watermark",
            "watermark_validation_script",
            "enable_cdc",
            "partition_column",
            "ddl_area",
            "ddl_output",
            "execution_id",
            "manifest_path",
        }
        self.assertFalse(required - set(FIELD_HELP))
        self.assertIn(
            "provisionamento automático do destino",
            FIELD_HELP["create_structure_if_needed"],
        )
        self.assertIn("Aplicar DDL", FIELD_HELP["create_structure_if_needed"])
        self.assertEqual(
            FIELD_HELP["enable_cdc"],
            "Ativa e confirma o CDC em "
            "banco_origem.esquema_origem.tabela_origem antes da exportação.",
        )


if __name__ == "__main__":
    unittest.main()
