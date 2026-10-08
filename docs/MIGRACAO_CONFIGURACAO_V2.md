# Migração da configuração em português para o contrato V2

Este guia converte o contrato preliminar em português documentado no escopo
para o contrato V2 efetivamente aceito pelo motor. A migração é manual e
explícita: o carregador não mantém aliases e rejeita qualquer propriedade
desconhecida. Preserve o arquivo anterior como evidência e crie um novo JSON.

Os nomes de parâmetros, variáveis e identificadores de código são sempre em
inglês americano. A interface gráfica apresenta rótulos, opções e defaults em
português do Brasil. Os identificadores de negócio de `perimeter` são a exceção
deliberada: devem ser persistidos exatamente como `DREADS`, `HOMOLOGAÇÃO` ou
`CAPGV`. Os nomes físicos dos objetos persistentes de controle também seguem a
convenção em português: isso não cria aliases para os parâmetros JSON.

## Mapeamento da raiz

| Contrato anterior | Contrato V2 | Observação |
|---|---|---|
| `versao_configuracao` | `config_version` | Deve ser o número `2`. |
| `perimetro` | `perimeter` | Enum exato: `DREADS`, `HOMOLOGAÇÃO` ou `CAPGV`. |
| — | `scope` | Descrição livre opcional; não substitui `perimeter`. |
| `origem` | `source` | Endpoint obrigatório. |
| `destino_bronze` | `bronze_destination` | Obrigatório quando Bronze é o destino ativo e há importação. |
| `destino_landing` | `landing_destination` | Usado somente para gerar/aplicar DDL Landing. |
| `destino_ativo` | `active_destination` | Deve ser `bronze`; Landing não recebe dados. |
| `executar_importacao` | `execute_import` | Com `false`, o fluxo é somente exportação. |
| `criar_estrutura_se_necessario` | `create_structure_if_needed` | Booleano; default `true`. A GUI exibe **Criar estrutura se necessário**, marcada por padrão. |
| `diretorio_executor` | `executor_directory` | **Diretório de exportação dos arquivos** na GUI; caminho absoluto visto pelo processo Python/BCP. |
| `diretorio_sql_destino` | `destination_sql_directory` | **Diretório de importação dos arquivos** na GUI; caminho absoluto dos mesmos bytes, visto pelo SQL Server. |
| — | `artifact_reader_sids` | Lista opcional de SIDs Windows específicos que precisam ler os artefatos; default `[]`. |
| — | `artifact_writer_sids` | Lista excepcional de SIDs confiáveis que escrevem via identidade SMB distinta; default `[]`. |
| `diretorio_controle_local` | `local_control_directory` | Deve estar em disco local; UNC não é aceito para SQLite/WAL. |
| `linhas_por_bloco` | `rows_per_block` | Global; pode ser sobrescrito por tabela. |
| `arquivo_maximo_bytes` | `max_file_bytes` | Limite operacional em bytes; default `157286400` (150 MiB). |
| `espaco_livre_minimo_bytes` | `minimum_free_space_bytes` | Reserva em bytes. |
| `apagar_arquivos_confirmados` | `delete_confirmed_files` | Só é efetivo após importação confirmada. |
| `continuar_apos_erro_tabela` | `continue_after_table_error` | Não transforma falha global em falha por tabela. |
| `estimativas` | `estimates` | Veja a tabela de seções aninhadas. |
| `loteamento` | `batching` | Veja a tabela de seções aninhadas. |
| `estrutura` | `structure` | Veja a tabela de seções aninhadas. |
| `id_evento_bronze` | `bronze_event_id` | Veja a tabela de seções aninhadas. |
| `tabelas` | `tables` | Array não vazio. |

Parâmetros operacionais que não apareciam no exemplo preliminar usam somente
os nomes V2: `odbc_driver`, `bcp_executable`,
`connection_timeout_seconds`, `sql_timeout_seconds`,
`bcp_timeout_seconds`, `control_schema`, `artifact_reader_sids`,
`artifact_writer_sids` e `tls`. Não crie traduções ou aliases para eles. As
listas de ACL não aceitam nomes de conta nem grupos amplos; use SIDs específicos
de contas de serviço e conceda escrita apenas à identidade SMB efetiva que já
faça parte da fronteira de confiança.

`control_schema` deve conter obrigatoriamente `dbo`. Na Bronze do laboratório,
o controle SQL é persistente exclusivamente em `DBRO684.dbo.execucao`,
`DBRO684.dbo.execucao_tabela` e `DBRO684.dbo.execucao_lote`; a versão física
fica em `DBRO684.dbo.versao_esquema`. O schema não é intercambiável. Nomes
legados são tratados apenas por uma migração administrativa explícita e não
devem ser reutilizados em configurações novas.

Na migração autorizada para esse contrato, remova as tabelas SQL do antigo
schema `controle_transferencia` na ordem das dependências
(`execucao_lote`, `execucao_tabela`, `execucao`, `versao_esquema`) e então
remova o próprio schema. Recrie o contrato em `DBRO684.dbo`. Essa operação não
atinge o arquivo local `controle_transferencia.sqlite3`, que possui finalidade e
ciclo de vida independentes.

## Endpoints, autenticação e TLS

O mapeamento abaixo vale dentro de `source`, `bronze_destination` e
`landing_destination`, conforme a aplicabilidade de cada campo.

| Contrato anterior | Contrato V2 |
|---|---|
| `instancia` | `instance` |
| `porta` | `port` |
| `banco` | `database` |
| `banco_leitura` | `read_database` |
| `esquema` | `schema` |
| `perfil_estrutura` | `structure_profile` |
| `autenticacao` | `authentication` |
| `tipo` | `type` |
| `usuario` | `username` |
| `dominio` | `domain` |
| `senha` | `password` |
| `provedor` | `provider` |
| `referencia` | `reference` |

Também converta os valores enumerados:

| Valor anterior | Valor V2 |
|---|---|
| `windows_integrada` | `windows_integrated` |
| `windows_credencial` | `windows_credentials` |
| `sql` | `sql` |
| `prompt` | `prompt` |

Nunca migre uma senha literal. `password` deve conter somente o descritor do
segredo. Para `provider: "prompt"`, use `reference: null`; para `env` ou
`windows_credential_manager`, informe uma referência não vazia. Cada endpoint
tem autenticação independente. Use referências distintas, por exemplo
`BCP_SOURCE_SQL_PASSWORD`, `BCP_BRONZE_SQL_PASSWORD` e
`BCP_LANDING_SQL_PASSWORD`.

Para autenticação `sql`, o perímetro fornece apenas o usuário sugerido:
`DREADS` → `u684`, `HOMOLOGAÇÃO` → `h684` e `CAPGV` → `s684`. O campo
`username` de cada endpoint pode sobrescrever essa sugestão, inclusive com três
usuários completamente diferentes.

`port` é obrigatório em configurações novas e aceita valores de 1 a 65535. O
motor normaliza arquivos V2 legados sem esse campo para `1433`, mas ao editar ou
salvar a configuração informe a porta explicitamente. A conexão é formada a
partir de `instance` e `port`; não duplique `,porta` dentro de `instance`.

`read_database` permanece aceito como opção avançada do JSON da Origem e, se
omitido, recebe `database`. A GUI apresenta apenas **Banco de dados**.

TLS global usa `tls.encrypt`, `tls.trust_server_certificate`,
`tls.hostname_in_certificate` e `tls.bcp_switch`. O mesmo bloco pode ser
sobrescrito por endpoint. Os valores de `bcp_switch` aceitos são `-Ys`, `-Ym` e
`-Yo`.

## Seções operacionais

| Contrato anterior | Contrato V2 | Conversão de valor |
|---|---|---|
| `estimativas.contagem` | `estimates.row_count_method` | Somente `metadados` → `metadata`; a antiga contagem integral não é aceita. |
| `estimativas.amostra_maxima_linhas` | `estimates.maximum_sample_rows` | Inteiro positivo |
| `estimativas.fator_seguranca` | `estimates.safety_factor` | Número maior ou igual a `1` |
| — | `estimates.on_unavailable` | Novo: `stop` ou `warn` |
| `loteamento.politica_nulos` | `batching.null_policy` | `rejeitar_tabela` → `reject_table` |
| `loteamento.empates` | `batching.tie_policy` | `grupo_completo` → `complete_group` |
| `loteamento.limite_superior` | `batching.upper_bound_policy` | `capturar_no_inicio_da_tabela` → `capture_at_table_start` |
| `loteamento.exigir_indice_marca_dagua` | `batching.require_watermark_index` | Booleano |
| `estrutura.momento_indices_secundarios` | `structure.secondary_indexes_phase` | `apos_carga_tabela` → `after_table_load`; antes da carga → `before_load` |
| `id_evento_bronze.estrategia` | `bronze_event_id.strategy` | `sequence` ou `source_column` |
| `id_evento_bronze.nome_sequence` | `bronze_event_id.sequence_name` | Com `sequence`, deve conter `{destination_table}`. |
| `id_evento_bronze.coluna_origem` | `bronze_event_id.source_column` | Obrigatória apenas com `source_column`. |

## Tabelas e marca d'água

| Contrato anterior | Contrato V2 |
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

Uma marca composta anterior como:

```json
{"marca_dagua":{"colunas":[{"nome":"data_referencia","ordem":"ASC"}]}}
```

passa a ser:

```json
{"watermark":{"columns":[{"name":"data_referencia","direction":"ASC"}]}}
```

`watermark: null` não significa uma coluna vazia: solicita PK elegível, depois
UNIQUE elegível e, se nenhuma existir, avalia a carga direta limitada. Uma
marca explícita é somente validada; o motor não procura outra combinação para
substituí-la. `direction` aceita exclusivamente `ASC`; configurações antigas
com `DESC` devem ser corrigidas antes da execução.

Schemas, tabelas, colunas, constraints, índices e sequences criados no destino
são materializados em minúsculas. O banco mantém o nome configurado. Origem e
destino têm schemas independentes; não copie automaticamente `source.schema`
para o destino.

Na GUI, banco, schema e tabela são apresentados nesta ordem para cada lado do
mapeamento. `source_database`, `source_schema`, `destination_database` e
`destination_schema` são preenchidos a partir dos endpoints correspondentes e
ficam editáveis. No JSON, os campos de banco e schema continuam sendo
overrides opcionais e, quando omitidos, herdam os endpoints.

Nesta versão, entretanto, os campos de banco por tabela não implementam
roteamento independente: `source_database` deve coincidir com
`source.database`, e `destination_database` deve coincidir com
`bronze_destination.database`, sem diferenciar maiúsculas de minúsculas. A
validação rejeita divergências. Isso preserva o contrato real do motor, que usa
uma conexão Origem e uma conexão Bronze por execução. Para operar outro banco,
use outra configuração/execução.

## Campos novos, sem equivalência direta

- `keyless_direct_load_max_rows`: limite global de linhas para uma tabela sem
  PK, UNIQUE ou marca comprovada; default `5000000`; `0` desabilita a exceção.
  A pré-admissão usa metadados aproximados, sem `COUNT_BIG` na Origem; a
  quantidade real produzida pelo BCP é validada antes da importação.
- `allow_schema_evolution`: evolução aditiva nas estruturas Bronze/Landing;
  default `false`. Desligado, apenas registra colunas ausentes.
- `tables[].enable_cdc`: decisão por tabela; default `false`. O CDC do banco só
  é tratado quando ao menos uma tabela usa `true`.
- `cdc_retention_minutes`: prazo global, em minutos, do job de cleanup CDC na
  Origem; default `262800` (seis meses, aproximadamente 182,5 dias). Só é
  aceito entre `1` e `52494800`, limite do SQL Server, e só é
  consultado/aplicado quando ao menos uma tabela usa `enable_cdc=true`. Se o job
  ainda não existir logo depois de habilitar o banco, o valor fica pendente até
  a primeira tabela CDC ser habilitada. Quando o valor muda, o motor executa
  `sys.sp_cdc_change_job`, reinicia somente o cleanup com
  `sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job`, confirma a retenção antes de
  qualquer BCP e reutiliza o resultado nas tabelas seguintes. O job `capture`
  não é reiniciado.
- `tables[].source_database` e `tables[].destination_database`: herdam os
  bancos dos endpoints quando omitidos e, nesta versão, devem coincidir com
  eles; não criam conexões independentes por tabela.
- `tables[].partition_column`: coluna `DATETIME2(7)` opcional para
  particionamento mensal dos destinos. Ao adicionar uma tabela pela GUI, a
  opção vem habilitada com `dh_carga`; ausente no JSON, desabilita
  particionamento.
- `perimeter`: enum global exato `DREADS`, `HOMOLOGAÇÃO` ou `CAPGV`; default
  `DREADS`.
- `tables[].destination_area`, quando informado, deve ser `bronze`. A Landing
  é gerada pelo perfil estrutural e não participa da importação de linhas.
- Os provedores de segredo `env` e `windows_credential_manager` não tinham
  aliases definidos no exemplo preliminar; use diretamente esses valores V2.
- `odbc_dsn`: DSN opcional por endpoint; não substitui os demais dados do
  endpoint no contrato e não carrega senha.
- `estimates.on_unavailable`: define `stop` ou `warn` quando a estimativa não
  puder ser obtida.

Quando `partition_column` é informado, o perfil cria particionamento mensal
`RANGE RIGHT` do mês corrente até dezembro do ano corrente mais seis anos. A PK
técnica passa a `NONCLUSTERED`, e a coluna escolhida recebe o índice
`CLUSTERED`. O motor não converte silenciosamente uma tabela existente de não
particionada para particionada (nem o inverso).

O destino de uma tabela omitido passa a ser
`<source_database>_<source_table>` em minúsculas, usando o banco efetivo da
tabela (herdado de `source.database`). `source.read_database`
omitido recebe `source.database`. Para `execute_import: false`, o endpoint de
destino e `destination_sql_directory` podem ser omitidos, não há conexão com o
destino e os arquivos são preservados.

## Defaults e apresentação em português

Os principais defaults do contrato são: perímetro `DREADS`, destino de dados
Bronze, importação habilitada, criação automática de estrutura habilitada, lote
de 200.000 linhas, arquivo máximo de 157.286.400 bytes, limite sem chave de
5.000.000, fator de segurança `1.25`, schema de controle `dbo`, evolução de
schema desabilitada, retenção CDC de 262.800 minutos, CDC desabilitado por
tabela, estimativa por metadados, índices antes da carga, TLS criptografado e
certificado não confiado automaticamente. No formulário de uma tabela nova, a
GUI sugere particionamento por `dh_carga`; no JSON, omitir `partition_column`
continua desabilitando o particionamento.

Na interface, esses valores aparecem em português, por exemplo:

| Interface pt-BR | Valor persistido no JSON |
|---|---|
| `DREADS` / `HOMOLOGAÇÃO` / `CAPGV` | mesmo valor exato em `perimeter` |
| `Bronze` | `bronze` |
| `Sim` / caixa marcada | `true` |
| `Não` / caixa desmarcada | `false` |
| `Metadados (aproximado)` | `metadata` |
| `Após a carga da tabela` | `after_table_load` |
| `Antes da carga` | `before_load` |
| `Solicitar ao executar` | `prompt` |
| `Variável de ambiente` | `env` |

O cenário do laboratório usa deliberadamente lote de 1.500 linhas e outros
limites menores para homologação. Isso é um preenchimento pt-BR do laboratório,
não altera os defaults gerais do contrato.

## Exemplo V2 sem segredo

```json
{
  "config_version": 2,
  "perimeter": "HOMOLOGAÇÃO",
  "scope": "Exemplo de migração",
  "source": {
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
    "instance": "SQL-DESTINO\\INST02",
    "port": 1433,
    "database": "BASE_BRONZE",
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
    "instance": "SQL-LANDING\\INST03",
    "port": 1433,
    "database": "BASE_LANDING",
    "schema": "fonte_a",
    "structure_profile": "templates/landing.json",
    "authentication": {
      "type": "sql",
      "username": "login_ddl_landing",
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
      "destination_database": "BASE_BRONZE",
      "destination_schema": "fonte_a",
      "destination_table": "base_origem_cliente",
      "enable_cdc": false,
      "partition_column": "dh_carga",
      "watermark": null
    }
  ]
}
```

A referência de variável de ambiente não é a senha. Defina o valor apenas no
contexto da execução e não o grave no JSON, em linha de comando ou em logs.

## Validar antes de executar

1. Valide somente o contrato local, sem conexão SQL:

   ```powershell
   python -c "from bcp_engine.config import read_config; read_config(r'.\config.v2.json'); print('Configuração V2 válida')"
   ```

   ```bash
   python -c 'from bcp_engine.config import read_config; read_config("./config.v2.json"); print("Configuração V2 válida")'
   ```

2. Execute o plano somente leitura e revise origem, destino, estratégia de
   chave, prova da marca, quantidade de blocos, espaço e diagnósticos:

   ```powershell
   .\scripts\launchers\invoke-bcp.ps1 plan --config .\config.v2.json
   ```

   ```bash
   ./scripts/launchers/invoke-bcp.sh plan --config ./config.v2.json
   ```

3. Só depois execute a carga com confirmação explícita:

   ```powershell
   .\scripts\launchers\invoke-bcp.ps1 run --config .\config.v2.json --confirm-load
   ```

   ```bash
   ./scripts/launchers/invoke-bcp.sh run --config ./config.v2.json --confirm-load
   ```

Não mantenha simultaneamente a chave antiga e a nova: a validação V2 rejeita a
chave antiga. Ela também rejeita campos de autenticação conflitantes, nomes de
destino duplicados, perímetros fora do enum exato, `active_destination`
diferente de `bronze` e overrides de tabela que tentem direcionar dados à
Landing.
