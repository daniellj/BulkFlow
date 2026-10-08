**English** | [Português (Brasil)](README.pt-BR.md)

# BulkFlow — SQL Server Data Export/Import

BulkFlow is a configurable engine that exports data with `bcp queryout` and,
when import is enabled, loads it transactionally into one data-capable destination. The GUI
names the connections **Origem**, **Destino 01**, and **Destino 02**; this
documentation refers to the two destination databases as `BD_DESTINO_01` and
`BD_DESTINO_02`. Processing is sequential by table and block; rows never pass
through Python lists, DataFrames, or row-by-row transformations.

The engine, GUI, and CLI are environment- and infrastructure-agnostic. Docker,
Compose, containerized SQL Server, and fixed ports belong exclusively to the
optional local test lab; they are not product installation or operation
requirements.

This distribution includes:

- a reusable CLI for PowerShell and Linux shells;
- a simple `ttkbootstrap` desktop interface;
- versioned destination structure profiles;
- support for PKs, UNIQUE constraints, validated explicit watermarks, and
  bounded direct loads;
- optional CDC per table;
- configurable global CDC cleanup retention, with a default of 262,800 minutes;
- optional additive schema evolution;
- optional monthly partitioning on destination tables;
- free-space verification on the executor and active data-destination volumes;
- manifests, SHA-256, SQLite/SQL controls, and idempotent resume;
- self-contained Windows executables for the GUI and CLI.

## Quick start

Install Python 3.10+ and the dependencies:

```powershell
python -m pip install -r requirements.txt
```

Alternatively, on Windows x64, use the recommended offline installer:

```powershell
.\release\Setup-BulkFlow.exe
```

This package installs the application, Microsoft ODBC Driver 18, and the
Microsoft BCP utility without accessing the internet. End users do not need to
install Python or Python packages. Installation is elevated because it
registers native Windows components and writes the application to
`Program Files`. See the complete workflow in
[Offline installer for Windows](docs/INSTALADOR_OFFLINE.md).

The standalone binaries can also be run directly:

```powershell
.\release\BulkFlowGUI.exe
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
.\release\BulkFlowCLI.exe prerequisites --config .\caminho\config.v2.json
.\release\BulkFlowCLI.exe plan --config .\caminho\config.v2.json
```

The standalone executables embed Python, `pyodbc`, `ttkbootstrap`, `pywinpty`,
the engine, schemas, and default profiles. They do **not** embed or install
Microsoft ODBC Driver for SQL Server or the Microsoft `bcp` utility; use the
offline setup or provision these native prerequisites separately.

On the **executor**—the machine, VM, or container running the GUI/CLI—install
Microsoft ODBC Driver 18 and, preferably, BCP 18 or later. A BCP 17 build is
accepted only when it demonstrably supports the `-Y` and `-u` TLS controls.
`pip` does not install these native components. See installation instructions,
GUI fields, and checks in
[Executor prerequisites](docs/PRE_REQUISITOS.md). On Windows, `pywinpty`
provides the private password-prompt channel; on POSIX, the engine uses a
native PTY.

Run the automated prerequisite check before planning:

```powershell
python .\bcp_bronze.py prerequisites --config .\caminho\config.v2.json
# or
.\release\BulkFlowCLI.exe prerequisites --config .\caminho\config.v2.json
```

Direct CLI invocation:

```powershell
python .\bcp_bronze.py plan --config .\examples\config.full.json
```

PowerShell launcher:

```powershell
.\scripts\launchers\invoke-bcp.ps1 plan `
  --config .\examples\config.full.json
```

Linux launcher:

```bash
./scripts/launchers/invoke-bcp.sh plan \
  --config ./examples/config.linux-sql.json
```

Graphical interface:

```powershell
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
```

When a new configuration is created in the GUI running from source, the
default operational directories are created automatically under the project
root:

```text
Local\BulkFlow\
├── .bcp-control\
├── bcp-data\
└── ddl\
```

The import directory initially uses the same path as `bcp-data`. These local
artifacts are not versioned. Portable executables run from this project's
`release` directory use the same project-local defaults. In an installed or
standalone copy, the defaults remain under `%LOCALAPPDATA%\BulkFlow`, because
the application does not write mutable data to `Program Files`.

Guides:

- [executor prerequisites](docs/PRE_REQUISITOS.md);
- [offline installer for Windows](docs/INSTALADOR_OFFLINE.md);
- [PowerShell and Linux](docs/USO_CLI_POWERSHELL_LINUX.md);
- [graphical interface](docs/USO_INTERFACE_GRAFICA.md);
- [failures, checkpoints, and resume](docs/FALHAS_E_RETOMADA.md);
- [complete processing order](docs/ORDEM_PROCESSAMENTO.md);
- [migration from the Portuguese configuration to the V2 contract](docs/MIGRACAO_CONFIGURACAO_V2.md);
- [non-destructive migration of the legacy SQLite control](docs/MIGRACAO_CONTROLE_SQLITE.md).

## Commands

```text
python bcp_bronze.py prerequisites --config CONFIG
python bcp_bronze.py plan   --config CONFIG
python bcp_bronze.py ddl    --config CONFIG --area bronze|landing|both
                            --output DIRETORIO [--apply --confirm]
python bcp_bronze.py run    --config CONFIG [--confirm-load] [--export-only]
python bcp_bronze.py resume --config CONFIG --execution-id UUID [--confirm-load]
python bcp_bronze.py import --config CONFIG --manifest CAMINHO --confirm-load
python bcp_bronze.py status --config CONFIG --execution-id UUID
python bcp_bronze.py migrate-control --control-directory DIRETORIO
```

Exit codes:

- `0`: everything requested completed successfully;
- `2`: skipped table, partial failure, or pending work;
- `1`: global failure;
- `127`: launcher initialization failure.

`plan` is read-only. `run` creates and prints a UUID. `resume` reuses that exact
UUID. `import` does not query the source and validates the manifest contract
against the trusted local profile.

## Flow

```text
Configured source (role: data_provider)
  ├─ rows through BCP ───────────────> one data-capable destination when importing
  └─ metadata for DDL/evolution ───────> destinations whose role includes structure
```

```text
V2 configuration + versioned profile
  -> native prerequisites and configuration validation
  -> connection and effective identity (user, instance, port, and database)
  -> source catalog, types, and effective identity
  -> enable database CDC once, if at least one table requests it
  -> configure cleanup retention now, or leave it pending until the job exists
  -> enable table CDC; if retention is pending, confirm it before the first BCP
  -> select/prove the key and capture the upper bound
  -> estimate and verify space on the executor and active data destination
  -> create/evolve the active destination only when its role allows structure
  -> bcp queryout to a .partial file
  -> actual row count + SHA-256 + atomic publication
  -> OPENROWSET(BULK...) with INSERT, control, and checkpoint in one commit
  -> secondary indexes
  -> console, JSON, and CSV reports
```

There is no `bcp in`, linked server, `xp_cmdshell`, `OFFSET`, physical cursor,
or full staging table. A previously confirmed block is never inserted again.

Resume operates on logical blocks, never on the byte offset or row position of
a partial file. A failure during BCP reruns the range that has not yet been
published; a failure before the load commit rolls back the entire block; and a
proven SQL commit is not sent again. The complete procedure and failure
matrices are documented in
[Failures, checkpoints, and resume](docs/FALHAS_E_RETOMADA.md).

The recommended operational order is to validate prerequisites, plan and
provide independent credentials, generate/review/apply the applicable destination DDL,
and only then run export/import. With `create_structure_if_needed=true`—the
default—`run` also creates or idempotently completes the active destination
when its role is `structure_and_data`. With `false`, or with the `data_only`
role, the engine validates the existing structure and does not create missing
objects. The detailed sequence is documented in
[Complete processing order](docs/ORDEM_PROCESSAMENTO.md).

## Configuration

All parameter names, variable names, and code identifiers use American
English. The GUI presents labels and defaults in Brazilian Portuguese. The
business identifiers in the `perimeter` enum are the deliberate exception and
must remain exactly `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`. Persistent control
objects generated in SQL Server and SQLite also follow the Portuguese naming
convention described below. The versioned JSON schema is located at
`schemas/config-v2.schema.json`; the loader also performs semantic validation
and rejects unknown properties.

Core parameters:

| Parameter | Behavior |
|---|---|
| `perimeter` | exact enum `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`; default `DESENVOLVIMENTO` |
| `rows_per_block` | default 200,000; 1,500 is only the lab scenario |
| `max_file_bytes` | default 157,286,400 bytes (150 MiB) |
| `keyless_direct_load_max_rows` | default 5,000,000; `0` disables it |
| `allow_schema_evolution` | default `false` |
| `create_structure_if_needed` | default `true`; can be cleared in the GUI to validate existing structures only |
| `execute_import` | `false` produces artifacts only |
| `source.role` | required value `data_provider` |
| destination `role` | `structure_and_data`, `structure_only`, or `data_only` |
| `active_destination` | compatible area ID (`bronze` or `landing`); optional in the input because the single data-capable destination is derived from the roles, while an explicit value is validated |
| `enable_cdc` | independent flag for each table |
| `cdc_retention_minutes` | global CDC cleanup retention; default `262800` (six months, approximately 182.5 days) |
| `watermark` | `null` or an explicit list of columns; direction is always `ASC` |
| `partition_column` | optional per table; absence disables partitioning |
| `estimates.safety_factor` | default `1.25` |
| `control_schema` | required value `dbo`; SQL control exists in the active data destination |
| `structure.secondary_indexes_phase` | default `before_load` |
| `continue_after_table_error` | controls continuation between tables |
| `artifact_reader_sids` | specific Windows SIDs with read/traverse access to artifacts; default `[]` |
| `artifact_writer_sids` | trusted Windows SIDs with write access, only when the effective SMB identity differs; default `[]` |

Source, `BD_DESTINO_01`, and `BD_DESTINO_02` have independent `instance`,
`port`, `database`, schema, authentication, secret, and role settings. Source
always uses `data_provider`. Destination roles are:

- `structure_and_data`: allows manual/automatic DDL and data loading;
- `structure_only`: allows manual DDL and additive evolution, but never reads
  BCP files or receives rows;
- `data_only`: loads data into an already compatible layout and prohibits
  creation, evolution, and index creation.

At most one configured destination may have a role that includes data. When
`execute_import=true`, exactly one is required and its compatible area ID
becomes `active_destination`; with `execute_import=false`, zero or one is
allowed. Defaults are
`structure_and_data` for `BD_DESTINO_01` and `structure_only` for
`BD_DESTINO_02`. The persisted names `bronze_destination`,
`landing_destination`, and the areas `bronze`/`landing` are legacy-compatible
internal IDs, not fixed operational roles. The same configuration therefore supports shared or separate
instances. The usernames
suggested by the perimeter are `u684` for `DESENVOLVIMENTO`, `h684` for `HOMOLOGAÇÃO`,
and `s684` for `PRODUÇÃO`; each can be changed independently on its endpoint. The
supported authentication modes are:

- `windows_integrated`;
- `windows_credentials`, through a dedicated Windows context;
- `sql`, with a password obtained through `prompt`, `env`, or
  `windows_credential_manager`.

The BCP SQL password never appears in `-P`, argv, a manifest, or a log. The
engine responds to the masked prompt through a pseudoconsole/PTY and applies
centralized redaction. In the lab, the independent references are
`BCP_SOURCE_SQL_PASSWORD`, `BCP_BRONZE_SQL_PASSWORD`, and
`BCP_LANDING_SQL_PASSWORD`, even when all three resolve to the same test value.

Each `tables` item can also persist `source_database` and
`destination_database`. In the GUI, these fields inherit the Source and active
data-destination databases, respectively. In this version they make the mapping
explicit, but **do not create independent per-table routes**: when provided,
they must match `source.database` and the endpoint selected by
`active_destination`, case-insensitively. A mismatch is rejected during validation instead of
silently loading into the wrong database. The suggested destination name
remains lowercase `<source_database>_<source_table>`.

## Keys, watermarks, and keyless tables

The decision order is:

```text
explicit watermark -> prove existence, types, absence of NULLs, and uniqueness
no watermark       -> eligible PK -> eligible UNIQUE
no alternative     -> approximate metadata for direct-load pre-admission
```

An invalid explicit watermark is never replaced silently. The engine does not
discover column combinations by examining data. Composite watermarks use typed
lexicographic comparison while preserving direction, precision, and collation.

A keyless direct load (`DIRECT_KEYLESS`) is a single block with no invented
cursor. To avoid a preliminary scan, the engine uses `sys.partitions` (heap or
clustered index) as an approximate estimate and **does not run `COUNT_BIG` on
the source** to authorize this mode. The actual quantity copied by BCP is the
definitive limit: if it exceeds `keyless_direct_load_max_rows`, the manifest is
not published and nothing is imported.

This mode is a controlled exception, not an incremental strategy. The BCP
query uses `TABLOCK,HOLDLOCK` to stabilize the keyless read and can therefore
block writers for the entire export. For tables with a PK, UNIQUE constraint,
or proven watermark, the engine uses blocks and a key cursor and does not apply
this direct-mode-only lock.

## CDC per table

`enable_cdc` defaults to `false`.

- no table set to `true`: zero CDC calls are made against the database;
- at least one table set to `true`: database CDC is checked/enabled exactly
  once;
- if the cleanup job already exists, its retention is checked/adjusted during
  this preflight and the result is cached;
- on a newly enabled database, SQL Server may create the cleanup job only after
  the first table is enabled. In that case, its initial absence is recorded as
  pending retention, not a failure;
- `cdc_retention_minutes` defaults to `262800`, which represents the six-month
  business retention period (approximately 182.5 days); the number in JSON and
  generated SQL does not use a thousands separator. The accepted range is `1`
  through `52494800` minutes, the SQL Server maximum;
- each flagged table is checked/enabled immediately before BCP;
- when retention is pending, the engine enables/confirms the first CDC table,
  adjusts and confirms cleanup, and only then permits any BCP operation.
  Subsequent tables reuse the cached result;
- when the value must change, the engine runs `sys.sp_cdc_change_job` and
  restarts **only** the `cleanup` job with `sys.sp_cdc_stop_job` and
  `sys.sp_cdc_start_job`, so the new value takes effect immediately. The
  `capture` job is not restarted. BCP is released only after restart and
  catalog confirmation. Transient SQL Server Agent races are retried for up to
  30 seconds; permission errors fail immediately;
- the JSON report persists evidence under `cdc_database`, including the stage,
  confirmed minutes, `retention_changed`, and `retention_restarted`; the CLI
  and GUI also record `cdc_database`, `cdc_retention`, and `cdc_table`;
- a CDC failure skips that table, records its code/message, and always proceeds
  to the next table regardless of the general continuation policy.

After the first CDC table has been enabled, a missing job or an inability to
query, adjust, restart cleanup, and confirm retention is a failure. The current
table is skipped before BCP and the failed result is cached, blocking the other
tables with `enable_cdc=true`. Tables with `enable_cdc=false` continue to be
processed. When no table requests CDC, the engine neither queries nor modifies
CDC or its retention.

The engine preserves the intent of `scripts/05_ativa_cdc_banco_dados.sql` and
`scripts/06.1_ativa_cdc_tabelas.sql`, but uses parameterized calls and confirms
state in the catalog. The environment remains responsible for granting the
required authority.

## Additive schema evolution

When a destination table already exists, its catalog definition is compared
with the source's current business columns.

- `allow_schema_evolution=false`: nothing is changed; the table receives
  `SCHEMA_EVOLUTION_PENDING`, and the event contains the missing columns;
- `allow_schema_evolution=true`: only safe `ALTER TABLE ADD` operations are
  applied;
- incompatible existing types, nullability, and objects continue to block the
  operation;
- no column is removed, renamed, or modified;
- a new `NOT NULL` column on a populated table or a table with an unknown row
  count is rejected without an explicit backfill.

The rule applies to every destination whose role includes structure.
`data_only` explicitly prohibits schema evolution.

## Optional partitioning

When `tables[].partition_column` is provided, the column must exist in the
destination layout and use `DATETIME2(7)`. A structure-capable destination
profile creates:

- `pf_<coluna>_mensal` and `ps_<coluna>_mensal` when the column is `dh_carga`;
- the `_attr` suffix for any other column;
- monthly `RANGE RIGHT` boundaries from the current month through December of
  the current year plus six years, all on the `PRIMARY` filegroup;
- a technical `NONCLUSTERED` PK on `id_<tabela>`;
- a `CLUSTERED` index on the partition column, aligned with the partition
  scheme;
- removal of the redundant simple nonclustered-index contract for that column.

When a table is added through the GUI, partitioning is enabled with
`partition_column=dh_carga`; the operator can disable it or choose another
column. In the JSON contract the field remains optional: without
`partition_column`, no partitioning object is created and the profile's
traditional layout is preserved. An existing table is not silently
repartitioned: a mismatch between the existing layout and requested contract
requires an explicit migration.

## Physical destination contract

At the destinations, schemas, tables, columns, constraints, indexes, and
sequences are materialized in lowercase. Each database name is preserved as
configured.

### BD_DESTINO_01

By default, `BD_DESTINO_01` uses the compatible internal area `bronze` and the
`templates/bronze.json` profile, which creates:

- `id_{destination_table}` as `BIGINT`, generated by a sequence without
  `IDENTITY`;
- all source business columns in lowercase;
- `bi_lsn_evento`, `bi_sequencia_evento`, `cd_operacao`, `de_operacao`,
  `dh_carga`, and `dh_atualizacao`;
- `bi_lsn_evento` and `bi_sequencia_evento` receive
  `CONVERT(BINARY(10), 0)` in the `INSERT` itself; no subsequent `UPDATE`
  exists;
- `dh_carga` receives Brasília civil time in the `INSERT` itself
  (`E. South America Standard Time`), calculated from UTC and converted to
  `DATETIME2(7)` regardless of the SQL Server time zone;
- a clustered PK on the technical ID without partitioning, or a nonclustered
  PK when `partition_column` is configured;
- five indexes defined by the profile in the non-partitioned layout; with
  `partition_column=dh_carga`, the simple index on that column is replaced by
  the clustered partitioning index.

Some result grids display ten `0x00` bytes as a visually blank cell. To prove
the physical value, query it in hexadecimal; the expected result for both
attributes is `0x00000000000000000000`, with `DATALENGTH(...) = 10`:

```sql
SELECT TOP (10)
    CASE WHEN bi_lsn_evento IS NULL THEN 1 ELSE 0 END AS bi_lsn_evento_nulo,
    CONVERT(varchar(22), bi_lsn_evento, 1) AS bi_lsn_evento_hex,
    DATALENGTH(bi_lsn_evento) AS bi_lsn_evento_bytes,
    CASE WHEN bi_sequencia_evento IS NULL THEN 1 ELSE 0 END AS bi_sequencia_evento_nulo,
    CONVERT(varchar(22), bi_sequencia_evento, 1) AS bi_sequencia_evento_hex,
    DATALENGTH(bi_sequencia_evento) AS bi_sequencia_evento_bytes
FROM BD_DESTINO_01.<schema_name>.<table_name>;
```

For every row, the `*_nulo` indicators must return `0`. If the value were an
actual SQL `NULL`, the indicator would return `1` and the hexadecimal and
length columns would return `NULL`; a visually blank cell is therefore not
used as evidence. The lab verifier repeats this validation across all rows and
also confirms `BINARY(10) NOT NULL` in the catalog.

### BD_DESTINO_02

By default, `BD_DESTINO_02` uses the compatible internal area `landing` and the
`templates/landing.json` profile. It can be selected by `ddl --area
landing|both` and creates:

- `id_{source_table}` as `BIGINT IDENTITY(1,1)`;
- all source business columns in lowercase;
- structural columns `aud_ccid`, `aud_cntrrn`, and `aud_enttyp`, as defined by
  the profile;
- persisted computed columns `bi_lsn_evento`, `bi_sequencia_evento`, and
  `cd_operacao`;
- `dh_carga` with a default using Brasília civil time, calculated from UTC and
  converted to `DATETIME2(7)`, plus `dh_atualizacao`;
- a nonclustered PK and the five indexes defined by the profile; with
  partitioning, the simple index on the selected column is removed when
  redundant, and clustered organization moves to that column.

Roles, rather than internal area names, decide behavior. At most one of
`BD_DESTINO_01` or `BD_DESTINO_02` may use `structure_and_data` or `data_only`.
When import is enabled, exactly one must do so; that endpoint becomes
`active_destination` and receives the rows and SQL control. A `structure_only` endpoint can receive manual DDL/evolution but never
reads BCP files. A `data_only` endpoint receives rows only after strict layout
validation and never creates/evolves objects or indexes. The `ddl --area both`
command still uses the compatible area IDs to address both destinations.

These time-zone rules belong only to the default `templates/bronze.json` and
`templates/landing.json` profiles. A custom profile remains agnostic and can
define a different fill/default expression for `dh_carga`, taking full
responsibility for that contract.

## Artifacts and controls

Each block has an isolated directory, native file, XML format file, redacted
log, and V2 manifest. Files being written end in `.partial`; only a result with
a valid count and hash is published.

On Windows, the DACL for the root and every execution is protected and
contains only the executor, SYSTEM, Administrators, the specific read/traverse
SIDs from `artifact_reader_sids`, and the explicitly trusted writers from
`artifact_writer_sids`. Broad groups, unknown owners, reparse points, and hard
links are rejected. The manifest, format file, and BCP file remain open under a
lease that prevents writes/deletion from streaming SHA-256 verification until
the SQL transaction ends. On POSIX, directories/files remove group/other write
access, and the same interval uses file descriptors and a shared lock. On
POSIX, the root must use a GID shared with SQL when necessary; the engine
preserves `setgid`, removes group/other write access, and trusts only the same
executor/root UID.

Local SQLite uses `controle_transferencia.sqlite3`, with persistent tables
`metadados`, `execucao`, `execucao_tabela`, `execucao_lote`, and
`tentativa_lote`, plus `PRAGMA user_version=5`. This local file name is not a
SQL Server schema. SQL control is persisted in the active data destination as
`dbo.ctl_exec`, `dbo.ctl_exec_tabela`, and `dbo.ctl_exec_lote`; physical version
3 is stored in `dbo.ctl_exec_versao`. Destinations using the previous contract
must run `migrate_control_v2_to_v3.sql` administratively with no active load;
the script renames the objects in one transaction and preserves all history.
The engine validates the complete signature,
including columns, defaults, PK/FK constraints, indexes, and unexpected
objects; a partial, tampered, or incompatible structure fails closed. The
legacy SQL Server schema `controle_transferencia` is not part of the contract
and must not coexist with these tables. This rule neither renames nor removes
the local SQLite file `controle_transferencia.sqlite3`. A `structure_only`
destination does not receive load-control tables.

In the GUI, `executor_directory` is presented as **Diretório de exportação dos
arquivos**, and `destination_sql_directory` as **Diretório de importação dos
arquivos**. They can use different path syntax but must represent the same
bytes. The second initially receives the first field's value and can be changed
when SQL Server sees the shared storage through a different path. SQLite
control must remain on a local disk, never on a UNC share. When running from
source, the control, export, and DDL defaults are, respectively,
`Local\BulkFlow\.bcp-control`, `Local\BulkFlow\bcp-data`, and
`Local\BulkFlow\ddl`; they are always relative to the project root and are
created when absent.

After **Planejar**, the GUI prerequisite area compares the observable free
space on each of these paths with the consolidated BCP projection for all
tables. It displays the raw estimate, safety-factor margin, protected total,
balance for the total, operational balance, and per-table detail. Both balances
subtract the minimum free-space reserve; the operational balance uses the
predicted peak, which respects the file-retention policy. An unobservable path
or estimate appears as `indisponível`, never as zero. Reading free space is
effectively constant-time; the relevant cost is the bounded row sample for
each table. Cardinality always comes from `sys.partitions.rows`, with no full
count for this projection. See
[Graphical interface](docs/USO_INTERFACE_GRAFICA.md#1-prerequisites) and
[Prerequisites](docs/PRE_REQUISITOS.md#directory-capacity-and-bcp-projection).

Before provisioning and loading a table into the active data destination, when the byte estimate is
available, the engine multiplies it by the safety factor and compares it
separately with every database volume returned by `sys.dm_os_volume_stats`.
Data and log space are not treated as interchangeable: the displayed capacity
is the lowest availability among distinct volumes, and every volume must meet
the requirement. Repeated readings for the same mount point use the lowest
value; if any volume does not report `available_bytes`, the entire measurement
is unavailable. Proven insufficiency raises an alert, marks the table as
`SKIPPED_DESTINATION_INSUFFICIENT_SPACE`, does not start its import, and
proceeds to the next table. An unavailable estimate follows
`estimates.on_unavailable`; if only the active-destination volume query cannot be proven,
the engine records a warning and proceeds without claiming unmeasured
capacity.

For an independent `import --manifest`, the same barrier uses only the actual
bytes of blocks not yet confirmed in SQL control, summed and multiplied by the
safety factor. Previously confirmed blocks require zero additional bytes and
continue to idempotent reconciliation/finalization. Verification occurs before
creating SQL control, provisioning/evolving the table, or running `OPENROWSET`.
For a rejected fresh import, only local SQLite is changed; if a resume already
had an exactly compatible SQL link, that existing control receives the
terminal state without creating objects or loading rows.

## Windows executables and installer

To rebuild the binaries on a build workstation:

```powershell
python -m pip install -r .\requirements-build.txt
.\packaging\build_executables.ps1
```

The build generates `release\BulkFlowGUI.exe` and
`release\BulkFlowCLI.exe`. The distributable artifacts in `release/` are
versioned; only the intermediate `build/` workspace is ignored by Git. End
users of the EXEs do not need Python or Python packages, but still need the
ODBC Driver named in the configuration and a compatible `bcp` on the executor
machine. See
[Prerequisites](docs/PRE_REQUISITOS.md) and
[CLI usage](docs/USO_CLI_POWERSHELL_LINUX.md).

For a self-contained Windows distribution in a single package, generate and
deliver `release\Setup-BulkFlow.exe`. This offline installer includes both EXEs
and the pinned official ODBC Driver/BCP installers. See
[Offline installer for Windows](docs/INSTALADOR_OFFLINE.md) for interactive or
silent installation, UAC, licenses, logs, security, and clean-VM validation.

## Validated optional local lab

All lab files—Compose, configurations, fixtures, secrets, artifacts, and
evidence—are isolated under `docker/`. This directory is local, reproducible,
and ignored by Git; it does not define the topology required by the product.

## Tests

```powershell
python -m unittest discover -s tests -v
python -m compileall -q bcp_engine bcp_bronze.py bcp_gui.py
```

Tests cover configuration, authentication, PTY handling, batching, DDL, CDC,
schema evolution, controls, manifests, import, resume, GUI behavior, and
launchers. The Docker workflow adds real SQL Server/BCP validation; mocks are
not used as substitutes for that evidence.

## Deliberate limitations

The engine does not change the recovery model, create an index on the source,
grant permissions, open a firewall, configure delegation/shares, use
`xp_cmdshell`, or destructively migrate incompatible objects. A source being
written to is reported as `LIVE_BEST_EFFORT`; upper bounds and checkpoints do
not provide a global snapshot or exactly-once CDC semantics.

Types/features without a fidelity rule are rejected, including
memory-optimized tables, FileTable, temporal/RLS, and FILESTREAM,
generated/hidden, Always Encrypted, Dynamic Data Masking, typed XML, and
CLR/alias columns.
