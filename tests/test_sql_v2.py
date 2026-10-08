from __future__ import annotations

import unittest

from bcp_engine.sql import fetch_dicts


class _MultiResultCursor:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self._position = 0
        self.description = None
        self.closed = False

    def execute(self, sql, params=()):
        del sql, params
        self._position = 0
        self.description = self._result_sets[0][0]
        return self

    def nextset(self):
        self._position += 1
        if self._position >= len(self._result_sets):
            self.description = None
            return False
        self.description = self._result_sets[self._position][0]
        return True

    def fetchall(self):
        return self._result_sets[self._position][1]

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class FetchDictsTests(unittest.TestCase):
    def test_skips_non_tabular_results_before_select(self):
        cursor = _MultiResultCursor(
            [
                (None, []),
                (None, []),
                ((('retention_minutes',),), [(262_800,)]),
            ]
        )

        rows = fetch_dicts(_Connection(cursor), "USE [BD_ORIGEM]; SELECT retention;")

        self.assertEqual(rows, [{"retention_minutes": 262_800}])
        self.assertTrue(cursor.closed)

    def test_returns_empty_when_batch_has_no_tabular_result(self):
        cursor = _MultiResultCursor([(None, []), (None, [])])

        rows = fetch_dicts(_Connection(cursor), "USE [BD_ORIGEM];")

        self.assertEqual(rows, [])
        self.assertTrue(cursor.closed)


if __name__ == "__main__":
    unittest.main()
