**English** | [Português (Brasil)](USO_CLI_POWERSHELL_LINUX.pt-BR.md)

# Using the CLI from PowerShell and a Linux shell

The launchers described on this page invoke the same Python CLI used by the
engine. They can run from any directory, forward every argument without
reconstructing the command line, and return the engine's exit code.

On Windows, `BulkFlowCLI.exe` exposes exactly the same subcommands and already
includes Python and the project's packages. It does not include the ODBC Driver
or BCP; those components remain installed on the executor.

The current contract has one Source with role `data_provider` and at most one
data-capable destination. Exactly one is required when importing; an
export-only configuration may have none. Destination roles are `structure_and_data`,
`structure_only`, and `data_only`; defaults are `structure_and_data` for
`BD_DESTINO_01` and `structure_only` for `BD_DESTINO_02`. Docker and fixed lab
ports are not CLI requirements.

The global `perimeter` parameter accepts exactly `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or
`PRODUÇÃO`; its default is `DESENVOLVIMENTO`. These values suggest `u684`,
`h684`, and `s684`, respectively, as the SQL username, but `source`,
`bronze_destination`, and `landing_destination` retain independent, editable
role, instance, port, database, schema, username, and secret settings. Those
destination keys and the `bronze`/`landing` CLI areas are compatible internal
IDs; roles determine operational behavior.

The global `cdc_retention_minutes` parameter defines the retention period for
the CDC cleanup job on the Source. Its default is `262800` minutes (six months,
approximately 182.5 days). Use an integer from `1` through `52494800`, without
a thousands separator, for example:

```json
{
  "cdc_retention_minutes": 262800,
  "tables": [
    {"source_table": "CLIENTE", "enable_cdc": true}
  ]
}
```

The ODBC driver and BCP are installed on the host that actually runs the CLI,
not on the SQL Server instances. Python and the packages are also installed
when the scripts/launchers are used; `BulkFlowCLI.exe` already includes them.
Before running the examples below, follow the
[Executor prerequisites](PRE_REQUISITOS.md) guide, which also lists the
dependencies of each command.

A configuration saved by the GUI is the same JSON consumed by the CLI. There
is no conversion or additional export step.

The optional `tables[].source_database` and
`tables[].destination_database` fields make the mapping saved by the GUI
explicit, but do not open independent per-table connections in this version.
When present, they must match `source.database` and the endpoint selected by
`active_destination`. The engine rejects a configuration when they
differ. To process another database, use another configuration file and a
separate execution.

## PowerShell

From the project root:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 prerequisites --config .\examples\config.full.json
& .\scripts\launchers\invoke-bcp.ps1 plan --config .\examples\config.full.json
& .\scripts\launchers\invoke-bcp.ps1 ddl --config .\examples\config.full.json --area both --output .\ddl --apply --confirm
& .\scripts\launchers\invoke-bcp.ps1 run --config .\examples\config.full.json --confirm-load
$engineExitCode = $LASTEXITCODE
```

With the binaries distributed under `release/`:

```powershell
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
.\release\BulkFlowCLI.exe plan --config .\config.v2.json
.\release\BulkFlowCLI.exe ddl --config .\config.v2.json --area both --output .\ddl --apply --confirm
.\release\BulkFlowCLI.exe run --config .\config.v2.json --confirm-load
```

When `ddl --output` is omitted, the DDL directory defaults to
`<project root>\Local\BulkFlow\ddl` both from source and from a portable EXE
inside this project's `release` directory. An installed or standalone EXE
uses `%LOCALAPPDATA%\BulkFlow\ddl`. Supplying `--output` explicitly continues
to override this default.

The EXE can be copied to another directory. The V2 schema and the `bronze` and
`landing` profiles are embedded; configuration files, custom profiles, and
operational directories remain external and must use paths valid on the
executor.

To open the graphical interface on Windows, use the same Python environment or
the EXE:

```powershell
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
```

When the JSON uses the `env` provider, capture each password without echoing it
before running commands. The example below keeps separate references even when
the lab uses the same value:

```powershell
$sourceSecure = Read-Host 'Senha SQL da origem' -AsSecureString
$bronzeSecure = Read-Host 'Senha SQL da BD_DESTINO_01' -AsSecureString
$landingSecure = Read-Host 'Senha SQL da BD_DESTINO_02' -AsSecureString
$env:BCP_SOURCE_SQL_PASSWORD = [Net.NetworkCredential]::new('', $sourceSecure).Password
$env:BCP_BRONZE_SQL_PASSWORD = [Net.NetworkCredential]::new('', $bronzeSecure).Password
$env:BCP_LANDING_SQL_PASSWORD = [Net.NetworkCredential]::new('', $landingSecure).Password

# execute plan, ddl, run, resume, import ou status aqui

Remove-Item Env:\BCP_SOURCE_SQL_PASSWORD, Env:\BCP_BRONZE_SQL_PASSWORD, Env:\BCP_LANDING_SQL_PASSWORD
Remove-Variable sourceSecure, bronzeSecure, landingSecure
```

You can also invoke the file by absolute path or with `powershell
-File`/`pwsh -File`. If Python 3.10+ is not on `PATH`, specify only the
executable, with no additional options:

```powershell
$env:BCP_PYTHON = "$PWD\.venv\Scripts\python.exe"
& .\scripts\launchers\invoke-bcp.ps1 --verbose status `
    --config .\config.example.json `
    --execution-id 12345678-1234-5678-9234-567812345678
```

## Linux shell

Linux executor prerequisites:

- Python 3.10+ and the dependencies in `requirements.txt`;
- Microsoft ODBC Driver 18 (`msodbcsql18`) and preferably BCP 18 or later; a
  BCP 17 build is accepted only if it provides `-Y` and `-u`. On supported
  distributions, BCP is provided by `mssql-tools18`;
- SQL authentication through a secret reference for the path qualified in this
  release.

The [`examples/config.linux-sql.json`](../examples/config.linux-sql.json)
example uses only POSIX paths and environment-variable references. Adjust its
endpoints, users, tables, and mounts before execution. In particular:

- `executor_directory` is the writable path seen by the Python process;
- `local_control_directory` is a local, durable path for SQLite;
- `destination_sql_directory` is the destination SQL Server's view of the same
  bytes in `executor_directory`. In containers, these are normally the two
  sides of the same bind mount.

For Linux execution, start from the versionable
[`examples/config.linux-sql.json`](../examples/config.linux-sql.json) example
and adapt its endpoints, credentials, and paths to the actual environment. If
the executor runs in a container, `executor_directory` and
`destination_sql_directory` must represent both sides of the same shared
volume. Keep `local_control_directory` on durable local storage when an
execution must survive container recreation. Without equivalent mounts,
import prevalidation fails before the load.

From the project root:

```sh
./scripts/launchers/invoke-bcp.sh prerequisites --config ./examples/config.linux-sql.json
./scripts/launchers/invoke-bcp.sh plan --config ./examples/config.linux-sql.json
./scripts/launchers/invoke-bcp.sh ddl --config ./examples/config.linux-sql.json --area both --output ./ddl
./scripts/launchers/invoke-bcp.sh run --config ./examples/config.linux-sql.json --confirm-load
engine_exit_code=$?
```

On a Linux desktop with Tk and `DISPLAY` available, the interface can also be
opened with `python3 ./bcp_gui.py` or `python3 ./bcp_gui.py --config
./config.v2.json`. Use the CLI on a server without a graphical session.

If the file is not executable after being copied, run
`chmod +x scripts/launchers/invoke-bcp.sh` once. To select a virtual
environment explicitly:

```sh
BCP_PYTHON="$PWD/.venv/bin/python" \
  ./scripts/launchers/invoke-bcp.sh ddl \
  --config ./examples/config.full.json \
  --area both \
  --output ./ddl
```

For noninteractive SQL authentication, inject the variables referenced in the
JSON through the executor's secret manager. For an interactive Bash session,
one option that keeps the value out of shell history is:

```bash
read -r -s -p 'Senha SQL da origem: ' BCP_SOURCE_SQL_PASSWORD; printf '\n'
export BCP_SOURCE_SQL_PASSWORD
read -r -s -p 'Senha SQL da BD_DESTINO_01: ' BCP_BRONZE_SQL_PASSWORD; printf '\n'
export BCP_BRONZE_SQL_PASSWORD
read -r -s -p 'Senha SQL da BD_DESTINO_02: ' BCP_LANDING_SQL_PASSWORD; printf '\n'
export BCP_LANDING_SQL_PASSWORD
./scripts/launchers/invoke-bcp.sh plan --config ./examples/config.linux-sql.json
unset BCP_SOURCE_SQL_PASSWORD BCP_BRONZE_SQL_PASSWORD BCP_LANDING_SQL_PASSWORD
```

On Linux, the engine omits `-P` and responds to the masked `bcp` password
prompt through a private PTY. The password does not appear in argv, the `bcp`
process environment, a temporary file, or the log; centralized redaction also
covers any accidental utility output. Empty passwords and passwords containing
a NUL or newline are rejected before the process starts. The
`windows_credentials` mode remains Windows-only; integrated authentication on
Linux depends on a qualified Kerberos/ODBC installation and is not assumed by
this example.

Official installation instructions:

- [ODBC and the `sqlcmd`/`bcp` tools on Linux](https://learn.microsoft.com/sql/linux/install-upgrade/setup-tools);
- [complete prerequisite and verification guide for this engine](PRE_REQUISITOS.md);
- [secure `bcp` password prompt without `-P`](https://learn.microsoft.com/sql/tools/bcp-utility#-p-password).

`BCP_PYTHON` accepts the name or path of a single executable. Do not include
options, literal quotation marks, or a command line in that variable.

## Operational contract and secrets

The arguments are those of the Python CLI: `prerequisites`, `plan`, `ddl`,
`run`, `resume`, `import`, `status`, and `migrate-control`. Use `--help` on the
launcher for the main list and `<command> --help` for each operation's
parameters. When used, the global `--verbose` option must appear before the
command.

SQL control in the active data destination is not temporary: the contract uses
`dbo.ctl_exec`, `dbo.ctl_exec_tabela`, `dbo.ctl_exec_lote`, and the technical
table `dbo.ctl_exec_versao`. Local state uses
`controle_transferencia.sqlite3`, `PRAGMA user_version=5`, and the tables
`metadados`, `execucao`, `execucao_tabela`, `execucao_lote`, and
`tentativa_lote`.

On a Windows executor, specify in `artifact_reader_sids` only the specific SIDs
of SQL service accounts that actually need to read the same files. The default
is an empty list; the executor, SYSTEM, and Administrators are already
implicit. Groups such as Everyone, Authenticated Users, and BUILTIN\Users are
rejected. If an SMB connection uses an effective identity other than the local
process SID (for example, a machine account), specify only that trusted SID in
`artifact_writer_sids`; it receives full control and becomes part of the trust
boundary. Share/UNC permissions remain an external configuration, and third
parties must not be able to replace the parent of `executor_directory`.

On POSIX, use a dedicated executor-owned directory without group/other write
access. If SQL uses another UID, pre-provision a shared GID and `setgid`, for
example mode `2750`; published files use `0440`. The engine preserves the
`setgid` bit but does not run `chgrp`. The executor and SQL must see the same
GID/ACL, including through a bind mount. CIFS/NFS storage that ignores or
rejects `chmod`, ownership, locks, or rename semantics fails closed and must be
qualified in the actual environment.

The launchers do not print the argument list. Even so, never pass a password,
token, or other secret on the command line: it may remain visible in shell
history and the process list. The CLI has no password parameter. Configure
only a secret reference in the JSON (`prompt`, `env`, or
`windows_credential_manager`), as shown in the project's authentication
examples. The last provider is Windows-only. An environment variable is not
encryption; limit its lifetime and remove it after execution.

In the lab, all three endpoints use `u684`, but they do not share one reference
by contract: use `BCP_SOURCE_SQL_PASSWORD`, `BCP_BRONZE_SQL_PASSWORD`, and
`BCP_LANDING_SQL_PASSWORD`. In actual environments, each reference may resolve
to an entirely different password.

Exit codes are preserved without conversion:

- `0`: success;
- `1`: global failure or interrupted execution;
- `2`: partial result, state not yet complete, or invalid arguments;
- `127`: the launcher itself could not find Python or the CLI entry point.

For `prerequisites`, code `2` also indicates that the configured ODBC Driver or
a compatible BCP was not found. The command prints a JSON report and does not
open a SQL Server connection.

## Operational sequence

1. Run `prerequisites`.
2. Run `plan` and review the effective Source, `BD_DESTINO_01`, and
   `BD_DESTINO_02` role, username, instance, port, and database, as well as each
   table's strategy and active destination.
3. Generate and review `ddl`; apply it with `--apply --confirm` only to areas
   whose destination role includes structure.
4. Run `run --confirm-load` and retain the printed `execution_id`.
5. After a disruption, use `resume` with the same UUID.

With `create_structure_if_needed=true`—the default—a `structure_and_data`
destination is created or completed idempotently during `run`; with `false`,
the engine only validates the existing structure. `data_only` always requires
a compatible existing layout and prohibits creation, evolution, and indexes.
The explicit DDL step is recommended for every structure-capable destination.
The full sequence is documented in
[Processing sequence](ORDEM_PROCESSAMENTO.md).

## Automation-relevant behavior

- `enable_cdc=true` is evaluated per table. Database CDC is checked/enabled
  once and only when at least one table requests it. If the cleanup job already
  exists, retention is adjusted/confirmed in this preflight. Without a marked
  table, no CDC/retention query or change occurs.
- If the job does not yet exist in a newly enabled database, retention remains
  pending until the first CDC table is enabled. Adjustment and confirmation
  occur immediately afterward and before any BCP operation; the remaining
  tables use the cached result.
- When retention changes, the engine calls `sys.sp_cdc_change_job`, restarts
  only the `cleanup` job through `sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job`,
  and confirms the new value before BCP. The `capture` job is not restarted.
- The CLI records `cdc_database`, `cdc_retention`, and `cdc_table`; the JSON
  report retains consolidated evidence under `cdc_database`.
- If CDC preflight or confirmation of `cdc_retention_minutes` fails, affected
  tables with `enable_cdc=true` are skipped and recorded, while non-CDC tables
  continue processing. An isolated table-enablement failure skips only that
  table.
- `partition_column` is optional per table. The GUI suggests `dh_carga` and
  enables the option when adding a table. When present, it generates the
  monthly partitioning contract in structure-capable destination DDL; when absent, no
  partitioning object is created.
- In the standard destination profiles, `dh_carga` explicitly uses
  Brasília civil time (`E. South America Standard Time`), derived from UTC and
  converted to `DATETIME2(7)`, independently of the SQL Server time zone. A
  custom profile may define another expression and remains time-zone agnostic.
- Current operational defaults include `max_file_bytes=157286400`,
  `control_schema=dbo` (the only accepted value), and
  `structure.secondary_indexes_phase=before_load`.
- `DIRECT_KEYLESS` uses an approximate metadata row count for pre-admission,
  without `COUNT_BIG` on the Source. The actual BCP count is checked against
  the global limit before import.
- Before loading each table, the engine checks active-destination volumes individually;
  data and log capacity are not added together. The least available volume is
  the constraint, and every volume must satisfy the requirement. Confirmed
  insufficiency records the table as skipped and proceeds to the next one.
  This also applies to `import --manifest`: only the actual bytes of blocks not
  yet confirmed, plus the safety factor, are checked before creating SQL
  control, applying DDL, or executing `OPENROWSET`.
- `run`, `resume`, and `import` load only the destination selected by
  `active_destination`; a `structure_only` endpoint never reads BCP files.

## Failure and resume

After a `run` failure, do not run another `run`: preserve SQLite, the
artifacts, and SQL control, then use `resume` with the same UUID. `status`
queries local control; `query_control.sql` proves commits in the active destination.

```powershell
& .\scripts\launchers\invoke-bcp.ps1 status --config .\config.v2.json --execution-id UUID
& .\scripts\launchers\invoke-bcp.ps1 resume --config .\config.v2.json --execution-id UUID --confirm-load
```

```bash
./scripts/launchers/invoke-bcp.sh status --config ./config.v2.json --execution-id UUID
./scripts/launchers/invoke-bcp.sh resume --config ./config.v2.json --execution-id UUID --confirm-load
```

A BCP failure retries the incomplete logical block; it does not continue from
a byte or row inside the `.partial` file. During load, data, block record, and
checkpoint are committed or rolled back together. See the complete runbook in
[Failures, checkpoints, and resume](FALHAS_E_RETOMADA.md).
