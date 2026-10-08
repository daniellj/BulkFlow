import itertools
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.batching import (
    DirectSourceDriftError,
    after_predicate,
    at_or_before_predicate,
    bookmark_literal,
    candidate_limit_sql,
    capture_ceiling_sql,
    export_query,
    direct_keyless_export_query,
    direct_keyless_window,
    observe_export,
    observe_direct_keyless_export,
    order_by,
    plan_next_batch,
    range_predicate,
)
from bcp_engine.catalog import (
    DirectKeylessCountUnavailableError,
    DirectKeylessLimitExceededError,
    IndexCandidate,
    InvalidWatermarkError,
    NoEligibleWatermarkError,
    WARNING_NO_COMPATIBLE_INDEX,
    WARNING_DIRECT_KEYLESS,
    WatermarkColumn,
    WatermarkContainsNullError,
    WatermarkDataEvidence,
    WatermarkEmptyTableError,
    WatermarkIndexRequiredError,
    WatermarkNotUniqueError,
    discover_indexes,
    discover_watermark,
    explicit_watermark_data_evidence,
    exact_table_row_count,
    select_watermark,
)
from bcp_engine.estimates import (
    calculate_space_plan,
    estimate_row_count,
    estimate_row_size,
    metadata_row_count_sql,
    sample_row_size_sql,
)


def col(
    name,
    kind="int",
    length=4,
    precision=10,
    scale=0,
    nullable=False,
    collation=None,
):
    return {
        "name": name,
        "type_name": kind,
        "max_length": length,
        "precision": precision,
        "scale": scale,
        "is_nullable": nullable,
        "collation_name": collation,
    }


def key(name, descending=False, **changes):
    values = col(name, **changes)
    return WatermarkColumn.from_catalog(values, descending=descending)


def index(name, index_id, columns, *, pk=False, unique=False, **flags):
    return IndexCandidate(
        name=name,
        index_id=index_id,
        columns=tuple(columns),
        is_primary_key=pk,
        is_unique=unique,
        **flags,
    )


class QueueCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = []
        self._rows = []

    def execute(self, sql, params=()):
        self.connection.calls.append((sql, tuple(params)))
        if not self.connection.responses:
            raise AssertionError("Consulta inesperada: " + sql)
        response = self.connection.responses.pop(0)
        names = list(response[0]) if response else ["unused"]
        self.description = [(name,) for name in names]
        self._rows = [tuple(row[name] for name in names) for row in response]
        return self

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class QueueConnection:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def cursor(self):
        return QueueCursor(self)


class CatalogSelectionTests(unittest.TestCase):
    def test_explicit_watermark_precedes_pk_and_does_not_require_unique(self):
        columns = [col("id"), col("evento")]
        indexes = [index("pk_t", 1, [key("id")], pk=True, unique=True)]
        selected = select_watermark(
            columns,
            indexes,
            {"columns": [{"name": "evento", "direction": "ASC"}]},
        )
        self.assertEqual(selected.source, "explicit")
        self.assertEqual(selected.column_names, ("evento",))
        self.assertFalse(selected.columns[0].descending)
        self.assertIsNone(selected.index_name)
        self.assertEqual(selected.warnings, (WARNING_NO_COMPATIBLE_INDEX,))

    def test_explicit_watermark_rejects_descending_direction(self):
        with self.assertRaisesRegex(InvalidWatermarkError, "use sempre ASC"):
            select_watermark(
                [col("evento")],
                [],
                {"columns": [{"name": "evento", "direction": "DESC"}]},
            )

    def test_invalid_explicit_watermark_never_falls_back_to_pk(self):
        indexes = [index("pk_t", 1, [key("id")], pk=True, unique=True)]
        with self.assertRaises(InvalidWatermarkError):
            select_watermark(
                [col("id")],
                indexes,
                {"columns": [{"name": "missing", "direction": "ASC"}]},
            )

    def test_explicit_nonunique_compatible_index_is_accepted(self):
        columns = [col("evento"), col("desempate")]
        indexes = [
            index(
                "ix_evento",
                3,
                [key("evento", descending=True), key("desempate")],
                unique=False,
            )
        ]
        selected = select_watermark(
            columns,
            indexes,
            {"columns": [{"name": "evento", "direction": "ASC"}]},
            data_evidence=lambda _columns: WatermarkDataEvidence(True, False, False),
        )
        self.assertEqual(selected.index_name, "ix_evento")
        self.assertFalse(selected.columns[0].descending)
        self.assertTrue(selected.is_unique)  # unicidade comprovada nos dados atuais
        self.assertEqual(selected.warnings, ())

    def test_require_index_rejects_only_missing_compatible_index(self):
        with self.assertRaises(WatermarkIndexRequiredError):
            select_watermark(
                [col("evento")],
                [],
                {"columns": [{"name": "evento", "direction": "ASC"}]},
                require_index=True,
            )

    def test_primary_key_has_priority_over_clustered_unique(self):
        columns = [col("clustered_col"), col("pk_col")]
        indexes = [
            index("uq_clustered", 1, [key("clustered_col")], unique=True),
            index("pk_nonclustered", 2, [key("pk_col")], pk=True, unique=True),
        ]
        selected = select_watermark(columns, indexes, None)
        self.assertEqual(selected.source, "primary_key")
        self.assertEqual(selected.index_name, "pk_nonclustered")
        self.assertEqual(selected.column_names, ("pk_col",))

    def test_automatic_watermark_is_ascending_even_for_descending_index(self):
        columns = [col("id")]
        indexes = [
            index("pk_t", 1, [key("id", descending=True)], pk=True, unique=True)
        ]
        selected = select_watermark(columns, indexes, None)
        self.assertFalse(selected.columns[0].descending)

    def test_unique_fallback_ignores_filtered_disabled_and_hypothetical(self):
        columns = [col("a"), col("b"), col("c"), col("d")]
        indexes = [
            index("u_filter", 1, [key("a")], unique=True, has_filter=True),
            index("u_disabled", 2, [key("b")], unique=True, is_disabled=True),
            index("u_hypo", 3, [key("c")], unique=True, is_hypothetical=True),
            index("u_ok", 4, [key("d")], unique=True),
        ]
        selected = select_watermark(columns, indexes, None)
        self.assertEqual(selected.source, "unique")
        self.assertEqual(selected.index_name, "u_ok")

    def test_nullable_explicit_is_checked_against_data(self):
        with self.assertRaises(WatermarkContainsNullError):
            select_watermark(
                [col("evento", nullable=True)],
                [],
                {"columns": [{"name": "evento", "direction": "ASC"}]},
                data_evidence=lambda _columns: WatermarkDataEvidence(True, True, False),
            )

    def test_keyless_explicit_watermark_requires_positive_data_evidence(self):
        explicit = {"columns": [{"name": "evento", "direction": "ASC"}]}

        with self.assertRaisesRegex(InvalidWatermarkError, "exige prova"):
            select_watermark([col("evento")], [], explicit)

        selected = select_watermark(
            [col("evento")],
            [],
            explicit,
            data_evidence=lambda _columns: WatermarkDataEvidence(True, False, False),
        )
        self.assertEqual(selected.source, "explicit")
        self.assertEqual(selected.column_names, ("evento",))
        self.assertTrue(selected.is_unique)
        self.assertEqual(selected.warnings, (WARNING_NO_COMPATIBLE_INDEX,))

    def test_keyless_explicit_watermark_rejects_empty_and_duplicates(self):
        explicit = {"columns": [{"name": "evento", "direction": "ASC"}]}
        with self.assertRaises(WatermarkEmptyTableError):
            select_watermark(
                [col("evento")],
                [],
                explicit,
                data_evidence=lambda _columns: WatermarkDataEvidence(False, False, False),
            )
        with self.assertRaises(WatermarkNotUniqueError):
            select_watermark(
                [col("evento")],
                [],
                explicit,
                data_evidence=lambda _columns: WatermarkDataEvidence(True, False, True),
            )

    def test_pk_or_unique_is_selected_only_from_metadata_without_data_discovery(self):
        columns = [col("pk_col")]
        indexes = [index("pk_t", 1, [key("pk_col")], pk=True, unique=True)]
        selected = select_watermark(
            columns,
            indexes,
            None,
            data_evidence=lambda _columns: (_ for _ in ()).throw(
                AssertionError("nao deve procurar marca nos dados")
            ),
        )
        self.assertEqual(selected.source, "primary_key")

    def test_explicit_mark_on_table_with_structural_key_does_not_need_uniqueness_scan(self):
        columns = [col("id"), col("evento")]
        indexes = [index("uq_id", 1, [key("id")], unique=True)]
        selected = select_watermark(
            columns,
            indexes,
            {"columns": [{"name": "evento", "direction": "ASC"}]},
            data_evidence=lambda _columns: (_ for _ in ()).throw(
                AssertionError("tabela ja possui UNIQUE elegivel")
            ),
        )
        self.assertEqual(selected.source, "explicit")
        self.assertFalse(selected.is_unique)

    def test_explicit_data_evidence_uses_only_configured_compound_columns(self):
        connection = QueueConnection(
            [{"has_rows": True, "has_nulls": False, "has_duplicates": False}]
        )
        evidence = explicit_watermark_data_evidence(
            connection,
            "source",
            "sem_chave",
            [key("codigo"), key("tipo", kind="varchar", length=20, nullable=True)],
        )
        self.assertEqual(evidence, WatermarkDataEvidence(True, False, False))
        sql, params = connection.calls[0]
        self.assertEqual(params, ())
        self.assertIn("FROM [source].[sem_chave]", sql)
        self.assertIn("GROUP BY [codigo], [tipo]", sql)
        self.assertIn("[tipo] IS NULL", sql)
        self.assertNotIn("sys.columns", sql)

    def test_discover_watermark_wires_data_proof_for_keyless_table(self):
        connection = QueueConnection(
            [{"has_rows": True, "has_nulls": False, "has_duplicates": False}]
        )
        selected = discover_watermark(
            connection,
            "dbo",
            "eventos",
            {"columns": [{"name": "codigo", "direction": "ASC"}]},
            columns=[col("codigo")],
            indexes=[],
        )
        self.assertEqual(selected.column_names, ("codigo",))
        self.assertTrue(selected.is_unique)
        self.assertEqual(len(connection.calls), 1)

    def test_auto_skips_candidate_with_null_and_tries_next_unique(self):
        columns = [col("a", nullable=True), col("b")]
        indexes = [
            index("u_a", 1, [key("a", nullable=True)], unique=True),
            index("u_b", 2, [key("b")], unique=True),
        ]
        selected = select_watermark(
            columns,
            indexes,
            None,
            has_nulls=lambda selected: selected[0].name == "a",
        )
        self.assertEqual(selected.index_name, "u_b")

    def test_nullable_unique_without_data_probe_fails_closed(self):
        nullable = col("codigo", nullable=True)
        with self.assertRaisesRegex(NoEligibleWatermarkError, "continham NULL"):
            select_watermark(
                [nullable],
                [index("uq_codigo", 1, [key("codigo", nullable=True)], unique=True)],
                None,
            )

    def test_no_eligible_key_is_explicit_diagnostic(self):
        with self.assertRaises(NoEligibleWatermarkError):
            select_watermark([col("payload", "xml")], [], None)

    def test_keyless_table_uses_metadata_count_and_distinct_direct_mode(self):
        connection = QueueConnection([{"row_count": 4321}])
        selected = discover_watermark(
            connection,
            "dbo",
            "sem_chave",
            None,
            columns=[col("payload", "xml")],
            indexes=[],
            direct_keyless_row_limit=5_000_000,
        )
        self.assertTrue(selected.is_direct_keyless)
        self.assertEqual(selected.columns, ())
        self.assertEqual(selected.captured_row_count, 4321)
        self.assertIn(WARNING_DIRECT_KEYLESS, selected.warnings)
        sql, params = connection.calls[0]
        self.assertEqual(params, ("dbo", "sem_chave"))
        self.assertNotIn("COUNT_BIG", sql.upper())
        self.assertIn("sys.partitions", sql)
        self.assertNotIn("dm_db_partition_stats", sql)

    def test_explicit_duplicate_or_null_may_fall_back_but_bad_column_never_does(self):
        explicit = {"columns": [{"name": "evento", "direction": "ASC"}]}
        duplicated = QueueConnection(
            [{"has_rows": True, "has_nulls": False, "has_duplicates": True}],
            [{"row_count": 20}],
        )
        selected = discover_watermark(
            duplicated,
            "dbo",
            "eventos",
            explicit,
            columns=[col("evento")],
            indexes=[],
            direct_keyless_row_limit=100,
        )
        self.assertTrue(selected.is_direct_keyless)
        self.assertIn("duplicadas", selected.direct_reason)

        invalid = QueueConnection([{"row_count": 1}])
        with self.assertRaises(InvalidWatermarkError):
            discover_watermark(
                invalid,
                "dbo",
                "eventos",
                {"columns": [{"name": "inexistente", "direction": "ASC"}]},
                columns=[col("evento")],
                indexes=[],
                direct_keyless_row_limit=100,
            )
        self.assertEqual(invalid.calls, [])

    def test_empty_unproven_explicit_mark_becomes_empty_direct_load(self):
        connection = QueueConnection(
            [{"has_rows": False, "has_nulls": False, "has_duplicates": False}],
            [{"row_count": 0}],
        )
        selected = discover_watermark(
            connection,
            "dbo",
            "vazia",
            {"columns": [{"name": "codigo", "direction": "ASC"}]},
            columns=[col("codigo")],
            indexes=[],
            direct_keyless_row_limit=5_000_000,
        )
        self.assertTrue(selected.is_direct_keyless)
        self.assertEqual(selected.captured_row_count, 0)

    def test_direct_keyless_fails_closed_for_limit_count_failure_or_disabled_mode(self):
        over = QueueConnection([{"row_count": 101}])
        with self.assertRaisesRegex(DirectKeylessLimitExceededError, "101.*100"):
            discover_watermark(
                over,
                "dbo",
                "t",
                None,
                columns=[col("payload", "xml")],
                indexes=[],
                direct_keyless_row_limit=100,
            )

        unavailable = QueueConnection()
        with self.assertRaises(DirectKeylessCountUnavailableError):
            discover_watermark(
                unavailable,
                "dbo",
                "t",
                None,
                columns=[col("payload", "xml")],
                indexes=[],
                direct_keyless_row_limit=100,
            )

        disabled = QueueConnection([{"row_count": 1}])
        with self.assertRaises(NoEligibleWatermarkError):
            discover_watermark(
                disabled,
                "dbo",
                "t",
                None,
                columns=[col("payload", "xml")],
                indexes=[],
                direct_keyless_row_limit=0,
            )
        self.assertEqual(disabled.calls, [])

    def test_direct_resume_reuses_durable_metadata_estimate_without_recounting_source(self):
        connection = QueueConnection()
        selected = discover_watermark(
            connection,
            "dbo",
            "t",
            None,
            columns=[col("payload", "xml")],
            indexes=[],
            direct_keyless_row_limit=5_000_000,
            direct_keyless_captured_row_count=123,
        )
        self.assertTrue(selected.is_direct_keyless)
        self.assertEqual(selected.captured_row_count, 123)
        self.assertEqual(connection.calls, [])
        self.assertIn("ROW_COUNT_REUSED_FROM_DURABLE_CONTROL", selected.warnings)

        with self.assertRaises(DirectKeylessLimitExceededError):
            discover_watermark(
                connection,
                "dbo",
                "t",
                None,
                columns=[col("payload", "xml")],
                indexes=[],
                direct_keyless_row_limit=100,
                direct_keyless_captured_row_count=123,
            )

    def test_metadata_count_rejects_missing_scalar(self):
        connection = QueueConnection([])
        with self.assertRaises(DirectKeylessCountUnavailableError):
            exact_table_row_count(connection, "dbo", "t")

    def test_explicit_lob_type_is_not_comparable(self):
        with self.assertRaises(InvalidWatermarkError):
            select_watermark(
                [col("payload", "nvarchar", length=-1)],
                [],
                {"columns": [{"name": "payload", "direction": "ASC"}]},
            )

    def test_catalog_query_materializes_index_columns_and_pk_priority(self):
        def row(index_id, name, column_name, *, pk=False, unique=True, direction=False):
            return {
                "index_id": index_id,
                "index_name": name,
                "is_primary_key": pk,
                "is_unique": unique,
                "has_filter": False,
                "is_disabled": False,
                "is_hypothetical": False,
                "key_ordinal": 1,
                "is_descending_key": direction,
                "column_name": column_name,
                "type_name": "int",
                "max_length": 4,
                "precision": 10,
                "scale": 0,
                "collation_name": None,
                "is_nullable": False,
            }

        connection = QueueConnection(
            [row(1, "uq_clustered", "a"), row(2, "pk_t", "b", pk=True, direction=True)]
        )
        indexes = discover_indexes(connection, "dbo", "t")
        self.assertEqual([item.name for item in indexes], ["pk_t", "uq_clustered"])
        self.assertTrue(indexes[0].columns[0].descending)
        self.assertIn("i.is_primary_key = 1", connection.calls[0][0])


class BatchingSqlTests(unittest.TestCase):
    @staticmethod
    def _sqlite_predicate(sql):
        return re.sub(r"CONVERT\(int, N'(-?[0-9]+)'\)", r"\1", sql)

    def test_bookmarks_preserve_datetime_decimal_binary_and_collation(self):
        dt = key("dt", kind="datetime2", length=8, precision=27, scale=7)
        decimal_key = key("n", kind="decimal", length=17, precision=38, scale=18)
        binary_key = key("b", kind="varbinary", length=32)
        text_key = key(
            "v",
            kind="varchar",
            length=40,
            collation="Latin1_General_100_CI_AS",
        )
        self.assertIn("2026-09-11T14:37:01.1234567", bookmark_literal(dt, "2026-09-11T14:37:01.1234567"))
        self.assertIn("datetime2(7)", bookmark_literal(dt, "2026-09-11T14:37:01.1234567"))
        self.assertIn("99999999999999999999.123456789012345678", bookmark_literal(decimal_key, "99999999999999999999.123456789012345678"))
        self.assertIn("N'0x00FF', 1", bookmark_literal(binary_key, "0x00FF"))
        self.assertIn("COLLATE Latin1_General_100_CI_AS", bookmark_literal(text_key, "agua"))

    def test_lexicographic_range_is_exhaustive_for_mixed_directions(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE t (a int, b int, c int)")
        rows = list(itertools.product(range(3), repeat=3))
        database.executemany("INSERT INTO t VALUES (?,?,?)", rows)
        try:
            for directions in itertools.product([False, True], repeat=3):
                columns = [key(name, descending=direction) for name, direction in zip("abc", directions)]

                def rank(row):
                    return tuple(-value if direction else value for value, direction in zip(row, directions))

                ordered = sorted(rows, key=rank)
                lower = tuple(map(str, ordered[4]))
                upper = tuple(map(str, ordered[16]))
                ceiling = tuple(map(str, ordered[-1]))
                predicate = self._sqlite_predicate(range_predicate(columns, lower, upper, ceiling))
                selected = database.execute(
                    "SELECT a,b,c FROM t WHERE " + predicate + " ORDER BY " + order_by(columns)
                ).fetchall()
                self.assertEqual(selected, ordered[5:17])
        finally:
            database.close()

    def test_lower_is_exclusive_and_upper_is_inclusive(self):
        columns = [key("id")]
        self.assertIn("[id] > CONVERT(int, N'10')", after_predicate(columns, ("10",)))
        inclusive = at_or_before_predicate(columns, ("20",))
        self.assertIn("[id] < CONVERT(int, N'20')", inclusive)
        self.assertIn("[id] = CONVERT(int, N'20')", inclusive)

    def test_ceiling_uses_existing_tuple_in_reverse_order_not_independent_max(self):
        columns = [key("a"), key("b", descending=True)]
        sql = capture_ceiling_sql("dbo", "events", columns)
        self.assertIn("TOP (1)", sql)
        self.assertIn("ORDER BY [a] DESC, [b] ASC", sql)
        self.assertNotIn("MAX(", sql.upper())

    def test_candidate_limit_and_export_query_have_no_ordinal_pagination(self):
        columns = [key("a"), key("b", descending=True)]
        candidate = candidate_limit_sql(
            "dbo", "events", columns, ("1", "9"), ("9", "1"), 250000
        )
        exported = export_query(
            "dbo",
            "events",
            ["a", "b", "payload"],
            columns,
            ("1", "9"),
            ("4", "5"),
            ("9", "1"),
        )
        combined = (candidate + exported).upper()
        self.assertIn("TOP (250000)", candidate)
        self.assertIn("WITH CANDIDATE", combined)
        for forbidden in ("OFFSET", "FETCH NEXT", "ROW_NUMBER", "%%PHYSLOC%%", "NOLOCK"):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn("SELECT *", combined)

    def test_direct_keyless_is_one_unordered_locked_block_and_detects_drift(self):
        query = direct_keyless_export_query(
            "dbo", "sem_chave", ["codigo", "payload"]
        )
        upper = query.upper()
        self.assertIn("[DBO].[SEM_CHAVE] WITH (HOLDLOCK, TABLOCK)", upper)
        self.assertNotIn("ORDER BY", upper)
        for forbidden in ("TOP", "OFFSET", "ROW_NUMBER", "%%PHYSLOC%%", "SELECT *"):
            self.assertNotIn(forbidden, upper)

        window = direct_keyless_window(42)
        observation = observe_direct_keyless_export(window, 42, 900)
        self.assertEqual(observation.actual_rows, 42)
        self.assertEqual(observation.checkpoint, ())
        with self.assertRaisesRegex(DirectSourceDriftError, "KEYLESS_DIRECT_LOAD_SOURCE_DRIFT"):
            observe_direct_keyless_export(window, 41, 850)

        self.assertEqual(
            observe_direct_keyless_export(
                window, 41, 850, maximum_rows=5_000_000
            ).actual_rows,
            41,
        )
        with self.assertRaisesRegex(DirectSourceDriftError, "OVER_LIMIT"):
            observe_direct_keyless_export(window, 101, 900, maximum_rows=100)

        empty = direct_keyless_window(0)
        empty_observation = observe_direct_keyless_export(empty, 0, 0)
        self.assertEqual(empty_observation.status, "EMPTY_RANGE_CONFIRMED")
        self.assertFalse(empty_observation.import_required)

    def test_nonunique_boundary_exports_the_complete_tie_group(self):
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE t (marca int, payload text)")
        database.executemany(
            "INSERT INTO t VALUES (?,?)",
            [(1, "a"), (2, "b"), (2, "c"), (2, "d"), (3, "e")],
        )
        sql = export_query(
            "dbo", "t", ["marca", "payload"], [key("marca")], None, ("2",), ("3",)
        )
        sql = sql.replace("[dbo].[t]", "t")
        sql = self._sqlite_predicate(sql)
        try:
            rows = database.execute(sql).fetchall()
            self.assertEqual(sorted(rows), [(1, "a"), (2, "b"), (2, "c"), (2, "d")])
            self.assertGreater(len(rows), 2)  # alvo candidato podia ser 2; empate nao foi cortado
        finally:
            database.close()

    def test_plan_next_batch_records_expanded_group_counts(self):
        connection = QueueConnection(
            [{"bookmark_0": "2"}],
            [{"row_count": 4}],
            [{"row_count": 3}],
        )
        window = plan_next_batch(
            connection, "dbo", "t", [key("marca")], None, ("9",), 2
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.upper, ("2",))
        self.assertEqual(window.observed_rows, 4)
        self.assertEqual(window.boundary_group_rows, 3)
        self.assertEqual(window.target_rows, 2)

    def test_empty_export_advances_traceable_checkpoint_without_import(self):
        connection = QueueConnection(
            [{"bookmark_0": "20"}],
            [{"row_count": 10}],
            [{"row_count": 1}],
        )
        window = plan_next_batch(
            connection, "dbo", "t", [key("id")], ("10",), ("100",), 10
        )
        observation = observe_export(window, actual_rows=0, actual_bytes=0)
        self.assertEqual(observation.status, "EMPTY_RANGE_CONFIRMED")
        self.assertEqual(observation.checkpoint, ("20",))
        self.assertFalse(observation.import_required)


class EstimateTests(unittest.TestCase):
    def test_metadata_count_only_uses_heap_or_clustered(self):
        sql = metadata_row_count_sql().replace(" ", "").replace("\n", "").lower()
        self.assertIn("index_idin(0,1)", sql)
        self.assertIn("sys.partitions", sql)
        self.assertNotIn("dm_db_partition_stats", sql)

    def test_logical_counts_above_int_are_not_truncated(self):
        connection = QueueConnection([{"estimated_rows": 3_500_000_123}])
        estimate = estimate_row_count(connection, "dbo", "large_table")
        self.assertEqual(estimate.value, 3_500_000_123)
        self.assertTrue(estimate.approximate)
        self.assertIn("index_id IN (0, 1)", connection.calls[0][0])
        self.assertIn("sys.partitions", connection.calls[0][0])

    def test_unavailable_count_is_none_not_zero(self):
        connection = QueueConnection()
        estimate = estimate_row_count(connection, "dbo", "denied")
        self.assertIsNone(estimate.value)
        self.assertEqual(estimate.uncertainty, "indisponivel")
        self.assertIsNotNone(estimate.error)

    def test_invalid_count_method_is_configuration_error(self):
        for method in ("inventado", "count_big"):
            with self.subTest(method=method), self.assertRaisesRegex(
                ValueError, "count_method deve ser metadata"
            ):
                estimate_row_count(QueueConnection(), "dbo", "t", method=method)

    def test_row_sample_is_top_limited_and_uses_datalength(self):
        sql = sample_row_size_sql("dbo", "t", ["a", "texto"], 10000)
        self.assertIn("TOP (10000)", sql)
        self.assertIn("DATALENGTH([a])", sql)
        self.assertIn("DATALENGTH([texto])", sql)
        self.assertNotIn("COUNT_BIG", sql.upper())
        self.assertNotIn("NEWID", sql.upper())
        self.assertNotIn("RAND", sql.upper())

    def test_row_sample_reports_method_limit_and_uncertainty(self):
        connection = QueueConnection(
            [
                {
                    "sampled_rows": 7,
                    "average_bytes": Decimal("123.500000"),
                    "minimum_bytes": 20,
                    "maximum_bytes": 400,
                }
            ]
        )
        estimate = estimate_row_size(connection, "dbo", "t", ["a"], 100)
        self.assertEqual(estimate.sampled_rows, 7)
        self.assertEqual(estimate.sample_limit, 100)
        self.assertEqual(estimate.average_bytes, Decimal("123.500000"))
        self.assertIn("DATALENGTH", estimate.method)
        self.assertIn("nao e garantia", estimate.uncertainty)

    def test_space_peak_differs_between_export_only_and_streamed_import(self):
        common = dict(
            estimated_rows=1000,
            average_row_bytes=Decimal("10"),
            rows_per_block=100,
            safety_factor=Decimal("1.30"),
            retained_bytes=50,
            pending_retry_blocks=1,
        )
        export_only = calculate_space_plan(
            **common, execute_import=False, delete_confirmed=True
        )
        streamed = calculate_space_plan(
            **common, execute_import=True, delete_confirmed=True
        )
        self.assertEqual(export_only.estimated_bcp_total_bytes, 10000)
        self.assertEqual(export_only.planning_reserve_bytes, 13000)
        self.assertEqual(export_only.predicted_peak_bytes, 13050)
        self.assertEqual(streamed.estimated_bcp_block_bytes, 1000)
        self.assertEqual(streamed.predicted_peak_bytes, 2650)
        self.assertLess(streamed.predicted_peak_bytes, export_only.predicted_peak_bytes)
        self.assertIsNone(streamed.destination_data_bytes)
        self.assertIsNone(streamed.destination_log_bytes)

    def test_unknown_inputs_propagate_none_and_capacity_is_unknown(self):
        result = calculate_space_plan(
            None,
            None,
            rows_per_block=250000,
            safety_factor="1.30",
            execute_import=False,
            delete_confirmed=False,
            available_bytes=10_000,
        )
        self.assertIsNone(result.estimated_bcp_total_bytes)
        self.assertIsNone(result.predicted_peak_bytes)
        self.assertIsNone(result.capacity_ok)

    def test_empty_table_has_zero_volume_even_without_row_sample(self):
        result = calculate_space_plan(
            0,
            None,
            rows_per_block=250000,
            safety_factor="1.30",
            execute_import=False,
            delete_confirmed=False,
            retained_bytes=123,
            available_bytes=1000,
        )
        self.assertEqual(result.estimated_bcp_total_bytes, 0)
        self.assertEqual(result.predicted_peak_bytes, 123)
        self.assertTrue(result.capacity_ok)


if __name__ == "__main__":
    unittest.main()
