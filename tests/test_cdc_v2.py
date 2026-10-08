from __future__ import annotations

import unittest
from unittest.mock import patch

from bcp_engine.cdc import (
    CdcCoordinator,
    DEFAULT_CDC_RETENTION_MINUTES,
    MAX_CDC_RETENTION_MINUTES,
    ensure_database_cdc,
    ensure_table_cdc,
)


class FakeSqlError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = []
        self.closed = False

    def execute(self, sql, params=()):
        params = tuple(params)
        self.connection.calls.append((sql, params))
        if "FROM sys.databases" in sql:
            self.connection.database_inspections += 1
            self.description = [("state",), ("is_cdc_enabled",)]
            self._rows = (
                []
                if not self.connection.database_exists
                else [(self.connection.database_state, self.connection.database_enabled)]
            )
        elif "sp_cdc_enable_db" in sql:
            self.connection.database_enable_calls += 1
            if self.connection.database_enable_error is not None:
                if self.connection.database_enabled_after_error:
                    self.connection.database_enabled = True
                raise self.connection.database_enable_error
            if not self.connection.ignore_database_enable:
                self.connection.database_enabled = True
            self.description = None
            self._rows = []
        elif "FROM msdb.dbo.cdc_jobs" in sql:
            self.connection.retention_inspections += 1
            if self.connection.retention_inspection_error is not None:
                raise self.connection.retention_inspection_error
            self.description = [("retention_minutes",)]
            self._rows = (
                [(self.connection.retention_minutes,)]
                if self.connection.cleanup_job_exists
                else []
            )
        elif "sp_cdc_change_job" in sql:
            self.connection.retention_change_calls += 1
            (retention_minutes,) = params
            self.connection.retention_change_params.append(retention_minutes)
            if self.connection.retention_change_error is not None:
                if self.connection.retention_changed_after_error:
                    self.connection.retention_minutes = retention_minutes
                raise self.connection.retention_change_error
            if not self.connection.ignore_retention_change:
                self.connection.retention_minutes = retention_minutes
            self.description = None
            self._rows = []
        elif "sp_cdc_stop_job" in sql:
            self.connection.retention_stop_calls += 1
            if self.connection.retention_stop_error is not None:
                raise self.connection.retention_stop_error
            self.description = None
            self._rows = []
        elif "sp_cdc_start_job" in sql:
            self.connection.retention_start_calls += 1
            if self.connection.retention_start_errors:
                raise self.connection.retention_start_errors.pop(0)
            if self.connection.retention_start_error is not None:
                raise self.connection.retention_start_error
            self.description = None
            self._rows = []
        elif "FROM sys.tables AS t" in sql:
            self.connection.table_inspections += 1
            schema, table = params
            self.description = [("is_tracked_by_cdc",), ("supports_net_changes",)]
            state = self.connection.tables.get((schema, table))
            self._rows = [] if state is None else [(state["tracked"], state["has_pk"])]
        elif "sp_cdc_enable_table" in sql:
            self.connection.table_enable_calls += 1
            schema, table, supports_net_changes = params
            self.connection.table_enable_params.append(params)
            if self.connection.table_enable_error is not None:
                if self.connection.table_enabled_after_error:
                    self.connection.tables[(schema, table)]["tracked"] = True
                raise self.connection.table_enable_error
            if not self.connection.ignore_table_enable:
                self.connection.tables[(schema, table)]["tracked"] = True
            if self.connection.create_cleanup_job_on_table_enable:
                self.connection.cleanup_job_exists = True
            self.connection.last_supports_net_changes = supports_net_changes
            self.description = None
            self._rows = []
        else:
            raise AssertionError(f"SQL inesperado: {sql}")
        return self

    def fetchall(self):
        return list(self._rows)

    def nextset(self):
        return False

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.database_exists = True
        self.database_state = "ONLINE"
        self.database_enabled = False
        self.database_enable_error = None
        self.database_enabled_after_error = False
        self.ignore_database_enable = False
        self.table_enable_error = None
        self.table_enabled_after_error = False
        self.ignore_table_enable = False
        self.tables = {}
        self.calls = []
        self.database_inspections = 0
        self.database_enable_calls = 0
        self.table_inspections = 0
        self.table_enable_calls = 0
        self.table_enable_params = []
        self.last_supports_net_changes = None
        self.cleanup_job_exists = True
        self.create_cleanup_job_on_table_enable = False
        self.retention_minutes = DEFAULT_CDC_RETENTION_MINUTES
        self.retention_inspection_error = None
        self.retention_change_error = None
        self.retention_changed_after_error = False
        self.ignore_retention_change = False
        self.retention_inspections = 0
        self.retention_change_calls = 0
        self.retention_change_params = []
        self.retention_stop_calls = 0
        self.retention_start_calls = 0
        self.retention_stop_error = None
        self.retention_start_error = None
        self.retention_start_errors = []

    def cursor(self):
        return FakeCursor(self)


class DatabaseCdcTests(unittest.TestCase):
    def test_existing_database_cdc_is_idempotent(self):
        connection = FakeConnection()
        connection.database_enabled = True

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertTrue(result.success)
        self.assertTrue(result.enabled)
        self.assertFalse(result.changed)
        self.assertEqual(result.stage, "retention_verification")
        self.assertEqual(connection.database_enable_calls, 0)
        self.assertEqual(connection.database_inspections, 1)
        self.assertEqual(result.retention_minutes, DEFAULT_CDC_RETENTION_MINUTES)
        self.assertFalse(result.retention_changed)
        self.assertEqual(connection.retention_change_calls, 0)
        self.assertEqual(connection.retention_stop_calls, 0)
        self.assertEqual(connection.retention_start_calls, 0)

    def test_database_cdc_is_enabled_and_confirmed(self):
        connection = FakeConnection()

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertTrue(result.success)
        self.assertTrue(result.changed)
        self.assertEqual(connection.database_enable_calls, 1)
        self.assertEqual(connection.database_inspections, 2)
        self.assertIn("USE [BD_ORIGEM]", connection.calls[1][0])

    def test_cleanup_retention_is_changed_and_confirmed_idempotently(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.retention_minutes = 4_320

        result = ensure_database_cdc(
            connection,
            database="BD_ORIGEM",
            retention_minutes=262_800,
        )

        self.assertTrue(result.success)
        self.assertTrue(result.changed)
        self.assertTrue(result.retention_changed)
        self.assertTrue(result.retention_restarted)
        self.assertEqual(result.retention_minutes, 262_800)
        self.assertEqual(connection.retention_change_params, [262_800])
        self.assertEqual(connection.retention_inspections, 3)
        self.assertEqual(connection.retention_stop_calls, 1)
        self.assertEqual(connection.retention_start_calls, 1)
        lookup_sql = next(
            sql for sql, _ in connection.calls if "FROM msdb.dbo.cdc_jobs" in sql
        )
        self.assertIn("database_id = DB_ID()", lookup_sql)

        again = ensure_database_cdc(
            connection,
            database="BD_ORIGEM",
            retention_minutes=262_800,
        )
        self.assertTrue(again.success)
        self.assertFalse(again.changed)
        self.assertEqual(connection.retention_change_calls, 1)
        self.assertEqual(connection.retention_stop_calls, 1)
        self.assertEqual(connection.retention_start_calls, 1)

    def test_cleanup_job_is_mandatory_when_not_deferred(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.cleanup_job_exists = False

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_CLEANUP_JOB_NOT_FOUND")
        self.assertEqual(result.stage, "retention_inspection")

    def test_retention_failure_is_structured_and_redacted(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.retention_minutes = 1_000
        connection.retention_change_error = FakeSqlError(
            "42000", "permission denied (229); PWD=super-secret"
        )

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_PERMISSION_DENIED")
        self.assertNotIn("super-secret", result.message)
        self.assertIn("autorizada", result.permission_hint)

    def test_retention_value_is_validated_before_sql(self):
        for invalid in (True, 0, -1, MAX_CDC_RETENTION_MINUTES + 1, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ensure_database_cdc(
                    FakeConnection(),
                    database="BD_ORIGEM",
                    retention_minutes=invalid,
                )

    def test_retention_restart_failure_is_structured_and_start_is_attempted(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.retention_minutes = 4_320
        connection.retention_stop_error = FakeSqlError("job was already stopped")
        connection.retention_start_error = FakeSqlError("229", "permission denied")

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_PERMISSION_DENIED")
        self.assertEqual(result.stage, "retention_restart")
        self.assertEqual(connection.retention_stop_calls, 1)
        self.assertEqual(connection.retention_start_calls, 1)
        self.assertFalse(result.retention_restarted)

    def test_retention_restart_retries_transient_sql_agent_race(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.retention_minutes = 4_320
        connection.retention_start_errors = [
            FakeSqlError("22022", "SQLServerAgent has a pending request")
        ]

        with patch("bcp_engine.cdc.time.sleep") as delay:
            result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertTrue(result.success)
        self.assertTrue(result.retention_restarted)
        self.assertEqual(connection.retention_start_calls, 2)
        delay.assert_called_once_with(1)

    def test_database_must_exist_and_be_online(self):
        missing = FakeConnection()
        missing.database_exists = False
        result = ensure_database_cdc(missing, database="BD_ORIGEM")
        self.assertEqual(result.error_code, "CDC_DATABASE_NOT_FOUND")

        offline = FakeConnection()
        offline.database_state = "RESTORING"
        result = ensure_database_cdc(offline, database="BD_ORIGEM")
        self.assertEqual(result.error_code, "CDC_DATABASE_NOT_ONLINE")
        self.assertEqual(offline.database_enable_calls, 0)

    def test_database_permission_failure_is_structured_and_redacted(self):
        connection = FakeConnection()
        connection.database_enable_error = FakeSqlError(
            "42000", "The user does not have permission to perform this action. (15247)"
        )

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_PERMISSION_DENIED")
        self.assertIn("sysadmin", result.permission_hint)
        self.assertEqual(connection.database_inspections, 2)

    def test_database_postcondition_is_mandatory(self):
        connection = FakeConnection()
        connection.ignore_database_enable = True

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_DATABASE_ENABLE_NOT_CONFIRMED")

    def test_concurrent_database_activation_is_accepted_only_after_confirmation(self):
        connection = FakeConnection()
        connection.database_enable_error = FakeSqlError("22830", "concurrent activation")
        connection.database_enabled_after_error = True

        result = ensure_database_cdc(connection, database="BD_ORIGEM")

        self.assertTrue(result.success)
        self.assertTrue(result.enabled)
        self.assertFalse(result.changed)


class TableCdcTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.connection.database_enabled = True

    def test_table_with_primary_key_enables_net_changes_and_confirms(self):
        self.connection.tables[("dbo", "TABELA_ORIGEM_01")] = {"tracked": False, "has_pk": True}

        result = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="TABELA_ORIGEM_01"
        )

        self.assertTrue(result.success)
        self.assertTrue(result.changed)
        self.assertTrue(result.supports_net_changes)
        self.assertEqual(self.connection.table_enable_params, [("dbo", "TABELA_ORIGEM_01", 1)])
        self.assertEqual(self.connection.table_inspections, 2)

    def test_table_without_primary_key_disables_net_changes(self):
        self.connection.tables[("dbo", "TABELA_ORIGEM_03")] = {"tracked": False, "has_pk": False}

        result = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="TABELA_ORIGEM_03"
        )

        self.assertTrue(result.success)
        self.assertFalse(result.supports_net_changes)
        self.assertEqual(self.connection.last_supports_net_changes, 0)

    def test_existing_table_cdc_is_idempotent(self):
        self.connection.tables[("dbo", "TABELA_ORIGEM_01")] = {"tracked": True, "has_pk": True}

        result = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="TABELA_ORIGEM_01"
        )

        self.assertTrue(result.success)
        self.assertFalse(result.changed)
        self.assertEqual(self.connection.table_enable_calls, 0)

    def test_missing_table_and_permission_failure_are_structured(self):
        missing = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="INEXISTENTE"
        )
        self.assertEqual(missing.error_code, "CDC_TABLE_NOT_FOUND")

        self.connection.tables[("dbo", "TABELA_ORIGEM_01")] = {"tracked": False, "has_pk": True}
        self.connection.table_enable_error = FakeSqlError(
            "42000", "EXECUTE permission was denied on sp_cdc_enable_table. (229)"
        )
        denied = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="TABELA_ORIGEM_01"
        )
        self.assertFalse(denied.success)
        self.assertEqual(denied.error_code, "CDC_PERMISSION_DENIED")
        self.assertIn("db_owner", denied.permission_hint)

    def test_table_postcondition_is_mandatory(self):
        self.connection.tables[("dbo", "TABELA_ORIGEM_01")] = {"tracked": False, "has_pk": True}
        self.connection.ignore_table_enable = True

        result = ensure_table_cdc(
            self.connection, database="BD_ORIGEM", schema="dbo", table="TABELA_ORIGEM_01"
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CDC_TABLE_ENABLE_NOT_CONFIRMED")

    def test_identifiers_are_quoted_while_names_remain_parameterized_and_case_preserved(self):
        self.connection.tables[("MySchema", "MyTable")] = {"tracked": False, "has_pk": True}

        result = ensure_table_cdc(
            self.connection,
            database="D]B",
            schema="MySchema",
            table="MyTable",
        )

        self.assertTrue(result.success)
        self.assertTrue(all("USE [D]]B]" in sql for sql, _ in self.connection.calls))
        self.assertEqual(self.connection.table_enable_params[0][:2], ("MySchema", "MyTable"))

    def test_invalid_identifier_is_rejected_before_sql(self):
        with self.assertRaises(ValueError):
            ensure_table_cdc(
                self.connection,
                database="BD_ORIGEM",
                schema="dbo",
                table="bad\x00table",
            )
        self.assertEqual(self.connection.calls, [])


class CdcCoordinatorTests(unittest.TestCase):
    def test_database_preflight_runs_once_for_multiple_tables(self):
        connection = FakeConnection()
        connection.tables[("dbo", "A")] = {"tracked": True, "has_pk": True}
        connection.tables[("dbo", "B")] = {"tracked": True, "has_pk": False}
        coordinator = CdcCoordinator()

        first = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="A"
        )
        second = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="B"
        )

        self.assertTrue(first.success and second.success)
        self.assertEqual(connection.database_enable_calls, 1)
        # Initial state plus the single mandatory post-activation verification.
        self.assertEqual(connection.database_inspections, 2)

    def test_no_cdc_tables_means_zero_database_queries(self):
        connection = FakeConnection()
        coordinator = CdcCoordinator()

        result = coordinator.preflight(
            connection,
            database="BD_ORIGEM",
            tables=[{"enable_cdc": False}, {"enable_cdc": False}],
        )

        self.assertIsNone(result)
        self.assertEqual(connection.calls, [])

    def test_retention_waits_for_first_table_job_and_runs_once(self):
        connection = FakeConnection()
        connection.database_enabled = False
        connection.cleanup_job_exists = False
        connection.create_cleanup_job_on_table_enable = True
        connection.retention_minutes = 4_320
        connection.tables[('dbo', 'A')] = {"tracked": False, "has_pk": True}
        connection.tables[('dbo', 'B')] = {"tracked": False, "has_pk": True}
        coordinator = CdcCoordinator(retention_minutes=262_800)

        preflight = coordinator.preflight(
            connection,
            database="BD_ORIGEM",
            tables=[{"enable_cdc": True}],
        )
        self.assertTrue(preflight.success)
        self.assertTrue(preflight.retention_pending)
        self.assertEqual(connection.retention_change_calls, 0)

        first = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="A"
        )
        second = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="B"
        )

        self.assertTrue(first.success and second.success)
        self.assertEqual(first.retention_minutes, 262_800)
        self.assertTrue(first.retention_changed)
        self.assertEqual(connection.retention_change_calls, 1)
        self.assertEqual(connection.retention_stop_calls, 1)
        self.assertEqual(connection.retention_start_calls, 1)
        self.assertEqual(connection.database_enable_calls, 1)

    def test_deferred_retention_failure_is_cached_and_blocks_later_cdc_tables(self):
        connection = FakeConnection()
        connection.database_enabled = True
        connection.cleanup_job_exists = False
        connection.create_cleanup_job_on_table_enable = True
        connection.retention_minutes = 4_320
        connection.retention_change_error = FakeSqlError("229", "permission denied")
        connection.tables[("dbo", "A")] = {"tracked": False, "has_pk": True}
        connection.tables[("dbo", "B")] = {"tracked": False, "has_pk": True}
        coordinator = CdcCoordinator()

        coordinator.preflight(
            connection,
            database="BD_ORIGEM",
            tables=[{"enable_cdc": True}],
        )
        first = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="A"
        )
        second = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="B"
        )

        self.assertFalse(first.success or second.success)
        self.assertEqual(first.error_code, "CDC_PERMISSION_DENIED")
        self.assertEqual(second.stage, "database_preflight")
        self.assertEqual(connection.retention_change_calls, 1)
        self.assertEqual(connection.table_enable_calls, 1)

    def test_failed_database_preflight_is_cached_and_blocks_each_table(self):
        connection = FakeConnection()
        connection.database_enable_error = FakeSqlError("15247", "sysadmin required")
        coordinator = CdcCoordinator()

        first = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="A"
        )
        second = coordinator.ensure_table(
            connection, database="BD_ORIGEM", schema="dbo", table="B"
        )

        self.assertFalse(first.success or second.success)
        self.assertEqual(first.stage, "database_preflight")
        self.assertEqual(connection.database_enable_calls, 1)
        self.assertEqual(connection.table_inspections, 0)


if __name__ == "__main__":
    unittest.main()
