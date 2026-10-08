**English** | [Português (Brasil)](FALHAS_E_RETOMADA.pt-BR.md)

# Failures, checkpoints, and resume

This document describes the current implementation's operational contract for
failures and resume operations. It applies to both CLI and graphical-interface
execution and does not depend on Docker. Source uses `data_provider`. At most
one configured destination uses `structure_and_data` or `data_only`; when the
execution imports data, exactly one is required and becomes
`active_destination`. A `structure_only` destination never reads BCP files or
receives rows.

Shortcuts: [BCP export failures](#failures-during-bcp-export) ·
[destination load failures](#failures-during-the-destination-load) ·
[20-table example](#example-with-20-tables-and-a-failure-on-table-11) ·
[recovery procedure](#operational-recovery-procedure) ·
[situations without automatic resume](#situations-without-automatic-resume).

## Direct answer

Resume operates by **logical block** under the same execution UUID:

- if BCP stops in the middle of a file, the partial file is not reused; the
  same block range is exported again;
- if the load stops before the SQL commit, the entire block is rolled back and
  can be imported again;
- if the SQL commit occurred but its response was lost, or the process stopped
  before updating local control, the exact SQL control row proves the commit
  and prevents another insertion;
- if an execution with 20 tables stops during the 11th table, the earlier
  tables are revalidated and reconciled without repeating their confirmed
  blocks; the 11th returns to its last durable block, after which processing
  continues with the remaining tables.

There is no resume within a `.bcp.partial` file. With `rows_per_block=1500`,
normal rework is the current block, but 1,500 is a target, not a maximum:
groups tied on the watermark may produce a larger block. A keyless table uses
one block and may need to be exported again in full.

## Guarantees and limits

Within the same linked execution, dataset, and destination, the engine
guarantees:

- advancement of the export checkpoint only after a verifiable final manifest;
- data, block record, and import checkpoint in the same SQL commit;
- no reapplication of a block already confirmed exactly by SQL control;
- detection of gaps, overlaps, incorrect order, mismatched counts, mismatched
  hashes, and structural changes;
- preservation of the final ceiling captured at the beginning of the table
  during resume.

These guarantees do not constitute a global snapshot or exactly-once capture
from a mutable source. The consistency mode is `LIVE_BEST_EFFORT`: another
attempt of a block that has not yet been confirmed may observe changes made at
Source between attempts. Optional CDC enablement does not turn this initial
load into consumption of CDC change tables.

"Do not reapply" means that the same block already confirmed at the same
destination is not inserted again. It does not mean that the engine detects
source content changes that preserve the row count, especially in direct
keyless mode.

## What must be preserved

Preserve all of the following elements to resume an execution:

| Element | Role in resume |
|---|---|
| Execution UUID | Identifies the existing execution; `resume` never creates another UUID. |
| `local_control_directory/controle_transferencia.sqlite3` | Stores local executions, tables, blocks, attempts, and cursors. |
| Execution directory under `executor_directory` | Stores the manifest, format file, and data that are still required. |
| the active destination SQL control data, when an import occurred | Proves which blocks were actually committed at the destination. |
| Compatible structural configuration | Preserves the logical source, watermark, batching, projection, and layout. |
| Data already written to the active destination, when an import occurred | Must remain consistent with SQL control. |

Credentials, `execute_import`, operational paths, and the active destination address do not
by themselves change the structural hash. This does not mean that they may be
changed arbitrarily during `resume`. Keep the same canonical SQLite and
artifact paths: manifest paths persisted in SQLite are absolute and must remain
contained in the execution's `executor_directory`.
`destination_sql_directory` may change only if it still represents exactly the
same files to SQL Server.

The logical Source instance is part of the structural contract. The active destination
address change during `resume` is safe only when it is an alias/route to the
same physical database, tables, and SQL control; it does not migrate the
execution. For another empty destination, use `import --manifest` with **all**
required manifests and data files. If retention policy has already deleted
confirmed data files, use a new execution against an empty destination.

A change to the execution's structural configuration blocks resume. Structural
items include the logical source, tables, watermark, `rows_per_block`, batching
rules, keyless-load limit, structure profile, `partition_column`, and metadata
mapping. The Source's physical identity, including instance/port/database, as
well as columns, projection, and layout, are also revalidated per table. The
physical snapshot of a table that does not yet have a ceiling, blocks, cursors,
or counts may be rediscovered; after any durable progress, a mismatch is
blocked.

## Sources of truth

There are three complementary levels of evidence:

1. **Local SQLite** — controls planning and the export and import cursors. The
   `status` command queries only this control database.
2. **Final manifest** — a complete `block_*.manifest.json` file with valid
   identity, boundaries, counts, and hashes proves a published export. A
   `.partial` file never proves completion.
3. **Active-destination SQL control** — `dbo.execucao_lote` proves the destination
   commit. Its row is created in the same transaction as the data and SQL
   checkpoint.

The [`query_control.sql`](../query_control.sql) file queries the destination's
durable source of truth. Set `@ExecutionId` at the beginning of the script to
filter by UUID. An exact row in `execucao_lote` means that the block and its
checkpoint were confirmed in the same commit.

When an imported table finishes with `execute_import=true`, the engine also
compares the physical destination `COUNT_BIG` with the sum of `imported_rows`
for confirmed blocks. This proves cardinality, not a hash of every row's
content.

## Normal block sequence

```text
capture/reuse final table ceiling
  -> plan block number and boundaries in SQLite
  -> check estimated space on the executor and the active destination volumes
  -> create/evolve for structure_and_data, or validate existing layout for data_only
  -> mark the block as EXPORTING and the attempt as RUNNING
  -> BCP queryout writes only block_*.bcp.partial
  -> validate completion, count, and bytes; bind the planned boundaries
  -> fsync and rename to block_*.bcp
  -> calculate SHA-256 for the data and format file
  -> atomically publish block_*.manifest.json
  -> mark EXPORTED and advance export_cursor in SQLite
  -> validate the artifacts again
  -> open a SQL transaction and acquire the destination applock
  -> INSERT ... SELECT ... OPENROWSET(BULK...)
  -> require ROWCOUNT_BIG to match the manifest
  -> write execucao_lote and advance import_cursor
  -> COMMIT
  -> mark IMPORTED in SQLite
  -> optionally remove only the confirmed .bcp file
```

The engine reconciles and imports all already-published blocks before
extracting the next block. A load failure therefore does not trigger another
export of a valid final file.

## Failures during BCP export

| Failure point | Possible durable state | Behavior on resume |
|---|---|---|
| Before block planning | No new block. The ceiling, if already captured, remains. | Plans the next block from the last `export_cursor`. |
| After planning, before BCP | `PLANNED` block without a final manifest. | Uses the same boundaries and starts a new attempt. |
| During BCP, including timeout, process failure, full disk, or file-size limit | A `.bcp.partial` may exist; the attempt may be `FAILED`, or still `RUNNING` after abrupt termination. The cursor does not advance. | The earlier open attempt becomes `INTERRUPTED`; its partial and attempt format file are discarded, and the entire block is exported again. |
| BCP finished, but publication did not reach a final manifest | A partial or final `.bcp` without a manifest may exist. Neither proves completion by itself. | Recreates the same block. The new attempt replaces orphaned data during publication; the checkpoint advances only after a verified final manifest. |
| A valid final manifest was published, but SQLite did not mark `EXPORTED` | The manifest contains identity, boundaries, counts, and hashes; the local cursor is still behind. | Validates the manifest against the planned block, materializes the local checkpoint, and does not run BCP again. |
| SQLite marked `EXPORTED`, but import has not occurred | The manifest and final file remain available. | Imports that block before planning another export. |
| Final manifest, format file, or data file was tampered with or corrupted | Inconsistent evidence. | Fails closed without advancing the checkpoint and without silently replacing an existing final manifest. Restore the exact set that satisfies the hashes. Without a backup and SQL confirmation, abandon the execution in a controlled manner and use a new execution with an empty destination/consistent state. |
| A pending file was deleted before the SQL commit | SQL does not confirm the block, and no trustworthy bytes remain to load. | Fails closed. Restore the exact file from backup. Without a backup, there is no automatic resume: abandon the execution in a controlled manner and use a new execution with an empty destination/consistent state. |

A failure may leave `block_*.manifest.json.partial`. This file is not treated
as a publication and may be replaced by the next attempt. An invalid **final**
manifest, however, is not automatically deleted or overwritten.

## Failures during the destination load

The load does not use `bcp in`. It uses
`INSERT ... SELECT ... OPENROWSET(BULK...)` under `SET XACT_ABORT ON`, in one
transaction per block and with `sp_getapplock` for the physical destination.

| Failure point | Destination result | Behavior on resume |
|---|---|---|
| Proven insufficient space on the active destination volumes before provisioning/loading | No row from that table is inserted. The table receives `SKIPPED_DESTINATION_INSUFFICIENT_SPACE`. | The engine logs the warning and continues to the next table, even when the general policy would stop after a table error. Free or expand space and resume the same UUID. |
| Before the transaction starts | No new row and no new SQL control row. | Revalidates the manifest and retries the same block. |
| During `OPENROWSET`, due to a SQL error or disconnection before commit | The transaction is rolled back; data, `execucao_lote`, and the SQL cursor are not partially confirmed. | Reimports the entire same block. |
| `ROWCOUNT_BIG` differs from manifest `rows_exported` | Full block rollback. | Keeps the file and fails; the cause must be fixed before resume. |
| Failure writing SQL control or checkpoint before commit | Full rollback, including inserted rows. | Reimports the same block. |
| SQL issued `COMMIT`, but the response was lost | Data, block control, and SQL cursor are confirmed together. | Queries `execucao_lote` using the exact identity, boundaries, rows, bytes, hashes, and names. If they match, treats the block as successful; it does not blindly resend it. |
| SQL committed, but the process stopped before marking SQLite | SQL confirms the block; on the normal path SQLite still shows `EXPORTED`. The legacy/recoverable `IMPORTING` state is also recognized. | Reconciles SQL, marks the block `IMPORTED` locally, and does not insert it again. |
| The process stopped after marking `IMPORTED` but before deleting `.bcp` | The block is confirmed; the file may remain. | Does not reload the block. The remaining file is safe and may stay; automatic deletion is not a consistency requirement. |
| `.bcp` was already deleted by the post-confirmation policy | Manifest and format file remain; SQL confirms the exact block. | Missing data is accepted only because SQL control proves the commit. |
| Data completed, but index creation failed | The table remains `DATA_COMPLETE_INDEXES_PENDING`. | After normal preflights and revalidation, retries missing indexes; it does not re-export or reinsert data. |

When a byte estimate is available, the active destination check queries the volumes
associated with data and log files through `sys.dm_os_volume_stats` and compares
each volume separately with the estimate multiplied by the safety factor. Data
and log free space are not added together: `available_bytes` represents the
lowest value among distinct mount points, and all must be sufficient. Repeated
mount points use the lowest observation; any `NULL` observation makes the
measurement unavailable. An unavailable estimate follows
`estimates.on_unavailable`. If only the active destination volume query cannot be proven,
the engine logs a warning and continues; it does not assert nonexistent
capacity or confuse missing evidence with proven insufficiency.

This barrier also protects `import --manifest`. For that command, the
requirement is
`CEILING(SUM(file_bytes for blocks not yet confirmed) × safety_factor)`, and it
is measured before creating SQL control, probing the import path, applying DDL,
or running `OPENROWSET`. Blocks already proven in SQL no longer consume the
budget, and a fully confirmed set uses a zero requirement, which permits
reconciliation and index completion. If capacity is insufficient, local SQLite
receives `SKIPPED_DESTINATION_INSUFFICIENT_SPACE` and the next table is still
evaluated. A fresh import does not create or change SQL control; on resume with
an exactly compatible SQL binding, the engine only finalizes the existing
control so that it does not remain artificially `RUNNING`.

Before any insertion, the engine also requires:

- the block immediately following the last confirmed block;
- exact equality between the lower boundary and the SQL `import_cursor`;
- the same ceiling, layout, projection, and destination;
- an empty destination on first binding, or a compatible prior binding to the
  same execution.

These rules block gaps, overlaps, accidental appends, and concurrent use of the
same destination by another execution.

## Example with 20 tables and a failure on table 11

Assume the execution stops at block 7 of table 11:

```text
tables 1 through 10  -> blocks confirmed in SQL
table 11             -> blocks 1 through 6 confirmed; block 7 partial or pending
tables 12 through 20 -> not yet started in this interrupted invocation
```

When `resume` runs with the same UUID, the engine traverses the configuration
from the first table; it does not literally jump to the 11th. Behavior is:

1. tables 1 through 10: revalidate contracts, reconcile control, verify
   cardinality and indexes; do not re-export or reinsert confirmed blocks;
2. table 11: first import any final manifest that is still pending. If block 7
   remained only partial, recreate the entire block 7 range from the last
   durable checkpoint;
3. tables 12 through 20: continue in configured order.

Normal `resume` still opens Source, validates BCP and connections, and
prepares/revalidates earlier tables. Source and BCP must therefore be available
even if blocks 1 through 10 are not transferred again. Only
`import --manifest` works without Source access. The exact tool matrix by
command is in
[Executor prerequisites](PRE_REQUISITOS.md#dependencies-by-operation).

With `continue_after_table_error=false`, a table error ends that invocation
after recording state, and subsequent tables do not start. With `true`, table
errors are recorded and later tables may be processed. Thus, tables 12 through
20 can already be complete only if table 11 failed as a handled error and the
flow continued; a process failure during block 7 ends the invocation before
them. Resume reconciles each table according to its state. Failure to enable
CDC on a table skips only the affected table and attempts the next one.

The CDC preflight checks/enables the database once per execution. If the
cleanup job already exists, it also adjusts and confirms retention to
`cdc_retention_minutes` (default `262800`, six months, approximately 182.5
days) and caches the result. In a newly enabled database, SQL Server may create
the job only after the first CDC table. Its initial absence is then a pending
state: the engine enables/confirms the first table, adjusts and confirms
retention, and only then allows its BCP operation. Subsequent tables reuse the
cached result.

When retention differs, the engine executes `sys.sp_cdc_change_job`, restarts
only the `cleanup` job through `sys.sp_cdc_stop_job` and
`sys.sp_cdc_start_job`, and confirms the value again. This makes the new policy
effective immediately without restarting the `capture` job. No table BCP
operation starts before restart and confirmation.

If the job remains absent after a table is enabled, or retention cannot be
queried, changed, applied by restarting cleanup, and confirmed, the current
table is skipped before BCP, and the cached result blocks subsequent tables
with `enable_cdc=true`. Tables with `enable_cdc=false` continue. An isolated
failure enabling one table skips only that table; while retention is pending,
the next CDC table may still create and complete it. If no table requests CDC,
the engine does not query or change CDC or retention. After fixing a
permission, job, or configuration issue, use `resume` with the same UUID; the
preflight is reevaluated.

The engine retries transient SQL Server Agent errors during cleanup `start` for
up to 30 seconds; permission errors are not retried. Final evidence is stored
in the JSON report under `cdc_database`, with `retention_minutes`,
`retention_changed`, `retention_restarted`, stage, and any error code.

## Strategy-specific behavior

### Proven PK, UNIQUE, or watermark

The cursor consists of the typed values from the selected key. A new attempt
repeats the same unconfirmed logical range, not a physical position or file
offset. The captured final ceiling is reused, so new rows above it do not
silently enter that execution.

`rows_per_block` is the planning target. To avoid splitting a group with equal
watermark values, a block may contain more rows than that target.

### Direct keyless load

The entire table is one block, without a row cursor. Pre-admission uses the
approximate `sys.partitions` count (heap or clustered index), without
`COUNT_BIG` on Source. After BCP, the actual copied count is compared with the
global `keyless_direct_load_max_rows` limit. If it exceeds the limit, the
manifest is not published, no row is imported, and the table receives
`SKIPPED_KEYLESS_DIRECT_LOAD_OVER_LIMIT`. If export fails before publication,
resume repeats the entire table.

The BCP query in this mode uses `TABLOCK,HOLDLOCK` to stabilize reads without a
reproducible key and may block writers for the duration of the export. Without
a key, content changes that preserve the count cannot be proven; this is an
explicit limitation. Subsequent tables stop or continue according to
`continue_after_table_error`, except the special CDC and active-destination-space skips,
which always continue to the next table.

### Export without immediate load

With `execute_import=false`, `resume` continues exporting under the same UUID,
and artifacts remain retained. A later import uses `import --manifest` and does
not query Source. The path may identify a manifest, a manifest index, or the
execution directory.

If a separate `import` command fails, run `import` again with the same manifest
set. SQL control recognizes already-confirmed blocks and loads only pending
ones. This differs from running `resume` after a `run` failure.

The `resume` command opens and revalidates Source even when published artifacts
already exist. When recovery must occur without Source access, use
`import --manifest` with the complete artifacts; that operation does not query
Source.

## Operational recovery procedure

In the commands below, replace `config.v2.json`, the UUID, and artifact
directories with the execution's actual paths. A manifest passed to `import`
must be under that UUID's directory in `executor_directory`.

### 1. Preserve state

Do not truncate the active destination, do not delete `dbo.execucao`,
`dbo.execucao_tabela`, `dbo.execucao_lote`, or `dbo.versao_esquema` there, and
do not remove SQLite or edit/delete
artifacts. Do not start another `run` to continue: that creates a new UUID.

In a disposable test environment, truncating targets and controls starts a new
test; it is not a resume of the previous test.

### 2. Identify the UUID and query local state

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 status `
  --config .\config.v2.json `
  --execution-id 12345678-1234-5678-9234-567812345678
$engineExitCode = $LASTEXITCODE
```

Linux shell:

```bash
./scripts/launchers/invoke-bcp.sh status \
  --config ./config.v2.json \
  --execution-id 12345678-1234-5678-9234-567812345678
engine_exit_code=$?
```

`status` is local. To inspect destination commits, run
[`query_control.sql`](../query_control.sql) in the active destination database and set
`@ExecutionId`.

### 3. Fix only the external cause

Examples include connectivity, disk space, share permissions, credentials, SQL
Server availability, or CDC authorization. Preserve the structural contract
and control data.

### 4. Resume the same UUID

When `execute_import=true`, explicit confirmation remains mandatory.

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 resume `
  --config .\config.v2.json `
  --execution-id 12345678-1234-5678-9234-567812345678 `
  --confirm-load
$engineExitCode = $LASTEXITCODE
```

Linux shell:

```bash
./scripts/launchers/invoke-bcp.sh resume \
  --config ./config.v2.json \
  --execution-id 12345678-1234-5678-9234-567812345678 \
  --confirm-load
engine_exit_code=$?
```

In the graphical interface, load the same configuration, enter the UUID in the
execution field, and use **Resume**. **Query status** reads the same SQLite
database as the CLI.

### 5. For a later import, resubmit the same manifests

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 import `
  --config .\config.v2.json `
  --manifest .\artefatos\12345678-1234-5678-9234-567812345678 `
  --confirm-load
```

Linux shell:

```bash
./scripts/launchers/invoke-bcp.sh import \
  --config ./config.v2.json \
  --manifest ./artefatos/12345678-1234-5678-9234-567812345678 \
  --confirm-load
```

All manifests are prevalidated before the destination is opened or changed.

### 6. Validate completion

- exit code `0`: everything requested completed;
- exit code `2`: a table was skipped, a partial error occurred, or work remains;
- exit code `1`: a global failure or interruption occurred;
- `status`: inspect local states, cursors, and counts;
- `query_control.sql`: inspect blocks and rows confirmed in the active destination;
- when each imported table completes (`execute_import=true`), the engine
  requires the destination's physical cardinality to equal the sum of its
  confirmed blocks.

For a stable source, test validation should also compare Source and the active destination row
counts. For a source receiving writes, compare against the ceiling and slice
captured by the execution, not necessarily the source's current `COUNT_BIG`.

## Situations without automatic resume

- **SQLite was lost or replaced:** `resume` cannot find the execution. Restore
  local control. If published manifests survived, `import --manifest` can
  recreate the state required for import and reconcile SQL without querying
  Source, but it cannot reconstruct an incomplete export.
- **SQL control was lost while the destination remains populated:** the engine
  refuses to bind to a nonempty destination without compatible control. Restore
  data and control as a consistent set, or use a new empty destination in a new
  execution.
- **Final manifest is corrupted or incompatible:** the engine fails closed. Do
  not edit hashes or boundaries to force a load. Restore the exact set from
  backup; without it and without SQL confirmation, automatic resume is not
  available.
- **Structural change:** a change to structural configuration, or to a table
  with a durable ceiling, block, or checkpoint, blocks `resume`. A table with no
  progress in a provisional or error state may be rediscovered and revalidated
  under the contract; this never reinterprets existing blocks.
- **External change to the active destination table:** cardinality verification may detect
  the mismatch, but the engine does not correct or truncate external data.

## Prohibited actions during recovery

- run `run` expecting it to recognize the earlier execution;
- reuse the UUID with a different structural configuration;
- truncate the active destination tables or control tables;
- delete `controle_transferencia.sqlite3`;
- delete a `.bcp`, XML, or block manifest that has not yet been confirmed;
- edit a manifest, hash, boundaries, or counts;
- resend SQL manually without first querying `execucao_lote`;
- run two concurrent resume operations for the same UUID.

The engine uses a local lease and SQL locks to prevent concurrency, but these
controls do not make destructive manual intervention safe.

## Local test evidence

In the real SQL Server/BCP test environment, execution
`5f249b78-bda3-445a-b825-db4d14c929cf` underwent a controlled failure caused
by truncated BCP output in block 32 of `TABELA_ORIGEM_03`. Blocks 1 through 31,
totaling 46,500 rows, were already confirmed; the incomplete block did not
advance the checkpoint. Resume under the same UUID completed 50,000 rows, and
a second resume after completion inserted zero additional rows.

This evidence proves the path exercised in the test environment. The
guarantees in this document also follow from the engine's transactions,
validations, and automated tests; the Docker infrastructure used for the test
is not part of the product.
