"""Estado local durável e versionado do BulkFlow.

O banco SQLite pertence ao executor e usa journal DELETE, nunca WAL. A base não
deve ser colocada em compartilhamento de rede; os manifestos portáteis ficam ao
lado dos arquivos de dados.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import uuid
from typing import Any, Iterator

from .models import ExecutionState, IndexState, TableStatus
from .util import stable_json, validate_canonical_uuid, validate_sha256_hex


SCHEMA_VERSION = 5
CONTROL_FILE_NAME = "controle_transferencia.sqlite3"
LEGACY_CONTROL_FILE_NAME = "bcp_control_v2.sqlite3"
LEGACY_SCHEMA_VERSION = 4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StateVersionError(RuntimeError):
    pass


class StructuralResumeError(RuntimeError):
    pass


# (nome, tipo declarado, NOT NULL, default SQL, posicao na chave primaria)
#
# O ``user_version`` sozinho nao prova que o arquivo ainda possui o contrato
# V2. Manter a assinatura aqui faz a abertura falhar de forma diagnostica em
# vez de aceitar uma base antiga/adulterada e descobrir a divergencia no meio
# de uma execucao.
_SCHEMA_COLUMNS: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
    "metadados": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "execucao": (
        ("execution_id", "TEXT", 0, None, 1),
        ("dataset_id", "TEXT", 1, None, 0),
        ("command", "TEXT", 1, None, 0),
        ("config_path", "TEXT", 0, None, 0),
        ("structural_hash", "TEXT", 1, None, 0),
        ("operational_hash", "TEXT", 1, None, 0),
        ("import_enabled", "INTEGER", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("started_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
        ("error_code", "TEXT", 0, None, 0),
        ("error_message", "TEXT", 0, None, 0),
    ),
    "execucao_tabela": (
        ("execution_id", "TEXT", 1, None, 1),
        ("table_id", "TEXT", 1, None, 2),
        ("ordinal", "INTEGER", 1, None, 0),
        ("source_schema", "TEXT", 1, None, 0),
        ("source_table", "TEXT", 1, None, 0),
        ("destination_area", "TEXT", 0, None, 0),
        ("destination_schema", "TEXT", 0, None, 0),
        ("destination_table", "TEXT", 0, None, 0),
        ("structural_hash", "TEXT", 1, None, 0),
        ("watermark_json", "TEXT", 0, None, 0),
        ("final_limit_json", "TEXT", 0, None, 0),
        ("export_cursor_json", "TEXT", 0, None, 0),
        ("import_cursor_json", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("rows_exported", "INTEGER", 1, "0", 0),
        ("rows_imported", "INTEGER", 1, "0", 0),
        ("bytes_exported", "INTEGER", 1, "0", 0),
        ("index_state", "TEXT", 1, "'NOT_STARTED'", 0),
        ("reason", "TEXT", 0, None, 0),
        ("warnings_json", "TEXT", 1, "'[]'", 0),
        ("started_at", "TEXT", 0, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
    ),
    "execucao_lote": (
        ("execution_id", "TEXT", 1, None, 1),
        ("table_id", "TEXT", 1, None, 2),
        ("block_id", "TEXT", 1, None, 3),
        ("block_number", "INTEGER", 1, None, 0),
        ("lower_bound_json", "TEXT", 0, None, 0),
        ("upper_bound_json", "TEXT", 1, None, 0),
        ("final_limit_json", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("attempts", "INTEGER", 1, "0", 0),
        ("empty_range", "INTEGER", 1, "0", 0),
        ("rows_exported", "INTEGER", 0, None, 0),
        ("rows_imported", "INTEGER", 0, None, 0),
        ("file_bytes", "INTEGER", 0, None, 0),
        ("data_sha256", "TEXT", 0, None, 0),
        ("format_sha256", "TEXT", 0, None, 0),
        ("manifest_path", "TEXT", 0, None, 0),
        ("exported_at", "TEXT", 0, None, 0),
        ("imported_at", "TEXT", 0, None, 0),
        ("error_code", "TEXT", 0, None, 0),
        ("error_message", "TEXT", 0, None, 0),
    ),
    "tentativa_lote": (
        ("execution_id", "TEXT", 1, None, 1),
        ("table_id", "TEXT", 1, None, 2),
        ("block_id", "TEXT", 1, None, 3),
        ("attempt", "INTEGER", 1, None, 4),
        ("status", "TEXT", 1, None, 0),
        ("started_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
        ("log_path", "TEXT", 0, None, 0),
        ("error_code", "TEXT", 0, None, 0),
        ("error_message", "TEXT", 0, None, 0),
    ),
}

# (nome explicito ou None, colunas, unique, origem SQLite, parcial)
_SCHEMA_INDEXES: dict[
    str, tuple[tuple[str | None, tuple[str, ...], int, str, int], ...]
] = {
    "metadados": ((None, ("key",), 1, "pk", 0),),
    "execucao": ((None, ("execution_id",), 1, "pk", 0),),
    "execucao_tabela": (
        (None, ("execution_id", "table_id"), 1, "pk", 0),
        (
            "ux_execucao_tabela_destino",
            ("execution_id", "destination_area", "destination_schema", "destination_table"),
            1,
            "c",
            1,
        ),
    ),
    "execucao_lote": (
        (None, ("execution_id", "table_id", "block_id"), 1, "pk", 0),
        (None, ("execution_id", "table_id", "block_number"), 1, "u", 0),
    ),
    "tentativa_lote": (
        (None, ("execution_id", "table_id", "block_id", "attempt"), 1, "pk", 0),
    ),
}

# (tabela referenciada, colunas locais, colunas remotas, ON UPDATE, ON DELETE, MATCH)
_SCHEMA_FOREIGN_KEYS: dict[
    str,
    tuple[tuple[str, tuple[str, ...], tuple[str, ...], str, str, str], ...],
] = {
    "metadados": (),
    "execucao": (),
    "execucao_tabela": (
        (
            "execucao",
            ("execution_id",),
            ("execution_id",),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ),
    "execucao_lote": (
        (
            "execucao_tabela",
            ("execution_id", "table_id"),
            ("execution_id", "table_id"),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ),
    "tentativa_lote": (
        (
            "execucao_lote",
            ("execution_id", "table_id", "block_id"),
            ("execution_id", "table_id", "block_id"),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ),
}


def _assert_local_control_path(path: Path) -> None:
    raw = str(path)
    win = PureWindowsPath(raw)
    if raw.startswith("\\\\") or win.drive.startswith("\\\\"):
        raise ValueError("local_control_directory não pode ser UNC; use disco local durável")
    if not win.is_absolute() and not path.is_absolute():
        raise ValueError("local_control_directory deve ser absoluto")


class LocalState:
    def __init__(self, directory: Path | str, *, create: bool = True) -> None:
        self.directory = Path(directory)
        _assert_local_control_path(self.directory)
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.path = _prepare_control_path(self.directory)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LocalState":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def execution_lease(self, execution_id: str) -> Iterator[None]:
        """Impede dois run/resume/import concorrentes para o mesmo UUID.

        O lock pertence ao descritor aberto e e liberado automaticamente pelo
        sistema operacional se o processo morrer. O pequeno arquivo permanece
        para evitar corrida entre unlink e uma nova aquisicao.
        """

        validate_canonical_uuid(execution_id, "execution_id")
        path = self.directory / f"execution_{execution_id}.lock"
        stream = path.open("a+b")
        try:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if __import__("os").name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                raise RuntimeError(
                    f"Execucao {execution_id} ja esta ativa neste executor"
                ) from exc
            try:
                yield
            finally:
                stream.seek(0)
                if __import__("os").name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _initialize(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, SCHEMA_VERSION):
            raise StateVersionError(
                f"Controle local versão {version} incompatível com {SCHEMA_VERSION}. "
                "Preserve o arquivo e execute uma migração explícita; nenhuma conversão foi feita."
            )
        if version == SCHEMA_VERSION:
            self._validate_schema()
            return
        existing_objects = self.connection.execute(
            """SELECT type,name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall()
        if existing_objects:
            names = ", ".join(f"{row['type']}:{row['name']}" for row in existing_objects)
            raise StateVersionError(
                "Controle local sem versao (user_version=0) contem objetos existentes: "
                f"{names}. Migre-o explicitamente; nenhuma estrutura foi alterada."
            )
        # sqlite3.Connection.executescript() executa COMMIT implícito antes do
        # script. Portanto ele não pode ficar dentro de transaction(), cujo
        # COMMIT final encontraria "no transaction is active". O próprio script
        # delimita a migração inteira e o handler abaixo garante rollback.
        try:
            self.connection.executescript("""
BEGIN IMMEDIATE;
CREATE TABLE metadados (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE execucao (
  execution_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  command TEXT NOT NULL,
  config_path TEXT,
  structural_hash TEXT NOT NULL,
  operational_hash TEXT NOT NULL,
  import_enabled INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  error_code TEXT,
  error_message TEXT
);
CREATE TABLE execucao_tabela (
  execution_id TEXT NOT NULL REFERENCES execucao(execution_id),
  table_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  source_schema TEXT NOT NULL,
  source_table TEXT NOT NULL,
  destination_area TEXT,
  destination_schema TEXT,
  destination_table TEXT,
  structural_hash TEXT NOT NULL,
  watermark_json TEXT,
  final_limit_json TEXT,
  export_cursor_json TEXT,
  import_cursor_json TEXT,
  status TEXT NOT NULL,
  rows_exported INTEGER NOT NULL DEFAULT 0,
  rows_imported INTEGER NOT NULL DEFAULT 0,
  bytes_exported INTEGER NOT NULL DEFAULT 0,
  index_state TEXT NOT NULL DEFAULT 'NOT_STARTED',
  reason TEXT,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  started_at TEXT,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (execution_id, table_id)
);
CREATE UNIQUE INDEX ux_execucao_tabela_destino
ON execucao_tabela(execution_id, destination_area, destination_schema, destination_table)
WHERE destination_table IS NOT NULL;
CREATE TABLE execucao_lote (
  execution_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  block_id TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  lower_bound_json TEXT,
  upper_bound_json TEXT NOT NULL,
  final_limit_json TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  empty_range INTEGER NOT NULL DEFAULT 0,
  rows_exported INTEGER,
  rows_imported INTEGER,
  file_bytes INTEGER,
  data_sha256 TEXT,
  format_sha256 TEXT,
  manifest_path TEXT,
  exported_at TEXT,
  imported_at TEXT,
  error_code TEXT,
  error_message TEXT,
  PRIMARY KEY (execution_id, table_id, block_id),
  UNIQUE (execution_id, table_id, block_number),
  FOREIGN KEY (execution_id, table_id) REFERENCES execucao_tabela(execution_id, table_id)
);
CREATE TABLE tentativa_lote (
  execution_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  block_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  log_path TEXT,
  error_code TEXT,
  error_message TEXT,
  PRIMARY KEY (execution_id, table_id, block_id, attempt),
  FOREIGN KEY (execution_id, table_id, block_id)
    REFERENCES execucao_lote(execution_id, table_id, block_id)
);
INSERT INTO metadados(key,value)
VALUES('created_at',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
PRAGMA user_version=5;
COMMIT;
""")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self._validate_schema()

    def _validate_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise StateVersionError(
                f"Controle local versao {version} incompatível com {SCHEMA_VERSION}; "
                "nenhuma migracao foi executada."
            )
        quick_check = tuple(
            str(row[0]) for row in self.connection.execute("PRAGMA quick_check")
        )
        if quick_check != ("ok",):
            raise StateVersionError(
                "Controle local corrompido (SQLite quick_check): " + "; ".join(quick_check)
            )
        if int(self.connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise StateVersionError(
                "Controle local inseguro: PRAGMA foreign_keys nao esta habilitado"
            )

        required = set(_SCHEMA_COLUMNS)
        actual = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = required - actual
        if missing:
            raise StateVersionError("Controle local corrompido; tabelas ausentes: " + ", ".join(sorted(missing)))
        unexpected = actual - required
        if unexpected:
            raise StateVersionError(
                "Controle local V2 possui tabelas nao versionadas: "
                + ", ".join(sorted(unexpected))
                + ". Migre explicitamente e atualize user_version."
            )

        problems: list[str] = []
        expected_named_objects = {
            *(('table', table, table) for table in _SCHEMA_COLUMNS),
            ('index', 'ux_execucao_tabela_destino', 'execucao_tabela'),
        }
        actual_named_objects = {
            (str(row['type']), str(row['name']), str(row['tbl_name']))
            for row in self.connection.execute(
                """SELECT type,name,tbl_name FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'"""
            )
        }
        missing_objects = expected_named_objects - actual_named_objects
        unexpected_objects = actual_named_objects - expected_named_objects
        if missing_objects:
            problems.append(
                "objetos requeridos ausentes: "
                + ", ".join(
                    f"{kind}:{name}" for kind, name, _table in sorted(missing_objects)
                )
            )
        if unexpected_objects:
            problems.append(
                "objetos nao versionados: "
                + ", ".join(
                    f"{kind}:{name}" for kind, name, _table in sorted(unexpected_objects)
                )
            )

        for table, expected_columns in _SCHEMA_COLUMNS.items():
            quoted_table = '"' + table.replace('"', '""') + '"'
            actual_columns = tuple(
                (
                    str(row["name"]),
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    row["dflt_value"],
                    int(row["pk"]),
                    int(row["hidden"]),
                )
                for row in self.connection.execute(f"PRAGMA table_xinfo({quoted_table})")
            )
            expected_columns_with_hidden = tuple(
                (*column, 0) for column in expected_columns
            )
            if actual_columns != expected_columns_with_hidden:
                problems.append(
                    f"{table}: colunas divergentes; esperado "
                    f"{expected_columns_with_hidden!r}, "
                    f"encontrado {actual_columns!r}"
                )
                # Indices dependem das colunas. Ainda assim validamos as outras
                # tabelas para produzir um diagnostico unico e acionavel.
                continue

            actual_indexes: list[dict[str, Any]] = []
            for row in self.connection.execute(f"PRAGMA index_list({quoted_table})"):
                index_name = str(row["name"])
                quoted_index = '"' + index_name.replace('"', '""') + '"'
                columns = tuple(
                    str(column[2])
                    for column in self.connection.execute(
                        f"PRAGMA index_info({quoted_index})"
                    )
                )
                key_details = tuple(
                    (
                        str(column[2]),
                        int(column[3]),
                        str(column[4]).upper(),
                    )
                    for column in self.connection.execute(
                        f"PRAGMA index_xinfo({quoted_index})"
                    )
                    if int(column[5]) == 1
                )
                actual_indexes.append(
                    {
                        "name": index_name,
                        "columns": columns,
                        "unique": int(row["unique"]),
                        "origin": str(row["origin"]),
                        "partial": int(row["partial"]),
                        "key_details": key_details,
                    }
                )

            if len(actual_indexes) != len(_SCHEMA_INDEXES[table]):
                problems.append(
                    f"{table}: quantidade de indices divergente; esperado "
                    f"{len(_SCHEMA_INDEXES[table])}, encontrado {len(actual_indexes)}"
                )
            for name, columns, unique, origin, partial in _SCHEMA_INDEXES[table]:
                expected_key_details = tuple(
                    (column, 0, "BINARY") for column in columns
                )
                match = next(
                    (
                        index
                        for index in actual_indexes
                        if (name is None or index["name"] == name)
                        and index["columns"] == columns
                        and index["unique"] == unique
                        and index["origin"] == origin
                        and index["partial"] == partial
                        and index["key_details"] == expected_key_details
                    ),
                    None,
                )
                if match is None:
                    label = name or f"{origin}:{','.join(columns)}"
                    problems.append(f"{table}: indice requerido ausente/divergente: {label}")

            foreign_key_rows = list(
                self.connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
            )
            grouped_foreign_keys: dict[int, list[sqlite3.Row]] = {}
            for row in foreign_key_rows:
                grouped_foreign_keys.setdefault(int(row["id"]), []).append(row)
            actual_foreign_keys = []
            for rows in grouped_foreign_keys.values():
                ordered = sorted(rows, key=lambda row: int(row["seq"]))
                first = ordered[0]
                actual_foreign_keys.append(
                    (
                        str(first["table"]),
                        tuple(str(row["from"]) for row in ordered),
                        tuple(str(row["to"]) for row in ordered),
                        str(first["on_update"]),
                        str(first["on_delete"]),
                        str(first["match"]),
                    )
                )
            if sorted(actual_foreign_keys) != sorted(_SCHEMA_FOREIGN_KEYS[table]):
                problems.append(
                    f"{table}: chaves estrangeiras divergentes; esperado "
                    f"{_SCHEMA_FOREIGN_KEYS[table]!r}, encontrado "
                    f"{tuple(actual_foreign_keys)!r}"
                )

            table_sql_row = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            table_sql = "" if table_sql_row is None else str(table_sql_row[0] or "")
            if re.search(r"\b(?:collate|check|on\s+conflict|without\s+rowid|strict)\b", table_sql, re.I):
                problems.append(
                    f"{table}: DDL contem semantica nao versionada"
                )

        index_sql_row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_execucao_tabela_destino'"
        ).fetchone()
        index_sql = "" if index_sql_row is None or index_sql_row[0] is None else str(index_sql_row[0])
        normalized_index_sql = re.sub(r'[\s"\[\]`;]+', "", index_sql).casefold()
        expected_index_sql = (
            "createuniqueindexux_execucao_tabela_destinoonexecucao_tabela"
            "(execution_id,destination_area,destination_schema,destination_table)"
            "wheredestination_tableisnotnull"
        )
        if normalized_index_sql != expected_index_sql:
            problems.append(
                "execucao_tabela: predicado do indice ux_execucao_tabela_destino divergente"
            )

        foreign_key_violations = list(
            self.connection.execute("PRAGMA foreign_key_check")
        )
        if foreign_key_violations:
            problems.append(
                f"integridade referencial divergente: {len(foreign_key_violations)} violacao(oes)"
            )

        meta_rows = list(self.connection.execute("SELECT key,value FROM metadados"))
        if (
            len(meta_rows) != 1
            or str(meta_rows[0]["key"]) != "created_at"
            or not str(meta_rows[0]["value"]).strip()
        ):
            problems.append("metadados: conteudo de versao divergente")

        if problems:
            raise StateVersionError(
                "Controle local V2 incompativel; nenhuma migracao foi executada: "
                + " | ".join(problems)
            )

    def start_execution(
        self,
        execution_id: str,
        dataset_id: str,
        command: str,
        structural_hash: str,
        operational_hash: str,
        import_enabled: bool,
        config_path: str | None = None,
    ) -> None:
        validate_canonical_uuid(execution_id, "execution_id")
        validate_sha256_hex(dataset_id, "dataset_id")
        validate_sha256_hex(structural_hash, "structural_hash")
        validate_sha256_hex(operational_hash, "operational_hash")
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO execucao
                (execution_id,dataset_id,command,config_path,structural_hash,operational_hash,
                 import_enabled,status,started_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'RUNNING',?,?)""",
                (execution_id, dataset_id, command, config_path, structural_hash,
                 operational_hash, int(import_enabled), now, now),
            )

    def resume_execution(self, execution_id: str, structural_hash: str, operational_hash: str) -> dict[str, Any]:
        validate_canonical_uuid(execution_id, "execution_id")
        validate_sha256_hex(structural_hash, "structural_hash")
        validate_sha256_hex(operational_hash, "operational_hash")
        row = self.connection.execute(
            "SELECT * FROM execucao WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Execução local não encontrada: {execution_id}")
        if row["structural_hash"] != structural_hash:
            raise StructuralResumeError(
                "Retomada bloqueada: origem/layout/projeção/marca d'água mudou; "
                "credenciais e opções operacionais não participam desse hash."
            )
        self.connection.execute(
            "UPDATE execucao SET operational_hash=?,updated_at=? WHERE execution_id=?",
            (operational_hash, utc_now(), execution_id),
        )
        return dict(row)

    def register_table(
        self,
        execution_id: str,
        table_id: str,
        ordinal: int,
        source_schema: str,
        source_table: str,
        destination_area: str | None,
        destination_schema: str | None,
        destination_table: str | None,
        structural_hash: str,
        watermark: Any = None,
    ) -> None:
        validate_canonical_uuid(execution_id, "execution_id")
        validate_sha256_hex(table_id, "table_id")
        validate_sha256_hex(structural_hash, "structural_hash")
        self.connection.execute(
            """INSERT INTO execucao_tabela
            (execution_id,table_id,ordinal,source_schema,source_table,destination_area,
             destination_schema,destination_table,structural_hash,watermark_json,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING',?)
            ON CONFLICT(execution_id,table_id) DO NOTHING""",
            (execution_id, table_id, ordinal, source_schema, source_table,
             destination_area, destination_schema, destination_table, structural_hash,
             stable_json(watermark) if watermark is not None else None, utc_now()),
        )

    def refresh_unstarted_table(
        self,
        execution_id: str,
        table_id: str,
        *,
        destination_area: str | None,
        destination_schema: str | None,
        destination_table: str | None,
        structural_hash: str,
        watermark: Any = None,
    ) -> None:
        """Atualiza o contrato de uma tabela somente antes de qualquer progresso.

        Esta operacao permite registrar uma tabela provisoriamente antes da
        descoberta e, depois, persistir layout/marca/destino resolvidos. Ela
        nunca reinterpreta blocos ou checkpoints ja existentes.
        """

        validate_canonical_uuid(execution_id, "execution_id")
        validate_sha256_hex(table_id, "table_id")
        validate_sha256_hex(structural_hash, "structural_hash")
        with self.transaction() as db:
            row = db.execute(
                """SELECT tr.*,
                       EXISTS(
                         SELECT 1 FROM execucao_lote AS b
                         WHERE b.execution_id=tr.execution_id AND b.table_id=tr.table_id
                       ) AS has_blocks
                FROM execucao_tabela AS tr
                WHERE tr.execution_id=? AND tr.table_id=?""",
                (execution_id, table_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Tabela local nao encontrada: {execution_id}/{table_id}")
            status = str(row["status"])
            refreshable_status = (
                status == "PENDING"
                or status.startswith("SKIPPED_")
                or status.endswith("_ERROR")
            )
            has_progress = (
                bool(row["has_blocks"])
                or row["final_limit_json"] is not None
                or row["export_cursor_json"] is not None
                or row["import_cursor_json"] is not None
                or int(row["rows_exported"] or 0) != 0
                or int(row["rows_imported"] or 0) != 0
                or int(row["bytes_exported"] or 0) != 0
            )
            if not refreshable_status or has_progress:
                raise StructuralResumeError(
                    "Contrato da tabela nao pode ser atualizado depois do inicio "
                    "de blocos/checkpoints ou em estado duravel"
                )
            db.execute(
                """UPDATE execucao_tabela
                SET destination_area=?,destination_schema=?,destination_table=?,
                    structural_hash=?,watermark_json=?,status='PENDING',
                    index_state='NOT_STARTED',reason=NULL,warnings_json='[]',
                    started_at=NULL,finished_at=NULL,updated_at=?
                WHERE execution_id=? AND table_id=?""",
                (
                    destination_area,
                    destination_schema,
                    destination_table,
                    structural_hash,
                    stable_json(watermark) if watermark is not None else None,
                    utc_now(),
                    execution_id,
                    table_id,
                ),
            )

    def table(self, execution_id: str, table_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM execucao_tabela WHERE execution_id=? AND table_id=?",
            (execution_id, table_id),
        ).fetchone()
        return None if row is None else dict(row)

    def blocks(self, execution_id: str, table_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM execucao_lote WHERE execution_id=? AND table_id=? ORDER BY block_number",
            (execution_id, table_id),
        )]

    def next_block_number(self, execution_id: str, table_id: str) -> int:
        value = self.connection.execute(
            "SELECT COALESCE(MAX(block_number),0)+1 FROM execucao_lote WHERE execution_id=? AND table_id=?",
            (execution_id, table_id),
        ).fetchone()[0]
        return int(value)

    def update_table(self, execution_id: str, table_id: str, **values: Any) -> None:
        allowed = {
            "final_limit_json", "export_cursor_json", "import_cursor_json", "status",
            "rows_exported", "rows_imported", "bytes_exported", "index_state", "reason",
            "warnings_json", "started_at", "finished_at",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("Campos de tabela inválidos: " + ", ".join(sorted(unknown)))
        if not values:
            return
        if "status" in values and values["status"] not in {
            item.value for item in TableStatus
        }:
            raise ValueError("status da tabela não pertence ao contrato V2")
        if "index_state" in values and values["index_state"] not in {
            item.value for item in IndexState
        }:
            raise ValueError("index_state não pertence ao contrato V2")
        values["updated_at"] = utc_now()
        sql = ",".join(f"{key}=?" for key in values)
        params = tuple(values.values()) + (execution_id, table_id)
        self.connection.execute(
            f"UPDATE execucao_tabela SET {sql} WHERE execution_id=? AND table_id=?", params
        )

    def plan_block(
        self,
        execution_id: str,
        table_id: str,
        block_number: int,
        lower_bound: list[str] | None,
        upper_bound: list[str],
        final_limit: list[str] | None,
    ) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM execucao_lote WHERE execution_id=? AND table_id=? AND block_number=?",
            (execution_id, table_id, block_number),
        ).fetchone()
        expected = (
            stable_json(lower_bound) if lower_bound is not None else None,
            stable_json(upper_bound),
            stable_json(final_limit) if final_limit is not None else None,
        )
        if existing:
            current = (
                existing["lower_bound_json"],
                existing["upper_bound_json"],
                existing["final_limit_json"],
            )
            if current != expected:
                raise StructuralResumeError(
                    "Limites de bloco publicado/planejado e teto final "
                    "não podem ser redimensionados"
                )
            return dict(existing)
        block_id = str(
            uuid.uuid5(
                uuid.UUID(execution_id),
                f"{table_id}:{block_number}:{expected[0]}:{expected[1]}:{expected[2]}",
            )
        )
        self.connection.execute(
            """INSERT INTO execucao_lote
            (execution_id,table_id,block_id,block_number,lower_bound_json,upper_bound_json,
             final_limit_json,status) VALUES(?,?,?,?,?,?,?,'PLANNED')""",
            (execution_id, table_id, block_id, block_number, expected[0], expected[1], expected[2]),
        )
        return dict(self.connection.execute(
            "SELECT * FROM execucao_lote WHERE execution_id=? AND table_id=? AND block_id=?",
            (execution_id, table_id, block_id),
        ).fetchone())

    def begin_attempt(self, execution_id: str, table_id: str, block_id: str, log_path: str | None) -> int:
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts,status FROM execucao_lote WHERE execution_id=? AND table_id=? AND block_id=?",
                (execution_id, table_id, block_id),
            ).fetchone()
            if row is None:
                raise KeyError("Bloco não encontrado")
            if row["status"] in {"EXPORTED", "IMPORTED", "EMPTY_CONFIRMED"}:
                raise RuntimeError("Bloco durável não pode iniciar nova exportação")
            attempt = int(row["attempts"]) + 1
            now = utc_now()
            # Starting a retry proves that a prior unfinished attempt was
            # interrupted before its outcome could be persisted.
            db.execute(
                """UPDATE tentativa_lote
                SET status='INTERRUPTED',finished_at=?,error_code='EXECUTOR_INTERRUPTED',
                    error_message='Nova tentativa iniciada antes do encerramento da anterior'
                WHERE execution_id=? AND table_id=? AND block_id=?
                  AND status='RUNNING' AND finished_at IS NULL""",
                (now, execution_id, table_id, block_id),
            )
            db.execute(
                """UPDATE execucao_lote
                SET attempts=?,status='EXPORTING',error_code=NULL,error_message=NULL
                WHERE execution_id=? AND table_id=? AND block_id=?""",
                (attempt, execution_id, table_id, block_id),
            )
            db.execute(
                """INSERT INTO tentativa_lote
                (execution_id,table_id,block_id,attempt,status,started_at,log_path)
                VALUES(?,?,?,?, 'RUNNING',?,?)""",
                (execution_id, table_id, block_id, attempt, now, log_path),
            )
            return attempt

    def fail_attempt(
        self,
        execution_id: str,
        table_id: str,
        block_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Fecha atomicamente a tentativa corrente e torna o bloco retomavel."""

        if not str(error_code).strip():
            raise ValueError("error_code da tentativa nao pode ser vazio")
        with self.transaction() as db:
            block = db.execute(
                """SELECT status FROM execucao_lote
                WHERE execution_id=? AND table_id=? AND block_id=?""",
                (execution_id, table_id, block_id),
            ).fetchone()
            if block is None:
                raise KeyError("Bloco nao encontrado")
            if block["status"] != "EXPORTING":
                raise RuntimeError(
                    "Somente bloco EXPORTING pode encerrar tentativa com falha"
                )
            active = db.execute(
                """SELECT attempt FROM tentativa_lote
                WHERE execution_id=? AND table_id=? AND block_id=?
                  AND status='RUNNING' AND finished_at IS NULL
                ORDER BY attempt DESC""",
                (execution_id, table_id, block_id),
            ).fetchall()
            if len(active) != 1:
                raise RuntimeError(
                    "Bloco deve possuir exatamente uma tentativa RUNNING para falhar"
                )
            now = utc_now()
            db.execute(
                """UPDATE tentativa_lote
                SET status='FAILED',finished_at=?,error_code=?,error_message=?
                WHERE execution_id=? AND table_id=? AND block_id=? AND attempt=?""",
                (
                    now,
                    error_code,
                    error_message,
                    execution_id,
                    table_id,
                    block_id,
                    active[0]["attempt"],
                ),
            )
            db.execute(
                """UPDATE execucao_lote
                SET status='PLANNED',error_code=?,error_message=?
                WHERE execution_id=? AND table_id=? AND block_id=?""",
                (error_code, error_message, execution_id, table_id, block_id),
            )

    def mark_exported(
        self,
        execution_id: str,
        table_id: str,
        block_id: str,
        manifest_path: str,
        rows: int,
        file_bytes: int,
        data_sha256: str | None,
        format_sha256: str,
        *,
        empty_range: bool = False,
    ) -> None:
        status = "EMPTY_CONFIRMED" if empty_range else "EXPORTED"
        now = utc_now()
        with self.transaction() as db:
            block = db.execute(
                "SELECT attempt FROM tentativa_lote WHERE execution_id=? AND table_id=? AND block_id=? ORDER BY attempt DESC LIMIT 1",
                (execution_id, table_id, block_id),
            ).fetchone()
            if block is None:
                raise RuntimeError("Tentativa de exportação não registrada")
            db.execute(
                """UPDATE execucao_lote SET status=?,empty_range=?,rows_exported=?,file_bytes=?,
                data_sha256=?,format_sha256=?,manifest_path=?,exported_at=?
                WHERE execution_id=? AND table_id=? AND block_id=?""",
                (status, int(empty_range), rows, file_bytes, data_sha256, format_sha256,
                 manifest_path, now, execution_id, table_id, block_id),
            )
            db.execute(
                """UPDATE tentativa_lote SET status='COMPLETED',finished_at=?
                WHERE execution_id=? AND table_id=? AND block_id=? AND attempt=?""",
                (now, execution_id, table_id, block_id, block["attempt"]),
            )
            upper = db.execute(
                "SELECT upper_bound_json FROM execucao_lote WHERE execution_id=? AND table_id=? AND block_id=?",
                (execution_id, table_id, block_id),
            ).fetchone()[0]
            db.execute(
                """UPDATE execucao_tabela SET export_cursor_json=?,rows_exported=rows_exported+?,
                bytes_exported=bytes_exported+?,updated_at=? WHERE execution_id=? AND table_id=?""",
                (upper, rows, file_bytes, now, execution_id, table_id),
            )

    @staticmethod
    def _recompute_table_progress(
        db: sqlite3.Connection,
        execution_id: str,
        table_id: str,
        now: str,
    ) -> None:
        """Reconstrói os totais da tabela a partir dos blocos duráveis.

        A importação portátil pode começar em um executor cujo SQLite ainda
        não conhece os blocos produzidos em outro host. Recalcular, em vez de
        incrementar, torna a reconciliação repetível e também corrige uma
        interrupção ocorrida entre a persistência do bloco e do agregado.
        """

        durable = "'EXPORTED','IMPORTING','IMPORTED','EMPTY_CONFIRMED'"
        totals = db.execute(
            f"""SELECT
                  COALESCE(SUM(CASE WHEN status IN ({durable})
                                    THEN COALESCE(rows_exported,0) ELSE 0 END),0),
                  COALESCE(SUM(CASE WHEN status='IMPORTED'
                                    THEN COALESCE(rows_imported,0) ELSE 0 END),0),
                  COALESCE(SUM(CASE WHEN status IN ({durable})
                                    THEN COALESCE(file_bytes,0) ELSE 0 END),0)
                FROM execucao_lote WHERE execution_id=? AND table_id=?""",
            (execution_id, table_id),
        ).fetchone()
        exported_cursor = db.execute(
            f"""SELECT upper_bound_json FROM execucao_lote
                WHERE execution_id=? AND table_id=? AND status IN ({durable})
                ORDER BY block_number DESC LIMIT 1""",
            (execution_id, table_id),
        ).fetchone()
        imported_cursor = db.execute(
            """SELECT upper_bound_json FROM execucao_lote
               WHERE execution_id=? AND table_id=? AND status='IMPORTED'
               ORDER BY block_number DESC LIMIT 1""",
            (execution_id, table_id),
        ).fetchone()
        db.execute(
            """UPDATE execucao_tabela
               SET export_cursor_json=?,import_cursor_json=?,rows_exported=?,
                   rows_imported=?,bytes_exported=?,updated_at=?
               WHERE execution_id=? AND table_id=?""",
            (
                None if exported_cursor is None else exported_cursor[0],
                None if imported_cursor is None else imported_cursor[0],
                int(totals[0]),
                int(totals[1]),
                int(totals[2]),
                now,
                execution_id,
                table_id,
            ),
        )

    def reconcile_manifest_block(
        self,
        execution_id: str,
        table_id: str,
        block_id: str,
        block_number: int,
        lower_bound: list[str] | None,
        upper_bound: list[str],
        final_limit: list[str] | None,
        manifest_path: str,
        rows: int,
        file_bytes: int,
        data_sha256: str | None,
        format_sha256: str,
        *,
        empty_range: bool = False,
        exported_at: str | None = None,
    ) -> None:
        """Materializa no controle local um manifesto verificado em outro host.

        Identidade, limites, contagens e hashes são imutáveis. Uma repetição
        idêntica apenas atualiza o caminho local do manifesto e recompõe os
        agregados; conflito por UUID ou número do bloco interrompe a retomada.
        """

        validate_canonical_uuid(execution_id, "execution_id")
        validate_canonical_uuid(block_id, "block_id")
        validate_sha256_hex(table_id, "table_id")
        if data_sha256 is not None:
            validate_sha256_hex(data_sha256, "data_sha256")
        validate_sha256_hex(format_sha256, "format_sha256")
        if block_number < 1 or rows < 0 or file_bytes < 0:
            raise ValueError("Metadados numéricos do manifesto são inválidos")
        if empty_range and (rows != 0 or file_bytes != 0 or data_sha256 is not None):
            raise ValueError("Manifesto de faixa vazia possui dados incompatíveis")

        lower_json = stable_json(lower_bound) if lower_bound is not None else None
        upper_json = stable_json(upper_bound)
        final_json = stable_json(final_limit) if final_limit is not None else None
        now = utc_now()
        durable_status = "EMPTY_CONFIRMED" if empty_range else "EXPORTED"

        with self.transaction() as db:
            table = db.execute(
                """SELECT final_limit_json FROM execucao_tabela
                   WHERE execution_id=? AND table_id=?""",
                (execution_id, table_id),
            ).fetchone()
            if table is None:
                raise KeyError(f"Tabela local não encontrada: {execution_id}/{table_id}")
            if table["final_limit_json"] is not None and table["final_limit_json"] != final_json:
                raise StructuralResumeError(
                    "Teto final do manifesto diverge do controle local"
                )

            matches = db.execute(
                """SELECT * FROM execucao_lote
                   WHERE execution_id=? AND table_id=?
                     AND (block_id=? OR block_number=?)""",
                (execution_id, table_id, block_id, block_number),
            ).fetchall()
            if len(matches) > 1:
                raise StructuralResumeError(
                    "UUID e número do manifesto pertencem a blocos locais diferentes"
                )

            if matches:
                current = matches[0]
                identity = {
                    "block_id": block_id,
                    "block_number": block_number,
                    "lower_bound_json": lower_json,
                    "upper_bound_json": upper_json,
                    "final_limit_json": final_json,
                }
                if any(current[name] != expected for name, expected in identity.items()):
                    raise StructuralResumeError(
                        "Manifesto diverge da identidade ou dos limites do bloco local"
                    )
                durable = current["status"] in {
                    "EXPORTED", "IMPORTING", "IMPORTED", "EMPTY_CONFIRMED"
                }
                immutable = {
                    "rows_exported": rows,
                    "file_bytes": file_bytes,
                    "data_sha256": data_sha256,
                    "format_sha256": format_sha256,
                }
                if durable and (
                    int(current["empty_range"] or 0) != int(empty_range)
                    or any(current[name] != expected for name, expected in immutable.items())
                ):
                    raise StructuralResumeError(
                        "Contagens ou hashes do manifesto divergem do bloco local durável"
                    )
                next_status = "IMPORTED" if current["status"] == "IMPORTED" else durable_status
                db.execute(
                    """UPDATE execucao_lote SET status=?,empty_range=?,rows_exported=?,file_bytes=?,
                       data_sha256=?,format_sha256=?,manifest_path=?,
                       exported_at=COALESCE(exported_at,?),error_code=NULL,error_message=NULL
                       WHERE execution_id=? AND table_id=? AND block_id=?""",
                    (
                        next_status,
                        int(empty_range),
                        rows,
                        file_bytes,
                        data_sha256,
                        format_sha256,
                        manifest_path,
                        exported_at or now,
                        execution_id,
                        table_id,
                        block_id,
                    ),
                )
            else:
                db.execute(
                    """INSERT INTO execucao_lote
                       (execution_id,table_id,block_id,block_number,lower_bound_json,
                        upper_bound_json,final_limit_json,status,empty_range,rows_exported,
                        file_bytes,data_sha256,format_sha256,manifest_path,exported_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        execution_id,
                        table_id,
                        block_id,
                        block_number,
                        lower_json,
                        upper_json,
                        final_json,
                        durable_status,
                        int(empty_range),
                        rows,
                        file_bytes,
                        data_sha256,
                        format_sha256,
                        manifest_path,
                        exported_at or now,
                    ),
                )

            if table["final_limit_json"] is None:
                db.execute(
                    """UPDATE execucao_tabela SET final_limit_json=?
                       WHERE execution_id=? AND table_id=?""",
                    (final_json, execution_id, table_id),
                )
            self._recompute_table_progress(db, execution_id, table_id, now)

    def mark_imported(self, execution_id: str, table_id: str, block_id: str, rows: int) -> None:
        now = utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT status,upper_bound_json,rows_exported,rows_imported FROM execucao_lote WHERE execution_id=? AND table_id=? AND block_id=?",
                (execution_id, table_id, block_id),
            ).fetchone()
            if row is not None and row["status"] == "IMPORTED":
                if int(row["rows_imported"] or 0) != rows:
                    raise RuntimeError("Bloco já importado possui contagem divergente")
                self._recompute_table_progress(db, execution_id, table_id, now)
                return
            if row is None or row["status"] not in {"EXPORTED", "EMPTY_CONFIRMED", "IMPORTING"}:
                raise RuntimeError("Bloco não está pronto para confirmação de importação")
            if rows != int(row["rows_exported"] or 0):
                raise RuntimeError("Contagem importada diverge do manifesto local")
            db.execute(
                """UPDATE execucao_lote SET status='IMPORTED',rows_imported=?,imported_at=?
                WHERE execution_id=? AND table_id=? AND block_id=?""",
                (rows, now, execution_id, table_id, block_id),
            )
            self._recompute_table_progress(db, execution_id, table_id, now)

    def execution(self, execution_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM execucao WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tables"] = [dict(item) for item in self.connection.execute(
            "SELECT * FROM execucao_tabela WHERE execution_id=? ORDER BY ordinal", (execution_id,)
        )]
        return result

    def pending_manifests(self, execution_id: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            """SELECT manifest_path FROM execucao_lote WHERE execution_id=? AND status IN ('EXPORTED','IMPORTING')
            AND manifest_path IS NOT NULL ORDER BY table_id,block_number""", (execution_id,)
        )]

    def finish_execution(self, execution_id: str, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
        if status not in {item.value for item in ExecutionState}:
            raise ValueError("status da execução não pertence ao contrato V2")
        now = utc_now()
        self.connection.execute(
            """UPDATE execucao SET status=?,finished_at=?,updated_at=?,error_code=?,error_message=?
            WHERE execution_id=?""",
            (status, now, now, error_code, error_message, execution_id),
        )


_LEGACY_TABLE_KEYS = {
    "meta": ("key",),
    "executions": ("execution_id",),
    "table_runs": ("execution_id", "table_id"),
    "blocks": ("execution_id", "table_id", "block_id"),
    "attempts": ("execution_id", "table_id", "block_id", "attempt"),
}
_CURRENT_TABLE_KEYS = {
    "metadados": ("key",),
    "execucao": ("execution_id",),
    "execucao_tabela": ("execution_id", "table_id"),
    "execucao_lote": ("execution_id", "table_id", "block_id"),
    "tentativa_lote": ("execution_id", "table_id", "block_id", "attempt"),
}
_LEGACY_TO_CURRENT_TABLE = {
    "meta": "metadados",
    "executions": "execucao",
    "table_runs": "execucao_tabela",
    "blocks": "execucao_lote",
    "attempts": "tentativa_lote",
}


def _quoted_local_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _control_content_fingerprint(
    connection: sqlite3.Connection,
    table_keys: dict[str, tuple[str, ...]],
) -> str:
    """Hash all durable rows independently from database page layout."""

    digest = hashlib.sha256()
    for table, keys in table_keys.items():
        quoted_table = _quoted_local_identifier(table)
        columns = tuple(
            str(row[1])
            for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
            if int(row[6]) == 0
        )
        if not columns:
            raise StateVersionError(
                f"Controle local legado incompativel: tabela ausente {table}"
            )
        order_by = ",".join(_quoted_local_identifier(key) for key in keys)
        digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
        for row in connection.execute(
            f"SELECT * FROM {quoted_table} ORDER BY {order_by}"
        ):
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source, timeout=30, isolation_level=None)) as source_db:
        with closing(
            sqlite3.connect(destination, timeout=30, isolation_level=None)
        ) as destination_db:
            source_db.backup(destination_db)
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())


def _sync_local_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_current_control(path: Path) -> str:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        state = object.__new__(LocalState)
        state.directory = path.parent
        state.path = path
        state.connection = connection
        state._validate_schema()
        return _control_content_fingerprint(connection, _CURRENT_TABLE_KEYS)
    finally:
        connection.close()


def _migrate_v4_control_copy(source: Path, target: Path) -> Path:
    """Publish a validated v5 copy while preserving the v4 source."""

    candidate = target.with_name(
        f".{target.name}.v5.migrating.{uuid.uuid4().hex}"
    )
    backup: Path | None = None
    try:
        _sqlite_backup(source, candidate)
        with closing(sqlite3.connect(candidate, timeout=30, isolation_level=None)) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version != LEGACY_SCHEMA_VERSION:
                raise StateVersionError(
                    "Migracao automatica de nomenclatura aceita somente o controle "
                    f"SQLite v{LEGACY_SCHEMA_VERSION}; encontrada versao {version}. "
                    "O arquivo original foi preservado."
                )
            initial_fingerprint = _control_content_fingerprint(
                db, _LEGACY_TABLE_KEYS
            )
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DROP INDEX ux_table_destination")
                for old_name, new_name in _LEGACY_TO_CURRENT_TABLE.items():
                    db.execute(
                        f"ALTER TABLE {_quoted_local_identifier(old_name)} "
                        f"RENAME TO {_quoted_local_identifier(new_name)}"
                    )
                db.execute(
                    "CREATE UNIQUE INDEX ux_execucao_tabela_destino "
                    "ON execucao_tabela(execution_id,destination_area,"
                    "destination_schema,destination_table) "
                    "WHERE destination_table IS NOT NULL"
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

        migrated_fingerprint = _validate_current_control(candidate)
        if migrated_fingerprint != initial_fingerprint:
            raise StateVersionError(
                "Migracao do controle SQLite alterou o conteudo logico; "
                "o arquivo original foi preservado."
            )

        # If the source already has the new filename, retain an explicit v4
        # backup before atomically replacing it. When names differ, the legacy
        # file itself is the immutable recovery copy.
        if source == target:
            backup = source.with_name(source.name + ".v4.backup")
            if backup.exists():
                backup = source.with_name(
                    source.name + f".v4.backup.{uuid.uuid4().hex}"
                )
            _sqlite_backup(source, backup)
            _sync_local_directory(backup.parent)

        with closing(sqlite3.connect(source, timeout=30, isolation_level=None)) as db:
            current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            current_fingerprint = _control_content_fingerprint(
                db, _LEGACY_TABLE_KEYS
            )
        if (
            current_version != LEGACY_SCHEMA_VERSION
            or current_fingerprint != initial_fingerprint
        ):
            raise StateVersionError(
                "Controle SQLite mudou durante a migracao; tente novamente com "
                "o motor parado. O original foi preservado."
            )

        os.replace(candidate, target)
        _sync_local_directory(target.parent)
        _validate_current_control(target)
        return target
    finally:
        if candidate.exists():
            candidate.unlink()


def _copy_current_legacy_name(source: Path, target: Path) -> Path:
    """Adopt a v5 database still carrying the legacy filename."""

    candidate = target.with_name(
        f".{target.name}.copying.{uuid.uuid4().hex}"
    )
    try:
        _sqlite_backup(source, candidate)
        copied_fingerprint = _validate_current_control(candidate)
        source_fingerprint = _validate_current_control(source)
        if copied_fingerprint != source_fingerprint:
            raise StateVersionError(
                "Controle SQLite mudou durante a copia; o original foi preservado."
            )
        os.replace(candidate, target)
        _sync_local_directory(target.parent)
        return target
    finally:
        if candidate.exists():
            candidate.unlink()


def _prepare_control_path(directory: Path) -> Path:
    target = directory / CONTROL_FILE_NAME
    legacy = directory / LEGACY_CONTROL_FILE_NAME

    if target.exists():
        with closing(sqlite3.connect(target, timeout=30, isolation_level=None)) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if version == LEGACY_SCHEMA_VERSION:
            return _migrate_v4_control_copy(target, target)
        if version == 0 and legacy.exists():
            raise StateVersionError(
                f"Controle vazio {target} conflita com o legado {legacy}; "
                "nenhum arquivo foi alterado. Remova ou renomeie o arquivo vazio "
                "apos verificar o legado."
            )
        return target

    if not legacy.exists():
        return target

    with closing(sqlite3.connect(legacy, timeout=30, isolation_level=None)) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version == LEGACY_SCHEMA_VERSION:
        return _migrate_v4_control_copy(legacy, target)
    if version == SCHEMA_VERSION:
        return _copy_current_legacy_name(legacy, target)
    raise StateVersionError(
        f"Controle legado {legacy} usa versao {version}. Execute a migracao "
        "explicita antes de iniciar o motor; nenhum novo controle foi criado."
    )
