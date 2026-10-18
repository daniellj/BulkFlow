from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.planner import TablePlan  # noqa: E402
from bcp_engine.models import ExecutionReport, TableResult  # noqa: E402
from bcp_engine.reporting import render_execution, render_plan_rows  # noqa: E402


class PlanRenderingTests(unittest.TestCase):
    def test_renderer_consumes_canonical_table_plan_contract(self):
        plan = TablePlan(
            source={
                "instance": "127.0.0.1,14333",
                "database": "BD_ORIGEM",
                "read_database": "BD_ORIGEM",
                "schema": "dbo",
                "table": "TABELA_ORIGEM_01",
            },
            destination={
                "instance": "127.0.0.1,14334",
                "database": "DBRO684",
                "schema": "s344",
                "table": "bd_origem_tabela_origem_01",
            },
            status="PENDING",
            batching_strategy="keyset_complete_tie_group_with_initial_ceiling",
            watermark=[{"name": "COLUNA_MARCA_DAGUA_01", "direction": "ASC"}],
            estimated_rows=18,
            row_count_method="sys.dm_db_partition_stats (aproximado)",
            estimated_bcp_total_bytes=4096,
            estimated_bcp_block_bytes=2048,
            approximate_blocks=1,
            planning_reserve_bytes=8192,
            predicted_peak_bytes=8192,
            executor_free_bytes=65536,
            source_database_used_bytes=32768,
            sampled_rows=18,
            sample_method="sample",
            sample_observed_at="2026-10-06T12:00:00+00:00",
            sample_uncertainty="estimativa",
        )

        rendered = render_plan_rows([asdict(plan)])

        self.assertIn("BD_ORIGEM.dbo.TABELA_ORIGEM_01", rendered)
        self.assertIn("DBRO684.s344.bd_origem_tabela_origem_01", rendered)
        self.assertIn("COLUNA_MARCA_DAGUA_01 ASC", rendered)
        self.assertIn("keyset_complete_tie_group_with_initial_ceiling", rendered)
        self.assertIn("4.00 KiB", rendered)
        self.assertIn("blocos aproximados: 1", rendered)
        self.assertIn("reserva calculada: 8.00 KiB", rendered)
        self.assertIn("fator de segurança: 1.25", rendered)
        self.assertIn("amostra: 18 linhas", rendered)
        self.assertNotIn("BCP total estimado: indisponível", rendered)

    def test_execution_summary_reports_total_rows_and_elapsed_time(self):
        report = ExecutionReport(
            execution_id="00000000-0000-4000-8000-000000000001",
            command="run",
            started_at="2026-10-18T10:00:00+00:00",
            finished_at="2026-10-18T10:02:03.456000+00:00",
            tables=[
                TableResult("a", "dst_a", "COMPLETED", rows_exported=100, rows_imported=100),
                TableResult("b", "dst_b", "COMPLETED", rows_exported=250, rows_imported=250),
            ],
        )

        rendered = render_execution(report)

        self.assertIn("Resumo final:", rendered)
        self.assertIn("linhas exportadas: 350", rendered)
        self.assertIn("linhas importadas: 350", rendered)
        self.assertIn("tempo total: 00:02:03.456", rendered)

    def test_cancelled_execution_explains_resume_with_same_uuid(self):
        execution_id = "00000000-0000-4000-8000-000000000002"
        report = ExecutionReport(
            execution_id=execution_id,
            command="run",
            started_at="2026-10-18T10:00:00+00:00",
            finished_at="2026-10-18T10:00:05+00:00",
            cancelled=True,
        )

        rendered = render_execution(report)

        self.assertEqual(report.exit_code(), 3)
        self.assertIn("cancelada de forma consistente", rendered)
        self.assertIn(execution_id, rendered)
        self.assertIn("retomar do ponto salvo", rendered)


if __name__ == "__main__":
    unittest.main()
