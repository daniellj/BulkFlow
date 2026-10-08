**English** | [Português (Brasil)](MIGRACAO_CONFIGURACAO_V2.pt-BR.md)

# Migrating a Portuguese configuration to the V2 contract

This guide converts the preliminary Portuguese contract documented in the
original scope into the V2 contract that the engine actually accepts. Migration
is manual and explicit: the loader does not maintain aliases and rejects every
unknown property. Preserve the previous file as evidence and create a new JSON
file.

Parameter, variable, and code identifier names always use American English.
The graphical interface presents labels, options, and defaults in Brazilian
Portuguese. The `perimeter` business identifiers are the deliberate exception:
they must be persisted exactly as `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`. Physical
names for persistent control objects also follow the Portuguese business naming
convention; this does not create aliases for JSON parameters.

## Root mapping

| Previous contract | V2 contract | Notes |
|---|---|---|
| `versao_configuracao` | `config_version` | Must be the number `2`. |
| `perimetro` | `perimeter` | Exact enum: `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`. |
| — | `scope` | Optional free-form description; it does not replace `perimeter`. |
| `origem` | `source` | Required endpoint. |
| `destino_bronze` | `bronze_destination` | Compatible internal key shown as **Destino 01** in the GUI. |
| `destino_landing` | `landing_destination` | Compatible internal key shown as **Destino 02** in the GUI. |
| `destino_ativo` | `active_destination` | Area ID of the data-capable destination: `bronze` or `landing`; optional in the input because roles derive it, and an explicit value is validated. |
| `executar_importacao` | `execute_import` | With `false`, the flow exports only. |
| `criar_estrutura_se_necessario` | `create_structure_if_needed` | Boolean; default `true`. The GUI displays **Criar estrutura se necessário**, selected by default. |
| `diretorio_executor` | `executor_directory` | **Diretório de exportação dos arquivos** in the GUI; absolute path visible to the Python/BCP process. |
| `diretorio_sql_destino` | `destination_sql_directory` | **Diretório de importação dos arquivos** in the GUI; absolute path to the same bytes as seen by SQL Server. |
| — | `artifact_reader_sids` | Optional list of specific Windows SIDs that must read the artifacts; default `[]`. |
| — | `artifact_writer_sids` | Exceptional list of trusted SIDs that write through a different SMB identity; default `[]`. |
| `diretorio_controle_local` | `local_control_directory` | Must be on a local disk; UNC is not accepted for SQLite/WAL. |
| `linhas_por_bloco` | `rows_per_block` | Global value; each table may override it. |
| `arquivo_maximo_bytes` | `max_file_bytes` | Operational limit in bytes; default `157286400` (150 MiB). |
| `espaco_livre_minimo_bytes` | `minimum_free_space_bytes` | Reserved free space in bytes. |
| `apagar_arquivos_confirmados` | `delete_confirmed_files` | Effective only after a confirmed import. |
| `continuar_apos_erro_tabela` | `continue_after_table_error` | Does not turn a global failure into a per-table failure. |
| `estimativas` | `estimates` | See the nested-sections table. |
| `loteamento` | `batching` | See the nested-sections table. |
| `estrutura` | `structure` | See the nested-sections table. |
| `id_evento_bronze` | `bronze_event_id` | See the nested-sections table. |
| `tabelas` | `tables` | Non-empty array. |

Operational parameters that were absent from the preliminary example use only
their V2 names: `odbc_driver`, `bcp_executable`,
`connection_timeout_seconds`, `sql_timeout_seconds`,
`bcp_timeout_seconds`, `control_schema`, `artifact_reader_sids`,
`artifact_writer_sids`, and `tls`. Do not create translations or aliases for
them. ACL lists do not accept account names or broad groups; use specific
service-account SIDs and grant write access only to the effective SMB identity
that is already inside the trust boundary.

`control_schema` must be `dbo`. In the active data destination, SQL control
state is persisted as `dbo.ctl_exec`, `dbo.ctl_exec_tabela`, and
`dbo.ctl_exec_lote`; the physical schema version is stored in
`dbo.ctl_exec_versao`. The schema is not
interchangeable. Legacy names are handled only through an explicit
administrative migration and must not be reused in new configurations.

For the authorized migration to this contract, remove the SQL tables from the
old `controle_transferencia` schema in dependency order (`execucao_lote`,
`execucao_tabela`, `execucao`, `versao_esquema`), then remove the schema itself.
Recreate the contract under `dbo` in the active data destination. This operation does not affect the
local `controle_transferencia.sqlite3` file, which has an independent purpose
and lifecycle.

## Endpoints, authentication, and TLS

The mapping below applies inside `source`, `bronze_destination`, and
`landing_destination`, according to the applicability of each field.

| Previous contract | V2 contract |
|---|---|
| `instancia` | `instance` |
| `porta` | `port` |
| `banco` | `database` |
| `banco_leitura` | `read_database` |
| `esquema` | `schema` |
| `perfil_estrutura` | `structure_profile` |
| `funcao` | `role` |
| `autenticacao` | `authentication` |
| `tipo` | `type` |
| `usuario` | `username` |
| `dominio` | `domain` |
| `senha` | `password` |
| `provedor` | `provider` |
| `referencia` | `reference` |

Convert enumerated values as well:

| Previous value | V2 value |
|---|---|
| `windows_integrada` | `windows_integrated` |
| `windows_credencial` | `windows_credentials` |
| `sql` | `sql` |
| `prompt` | `prompt` |
| `provedor_dados` | `data_provider` |
| `estrutura_e_dados` | `structure_and_data` |
| `somente_estrutura` | `structure_only` |
| `somente_dados` | `data_only` |

`source.role` must be `data_provider`. Destination roles are
`structure_and_data`, `structure_only`, and `data_only`. Defaults are
`structure_and_data` for `bronze_destination` (`BD_DESTINO_01`) and
`structure_only` for `landing_destination` (`BD_DESTINO_02`). At most one
configured destination may include data. Exactly one is required when
`execute_import=true`; with `false`, zero or one is allowed. Its compatible
area ID becomes `active_destination`. `structure_only` never reads BCP files or receives rows.
`data_only` requires a compatible existing layout and prohibits object,
evolution, and index creation.

Never migrate a literal password. `password` must contain only the secret
descriptor. For `provider: "prompt"`, use `reference: null`; for `env` or
`windows_credential_manager`, provide a non-empty reference. Authentication is
independent for each endpoint. Use distinct references such as
`BCP_SOURCE_SQL_PASSWORD`, `BCP_BRONZE_SQL_PASSWORD`, and
`BCP_LANDING_SQL_PASSWORD`.

For `sql` authentication, the perimeter provides only a suggested username:
`DESENVOLVIMENTO` → `u684`, `HOMOLOGAÇÃO` → `h684`, and `PRODUÇÃO` → `s684`. The `username`
field on each endpoint may override that suggestion, including with three
completely different users.

`port` is required in new configurations and accepts values from 1 through
65535. The engine normalizes legacy V2 files that omit it to `1433`, but always
specify the port when editing or saving a configuration. The connection target
is assembled from `instance` and `port`; do not duplicate `,port` inside
`instance`.

`read_database` remains available as an advanced source JSON option and
defaults to `database` when omitted. The GUI displays only **Banco de dados**.

Global TLS settings use `tls.encrypt`, `tls.trust_server_certificate`,
`tls.hostname_in_certificate`, and `tls.bcp_switch`. Each endpoint may override
the same block. Accepted `bcp_switch` values are `-Ys`, `-Ym`, and `-Yo`.

## Operational sections

| Previous contract | V2 contract | Value conversion |
|---|---|---|
| `estimativas.contagem` | `estimates.row_count_method` | Only `metadados` → `metadata`; the former full row count is not accepted. |
| `estimativas.amostra_maxima_linhas` | `estimates.maximum_sample_rows` | Positive integer |
| `estimativas.fator_seguranca` | `estimates.safety_factor` | Number greater than or equal to `1` |
| — | `estimates.on_unavailable` | New: `stop` or `warn` |
| `loteamento.politica_nulos` | `batching.null_policy` | `rejeitar_tabela` → `reject_table` |
| `loteamento.empates` | `batching.tie_policy` | `grupo_completo` → `complete_group` |
| `loteamento.limite_superior` | `batching.upper_bound_policy` | `capturar_no_inicio_da_tabela` → `capture_at_table_start` |
| `loteamento.exigir_indice_marca_dagua` | `batching.require_watermark_index` | Boolean |
| `estrutura.momento_indices_secundarios` | `structure.secondary_indexes_phase` | `apos_carga_tabela` → `after_table_load`; before load → `before_load` |
| `id_evento_bronze.estrategia` | `bronze_event_id.strategy` | `sequence` or `source_column` |
| `id_evento_bronze.nome_sequence` | `bronze_event_id.sequence_name` | With `sequence`, must contain `{destination_table}`. |
| `id_evento_bronze.coluna_origem` | `bronze_event_id.source_column` | Required only with `source_column`. |

## Tables and watermarks

| Previous contract | V2 contract |
|---|---|
| `origem` | `source_table` |
| `destino` | `destination_table` |
| `banco_origem` | `source_database` |
| `banco_destino` | `destination_database` |
| `esquema_origem` | `source_schema` |
| `esquema_destino` | `destination_schema` |
| `marca_dagua` | `watermark` |
| `marca_dagua.colunas` | `watermark.columns` |
| `nome` | `name` |
| `ordem` | `direction` |
| `linhas_por_bloco` | `rows_per_block` |
| `coluna_particionamento` | `partition_column` |
| `perfil_estrutura` | `structure_profile` |

A previous composite watermark such as:

```json
{"marca_dagua":{"colunas":[{"nome":"data_referencia","ordem":"ASC"}]}}
```

becomes:

```json
{"watermark":{"columns":[{"name":"data_referencia","direction":"ASC"}]}}
```

`watermark: null` does not mean an empty column. It requests an eligible primary
key, then an eligible UNIQUE key, and, if neither exists, evaluates the limited
direct-load path. An explicit watermark is validated as provided; the engine
does not search for a different combination to replace it. `direction` accepts
only `ASC`; old configurations that use `DESC` must be corrected before
execution.

Schemas, tables, columns, constraints, indexes, and sequences created at the
destination are materialized in lowercase. The database retains its configured
name. Source and destination schemas are independent; do not automatically copy
`source.schema` to the destination.

In the GUI, database, schema, and table are presented in that order on both
sides of the mapping. `source_database`, `source_schema`,
`destination_database`, and `destination_schema` are initialized from their
corresponding endpoints and remain editable. In JSON, database and schema fields
remain optional overrides and inherit their endpoints when omitted.

In this version, however, per-table database fields do not implement independent
routing: `source_database` must match `source.database`, and
`destination_database` must match the endpoint selected by
`active_destination`, case-insensitively. Validation rejects mismatches. This
preserves one Source connection and one active data-destination connection per
execution. Use a separate configuration and execution to operate on another
database.

## New fields with no direct equivalent

- `keyless_direct_load_max_rows`: global row limit for a table without a primary
  key, UNIQUE key, or proven watermark; default `5000000`; `0` disables the
  exception. Pre-admission uses approximate metadata and never runs `COUNT_BIG`
  on the Source; the actual row count produced by BCP is checked before import.
- `allow_schema_evolution`: additive evolution for destinations whose role
  includes structure; default `false`. `data_only` always prohibits evolution.
- `tables[].enable_cdc`: per-table decision; default `false`. Database CDC is
  handled only when at least one table sets it to `true`.
- `cdc_retention_minutes`: global CDC cleanup retention period on the Source,
  in minutes; default `262800` (six months, approximately 182.5 days). Accepted
  values range from `1` through `52494800`, the SQL Server limit, and the value
  is queried or applied only when at least one table has `enable_cdc=true`. If
  the job does not exist immediately after the database is enabled, the value
  remains pending until the first CDC table is enabled. When the value changes,
  the engine runs `sys.sp_cdc_change_job`, restarts only the cleanup job with
  `sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job`, confirms retention before any
  BCP operation, and reuses the result for subsequent tables. The `capture` job
  is not restarted.
- `tables[].source_database` and `tables[].destination_database`: inherit the
  endpoint databases when omitted and, in this version, must match them; they do
  not create independent per-table connections.
- `tables[].partition_column`: optional `DATETIME2(7)` column for monthly
  destination partitioning. The GUI enables `dh_carga` by default for a newly
  added table; omitting `partition_column` in JSON disables partitioning.
- `perimeter`: exact global enum `DESENVOLVIMENTO`, `HOMOLOGAÇÃO`, or `PRODUÇÃO`; default
  `DESENVOLVIMENTO`.
- `tables[].destination_area`, when present, must equal the compatible area ID
  selected by `active_destination`: `bronze` or `landing`.
- Secret providers `env` and `windows_credential_manager` had no aliases in the
  preliminary example; use these V2 values directly.
- `odbc_dsn`: optional DSN per endpoint. It does not replace the endpoint's
  other connection data and does not carry a password.
- `estimates.on_unavailable`: selects `stop` or `warn` when an estimate cannot
  be obtained.

When `partition_column` is provided, the profile creates monthly `RANGE RIGHT`
partitioning from the current month through December of the current year plus
six years. The technical primary key becomes `NONCLUSTERED`, and the selected
column receives the `CLUSTERED` index. The engine does not silently convert an
existing nonpartitioned table to partitioned form, or the reverse.

An omitted table destination becomes `<source_database>_<source_table>` in
lowercase, using the table's effective source database inherited from
`source.database`. An omitted `source.read_database` receives
`source.database`. With `execute_import: false`, the active destination is not
contacted for data and files are preserved. An export-only configuration may
omit both a data-capable destination and `active_destination`.

## Defaults and interface presentation

The contract's principal defaults are: `DESENVOLVIMENTO` perimeter,
`data_provider` Source role, `structure_and_data` for `BD_DESTINO_01`,
`structure_only` for `BD_DESTINO_02`, `bronze` as the compatible active area,
import enabled, automatic structure creation enabled, 200,000 rows
per block, maximum file size of 157,286,400 bytes, keyless limit of 5,000,000,
safety factor `1.25`, `dbo` control schema, schema evolution disabled, CDC
retention of 262,800 minutes, per-table CDC disabled, metadata estimates,
indexes before load, encrypted TLS, and no automatic trust of the server
certificate. For a new table, the GUI suggests partitioning by `dh_carga`; in
JSON, omitting `partition_column` continues to disable partitioning.

The pt-BR GUI displays localized values. This English guide refers to the role
values with the following natural translations:

| English guide | Value persisted in JSON |
|---|---|
| `DESENVOLVIMENTO` / `HOMOLOGAÇÃO` / `PRODUÇÃO` | same exact value in `perimeter` |
| `data provider` | `data_provider` |
| `structure and data` | `structure_and_data` |
| `structure only` | `structure_only` |
| `data only (premise: the structure must already exist)` | `data_only` |
| `Sim` / selected checkbox | `true` |
| `Não` / cleared checkbox | `false` |
| `Metadados (aproximado)` | `metadata` |
| `Após a carga da tabela` | `after_table_load` |
| `Antes da carga` | `before_load` |
| `Solicitar ao executar` | `prompt` |
| `Variável de ambiente` | `env` |

The laboratory scenario deliberately uses 1,500 rows per block and lower limits
for validation. That is a laboratory-specific pt-BR configuration and does not
change the general contract defaults.

## Secret-free V2 example

```json
{
  "config_version": 2,
  "perimeter": "HOMOLOGAÇÃO",
  "scope": "Migration example",
  "source": {
    "role": "data_provider",
    "instance": "SQL-ORIGEM\\INST01",
    "port": 1433,
    "database": "BASE_ORIGEM",
    "schema": "dbo",
    "authentication": {
      "type": "sql",
      "username": "login_leitura",
      "password": {
        "provider": "env",
        "reference": "BCP_SOURCE_SQL_PASSWORD"
      }
    }
  },
  "bronze_destination": {
    "role": "structure_and_data",
    "instance": "SQL-DESTINO-01\\INST01",
    "port": 1433,
    "database": "BD_DESTINO_01",
    "schema": "fonte_a",
    "structure_profile": "templates/bronze.json",
    "authentication": {
      "type": "sql",
      "username": "login_carga",
      "password": {
        "provider": "env",
        "reference": "BCP_BRONZE_SQL_PASSWORD"
      }
    }
  },
  "landing_destination": {
    "role": "structure_only",
    "instance": "SQL-DESTINO-02\\INST01",
    "port": 1433,
    "database": "BD_DESTINO_02",
    "schema": "fonte_a",
    "structure_profile": "templates/landing.json",
    "authentication": {
      "type": "sql",
      "username": "login_estrutura",
      "password": {
        "provider": "env",
        "reference": "BCP_LANDING_SQL_PASSWORD"
      }
    }
  },
  "active_destination": "bronze",
  "execute_import": true,
  "create_structure_if_needed": true,
  "executor_directory": "D:\\BCP_Data",
  "destination_sql_directory": "\\\\ARQUIVOS\\cargas\\BCP_Data",
  "local_control_directory": "C:\\BCP\\controle",
  "rows_per_block": 200000,
  "max_file_bytes": 157286400,
  "keyless_direct_load_max_rows": 5000000,
  "allow_schema_evolution": false,
  "cdc_retention_minutes": 262800,
  "control_schema": "dbo",
  "structure": {"secondary_indexes_phase": "before_load"},
  "tables": [
    {
      "source_database": "BASE_ORIGEM",
      "source_schema": "dbo",
      "source_table": "CLIENTE",
      "destination_database": "BD_DESTINO_01",
      "destination_schema": "fonte_a",
      "destination_table": "base_origem_cliente",
      "enable_cdc": false,
      "partition_column": "dh_carga",
      "watermark": null
    }
  ]
}
```

The environment-variable reference is not the password. Define the value only
in the execution context, and never store it in JSON, a command line, or logs.

## Validate before execution

1. Validate only the local contract, without opening a SQL connection:

   ```powershell
   python -c "from bcp_engine.config import read_config; read_config(r'.\config.v2.json'); print('Valid V2 configuration')"
   ```

   ```bash
   python -c 'from bcp_engine.config import read_config; read_config("./config.v2.json"); print("Valid V2 configuration")'
   ```

2. Run the read-only plan and review source, destination, key strategy,
   watermark proof, block count, space, and diagnostics:

   ```powershell
   .\scripts\launchers\invoke-bcp.ps1 plan --config .\config.v2.json
   ```

   ```bash
   ./scripts/launchers/invoke-bcp.sh plan --config ./config.v2.json
   ```

3. Only then run the load with explicit confirmation:

   ```powershell
   .\scripts\launchers\invoke-bcp.ps1 run --config .\config.v2.json --confirm-load
   ```

   ```bash
   ./scripts/launchers/invoke-bcp.sh run --config ./config.v2.json --confirm-load
   ```

Do not keep both the old and new key in the same file: V2 validation rejects the
old key. It also rejects conflicting authentication fields, duplicate
destination names, perimeters outside the exact enum, zero or multiple
data-capable destinations, an `active_destination` inconsistent with roles,
and table overrides that target a different destination.
