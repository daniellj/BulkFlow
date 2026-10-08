[English](FALHAS_E_RETOMADA.md) | **Português (Brasil)**

# Falhas, checkpoints e retomada

Este documento descreve o contrato operacional da implementação atual para
falhas e retomadas. Ele se aplica tanto à execução pela CLI quanto pela
interface gráfica e independe de Docker. A única rota de dados é da origem para
o endpoint Bronze; o endpoint Landing recebe somente DDL.

Atalhos: [exportação BCP](#falhas-durante-a-exportação-bcp) ·
[carga no destino](#falhas-durante-a-carga-no-destino) ·
[20 tabelas](#exemplo-com-20-tabelas-e-pane-na-tabela-11) ·
[procedimento de recuperação](#procedimento-operacional-de-recuperação) ·
[situações sem retomada automática](#situações-que-não-têm-retomada-automática).

## Resposta objetiva

A retomada é feita por **bloco lógico** e pelo mesmo UUID de execução:

- se o BCP parar no meio de um arquivo, o arquivo parcial não é aproveitado;
  a mesma faixa do bloco é exportada novamente;
- se a carga parar antes do commit SQL, todo o bloco é revertido e pode ser
  importado novamente;
- se o commit SQL ocorreu, mas a resposta se perdeu ou o processo caiu antes de
  atualizar o controle local, o registro exato no controle SQL comprova o
  commit e impede uma nova inserção;
- em uma execução com 20 tabelas que pare durante a 11ª, as tabelas anteriores
  são revalidadas e reconciliadas, sem repetir seus blocos confirmados; a 11ª
  volta ao último bloco durável e, depois, o fluxo segue para as demais.

Não existe retomada dentro de um `.bcp.partial`. Com `rows_per_block=1500`, o
retrabalho normal é o bloco corrente, mas 1.500 é um alvo, não um máximo:
grupos empatados na marca d'água podem produzir um bloco maior. Uma tabela sem
chave usa um único bloco e pode precisar ser exportada integralmente de novo.

## Garantias e limites

O motor garante, dentro da mesma execução, dataset e destino vinculados:

- avanço do checkpoint de exportação somente após manifesto final verificável;
- dados, registro do bloco e checkpoint de importação no mesmo commit SQL;
- não reaplicação de um bloco que o controle SQL já confirmou exatamente;
- detecção de gap, sobreposição, ordem incorreta, contagem divergente, hash
  divergente e mudança estrutural;
- preservação do teto final capturado no início da tabela durante a retomada.

Essas garantias não representam snapshot global nem captura exatamente uma vez
de uma origem mutável. O modo de consistência é `LIVE_BEST_EFFORT`: uma nova
tentativa de um bloco ainda não confirmado pode observar alterações feitas na
origem entre as tentativas. A ativação opcional de CDC não transforma esta carga
inicial em consumo das tabelas de mudança do CDC.

"Não reaplicar" significa não inserir outra vez o mesmo bloco já confirmado no
mesmo destino. Não significa detectar que o conteúdo da origem mudou mantendo a
mesma quantidade de linhas, especialmente no modo direto sem chave.

## O que precisa ser preservado

Para retomar uma execução, preserve todos estes elementos:

| Elemento | Função na retomada |
|---|---|
| UUID da execução | Identifica a execução existente; `resume` nunca cria outro UUID. |
| `local_control_directory/controle_transferencia.sqlite3` | Guarda execução, tabelas, blocos, tentativas e cursores locais. |
| Diretório da execução em `executor_directory` | Guarda manifesto, formato e dados ainda necessários. |
| Controle SQL da Bronze, quando houve importação | Comprova quais blocos foram efetivamente confirmados no destino. |
| Configuração estrutural compatível | Preserva origem lógica, marca d'água, loteamento, projeção e layout. |
| Dados já gravados na Bronze, quando houve importação | Devem permanecer coerentes com o controle SQL. |

Credenciais, `execute_import`, caminhos operacionais e o endereço da Bronze não
alteram, por si sós, o hash estrutural. Isso não significa que possam ser
trocados arbitrariamente durante `resume`. Mantenha os mesmos caminhos
canônicos do SQLite e dos artefatos: os caminhos de manifesto persistidos no
SQLite são absolutos e precisam permanecer contidos no `executor_directory` da
execução. `destination_sql_directory` só pode mudar se continuar representando
exatamente os mesmos arquivos para o SQL Server.

A instância lógica da origem faz parte do contrato estrutural. Uma alteração de
endereço da Bronze em `resume` só é segura quando é um alias/rota para o mesmo
banco físico, as mesmas tabelas e o mesmo controle SQL; ela não migra a
execução. Para outro destino vazio, use `import --manifest` com **todos** os
manifestos e arquivos de dados necessários. Se os dados já confirmados foram
apagados pela política de retenção, use uma nova execução contra destino vazio.

Uma mudança na configuração estrutural da execução bloqueia a retomada. Entre
os itens estruturais estão a origem lógica, tabelas, marca d'água,
`rows_per_block`, regras de loteamento, limite para carga sem chave, perfil de
estrutura, `partition_column` e mapeamento de metadados. A identidade física da
origem, inclusive instância/porta/banco, as colunas, a projeção e o layout
também são revalidados por tabela. O snapshot físico de uma tabela ainda sem
teto, blocos, cursores ou contagens pode ser redescoberto; depois de qualquer
progresso durável, uma divergência é bloqueada.

## Fontes de verdade

Há três níveis complementares de evidência:

1. **SQLite local** — controla o planejamento e os cursores de exportação e
   importação. O comando `status` consulta somente esse controle.
2. **Manifesto final** — um arquivo `block_*.manifest.json` completo, com
   identidade, limites, contagens e hashes válidos, comprova uma exportação
   publicada. Um `.partial` nunca comprova conclusão.
3. **Controle SQL da Bronze** —
   `DBRO684.dbo.execucao_lote` comprova o commit no destino. O
   registro é criado na mesma transação dos dados e do checkpoint SQL.

O arquivo [`query_control.sql`](../query_control.sql) consulta a fonte durável
do destino. Ajuste `@ExecutionId` no início do script para filtrar o UUID. A
existência de uma linha exata em `execucao_lote` significa que aquele bloco e
seu checkpoint foram confirmados no mesmo commit.

Ao terminar uma tabela importada, quando `execute_import=true`, o motor também
compara `COUNT_BIG` do destino físico com a soma de `imported_rows` dos blocos
confirmados. Essa é uma prova de cardinalidade, não um hash do conteúdo de cada
linha.

## Sequência normal de um bloco

```text
capturar/reutilizar teto final da tabela
  -> planejar número e limites do bloco no SQLite
  -> verificar espaço estimado no executor e nos volumes da Bronze
  -> criar/revalidar/evoluir a estrutura Bronze
  -> marcar bloco como EXPORTING e tentativa como RUNNING
  -> BCP queryout grava somente block_*.bcp.partial
  -> validar término, contagem e bytes; vincular os limites planejados
  -> fsync e rename para block_*.bcp
  -> calcular SHA-256 do dado e do format file
  -> publicar block_*.manifest.json de forma atômica
  -> marcar EXPORTED e avançar export_cursor no SQLite
  -> validar novamente os artefatos
  -> abrir transação SQL e adquirir applock do destino
  -> INSERT ... SELECT ... OPENROWSET(BULK...)
  -> exigir ROWCOUNT_BIG igual ao manifesto
  -> gravar execucao_lote e avançar import_cursor
  -> COMMIT
  -> marcar IMPORTED no SQLite
  -> opcionalmente remover apenas o arquivo .bcp confirmado
```

O motor reconcilia e importa todos os blocos já publicados antes de extrair o
próximo bloco. Portanto, uma falha de carga não provoca nova extração de um
arquivo final válido.

## Falhas durante a exportação BCP

| Ponto da falha | Estado durável possível | Comportamento na retomada |
|---|---|---|
| Antes de planejar o bloco | Nenhum bloco novo. O teto, se já capturado, permanece. | Planeja o próximo bloco a partir do último `export_cursor`. |
| Depois do planejamento, antes do BCP | Bloco `PLANNED`, sem manifesto final. | Usa os mesmos limites e inicia nova tentativa. |
| Durante o BCP, inclusive timeout, pane, disco cheio ou limite de arquivo | Pode existir `.bcp.partial`; a tentativa pode estar `FAILED` ou ainda `RUNNING` após encerramento abrupto. O cursor não avança. | A tentativa anterior aberta vira `INTERRUPTED`; o parcial e o format file da tentativa são descartados e o bloco inteiro é exportado novamente. |
| BCP terminou, mas a publicação não chegou ao manifesto final | Pode existir parcial ou `.bcp` final sem manifesto. Nenhum deles comprova conclusão sozinho. | Refaz o mesmo bloco. A nova tentativa substitui o dado órfão durante a publicação; o checkpoint só avança depois do manifesto final verificado. |
| Manifesto final válido foi publicado, mas o SQLite não marcou `EXPORTED` | O manifesto contém identidade, limites, contagens e hashes; o cursor local ainda está anterior. | Valida o manifesto contra o bloco planejado, materializa o checkpoint local e não executa BCP novamente. |
| SQLite marcou `EXPORTED`, mas a importação ainda não ocorreu | Manifesto e arquivo final continuam disponíveis. | Importa esse bloco antes de planejar outra exportação. |
| Manifesto final, format file ou arquivo de dados foi adulterado/corrompido | Evidência inconsistente. | Falha fechada, sem avançar checkpoint e sem substituir silenciosamente um manifesto final existente. Restaure o conjunto exato que satisfaça os hashes. Sem backup e sem confirmação SQL, abandone a execução de forma controlada e use nova execução com destino vazio/estado coerente. |
| Arquivo pendente foi apagado antes do commit SQL | O SQL não confirma o bloco e não há bytes confiáveis para carregar. | Falha fechada. Restaure o arquivo exato a partir de backup. Sem backup, não há retomada automática: abandone a execução de forma controlada e use nova execução com destino vazio/estado coerente. |

Uma queda pode deixar `block_*.manifest.json.partial`. Esse arquivo não é
considerado publicação e pode ser substituído pela próxima tentativa. Já um
manifesto **final** inválido não é apagado ou sobrescrito automaticamente.

## Falhas durante a carga no destino

A carga não usa `bcp in`. Ela usa `INSERT ... SELECT ... OPENROWSET(BULK...)`
sob `SET XACT_ABORT ON`, em uma transação por bloco e com `sp_getapplock` para o
destino físico.

| Ponto da falha | Resultado no destino | Comportamento na retomada |
|---|---|---|
| Espaço insuficiente comprovado nos volumes da Bronze, antes do provisionamento/carga | Nenhuma linha dessa tabela é inserida. A tabela recebe `SKIPPED_DESTINATION_INSUFFICIENT_SPACE`. | O motor registra o alerta e segue para a próxima tabela, mesmo quando a política geral encerraria após erro de tabela. Libere/amplie o espaço e retome o mesmo UUID. |
| Antes de iniciar a transação | Nenhuma linha nova e nenhum controle SQL novo. | Revalida o manifesto e tenta o mesmo bloco. |
| Durante `OPENROWSET`, por erro SQL ou desconexão antes do commit | A transação é revertida; dados, `execucao_lote` e cursor SQL não ficam parcialmente confirmados. | Reimporta o mesmo bloco inteiro. |
| `ROWCOUNT_BIG` difere de `rows_exported` do manifesto | Rollback integral do bloco. | Mantém o arquivo e falha; a causa precisa ser corrigida antes da retomada. |
| Falha ao gravar o controle ou checkpoint SQL antes do commit | Rollback integral, inclusive das linhas inseridas. | Reimporta o mesmo bloco. |
| O SQL fez `COMMIT`, mas a resposta se perdeu | Dados, controle do bloco e cursor SQL estão confirmados juntos. | Consulta `execucao_lote` com identidade, limites, linhas, bytes, hashes e nomes exatos. Se houver correspondência, considera sucesso; não reenvia cegamente. |
| O commit SQL ocorreu, mas o processo caiu antes de marcar o SQLite | SQL confirma o bloco; no caminho normal, o SQLite ainda mostra `EXPORTED`. O estado legado/recuperável `IMPORTING` também é reconhecido. | Reconcilia o SQL, marca `IMPORTED` localmente e não insere novamente. |
| O processo caiu depois de marcar `IMPORTED`, antes de apagar o `.bcp` | O bloco está confirmado; o arquivo pode sobrar. | Não recarrega o bloco. A sobra é segura e pode permanecer; exclusão automática não é condição de consistência. |
| O `.bcp` já foi apagado pela política após confirmação | Manifesto e format file permanecem; SQL confirma exatamente o bloco. | A ausência do dado é aceita somente porque o controle SQL prova o commit. |
| Dados terminaram, mas a criação de índices falhou | Tabela fica `DATA_COMPLETE_INDEXES_PENDING`. | Após os preflights e revalidações normais, tenta os índices faltantes; não reexporta nem reinsere os dados. |

Quando a estimativa de bytes está disponível, a checagem da Bronze consulta os
volumes associados aos arquivos de dados e log por `sys.dm_os_volume_stats` e
compara cada volume separadamente com a estimativa multiplicada pelo fator de
segurança. Espaço de dados e de log não é somado: `available_bytes` representa
o menor valor entre os pontos de montagem distintos e todos precisam ser
suficientes. Pontos repetidos usam a menor observação; qualquer observação
`NULL` torna a medição indisponível. Estimativa indisponível obedece a
`estimates.on_unavailable`. Se
apenas a consulta dos volumes da Bronze não puder ser comprovada, o motor
registra aviso e prossegue; ele não declara capacidade inexistente nem confunde
ausência de evidência com insuficiência comprovada.

Esse bloqueio também protege `import --manifest`. Nesse comando, o requisito é
`CEILING(SUM(file_bytes dos blocos ainda não confirmados) × safety_factor)` e é
medido antes de criar o controle SQL, sondar o caminho de importação, aplicar
DDL ou executar `OPENROWSET`. Blocos já comprovados no SQL não voltam a consumir
o orçamento e um conjunto totalmente confirmado usa requisito zero, permitindo
reconciliação e finalização de índices. Se a capacidade for insuficiente, o
SQLite local recebe `SKIPPED_DESTINATION_INSUFFICIENT_SPACE` e a próxima tabela
continua sendo avaliada. Uma importação fresca não cria nem altera controle SQL;
em retomada com vínculo SQL exatamente compatível, o motor apenas terminaliza o
controle já existente para que ele não permaneça artificialmente `RUNNING`.

Antes de qualquer inserção, o motor exige ainda:

- o bloco imediatamente seguinte ao último confirmado;
- igualdade exata entre o limite inferior e o `import_cursor` SQL;
- mesmo teto, layout, projeção e destino;
- destino vazio no primeiro vínculo ou vínculo prévio compatível com a mesma
  execução.

Essas regras bloqueiam gap, sobreposição, append acidental e uso concorrente do
mesmo destino por outra execução.

## Exemplo com 20 tabelas e pane na tabela 11

Suponha que a execução pare no bloco 7 da tabela 11:

```text
tabelas 1 a 10  -> blocos confirmados no SQL
tabela 11       -> blocos 1 a 6 confirmados; bloco 7 parcial ou pendente
tabelas 12 a 20 -> ainda não iniciadas nesta chamada interrompida
```

Ao executar `resume` com o mesmo UUID, o motor percorre a configuração desde a
primeira tabela; ele não salta literalmente para a 11ª. O comportamento é:

1. tabelas 1 a 10: revalida contratos, reconcilia o controle, verifica a
   cardinalidade e os índices; não reexporta nem reinsere blocos confirmados;
2. tabela 11: importa primeiro qualquer manifesto final ainda pendente. Se o
   bloco 7 ficou apenas parcial, refaz toda a faixa do bloco 7 a partir do
   último checkpoint durável;
3. tabelas 12 a 20: seguem na ordem configurada.

O `resume` normal ainda abre a origem, valida BCP e conexões e prepara/revalida
as tabelas anteriores. Portanto, origem e BCP precisam estar disponíveis, mesmo
que os blocos 1 a 10 não sejam transferidos novamente. Somente `import
--manifest` dispensa acesso à origem. A matriz exata de ferramentas necessárias
por comando está em [Pré-requisitos do executor](PRE_REQUISITOS.pt-BR.md#dependências-por-operação).

Com `continue_after_table_error=false`, um erro de tabela encerra aquela
chamada após registrar o estado e as tabelas seguintes não são iniciadas. Com
`true`, erros de tabela são registrados e as seguintes podem ser processadas.
Assim, tabelas 12 a 20 somente podem já estar concluídas se a tabela 11 falhou
como erro tratável e o fluxo continuou; uma pane do processo durante o bloco 7
interrompe a chamada antes delas. A retomada reconcilia cada tabela conforme seu
estado. Falha ao ativar o CDC em uma tabela pula apenas a tabela afetada e
tenta a próxima.

O preflight CDC verifica/habilita o banco uma única vez por execução. Se o job
de cleanup já existir, também ajusta e confirma sua retenção para
`cdc_retention_minutes` (default `262800`, seis meses, aproximadamente 182,5
dias) e guarda o resultado em cache. Em um banco recém-habilitado, o SQL Server
pode criar o job apenas depois da primeira tabela CDC. A ausência inicial é
então um estado pendente: o motor habilita/confirma a primeira tabela, ajusta e
confirma a retenção e somente depois libera seu BCP. As demais tabelas
reutilizam o resultado em cache.

Quando a retenção diverge, o motor executa `sys.sp_cdc_change_job`, reinicia
somente o job `cleanup` com `sys.sp_cdc_stop_job` e `sys.sp_cdc_start_job` e
confirma novamente o valor. Isso torna a nova política imediatamente vigente
sem reiniciar o job `capture`. Nenhum BCP da tabela é iniciado antes da
reinicialização e da confirmação.

Se o job continuar ausente depois da ativação de uma tabela, ou se a retenção
não puder ser consultada, alterada, aplicada mediante reinicialização do
cleanup e confirmada, a tabela corrente é pulada antes do BCP e as demais
tabelas com `enable_cdc=true` ficam bloqueadas pelo resultado em cache. Tabelas
com `enable_cdc=false` continuam. Uma falha isolada ao ativar uma tabela pula
somente ela; enquanto a retenção estiver pendente, a próxima tabela CDC ainda
poderá criá-la e concluí-la. Sem tabela CDC marcada, o motor não consulta nem
altera CDC ou retenção. Após corrigir permissão, job ou configuração, use
`resume` com o mesmo UUID; o preflight será reavaliado.

O motor repete por até 30 segundos erros transitórios do SQL Server Agent
durante o `start` do cleanup; erro de permissão não é repetido. A prova final
fica no relatório JSON em `cdc_database`, com `retention_minutes`,
`retention_changed`, `retention_restarted`, estágio e eventual código de erro.

## Particularidades por estratégia

### PK, UNIQUE ou marca d'água comprovada

O cursor é composto pelos valores tipados da chave escolhida. A nova tentativa
repete a mesma faixa lógica não confirmada, e não uma posição física ou um
offset do arquivo. O teto final capturado é reutilizado, portanto linhas novas
acima dele não entram silenciosamente naquela execução.

`rows_per_block` é o alvo de planejamento. Para não dividir um grupo com os
mesmos valores de marca d'água, um bloco pode conter mais linhas que esse alvo.

### Carga direta sem chave

A tabela inteira é um único bloco, sem cursor de linha. A pré-admissão usa a
contagem aproximada de `sys.partitions` (heap ou índice clusterizado), sem
`COUNT_BIG` na Origem.
Depois do BCP, a quantidade real copiada é comparada com o limite global
`keyless_direct_load_max_rows`. Se exceder o limite, o manifesto não é
publicado, nenhuma linha é importada e a tabela recebe
`SKIPPED_KEYLESS_DIRECT_LOAD_OVER_LIMIT`.
Se a exportação falhar antes da publicação, a retomada repete a tabela inteira.

A consulta BCP desse modo usa `TABLOCK,HOLDLOCK` para estabilizar a leitura sem
uma chave reproduzível e pode bloquear escritores por toda a exportação. Sem
chave, não é possível provar alterações de conteúdo que preservem a contagem;
essa é uma limitação explícita. As próximas tabelas param ou continuam conforme
`continue_after_table_error`, exceto os skips especiais de CDC e espaço da
Bronze, que sempre seguem para a próxima.

### Exportação sem carga imediata

Com `execute_import=false`, `resume` continua a exportação do mesmo UUID e os
artefatos permanecem retidos. A importação posterior usa `import --manifest`
e não consulta a origem. O caminho pode ser um manifesto, um índice de
manifestos ou um diretório da execução.

Se um comando `import` separado falhar, execute novamente `import` com o mesmo
conjunto de manifestos. O controle SQL reconhecerá os blocos já confirmados e
carregará somente os pendentes. Isso é diferente de executar `resume` após uma
falha de `run`.

O comando `resume` abre e revalida a origem, mesmo quando já há artefatos
publicados. Quando a recuperação precisa ocorrer sem acesso à origem, use
`import --manifest` com os artefatos completos; essa operação não consulta a
origem.

## Procedimento operacional de recuperação

Nos comandos abaixo, substitua `config.v2.json`, UUID e diretórios de artefatos
pelos caminhos reais da execução. Um manifesto informado a `import` deve estar
sob o diretório daquele UUID em `executor_directory`.

### 1. Preserve o estado

Não trunque a Bronze, não apague `DBRO684.dbo.execucao`,
`DBRO684.dbo.execucao_tabela`, `DBRO684.dbo.execucao_lote` nem
`DBRO684.dbo.versao_esquema`, não remova o SQLite e não edite ou exclua
artefatos. Não
inicie outro `run` para tentar
continuar: isso cria um UUID novo.

Em um laboratório descartável, truncar alvos e controles inicia um teste novo;
não é uma retomada do teste anterior.

### 2. Identifique o UUID e consulte o estado local

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 status `
  --config .\config.v2.json `
  --execution-id 12345678-1234-5678-9234-567812345678
$engineExitCode = $LASTEXITCODE
```

Shell Linux:

```bash
./scripts/launchers/invoke-bcp.sh status \
  --config ./config.v2.json \
  --execution-id 12345678-1234-5678-9234-567812345678
engine_exit_code=$?
```

O `status` é local. Para verificar commits no destino, execute
[`query_control.sql`](../query_control.sql) no banco Bronze, preenchendo
`@ExecutionId`.

### 3. Corrija somente a causa externa

Exemplos: conectividade, espaço em disco, permissão do compartilhamento,
credencial, disponibilidade do SQL Server ou autorização para CDC. Preserve o
contrato estrutural e os controles.

### 4. Retome o mesmo UUID

Quando `execute_import=true`, a confirmação explícita continua obrigatória.

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 resume `
  --config .\config.v2.json `
  --execution-id 12345678-1234-5678-9234-567812345678 `
  --confirm-load
$engineExitCode = $LASTEXITCODE
```

Shell Linux:

```bash
./scripts/launchers/invoke-bcp.sh resume \
  --config ./config.v2.json \
  --execution-id 12345678-1234-5678-9234-567812345678 \
  --confirm-load
engine_exit_code=$?
```

Na interface gráfica, carregue a mesma configuração, informe o UUID no campo
de execução e use **Retomar**. A ação **Consultar status** lê o mesmo SQLite da
CLI.

### 5. Para importação posterior, reenvie os mesmos manifestos

PowerShell:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 import `
  --config .\config.v2.json `
  --manifest .\artefatos\12345678-1234-5678-9234-567812345678 `
  --confirm-load
```

Shell Linux:

```bash
./scripts/launchers/invoke-bcp.sh import \
  --config ./config.v2.json \
  --manifest ./artefatos/12345678-1234-5678-9234-567812345678 \
  --confirm-load
```

Todos os manifestos são pré-validados antes de abrir e alterar o destino.

### 6. Valide a conclusão

- código `0`: tudo que foi solicitado foi concluído;
- código `2`: há tabela pulada, erro parcial ou trabalho pendente;
- código `1`: houve falha global ou interrupção;
- `status`: confira estados, cursores e contagens locais;
- `query_control.sql`: confira os blocos e linhas confirmados na Bronze;
- ao concluir cada tabela importada (`execute_import=true`), o motor exige que
  a cardinalidade física do destino seja igual à soma dos blocos confirmados.

Em uma origem estabilizada, a validação de homologação também deve comparar a
quantidade da origem com a Bronze. Em uma origem em escrita, compare com o teto
e o recorte capturados pela execução, não necessariamente com o `COUNT_BIG`
atual da origem.

## Situações que não têm retomada automática

- **SQLite perdido ou substituído:** `resume` não encontra a execução. Restaure
  o controle local. Se os manifestos publicados sobreviveram, `import
  --manifest` pode recriar o estado necessário à importação e reconciliar o
  SQL sem consultar a origem, mas não reconstrói uma exportação incompleta.
- **Controle SQL perdido com destino já povoado:** o motor recusa vincular um
  destino não vazio sem controle compatível. Restaure dados e controle como um
  conjunto consistente ou use um novo destino vazio em uma nova execução.
- **Manifesto final corrompido ou incompatível:** o motor falha fechado. Não
  edite hashes ou limites para forçar a carga. Restaure o conjunto exato a
  partir de backup; sem ele e sem confirmação SQL, não há retomada automática.
- **Mudança estrutural:** uma mudança na configuração estrutural ou em tabela
  com teto, bloco ou checkpoint durável bloqueia `resume`. Uma tabela ainda sem
  qualquer progresso, em estado provisório ou de erro, pode ser redescoberta e
  revalidada conforme o contrato; isso nunca reinterpreta blocos existentes.
- **Alteração externa na tabela Bronze:** a verificação de cardinalidade pode
  detectar a divergência, mas o motor não corrige ou trunca dados externos.

## Ações proibidas durante uma recuperação

- executar `run` esperando que ele reconheça a execução anterior;
- reutilizar o UUID com uma configuração estrutural diferente;
- truncar tabelas da Bronze ou tabelas de controle;
- apagar `controle_transferencia.sqlite3`;
- apagar um `.bcp`, XML ou manifesto de bloco ainda não confirmado;
- editar manifesto, hash, limites ou contagens;
- reenviar SQL manualmente sem consultar primeiro `execucao_lote`;
- executar duas retomadas concorrentes do mesmo UUID.

O motor possui lease local e locks SQL para bloquear concorrência, mas esses
controles não tornam intervenções manuais destrutivas seguras.

## Evidência de homologação local

No laboratório SQL Server/BCP real, a execução
`5f249b78-bda3-445a-b825-db4d14c929cf` sofreu falha controlada por saída BCP
truncada no bloco 32 de `TABELA_ORIGEM_03`. Os blocos 1 a 31, totalizando 46.500 linhas,
já estavam confirmados; o bloco incompleto não avançou o checkpoint. A retomada
com o mesmo UUID concluiu 50.000 linhas, e uma segunda retomada após a conclusão
inseriu zero linhas adicionais.

Essa evidência comprova o caminho exercitado no laboratório. As garantias deste
documento decorrem também das transações, validações e testes automatizados do
motor; a infraestrutura Docker usada na prova não faz parte do produto.
