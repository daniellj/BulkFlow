"""Explicit, fail-closed migrations for the durable local SQLite control.

Migrations in this module are deliberately not invoked by :class:`LocalState`.
The operator must run the dedicated CLI command while the BCP engine is
stopped.  Version 3 is the only legacy layout accepted here; unversioned,
version 2, future, and structurally modified databases remain rejected.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any

from .state import (
    CONTROL_FILE_NAME,
    LEGACY_CONTROL_FILE_NAME,
    LEGACY_SCHEMA_VERSION as V4_SCHEMA_VERSION,
    SCHEMA_VERSION,
    StateVersionError,
    _assert_local_control_path,
    _migrate_v4_control_copy,
    _validate_current_control,
)


LEGACY_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class StateMigrationReport:
    """Result of an explicit local-control migration."""

    state_path: str
    from_version: int
    to_version: int
    migrated: bool
    backup_path: str | None
    archived_index_state_rows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_V3_INDEX_STATE_COLUMNS = (
    ("execution_id", "TEXT", 1, None, 1, 0),
    ("table_id", "TEXT", 1, None, 2, 0),
    ("index_name", "TEXT", 1, None, 3, 0),
    ("state", "TEXT", 1, None, 0, 0),
    ("error_message", "TEXT", 0, None, 0, 0),
    ("updated_at", "TEXT", 1, None, 0, 0),
)
_CORE_TABLES = ("meta", "executions", "table_runs", "blocks", "attempts")
_V3_TABLES = (*_CORE_TABLES, "index_states")
_PRIMARY_KEYS = {
    "meta": ("key",),
    "executions": ("execution_id",),
    "table_runs": ("execution_id", "table_id"),
    "blocks": ("execution_id", "table_id", "block_id"),
    "attempts": ("execution_id", "table_id", "block_id", "attempt"),
    "index_states": ("execution_id", "table_id", "index_name"),
}


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _quick_check(connection: sqlite3.Connection) -> None:
    result = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
    if result != ("ok",):
        raise StateVersionError(
            "Controle local corrompido (SQLite quick_check): " + "; ".join(result)
        )


def _validate_v3_index_states(connection: sqlite3.Connection) -> int:
    actual_columns = tuple(
        (
            str(row["name"]),
            str(row["type"]).upper(),
            int(row["notnull"]),
            row["dflt_value"],
            int(row["pk"]),
            int(row["hidden"]),
        )
        for row in connection.execute('PRAGMA table_xinfo("index_states")')
    )
    if actual_columns != _V3_INDEX_STATE_COLUMNS:
        raise StateVersionError(
            "Controle local v3 incompatível: index_states possui colunas divergentes; "
            "nenhuma migração foi executada."
        )

    indexes = list(connection.execute('PRAGMA index_list("index_states")'))
    if len(indexes) != 1:
        raise StateVersionError(
            "Controle local v3 incompatível: index_states possui índices divergentes; "
            "nenhuma migração foi executada."
        )
    index = indexes[0]
    index_name = str(index["name"])
    index_columns = tuple(
        str(row[2])
        for row in connection.execute(f"PRAGMA index_info({_quoted(index_name)})")
    )
    key_details = tuple(
        (str(row[2]), int(row[3]), str(row[4]).upper())
        for row in connection.execute(f"PRAGMA index_xinfo({_quoted(index_name)})")
        if int(row[5]) == 1
    )
    if (
        int(index["unique"]) != 1
        or str(index["origin"]) != "pk"
        or int(index["partial"]) != 0
        or index_columns != _PRIMARY_KEYS["index_states"]
        or key_details
        != tuple((column, 0, "BINARY") for column in _PRIMARY_KEYS["index_states"])
    ):
        raise StateVersionError(
            "Controle local v3 incompatível: chave primária de index_states divergente; "
            "nenhuma migração foi executada."
        )

    foreign_keys = list(connection.execute('PRAGMA foreign_key_list("index_states")'))
    ordered = sorted(foreign_keys, key=lambda row: (int(row["id"]), int(row["seq"])))
    actual_foreign_key = tuple(
        (
            str(row["table"]),
            str(row["from"]),
            str(row["to"]),
            str(row["on_update"]),
            str(row["on_delete"]),
            str(row["match"]),
        )
        for row in ordered
    )
    expected_foreign_key = (
        ("table_runs", "execution_id", "execution_id", "NO ACTION", "NO ACTION", "NONE"),
        ("table_runs", "table_id", "table_id", "NO ACTION", "NO ACTION", "NONE"),
    )
    if actual_foreign_key != expected_foreign_key:
        raise StateVersionError(
            "Controle local v3 incompatível: chave estrangeira de index_states divergente; "
            "nenhuma migração foi executada."
        )

    ddl_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='index_states'"
    ).fetchone()
    ddl = "" if ddl_row is None else str(ddl_row[0] or "")
    if re.search(r"\b(?:collate|check|on\s+conflict|without\s+rowid|strict)\b", ddl, re.I):
        raise StateVersionError(
            "Controle local v3 incompatível: index_states contém semântica não versionada; "
            "nenhuma migração foi executada."
        )
    return int(connection.execute("SELECT COUNT(*) FROM index_states").fetchone()[0])


def _content_fingerprint(connection: sqlite3.Connection, tables: tuple[str, ...]) -> str:
    """Hash logical rows, independent from SQLite page layout."""

    digest = hashlib.sha256()
    for table in tables:
        columns = tuple(
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_xinfo({_quoted(table)})")
            if int(row["hidden"]) == 0
        )
        order = ",".join(_quoted(column) for column in _PRIMARY_KEYS[table])
        rows = connection.execute(
            f"SELECT * FROM {_quoted(table)} ORDER BY {order}"
        )
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        digest.update(json.dumps(columns, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")
        for row in rows:
            values = [row[column] for column in columns]
            digest.update(
                json.dumps(
                    values,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _validate_v3(path: Path) -> tuple[str, str, int]:
    """Validate the exact v3 contract and return full/core fingerprints."""

    with closing(_connect(path)) as connection:
        version = _version(connection)
        if version != LEGACY_SCHEMA_VERSION:
            raise StateVersionError(
                f"Migração explícita aceita somente controle local versão 3; "
                f"encontrada versão {version}. Nenhuma alteração foi feita."
            )
        _quick_check(connection)
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise StateVersionError("PRAGMA foreign_keys não pôde ser habilitado")
        actual_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_tables != set(_V3_TABLES):
            raise StateVersionError(
                "Controle local v3 incompatível: conjunto de tabelas divergente; "
                "nenhuma migração foi executada."
            )
        archived_rows = _validate_v3_index_states(connection)
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise StateVersionError(
                "Controle local v3 incompatível: "
                f"{len(violations)} violação(ões) de integridade referencial; "
                "nenhuma migração foi executada."
            )
        full = _content_fingerprint(connection, _V3_TABLES)
        core = _content_fingerprint(connection, _CORE_TABLES)
        return full, core, archived_rows


def _validate_current(path: Path) -> str:
    """Run the complete current physical/semantic validation on any path."""

    return _validate_current_control(path)


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(_connect(source)) as source_connection, closing(
        _connect(destination)
    ) as destination_connection:
        source_connection.backup(destination_connection)
    # Windows rejects fsync on a read-only descriptor; r+b does not alter the
    # file and gives FlushFileBuffers a writable handle.
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_local_state(
    control_directory: Path | str,
    *,
    backup_path: Path | str | None = None,
) -> StateMigrationReport:
    """Migrate a validated legacy local control to the current contract.

    A legacy source is never overwritten when it carries the old filename.
    Version 3 also receives a backup retaining ``index_states``. The current
    file is published only after structural and logical validation succeeds.
    """

    directory = Path(control_directory).resolve()
    _assert_local_control_path(directory)
    current_path = directory / CONTROL_FILE_NAME
    legacy_path = directory / LEGACY_CONTROL_FILE_NAME

    if current_path.is_file():
        source_path = current_path
    elif legacy_path.is_file():
        source_path = legacy_path
    else:
        raise FileNotFoundError(
            f"Controle local não encontrado: {current_path} ou {legacy_path}"
        )

    with closing(_connect(source_path)) as initial:
        current_version = _version(initial)

    if current_version == SCHEMA_VERSION:
        _validate_current(source_path)
        if source_path != current_path:
            from .state import _copy_current_legacy_name

            _copy_current_legacy_name(source_path, current_path)
        return StateMigrationReport(
            state_path=str(current_path),
            from_version=SCHEMA_VERSION,
            to_version=SCHEMA_VERSION,
            migrated=source_path != current_path,
            backup_path=str(source_path) if source_path != current_path else None,
            archived_index_state_rows=0,
        )

    if current_version == V4_SCHEMA_VERSION:
        migrated_path = _migrate_v4_control_copy(source_path, current_path)
        return StateMigrationReport(
            state_path=str(migrated_path),
            from_version=V4_SCHEMA_VERSION,
            to_version=SCHEMA_VERSION,
            migrated=True,
            backup_path=(
                str(source_path)
                if source_path != current_path
                else str(current_path) + ".v4.backup"
            ),
            archived_index_state_rows=0,
        )

    if current_version != LEGACY_SCHEMA_VERSION:
        raise StateVersionError(
            "Migração explícita aceita somente controles SQLite nas versões "
            f"{LEGACY_SCHEMA_VERSION}, {V4_SCHEMA_VERSION} ou {SCHEMA_VERSION}; "
            f"encontrada versão {current_version}. Nenhuma alteração foi feita."
        )

    initial_full, initial_core, archived_rows = _validate_v3(source_path)
    selected_backup = (
        Path(backup_path).resolve()
        if backup_path is not None
        else source_path.with_name(source_path.name + ".v3.backup")
    )
    if selected_backup in {source_path, current_path}:
        raise ValueError("backup_path deve ser diferente dos arquivos de controle")
    selected_backup.parent.mkdir(parents=True, exist_ok=True)

    legacy_candidate = current_path.with_name(
        f".{current_path.name}.v4.migrating.{uuid.uuid4().hex}"
    )
    current_candidate = current_path.with_name(
        f".{current_path.name}.v5.migrating.{uuid.uuid4().hex}"
    )
    try:
        _sqlite_backup(source_path, legacy_candidate)
        candidate_full, candidate_core, candidate_archived_rows = _validate_v3(
            legacy_candidate
        )
        if (
            candidate_full != initial_full
            or candidate_core != initial_core
            or candidate_archived_rows != archived_rows
        ):
            raise StateVersionError(
                "Controle local mudou durante a preparação da migração; tente "
                "novamente com o motor parado. Nada foi publicado."
            )

        with closing(_connect(legacy_candidate)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DROP TABLE index_states")
                connection.execute(f"PRAGMA user_version={V4_SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

        # This step performs full current-schema validation and proves that the
        # V3 core was not structurally altered before any backup/publication.
        _migrate_v4_control_copy(legacy_candidate, current_candidate)
        _validate_current(current_candidate)

        if selected_backup.exists():
            backup_full, _backup_core, _backup_rows = _validate_v3(selected_backup)
            if backup_full != candidate_full:
                raise StateVersionError(
                    f"Backup v3 já existe com conteúdo diferente: {selected_backup}. "
                    "Nenhum arquivo foi sobrescrito."
                )
        else:
            _sqlite_backup(source_path, selected_backup)
            _sync_directory(selected_backup.parent)
            backup_full, _backup_core, _backup_rows = _validate_v3(selected_backup)
            if backup_full != candidate_full:
                raise StateVersionError(
                    "Falha ao validar o backup v3; o original foi preservado."
                )

        current_full, current_core, current_archived_rows = _validate_v3(source_path)
        if (
            current_full != initial_full
            or current_core != initial_core
            or current_archived_rows != archived_rows
        ):
            raise StateVersionError(
                "Controle local mudou durante a migração; tente novamente com o "
                "motor parado. O backup foi preservado e nada foi publicado."
            )

        if current_path.exists() and current_path != source_path:
            raise StateVersionError(
                f"Destino de controle passou a existir durante a migração: {current_path}. "
                "Nenhum arquivo foi sobrescrito."
            )
        os.replace(current_candidate, current_path)
        _sync_directory(directory)
        _validate_current(current_path)
        return StateMigrationReport(
            state_path=str(current_path),
            from_version=LEGACY_SCHEMA_VERSION,
            to_version=SCHEMA_VERSION,
            migrated=True,
            backup_path=str(selected_backup),
            archived_index_state_rows=archived_rows,
        )
    finally:
        for candidate in (legacy_candidate, current_candidate):
            if candidate.exists():
                candidate.unlink()


def migrate_local_state_v3_to_v4(
    control_directory: Path | str,
    *,
    backup_path: Path | str | None = None,
) -> StateMigrationReport:
    """Backward-compatible alias for the generic migration entry point."""

    return migrate_local_state(control_directory, backup_path=backup_path)


__all__ = [
    "CONTROL_FILE_NAME",
    "LEGACY_SCHEMA_VERSION",
    "StateMigrationReport",
    "migrate_local_state",
    "migrate_local_state_v3_to_v4",
]
