**English** | [Português (Brasil)](MIGRACAO_CONTROLE_SQLITE.pt-BR.md)

# Nondestructive migration of legacy SQLite control state

Current local control state is persistent and uses the
`controle_transferencia.sqlite3` file. Its physical signature contains only
Portuguese names:

- `metadados`;
- `execucao`;
- `execucao_tabela`;
- `execucao_lote`;
- `tentativa_lote`.

The local version is `PRAGMA user_version=5`. `versao_esquema` is the technical
version table for SQL Server control state; it is neither a SQLite table nor a
SQLite key.

## Automatic migration from legacy v4

When control state is opened, the engine recognizes only the exact v4
signature. If the legacy `bcp_control_v2.sqlite3` file is present in the
directory, the engine publishes a validated v5 copy as
`controle_transferencia.sqlite3` and preserves the legacy file unchanged. If a
v4 layout already uses the new filename, the routine creates
`controle_transferencia.sqlite3.v4.backup` before the atomic replacement.

Reopening a v5 layout is idempotent. Versions 0, 1, and 2, a future version, or
an unknown, partial, or tampered structure fail closed without publishing or
modifying control state.

## Explicit migration from legacy v3

For v3, stop every process that uses the directory and run:

```powershell
python .\bcp_bronze.py migrate-control `
  --control-directory C:\path\to\control
```

```bash
python3 ./bcp_bronze.py migrate-control \
  --control-directory /path/to/control
```

To select the v3 backup location, add `--backup-path PATH`. The destination
cannot be the source file or `controle_transferencia.sqlite3`, and it is never
overwritten.

The `migrate-control` command converts v3 directly to v5 and preserves the
complete v3 backup, including the legacy `index_states` table. A backup is
never overwritten; if the selected path already contains another file, the
operation fails without modifying the source.

## Common guarantees

The routine recognizes the supported legacy signature, copies every record to
a new candidate, validates logical equivalence, and only then publishes
`controle_transferencia.sqlite3`. The legacy source is neither deleted nor
overwritten. An unknown, partial, or tampered structure fails closed without
publishing an incomplete destination.

Before publication, the routine:

1. opens the legacy file in a controlled mode and validates its complete
   signature;
2. creates the candidate with `metadados`, `execucao`, `execucao_tabela`,
   `execucao_lote`, and `tentativa_lote`;
3. converts names and relationships without changing UUIDs, checkpoints,
   counts, cursors, attempts, or states;
4. runs `PRAGMA integrity_check` and compares counts and keys between the source
   and candidate;
5. publishes the new file through atomic replacement and retains the legacy
   file for audit and recovery.

If `controle_transferencia.sqlite3` already exists, it must have the exact v5
signature and represent the same state. Running the process again after a
completed migration is idempotent. A mismatch between legacy and destination
state is an error, and no file is repaired by assumption.

## Legacy names

Only this section mentions the previous identifiers so an old installation can
be located and audited. The file was named `bcp_control_v2.sqlite3`, with the
internal tables `meta`, `executions`, `table_runs`, `blocks`, and `attempts`;
still older versions could also contain `index_states`. These names are not
used by the new structure and must not be created manually.

After migration, retain the legacy file throughout the validation period. Do
not rename it to the new filename: the engine distinguishes contracts by their
physical signatures, not only by filenames.

## Previous configurations

The command migrates only local SQLite state. It does not convert JSON
configuration files or persistent destination SQL objects. Recreate older
configurations from the current examples and validate them with `plan`. In
Bronze SQL Server, the current contract uses only `DBRO684.dbo.execucao`,
`DBRO684.dbo.execucao_tabela`, `DBRO684.dbo.execucao_lote`, and the technical
table `DBRO684.dbo.versao_esquema`. A legacy SQL schema may be removed only
during an explicit administrative migration; the engine never deletes history
automatically. The preservation rule on this page continues to apply to SQLite
files, including legacy `bcp_control_v2.sqlite3`. Landing does not receive
control tables.
