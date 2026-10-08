[English](README.md) | **Português (Brasil)**

# BulkFlow — Exportação/Importação de Dados para SQL Server

Motor configurável para exportar dados com `bcp queryout` e carregá-los de
forma transacional no endpoint Bronze. O endpoint Landing recebe somente DDL
no padrão Landing — criação e evolução aditiva de tabelas — e nunca recebe
linhas. O processo é sequencial por tabela e bloco; as linhas não passam por
listas, DataFrames ou transformação linha a linha em Python.

O motor, a GUI e a CLI são agnósticos de ambiente e infraestrutura. Docker,
Compose, SQL Server em contêiner, portas fixas, `BD_ORIGEM`, `DBRO684` e
`DLAN684` pertencem exclusivamente ao laboratório opcional de testes locais;
não são requisitos de instalação ou operação do produto.

Esta entrega inclui:

- CLI reutilizável em PowerShell e shell Linux;
- interface desktop simples em `ttkbootstrap`;
- perfis exatos Bronze e Landing;
- PK, UNIQUE, marca d'água explícita comprovada e carga direta limitada;
- CDC opcional por tabela;
- retenção global do cleanup CDC configurável, com default de 262.800 minutos;
- evolução aditiva de esquema opcional;
- particionamento mensal opcional nos destinos;
- verificação de espaço no executor e nos volumes do banco Bronze;
- manifestos, SHA-256, controles SQLite/SQL e retomada idempotente;
- executáveis Windows autocontidos para GUI e CLI.

## Início rápido

Instale Python 3.10+ e as dependências:

```powershell
python -m pip install -r requirements.txt
```

Ou, no Windows x64, use o instalador offline recomendado:

```powershell
.\release\Setup-BulkFlow.exe
```

Esse pacote instala a aplicação, o Microsoft ODBC Driver 18 e o utilitário
Microsoft BCP sem acessar a internet. O usuário final não precisa instalar
Python nem pacotes Python. A instalação é elevada porque registra componentes
nativos no Windows e grava a aplicação em `Program Files`. Veja o fluxo
completo em [Instalador offline para Windows](docs/INSTALADOR_OFFLINE.pt-BR.md).

Os binários avulsos também podem ser executados diretamente:

```powershell
.\release\BulkFlowGUI.exe
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
.\release\BulkFlowCLI.exe prerequisites --config .\caminho\config.v2.json
.\release\BulkFlowCLI.exe plan --config .\caminho\config.v2.json
```

Os executáveis avulsos incorporam Python, `pyodbc`, `ttkbootstrap`, `pywinpty`,
o motor, os schemas e os perfis padrão. Eles **não** incorporam nem instalam o
Microsoft ODBC Driver for SQL Server ou o utilitário Microsoft `bcp`; use o
setup offline ou provisione esses pré-requisitos nativos separadamente.

No **executor** — a máquina, VM ou contêiner que roda a GUI/CLI — instale o
Microsoft ODBC Driver 18 e, preferencialmente, BCP 18 ou superior. Uma build
BCP 17 somente é aceita quando comprova os controles TLS `-Y` e `-u`. `pip` não
instala esses componentes nativos. Veja instalação, campos da GUI e
verificações em [Pré-requisitos do executor](docs/PRE_REQUISITOS.pt-BR.md). No
Windows, `pywinpty` fornece o canal privado do prompt de senha; em POSIX, o
motor usa PTY nativa.

Faça a verificação automática antes do planejamento:

```powershell
python .\bcp_bronze.py prerequisites --config .\caminho\config.v2.json
# ou
.\release\BulkFlowCLI.exe prerequisites --config .\caminho\config.v2.json
```

CLI direta:

```powershell
python .\bcp_bronze.py plan --config .\examples\config.full.json
```

Launcher PowerShell:

```powershell
.\scripts\launchers\invoke-bcp.ps1 plan `
  --config .\examples\config.full.json
```

Launcher Linux:

```bash
./scripts/launchers/invoke-bcp.sh plan \
  --config ./examples/config.linux-sql.json
```

Interface gráfica:

```powershell
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
```

Ao criar uma configuração nova pela GUI executada a partir do código-fonte,
os diretórios operacionais padrão são criados automaticamente na raiz do
projeto:

```text
Local\BulkFlow\
├── .bcp-control\
├── bcp-data\
└── ddl\
```

O diretório de importação começa com o mesmo caminho de `bcp-data`. Esses
artefatos locais não são versionados. Os executáveis portáteis iniciados pela
pasta `release` deste projeto usam os mesmos valores locais. Em uma cópia
instalada ou isolada, os defaults permanecem em `%LOCALAPPDATA%\BulkFlow`, pois
a aplicação não grava dados mutáveis em `Program Files`.

Guias:

- [pré-requisitos do executor](docs/PRE_REQUISITOS.pt-BR.md);
- [instalador offline para Windows](docs/INSTALADOR_OFFLINE.pt-BR.md);
- [PowerShell e Linux](docs/USO_CLI_POWERSHELL_LINUX.pt-BR.md);
- [interface gráfica](docs/USO_INTERFACE_GRAFICA.pt-BR.md);
- [falhas, checkpoints e retomada](docs/FALHAS_E_RETOMADA.pt-BR.md);
- [ordem completa do processamento](docs/ORDEM_PROCESSAMENTO.pt-BR.md);
- [migração da configuração em português para o contrato V2](docs/MIGRACAO_CONFIGURACAO_V2.pt-BR.md);
- [migração não destrutiva do controle SQLite legado](docs/MIGRACAO_CONTROLE_SQLITE.pt-BR.md).

## Comandos

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

Códigos de saída:

- `0`: tudo que foi solicitado foi concluído;
- `2`: tabela pulada, erro parcial ou trabalho pendente;
- `1`: falha global;
- `127`: falha de inicialização do launcher.

`plan` é somente leitura. `run` cria e imprime um UUID. `resume` reutiliza
exatamente esse UUID. `import` não consulta a origem e valida o contrato do
manifesto contra o perfil local confiável.

## Fluxo

```text
Origem configurada
  ├─ linhas via BCP ───────────────> Bronze + controle persistente
  └─ metadados para criação/evolução ─> Landing (somente estrutura)
```

```text
configuração V2 + perfil versionado
  -> pré-requisitos nativos e validação da configuração
  -> conexão e identidade efetiva (usuário, instância, porta e banco)
  -> catálogo, tipos e identidade efetiva da origem
  -> CDC do banco uma vez, se ao menos uma tabela solicitar
  -> retenção do cleanup agora, ou pendente até o job existir
  -> CDC da tabela; se pendente, confirmar a retenção antes do primeiro BCP
  -> seleção/prova da chave e captura do teto
  -> estimativa e verificação de espaço no executor e na Bronze
  -> criação/evolução obrigatória da estrutura Bronze
  -> bcp queryout para arquivo .partial
  -> contagem real + SHA-256 + publicação atômica
  -> OPENROWSET(BULK...) com INSERT, controle e checkpoint no mesmo commit
  -> índices secundários
  -> relatório de console, JSON e CSV
```

Não há `bcp in`, linked server, `xp_cmdshell`, `OFFSET`, cursor físico nem
staging integral. Um bloco já confirmado não é reinserido.

A retomada é por bloco lógico, nunca pelo byte ou pela linha de um arquivo
parcial. Uma falha durante o BCP refaz a faixa ainda não publicada; uma falha
antes do commit da carga reverte o bloco inteiro; e um commit SQL comprovado
não é reenviado. O procedimento completo e as matrizes de falha estão em
[Falhas, checkpoints e retomada](docs/FALHAS_E_RETOMADA.pt-BR.md).

A ordem operacional recomendada é: validar pré-requisitos, planejar e informar
as credenciais independentes, gerar/revisar/aplicar o DDL de Bronze e Landing,
e somente então executar a exportação/importação. Com
`create_structure_if_needed=true` — valor padrão — o `run` também cria ou
completa de forma idempotente a estrutura Bronze. Com `false`, o motor apenas
valida a estrutura existente e não cria os objetos ausentes. A sequência
detalhada está em
[Ordem completa do processamento](docs/ORDEM_PROCESSAMENTO.pt-BR.md).

## Configuração

Todos os nomes de parâmetros, variáveis e identificadores de código são em
inglês americano. A GUI apresenta rótulos e defaults em português do Brasil.
Os identificadores de negócio do enum `perimeter` são a exceção deliberada e
devem permanecer exatamente `DESENVOLVIMENTO`, `HOMOLOGAÇÃO` ou `PRODUÇÃO`. Os objetos
persistentes de controle gerados no SQL Server e no SQLite também seguem a
convenção em português descrita adiante. O schema JSON versionado está em
`schemas/config-v2.schema.json`; o carregador também valida semanticamente e
rejeita propriedades desconhecidas.

Parâmetros centrais:

| Parâmetro | Comportamento |
|---|---|
| `perimeter` | enum exato `DESENVOLVIMENTO`, `HOMOLOGAÇÃO` ou `PRODUÇÃO`; default `DESENVOLVIMENTO` |
| `rows_per_block` | 200.000 por padrão; 1.500 é somente o cenário do laboratório |
| `max_file_bytes` | 157.286.400 bytes por padrão (150 MiB) |
| `keyless_direct_load_max_rows` | 5.000.000 por padrão; `0` desabilita |
| `allow_schema_evolution` | `false` por padrão |
| `create_structure_if_needed` | `true` por padrão; pode ser desmarcado na GUI para somente validar estruturas já existentes |
| `execute_import` | `false` produz somente artefatos |
| `enable_cdc` | flag independente em cada tabela |
| `cdc_retention_minutes` | retenção global do cleanup CDC; default `262800` (seis meses, aproximadamente 182,5 dias) |
| `watermark` | `null` ou lista explícita de colunas; a direção é sempre `ASC` |
| `partition_column` | opcional por tabela; ausente desabilita particionamento |
| `estimates.safety_factor` | `1.25` por padrão |
| `control_schema` | valor obrigatório `dbo`; o controle SQL existe exclusivamente em `DBRO684.dbo` no laboratório |
| `structure.secondary_indexes_phase` | `before_load` por padrão |
| `continue_after_table_error` | controla continuação entre tabelas |
| `artifact_reader_sids` | SIDs Windows específicos com leitura/travessia nos artefatos; default `[]` |
| `artifact_writer_sids` | SIDs Windows confiáveis com escrita, somente para identidade SMB efetiva diferente; default `[]` |

Origem, Bronze e Landing têm `instance`, `port`, `database`, schema,
autenticação e segredo independentes. Por isso, a mesma configuração atende
tanto ambientes em que Landing e Bronze compartilham uma instância quanto
ambientes em que cada endpoint reside em uma instância distinta. Os usuários
sugeridos pelo perímetro são
`u684` para `DESENVOLVIMENTO`, `h684` para `HOMOLOGAÇÃO` e `s684` para `PRODUÇÃO`; cada um
pode ser alterado no endpoint. São suportados:

- `windows_integrated`;
- `windows_credentials`, por contexto Windows dedicado;
- `sql`, com senha obtida por `prompt`, `env` ou
  `windows_credential_manager`.

A senha SQL do BCP nunca entra em `-P`, argv, manifesto ou log. O motor responde
ao prompt mascarado por pseudoconsole/PTY e aplica redação centralizada.
No laboratório, as referências independentes são `BCP_SOURCE_SQL_PASSWORD`,
`BCP_BRONZE_SQL_PASSWORD` e `BCP_LANDING_SQL_PASSWORD`, ainda que as três
resolvam para o mesmo valor de teste.

Cada item de `tables` também pode persistir `source_database` e
`destination_database`. Na GUI, esses campos herdam, respectivamente, os
bancos dos endpoints Origem e Bronze. Nesta versão eles tornam o mapeamento
explícito, mas **não criam rotas independentes por tabela**: quando informados,
devem coincidir, sem diferenciar maiúsculas de minúsculas, com
`source.database` e `bronze_destination.database`. Uma divergência é rejeitada
na validação, em vez de carregar silenciosamente no banco errado. O nome de
destino sugerido continua sendo `<source_database>_<source_table>` em
minúsculas.

## Chave, marca d'água e tabela sem chave

A ordem de decisão é:

```text
watermark explícita -> provar existência, tipos, ausência de NULL e unicidade
sem watermark       -> PK elegível -> UNIQUE elegível
sem alternativa     -> metadados aproximados para pré-admissão da carga direta
```

Uma marca explícita inválida nunca é substituída silenciosamente. O motor não
descobre combinações de colunas examinando dados. Marcas compostas usam
comparação lexicográfica tipada, preservando direção, precisão e collation.

A carga direta sem chave (`DIRECT_KEYLESS`) é um único bloco, sem cursor
inventado. Para evitar uma varredura prévia, o motor usa
`sys.partitions` (heap ou índice clusterizado) como estimativa aproximada e **não executa
`COUNT_BIG` na origem** para autorizar esse modo. A quantidade real copiada pelo
BCP é o limite definitivo: se exceder `keyless_direct_load_max_rows`, o
manifesto não é publicado e nada é importado.

Esse modo é uma exceção controlada, não uma estratégia incremental. A consulta
BCP usa `TABLOCK,HOLDLOCK` para estabilizar a leitura sem chave; portanto pode
bloquear escritores durante toda a exportação. Em tabelas com PK, UNIQUE ou
marca d'água comprovada, o motor usa blocos e cursor por chave e não aplica
esse lock exclusivo do modo direto.

## CDC por tabela

`enable_cdc` tem default `false`.

- nenhuma tabela com `true`: zero chamadas CDC no banco;
- ao menos uma com `true`: o CDC do banco é verificado/habilitado uma única vez;
- se o job de cleanup já existir, sua retenção é verificada/ajustada nesse
  preflight e o resultado fica em cache;
- em um banco recém-habilitado, o SQL Server pode criar o job de cleanup apenas
  depois de habilitar a primeira tabela. Nesse caso, a ausência inicial é
  registrada como retenção pendente, não como falha;
- `cdc_retention_minutes` tem default `262800`, correspondente ao prazo de
  negócio de seis meses (aproximadamente 182,5 dias); no JSON e no SQL gerado
  o número não usa ponto como separador de milhar. O intervalo aceito é de
  `1` a `52494800` minutos, limite máximo do SQL Server;
- cada tabela sinalizada é verificada/habilitada imediatamente antes do BCP;
- quando a retenção está pendente, o motor habilita/confirma a primeira tabela
  CDC, ajusta e confirma o cleanup e somente então permite qualquer BCP. As
  tabelas seguintes reutilizam o resultado em cache;
- quando o valor precisa mudar, o motor executa `sys.sp_cdc_change_job` e
  reinicia **somente** o job `cleanup`, com `sys.sp_cdc_stop_job` e
  `sys.sp_cdc_start_job`, para vigência imediata. O job `capture` não é
  reiniciado. O BCP só é liberado depois da reinicialização e da confirmação
  do valor no catálogo. Corridas transitórias do SQL Server Agent são tentadas
  novamente por até 30 segundos; erros de permissão falham imediatamente;
- o relatório JSON persiste a evidência em `cdc_database`, incluindo estágio,
  minutos confirmados, `retention_changed` e `retention_restarted`; CLI e GUI
  também registram `cdc_database`, `cdc_retention` e `cdc_table`;
- falha de CDC pula essa tabela, grava código/mensagem e sempre segue para a
  próxima tabela, independentemente da política geral de continuação.

Depois da primeira tabela CDC habilitada, a ausência do job ou a impossibilidade
de consultar, ajustar, reiniciar o cleanup e confirmar a retenção é falha. A
tabela corrente é pulada antes do BCP e o resultado de falha fica em cache,
bloqueando as demais tabelas com `enable_cdc=true`. Tabelas com
`enable_cdc=false` continuam sendo processadas. Quando nenhuma tabela solicita
CDC, o motor não consulta nem altera o CDC ou sua retenção.

O motor mantém a essência de `scripts/05_ativa_cdc_banco_dados.sql` e
`scripts/06.1_ativa_cdc_tabelas.sql`, mas usa chamadas parametrizadas e confirma
o estado no catálogo. A autoridade necessária continua sendo responsabilidade
do ambiente.

## Evolução aditiva de esquema

Quando uma tabela de destino já existe, o catálogo é comparado com as colunas
de negócio atuais da origem.

- `allow_schema_evolution=false`: nada é alterado; a tabela recebe
  `SCHEMA_EVOLUTION_PENDING` e o evento contém as colunas ausentes;
- `allow_schema_evolution=true`: somente `ALTER TABLE ADD` seguro é aplicado;
- tipos, nulabilidade e objetos existentes incompatíveis continuam bloqueando;
- nenhuma coluna é removida, renomeada ou alterada;
- coluna `NOT NULL` nova em tabela povoada/contagem desconhecida é recusada sem
  backfill explícito.

A regra vale igualmente para os endpoints Bronze e Landing configurados
(`DBRO684` e `DLAN684` somente no laboratório).

## Particionamento opcional

Quando `tables[].partition_column` é informado, a coluna deve existir no layout
de destino e ser `DATETIME2(7)`. O perfil cria, em Bronze e Landing:

- `pf_<coluna>_mensal` e `ps_<coluna>_mensal` quando a coluna é `dh_carga`;
- sufixo `_attr` para qualquer outra coluna;
- limites mensais `RANGE RIGHT`, do mês corrente até dezembro do ano corrente
  mais seis anos, todos no filegroup `PRIMARY`;
- PK técnica `NONCLUSTERED` em `id_<tabela>`;
- índice `CLUSTERED` na coluna de particionamento, alinhado ao partition scheme;
- remoção do contrato de índice simples nonclustered redundante nessa coluna.

Ao adicionar uma tabela pela GUI, o particionamento vem habilitado com
`partition_column=dh_carga`; o operador pode desabilitá-lo ou escolher outra
coluna. No contrato JSON, o campo continua opcional: sem `partition_column`,
nenhum objeto de particionamento é criado e o layout tradicional do perfil é
preservado. Uma tabela existente não é reparticionada
silenciosamente: divergência entre layout existente e contrato solicitado exige
migração explícita.

## Contrato físico dos destinos

Nos destinos, schemas, tabelas, colunas, constraints, índices e sequences são
materializados em minúsculas. O nome de cada banco é preservado como
configurado; no laboratório, `DBRO684` e `DLAN684` permanecem em maiúsculas.

### Bronze

O perfil `templates/bronze.json` cria:

- `id_{destination_table}` `BIGINT`, gerado por sequence, sem `IDENTITY`;
- todas as colunas de negócio da origem em minúsculas;
- `bi_lsn_evento`, `bi_sequencia_evento`, `cd_operacao`, `de_operacao`,
  `dh_carga` e `dh_atualizacao`;
- `bi_lsn_evento` e `bi_sequencia_evento` recebem
  `CONVERT(BINARY(10), 0)` no próprio `INSERT`; não existe `UPDATE` posterior;
- `dh_carga` recebe, no próprio `INSERT`, o horário civil de Brasília
  (`E. South America Standard Time`) calculado a partir de UTC e convertido para
  `DATETIME2(7)`, independentemente do fuso configurado no SQL Server;
- PK clustered no ID técnico sem particionamento, ou nonclustered quando
  `partition_column` estiver configurada;
- cinco índices definidos no perfil no layout sem particionamento; com
  `partition_column=dh_carga`, o índice simples dessa coluna é substituído pelo
  índice clustered de particionamento.

Algumas grades exibem dez bytes `0x00` como uma célula visualmente vazia. Para
comprovar o valor físico, consulte em hexadecimal; o resultado esperado para os
dois atributos é `0x00000000000000000000`, com `DATALENGTH(...) = 10`:

```sql
SELECT TOP (10)
    CASE WHEN bi_lsn_evento IS NULL THEN 1 ELSE 0 END AS bi_lsn_evento_nulo,
    CONVERT(varchar(22), bi_lsn_evento, 1) AS bi_lsn_evento_hex,
    DATALENGTH(bi_lsn_evento) AS bi_lsn_evento_bytes,
    CASE WHEN bi_sequencia_evento IS NULL THEN 1 ELSE 0 END AS bi_sequencia_evento_nulo,
    CONVERT(varchar(22), bi_sequencia_evento, 1) AS bi_sequencia_evento_hex,
    DATALENGTH(bi_sequencia_evento) AS bi_sequencia_evento_bytes
FROM DBRO684.<schema_name>.<table_name>;
```

Para cada linha, os indicadores `*_nulo` devem retornar `0`. Se o valor fosse
SQL `NULL` de fato, o indicador retornaria `1` e as colunas hexadecimal e de
tamanho retornariam `NULL`; uma célula visualmente vazia, portanto, não é usada
como prova. O verificador da homologação repete essa validação sobre todas as
linhas e também confirma no catálogo `BINARY(10) NOT NULL`.

### Landing

O perfil `templates/landing.json` é usado exclusivamente por `ddl --area
landing|both` e cria:

- `id_{source_table}` `BIGINT IDENTITY(1,1)`;
- todas as colunas de negócio da origem em minúsculas;
- colunas estruturais `aud_ccid`, `aud_cntrrn` e `aud_enttyp`, definidas pelo perfil;
- computed persistidas `bi_lsn_evento`, `bi_sequencia_evento` e
  `cd_operacao`;
- `dh_carga` com default no horário civil de Brasília, calculado a partir de
  UTC e convertido para `DATETIME2(7)`, e `dh_atualizacao`;
- PK nonclustered e os cinco índices definidos no perfil; com particionamento,
  o índice simples da coluna escolhida é removido quando redundante e a
  organização clustered passa para essa coluna.

Não existe carga ou encadeamento de dados pela Landing. `run`, `resume` e
`import` transportam dados exclusivamente da origem configurada para a Bronze;
a Landing permanece vazia e contém somente estruturas. Em um cenário genérico,
isso corresponde a `BD_ORIGEM` → `DBRO684`, com `DLAN684` estrutural. O comando `ddl
--area both` cria/evolui, de forma independente, ambos os destinos.

Essas regras de fuso pertencem somente aos perfis padrão
`templates/bronze.json` e `templates/landing.json`. Um perfil customizado
continua agnóstico e pode definir outra expressão de preenchimento/default para
`dh_carga`, assumindo integralmente esse contrato.

## Artefatos e controles

Cada bloco possui diretório isolado, arquivo nativo, formato XML, log redigido e
manifesto V2. Arquivos em escrita terminam em `.partial`; somente um resultado
com contagem e hash válidos é publicado.

No Windows, a DACL da raiz e de toda execução é protegida e contém apenas
executor, SYSTEM, Administradores, os SIDs específicos de
`artifact_reader_sids` em somente leitura e os escritores explicitamente
confiáveis de `artifact_writer_sids`. Grupos amplos, owners desconhecidos,
reparse points e hardlinks são recusados. O
manifesto, formato e arquivo BCP permanecem abertos com lease que impede
escrita/exclusão desde a verificação SHA-256 em streaming até o fim da transação
SQL. Em POSIX, diretórios/arquivos removem escrita de grupo/terceiros e o mesmo
intervalo usa descritores e lock compartilhado. No POSIX, a raiz deve usar um
GID compartilhado com o SQL quando necessário; o motor preserva `setgid`,
remove escrita de grupo/terceiros e confia somente no mesmo UID executor/root.

O SQLite local usa `controle_transferencia.sqlite3`, com as tabelas persistentes
`metadados`, `execucao`, `execucao_tabela`, `execucao_lote` e
`tentativa_lote` e `PRAGMA user_version=5`. Esse nome de arquivo local não é um
schema do SQL Server. O controle SQL do endpoint Bronze é persistente
exclusivamente em `DBRO684.dbo.execucao`, `DBRO684.dbo.execucao_tabela` e
`DBRO684.dbo.execucao_lote`; a versão física fica em
`DBRO684.dbo.versao_esquema`. O motor valida a
assinatura completa, incluindo colunas, defaults, PK/FK, índices e objetos
inesperados; estrutura parcial, adulterada ou incompatível falha fechada.
O schema SQL Server legado `controle_transferencia` não faz parte do contrato e
não deve coexistir com essas tabelas. Essa regra não renomeia nem remove o
arquivo SQLite local `controle_transferencia.sqlite3`.
O endpoint Landing recebe somente as tabelas do perfil Landing, não as tabelas
de controle da carga.

Na GUI, `executor_directory` é apresentado como **Diretório de exportação dos
arquivos** e `destination_sql_directory` como **Diretório de importação dos
arquivos**. Eles podem ter sintaxes distintas, mas devem representar os mesmos
bytes. O segundo recebe inicialmente o mesmo valor do primeiro e pode ser
ajustado quando o SQL Server enxerga o compartilhamento por outro caminho. O
controle SQLite deve ficar em disco local, nunca em share UNC. No código-fonte,
os defaults de controle, exportação e DDL são, respectivamente,
`Local\BulkFlow\.bcp-control`, `Local\BulkFlow\bcp-data` e
`Local\BulkFlow\ddl`, sempre relativos à raiz do projeto e criados quando
ausentes.

Após **Planejar**, a área de pré-requisitos da GUI compara o espaço livre
observável em cada um desses caminhos com a projeção BCP consolidada de todas
as tabelas. Ela mostra bruto, margem do fator de segurança, total protegido,
saldo para o total, saldo operacional e detalhamento por tabela. Ambos os
saldos descontam o espaço livre mínimo; o operacional usa o pico previsto, que
respeita a política de retenção dos arquivos. Caminho ou estimativa não observável aparece como
`indisponível`, nunca como zero. A leitura do espaço é praticamente
constante; o custo relevante é a amostra limitada de linhas por tabela. A
cardinalidade vem sempre de `sys.partitions.rows`, sem contagem integral
para essa projeção. Consulte
[Interface gráfica](docs/USO_INTERFACE_GRAFICA.pt-BR.md#1-pré-requisitos) e
[Pré-requisitos](docs/PRE_REQUISITOS.pt-BR.md#capacidade-dos-diretórios-e-projeção-bcp).

Antes de provisionar e carregar uma tabela na Bronze, quando a estimativa de
bytes está disponível, o motor a multiplica pelo fator de segurança e compara
separadamente com cada volume do banco retornado por `sys.dm_os_volume_stats`.
Dados e log não são tratados como espaço intercambiável: a capacidade exibida é
a menor disponibilidade entre os volumes distintos e todos precisam comportar
o requisito. Leituras repetidas do mesmo ponto de montagem usam o menor valor;
se qualquer volume não informar `available_bytes`, a medição inteira fica
indisponível.
Insuficiência comprovada gera alerta, marca a tabela como
`SKIPPED_DESTINATION_INSUFFICIENT_SPACE`, não inicia sua importação e segue para
a próxima tabela. Estimativa indisponível obedece a
`estimates.on_unavailable`; se apenas a consulta dos volumes da Bronze não
puder ser comprovada, o motor registra um aviso e prossegue, sem alegar
capacidade que não foi medida.

Na importação independente por `import --manifest`, a mesma barreira usa apenas
os bytes reais dos blocos ainda não confirmados no controle SQL, somados e
multiplicados pelo fator de segurança. Blocos já confirmados exigem zero byte
adicional e continuam para reconciliação/finalização idempotente. A verificação
ocorre antes de criar controle SQL, provisionar/evoluir a tabela ou executar
`OPENROWSET`. Em uma importação fresca recusada, somente o SQLite local é
alterado; se a retomada já possuía vínculo SQL exatamente compatível, esse
controle existente recebe o estado terminal, sem criar objetos ou carregar
linhas.

## Executáveis e instalador Windows

Para reconstruir os binários em uma estação de build:

```powershell
python -m pip install -r .\requirements-build.txt
.\packaging\build_executables.ps1
```

O build gera `release\BulkFlowGUI.exe` e `release\BulkFlowCLI.exe`. A pasta
`release/` contém os artefatos distribuíveis versionados; apenas a área
intermediária `build/` é ignorada pelo Git. O usuário final dos EXEs não precisa
instalar Python nem pacotes Python, mas ainda precisa do Driver ODBC cujo nome
consta na configuração e do `bcp` compatível na máquina executora. Consulte
[Pré-requisitos](docs/PRE_REQUISITOS.pt-BR.md) e
[uso da CLI](docs/USO_CLI_POWERSHELL_LINUX.pt-BR.md).

Para a distribuição Windows autocontida em um único pacote, gere e entregue
`release\Setup-BulkFlow.exe`. Esse instalador offline inclui os dois EXEs e
os instaladores oficiais fixados do Driver ODBC/BCP. Consulte
[Instalador offline para Windows](docs/INSTALADOR_OFFLINE.pt-BR.md) para instalação
interativa ou silenciosa, UAC, licenças, logs, segurança e homologação em VM
limpa.

## Laboratório local opcional homologado

Todos os arquivos do laboratório — Compose, configurações, fixtures, segredos,
artefatos e evidências — ficam isolados em `docker/`. Essa pasta é local,
reproduzível e ignorada pelo Git; não define a topologia exigida pelo produto.

## Testes

```powershell
python -m unittest discover -s tests -v
python -m compileall -q bcp_engine bcp_bronze.py bcp_gui.py
```

Os testes cobrem configuração, autenticação, PTY, loteamento, DDL, CDC,
evolução, controles, manifesto, importação, retomada, GUI e launchers. O roteiro
Docker acrescenta SQL Server/BCP reais; mocks não são usados como substitutos
dessas provas.

## Limites deliberados

O motor não altera recovery model, não cria índice na origem, não concede
permissões, não abre firewall, não configura delegação/share, não usa
`xp_cmdshell` e não migra objetos incompatíveis destrutivamente. Origem em
escrita é reportada como `LIVE_BEST_EFFORT`; teto e checkpoints não equivalem a
snapshot global nem a CDC exatamente uma vez.

Tipos/recursos sem regra de fidelidade são rejeitados, incluindo tabelas
memory-optimized, FileTable, temporal/RLS e colunas FILESTREAM,
generated/hidden, Always Encrypted, Dynamic Data Masking, XML tipado e tipos
CLR/alias.
