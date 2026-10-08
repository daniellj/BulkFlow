**English** | [Português (Brasil)](USO_INTERFACE_GRAFICA.pt-BR.md)

# BulkFlow graphical interface

The `ttkbootstrap` interface uses the same V2 contract and engine as the CLI.
There is no second load implementation: its actions call the planning, DDL,
execution, resume, import, and status services.

The application is infrastructure-agnostic. Docker, fixed ports, and the
`BD_ORIGEM`, `DBRO684`, and `DLAN684` databases belong only to the local lab.

## Opening the interface

With Python 3.10+:

```powershell
python -m pip install -r .\requirements.txt
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
```

With the Windows executable:

```powershell
.\release\BulkFlowGUI.exe
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
```

`BulkFlowGUI.exe` includes Python, Tk, `ttkbootstrap`, `pyodbc`, `pywinpty`,
the engine, schemas, and standard profiles. It does not include the Microsoft
ODBC Driver or the Microsoft `bcp` utility; both must be installed and visible
to the same account that starts the EXE. See
[Executor prerequisites](PRE_REQUISITOS.md).

Without `--config`, the window opens an infrastructure-neutral template that
must be completed. Its principal defaults are:

- `DESENVOLVIMENTO` perimeter, with the editable username suggestion `u684`;
- 200,000 rows per block;
- `1.25` safety factor;
- 5,000,000-row limit for keyless direct load;
- additive schema evolution disabled;
- CDC retention of 262,800 minutes (six months, approximately 182.5 days);
- CDC disabled for each table;
- structure creation enabled;
- maximum file size of 157,286,400 bytes (150 MiB);
- `dbo` control schema and secondary indexes before the load;
- `ODBC Driver 18 for SQL Server` driver and `bcp` executable;
- when running from source, control under `Local\BulkFlow\.bcp-control`,
  export and import under `Local\BulkFlow\bcp-data`, and DDL scripts under
  `Local\BulkFlow\ddl`, all relative to the project root.

A highlighted background identifies suggested or inherited values that can
normally be accepted without adjustment; these fields remain editable. Some
operational fields are also prefilled but retain a white background to signal
that the operator must review them: CDC retention, both directories, and the
instance, port, database, and schema of every connection. Every visible
parameter has brief help available through its `?` tooltip.

Highlighted fields include the optional reader/writer SIDs, each endpoint's
optional DSN, and the per-table batch, profile, partitioning, and inherited
database/schema/destination values. Background color does not change
validation: `*` and the field text determine whether a value is required.

## General tab

This tab groups the perimeter, execution mode, directories, files, limits,
timeouts, and tools. Fields marked with `*` are required.

The data flow is fixed: Source → Bronze. Landing is structure-only. The former
scope field is not displayed. **Criar estrutura se necessário** is available
again and selected by default. When selected, it allows `run`, `resume`, and
manifest import to create or complete missing Bronze objects automatically.
When cleared, these operations only validate the existing structure and report
missing/incomplete structures without creating them. **Gerar DDL** is always
non-mutating, and explicitly selecting **Aplicar DDL** remains a separate
authorization to apply Bronze/Landing scripts regardless of this checkbox.
The field's tooltip explains this behavior. **Apagar arquivos exportados após
confirmação** removes a data file only after the Bronze commit has been proven.

### Perimeter and suggested users

The perimeter accepts exactly `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`, always in
uppercase. These values suggest `u684`, `h684`, and `s684`, respectively. The
suggestion does not bind the connections: each endpoint may use another
username and password.

### Planning and tools

- **Driver ODBC:** the system-registered name used by `pyodbc`, for example
  `ODBC Driver 18 for SQL Server`.
- **Executável BCP:** `bcp` when it is on `PATH`, or its absolute path.
- **Fator de segurança:** multiplies the byte estimate; default `1.25`.
- **Retenção do CDC (minutos):** retention period of the Source cleanup job;
  default `262800`, equivalent to the six-month business period
  (approximately 182.5 days). Enter digits only, without a thousands
  separator; the accepted range is `1` through `52494800` minutes.
- **Esquema de controle:** required value `dbo`. In the lab's Bronze database,
  the persistent contract uses `DBRO684.dbo.execucao`,
  `DBRO684.dbo.execucao_tabela`, `DBRO684.dbo.execucao_lote`, and the technical
  table `DBRO684.dbo.versao_esquema`.
- **Índices secundários:** defaults to **Antes da carga**, persisted as
  `before_load`.

BCP 18+ is recommended. A build 17 is accepted only when it demonstrates
support for the `-Y` and `-u` TLS options.

### Files and limits

- **Linhas por bloco:** default 200,000; may be overridden per table.
- **Limite para tabelas sem chave:** default 5,000,000; zero disables
  `DIRECT_KEYLESS`.
- **Arquivo máximo em bytes:** default `157286400` (150 MiB).
- **Arquivo máximo em bytes** and **Espaço livre mínimo em bytes:** remain
  editable in bytes and have an adjacent box that converts the value to MB and
  GB.
- **Diretório de exportação dos arquivos:** writable path used by the
  executor/BCP. When running from source, the default is
  `<project root>\Local\BulkFlow\bcp-data`.
- **Diretório de importação dos arquivos:** the Bronze SQL Server's view of the
  same bytes. It initially matches the export directory but may be changed to
  an equivalent share or mount path.
- **Diretório de controle local:** stores SQLite and checkpoints. When running
  from source, the default is
  `<project root>\Local\BulkFlow\.bcp-control`.

The standard control, export, and DDL directories are created automatically
when absent. Their contents are excluded from Git. Opening an existing
configuration preserves its explicit paths; paths used by the Docker lab are
not replaced either.

A table without a watermark, eligible PK, or eligible UNIQUE key uses
approximate metadata for pre-admission, without `COUNT_BIG` on the Source. The
actual number of rows copied by BCP is checked against the limit before
publication/import.

The executor-visible path and Bronze SQL Server-visible path may use different
syntax, but they must point to the same bytes. SQLite control must remain on
local storage, not UNC.

## Connections tab

Source, Bronze, and Landing have independent parameters:

- instance;
- required port, separate from the instance;
- database;
- schema;
- optional DSN;
- authentication and username;
- TLS.

This separation naturally supports both a perimeter in which Landing and
Bronze share an instance and one in which all three endpoints use different
instances and ports.

For Source, **Banco de dados** is the database being read; there is no second
**Banco para leitura** field. The Bronze schema starts blank and must be
provided. The Landing schema inherits the current Bronze value when
appropriate, but remains editable and required.

The TLS option is displayed as **Confiar no certificado do servidor**.

### Authentication and passwords

The interface supports:

- Windows integrated authentication;
- SQL Server authentication;
- explicit Windows credentials.

The **Domínio** field is editable only with Windows credentials. The screen
does not display a technical secret-reference box. When an operation requires
a password, the interface requests it in a masked dialog and retains it only
for the necessary session. The password is neither saved in JSON nor displayed
in status output.

The JSON/CLI contract also supports the advanced `prompt`, `env`, and
`windows_credential_manager` providers. When opening advanced configurations,
validate the saved JSON before reusing it in automation.

## Tables tab

To add or edit a table, provide:

- required source database, inherited from the Source connection;
- required source schema, inherited from the Source connection;
- source table;
- required destination database, inherited from the Bronze connection;
- required destination schema, inherited from the Bronze connection;
- destination table, automatically suggested as
  `<source_database>_<source_table>` in lowercase;
- optional watermark, containing only comma-separated column names, for
  example `data_referencia, sequencial`; cursor direction is always ascending;
- optional per-table batch size, initially equal to the global value;
- **Ativar CDC nesta tabela** flag;
- optional structure profile, initially inherited from the destination;
- optional partitioning, enabled with `dh_carga` by default when adding a
  table.

Inherited databases and schemas, the generated destination table, per-table
batch size, profile, and partitioning use the default-value background. In this
version, editing the database fields does not create a separate per-table
connection: **Banco de dado de origem** must remain equal to
`source.database`, and **Banco de dado de destino** must remain equal to
`bronze_destination.database`, case-insensitively. Validation/save rejects a
mismatch so that the interface does not promise a route the engine cannot
execute. Save and run a separate configuration for another database pair.

An empty watermark means: try an eligible PK, then an eligible UNIQUE key, and
if neither exists, evaluate `DIRECT_KEYLESS`. A provided watermark is only
validated; the engine never discovers or replaces the combination by itself.
The name order in a composite watermark is significant and preserved in the
contract, always with `direction: "ASC"`.

### Downloading and running watermark validation

In the **Adicionar ou Editar tabela** dialog, complete **Banco de dados de
origem**, **Esquema de origem**, **Tabela de origem**, and **Marca d'água
(opcional)**. The watermark accepts one column name or multiple comma-separated
names, such as `data_evento, sequencial`. Do not specify `ASC` or `DESC`: the
cursor is always ascending and preserves the entered order.

Select **Baixar script de validação…** and choose where to save the `.sql`
file. The file already contains, as literals, the database, schema, table, and
columns filled in within the dialog. It leaves no macros for the operator to
replace and does not attempt to discover another combination. If any required
field or column is invalid, the GUI reports the error before opening the save
dialog.

Run the file on the same SQL Server instance as the Source, preferably with the
same read-only credentials the engine will use. The script can be rerun in the
same session and does not create or modify persistent objects. In its single
result row, evaluate primarily:

- `marca_dagua_aceitavel`: `1` means the combination meets the engine
  contract; `0` means rejection or an inconclusive result;
- `marca_dagua_distingue_registros`: reports whether the combination is unique
  in the observed data;
- `registros_com_nulo_na_marca`, `grupos_com_duplicidade`, and
  `registros_duplicados_excedentes`: evidence supporting the decision;
- `decisao_sugerida`: consolidated operational guidance.

A table without an eligible PK/UNIQUE key proves an explicit watermark only
when it is nonempty, contains no NULL in the combination, and has no
duplicates. When an eligible PK/UNIQUE key exists, the engine also accepts a
comparable, ascending, non-NULL watermark; ties are exported as a complete
group.

The file uses `COUNT_BIG` and `GROUP BY` to produce evidence and may scan the
table. Under the default `READ COMMITTED` isolation, this read may acquire
S-locks and block concurrent writes during parts of the scan. `LOCK_TIMEOUT`
limits only how long the script waits for locks held by others; it does not
limit the duration of locks acquired by the read itself. Run it during an
appropriate operational window and, with database administration, evaluate a
compatible index or already-enabled row-versioning isolation. The script does
not use `NOLOCK`, because dirty reads cannot safely prove uniqueness.

The table grid uses one header row and consistent naming. Rows may be reordered,
and their order determines sequential processing.

### CDC

If no table is selected, the engine neither attempts to enable database CDC
nor queries its retention. If at least one table is selected, database CDC is
checked/enabled once. When the cleanup job already exists, its retention is
adjusted and confirmed during this preflight. In a newly enabled database, SQL
Server may create the job only after the first CDC table; in that case, the
engine enables/confirms that table, adjusts and confirms retention, and only
then starts any BCP operation. The result is cached for subsequent tables.
Global retention is persisted in JSON as `cdc_retention_minutes`.

If the configured value differs from the current value, the engine applies the
change with `sys.sp_cdc_change_job`, restarts only the `cleanup` job through
`sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job` for immediate effect, and confirms
the value before releasing BCP. The `capture` job is not restarted. Status
output displays database, retention, and table events, and the JSON report
persists consolidated evidence under `cdc_database`.

A database preflight or retention-confirmation failure blocks marked CDC
tables with a diagnostic, without preventing unmarked tables from running. The
initial absence of the job is an expected pending state, not a failure. An
isolated failure enabling one CDC table skips that table and proceeds to the
next.

### Partitioning

When adding a table, partitioning is enabled and filled with `dh_carga`. It is
created only while this optional parameter remains enabled and populated; the
operator may disable it or provide another column. The column must exist at the
destination and have type `DATETIME2(7)`.

Bronze and Landing receive a `RANGE RIGHT` function and scheme, with monthly
boundaries from the current month through December of the current year plus
six years, all on `PRIMARY`. For `dh_carga`, names use the `mensal` descriptor;
other columns use `attr`. The technical ID remains the `NONCLUSTERED` PK, and
the partition column receives the only `CLUSTERED` index. A redundant simple
nonclustered index on that column is removed from the contract.

Without the parameter, no partitioning object is created. The engine does not
silently repartition an incompatible existing table; that case requires an
explicit migration.

In the standard Bronze and Landing profiles, `dh_carga` records Brasília civil
time. The expression starts from UTC, explicitly applies the SQL Server
`E. South America Standard Time` time zone, and converts the result to
`DATETIME2(7)`; it therefore does not depend on the server time zone. Custom
profiles may replace this expression and remain responsible for their chosen
date/time semantics.

## Run and monitor tab

The screen follows the operational sequence below.

### 1. Prerequisites

Check the ODBC Driver selected under **Geral → Planejamento e ferramentas** and
BCP. The result shows whether the driver was found and the utility's
version/capabilities. This check does not install components.

After **Planejar**, this same area also compares the BCP-file projection with
the free space observed in both paths configured under **Geral → Arquivos e
limites**:

- **Diretório de exportação dos arquivos**, as seen by the executor;
- **Diretório de importação dos arquivos**, as seen by the executor when that
  path is also accessible from this machine.

The panel shows the consolidated raw total for all configured tables, the byte
margin, the total protected by **Fator de segurança**, the projected
operational peak, free space, and the balance for each directory. Per-table
details identify how much each estimate contributes to the total. A table for
which row count or average size could not be estimated is marked
**indisponível**; in that case, the consolidated total and balance are also
unknown instead of assuming zero. The known subtotal remains visible but is
not presented as the complete total.

The protected total is the sum of the per-table reservations, so each part
receives the factor and conservative rounding before summation. Operational
capacity uses the projected peak, and the balance is
`free - minimum_free_space - peak`. When files are retained, the peak
accumulates reservations; when files are deleted only after a confirmed
commit, the peak considers the blocks required simultaneously. The
raw/protected total remains available to show how much will be generated over
the entire execution, even when that amount is not all present on disk at the
same time. The screen also retains the direct comparison
`free - minimum_free_space - protected total`, making it explicit whether the
entire set would fit simultaneously.

Both paths normally represent the same files through different names or
mounts. Capacities are therefore compared separately, and the engine does not
double the projection. When the import path exists only on the SQL Server
host, the executor cannot measure its volume and displays **indisponível**.
This does not mean zero free bytes. Later proof that Bronze sees the same bytes
and validation of the database data/log volumes remain separate controls.

#### Projection cost

Reading free space is one operating-system metadata call per path and has
negligible cost. Size projection, however, requires Source credentials and is
therefore calculated during **Planejar**, not when merely checking the
Driver/BCP.

With the defaults, cardinality comes from `sys.partitions.rows` (approximate
and proportional to the partition count), while average size is obtained from
`TOP (10000)` with `DATALENGTH` over the exported columns. Size-estimation work
is therefore limited to at most 10,000 rows per table plus small catalog
queries; it is neither an export nor an intentional full scan. Cost grows
approximately with `tables × sampled_rows × exported_columns` and may cause
page reads when the sample is not cached.

Planning cardinality always comes from metadata; the contract performs no full
scan for that purpose. This does not change validation of an explicit
watermark without a PK/UNIQUE key: to prove uniqueness, the validation script
uses aggregation and may scan the table. The sample is based on convenience,
and actual row formats/values may vary; the safety factor reduces this
uncertainty but does not turn the projection into a guarantee. The engine
continues to revalidate limits, rows actually copied, and space during
execution.

### 2. Plan

Select **Planejar** and, when requested, enter the masked password for each
environment. Planning is read-only. In addition to per-table results, the
screen shows the effective Source, Landing, and Bronze username, instance,
port, and database.

### 3. DDL

**Gerar DDL** writes scripts for Bronze, Landing, or both. **Aplicar DDL**
requests confirmation and applies the idempotent scripts. Landing receives
structure only, including evolution and partitioning when configured; it never
receives rows. When running from source, **Diretório dos scripts** starts at
`<project root>\Local\BulkFlow\ddl`.

### 4. Data

- **Executar nova carga:** creates a UUID, exports from Source, and imports only
  into Bronze.
- **Retomar:** uses the same UUID and durable checkpoints.
- **Consultar status:** reads local SQLite control.
- **Importar manifestos:** imports published artifacts without querying Source.

Long-running tasks execute outside the UI thread. The window prevents a second
operation and does not offer forced cancellation. After a disruption, retain
the UUID, SQLite, artifacts, and the four control tables under `DBRO684.dbo`;
reopen the same configuration and select **Retomar**.

Resume occurs by logical block, never by byte or row within a `.partial` file.
A block with a proven SQL commit is not inserted again. If free space on the
Bronze volumes is demonstrably insufficient, the table is marked and skipped
before import, and processing continues with the next table. Capacity is
checked by volume: data and log space are not added together, and the smallest
volume is the constraint. See
[Failures, checkpoints, and resume](FALHAS_E_RETOMADA.md).

## Save in the GUI and run from the CLI

**Abrir** uses the same V2 JSON as the CLI. **Validar configuração** does not
connect to SQL. **Salvar** uses atomic publication and writes only a validated
configuration.

The generated file can be reused without conversion:

```powershell
.\release\BulkFlowCLI.exe prerequisites --config .\config.gui.json
.\release\BulkFlowCLI.exe plan --config .\config.gui.json
.\scripts\launchers\invoke-bcp.ps1 run --config .\config.gui.json --confirm-load
```

```bash
./scripts/launchers/invoke-bcp.sh prerequisites --config ./config.gui.json
./scripts/launchers/invoke-bcp.sh plan --config ./config.gui.json
./scripts/launchers/invoke-bcp.sh run --config ./config.gui.json --confirm-load
```

When opening a file, relative profiles are resolved against that
configuration's directory so that **Salvar como** does not silently change the
profile.

## Automated interface validation

JSON assembly rules live in `bcp_engine/gui_model.py` and have display-free
tests. The window smoke test runs when the environment provides a graphical
session.

```powershell
python -m unittest discover -s tests -p "test_gui_v2.py" -v
```
