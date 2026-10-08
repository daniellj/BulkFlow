from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from bcp_engine.config import (
    CONFIG_VERSION,
    DEFAULT_ENDPOINT_ROLES,
    ConfigError,
    active_destination,
    data_destination_areas,
    endpoint_role,
    effective_delete_confirmed_files,
    effective_tables,
    fingerprints,
    operational_fingerprint,
    read_config,
    role_includes_data,
    role_includes_structure,
    structural_fingerprint,
    validate_config,
)
from bcp_engine.profiles import load_profile


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class ConfigExamplesTests(unittest.TestCase):
    def test_all_examples_are_valid_and_normalization_is_idempotent(self):
        paths = sorted(EXAMPLES.glob("config.*.json"))
        self.assertGreaterEqual(len(paths), 6)
        for path in paths:
            with self.subTest(path=path.name):
                config = read_config(path)
                self.assertEqual(config["config_version"], CONFIG_VERSION)
                self.assertEqual(config["perimeter"], "DESENVOLVIMENTO")
                self.assertEqual(config["active_destination"], "bronze")
                self.assertEqual(validate_config(config), config)

    def test_full_example_has_script_tables_and_independent_endpoints(self):
        config = read_config(EXAMPLES / "config.full.json")
        self.assertEqual(len(config["tables"]), 3)
        self.assertEqual(
            [table["source_table"] for table in config["tables"]],
            ["TABELA_ORIGEM_01", "TABELA_ORIGEM_02", "TABELA_ORIGEM_03"],
        )
        self.assertEqual(config["rows_per_block"], 1500)
        self.assertEqual(
            config["tables"][2]["watermark"]["columns"],
            [{"name": "COLUNA_MARCA_DAGUA", "direction": "ASC"}],
        )
        self.assertEqual(config["source"]["schema"], "ESQUEMA_ORIGEM")
        self.assertEqual(config["bronze_destination"]["schema"], "esquema_destino")
        self.assertEqual(config["landing_destination"]["schema"], "esquema_destino")
        self.assertIsNot(
            config["source"]["authentication"],
            config["bronze_destination"]["authentication"],
        )
        self.assertEqual(active_destination(config)["database"], "BD_DESTINO_01")
        self.assertEqual(config["connection_timeout_seconds"], 30)
        self.assertEqual(config["cdc_retention_minutes"], 262_800)
        self.assertEqual(config["source"]["port"], 1433)
        self.assertEqual(config["bronze_destination"]["port"], 1433)
        self.assertEqual(config["control_schema"], "dbo")
        self.assertEqual(config["source"]["role"], "data_provider")
        self.assertEqual(
            config["bronze_destination"]["role"], "structure_and_data"
        )
        self.assertEqual(
            config["landing_destination"]["role"], "structure_only"
        )
        self.assertEqual(data_destination_areas(config), ("bronze",))
        self.assertFalse(config["allow_schema_evolution"])
        self.assertEqual(config["artifact_reader_sids"], [])
        self.assertEqual(config["artifact_writer_sids"], [])
        self.assertTrue(all(table["enable_cdc"] for table in config["tables"]))
        self.assertTrue(all(table["enable_cdc"] for table in effective_tables(config)))

    def test_landing_example_is_ddl_only_and_has_no_data_mapping(self):
        config = read_config(EXAMPLES / "config.landing.json")
        self.assertFalse(config["execute_import"])
        self.assertNotIn("bronze_destination", config)
        self.assertEqual(config["landing_destination"]["database"], "BD_DESTINO_02")
        self.assertEqual(
            config["landing_destination"]["structure_profile"],
            "../templates/landing.json",
        )
        self.assertNotIn("metadata_mapping", config["tables"][0])

    def test_sql_example_uses_independent_endpoint_secret_references(self):
        config = read_config(EXAMPLES / "config.auth-sql.json")
        references = {
            config[name]["authentication"]["password"]["reference"]
            for name in ("source", "bronze_destination", "landing_destination")
        }
        self.assertEqual(len(references), 3)

    def test_export_only_needs_no_destination_or_sql_visible_path(self):
        config = read_config(EXAMPLES / "config.export-only.json")
        self.assertFalse(config["execute_import"])
        self.assertNotIn("bronze_destination", config)
        self.assertNotIn("landing_destination", config)
        self.assertNotIn("destination_sql_directory", config)
        self.assertIsNone(active_destination(config, required=False))
        self.assertFalse(effective_delete_confirmed_files(config))
        tables = effective_tables(config)
        self.assertIsNone(tables[0]["destination"]["instance"])
        self.assertIsNone(tables[0]["destination"]["schema"])
        self.assertEqual(
            tables[0]["destination"]["structure_profile"], "bronze"
        )

    def test_every_example_profile_resolves_from_its_config_directory(self):
        for path in sorted(EXAMPLES.glob("config.*.json")):
            with self.subTest(path=path.name):
                config = read_config(path)
                first = effective_tables(config)[0]
                profile = load_profile(
                    first["destination"]["structure_profile"],
                    directory=path.parent,
                )
                self.assertIn(profile["name"], {"bronze", "landing"})

    def test_table_defaults_and_overrides_are_effective(self):
        config = read_config(EXAMPLES / "config.watermark-composite.json")
        table = effective_tables(config)[0]
        self.assertEqual(table["source"]["schema"], "ESQUEMA_ORIGEM")
        self.assertEqual(table["destination"]["schema"], "esquema_destino")
        self.assertEqual(table["rows_per_block"], 100_000)
        self.assertEqual(
            table["destination"]["structure_profile"], "bronze"
        )
        self.assertEqual(
            table["watermark"]["columns"],
            [
                {"name": "COLUNA_MARCA_DAGUA_01", "direction": "ASC"},
                {"name": "COLUNA_MARCA_DAGUA_02", "direction": "ASC"},
            ],
        )

    def test_watermark_direction_is_always_ascending(self):
        value = json.loads((EXAMPLES / "config.full.json").read_text(encoding="utf-8"))
        value["tables"][2]["watermark"]["columns"][0]["direction"] = "DESC"
        with self.assertRaisesRegex(ConfigError, "direction deve ser ASC"):
            validate_config(value)

        value["tables"][2]["watermark"]["columns"][0].pop("direction")
        normalized = validate_config(value)
        self.assertEqual(
            normalized["tables"][2]["watermark"]["columns"][0]["direction"],
            "ASC",
        )

    def test_destination_identifiers_are_normalized_to_lowercase(self):
        value = json.loads((EXAMPLES / "config.full.json").read_text(encoding="utf-8"))
        value["bronze_destination"]["schema"] = "S344"
        value["tables"][0]["destination_schema"] = "CUSTOM_SCHEMA"
        value["tables"][0]["destination_table"] = "CUSTOM_TABLE"
        normalized = validate_config(value)
        self.assertEqual(normalized["bronze_destination"]["schema"], "s344")
        self.assertEqual(normalized["tables"][0]["destination_schema"], "custom_schema")
        self.assertEqual(normalized["tables"][0]["destination_table"], "custom_table")
        self.assertEqual(normalized["tables"][0]["source_table"], "TABELA_ORIGEM_01")

    def test_sql_identifiers_use_sysname_utf16_limit_after_lowercase(self):
        value = json.loads((EXAMPLES / "config.full.json").read_text(encoding="utf-8"))
        value["source"]["schema"] = "😀" * 65
        with self.assertRaisesRegex(ConfigError, "128 unidades UTF-16"):
            validate_config(value)

        value = json.loads((EXAMPLES / "config.full.json").read_text(encoding="utf-8"))
        value["bronze_destination"]["schema"] = "İ" * 128
        with self.assertRaisesRegex(ConfigError, "128 unidades UTF-16"):
            validate_config(value)

    def test_schema_document_is_versioned_json(self):
        schema = json.loads((ROOT / "schemas" / "config-v2.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["config_version"]["const"], 2)
        direct = schema["properties"]["keyless_direct_load_max_rows"]
        self.assertEqual(direct["default"], 5_000_000)
        self.assertEqual(direct["minimum"], 0)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["connection_timeout_seconds"]["default"], 30
        )
        self.assertEqual(schema["properties"]["rows_per_block"]["default"], 200_000)
        self.assertEqual(
            schema["properties"]["cdc_retention_minutes"]["default"], 262_800
        )
        self.assertEqual(schema["properties"]["cdc_retention_minutes"]["minimum"], 1)
        self.assertEqual(
            schema["properties"]["cdc_retention_minutes"]["maximum"], 52_494_800
        )
        self.assertEqual(
            schema["properties"]["estimates"]["properties"]["safety_factor"]["default"],
            1.25,
        )
        self.assertEqual(
            schema["properties"]["estimates"]["properties"]["row_count_method"],
            {"const": "metadata", "default": "metadata"},
        )
        self.assertEqual(
            schema["properties"]["create_structure_if_needed"],
            {
                "type": "boolean",
                "default": True,
                "description": (
                    "Quando true, cria as estruturas de destino ausentes. "
                    "Quando false, exige que elas j\u00e1 existam."
                ),
            },
        )
        self.assertEqual(schema["properties"]["max_file_bytes"]["default"], 157_286_400)
        self.assertEqual(schema["properties"]["control_schema"]["default"], "dbo")
        self.assertEqual(schema["properties"]["control_schema"]["const"], "dbo")
        self.assertEqual(
            schema["properties"]["structure"]["properties"]
            ["secondary_indexes_phase"]["default"],
            "before_load",
        )
        table_properties = schema["$defs"]["table"]["properties"]
        self.assertIn("source_database", table_properties)
        self.assertIn("destination_database", table_properties)
        self.assertIn("port", schema["$defs"]["sourceEndpoint"]["required"])
        self.assertEqual(
            schema["$defs"]["sourceEndpoint"]["properties"]["role"]["const"],
            "data_provider",
        )
        self.assertEqual(
            schema["properties"]["bronze_destination"]["properties"]["role"]["default"],
            "structure_and_data",
        )
        self.assertEqual(
            schema["properties"]["landing_destination"]["properties"]["role"]["default"],
            "structure_only",
        )
        self.assertEqual(
            schema["properties"]["active_destination"]["enum"],
            ["bronze", "landing"],
        )
        self.assertFalse(schema["$defs"]["table"]["properties"]["enable_cdc"]["default"])


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.full = read_config(EXAMPLES / "config.full.json")

    def test_row_count_method_accepts_only_metadata(self):
        exact = copy.deepcopy(self.full)
        exact["estimates"]["row_count_method"] = "count_big"
        with self.assertRaisesRegex(
            ConfigError, "estimates.row_count_method deve ser metadata"
        ):
            validate_config(exact)

    def test_cdc_retention_defaults_and_requires_positive_sql_int(self):
        without_explicit_value = copy.deepcopy(self.full)
        without_explicit_value.pop("cdc_retention_minutes", None)
        self.assertEqual(
            validate_config(without_explicit_value)["cdc_retention_minutes"],
            262_800,
        )

        for invalid in (0, -1, True, 52_494_801, "262800"):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["cdc_retention_minutes"] = invalid
                with self.assertRaisesRegex(ConfigError, "cdc_retention_minutes"):
                    validate_config(rejected)

    def test_unknown_root_table_and_auth_keys_are_rejected(self):
        cases = []
        root = copy.deepcopy(self.full)
        root["global_password"] = "não permitido"
        cases.append(root)
        table = copy.deepcopy(self.full)
        table["tables"][0]["key"] = ["id"]
        cases.append(table)
        auth = copy.deepcopy(self.full)
        auth["source"]["authentication"]["reuse_destination"] = True
        cases.append(auth)
        for value in cases:
            with self.subTest(keys=value.keys()):
                with self.assertRaisesRegex(ConfigError, "desconhecido|desconhecidos"):
                    validate_config(value)

    def test_perimeter_defaults_to_development_and_requires_exact_enum(self):
        value = copy.deepcopy(self.full)
        value.pop("perimeter")
        self.assertEqual(validate_config(value)["perimeter"], "DESENVOLVIMENTO")

        for invalid in (
            "desenvolvimento",
            "Homologação",
            "HOMOLOGACAO",
            "produção",
            "PRODUCAO",
            "PRODUÇÃO ",
            "OUTRO",
        ):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["perimeter"] = invalid
                with self.assertRaisesRegex(ConfigError, "perimeter"):
                    validate_config(rejected)

    def test_previous_perimeter_values_are_not_accepted(self):
        previous_values = (
            "".join(map(chr, (68, 82, 69, 65, 68, 83))),
            "".join(map(chr, (67, 65, 80, 71, 86))),
        )
        for previous_value in previous_values:
            with self.subTest(previous_value=previous_value):
                rejected = copy.deepcopy(self.full)
                rejected["perimeter"] = previous_value
                with self.assertRaisesRegex(ConfigError, "perimeter"):
                    validate_config(rejected)

    def test_perimeter_supplies_only_missing_sql_usernames(self):
        expected_by_perimeter = {
            "DESENVOLVIMENTO": "u684",
            "HOMOLOGAÇÃO": "h684",
            "PRODUÇÃO": "s684",
        }
        sql_example = read_config(EXAMPLES / "config.auth-sql.json")
        endpoints = ("source", "bronze_destination", "landing_destination")
        for perimeter, expected in expected_by_perimeter.items():
            with self.subTest(perimeter=perimeter):
                value = copy.deepcopy(sql_example)
                value["perimeter"] = perimeter
                for endpoint in endpoints:
                    value[endpoint]["authentication"].pop("username")
                normalized = validate_config(value)
                self.assertEqual(
                    [normalized[name]["authentication"]["username"] for name in endpoints],
                    [expected, expected, expected],
                )

        custom = copy.deepcopy(sql_example)
        custom["perimeter"] = "PRODUÇÃO"
        custom["source"]["authentication"]["username"] = "custom_source"
        custom["bronze_destination"]["authentication"]["username"] = "custom_bronze"
        custom["landing_destination"]["authentication"]["username"] = "custom_landing"
        normalized = validate_config(custom)
        self.assertEqual(normalized["source"]["authentication"]["username"], "custom_source")
        self.assertEqual(
            normalized["bronze_destination"]["authentication"]["username"],
            "custom_bronze",
        )
        self.assertEqual(
            normalized["landing_destination"]["authentication"]["username"],
            "custom_landing",
        )

    def test_direct_keyless_limit_defaults_to_five_million_and_zero_disables(self):
        without_explicit_value = copy.deepcopy(self.full)
        without_explicit_value.pop("keyless_direct_load_max_rows", None)
        self.assertEqual(
            validate_config(without_explicit_value)[
                "keyless_direct_load_max_rows"
            ],
            5_000_000,
        )

        disabled = copy.deepcopy(self.full)
        disabled["keyless_direct_load_max_rows"] = 0
        self.assertEqual(
            validate_config(disabled)["keyless_direct_load_max_rows"], 0
        )

        for invalid in (-1, True, 9_223_372_036_854_775_808):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["keyless_direct_load_max_rows"] = invalid
                with self.assertRaisesRegex(
                    ConfigError, "keyless_direct_load_max_rows"
                ):
                    validate_config(rejected)

    def test_artifact_reader_sids_are_specific_deduplicated_windows_sids(self):
        configured = copy.deepcopy(self.full)
        configured["artifact_reader_sids"] = [
            "S-1-5-80-123-456",
            "s-1-5-80-123-456",
            "S-1-5-21-1-2-3-1001",
        ]
        self.assertEqual(
            validate_config(configured)["artifact_reader_sids"],
            ["S-1-5-80-123-456", "S-1-5-21-1-2-3-1001"],
        )

        for invalid in (
            "Todos",
            "S-1-1-0",
            "S-1-5-11",
            "S-1-5-32-545",
        ):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["artifact_reader_sids"] = [invalid]
                with self.assertRaisesRegex(ConfigError, "SID Windows|grupo amplo"):
                    validate_config(rejected)

        writer = copy.deepcopy(self.full)
        writer["artifact_writer_sids"] = ["S-1-5-21-1-2-3-2001"]
        self.assertEqual(
            validate_config(writer)["artifact_writer_sids"],
            ["S-1-5-21-1-2-3-2001"],
        )

    def test_enable_cdc_defaults_to_false_and_requires_boolean(self):
        without_flag = copy.deepcopy(self.full)
        without_flag["tables"][0].pop("enable_cdc")
        normalized = validate_config(without_flag)
        self.assertFalse(normalized["tables"][0]["enable_cdc"])
        self.assertFalse(effective_tables(normalized)[0]["enable_cdc"])

        for invalid in (1, 0, "true", None):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["tables"][0]["enable_cdc"] = invalid
                with self.assertRaisesRegex(ConfigError, "enable_cdc"):
                    validate_config(rejected)

    def test_plaintext_password_and_conflicting_auth_fields_are_rejected(self):
        sql = copy.deepcopy(self.full)
        sql["source"]["authentication"] = {
            "type": "sql", "username": "leitor", "password": "segredo-em-claro"
        }
        with self.assertRaises(ConfigError):
            validate_config(sql)

        integrated = copy.deepcopy(self.full)
        integrated["source"]["authentication"]["username"] = "usuario_indevido"
        with self.assertRaisesRegex(ConfigError, "windows_integrated não aceita"):
            validate_config(integrated)

    def test_duplicate_effective_destination_is_rejected_case_insensitively(self):
        value = copy.deepcopy(self.full)
        value["tables"][1]["destination_table"] = value["tables"][0]["destination_table"].upper()
        with self.assertRaisesRegex(ConfigError, "mesmo objeto"):
            validate_config(value)

    def test_endpoint_accepts_explicit_odbc_dsn(self):
        value = copy.deepcopy(self.full)
        value["source"]["odbc_dsn"] = "BCP_ORIGEM_LOCAL"
        normalized = validate_config(value)
        self.assertEqual(normalized["source"]["odbc_dsn"], "BCP_ORIGEM_LOCAL")

        for invalid in ("x;UID=outro", "{dsn}", "x" * 33):
            with self.subTest(invalid=invalid):
                rejected = copy.deepcopy(self.full)
                rejected["source"]["odbc_dsn"] = invalid
                with self.assertRaisesRegex(ConfigError, "odbc_dsn"):
                    validate_config(rejected)

    def test_connection_and_sql_timeouts_accept_endpoint_overrides(self):
        value = copy.deepcopy(self.full)
        value["connection_timeout_seconds"] = 41
        value["sql_timeout_seconds"] = 59
        value["source"]["connection_timeout_seconds"] = 7
        value["source"]["sql_timeout_seconds"] = 11
        normalized = validate_config(value)
        self.assertEqual(normalized["connection_timeout_seconds"], 41)
        self.assertEqual(normalized["sql_timeout_seconds"], 59)
        self.assertEqual(normalized["source"]["connection_timeout_seconds"], 7)
        self.assertEqual(normalized["source"]["sql_timeout_seconds"], 11)

        for key in ("connection_timeout_seconds", "sql_timeout_seconds"):
            rejected = copy.deepcopy(self.full)
            rejected[key] = -1
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                validate_config(rejected)

    def test_endpoint_ports_are_normalized_and_bounded(self):
        value = copy.deepcopy(self.full)
        value["source"]["port"] = 15433
        self.assertEqual(validate_config(value)["source"]["port"], 15433)

        legacy = copy.deepcopy(self.full)
        legacy["source"].pop("port")
        self.assertEqual(validate_config(legacy)["source"]["port"], 1433)

        for invalid in (0, 65_536, "1433", True):
            rejected = copy.deepcopy(self.full)
            rejected["source"]["port"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ConfigError, "port"):
                validate_config(rejected)

    def test_latest_operational_defaults_are_applied(self):
        value = copy.deepcopy(self.full)
        for key in ("create_structure_if_needed", "max_file_bytes", "control_schema"):
            value.pop(key, None)
        value.pop("structure", None)

        normalized = validate_config(value)

        self.assertTrue(normalized["create_structure_if_needed"])
        self.assertEqual(normalized["max_file_bytes"], 157_286_400)
        self.assertEqual(normalized["control_schema"], "dbo")
        self.assertEqual(
            normalized["structure"]["secondary_indexes_phase"], "before_load"
        )

    def test_structure_creation_can_be_disabled_explicitly(self):
        value = copy.deepcopy(self.full)
        value["create_structure_if_needed"] = False
        self.assertFalse(validate_config(value)["create_structure_if_needed"])

    def test_per_table_databases_accept_matching_global_endpoints(self):
        value = copy.deepcopy(self.full)
        table = value["tables"][0]
        table["source_database"] = value["source"]["database"].lower()
        table["destination_database"] = value["bronze_destination"][
            "database"
        ].lower()
        table.pop("destination_table", None)

        normalized = validate_config(value)
        normalized_table = normalized["tables"][0]
        effective = effective_tables(normalized)[0]

        self.assertEqual(normalized_table["source_database"], "bd_origem")
        self.assertEqual(normalized_table["destination_database"], "bd_destino_01")
        self.assertEqual(normalized_table["destination_table"], "bd_origem_tabela_origem_01")
        self.assertEqual(effective["source"]["database"], "bd_origem")
        self.assertEqual(effective["destination"]["database"], "bd_destino_01")

    def test_automatic_destination_name_uses_source_database_and_table(self):
        value = copy.deepcopy(self.full)
        value["source"]["database"] = "LegacyDB"
        table = value["tables"][0]
        table["source_database"] = "legacydb"
        table["source_table"] = "BusinessEvent"
        table.pop("destination_table", None)

        normalized = validate_config(value)

        self.assertEqual(
            normalized["tables"][0]["destination_table"],
            "legacydb_businessevent",
        )

    def test_per_table_source_database_rejects_independent_routing(self):
        value = copy.deepcopy(self.full)
        value["tables"][0]["source_database"] = "OTHER_SOURCE"
        with self.assertRaisesRegex(ConfigError, "source_database.*source.database"):
            validate_config(value)

    def test_per_table_destination_database_rejects_independent_routing(self):
        value = copy.deepcopy(self.full)
        value["tables"][0]["destination_database"] = "OTHER_DESTINATION"
        with self.assertRaisesRegex(
            ConfigError, "destination_database.*bronze_destination.database"
        ):
            validate_config(value)

    def test_partition_column_is_optional_and_normalized(self):
        value = copy.deepcopy(self.full)
        value["tables"][0]["partition_column"] = "DH_CARGA"
        normalized = validate_config(value)
        self.assertEqual(normalized["tables"][0]["partition_column"], "dh_carga")
        self.assertEqual(effective_tables(normalized)[0]["partition_column"], "dh_carga")

        absent = copy.deepcopy(self.full)
        absent["tables"][0].pop("partition_column", None)
        self.assertIsNone(effective_tables(validate_config(absent))[0]["partition_column"])

    def test_import_requires_active_destination_and_sql_path(self):
        export_only = read_config(EXAMPLES / "config.export-only.json")
        export_only["execute_import"] = True
        with self.assertRaisesRegex(ConfigError, "exatamente um destino"):
            validate_config(export_only)

        without_path = copy.deepcopy(self.full)
        without_path.pop("destination_sql_directory")
        with self.assertRaisesRegex(ConfigError, "destination_sql_directory"):
            validate_config(without_path)

    def test_destination_schema_is_never_copied_from_source(self):
        value = copy.deepcopy(self.full)
        value["bronze_destination"].pop("schema")
        with self.assertRaisesRegex(ConfigError, "bronze_destination.schema"):
            validate_config(value)

    def test_control_state_directory_must_be_local(self):
        value = copy.deepcopy(self.full)
        value["local_control_directory"] = r"\\servidor\share\controle"
        with self.assertRaisesRegex(ConfigError, "disco local"):
            validate_config(value)

    def test_endpoint_roles_default_for_legacy_v2_and_helpers_are_explicit(self):
        raw = json.loads(
            (EXAMPLES / "config.full.json").read_text(encoding="utf-8")
        )
        for key in ("source", "bronze_destination", "landing_destination"):
            raw[key].pop("role", None)

        normalized = validate_config(raw)

        for key, expected in DEFAULT_ENDPOINT_ROLES.items():
            self.assertEqual(normalized[key]["role"], expected)
            self.assertEqual(endpoint_role(normalized, key), expected)
        self.assertTrue(role_includes_data("data_provider"))
        self.assertTrue(role_includes_data("structure_and_data"))
        self.assertTrue(role_includes_data("data_only"))
        self.assertFalse(role_includes_data("structure_only"))
        self.assertTrue(role_includes_structure("structure_and_data"))
        self.assertTrue(role_includes_structure("structure_only"))
        self.assertFalse(role_includes_structure("data_only"))

    def test_endpoint_roles_reject_unknown_or_inapplicable_values(self):
        invalid_source = copy.deepcopy(self.full)
        invalid_source["source"]["role"] = "structure_and_data"
        with self.assertRaisesRegex(ConfigError, r"source\.role"):
            validate_config(invalid_source)

        invalid_destination = copy.deepcopy(self.full)
        invalid_destination["bronze_destination"]["role"] = "data_provider"
        with self.assertRaisesRegex(ConfigError, r"bronze_destination\.role"):
            validate_config(invalid_destination)

    def test_import_requires_exactly_one_data_destination(self):
        none = copy.deepcopy(self.full)
        none["bronze_destination"]["role"] = "structure_only"
        with self.assertRaisesRegex(ConfigError, "exatamente um destino"):
            validate_config(none)

        two = copy.deepcopy(self.full)
        two["landing_destination"]["role"] = "data_only"
        with self.assertRaisesRegex(ConfigError, "no máximo um destino"):
            validate_config(two)

        export_only = copy.deepcopy(self.full)
        export_only["execute_import"] = False
        export_only["landing_destination"]["role"] = "data_only"
        with self.assertRaisesRegex(ConfigError, "no máximo um destino"):
            validate_config(export_only)

    def test_landing_can_be_the_single_data_destination(self):
        value = copy.deepcopy(self.full)
        value["bronze_destination"]["role"] = "structure_only"
        value["landing_destination"]["role"] = "structure_and_data"
        value["active_destination"] = "landing"
        for table in value["tables"]:
            table["destination_area"] = "landing"
            table["destination_database"] = value["landing_destination"]["database"]
            table["metadata_mapping"] = {
                "aud_ccid": {"constant": 0},
                "aud_cntrrn": {"constant": 0},
                "aud_enttyp": {"constant": "PT"},
            }

        normalized = validate_config(value)
        effective = effective_tables(normalized)

        self.assertEqual(normalized["active_destination"], "landing")
        self.assertEqual(data_destination_areas(normalized), ("landing",))
        self.assertTrue(all(row["destination"]["area"] == "landing" for row in effective))
        self.assertTrue(
            all(
                row["destination"]["database"]
                == normalized["landing_destination"]["database"]
                for row in effective
            )
        )

    def test_active_destination_is_derived_or_validated_from_roles(self):
        derived = copy.deepcopy(self.full)
        derived["bronze_destination"]["role"] = "structure_only"
        derived["landing_destination"]["role"] = "data_only"
        derived.pop("active_destination")
        for table in derived["tables"]:
            table["destination_area"] = "landing"
            table["destination_database"] = derived["landing_destination"]["database"]
            table["metadata_mapping"] = {
                "aud_ccid": {"constant": 0},
                "aud_cntrrn": {"constant": 0},
                "aud_enttyp": {"constant": "PT"},
            }
        self.assertEqual(validate_config(derived)["active_destination"], "landing")

        mismatch = copy.deepcopy(derived)
        mismatch["active_destination"] = "bronze"
        with self.assertRaisesRegex(ConfigError, "active_destination diverge"):
            validate_config(mismatch)

    def test_table_area_must_match_the_role_derived_data_destination(self):
        table_area = copy.deepcopy(self.full)
        table_area["tables"][0]["destination_area"] = "landing"
        with self.assertRaisesRegex(ConfigError, "active_destination"):
            validate_config(table_area)

        mapping = copy.deepcopy(self.full)
        mapping["tables"][0]["metadata_mapping"] = {
            "aud_ccid": {"constant": 0},
            "aud_cntrrn": {"constant": 0},
            "aud_enttyp": {"constant": "PT"},
        }
        with self.assertRaisesRegex(ConfigError, "metadata_mapping"):
            validate_config(mapping)


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.base = read_config(EXAMPLES / "config.auth-sql.json")

    def test_default_roles_preserve_pre_role_v2_fingerprints(self):
        legacy = copy.deepcopy(self.base)
        for key in ("source", "bronze_destination", "landing_destination"):
            legacy[key].pop("role", None)

        self.assertEqual(
            structural_fingerprint(legacy), structural_fingerprint(self.base)
        )
        self.assertEqual(
            operational_fingerprint(legacy), operational_fingerprint(self.base)
        )

    def test_operational_changes_do_not_change_structural_fingerprint(self):
        changed = copy.deepcopy(self.base)
        changed["execute_import"] = False
        changed["sql_timeout_seconds"] = 917
        changed["executor_directory"] = r"E:\outro\diretorio"
        changed["source"]["authentication"]["password"]["reference"] = "NOVA_REFERENCIA"
        self.assertEqual(
            structural_fingerprint(changed), structural_fingerprint(self.base)
        )
        self.assertNotEqual(
            operational_fingerprint(changed), operational_fingerprint(self.base)
        )
        self.assertEqual(set(fingerprints(self.base)), {"structural", "operational"})

    def test_control_schema_is_fixed_to_dbo(self):
        changed = copy.deepcopy(self.base)
        changed["control_schema"] = "another_control_schema"
        with self.assertRaisesRegex(ConfigError, "control_schema deve ser dbo"):
            validate_config(changed)

        uppercase = copy.deepcopy(self.base)
        uppercase["control_schema"] = "DBO"
        self.assertEqual(validate_config(uppercase)["control_schema"], "dbo")

    def test_artifact_reader_allowlist_is_operational_and_empty_is_backward_compatible(self):
        historical = copy.deepcopy(self.base)
        historical.pop("artifact_reader_sids", None)
        self.assertEqual(
            operational_fingerprint(historical), operational_fingerprint(self.base)
        )

        changed = copy.deepcopy(self.base)
        changed["artifact_reader_sids"] = ["S-1-5-80-123-456"]
        self.assertEqual(
            structural_fingerprint(changed), structural_fingerprint(self.base)
        )
        self.assertNotEqual(
            operational_fingerprint(changed), operational_fingerprint(self.base)
        )

        writer = copy.deepcopy(self.base)
        writer["artifact_writer_sids"] = ["S-1-5-21-1-2-3-2001"]
        self.assertEqual(
            structural_fingerprint(writer), structural_fingerprint(self.base)
        )
        self.assertNotEqual(
            operational_fingerprint(writer), operational_fingerprint(self.base)
        )

    def test_structural_changes_change_only_the_structural_identity(self):
        mutations = []

        block = copy.deepcopy(self.base)
        block["tables"][0]["rows_per_block"] = 12_345
        mutations.append(block)

        watermark = copy.deepcopy(self.base)
        watermark["tables"][0]["watermark"] = {
            "columns": [{"name": "id", "direction": "ASC"}]
        }
        mutations.append(watermark)

        profile = copy.deepcopy(self.base)
        profile["tables"][0]["structure_profile"] = "templates/bronze-v3.json"
        mutations.append(profile)

        source = copy.deepcopy(self.base)
        source["source"]["read_database"] = "OUTRA_ORIGEM"
        mutations.append(source)

        direct_limit = copy.deepcopy(self.base)
        direct_limit["keyless_direct_load_max_rows"] = 123
        mutations.append(direct_limit)

        expected = structural_fingerprint(self.base)
        for value in mutations:
            with self.subTest(value=value):
                self.assertNotEqual(structural_fingerprint(value), expected)

    def test_physical_destination_routing_is_operational_not_artifact_structure(self):
        changed = copy.deepcopy(self.base)
        changed["bronze_destination"]["instance"] = r"OUTRO\SQL"
        changed["bronze_destination"]["database"] = "OUTRA_BRONZE"
        changed["bronze_destination"]["schema"] = "outra_fonte"
        self.assertEqual(structural_fingerprint(changed), structural_fingerprint(self.base))
        self.assertNotEqual(operational_fingerprint(changed), operational_fingerprint(self.base))

    def test_cdc_activation_is_operational_not_artifact_structure(self):
        changed = copy.deepcopy(self.base)
        changed["tables"][0]["enable_cdc"] = not changed["tables"][0]["enable_cdc"]
        self.assertEqual(structural_fingerprint(changed), structural_fingerprint(self.base))
        self.assertNotEqual(operational_fingerprint(changed), operational_fingerprint(self.base))

    def test_cdc_retention_is_operational_not_artifact_structure(self):
        changed = copy.deepcopy(self.base)
        changed["cdc_retention_minutes"] = 525_600
        self.assertEqual(structural_fingerprint(changed), structural_fingerprint(self.base))
        self.assertNotEqual(
            operational_fingerprint(changed), operational_fingerprint(self.base)
        )

    def test_landing_endpoint_does_not_change_bronze_execution_fingerprints(self):
        changed = copy.deepcopy(self.base)
        changed["landing_destination"].update(
            {
                "instance": r"OUTRO-LANDING\SQL",
                "database": "OUTRA_LANDING",
                "schema": "outro_esquema",
                "structure_profile": "templates/landing-v3.json",
            }
        )
        changed["landing_destination"]["authentication"]["username"] = "outro_login"
        changed["landing_destination"]["authentication"]["password"][
            "reference"
        ] = "OUTRA_REFERENCIA_LANDING"
        self.assertEqual(structural_fingerprint(changed), structural_fingerprint(self.base))
        self.assertEqual(operational_fingerprint(changed), operational_fingerprint(self.base))


if __name__ == "__main__":
    unittest.main()
