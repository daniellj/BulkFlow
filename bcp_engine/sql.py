"""Fronteira pequena para acesso SQL Server.

As funções aceitam qualquer objeto DB-API compatível, o que permite testes com
fakes sem simular ``pyodbc`` internamente.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence


SESSION_OPTIONS = """SET NOCOUNT ON;
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;"""


def fetch_dicts(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, tuple(params))
        # pyodbc exposes statements such as ``USE [database]`` as an
        # intermediate result without a column description.  Advance through
        # those results until the batch reaches its tabular SELECT.  Returning
        # an empty list when no tabular result exists keeps the helper's
        # original contract while making multi-statement SQL deterministic on
        # a real SQL Server connection.
        while cursor.description is None:
            if not cursor.nextset():
                return []
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def scalar(connection: Any, sql: str, params: Sequence[Any] = ()) -> Any:
    cursor = connection.cursor()
    try:
        row = cursor.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]
    finally:
        cursor.close()


def execute(connection: Any, sql: str, params: Sequence[Any] = ()) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, tuple(params))
        while cursor.nextset():
            pass
    finally:
        cursor.close()


_GO_LINE = re.compile(r"(?im)^\s*GO(?:\s+\d+)?\s*$")


def split_batches(script: str) -> list[str]:
    """Separa ``GO`` para que nunca seja enviado a ``cursor.execute``."""

    result: list[str] = []
    start = 0
    for match in _GO_LINE.finditer(script):
        batch = script[start:match.start()].strip()
        if batch:
            result.append(batch)
        start = match.end()
    tail = script[start:].strip()
    if tail:
        result.append(tail)
    return result


def execute_script(connection: Any, script: str) -> None:
    for batch in split_batches(script):
        execute(connection, batch)


def configure_session(connection: Any) -> None:
    execute(connection, SESSION_OPTIONS)


def close_all(connections: Iterable[Any]) -> None:
    for connection in connections:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
