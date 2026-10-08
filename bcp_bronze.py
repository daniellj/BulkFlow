#!/usr/bin/env python3
"""Entrada compatível do BulkFlow - SQL Server Data Export & Load.

A lógica reside no pacote :mod:`bcp_engine`; este arquivo permanece para os
comandos operacionais documentados e para utilitários puros historicamente
importados por testes/scripts locais.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bcp_engine.batching import (
    after_predicate,
    at_or_before_predicate,
    bookmark_literal,
    bookmark_text_expression,
    order_by as _order_by,
)
from bcp_engine.bcp import NS, BcpRunner, make_format_file
from bcp_engine.catalog import WatermarkColumn
from bcp_engine.cli import main
from bcp_engine.config import read_config
from bcp_engine.ddl import generate_ddl_script
from bcp_engine.profiles import resolve_profile
from bcp_engine.util import digest, file_hash, qi, qs, stable_json, type_sql


@dataclass(frozen=True)
class Key:
    """Adaptador legado para os predicados tipados V2."""

    column: Mapping[str, Any]
    descending: bool = False

    def as_watermark(self) -> WatermarkColumn:
        return WatermarkColumn(
            name=str(self.column["name"]),
            type_name=str(self.column["type_name"]).casefold(),
            max_length=self.column.get("max_length"),
            precision=self.column.get("precision"),
            scale=self.column.get("scale"),
            nullable=bool(self.column.get("is_nullable", self.column.get("nullable", False))),
            collation_name=self.column.get("collation_name", self.column.get("collation")),
            descending=self.descending,
        )


def _keys(keys: Sequence[Key]) -> tuple[WatermarkColumn, ...]:
    return tuple(key.as_watermark() for key in keys)


def key_literal(key: Key, value: str) -> str:
    return bookmark_literal(key.as_watermark(), value)


def key_text(key: Key) -> str:
    return bookmark_text_expression(key.as_watermark())


def after(keys: Sequence[Key], values: Sequence[str] | None) -> str:
    return after_predicate(_keys(keys), values)


def order_by(keys: Sequence[Key], reverse: bool = False) -> str:
    return _order_by(_keys(keys), reverse=reverse)


def export_query(
    table: str,
    columns: Sequence[Mapping[str, Any]],
    keys: Sequence[Key],
    lower: Sequence[str] | None,
    upper: Sequence[str],
) -> str:
    names = ", ".join(qi(str(column["name"])) for column in columns)
    watermark = _keys(keys)
    predicate = " AND ".join((
        f"({after_predicate(watermark, lower)})",
        f"({at_or_before_predicate(watermark, upper)})",
    ))
    return f"SELECT {names} FROM {table} WHERE {predicate}"


def target_ddl(schema: str, table: str, columns: Sequence[Mapping[str, Any]]) -> str:
    """Gera o contrato Bronze V2 exato (ID por sequence, sem IDENTITY)."""

    parts = table.split("_", 1)
    layout = resolve_profile(
        "bronze",
        columns,
        source_database=parts[0] if len(parts) == 2 else "source",
        source_table=parts[-1],
        destination_schema=schema,
        destination_table=table,
    )
    return generate_ddl_script(layout)


def make_format(
    config: dict[str, Any],
    source_name: str,
    columns: list[dict[str, Any]],
    path: Path,
    log: Path,
) -> None:
    """Compatibilidade estreita; produção chama ``make_format_file`` diretamente."""

    from bcp_engine.auth import SecretResolver, build_bcp_invocation

    resolver = SecretResolver()
    endpoint = config["source"]
    if "authentication" not in endpoint:
        endpoint = {**endpoint, "authentication": {"type": "windows_integrated"}}
    invocation = build_bcp_invocation(endpoint, config, resolver, endpoint_name="source")
    make_format_file(BcpRunner(), invocation, source_name, columns, path, log)


if __name__ == "__main__":
    raise SystemExit(main())
