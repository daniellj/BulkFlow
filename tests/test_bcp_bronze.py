import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import bcp_bronze as app
from bcp_engine.bcp import NS, make_format_file
from bcp_engine.config import effective_tables


def col(name, kind="int", length=4, precision=10, scale=0, nullable=False, collation=None):
    return {
        "name": name, "type_name": kind, "max_length": length,
        "precision": precision, "scale": scale, "is_nullable": nullable,
        "collation_name": collation, "is_identity": False, "is_computed": False,
    }


class CoreCompatibilityTests(unittest.TestCase):
    def test_identifier_and_literal_quoting(self):
        self.assertEqual(app.qi("a]b"), "[a]]b]")
        self.assertEqual(app.qs("d'água"), "N'd''água'")
        for value in ("", "x" * 129, "a\x00b"):
            with self.assertRaises(ValueError):
                app.qi(value)

    def test_native_type_declarations(self):
        self.assertEqual(app.type_sql(col("a", "nvarchar", -1)), "nvarchar(max)")
        self.assertEqual(app.type_sql(col("a", "nchar", 20)), "nchar(10)")
        self.assertEqual(app.type_sql(col("a", "decimal", 17, 38, 18)), "decimal(38,18)")
        self.assertEqual(app.type_sql(col("a", "datetime2", 8, 27, 7)), "datetime2(7)")
        self.assertEqual(app.type_sql(col("a", "timestamp", 8)), "binary(8)")
        with self.assertRaises(ValueError):
            app.type_sql(col("x", "geography"))

    def test_typed_bookmarks_preserve_precision(self):
        dt = app.Key(col("dt", "datetime2", 8, 27, 7))
        text = "2026-09-11T14:37:01.1234567"
        self.assertIn(text, app.key_literal(dt, text))
        self.assertIn("datetime2(7)", app.key_literal(dt, text))
        decimal = app.Key(col("n", "decimal", 17, 38, 18))
        value = "99999999999999999999.123456789012345678"
        self.assertIn(value, app.key_literal(decimal, value))
        binary = app.Key(col("b", "varbinary", 32))
        self.assertIn("N'0x00FF', 1", app.key_literal(binary, "0x00FF"))

    def test_collation_is_applied_before_varchar_conversion(self):
        key = app.Key(col("v", "varchar", 40, collation="Latin1_General_100_CI_AS"))
        self.assertEqual(
            app.key_literal(key, "á'b"),
            "CONVERT(varchar(40), N'á''b' COLLATE Latin1_General_100_CI_AS)",
        )

    def test_keyset_is_exhaustive_for_all_integer_directions(self):
        db = sqlite3.connect(":memory:")
        db.execute("create table t (a int,b int,c int)")
        rows = list(itertools.product(range(3), repeat=3))
        db.executemany("insert into t values (?,?,?)", rows)
        for directions in itertools.product([False, True], repeat=3):
            keys = [app.Key(col(name), direction) for name, direction in zip("abc", directions)]
            rank = lambda row: tuple(-value if direction else value for value, direction in zip(row, directions))
            ordered = sorted(rows, key=rank)
            lower = None
            recovered = []
            for start in range(0, len(rows), 4):
                page = ordered[start:start + 4]
                upper = list(map(str, page[-1]))
                predicate = f"({app.after(keys, lower)}) AND NOT ({app.after(keys, upper)})"
                predicate = re.sub(r"CONVERT\(int, N'([0-9]+)'\)", r"\1", predicate)
                selected = db.execute(
                    "select a,b,c from t where " + predicate + " order by " + app.order_by(keys)
                ).fetchall()
                self.assertEqual(selected, page)
                recovered.extend(selected)
                lower = upper
            self.assertEqual(recovered, ordered)
        db.close()

    def test_export_query_is_explicit_and_has_no_unsafe_pagination(self):
        column = col("cod")
        query = app.export_query("[dbo].[t]", [column], [app.Key(column)], ["10"], ["20"])
        self.assertIn("SELECT [cod]", query)
        for forbidden in ("OFFSET", "ROW_NUMBER", "NOLOCK", "SELECT *"):
            self.assertNotIn(forbidden, query)

    def test_bronze_wrapper_uses_exact_v2_contract(self):
        ddl = app.target_ddl("s344", "bd_origem_tabela_origem_01", [col("cod")])
        self.assertIn("[id_bd_origem_tabela_origem_01] BIGINT NOT NULL", ddl)
        self.assertNotIn("IDENTITY(1,1)", ddl)
        self.assertIn("CREATE SEQUENCE [s344].[seq_bd_origem_tabela_origem_01] AS BIGINT", ddl)
        self.assertIn("PRIMARY KEY CLUSTERED", ddl)
        for number in range(1, 6):
            self.assertIn(f"ix_bd_origem_tabela_origem_01_0{number}", ddl)

    def test_config_has_the_three_current_source_tables(self):
        config = app.read_config(ROOT / "config.example.json")
        self.assertEqual(config["config_version"], 2)
        tables = effective_tables(config)
        self.assertEqual(
            [item["source"]["table"] for item in tables],
            ["TABELA_ORIGEM_01", "TABELA_ORIGEM_02", "TABELA_ORIGEM_03"],
        )
        for item in tables:
            self.assertEqual(
                item["destination"]["table"], "bd_origem_" + item["source"]["table"].lower()
            )

    def test_hashes_are_deterministic(self):
        self.assertEqual(app.digest({"a": 1, "b": 2}), app.digest({"b": 2, "a": 1}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"abc")
            self.assertEqual(
                app.file_hash(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_format_file_lob_is_patched(self):
        xml = f'''<BCPFORMAT xmlns="{NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><RECORD><FIELD ID="1" xsi:type="NCharPrefix" PREFIX_LENGTH="8" MAX_LENGTH="8000"/></RECORD><ROW><COLUMN SOURCE="1" NAME="texto" xsi:type="SQLNVARCHAR"/></ROW></BCPFORMAT>'''
        operation = []

        class FakeInvocation:
            def with_operation(self, *args):
                operation.extend(args)
                return self

        class FakeRunner:
            def run(self, _invocation, _log, **_kwargs):
                path.write_text(xml, encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.xml"
            make_format_file(
                FakeRunner(), FakeInvocation(), "[dbo].[t]", [col("texto", "nvarchar", -1)],
                path, Path(tmp) / "test.log",
            )
            field = ET.parse(path).getroot().find("{" + NS + "}RECORD")[0]
            self.assertEqual(field.attrib["MAX_LENGTH"], "2147483647")
            self.assertEqual(operation[:3], ["[dbo].[t]", "format", os.devnull])


if __name__ == "__main__":
    unittest.main()
