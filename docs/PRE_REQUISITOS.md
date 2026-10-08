**English** | [Português (Brasil)](PRE_REQUISITOS.pt-BR.md)

# Executor prerequisites

## Direct answer

Install the ODBC driver and the `bcp` utility **on the machine, VM, or container
that will run the engine**. This document calls that environment the
**executor**.

Python, Tk, and the packages in `requirements.txt` are also required when
running the `.py` files or launchers. The Windows executables
`BulkFlowGUI.exe` and `BulkFlowCLI.exe` already bundle Python and the project
packages, but they do **not** bundle the ODBC driver or BCP. Those are native
Microsoft components and remain mandatory on the executor.

On Windows x64, `Setup-BulkFlow.exe` provides these dependencies: the offline
package contains both EXEs and the official installers for Microsoft ODBC
Driver 18 and the Command Line Utilities/BCP. The target workstation does not
need internet access, Python, or `pip`, but UAC elevation is required to install
the native components for the machine. See
[Offline installer for Windows](INSTALADOR_OFFLINE.md).

These tools do not need to be installed on the Source, Bronze, or Landing SQL
Server instances solely for the engine to work. Those servers must accept the
connections, provide the required permissions, and, for Bronze, be able to read
the files produced by the executor.

If the process runs through Task Scheduler, a service, a pipeline, or a
container, the installation, `PATH`, DSNs, and permissions must work for that
process's effective identity. Validation performed only in another user's
interactive session is not sufficient.

## Supported components and versions

| Component | Requirement | Notes |
|---|---|---|
| Python | 3.10 or later | Use the same Python/virtual environment for installation and execution. |
| Tk | 8.6 or later | Required only for the graphical interface. |
| Python packages | `python -m pip install -r requirements.txt` | Includes `pyodbc`, `ttkbootstrap`, and, on Windows, `pywinpty`. |
| ODBC Driver | Microsoft ODBC Driver 18 for SQL Server | The registered name must exactly match the configuration field. |
| BCP | 18+ recommended; 17 only if it provides `-Y` and `-u` | The engine validates the version and capabilities before exporting. |

For the EXEs, the Python, Tk, and package rows are already satisfied by the
bundle. For the PowerShell/Linux launchers and direct script execution, every
row in the table applies.

When the offline setup is used, it also satisfies the ODBC Driver and BCP rows
on the Windows workstation. The prerequisites remain visible to the operating
system and are validated normally by the `prerequisites` command.

`pip` does **not** install the native ODBC driver or the `bcp` executable. They
are separate external components. Installing the driver does not prove that
BCP is installed, and vice versa.

On Windows, `[Microsoft][ODBC Driver Manager]` in an error message identifies
the manager provided by the operating system, not the name or version to enter
in the GUI. The client component expected by the **ODBC Driver** field is
`ODBC Driver 18 for SQL Server`.

For a new installation, use a current supported BCP version 18 or later rather
than pinning an old patch. This is the official reference for the `-Y` and `-u`
controls. For compatibility, the engine accepts a build identified as major 17
**only** when `bcp -?` proves that it also provides both controls; a version 17
build without that capability is rejected. This check reflects the validated
behavior and must not be interpreted as unrestricted support for every BCP 17
build.

The BCP client version is independent of the SQL Server instance version. For
example, using SQL Server 2022 does not automatically install the tool on the
executor.

Official Microsoft references:

- [Download Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server);
- [Download and install BCP](https://learn.microsoft.com/sql/tools/bcp/bcp-download-install);
- [BCP utility options and versions](https://learn.microsoft.com/sql/tools/bcp-utility);
- [Install ODBC, BCP, and sqlcmd on Linux](https://learn.microsoft.com/sql/linux/install-upgrade/setup-tools).

## Where each prerequisite belongs

| Topology component | Python/`pyodbc` | ODBC Driver | BCP | Server requirement |
|---|---:|---:|---:|---|
| GUI, CLI, or job executor | Yes for scripts; bundled in the EXEs | Yes, for SQL operations | Yes, for `run` and `resume` | Opens connections and creates artifacts. |
| Source SQL Server | No | No | No | TDS access, read access, catalog access, and CDC permissions when requested. |
| Bronze SQL Server | No | No | No | DDL/DML/control operations and artifact reads through `OPENROWSET`. |
| Landing SQL Server | No | No | No | DDL and schema evolution; receives no rows. |
| Operator workstation | Only if it is also the executor | Same | Same | SSMS, RDP, or SSH alone does not run the engine. |

If the executor and SQL Server are on the same machine, installation remains
necessary because of the executor role, not because of the SQL service.

Each connection has separate `instance` and `port` values. The port is required
in new configurations and must be reachable between the executor and that
endpoint. Source, Bronze, and Landing may share an instance or reside on three
different instances and ports without changing the flow.

## Dependencies by operation

| Operation | ODBC | BCP | Notes |
|---|---|---|---|
| Open the GUI, edit, and save JSON | No | No | With scripts, requires Python, Tk, and `ttkbootstrap`; the EXE already bundles them. |
| `prerequisites` | Does not connect to SQL | Inspects the binary | Checks the registered driver and the BCP location, version, and capabilities. |
| `status` and `migrate-control` | No | No | Operate on the local SQLite control database. |
| `plan` | All configured endpoints | No | Inspects identities, metadata, cardinality, contracts, and the applicable destination. |
| `ddl` without `--apply` | Source | No | Generates scripts only. |
| `ddl --apply` | Source and selected destinations | No | Applies DDL to Bronze, Landing, or both according to `--area`. |
| `run --export-only` and `resume` for an execution without loading | Source | Yes | Uses `execute_import=false` and does not open Bronze. |
| `run` and `resume` with loading | Source and Bronze | Yes | BCP exports from Source; Bronze imports through SQL bulk operations. |
| `import --manifest` | Bronze | No | Does not open Source and does not run BCP. |

`resume` performs BCP pre-validation even when all exported blocks will only be
reconciled. To import existing manifests without Source or BCP, use
`import --manifest`.

## Automatic validation

Run this before `plan`:

```powershell
python .\bcp_bronze.py prerequisites --config .\config.v2.json
# or, without installing Python
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
```

```bash
./scripts/launchers/invoke-bcp.sh prerequisites --config ./config.v2.json
```

The command returns JSON. Exit code `0` means that the configured driver was
found and BCP passed validation; exit code `2` means that a prerequisite is
missing or incompatible. This check does not replace the connection and
permission tests performed by `plan`.

After **Plan**, the GUI prerequisite area also displays the capacity assessment
for the export and import directories. This second item is not part of the CLI
`prerequisites` command because it depends on the projection for every table
and therefore on the connections and credentials used by `plan`.

## Graphical interface fields

On the **General** tab, under **Planning and tools**:

- **ODBC Driver** corresponds to `odbc_driver`. The default value is
  `ODBC Driver 18 for SQL Server` and must appear with exactly the same spelling
  in the list returned by `pyodbc.drivers()`. Do not enter only `18`, a DLL
  path, an MSI installer, or the BCP path.
- **BCP executable** corresponds to `bcp_executable`. Use `bcp` when the
  executable is on the `PATH` of the account that runs the engine. For jobs and
  services, prefer the absolute path returned by system inspection.

These fields do not install or download components. The global driver is used
for ODBC connections that do not have an endpoint-specific setting. The BCP
executable is a separate process and is not selected by the ODBC Driver field.

The JSON contract accepts an endpoint-specific `odbc_driver`. Review the saved
file when an environment uses advanced overrides that are not exposed in the
interface.

When an endpoint uses `odbc_dsn`, that DSN must exist on the executor and be
visible to the same account and process architecture. For that endpoint's ODBC
connection, made by `pyodbc`, the DSN replaces selection by the global driver
name. The DSN is not used by BCP: the BCP process does not receive `-D` and
always uses `-S <instance>,<port>`. Therefore, `instance` and `port` must
identify a valid network route even when `odbc_dsn` is configured.

## Installation and validation on Windows

### Recommended option: offline setup

Run `Setup-BulkFlow.exe`, accept the UAC elevation and license terms, and then
run prerequisite validation. The setup installs the application under
`C:\Program Files\BulkFlow` and keeps mutable user data under
`%LOCALAPPDATA%\BulkFlow` or the explicitly configured directories.

For interactive or silent installation, repair, upgrade, uninstallation, logs,
signing, and the mandatory clean-VM procedure, see
[Offline installer for Windows](INSTALADOR_OFFLINE.md).

### Manual component installation

1. Install Microsoft ODBC Driver 18 using the official installer.
2. Install the current Microsoft Command Line Utilities. For a new
   installation, prefer BCP 18 or later.
3. Install the Python dependencies in the environment that will run the engine.

```powershell
python -m pip install -r .\requirements.txt

# The driver must appear under the name used in the configuration.
Get-OdbcDriver | Where-Object Name -eq 'ODBC Driver 18 for SQL Server'
python -c "import pyodbc; print(pyodbc.drivers())"

# Locate the exact BCP selected through PATH and check its version/options.
Get-Command bcp
where.exe bcp
bcp -v
bcp -?

# Useful for diagnosing a driver or DSN with a different architecture.
python -c "import struct; print(struct.calcsize('P') * 8)"
```

The output of `bcp -v` should preferably report major version 18 or later. If
it reports 17, the help must include `-Y` and `-u`; the engine performs the same
check and rejects a binary without these controls. If `where.exe bcp` returns
more than one copy, configure the absolute path of the correct version in
**BCP executable**.

Run these checks with the same Python, virtual environment, and identity that
will run the application. After installing tools or changing `PATH`, restart
the terminal, service, or agent that will run the engine.

### Using the standalone Windows executables

Distribute the two files produced under `release/`, together with the
configuration when applicable:

```powershell
.\BulkFlowGUI.exe --config .\config.v2.json
.\BulkFlowCLI.exe prerequisites --config .\config.v2.json
.\BulkFlowCLI.exe plan --config .\config.v2.json
```

The user workstation does not need Python, `pyodbc`, `ttkbootstrap`, or
`pywinpty`. The configured ODBC Driver and BCP must still be installed and
visible to the same process architecture and identity. The bundle does not
change `PATH`, DSNs, firewall rules, certificates, or permissions.

This requirement for standalone EXEs does not apply when the application is
installed through `Setup-BulkFlow.exe`, because the setup provisions the ODBC
Driver and BCP. The installer still does not change DSNs, firewall rules,
certificates, or database permissions.

The build workstation needs Python and the dependencies in
`requirements-build.txt` to rebuild the EXEs:

```powershell
python -m pip install -r .\requirements-build.txt
.\packaging\build_executables.ps1
```

## Installation and validation on Linux

Follow Microsoft's official procedure for the distribution in use. In
general, `msodbcsql18` provides the driver and `mssql-tools18` provides `bcp`
and `sqlcmd`; package names and installation commands may vary by distribution.

```bash
python3 -m pip install -r ./requirements.txt

odbcinst -q -d
python3 -c 'import pyodbc; print(pyodbc.drivers())'

BCP_BIN="$(command -v bcp 2>/dev/null || true)"
[ -n "$BCP_BIN" ] || BCP_BIN=/opt/mssql-tools18/bin/bcp
"$BCP_BIN" -v
"$BCP_BIN" -?
```

If `/opt/mssql-tools18/bin` is not on the job's `PATH`, set
`bcp_executable` to `/opt/mssql-tools18/bin/bcp`. Do not assume that the
`PATH` of an SSH session is the same as that of `systemd`, cron, a pipeline, or
a container.

For the GUI on Linux, Python must also provide Tk and access to a graphical
session. The CLI does not require a display.

## Files shared with Bronze

Artifact paths have different points of view:

- `executor_directory`: **File export directory** in the GUI; an absolute,
  writable path seen by Python/BCP;
- `destination_sql_directory`: **File import directory** in the GUI; an
  absolute path seen by the Bronze SQL Server for the **same bytes** and the
  same relative structure. In the interface, it initially has the same value
  as the export directory and must be adjusted when SQL sees those bytes
  through a different path;
- `local_control_directory`: local, durable storage for the SQLite control
  database; it must not be a UNC path.

A local executor directory does not automatically become visible to a remote
SQL Server. Use an SMB share, volume, or bind mount and grant read access to the
effective SQL Server/BULK service identity. Before loading data, the engine
performs a path proof: it writes random content through the executor and
requires Bronze to read exactly the same bytes through `OPENROWSET`.

The Landing endpoint does not read data files. `destination_sql_directory` is
required for loading into Bronze and for `import --manifest`, but not for an
export-only execution.

### Directory capacity and BCP projection

After planning, the GUI sums `estimated_bcp_total_bytes` for all configured
tables and displays both the raw total and:

```text
table reserve = ceiling(table raw BCP × safety factor)
protected total = sum(reserve for each table)
margin = protected total - raw total
balance for the total = free space - minimum free space - protected total
balance = free space - minimum free space - predicted operating peak
```

The per-table breakdown preserves the raw and protected values that make up
this aggregate. The operating peak comes from `predicted_peak_bytes`: when
files are retained, it accumulates the reserves; with post-commit deletion, it
accounts for blocks that may coexist. The screen therefore distinguishes
**how much will be generated over the entire flow** from **how much must fit on
the volume at one time**. If any configured table has an unknown estimate, the
GUI preserves the **unavailable** state in the aggregate and balance; it does
not replace the unknown value with zero or produce a false approval. A known
subtotal may be displayed only as partial evidence.

Each directory is queried independently through the executor's operating
system. If both paths are aliases for the same share, they still represent one
set of files: the projection is compared with each view, not added twice. If
the import path exists only in the SQL Server host namespace, the local
measurement is **unavailable**. The application does not confuse this state
with proven insufficient space.

This indication is for planning; it is not a filesystem reservation. Other
processes may consume space after the observation, data distribution may
differ from the sample, and retained files from earlier executions also occupy
the volume. The engine retains its execution barriers and separate validation
for the Bronze data/log volumes.

### Computational cost of the projection

- **Free space:** two operating-system metadata queries, `O(1)` per path from
  the application's perspective; normally negligible.
- **Row count (default `metadata`):** reads `sys.partitions.rows`, approximately
  `O(number of partitions)`, without counting every row.
- **Average size:** `TOP (maximum_sample_rows)` and `DATALENGTH` over the
  exported columns; the default is 10,000 rows. Approximate cost is
  `O(tables × sampled_rows × exported_columns)` and involves I/O when pages are
  not in cache.
- **Full count:** never runs to estimate file cardinality; the contract accepts
  only `metadata` in `estimates.row_count_method`. Proving the uniqueness of an
  explicit watermark without PK/UNIQUE is a different validation and may scan
  and group the table.
- **Application aggregation and comparison:** `O(number of tables)`, negligible
  compared with SQL reads.

The sample is a convenience sample (`TOP`, without random ordering) and offers
no statistical guarantee. The safety factor, default `1.25`, adds 25% to the
projection, but it is not a physical guarantee and does not replace validation
during BCP.

## Minimum permissions by role

Exact grants must follow the environment's policy and the principle of least
privilege:

- **Source:** read access to data and metadata. If any table uses
  `enable_cdc=true`, the credential must also be authorized to enable CDC on
  the database and table, query the cleanup job in `msdb.dbo.cdc_jobs`, and
  execute `sys.sp_cdc_change_job` in the Source database when retention differs
  from `cdc_retention_minutes`. In that case, it must also be able to execute
  `sys.sp_cdc_stop_job` and `sys.sp_cdc_start_job` to restart only cleanup and
  make the change effective immediately. Without this authority, the error is
  logged and tables that requested CDC are skipped; tables with
  `enable_cdc=false` continue. These permissions must also be available while
  enabling the first table: in a newly enabled database, SQL Server may create
  the cleanup job only then, and the engine restarts cleanup when necessary and
  confirms retention before starting any BCP operation. The `capture` job is
  not restarted.
- **Bronze:** creation/evolution of schemas, tables, sequences, constraints,
  and indexes; `INSERT`; creation or validation and maintenance of the
  `dbo.versao_esquema`, `dbo.execucao`, `dbo.execucao_tabela`, and
  `dbo.execucao_lote` control tables in the Bronze database (`DBRO684` in the
  test environment); file reads through `OPENROWSET(BULK...)`; and volume-space
  queries. On SQL Server 2022+, `sys.dm_os_volume_stats` normally requires
  `VIEW SERVER PERFORMANCE STATE`. Partitioning requires the applicable
  dataspace permission, normally `ALTER ANY DATASPACE`, in addition to the
  database DDL permissions.
- **Landing:** DDL and schema-evolution permissions. It does not need to read
  BCP files or receive load DML.

The engine does not grant permissions, open firewalls, create shares, or
configure a service account. Missing evidence when querying Bronze space is
logged as a warning; proven insufficient space causes the table to be skipped
before import.

## Quick diagnostics

### `[Microsoft][ODBC Driver Manager] ...`

`ODBC Driver Manager` is the component that loaded or attempted to locate the
driver; this message does not by itself identify the installed version.

1. Copy the exact **ODBC Driver** value from the GUI.
2. Compare it with `python -c "import pyodbc; print(pyodbc.drivers())"` run by
   the same Python and engine account.
3. If a DSN is used, confirm its name, user/system scope, and architecture.
4. Do not try to fix this error by changing the BCP path: they are independent
   components.

Error `IM002` normally indicates a missing driver/DSN name or one that is not
visible to the current process.

### `bcp` not found or version rejected

Check `where.exe bcp` on Windows or `command -v bcp` on Linux. In a service or
job, configure an absolute path. Validate with `bcp -v` and `bcp -?`; the
engine rejects an installation without the required TLS controls.

### Bronze cannot read the file

Reinstalling ODBC or BCP does not fix this problem. Check the share/mount, the
mapping between the two paths, and permissions for the effective SQL Server
identity.

`sqlcmd` can be useful for administration and test environments, but it is not
an engine runtime dependency.
