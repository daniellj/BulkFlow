**English** | [Português (Brasil)](ORDEM_PROCESSAMENTO.pt-BR.md)

# Processing order

This is the operational flow for **BulkFlow**. The same order applies to the
graphical interface, the Python CLI, and the `BulkFlowCLI.exe` executable.
Docker is used only by the local test environment.

## Recommended operator workflow

### 1. Satisfy and validate the prerequisites

On the machine that will run the engine, install the Microsoft ODBC Driver
specified by `odbc_driver` and the Microsoft `bcp` utility specified by
`bcp_executable`. Validate both before opening business connections:

```powershell
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
```

```bash
./scripts/launchers/invoke-bcp.sh prerequisites --config ./config.v2.json
```

The Windows EXEs bundle Python and the project packages, but they cannot bundle
or install these native Microsoft components.

### 2. Plan

Provide credentials for Source, `BD_DESTINO_01`, and `BD_DESTINO_02`. Each
endpoint has its own role, instance, port, database, schema, authentication
type, user, and secret. Source must use `data_provider`; at most one destination
may use `structure_and_data` or `data_only`. Exactly one is required when
`execute_import=true` and becomes `active_destination`. Password input is masked and is not written to the
configuration, command-line arguments, manifests, or logs.

The databases displayed for each table inherit their endpoint settings. In
this version, they are not additional routes: a table's source database must
match the Source endpoint, and its destination database must match the active
data destination. Use a separate configuration and execution for a different database
pair.

`plan` is read-only. It validates the configuration, connections, effective
identity, source catalog, key strategy, profile compatibility, estimates, and
executor disk space. After planning, the GUI displays the user, instance,
port, and database actually used for each environment.

### 3. Generate, review, and apply DDL

Generate DDL for `bronze`, `landing`, or `both`; review the script, and then
apply it. The area names are compatible internal IDs: `bronze` identifies
`bronze_destination` (`BD_DESTINO_01`) and `landing` identifies
`landing_destination` (`BD_DESTINO_02`); roles determine behavior. A
`structure_only` endpoint accepts manual DDL/evolution and never data. A
`data_only` endpoint rejects DDL, evolution, and index creation. A
`structure_and_data` endpoint permits both. `create_structure_if_needed` is enabled by default and controls the
**automatic** provisioning performed by `run`, `resume`, and manifest imports.
When disabled, these operations only validate the existing structure and do
not create missing objects. The explicit `ddl --apply --confirm` command (and
the **Apply DDL** button) is a separate authorization: it applies the selected
scripts even when the checkbox is cleared. `allow_schema_evolution` remains a
separate decision: it controls whether new business columns may be added to
existing tables.

```powershell
.\release\BulkFlowCLI.exe ddl --config .\config.v2.json `
  --area both --output .\ddl

.\release\BulkFlowCLI.exe ddl --config .\config.v2.json `
  --area both --output .\ddl --apply --confirm
```

When import is enabled, exactly one configured destination includes data. It
receives the rows and the persistent control objects under `dbo`; a `structure_only` destination never
reads BCP artifacts.

### 4. Run the export/import

```powershell
.\release\BulkFlowCLI.exe run --config .\config.v2.json --confirm-load
```

The command creates and displays a UUID. Preserve that UUID, the local SQLite
database, the artifacts, and the active-destination SQL control data. If a failure occurs,
use `resume` with the same UUID; do not start another `run` in an attempt to
continue.

## Internal execution order

1. Validate the V2 contract and calculate the structural and operational
   fingerprints.
2. Open the local `controle_transferencia.sqlite3` control database and acquire
   the UUID lease.
3. Validate the artifact directory, connect to Source, and test the BCP version
   and capabilities.
4. When `execute_import=true`, connect to the active data destination, prove that SQL Server sees the
   same bytes, and validate/create `dbo.ctl_exec_versao`, `dbo.ctl_exec`,
   `dbo.ctl_exec_tabela`, and `dbo.ctl_exec_lote` in that database.
5. If at least one table has `enable_cdc=true`, verify/enable CDC on the Source
   database once. If the cleanup job already exists, verify, adjust, and
   confirm its retention at this stage, then cache the result. Retention uses
   `cdc_retention_minutes`, whose default is `262800` minutes (six months,
   approximately 182.5 days). In a newly enabled database, SQL Server may
   create the job only after the first CDC table; its initial absence is marked
   as pending. If no table requests CDC, the engine does not query or change
   CDC or its retention.
6. Process tables in configuration order. For each table:

   1. when requested, verify the CDC preflight and enable/confirm CDC on the
      table. If retention is pending, verify, adjust, and confirm the cleanup
      job immediately after that activation and **before any BCP operation**.
      When the value changes, execute `sys.sp_cdc_change_job`, stop and start
      only the `cleanup` job through `sys.sp_cdc_stop_job` and
      `sys.sp_cdc_start_job`, and confirm retention again. The `capture` job is
      not restarted. The confirmed result or failure is cached for subsequent
      tables. A table activation failure skips only that table; a database CDC
      or cleanup/retention failure blocks subsequent CDC tables without
      preventing tables that did not request CDC;
   2. validate catalog metadata, special types, PK/UNIQUE, or the explicit
      watermark;
   3. capture/reuse the table ceiling and checkpoint;
   4. calculate estimates and verify executor disk space;
   5. when a byte estimate is available, check each active-destination volume separately,
      using the lowest available space as the limiting capacity; proven
      insufficiency records `SKIPPED_DESTINATION_INSUFFICIENT_SPACE` and moves
      to the next table;
   6. create/revalidate/evolve the active structure only for
      `structure_and_data`; strictly validate an existing layout for `data_only`;
   7. first reconcile and import any pending final manifest;
   8. export the next block with `bcp queryout` to a `.partial` file;
   9. verify the actual row count, boundaries, and size, calculate SHA-256, and
      atomically publish the data and manifest;
   10. import into the active data destination through `INSERT ... SELECT ... OPENROWSET(BULK...)`;
       in that same `INSERT`, populate `bi_lsn_evento` and
       `bi_sequencia_evento` with zero as `BINARY(10)`, without a later
       `UPDATE`;
   11. commit the data, `dbo.ctl_exec_lote`, and the SQL checkpoint in
       the same transaction;
   12. confirm the block in SQLite and, when configured, delete only the
       already-confirmed data file;
   13. repeat until the ceiling is reached, validate destination cardinality,
       and finish secondary indexes only when the role allows their creation.
7. Write the JSON and CSV reports and return the consolidated exit code.

The execution JSON records consolidated evidence in `cdc_database`. When the
value changes, `retention_changed=true`; when the restart is confirmed,
`retention_restarted=true`. A transient SQL Server Agent race during restart is
retried for up to 30 seconds before failing closed.

A database preflight or retention confirmation failure does not stop tables
that did not request CDC: affected tables with `enable_cdc=true` are skipped
with a log entry, while tables with `enable_cdc=false` continue in configured
order. The job's absence before the first CDC table is not a failure by itself;
it becomes a failure if the job is still absent after a table has been
enabled/confirmed.

For `structure_and_data`, `run` also provisions the active structure
idempotently before loading each table. For `data_only`, it validates a
compatible existing layout and never creates/evolves objects or indexes. The
explicit DDL step remains recommended for every structure-capable destination.

## Cursor strategy and keyless tables

Precedence is a proven explicit watermark, eligible PK, eligible UNIQUE, and,
last, `DIRECT_KEYLESS`. The engine does not discover a column combination by
trial and error.

For `DIRECT_KEYLESS`, pre-admission uses the approximate metadata row count,
without `COUNT_BIG` on Source. BCP exports the table as a single block, and the
actual copied row count is compared with `keyless_direct_load_max_rows` before
publication/import. This mode uses `TABLOCK,HOLDLOCK`, may block writers during
the read, and cannot resume within the file; an interrupted attempt exports the
entire table again.

## Resume

`resume` processes the configuration order again to revalidate contracts, but
does not re-export or reinsert blocks that are already confirmed. If a failure
occurs on the 11th of 20 tables, the first ten are reconciled, the 11th returns
to its last durable block, and processing then continues with the remaining
tables. See [Failures, checkpoints, and resume](FALHAS_E_RETOMADA.md) for the
complete matrix.
