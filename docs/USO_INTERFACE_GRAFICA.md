# Interface gráfica do BulkFlow

A interface `ttkbootstrap` usa o mesmo contrato V2 e o mesmo motor da CLI. Não
existe uma segunda implementação da carga: as ações chamam os serviços de
planejamento, DDL, execução, retomada, importação e consulta de estado.

A aplicação é agnóstica de infraestrutura. Docker, portas fixas e os bancos
`BD_ORIGEM`, `DBRO684` e `DLAN684` pertencem somente ao laboratório local.

## Abrir a interface

Com Python 3.10+:

```powershell
python -m pip install -r .\requirements.txt
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
```

Com o executável Windows:

```powershell
.\release\BulkFlowGUI.exe
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
```

`BulkFlowGUI.exe` incorpora Python, Tk, `ttkbootstrap`, `pyodbc`, `pywinpty`,
o motor, os schemas e os perfis padrão. Ele não incorpora o Microsoft ODBC
Driver nem o utilitário Microsoft `bcp`; ambos precisam estar instalados e
visíveis para a mesma conta que inicia o EXE. Consulte
[Pré-requisitos do executor](PRE_REQUISITOS.md).

Sem `--config`, a janela abre um modelo agnóstico que deve ser preenchido. Os
principais defaults são:

- perímetro `DREADS`, com sugestão editável de usuário `u684`;
- 200.000 linhas por bloco;
- fator de segurança `1.25`;
- limite de 5.000.000 de linhas para carga direta sem chave;
- evolução aditiva de esquema desabilitada;
- retenção CDC de 262.800 minutos (seis meses, aproximadamente 182,5 dias);
- CDC desabilitado em cada tabela;
- criação de estrutura habilitada;
- arquivo máximo de 157.286.400 bytes (150 MiB);
- schema de controle `dbo` e índices secundários antes da carga;
- Driver `ODBC Driver 18 for SQL Server` e executável `bcp`;
- no código-fonte, controle em `Local\MotorDados\.bcp-control`, exportação e
  importação em `Local\MotorDados\bcp-data` e scripts DDL em
  `Local\MotorDados\ddl`, relativos à raiz do projeto.

O fundo destacado identifica valores sugeridos ou herdados que normalmente
podem ser aceitos sem ajuste; os campos continuam editáveis. Alguns campos
operacionais também abrem preenchidos, mas permanecem com fundo branco para
sinalizar que o operador deve revisá-los: retenção CDC, os dois diretórios e
instância, porta, banco e schema de cada conexão. Todos os parâmetros visíveis
possuem ajuda breve por tooltip no ícone `?`.

Entre os campos destacados estão os SIDs opcionais de leitores/escritores, o
DSN opcional de cada endpoint e, no formulário de tabela, lote, perfil,
particionamento e os valores herdados de banco/schema/destino. Cor de fundo não
altera a validação: `*` e o texto do campo determinam obrigatoriedade.

## Aba Geral

A aba reúne perímetro, modo de execução, diretórios, arquivos, limites,
timeouts e ferramentas. Campos marcados com `*` são obrigatórios.

O fluxo de dados é fixo: Origem → Bronze. Landing é exclusivamente estrutural.
O campo antigo de escopo não é exibido. **Criar estrutura se necessário** volta
a ser uma opção e vem marcada por padrão. Marcada, permite que `run`, `resume`
e a importação de manifestos criem ou completem automaticamente os objetos
Bronze ausentes; desmarcada, essas operações somente validam a estrutura
existente e acusam estrutura ausente/incompleta, sem criá-la. **Gerar DDL** é
sempre não mutável e o clique explícito em **Aplicar DDL** continua sendo uma
autorização separada para aplicar os scripts Bronze/Landing, qualquer que seja
o estado desse checkbox. O tooltip do campo explica esse efeito. **Apagar
arquivos exportados após confirmação** só remove
o arquivo de dados depois que o commit na Bronze foi comprovado.

### Perímetro e usuários sugeridos

O perímetro aceita exatamente `DREADS`, `HOMOLOGAÇÃO` ou `CAPGV`, sempre em
maiúsculas. Ele sugere, respectivamente, `u684`, `h684` ou `s684`. A sugestão
não vincula as conexões: cada endpoint pode usar outro usuário e outra senha.

### Planejamento e ferramentas

- **Driver ODBC:** nome registrado no sistema e usado pelo `pyodbc`, por
  exemplo `ODBC Driver 18 for SQL Server`.
- **Executável BCP:** `bcp` quando estiver no `PATH`, ou seu caminho absoluto.
- **Fator de segurança:** multiplica a estimativa de bytes; default `1.25`.
- **Retenção do CDC (minutos):** prazo do job de cleanup na Origem; default
  `262800`, equivalente ao prazo de negócio de seis meses (aproximadamente
  182,5 dias). Informe apenas dígitos, sem ponto separador de milhar; o intervalo
  aceito é de `1` a `52494800` minutos.
- **Esquema de controle:** valor obrigatório `dbo`. Na Bronze do laboratório,
  o contrato persistente usa `DBRO684.dbo.execucao`,
  `DBRO684.dbo.execucao_tabela`, `DBRO684.dbo.execucao_lote` e a tabela técnica
  `DBRO684.dbo.versao_esquema`.
- **Índices secundários:** default **Antes da carga**, persistido como
  `before_load`.

BCP 18+ é recomendado. Uma build 17 só é aceita quando comprova as opções TLS
`-Y` e `-u`.

### Arquivos e limites

- **Linhas por bloco:** default 200.000; pode ser sobrescrito por tabela.
- **Limite para tabelas sem chave:** default 5.000.000; zero desabilita
  `DIRECT_KEYLESS`.
- **Arquivo máximo em bytes:** default `157286400` (150 MiB).
- **Arquivo máximo em bytes** e **Espaço livre mínimo em bytes:** permanecem
  editáveis em bytes e têm uma caixa ao lado com a conversão para MB e GB.
- **Diretório de exportação dos arquivos:** caminho gravável usado pelo
  executor/BCP. O default no código-fonte é
  `<raiz do projeto>\Local\MotorDados\bcp-data`.
- **Diretório de importação dos arquivos:** visão dos mesmos bytes pelo SQL
  Server Bronze; começa com o mesmo valor do diretório de exportação, mas pode
  ser alterado para um caminho de compartilhamento ou mount equivalente.
- **Diretório de controle local:** guarda SQLite e checkpoints. O default no
  código-fonte é `<raiz do projeto>\Local\MotorDados\.bcp-control`.

Os diretórios padrão de controle, exportação e DDL são criados
automaticamente quando ausentes. Seus conteúdos estão excluídos do Git. Abrir
uma configuração existente preserva os caminhos explícitos nela; inclusive os
caminhos usados pelo laboratório Docker não são substituídos.

Uma tabela sem marca d'água, PK ou UNIQUE elegível usa metadados aproximados
para pré-admissão, sem `COUNT_BIG` na Origem. A quantidade real copiada pelo
BCP é validada contra o limite antes da publicação/importação.

O caminho visto pelo executor e o caminho visto pelo SQL Server Bronze podem
ter sintaxes diferentes, mas devem apontar para os mesmos bytes. O controle
SQLite deve permanecer em armazenamento local, não em UNC.

## Aba Conexões

Origem, Bronze e Landing possuem parâmetros independentes:

- instância;
- porta obrigatória, separada da instância;
- banco de dados;
- schema;
- DSN opcional;
- autenticação e usuário;
- TLS.

Essa separação atende naturalmente tanto a um perímetro no qual Landing e
Bronze compartilham uma instância quanto a outro em que os três endpoints ficam
em instâncias e portas diferentes.

Na Origem, **Banco de dados** é o banco lido; não há um segundo campo visual
**Banco para leitura**. Na Bronze, o schema começa em branco e deve ser
preenchido. O schema Landing herda o valor Bronze corrente quando apropriado,
mas permanece editável e obrigatório.

A opção TLS é exibida como **Confiar no certificado do servidor**.

### Autenticação e senhas

A interface suporta:

- Windows integrada;
- SQL Server;
- credencial Windows explícita.

O campo **Domínio** só é editável com credencial Windows. A tela não exibe uma
caixa técnica de referência do segredo. Quando uma operação precisa de senha,
ela é solicitada em diálogo mascarado e mantida apenas durante a sessão
necessária. A senha não é salva no JSON nem apresentada no acompanhamento.

O contrato JSON/CLI também suporta os provedores avançados `prompt`, `env` e
`windows_credential_manager`. Ao abrir configurações avançadas, valide o JSON
salvo antes de reutilizá-lo em automação.

## Aba Tabelas

Para adicionar ou editar uma tabela, informe:

- banco de dados de origem obrigatório, herdado da conexão Origem;
- schema de origem obrigatório, herdado da conexão Origem;
- tabela de origem;
- banco de dados de destino obrigatório, herdado da conexão Bronze;
- schema de destino obrigatório, herdado da conexão Bronze;
- tabela de destino, sugerida automaticamente como
  `<banco_origem>_<tabela_origem>` em minúsculas;
- marca d'água opcional, informada somente pelos nomes das colunas separados
  por vírgula, por exemplo `data_referencia, sequencial`; a direção do cursor
  é sempre ascendente;
- lote opcional por tabela, inicialmente igual ao valor global;
- flag **Ativar CDC nesta tabela**;
- perfil de estrutura opcional, inicialmente herdado do destino;
- particionamento opcional, habilitado por padrão com `dh_carga` ao adicionar
  uma tabela.

Os bancos e schemas herdados, a tabela de destino gerada, o lote por tabela, o
perfil e o particionamento aparecem com o fundo de valor default. Nesta versão,
editar os campos de banco não cria uma conexão separada por tabela:
**Banco de dado de origem** deve continuar igual a `source.database`, e
**Banco de dado de destino** deve continuar igual a
`bronze_destination.database`, sem diferenciar maiúsculas de minúsculas. Uma
divergência é rejeitada ao validar/salvar, evitando que a interface prometa uma
rota que o motor não executaria. Para outro par de bancos, salve e execute uma
configuração separada.

Marca d'água vazia significa: tentar PK elegível, depois UNIQUE elegível e, se
nenhuma existir, avaliar `DIRECT_KEYLESS`. Uma marca informada é somente
validada; o motor nunca descobre ou substitui a combinação por conta própria.
A ordem dos nomes em uma marca composta é significativa e é preservada no
contrato, sempre com `direction: "ASC"`.

### Baixar e executar a validação da marca d'água

No diálogo **Adicionar ou Editar tabela**, preencha **Banco de dados de
origem**, **Esquema de origem**, **Tabela de origem** e **Marca d'água
(opcional)**. A marca recebe somente um nome de coluna ou vários nomes
separados por vírgula, como `data_evento, sequencial`. Não informe `ASC` ou
`DESC`: o cursor é sempre ascendente e preserva a ordem digitada.

Clique em **Baixar script de validação…** e escolha onde salvar o arquivo
`.sql`. O arquivo já contém, como literais, o banco, o esquema, a tabela e as
colunas que estavam preenchidos no diálogo; ele não deixa macros para o
operador substituir e não tenta descobrir outra combinação. Se qualquer campo
obrigatório ou coluna estiver inválido, a GUI informa o erro antes de abrir a
janela de gravação.

Execute o arquivo na mesma instância SQL Server da Origem, preferencialmente
com a mesma credencial somente leitura que será usada pelo motor. O script é
reexecutável na mesma sessão e não cria ou altera objetos persistentes. Na
única linha de resultado, avalie principalmente:

- `marca_dagua_aceitavel`: `1` indica que a combinação atende ao contrato do
  motor; `0` indica rejeição ou resultado inconclusivo;
- `marca_dagua_distingue_registros`: informa se a combinação é única nos dados
  observados;
- `registros_com_nulo_na_marca`, `grupos_com_duplicidade` e
  `registros_duplicados_excedentes`: evidências que justificam a decisão;
- `decisao_sugerida`: orientação operacional consolidada.

Uma tabela sem PK/UNIQUE elegível só comprova a marca explícita quando não está
vazia, não contém NULL na combinação e não apresenta duplicidades. Quando há
PK/UNIQUE elegível, o motor também aceita uma marca comparável, ascendente e
sem NULL; empates são exportados como grupo completo.

O arquivo usa `COUNT_BIG` e `GROUP BY` para produzir a evidência e pode varrer
a tabela. Sob o isolamento padrão `READ COMMITTED`, essa leitura pode adquirir
S-locks e bloquear gravações concorrentes durante partes da varredura.
`LOCK_TIMEOUT` limita apenas quanto o script espera por locks de terceiros; ele
não limita a duração dos locks adquiridos pela própria leitura. Execute em uma
janela operacional adequada e, com a administração do banco, avalie índice
compatível ou isolamento por versionamento já habilitado. O script não usa
`NOLOCK`, pois leitura suja não comprova unicidade com segurança.

A grade de tabelas usa uma única linha de cabeçalho e nomenclatura consistente.
Ela pode ser reordenada, e essa é a ordem sequencial do processamento.

### CDC

Se nenhuma tabela estiver marcada, o motor não tenta ativar CDC no banco nem
consulta sua retenção. Se ao menos uma estiver marcada, o CDC do banco é
verificado/habilitado uma única vez. Quando o job de cleanup já existe, sua
retenção é ajustada e confirmada nesse preflight. Em um banco recém-habilitado,
o SQL Server pode criar o job apenas depois da primeira tabela CDC; nesse caso,
o motor habilita/confirma essa tabela, ajusta e confirma a retenção e só então
inicia qualquer BCP. O resultado fica em cache para as tabelas seguintes. A
retenção global é persistida no JSON como `cdc_retention_minutes`.

Se o valor configurado for diferente do atual, o motor aplica a mudança com
`sys.sp_cdc_change_job`, reinicia somente o job `cleanup` por
`sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job` para vigência imediata e confirma
o valor antes de liberar o BCP. O job `capture` não é reiniciado.
O acompanhamento exibe os eventos do banco, da retenção e da tabela, e o
relatório JSON persiste a evidência consolidada em `cdc_database`.

Falha no preflight do banco ou na confirmação da retenção bloqueia as tabelas
marcadas para CDC, com diagnóstico, sem impedir as tabelas sem a flag. A ausência
inicial do job é um estado pendente esperado, não uma falha. Uma falha isolada
ao habilitar uma tabela CDC pula essa tabela e segue para a próxima.

### Particionamento

Ao adicionar uma tabela, o particionamento vem habilitado e preenchido com
`dh_carga`. Ele só é criado quando o parâmetro opcional permanece habilitado e
preenchido; o usuário pode desabilitá-lo ou informar outra coluna. Ela
deve existir no destino e ter tipo `DATETIME2(7)`.

Bronze e Landing recebem função e scheme `RANGE RIGHT`, com limites mensais do
mês corrente até dezembro do ano corrente mais seis anos, todos em `PRIMARY`.
Para `dh_carga`, os nomes usam o descritor `mensal`; outras colunas usam
`attr`. O ID técnico permanece a PK `NONCLUSTERED`, e a coluna particionada
recebe o único índice `CLUSTERED`. Um índice simples nonclustered redundante
nessa coluna é retirado do contrato.

Sem o parâmetro, nenhum objeto de particionamento é criado. O motor não
reparticiona silenciosamente uma tabela existente incompatível; esse caso exige
migração explícita.

Nos perfis padrão Bronze e Landing, `dh_carga` registra o horário civil de
Brasília. A expressão parte de UTC, aplica explicitamente o fuso SQL Server
`E. South America Standard Time` e converte o resultado para `DATETIME2(7)`;
portanto, não depende do fuso do servidor. Perfis customizados podem substituir
essa expressão e continuam responsáveis pela semântica de data/hora escolhida.

## Aba Executar e acompanhar

A tela segue a ordem operacional abaixo.

### 1. Pré-requisitos

Verifique o Driver ODBC indicado em **Geral → Planejamento e ferramentas** e o
BCP. O resultado mostra se o driver foi localizado e a versão/capacidade do
utilitário. Essa verificação não instala componentes.

Depois de **Planejar**, esta mesma área também compara a projeção dos arquivos
BCP com o espaço livre observado nos dois caminhos configurados em **Geral →
Arquivos e limites**:

- **Diretório de exportação dos arquivos**, conforme enxergado pelo executor;
- **Diretório de importação dos arquivos**, conforme enxergado pelo executor,
  quando esse caminho também estiver acessível nessa máquina.

O quadro apresenta o total bruto consolidado de todas as tabelas configuradas, a margem
em bytes, o total protegido pelo **Fator de segurança**, o pico operacional
previsto, o espaço livre e o saldo de cada diretório. O detalhamento por tabela
permite localizar qual estimativa compõe o total. Uma tabela cuja
quantidade de linhas ou tamanho médio não pôde ser estimado fica identificada
como **indisponível**; nesse caso, o consolidado e o saldo também são
desconhecidos, em vez de assumir zero. O subtotal conhecido continua visível,
mas não é apresentado como total completo.

O total protegido é a soma das reservas calculadas por tabela; por isso cada
parcela recebe o fator e o arredondamento conservador antes da soma. A
capacidade operacional usa o pico previsto, e o saldo é
`livre - espaço_livre_mínimo - pico`. Quando os arquivos são retidos, o pico
acumula as reservas; quando são apagados somente após o commit confirmado, o
pico considera os blocos simultaneamente necessários. O total bruto/protegido
continua exposto para informar quanto será gerado ao longo de toda a execução,
mesmo quando esse total não permanece inteiro em disco ao mesmo tempo. A tela
mantém também a comparação direta `livre - espaço_livre_mínimo - total
protegido`, deixando explícito se todo o conjunto caberia simultaneamente.

Os dois caminhos normalmente representam os mesmos arquivos por nomes ou
mounts diferentes. Por isso, as capacidades são comparadas separadamente e o
motor não soma duas vezes a projeção. Quando o caminho de importação existir
somente no host do SQL Server, o executor não pode medir seu volume e exibe
**indisponível**. Isso não significa zero byte livre. A prova posterior de que
a Bronze enxerga os mesmos bytes e a verificação dos volumes de dados/log do
banco continuam sendo controles distintos.

#### Custo da projeção

A leitura de espaço livre é uma chamada de metadados do sistema operacional
por caminho e tem custo desprezível. A projeção de tamanho, porém, precisa das
credenciais da Origem e por isso é calculada no **Planejar**, não no simples
clique de verificação do Driver/BCP.

Com os defaults, a cardinalidade vem de
`sys.partitions.rows` (aproximada e proporcional ao número de partições)
e o tamanho médio é obtido por `TOP (10000)` com `DATALENGTH` nas colunas
exportadas. Assim, o trabalho de estimativa de tamanho é limitado a até 10.000
linhas por tabela, mais consultas pequenas de catálogo; não é uma exportação
nem uma varredura integral deliberada. O custo cresce aproximadamente com
`tabelas × linhas_amostradas × colunas_exportadas` e pode gerar leitura de
páginas se a amostra não estiver em cache.

A cardinalidade de planejamento é sempre obtida por metadados; o contrato não
faz varredura integral para essa finalidade. Isso não altera a validação de
uma marca d'água explícita sem PK/UNIQUE: para comprovar sua unicidade, o
script de validação usa agregação e pode percorrer a tabela. A amostra é de
conveniência e o formato/valores das linhas reais podem variar; o fator de
segurança reduz essa incerteza, mas não transforma a projeção em garantia. O
motor continua revalidando limites, linhas realmente copiadas e espaço durante
a execução.

### 2. Planejar

Clique em **Planejar** e informe, quando necessário, a senha mascarada de cada
ambiente. O planejamento é somente leitura. Além dos resultados por tabela, a
tela mostra usuário, instância, porta e banco efetivos de Origem, Landing e
Bronze.

### 3. DDL

**Gerar DDL** grava scripts para Bronze, Landing ou ambos. **Aplicar DDL** pede
confirmação e aplica os scripts idempotentes. Landing recebe apenas estrutura,
inclusive evolução e particionamento quando configurados; nunca recebe linhas.
O campo **Diretório dos scripts** começa em
`<raiz do projeto>\Local\MotorDados\ddl` ao executar pelo código-fonte.

### 4. Dados

- **Executar nova carga:** cria um UUID, exporta da Origem e importa somente na
  Bronze.
- **Retomar:** usa o mesmo UUID e os checkpoints duráveis.
- **Consultar status:** lê o controle SQLite local.
- **Importar manifestos:** importa artefatos publicados sem consultar a Origem.

As tarefas longas rodam fora da thread visual. A janela impede uma segunda
operação e não oferece cancelamento forçado. Em caso de pane, preserve o UUID,
o SQLite, os artefatos e as quatro tabelas de controle em `DBRO684.dbo`; reabra
a mesma configuração e use **Retomar**.

A retomada ocorre por bloco lógico, nunca pelo byte ou pela linha do arquivo
`.partial`. Um bloco com commit SQL comprovado não é inserido novamente. Se o
espaço livre nos volumes da Bronze for comprovadamente insuficiente, a tabela é
sinalizada e pulada antes da importação, e o fluxo segue para a próxima. A
capacidade é verificada por volume: espaços de dados e log não são somados, e o
menor volume é o limitante.
Consulte [Falhas, checkpoints e retomada](FALHAS_E_RETOMADA.md).

## Salvar na GUI e executar pela CLI

**Abrir** usa o mesmo JSON V2 da CLI. **Validar configuração** não conecta a
SQL. **Salvar** usa publicação atômica e só grava uma configuração validada.

O arquivo gerado pode ser reutilizado sem conversão:

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

Ao abrir um arquivo, perfis relativos são resolvidos contra o diretório dessa
configuração para que **Salvar como** não mude silenciosamente o perfil.

## Validação automatizada da interface

As regras de montagem do JSON ficam em `bcp_engine/gui_model.py` e possuem
testes sem display. O smoke test da janela roda quando o ambiente oferece uma
sessão gráfica.

```powershell
python -m unittest discover -s tests -p "test_gui_v2.py" -v
```
