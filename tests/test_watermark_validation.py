from __future__ import annotations

import json
import unittest

from bcp_engine.watermark_validation import (
    WATERMARK_VALIDATION_GUIDANCE_PT_BR,
    normalize_watermark_columns,
    render_watermark_validation_sql,
    watermark_validation_filename,
)


class WatermarkValidationScriptTests(unittest.TestCase):
    def test_renders_materialized_ascending_only_script(self) -> None:
        sql = render_watermark_validation_sql(
            source_database="BD_ORIGEM",
            source_schema="dbo",
            source_table="TABELA_ORIGEM_04",
            columns=["COLUNA_MARCA_DAGUA_01", "COLUNA_MARCA_DAGUA_02"],
        )

        self.assertIn("DECLARE @SourceDatabase       sysname       = N'BD_ORIGEM';", sql)
        self.assertIn("DECLARE @SourceSchema         sysname       = N'dbo';", sql)
        self.assertIn("DECLARE @SourceTable          sysname       = N'TABELA_ORIGEM_04';", sql)
        self.assertIn(
            "DECLARE @WatermarkColumnsJson nvarchar(max) = "
            + "N'"
            + json.dumps(["COLUNA_MARCA_DAGUA_01", "COLUNA_MARCA_DAGUA_02"], separators=(",", ":"))
            + "';",
            sql,
        )
        self.assertNotIn("__SOURCE_", sql)
        self.assertNotIn("__WATERMARK_", sql)
        self.assertNotIn(":DESC", sql.upper())
        self.assertIn("marca_dagua_aceitavel", sql)
        self.assertIn("possui_pk_elegivel", sql)
        self.assertIn("possui_unique_elegivel", sql)
        self.assertIn("SET LOCK_TIMEOUT 15000", sql)
        self.assertNotIn("NOLOCK)", sql.upper())
        self.assertLess(
            sql.index("USE ' + QUOTENAME(@SourceDatabase)"),
            sql.index("CREATE TABLE #configured_column"),
        )
        self.assertIn(
            "column_name    nvarchar(4000) COLLATE DATABASE_DEFAULT NULL", sql
        )
        self.assertEqual(sql.count("DROP TABLE IF EXISTS #configured_column"), 2)

    def test_escapes_literals_while_dynamic_identifiers_use_quotename(self) -> None:
        sql = render_watermark_validation_sql(
            source_database="Base'Dados",
            source_schema="esq]uema",
            source_table="tab'ela]",
            columns=["col'una]", "outra"],
        )

        self.assertIn("N'Base''Dados'", sql)
        self.assertIn("N'esq]uema'", sql)
        self.assertIn("N'tab''ela]'", sql)
        self.assertIn("col''una]", sql)
        self.assertIn("QUOTENAME(@SourceDatabase)", sql)
        self.assertIn("QUOTENAME(@pSchema)", sql)
        self.assertIn("QUOTENAME(@pTable)", sql)

    def test_rejects_empty_duplicate_and_oversized_columns(self) -> None:
        with self.assertRaisesRegex(ValueError, "ao menos uma"):
            normalize_watermark_columns([])
        with self.assertRaisesRegex(ValueError, "repete"):
            normalize_watermark_columns(["Codigo", "codigo"])
        with self.assertRaisesRegex(ValueError, "128"):
            normalize_watermark_columns(["x" * 129])

    def test_rejects_control_characters_in_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "controle"):
            render_watermark_validation_sql(
                source_database="BD_ORIGEM",
                source_schema="dbo",
                source_table="bad\nname",
                columns=["id"],
            )

    def test_suggested_filename_is_portable(self) -> None:
        filename = watermark_validation_filename("BD_ORIGEM", "s 344", "TABELA/ORIGEM:04")
        self.assertEqual(
            filename, "validar_marca_dagua_BD_ORIGEM_s_344_TABELA_ORIGEM_04.sql"
        )

    def test_guidance_explains_validation_scope_and_scan_cost(self) -> None:
        self.assertIn("não tenta\n   descobrir", WATERMARK_VALIDATION_GUIDANCE_PT_BR)
        self.assertIn("COUNT_BIG/GROUP BY", WATERMARK_VALIDATION_GUIDANCE_PT_BR)
        self.assertIn("somente leitura", WATERMARK_VALIDATION_GUIDANCE_PT_BR)
        self.assertIn("S-locks", WATERMARK_VALIDATION_GUIDANCE_PT_BR)
        self.assertIn("bloquear gravações", WATERMARK_VALIDATION_GUIDANCE_PT_BR)


if __name__ == "__main__":
    unittest.main()
