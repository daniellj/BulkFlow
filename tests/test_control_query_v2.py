from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ControlQueryV2Tests(unittest.TestCase):
    def test_operator_query_uses_fixed_dbro684_dbo_control_contract(self):
        sql = (ROOT / "query_control.sql").read_text(encoding="utf-8")
        self.assertFalse((ROOT / "consultar_controle.sql").exists())

        self.assertIn("USE [DBRO684];", sql)
        for object_name in (
            "versao_esquema",
            "execucao",
            "execucao_tabela",
            "execucao_lote",
        ):
            self.assertIn(f"OBJECT_ID(N'dbo.{object_name}'", sql)
        for object_name in ("execucao", "execucao_tabela", "execucao_lote"):
            self.assertIn(f"[dbo].[{object_name}]", sql)
        self.assertNotIn("@ControlSchema", sql)
        self.assertNotIn("QUOTENAME", sql)
        self.assertNotIn("sp_executesql", sql)
        self.assertNotIn("[controle_transferencia]", sql)

        installer = (ROOT / "packaging" / "wix" / "Product.wxs").read_text(
            encoding="utf-8"
        )
        self.assertIn('Source="$(var.ProjectRoot)\\query_control.sql"', installer)
        self.assertIn('<ComponentRef Id="ControlQueryComponent" />', installer)

        for real_column in (
            "last_imported_block",
            "import_cursor_json",
            "final_limit_json",
            "manifest_rows",
            "imported_rows",
            "commit_recorded_at",
        ):
            self.assertIn(real_column, sql)

        for obsolete_name in (
            "controle_bcp",
            "bcp_execucao",
            "bcp_tabela",
            "bcp_bloco",
            "ultimo_bloco",
            "linhas_importadas",
            "dh_commit_registrado",
        ):
            self.assertNotIn(obsolete_name, sql)


if __name__ == "__main__":
    unittest.main()
