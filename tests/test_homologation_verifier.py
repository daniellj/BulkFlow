from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "docker" / "scripts" / "verify_loaded_data.py"
if MODULE_PATH.is_file():
    SPEC = importlib.util.spec_from_file_location(
        "_bulkflow_verify_loaded_data_tests",
        MODULE_PATH,
    )
    if SPEC is None or SPEC.loader is None:  # pragma: no cover - bootstrap failure
        raise RuntimeError(f"Nao foi possivel carregar {MODULE_PATH}")
    verifier = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = verifier
    SPEC.loader.exec_module(verifier)
else:  # The local Docker homologation laboratory is intentionally not versioned.
    verifier = None


def valid_catalog() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "type_name": "binary",
            "max_length": 10,
            "is_nullable": 0,
            "is_computed": 0,
        }
        for name in verifier.BRONZE_ZERO_EVENT_COLUMNS
    ]


def valid_metrics(*, total_rows: int = 18) -> dict[str, object]:
    zero_hex = "0x00000000000000000000"
    return {
        "total_rows": total_rows,
        "lsn_null_rows": 0,
        "sequence_null_rows": 0,
        "lsn_wrong_length_rows": 0,
        "sequence_wrong_length_rows": 0,
        "lsn_nonzero_rows": 0,
        "sequence_nonzero_rows": 0,
        "lsn_min_hex": zero_hex if total_rows else None,
        "lsn_max_hex": zero_hex if total_rows else None,
        "sequence_min_hex": zero_hex if total_rows else None,
        "sequence_max_hex": zero_hex if total_rows else None,
    }


@unittest.skipUnless(MODULE_PATH.is_file(), "laboratorio Docker local ausente")
class BronzeEventMetadataVerificationTests(unittest.TestCase):
    def test_proves_catalog_null_count_length_and_physical_zero(self):
        with patch.object(
            verifier,
            "query_rows",
            side_effect=[valid_catalog(), [valid_metrics()]],
        ) as query:
            verifier._verify_bronze_zero_event_metadata(
                connection=object(), schema="s344", table="bd_origem_tabela_origem_01"
            )

        catalog_sql = query.call_args_list[0].args[1]
        metrics_sql = query.call_args_list[1].args[1]
        self.assertIn("c.max_length", catalog_sql)
        self.assertIn("c.is_nullable", catalog_sql)
        self.assertIn("[bi_lsn_evento] IS NULL", metrics_sql)
        self.assertIn("DATALENGTH([bi_lsn_evento]) <> 10", metrics_sql)
        self.assertIn("[bi_lsn_evento] <> CONVERT(binary(10),0)", metrics_sql)
        self.assertIn("MIN(CONVERT(varchar(22),[bi_lsn_evento],1))", metrics_sql)

    def test_rejects_nullable_catalog_even_when_current_rows_are_zero(self):
        catalog = valid_catalog()
        catalog[0] = {**catalog[0], "is_nullable": 1}
        with patch.object(verifier, "query_rows", return_value=catalog):
            with self.assertRaisesRegex(RuntimeError, "Contrato fisico"):
                verifier._verify_bronze_zero_event_metadata(
                    connection=object(), schema="s344", table="bd_origem_tabela_origem_01"
                )

    def test_rejects_a_single_null_row(self):
        metrics = valid_metrics()
        metrics["lsn_null_rows"] = 1
        with patch.object(
            verifier,
            "query_rows",
            side_effect=[valid_catalog(), [metrics]],
        ):
            with self.assertRaisesRegex(RuntimeError, "lsn_null_rows"):
                verifier._verify_bronze_zero_event_metadata(
                    connection=object(), schema="s344", table="bd_origem_tabela_origem_01"
                )


@unittest.skipUnless(MODULE_PATH.is_file(), "laboratorio Docker local ausente")
class LoadTimestampVerificationTests(unittest.TestCase):
    def test_proves_brasilia_window_and_landing_default(self):
        timestamp_metrics = {
            "total_rows": 18,
            "min_load_time": "2026-10-07 16:14:23",
            "max_load_time": "2026-10-07 16:14:24",
            "null_rows": 0,
            "outside_execution_rows": 0,
            "future_rows": 0,
            "start_brasilia": "2026-10-07 16:13:21",
            "end_brasilia": "2026-10-07 16:17:24",
            "server_local_now": "2026-10-07 19:16:30",
            "server_utc_now": "2026-10-07 19:16:30",
            "brasilia_now": "2026-10-07 16:16:30",
        }
        landing_default = {
            "definition": (
                "(CONVERT([datetime2](7),((sysutcdatetime() AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'E. South America Standard Time')))"
            )
        }
        with patch.object(
            verifier,
            "query_rows",
            side_effect=[[timestamp_metrics], [landing_default]],
        ) as query:
            verifier._verify_bronze_load_timestamp(
                connection=object(),
                schema="s344",
                table="bd_origem_tabela_origem_01",
                control_schema="dbo",
                execution_id="3800e43b-de4c-4cfd-870c-aead96997ab4",
            )
            verifier._verify_landing_load_timestamp_default(
                connection=object(), schema="s344", table="bd_origem_tabela_origem_01"
            )

        timestamp_sql = query.call_args_list[0].args[1]
        self.assertIn("SYSUTCDATETIME() AT TIME ZONE 'UTC'", timestamp_sql)
        self.assertIn("outside_execution_rows", timestamp_sql)
        self.assertIn("future_rows", timestamp_sql)

    def test_rejects_future_bronze_load_time(self):
        metrics = {
            "total_rows": 18,
            "min_load_time": "2026-10-07 19:14:23",
            "max_load_time": "2026-10-07 19:14:24",
            "null_rows": 0,
            "outside_execution_rows": 18,
            "future_rows": 18,
            "start_brasilia": "2026-10-07 16:13:21",
            "end_brasilia": "2026-10-07 16:17:24",
            "server_local_now": "2026-10-07 19:16:30",
            "server_utc_now": "2026-10-07 19:16:30",
            "brasilia_now": "2026-10-07 16:16:30",
        }
        with patch.object(verifier, "query_rows", return_value=[metrics]):
            with self.assertRaisesRegex(RuntimeError, "horario de Brasilia"):
                verifier._verify_bronze_load_timestamp(
                    connection=object(),
                    schema="s344",
                    table="bd_origem_tabela_origem_01",
                    control_schema="dbo",
                    execution_id="3800e43b-de4c-4cfd-870c-aead96997ab4",
                )

    def test_rejects_landing_default_without_brasilia_conversion(self):
        with patch.object(
            verifier,
            "query_rows",
            return_value=[{"definition": "(sysdatetime())"}],
        ):
            with self.assertRaisesRegex(RuntimeError, "nao converte UTC"):
                verifier._verify_landing_load_timestamp_default(
                    connection=object(), schema="s344", table="bd_origem_tabela_origem_01"
                )


if __name__ == "__main__":
    unittest.main()
