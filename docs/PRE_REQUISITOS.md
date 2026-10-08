# Pré-requisitos do executor

## Resposta direta

Instale o driver ODBC e o utilitário `bcp` **na máquina, VM ou contêiner que
executará o motor**. Este documento chama esse ambiente de **executor**.

Python, Tk e os pacotes de `requirements.txt` também são necessários quando a
execução é feita pelos arquivos `.py` ou launchers. Os executáveis Windows
`BulkFlowGUI.exe` e `BulkFlowCLI.exe` já incorporam Python e os pacotes do
projeto, mas **não** incorporam o driver ODBC nem o BCP, que são componentes
nativos da Microsoft e permanecem obrigatórios no executor.

No Windows x64, `Setup-BulkFlow.exe` resolve esse provisionamento: o pacote
offline contém os dois EXEs e os instaladores oficiais do Microsoft ODBC Driver
18 e das Command Line Utilities/BCP. Ele não exige internet, Python ou `pip` na
estação final, mas exige elevação UAC para instalar os componentes nativos para
a máquina. Veja [Instalador offline para Windows](INSTALADOR_OFFLINE.md).

Não é necessário instalar essas ferramentas nas instâncias SQL Server de
Origem, Bronze ou Landing apenas para o motor funcionar. Esses servidores
precisam aceitar as conexões, possuir as permissões previstas e, no caso da
Bronze, conseguir ler os arquivos produzidos pelo executor.

Se o processo for executado por Agendador de Tarefas, serviço, pipeline ou
contêiner, a instalação, o `PATH`, os DSNs e as permissões devem funcionar para
a identidade efetiva desse processo. Uma validação feita somente na sessão
interativa de outro usuário não é suficiente.

## Componentes e versões suportadas

| Componente | Requisito | Observação |
|---|---|---|
| Python | 3.10 ou superior | Use o mesmo Python/ambiente virtual na instalação e na execução. |
| Tk | 8.6 ou superior | Necessário somente para a interface gráfica. |
| Pacotes Python | `python -m pip install -r requirements.txt` | Inclui `pyodbc`, `ttkbootstrap` e, no Windows, `pywinpty`. |
| Driver ODBC | Microsoft ODBC Driver 18 for SQL Server | O nome registrado deve coincidir exatamente com o campo da configuração. |
| BCP | 18+ recomendado; 17 somente se oferecer `-Y` e `-u` | O motor valida versão e capacidade antes de exportar. |

Para os EXEs, as linhas de Python, Tk e pacotes já estão atendidas pelo
empacotamento. Para os launchers PowerShell/Linux e para a execução direta dos
scripts, todas as linhas da tabela se aplicam.

Quando o setup offline é usado, ele também atende as linhas de Driver ODBC e
BCP na estação Windows. Os pré-requisitos continuam visíveis ao sistema e são
validados normalmente pelo comando `prerequisites`.

`pip` **não instala** o driver ODBC nativo nem o executável `bcp`. São dois
componentes externos e distintos. Instalar o driver não comprova que o BCP
está instalado, e vice-versa.

No Windows, `[Microsoft][ODBC Driver Manager]` visto em uma mensagem de erro é
o gerenciador fornecido pelo sistema, não o nome nem a versão a preencher na
GUI. O componente cliente esperado pelo campo **Driver ODBC** é
`ODBC Driver 18 for SQL Server`.

Para uma instalação nova, use uma versão suportada e atual do BCP 18 ou
superior, sem fixar a instalação em um patch antigo. Essa é a referência
oficial para os controles `-Y` e `-u`. Por compatibilidade, o motor aceita uma
build identificada como major 17 **somente** quando `bcp -?` comprovar que ela
também oferece ambos os controles; uma build 17 sem essa capacidade é
recusada. Essa verificação corresponde ao comportamento homologado e não deve
ser interpretada como suporte irrestrito a qualquer BCP 17.

A versão do cliente BCP é independente da versão da instância SQL Server: usar
SQL Server 2022, por exemplo, não instala automaticamente a ferramenta no
executor.

Referências oficiais da Microsoft:

- [baixar o Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server);
- [baixar e instalar o BCP](https://learn.microsoft.com/sql/tools/bcp/bcp-download-install);
- [opções e versões do utilitário BCP](https://learn.microsoft.com/sql/tools/bcp-utility);
- [instalar ODBC, BCP e sqlcmd no Linux](https://learn.microsoft.com/sql/linux/install-upgrade/setup-tools).

## Onde cada requisito fica

| Componente da topologia | Python/`pyodbc` | Driver ODBC | BCP | Requisito no servidor |
|---|---:|---:|---:|---|
| Executor da GUI, CLI ou job | Sim para scripts; incorporado nos EXEs | Sim, para operações SQL | Sim, para `run` e `resume` | Abre conexões e cria os artefatos. |
| SQL Server de Origem | Não | Não | Não | Acesso TDS, leitura, catálogo e permissões de CDC quando solicitado. |
| SQL Server Bronze | Não | Não | Não | DDL/DML/controle e leitura dos artefatos por `OPENROWSET`. |
| SQL Server Landing | Não | Não | Não | DDL e evolução estrutural; não recebe linhas. |
| Estação do operador | Apenas se também for o executor | Idem | Idem | SSMS, RDP ou SSH, isoladamente, não executam o motor. |

Se executor e SQL Server estiverem na mesma máquina, a instalação continua
sendo necessária por causa do papel de executor, não por causa do serviço SQL.

Cada conexão usa `instance` e `port` separados; a porta é obrigatória em novas
configurações e deve estar liberada entre o executor e aquele endpoint. Origem,
Bronze e Landing podem compartilhar uma instância ou residir em três instâncias
e portas diferentes, sem mudar o fluxo.

## Dependências por operação

| Operação | ODBC | BCP | Observação |
|---|---|---|---|
| Abrir a GUI, editar e salvar JSON | Não | Não | Com script, requer Python, Tk e `ttkbootstrap`; o EXE já os incorpora. |
| `prerequisites` | Não conecta a SQL | Inspeciona o binário | Verifica driver registrado, localização, versão e capacidades do BCP. |
| `status` e `migrate-control` | Não | Não | Trabalham com o controle SQLite local. |
| `plan` | Todos os endpoints configurados | Não | Inspeciona identidades, metadados, cardinalidade, contratos e destino aplicável. |
| `ddl` sem `--apply` | Origem | Não | Apenas gera os scripts. |
| `ddl --apply` | Origem e destinos selecionados | Não | Aplica DDL na Bronze, Landing ou ambas conforme `--area`. |
| `run --export-only` e `resume` de execução sem carga | Origem | Sim | Usa `execute_import=false` e não abre a Bronze. |
| `run` e `resume` com carga | Origem e Bronze | Sim | O BCP exporta da Origem; a Bronze importa por SQL bulk. |
| `import --manifest` | Bronze | Não | Não abre a Origem e não executa BCP. |

O `resume` faz a pré-validação do BCP mesmo quando todos os blocos já
exportados serão apenas reconciliados. Para importar manifestos já existentes
sem Origem e sem BCP, use `import --manifest`.

## Validação automática

Execute antes de `plan`:

```powershell
python .\bcp_bronze.py prerequisites --config .\config.v2.json
# ou, sem instalação do Python
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
```

```bash
./scripts/launchers/invoke-bcp.sh prerequisites --config ./config.v2.json
```

O comando retorna JSON. Código `0` significa que o driver configurado foi
encontrado e o BCP passou na validação; código `2` significa pré-requisito
ausente ou incompatível. Essa verificação não substitui o teste de conexão e
permissões feito por `plan`.

Na GUI, a área visual de pré-requisitos também recebe, após **Planejar**, a
avaliação de capacidade dos diretórios de exportação e importação. Essa
segunda informação não faz parte do comando CLI `prerequisites`, pois depende
da projeção de todas as tabelas e, portanto, das conexões e credenciais usadas
por `plan`.

## Campos da interface gráfica

Na aba **Geral**, seção **Planejamento e ferramentas**:

- **Driver ODBC** corresponde a `odbc_driver`. O valor padrão é
  `ODBC Driver 18 for SQL Server` e deve aparecer, com a mesma grafia, na lista
  retornada por `pyodbc.drivers()`. Não informe apenas `18`, o caminho de uma
  DLL, o instalador MSI ou o caminho do BCP.
- **Executável BCP** corresponde a `bcp_executable`. Use `bcp` quando o
  executável estiver no `PATH` da conta que roda o motor. Em jobs e serviços,
  prefira o caminho absoluto retornado pela verificação do sistema.

Esses campos não instalam nem baixam componentes. O driver global é usado nas
conexões ODBC que não tenham uma configuração específica. O executável BCP é
um processo separado e não é selecionado pelo campo do driver ODBC.

O contrato JSON aceita `odbc_driver` específico por endpoint. Revise o arquivo
salvo quando um ambiente usar overrides avançados que não estejam expostos na
interface.

Quando um endpoint usa `odbc_dsn`, esse DSN deve existir no executor e estar
visível à mesma conta e arquitetura do processo. Na conexão ODBC desse
endpoint, feita por `pyodbc`, o DSN substitui a seleção pelo nome global do
driver. O DSN não é usado pelo BCP: o processo BCP não recebe `-D` e sempre
usa `-S <instance>,<port>`. Portanto, `instance` e `port` devem identificar uma
rota de rede válida mesmo quando `odbc_dsn` estiver configurado.

## Instalação e validação no Windows

### Opção recomendada: setup offline

Execute `Setup-BulkFlow.exe`, aceite a elevação UAC e os termos de licença e,
ao final, rode a verificação de pré-requisitos. O setup instala a aplicação em
`C:\Program Files\BulkFlow` e mantém os dados mutáveis do usuário em
`%LOCALAPPDATA%\MotorDados` ou nos diretórios explicitamente configurados.

Para instalação interativa, silenciosa, reparo, atualização, desinstalação,
logs, assinatura e o roteiro obrigatório de VM limpa, consulte
[Instalador offline para Windows](INSTALADOR_OFFLINE.md).

### Instalação manual dos componentes

1. Instale o Microsoft ODBC Driver 18 usando o instalador oficial.
2. Instale a versão atual das Microsoft Command Line Utilities. Para uma nova
   instalação, prefira BCP 18 ou superior.
3. Instale as dependências Python no ambiente que executará o motor.

```powershell
python -m pip install -r .\requirements.txt

# O driver precisa aparecer com o nome usado na configuração.
Get-OdbcDriver | Where-Object Name -eq 'ODBC Driver 18 for SQL Server'
python -c "import pyodbc; print(pyodbc.drivers())"

# Localize exatamente o BCP selecionado pelo PATH e confira sua versão/opções.
Get-Command bcp
where.exe bcp
bcp -v
bcp -?

# Útil para diagnosticar driver ou DSN de arquitetura diferente.
python -c "import struct; print(struct.calcsize('P') * 8)"
```

O resultado de `bcp -v` deve indicar major 18 ou superior, preferencialmente.
Se indicar 17, a ajuda precisa conter `-Y` e `-u`; o motor fará a mesma
verificação e recusará o binário sem esses controles. Se `where.exe bcp`
retornar mais de uma cópia, configure o caminho absoluto da versão correta em
**Executável BCP**.

Execute essas verificações com o mesmo Python, ambiente virtual e identidade
que rodará a aplicação. Após instalar ferramentas ou alterar o `PATH`, reinicie
o terminal, serviço ou agente que executará o motor.

### Uso dos executáveis Windows avulsos

Distribua os dois arquivos produzidos em `release/` juntamente com a
configuração, quando aplicável:

```powershell
.\BulkFlowGUI.exe --config .\config.v2.json
.\BulkFlowCLI.exe prerequisites --config .\config.v2.json
.\BulkFlowCLI.exe plan --config .\config.v2.json
```

Não é necessário instalar Python, `pyodbc`, `ttkbootstrap` ou `pywinpty` na
estação usuária. Ainda é obrigatório instalar e tornar visíveis, para a mesma
arquitetura e identidade do processo, o Driver ODBC configurado e o BCP. O
empacotamento não altera `PATH`, DSNs, firewall, certificados ou permissões.

Essa obrigação dos EXEs avulsos não se aplica quando a aplicação é instalada
por `Setup-BulkFlow.exe`, pois o setup provisiona o Driver ODBC e o BCP. O
instalador também não altera DSNs, firewall, certificados ou permissões dos
bancos.

Para reconstruir os EXEs, a estação de build precisa de Python e das
dependências de `requirements-build.txt`:

```powershell
python -m pip install -r .\requirements-build.txt
.\packaging\build_executables.ps1
```

## Instalação e validação no Linux

Siga a receita oficial da Microsoft para a distribuição em uso. Em geral,
`msodbcsql18` fornece o driver e `mssql-tools18` fornece `bcp` e `sqlcmd`; os
nomes e comandos de instalação podem variar por distribuição.

```bash
python3 -m pip install -r ./requirements.txt

odbcinst -q -d
python3 -c 'import pyodbc; print(pyodbc.drivers())'

BCP_BIN="$(command -v bcp 2>/dev/null || true)"
[ -n "$BCP_BIN" ] || BCP_BIN=/opt/mssql-tools18/bin/bcp
"$BCP_BIN" -v
"$BCP_BIN" -?
```

Se `/opt/mssql-tools18/bin` não estiver no `PATH` do job, informe
`/opt/mssql-tools18/bin/bcp` em `bcp_executable`. Não presuma que o `PATH` de
uma sessão SSH seja igual ao de `systemd`, cron, pipeline ou contêiner.

Para a GUI em Linux, o Python também precisa ter Tk e acesso a uma sessão
gráfica. A CLI não exige display.

## Arquivos compartilhados com a Bronze

Os caminhos de artefatos têm pontos de vista diferentes:

- `executor_directory`: **Diretório de exportação dos arquivos** na GUI;
  caminho absoluto, gravável e visto pelo Python/BCP;
- `destination_sql_directory`: **Diretório de importação dos arquivos** na
  GUI; caminho absoluto, visto pelo SQL Server Bronze, para os **mesmos bytes**
  e a mesma estrutura relativa. Na interface, começa com o mesmo valor do
  diretório de exportação e deve ser ajustado quando o SQL enxerga esses bytes
  por outro caminho;
- `local_control_directory`: armazenamento local e durável do controle SQLite;
  não deve ser um caminho UNC.

Um diretório local do executor não se torna visível automaticamente a um SQL
Server remoto. Use compartilhamento SMB, volume ou bind mount e conceda leitura
à identidade efetiva do serviço SQL/BULK. Antes de carregar dados, o motor faz
uma prova de caminho: grava conteúdo aleatório pelo executor e exige que a
Bronze leia exatamente os mesmos bytes por `OPENROWSET`.

O endpoint Landing não lê arquivos de dados. `destination_sql_directory` é
necessário para a carga na Bronze e para `import --manifest`, mas não para uma
execução exclusivamente de exportação.

### Capacidade dos diretórios e projeção BCP

Depois do planejamento, a GUI soma `estimated_bcp_total_bytes` de todas as
tabelas configuradas e mostra tanto o total bruto quanto:

```text
reserva da tabela = teto(BCP bruto da tabela × fator de segurança)
total protegido = soma(reserva de cada tabela)
margem = total protegido - total bruto
saldo para o total = espaço livre - espaço livre mínimo - total protegido
saldo = espaço livre - espaço livre mínimo - pico operacional previsto
```

O detalhamento por tabela conserva o valor bruto e protegido que formam esse
consolidado. O pico operacional vem de `predicted_peak_bytes`: com retenção
dos arquivos ele acumula as reservas; com exclusão pós-commit ele considera os
blocos que podem coexistir. Assim, a tela distingue **quanto será gerado ao
longo do fluxo** de **quanto precisa caber simultaneamente no volume**. Se
qualquer tabela configurada tiver estimativa desconhecida, a GUI
preserva o estado **indisponível** no consolidado e no saldo; não substitui o
desconhecido por zero nem produz uma falsa aprovação. Um subtotal conhecido
pode ser exibido apenas como evidência parcial.

Cada diretório é consultado independentemente no sistema operacional do
executor. Se ambos os caminhos forem aliases do mesmo compartilhamento, eles
continuam representando uma única coleção de arquivos: a projeção é comparada
com cada visão, não somada duas vezes. Se o caminho de importação só existir
no namespace do host SQL Server, a medição local será **indisponível**. A
aplicação não confunde isso com falta comprovada de espaço.

Essa indicação é de planejamento, não uma reserva no filesystem. Outros
processos podem consumir espaço depois da observação, os dados podem ter
distribuição diferente da amostra e arquivos retidos de execuções anteriores
também ocupam o volume. O motor mantém as barreiras de execução e a validação
separada dos volumes de dados/log da Bronze.

### Custo computacional da projeção

- **Espaço livre:** duas consultas de metadados ao sistema operacional, `O(1)`
  por caminho na perspectiva da aplicação; normalmente desprezíveis.
- **Quantidade de linhas (default `metadata`):** leitura de
  `sys.partitions.rows`, aproximadamente `O(número de partições)`, sem
  contar cada linha.
- **Tamanho médio:** `TOP (maximum_sample_rows)` e `DATALENGTH` sobre as
  colunas exportadas; default de 10.000 linhas. O custo aproximado é
  `O(tabelas × linhas_amostradas × colunas_exportadas)` e envolve I/O quando
  as páginas não estão no cache.
- **Contagem integral:** não é executada para estimar a cardinalidade dos
  arquivos; o contrato aceita somente `metadata` em
  `estimates.row_count_method`. A prova de unicidade de uma marca explícita sem
  PK/UNIQUE é uma validação diferente e pode varrer/agrupar a tabela.
- **Agregação e comparação na aplicação:** `O(número de tabelas)`, irrelevante
  diante das leituras SQL.

A amostra é de conveniência (`TOP`, sem ordenação aleatória) e não oferece
garantia estatística. O fator de segurança, default `1.25`, acrescenta 25% à
projeção, mas não é garantia física nem substitui a revalidação durante o BCP.

## Permissões mínimas por função

As concessões exatas devem seguir a política do ambiente e o princípio do menor
privilégio:

- **Origem:** leitura de dados e metadados. Se qualquer tabela usar
  `enable_cdc=true`, a credencial também precisa de autoridade para habilitar
  CDC no banco e na tabela, consultar o job de cleanup em
  `msdb.dbo.cdc_jobs` e executar `sys.sp_cdc_change_job` no banco de Origem
  quando a retenção divergir de `cdc_retention_minutes`. Nesse caso, também
  precisa executar `sys.sp_cdc_stop_job` e `sys.sp_cdc_start_job` para reiniciar
  somente o cleanup e tornar a mudança imediatamente vigente. Sem essa
  autoridade, o erro é registrado e as tabelas que solicitaram CDC são puladas;
  tabelas com `enable_cdc=false` continuam. Essas permissões precisam estar
  disponíveis também durante a ativação da primeira tabela: em um banco
  recém-habilitado, o SQL Server pode criar o job de cleanup somente nesse
  momento, e o motor reinicia o cleanup quando necessário e confirma a retenção
  antes de iniciar qualquer BCP. O job `capture` não é reiniciado.
- **Bronze:** criação/evolução de schemas, tabelas, sequences, constraints e
  índices; `INSERT`; criação ou validação e manutenção das tabelas de controle
  `dbo.versao_esquema`, `dbo.execucao`, `dbo.execucao_tabela` e
  `dbo.execucao_lote` no banco Bronze (DBRO684 no laboratório); leitura
  dos arquivos por `OPENROWSET(BULK...)`; e consulta ao espaço dos volumes. Em
  SQL Server 2022+, `sys.dm_os_volume_stats` normalmente exige
  `VIEW SERVER PERFORMANCE STATE`. Particionamento requer a permissão aplicável
  para dataspace, normalmente `ALTER ANY DATASPACE`, além das permissões DDL do
  banco.
- **Landing:** permissões DDL e de evolução estrutural. Não precisa ler os
  arquivos BCP nem receber DML de carga.

O motor não concede permissões, abre firewall, cria share ou configura uma
conta de serviço. Falta de evidência para consultar o espaço da Bronze é
registrada como aviso; insuficiência de espaço comprovada faz a tabela ser
pulada antes da importação.

## Diagnóstico rápido

### `[Microsoft][ODBC Driver Manager] ...`

`ODBC Driver Manager` é o componente que carregou ou tentou localizar o
driver; essa mensagem não identifica, por si só, a versão instalada.

1. Copie exatamente o valor de **Driver ODBC** da GUI.
2. Compare-o com `python -c "import pyodbc; print(pyodbc.drivers())"` executado
   pelo mesmo Python e pela mesma conta do motor.
3. Se houver DSN, confirme nome, escopo de usuário/sistema e arquitetura.
4. Não tente corrigir esse erro alterando o caminho do BCP: são componentes
   independentes.

O erro `IM002` normalmente indica nome de driver/DSN ausente ou invisível para
o processo atual.

### `bcp` não encontrado ou versão rejeitada

Confira `where.exe bcp` no Windows ou `command -v bcp` no Linux. Em serviço ou
job, configure um caminho absoluto. Valide com `bcp -v` e `bcp -?`; o motor
recusa uma instalação sem os controles TLS exigidos.

### A Bronze não consegue ler o arquivo

Esse problema não é corrigido reinstalando ODBC ou BCP. Verifique o
compartilhamento/mount, a correspondência entre os dois caminhos e as
permissões da identidade efetiva do SQL Server.

`sqlcmd` pode ser útil para administração e laboratório, mas não é dependência
de execução do motor.
