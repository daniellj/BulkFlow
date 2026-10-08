from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bcp_engine.catalog import (  # noqa: E402
    IndexCandidate,
    InvalidWatermarkError,
    NoEligibleWatermarkError,
    WatermarkColumn,
    WatermarkContainsNullError,
    WatermarkIndexRequiredError,
    WatermarkSelection,
)
from bcp_engine.ddl import (  # noqa: E402
    CatalogComparison,
    CatalogState,
    compare_catalog,
    partition_boundary_values,
)
from bcp_engine.estimates import (  # noqa: E402
    METADATA_ROW_COUNT_METHOD,
    SAMPLE_METHOD,
    SAMPLE_UNCERTAINTY,
    RowCountEstimate,
    RowSizeEstimate,
)
from bcp_engine.inspection import inspect_destination_catalog  # noqa: E402
from bcp_engine.models import TableStatus  # noqa: E402
from bcp_engine.planner import (  # noqa: E402
    SourceDatabaseStorage,
    build_plan,
    inspect_destination_space,
    inspect_source_database_storage,
)
from bcp_engine.profiles import (  # noqa: E402
    ColumnDefinition,
    IndexDefinition,
    IndexKey,
    PartitionDefinition,
    SequenceDefinition,
    TableLayout,
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


def target_layout():
    return TableLayout(
        profile_name="test",
        profile_version=1,
        schema="s344",
        table="target",
        source_database="source_db",
        source_table="source_table",
        columns=(
            ColumnDefinition(
                "id_target",
                "bigint",
                False,
                identity=True,
                identity_seed=1,
                identity_increment=1,
                role="technical",
            ),
            ColumnDefinition(
                "name",
                "varchar(20)",
                True,
                default_expression="('unknown')",
                default_name="df_target_name",
                collation="Latin1_General_100_CI_AS",
            ),
            ColumnDefinition(
                "calc",
                "int",
                False,
                computed_expression="([id_target]+(1))",
                persisted=True,
                role="metadata",
            ),
        ),
        primary_key=IndexDefinition(
            "pk_target",
            (IndexKey("id_target", "ASC"),),
            unique=True,
            clustered=True,
            primary_key=True,
            phase="base",
        ),
        indexes=(
            IndexDefinition(
                "ix_target_name",
                (IndexKey("name", "DESC"),),
                unique=False,
                clustered=False,
                includes=("calc",),
                filter_predicate="[name] IS NOT NULL",
                disabled=True,
                phase="secondary",
            ),
        ),
        sequence=SequenceDefinition("seq_target", "BIGINT", 1, 1, False),
        technical_id_strategy="sequence",
        technical_id_source_column=None,
        fill_rules=(),
    )


def column_row(
    column_id,
    name,
    kind,
    *,
    max_length,
    precision,
    scale,
    nullable=False,
    identity=False,
    seed=None,
    increment=None,
    computed=False,
    computed_definition=None,
    persisted=False,
    default_name=None,
    default_definition=None,
    collation=None,
):
    return {
        "column_id": column_id,
        "name": name,
        "type_name": kind,
        "max_length": max_length,
        "precision": precision,
        "scale": scale,
        "collation_name": collation,
        "is_nullable": nullable,
        "is_identity": identity,
        "identity_seed": seed,
        "identity_increment": increment,
        "is_computed": computed,
        "computed_definition": computed_definition,
        "is_persisted": persisted,
        "default_name": default_name,
        "default_definition": default_definition,
    }


def index_row(
    index_id,
    name,
    column,
    *,
    index_type,
    type_desc,
    key_ordinal,
    index_column_id,
    descending=False,
    included=False,
    unique=False,
    primary=False,
    disabled=False,
    filter_definition=None,
    data_space_name=None,
    data_space_type=None,
    partition_ordinal=0,
):
    return {
        "index_id": index_id,
        "index_name": name,
        "type": index_type,
        "type_desc": type_desc,
        "is_unique": unique,
        "is_primary_key": primary,
        "is_disabled": disabled,
        "has_filter": filter_definition is not None,
        "filter_definition": filter_definition,
        "index_column_id": index_column_id,
        "key_ordinal": key_ordinal,
        "is_descending_key": descending,
        "is_included_column": included,
        "column_name": column,
        "data_space_name": data_space_name,
        "data_space_type": data_space_type,
        "partition_ordinal": partition_ordinal,
    }


class DestinationInspectionTests(unittest.TestCase):
    def test_destination_space_requires_each_distinct_volume_to_fit(self):
        connection = QueueConnection(
            [
                {"volume_mount_point": "D:\\", "available_bytes": 1_000},
                {"volume_mount_point": "L:\\", "available_bytes": 500},
            ]
        )
        # A soma (1.500) seria suficiente, mas dados e log nao podem usar o
        # espaco um do outro: o volume L ainda bloqueia a carga de 800 bytes.
        assessment = inspect_destination_space(connection, 800)
        self.assertEqual(assessment.available_bytes, 500)
        self.assertFalse(assessment.sufficient)
        self.assertEqual(
            [volume["sufficient"] for volume in assessment.volumes],
            [True, False],
        )
        self.assertIn("sys.dm_os_volume_stats", connection.calls[0][0])

    def test_destination_space_uses_smallest_duplicate_mount_observation(self):
        connection = QueueConnection(
            [
                {"volume_mount_point": "D:\\", "available_bytes": 1_000},
                {"volume_mount_point": "d:\\", "available_bytes": 700},
                {"volume_mount_point": "L:\\", "available_bytes": 900},
            ]
        )
        assessment = inspect_destination_space(connection, 800)
        self.assertEqual(assessment.available_bytes, 700)
        self.assertFalse(assessment.sufficient)
        self.assertEqual(len(assessment.volumes), 2)
        self.assertEqual(
            assessment.volumes,
            (
                {
                    "volume_mount_point": "D:\\",
                    "available_bytes": 700,
                    "sufficient": False,
                },
                {
                    "volume_mount_point": "L:\\",
                    "available_bytes": 900,
                    "sufficient": True,
                },
            ),
        )

    def test_destination_space_keeps_posix_mounts_case_sensitive(self):
        connection = QueueConnection(
            [
                {"volume_mount_point": "/Data", "available_bytes": 1_000},
                {"volume_mount_point": "/data", "available_bytes": 400},
            ]
        )
        assessment = inspect_destination_space(connection, 500)
        self.assertEqual(len(assessment.volumes), 2)
        self.assertEqual(assessment.available_bytes, 400)
        self.assertFalse(assessment.sufficient)
        self.assertEqual(
            [volume["volume_mount_point"] for volume in assessment.volumes],
            ["/Data", "/data"],
        )

    def test_destination_space_is_unavailable_when_any_volume_has_null_capacity(self):
        connection = QueueConnection(
            [
                {"volume_mount_point": "D:\\", "available_bytes": 1_000},
                {"volume_mount_point": "L:\\", "available_bytes": None},
            ]
        )
        assessment = inspect_destination_space(connection, 500)
        self.assertIsNone(assessment.available_bytes)
        self.assertIsNone(assessment.sufficient)
        self.assertEqual(assessment.error, "DESTINATION_SPACE_UNAVAILABLE")
        self.assertEqual(assessment.volumes[0]["sufficient"], True)
        self.assertIsNone(assessment.volumes[1]["sufficient"])

    def test_destination_space_reports_limiting_capacity_when_all_volumes_fit(self):
        connection = QueueConnection(
            [
                {"volume_mount_point": "D:\\", "available_bytes": 1_000},
                {"volume_mount_point": "L:\\", "available_bytes": 900},
            ]
        )
        assessment = inspect_destination_space(connection, 800)
        self.assertEqual(assessment.available_bytes, 900)
        self.assertTrue(assessment.sufficient)
        self.assertTrue(all(volume["sufficient"] for volume in assessment.volumes))

    def test_destination_space_is_fail_open_only_when_evidence_is_unavailable(self):
        connection = QueueConnection([])
        assessment = inspect_destination_space(connection, 1)
        self.assertIsNone(assessment.available_bytes)
        self.assertIsNone(assessment.sufficient)
        self.assertEqual(assessment.error, "DESTINATION_SPACE_UNAVAILABLE")

    def test_partition_catalog_is_inspected_read_only_and_compares_exactly(self):
        base = target_layout()
        partition = PartitionDefinition(
            column="loaded_at",
            function_name="pf_loaded_at_attr",
            scheme_name="ps_loaded_at_attr",
            clustered_index_name="ix_target_loaded_at_clustered",
            descriptor="attr",
        )
        loaded_at = ColumnDefinition(
            "loaded_at", "DATETIME2(7)", False, role="metadata"
        )
        clustered = IndexDefinition(
            partition.clustered_index_name,
            (IndexKey(partition.column, "ASC"),),
            clustered=True,
            phase="base",
        )
        layout = replace(
            base,
            columns=base.columns + (loaded_at,),
            primary_key=replace(base.primary_key, clustered=False),
            indexes=(clustered,) + base.indexes,
            partition=partition,
        )
        boundary_values = partition_boundary_values(partition)
        connection = QueueConnection(
            [
                {
                    "object_id": 42,
                    "object_type": "U",
                    "type_desc": "USER_TABLE",
                    "create_date": datetime(2026, 1, 1),
                    "schema_name": "s344",
                    "name": "target",
                }
            ],
            [
                {
                    "name": "seq_target",
                    "type_name": "bigint",
                    "start_value": 1,
                    "increment_by": 1,
                    "minimum_value": 1,
                    "maximum_value": 9223372036854775807,
                    "is_cycling": False,
                    "is_cached": True,
                    "cache_size": 50,
                    "current_value": 10,
                }
            ],
            [
                column_row(
                    1, "id_target", "bigint", max_length=8, precision=19,
                    scale=0, identity=True, seed=1, increment=1,
                ),
                column_row(
                    2, "name", "varchar", max_length=20, precision=0,
                    scale=0, nullable=True, default_name="df_target_name",
                    default_definition="('unknown')",
                    collation="Latin1_General_100_CI_AS",
                ),
                column_row(
                    3, "calc", "int", max_length=4, precision=10, scale=0,
                    computed=True, computed_definition="([id_target]+(1))",
                    persisted=True,
                ),
                column_row(
                    4, "loaded_at", "datetime2", max_length=8,
                    precision=27, scale=7,
                ),
            ],
            [
                index_row(
                    1, "pk_target", "id_target", index_type=2,
                    type_desc="NONCLUSTERED", key_ordinal=1,
                    index_column_id=1, unique=True, primary=True,
                    data_space_name="PRIMARY", data_space_type="FG",
                ),
                index_row(
                    2, partition.clustered_index_name, "loaded_at", index_type=1,
                    type_desc="CLUSTERED", key_ordinal=1, index_column_id=1,
                    data_space_name=partition.scheme_name, data_space_type="PS",
                    partition_ordinal=1,
                ),
                index_row(
                    3, "ix_target_name", "name", index_type=2,
                    type_desc="NONCLUSTERED", key_ordinal=1,
                    index_column_id=1, descending=True, disabled=True,
                    filter_definition="[name] IS NOT NULL",
                    data_space_name="PRIMARY", data_space_type="FG",
                ),
                index_row(
                    3, "ix_target_name", "calc", index_type=2,
                    type_desc="NONCLUSTERED", key_ordinal=0,
                    index_column_id=2, included=True, disabled=True,
                    filter_definition="[name] IS NOT NULL",
                    data_space_name="PRIMARY", data_space_type="FG",
                ),
            ],
            [
                {
                    "scheme_name": partition.scheme_name,
                    "function_name": partition.function_name,
                    "boundary_value_on_right": True,
                    "type_name": "datetime2",
                    "max_length": 8,
                    "precision": 27,
                    "scale": 7,
                    "partition_column": partition.column,
                }
            ],
            [{"boundary_value": value} for value in boundary_values],
            [{"filegroup_name": "PRIMARY"}],
            [{"row_count": 123}],
        )

        snapshot = inspect_destination_catalog(
            connection, "s344", "target", sequence_name="seq_target"
        )

        self.assertEqual(snapshot["partition"]["scheme_name"], partition.scheme_name)
        self.assertEqual(snapshot["partition"]["boundaries"], list(boundary_values))
        self.assertEqual(snapshot["indexes"][1]["partition_column"], "loaded_at")
        self.assertEqual(compare_catalog(layout, snapshot).state, CatalogState.COMPATIBLE)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql, _ in connection.calls))

    def test_snapshot_is_directly_compatible_with_catalog_comparer(self):
        layout = target_layout()
        connection = QueueConnection(
            [
                {
                    "object_id": 42,
                    "object_type": "U",
                    "type_desc": "USER_TABLE",
                    "create_date": datetime(2026, 1, 1),
                    "schema_name": "s344",
                    "name": "target",
                }
            ],
            [
                {
                    "name": "seq_target",
                    "type_name": "bigint",
                    "start_value": 1,
                    "increment_by": 1,
                    "minimum_value": 1,
                    "maximum_value": 9223372036854775807,
                    "is_cycling": False,
                    "is_cached": True,
                    "cache_size": 50,
                    "current_value": 10,
                }
            ],
            [
                column_row(
                    1,
                    "id_target",
                    "bigint",
                    max_length=8,
                    precision=19,
                    scale=0,
                    identity=True,
                    seed=1,
                    increment=1,
                ),
                column_row(
                    2,
                    "name",
                    "varchar",
                    max_length=20,
                    precision=0,
                    scale=0,
                    nullable=True,
                    default_name="df_target_name",
                    default_definition="('unknown')",
                    collation="Latin1_General_100_CI_AS",
                ),
                column_row(
                    3,
                    "calc",
                    "int",
                    max_length=4,
                    precision=10,
                    scale=0,
                    computed=True,
                    computed_definition="([id_target]+(1))",
                    persisted=True,
                ),
            ],
            [
                index_row(
                    1,
                    "pk_target",
                    "id_target",
                    index_type=1,
                    type_desc="CLUSTERED",
                    key_ordinal=1,
                    index_column_id=1,
                    unique=True,
                    primary=True,
                ),
                index_row(
                    2,
                    "ix_target_name",
                    "name",
                    index_type=2,
                    type_desc="NONCLUSTERED",
                    key_ordinal=1,
                    index_column_id=1,
                    descending=True,
                    disabled=True,
                    filter_definition="[name] IS NOT NULL",
                ),
                index_row(
                    2,
                    "ix_target_name",
                    "calc",
                    index_type=2,
                    type_desc="NONCLUSTERED",
                    key_ordinal=0,
                    index_column_id=2,
                    included=True,
                    disabled=True,
                    filter_definition="[name] IS NOT NULL",
                ),
            ],
            [{"row_count": 123}],
        )
        snapshot = inspect_destination_catalog(
            connection, "s344", "target", sequence_name="seq_target"
        )
        comparison = compare_catalog(layout, snapshot)
        self.assertEqual(comparison.state, CatalogState.COMPATIBLE)
        self.assertEqual(snapshot["row_count"], 123)
        self.assertEqual(snapshot["columns"][0]["sql_type"], "bigint")
        self.assertEqual(snapshot["columns"][1]["default_name"], "df_target_name")
        self.assertTrue(snapshot["columns"][2]["persisted"])
        self.assertEqual(snapshot["indexes"][1]["keys"], [{"name": "name", "direction": "DESC"}])
        self.assertEqual(snapshot["indexes"][1]["includes"], ["calc"])
        self.assertTrue(snapshot["indexes"][1]["disabled"])
        self.assertEqual(snapshot["sequence"]["increment"], 1)
        for sql, _params in connection.calls:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
            for mutation in ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE "):
                self.assertNotIn(mutation, sql.upper())

    def test_absent_table_is_not_confused_with_empty_table(self):
        connection = QueueConnection([])
        snapshot = inspect_destination_catalog(connection, "s344", "missing")
        self.assertFalse(snapshot["exists"])
        self.assertIsNone(snapshot["row_count"])
        self.assertEqual(snapshot["columns"], [])
        self.assertEqual(len(connection.calls), 1)

    def test_source_database_storage_is_separate_read_only_measurement(self):
        connection = QueueConnection(
            [
                {
                    "data_used_bytes": 111,
                    "data_allocated_bytes": 222,
                    "log_allocated_bytes": 333,
                }
            ]
        )
        storage = inspect_source_database_storage(connection)
        self.assertEqual(storage, SourceDatabaseStorage(111, 222, 333))
        self.assertIn("sys.database_files", connection.calls[0][0])
        self.assertTrue(connection.calls[0][0].lstrip().upper().startswith("SELECT"))


def source_col(name="id"):
    return {
        "name": name,
        "type_name": "int",
        "max_length": 4,
        "precision": 10,
        "scale": 0,
        "is_nullable": False,
        "collation_name": None,
    }


def selection():
    return WatermarkSelection(
        columns=(WatermarkColumn.from_catalog(source_col()),),
        source="primary_key",
        index_name="pk_source",
        is_unique=True,
    )


def count_estimate(value=1000):
    return RowCountEstimate(
        value=value,
        method=METADATA_ROW_COUNT_METHOD,
        observed_at="2026-10-06T00:00:00+00:00",
        approximate=True,
        uncertainty="aproximada",
    )


def size_estimate(value=Decimal("10")):
    return RowSizeEstimate(
        average_bytes=value,
        minimum_bytes=None if value is None else 5,
        maximum_bytes=None if value is None else 20,
        sampled_rows=0 if value is None else 100,
        sample_limit=100,
        method=SAMPLE_METHOD,
        observed_at="2026-10-06T00:00:01+00:00",
        uncertainty=SAMPLE_UNCERTAINTY,
    )


def planner_config(table_names=("a",), *, policy="stop", import_enabled=False):
    return {
        "source": {
            "instance": "source",
            "database": "source_db",
            "read_database": "source_db",
            "schema": "dbo",
        },
        "bronze_destination": {
            "instance": "target",
            "database": "bronze_db",
            "schema": "s344",
            "structure_profile": "bronze",
        },
        "active_destination": "bronze",
        "execute_import": import_enabled,
        "create_structure_if_needed": True,
        "executor_directory": r"C:\BCP",
        "rows_per_block": 100,
        "minimum_free_space_bytes": 100,
        "delete_confirmed_files": True,
        "estimates": {
            "row_count_method": "metadata",
            "maximum_sample_rows": 100,
            "safety_factor": 1.30,
            "on_unavailable": policy,
        },
        "batching": {"require_watermark_index": False},
        "bronze_event_id": {
            "strategy": "sequence",
            "sequence_name": "seq_{destination_table}",
            "source_column": None,
        },
        "tables": [
            {"source_table": name, "destination_table": "dst_" + name, "watermark": None}
            for name in table_names
        ],
    }


def no_destination_profile(*_args, **kwargs):
    return SimpleNamespace(
        schema=kwargs["destination_schema"],
        table=kwargs["destination_table"],
        sequence=None,
    )


class PlannerTests(unittest.TestCase):
    def common_dependencies(self):
        return {
            "columns_discoverer": lambda *_args, **_kwargs: [source_col()],
            "watermark_discoverer": lambda *_args, **_kwargs: selection(),
            "row_count_estimator": lambda *_args, **_kwargs: count_estimate(),
            "row_size_estimator": lambda *_args, **_kwargs: size_estimate(),
            "source_storage_inspector": lambda _connection: SourceDatabaseStorage(111, 222, 333),
            "profile_resolver": no_destination_profile,
        }

    def test_plan_does_not_require_or_touch_destination(self):
        called = []

        def forbidden_inspection(*_args, **_kwargs):
            called.append(True)
            raise AssertionError("destino nao deveria ser consultado")

        plan = build_plan(
            planner_config(),
            object(),
            disk_free_provider=lambda _path: 100_000,
            destination_inspector=forbidden_inspection,
            **self.common_dependencies(),
        )
        self.assertEqual(called, [])
        self.assertFalse(plan.destination_inspection_requested)
        table = plan.tables[0]
        self.assertEqual(table.status, TableStatus.PENDING.value)
        self.assertTrue(table.execution_allowed)
        self.assertIsNone(table.destination_catalog_state)
        self.assertIn("DESTINATION_NOT_INSPECTED", table.warnings)

    def test_direct_keyless_plan_uses_metadata_estimate_and_one_block(self):
        dependencies = self.common_dependencies()
        dependencies["watermark_discoverer"] = lambda *_args, **_kwargs: WatermarkSelection(
            columns=(),
            source="direct_keyless",
            index_name=None,
            is_unique=False,
            transfer_mode="DIRECT_KEYLESS",
            captured_row_count=73,
            direct_reason="sem chave",
        )
        dependencies["row_count_estimator"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("modo direto ja recebeu a estimativa de metadados")
        )
        plan = build_plan(
            planner_config(),
            object(),
            disk_free_provider=lambda _path: 100_000,
            **dependencies,
        )
        table = plan.tables[0]
        self.assertEqual(table.transfer_mode, "DIRECT_KEYLESS")
        self.assertEqual(table.source_rows_at_capture, 73)
        self.assertEqual(table.estimated_rows, 73)
        self.assertTrue(table.row_count_approximate)
        self.assertEqual(table.approximate_blocks, 1)
        self.assertEqual(table.batching_strategy, "direct_keyless_single_block")
        self.assertEqual(table.watermark, [])

    def test_plan_keeps_database_bcp_destination_and_disk_metrics_distinct(self):
        plan = build_plan(
            planner_config(),
            object(),
            disk_free_provider=lambda _path: 100_000,
            **self.common_dependencies(),
        )
        table = plan.tables[0]
        self.assertEqual(table.source_database_used_bytes, 111)
        self.assertEqual(table.source_database_allocated_bytes, 222)
        self.assertEqual(table.source_database_log_allocated_bytes, 333)
        self.assertEqual(table.estimated_bcp_total_bytes, 10_000)
        self.assertEqual(table.estimated_bcp_block_bytes, 1_000)
        self.assertEqual(table.planning_reserve_bytes, 13_000)
        self.assertEqual(table.predicted_peak_bytes, 13_000)
        self.assertEqual(table.executor_free_bytes, 100_000)
        self.assertIsNone(table.destination_data_bytes)
        self.assertIsNone(table.destination_log_bytes)
        self.assertEqual(table.peak_model, "export_only_accumulated")

    def test_export_only_peak_accumulates_previous_tables(self):
        dependencies = self.common_dependencies()
        dependencies["row_count_estimator"] = lambda *_args, **_kwargs: count_estimate(100)
        dependencies["row_size_estimator"] = lambda *_args, **_kwargs: size_estimate(Decimal("10"))
        config = planner_config(("a", "b"))
        config["estimates"]["safety_factor"] = 1
        plan = build_plan(
            config,
            object(),
            disk_free_provider=lambda _path: 100_000,
            **dependencies,
        )
        self.assertEqual([table.predicted_peak_bytes for table in plan.tables], [1000, 2000])

    def test_insufficient_disk_is_distinct_from_unavailable_disk(self):
        plan = build_plan(
            planner_config(),
            object(),
            disk_free_provider=lambda _path: 5_000,
            **self.common_dependencies(),
        )
        table = plan.tables[0]
        self.assertFalse(table.capacity_ok)
        self.assertFalse(table.execution_allowed)
        self.assertIn("INSUFFICIENT_EXECUTOR_SPACE", table.warnings)
        self.assertEqual(table.executor_free_bytes, 5_000)

    def test_unavailable_values_remain_none_and_follow_policy(self):
        dependencies = self.common_dependencies()
        dependencies["row_count_estimator"] = lambda *_args, **_kwargs: count_estimate(None)
        dependencies["row_size_estimator"] = lambda *_args, **_kwargs: size_estimate(None)
        dependencies["source_storage_inspector"] = lambda _connection: SourceDatabaseStorage(None, None, None)

        for policy, expected_allowed in (("stop", False), ("warn", True)):
            with self.subTest(policy=policy):
                plan = build_plan(
                    planner_config(policy=policy),
                    object(),
                    disk_free_provider=lambda _path: (_ for _ in ()).throw(OSError("denied")),
                    **dependencies,
                )
                table = plan.tables[0]
                self.assertIsNone(table.estimated_rows)
                self.assertIsNone(table.average_row_bytes)
                self.assertIsNone(table.estimated_bcp_total_bytes)
                self.assertIsNone(table.predicted_peak_bytes)
                self.assertIsNone(table.executor_free_bytes)
                self.assertIsNone(table.capacity_ok)
                self.assertEqual(table.execution_allowed, expected_allowed)
                self.assertIn("ROW_COUNT_UNAVAILABLE", table.warnings)

    def test_watermark_errors_become_individual_table_codes_and_flow_continues(self):
        errors = {
            "sem_chave": NoEligibleWatermarkError("sem chave"),
            "invalida": InvalidWatermarkError("invalida"),
            "nula": WatermarkContainsNullError("nula"),
            "sem_indice": WatermarkIndexRequiredError("sem indice"),
        }

        def discover(_connection, _schema, table, **_kwargs):
            raise errors[table]

        config = planner_config(tuple(errors))
        dependencies = self.common_dependencies()
        dependencies["watermark_discoverer"] = discover
        plan = build_plan(
            config,
            object(),
            disk_free_provider=lambda _path: 100_000,
            **dependencies,
        )
        self.assertEqual(
            [item.status for item in plan.tables],
            [
                TableStatus.SKIPPED_NO_KEY.value,
                TableStatus.SKIPPED_INVALID_WATERMARK.value,
                TableStatus.SKIPPED_NULL_WATERMARK.value,
                TableStatus.SKIPPED_INVALID_WATERMARK.value,
            ],
        )
        self.assertTrue(all(not item.execution_allowed for item in plan.tables))

    def test_optional_destination_catalog_incompatibility_is_reported_per_table(self):
        inspected = []

        def inspect(_connection, schema, table, **_kwargs):
            inspected.append((schema, table))
            return {"exists": True}

        def compare(_layout, _snapshot):
            return CatalogComparison(CatalogState.INCOMPATIBLE, errors=("layout divergente",))

        plan = build_plan(
            planner_config(import_enabled=True),
            object(),
            destination_connections={"bronze": object()},
            disk_free_provider=lambda _path: 100_000,
            destination_inspector=inspect,
            catalog_comparer=compare,
            **self.common_dependencies(),
        )
        table = plan.tables[0]
        self.assertEqual(inspected, [("s344", "dst_a")])
        self.assertEqual(table.destination_catalog_state, CatalogState.INCOMPATIBLE.value)
        self.assertEqual(table.status, TableStatus.LAYOUT_ERROR.value)
        self.assertFalse(table.execution_allowed)
        self.assertIn("layout divergente", table.reason)


if __name__ == "__main__":
    unittest.main()
