[English](USO_CLI_POWERSHELL_LINUX.md) | **Português (Brasil)**

# Uso da CLI por PowerShell e shell Linux

Os launchers desta página chamam a mesma CLI Python usada pelo motor. Eles podem
ser executados a partir de qualquer diretório, encaminham cada argumento sem
remontar a linha de comando e devolvem o código de saída do motor.

No Windows, `BulkFlowCLI.exe` expõe exatamente os mesmos subcomandos e já
incorpora Python e os pacotes do projeto. Ele não incorpora o Driver ODBC nem o
BCP, que continuam instalados no executor.

O contrato atual possui uma Origem com função `data_provider` e no máximo um
destino capaz de receber dados. Exatamente um é obrigatório ao importar; uma
configuração somente de exportação pode não ter nenhum. As funções dos destinos são
`structure_and_data`, `structure_only` e `data_only`; os defaults são
`structure_and_data` em `BD_DESTINO_01` e `structure_only` em
`BD_DESTINO_02`. Docker e portas fixas do laboratório não são requisitos da
CLI.

O parâmetro global `perimeter` aceita exatamente `DESENVOLVIMENTO`, `HOMOLOGAÇÃO` ou
`PRODUÇÃO`; seu default é `DESENVOLVIMENTO`. Esses valores sugerem,
respectivamente, `u684`, `h684` e `s684` como usuário SQL, mas `source`,
`bronze_destination` e `landing_destination` mantêm função, instância, porta,
banco, schema, usuário e segredo independentes e editáveis. Essas chaves de
destino e as áreas CLI `bronze`/`landing` são IDs internos compatíveis; as
funções determinam o comportamento operacional.

O parâmetro global `cdc_retention_minutes` define a retenção do job de cleanup
CDC na Origem. Seu default é `262800` minutos (seis meses, aproximadamente
182,5 dias). Use um inteiro de `1` a `52494800`, sem ponto separador de milhar,
por exemplo:

```json
{
  "cdc_retention_minutes": 262800,
  "tables": [
    {"source_table": "CLIENTE", "enable_cdc": true}
  ]
}
```

O driver ODBC e o BCP são instalados no host que realmente executa a CLI, não
nas instâncias SQL Server. Python e os pacotes também são instalados quando os
scripts/launchers forem usados; `BulkFlowCLI.exe` já os incorpora. Antes dos
exemplos abaixo, siga o guia de [Pré-requisitos do executor](PRE_REQUISITOS.pt-BR.md),
que também relaciona as dependências de cada comando.

Uma configuração salva pela GUI é o mesmo JSON consumido pela CLI. Não existe
etapa de conversão ou exportação adicional.

Os campos opcionais `tables[].source_database` e
`tables[].destination_database` tornam o mapeamento salvo pela GUI explícito,
mas não abrem conexões independentes por tabela nesta versão. Quando presentes,
devem coincidir com `source.database` e com o endpoint selecionado por
`active_destination`. O motor
rejeita a configuração se houver divergência. Para processar outro banco, use
outro arquivo de configuração e outra execução.

## PowerShell

Na raiz do projeto:

```powershell
& .\scripts\launchers\invoke-bcp.ps1 prerequisites --config .\examples\config.full.json
& .\scripts\launchers\invoke-bcp.ps1 plan --config .\examples\config.full.json
& .\scripts\launchers\invoke-bcp.ps1 ddl --config .\examples\config.full.json --area both --output .\ddl --apply --confirm
& .\scripts\launchers\invoke-bcp.ps1 run --config .\examples\config.full.json --confirm-load
$engineExitCode = $LASTEXITCODE
```

Com os binários distribuídos em `release/`:

```powershell
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
.\release\BulkFlowCLI.exe plan --config .\config.v2.json
.\release\BulkFlowCLI.exe ddl --config .\config.v2.json --area both --output .\ddl --apply --confirm
.\release\BulkFlowCLI.exe run --config .\config.v2.json --confirm-load
```

Quando `ddl --output` é omitido, o diretório de DDL assume
`<raiz do projeto>\Local\BulkFlow\ddl` tanto pelo código-fonte quanto por um
EXE portátil dentro da pasta `release` deste projeto. Um EXE instalado ou
isolado usa `%LOCALAPPDATA%\BulkFlow\ddl`. Informar `--output` explicitamente
continua sobrescrevendo esse padrão.

O EXE pode ser copiado para outra pasta. Perfis `bronze` e `landing` e o schema
V2 estão incorporados; arquivos de configuração, perfis customizados e
diretórios operacionais continuam externos e devem usar caminhos válidos no
executor.

Para abrir a interface gráfica no Windows, use o mesmo ambiente Python ou o
EXE:

```powershell
python .\bcp_gui.py
python .\bcp_gui.py --config .\caminho\config.v2.json
.\release\BulkFlowGUI.exe --config .\caminho\config.v2.json
```

Quando o JSON usar o provedor `env`, capture cada senha sem eco antes dos
comandos. O exemplo abaixo mantém referências separadas, mesmo quando o valor do
laboratório é igual:

```powershell
$sourceSecure = Read-Host 'Senha SQL da origem' -AsSecureString
$bronzeSecure = Read-Host 'Senha SQL da BD_DESTINO_01' -AsSecureString
$landingSecure = Read-Host 'Senha SQL da BD_DESTINO_02' -AsSecureString
$env:BCP_SOURCE_SQL_PASSWORD = [Net.NetworkCredential]::new('', $sourceSecure).Password
$env:BCP_BRONZE_SQL_PASSWORD = [Net.NetworkCredential]::new('', $bronzeSecure).Password
$env:BCP_LANDING_SQL_PASSWORD = [Net.NetworkCredential]::new('', $landingSecure).Password

# execute plan, ddl, run, resume, import ou status aqui

Remove-Item Env:\BCP_SOURCE_SQL_PASSWORD, Env:\BCP_BRONZE_SQL_PASSWORD, Env:\BCP_LANDING_SQL_PASSWORD
Remove-Variable sourceSecure, bronzeSecure, landingSecure
```

Também é possível chamar o arquivo por caminho absoluto ou com `powershell
-File`/`pwsh -File`. Se o Python 3.10+ não estiver no `PATH`, indique somente o
executável, sem opções adicionais:

```powershell
$env:BCP_PYTHON = "$PWD\.venv\Scripts\python.exe"
& .\scripts\launchers\invoke-bcp.ps1 --verbose status `
    --config .\config.example.json `
    --execution-id 12345678-1234-5678-9234-567812345678
```

## Shell Linux

Pré-requisitos do executor Linux:

- Python 3.10+ e as dependências de `requirements.txt`;
- Microsoft ODBC Driver 18 (`msodbcsql18`) e, preferencialmente, BCP 18 ou
  superior; uma build BCP 17 somente é aceita se oferecer `-Y` e `-u`. Nas
  distribuições suportadas, o BCP é fornecido por `mssql-tools18`;
- autenticação SQL por referência de segredo para o caminho homologado nesta
  entrega.

O exemplo [`examples/config.linux-sql.json`](../examples/config.linux-sql.json)
usa somente paths POSIX e referências de variáveis de ambiente. Ajuste os
endpoints, usuários, tabelas e mounts antes de executar. Em particular:

- `executor_directory` é o path gravável visto pelo processo Python;
- `local_control_directory` é um path local e durável para o SQLite;
- `destination_sql_directory` é a visão, pelo SQL Server de destino, dos mesmos
  bytes de `executor_directory`. Em contêineres, normalmente são dois lados do
  mesmo bind mount.

Para execução Linux, comece pelo exemplo versionável
[`examples/config.linux-sql.json`](../examples/config.linux-sql.json) e adapte
endpoints, credenciais e caminhos ao ambiente real. Se o executor estiver em um
contêiner, `executor_directory` e `destination_sql_directory` devem representar
os dois lados do mesmo volume compartilhado. Mantenha `local_control_directory`
em armazenamento local durável quando a execução precisar sobreviver à
recriação do contêiner. Sem essa equivalência de mounts, a pré-validação de
importação falha antes da carga.

Na raiz do projeto:

```sh
./scripts/launchers/invoke-bcp.sh prerequisites --config ./examples/config.linux-sql.json
./scripts/launchers/invoke-bcp.sh plan --config ./examples/config.linux-sql.json
./scripts/launchers/invoke-bcp.sh ddl --config ./examples/config.linux-sql.json --area both --output ./ddl
./scripts/launchers/invoke-bcp.sh run --config ./examples/config.linux-sql.json --confirm-load
engine_exit_code=$?
```

Em um desktop Linux com Tk e `DISPLAY` disponíveis, a interface também pode ser
aberta com `python3 ./bcp_gui.py` ou `python3 ./bcp_gui.py --config
./config.v2.json`. Em servidor sem sessão gráfica, use a CLI.

Se o arquivo ainda não tiver permissão de execução após ser copiado, aplique uma
vez `chmod +x scripts/launchers/invoke-bcp.sh`. Para selecionar um ambiente
virtual explicitamente:

```sh
BCP_PYTHON="$PWD/.venv/bin/python" \
  ./scripts/launchers/invoke-bcp.sh ddl \
  --config ./examples/config.full.json \
  --area both \
  --output ./ddl
```

Para autenticação SQL não interativa, injete as variáveis referenciadas no JSON
por um gerenciador de segredos do executor. Para uma sessão Bash interativa, uma
opção que evita registrar o valor no histórico é:

```bash
read -r -s -p 'Senha SQL da origem: ' BCP_SOURCE_SQL_PASSWORD; printf '\n'
export BCP_SOURCE_SQL_PASSWORD
read -r -s -p 'Senha SQL da BD_DESTINO_01: ' BCP_BRONZE_SQL_PASSWORD; printf '\n'
export BCP_BRONZE_SQL_PASSWORD
read -r -s -p 'Senha SQL da BD_DESTINO_02: ' BCP_LANDING_SQL_PASSWORD; printf '\n'
export BCP_LANDING_SQL_PASSWORD
./scripts/launchers/invoke-bcp.sh plan --config ./examples/config.linux-sql.json
unset BCP_SOURCE_SQL_PASSWORD BCP_BRONZE_SQL_PASSWORD BCP_LANDING_SQL_PASSWORD
```

No Linux, o motor omite `-P` e responde ao prompt mascarado do `bcp` por uma
PTY privada. A senha não entra em argv, no ambiente do processo `bcp`, em
arquivo temporário ou no log; a redação central também cobre uma eventual
saída indevida do utilitário. Senhas vazias, com NUL ou quebra de linha são
recusadas antes de iniciar o processo. O modo `windows_credentials` permanece
exclusivo do Windows; autenticação integrada no Linux depende de uma instalação
Kerberos/ODBC homologada e não é presumida por este exemplo.

Instruções oficiais de instalação:

- [ODBC e ferramentas `sqlcmd`/`bcp` no Linux](https://learn.microsoft.com/sql/linux/install-upgrade/setup-tools);
- [guia completo de pré-requisitos e verificação deste motor](PRE_REQUISITOS.pt-BR.md);
- [prompt seguro de senha do `bcp` sem `-P`](https://learn.microsoft.com/sql/tools/bcp-utility#-p-password).

`BCP_PYTHON` aceita o nome ou o caminho de um único executável. Não inclua
opções, aspas literais ou uma linha de comando nessa variável.

## Contrato operacional e segredos

Os argumentos são os da CLI Python: `prerequisites`, `plan`, `ddl`, `run`,
`resume`, `import`, `status` e `migrate-control`. Use `--help` no launcher para a lista principal e
`<comando> --help` para os parâmetros de cada operação. A opção global
`--verbose`, quando usada, deve aparecer antes do comando.

O controle SQL no destino de dados ativo não é temporário: o contrato usa
`dbo.execucao`, `dbo.execucao_tabela`, `dbo.execucao_lote` e a tabela técnica
`dbo.versao_esquema`. O estado local
usa `controle_transferencia.sqlite3`, `PRAGMA user_version=5` e as tabelas
`metadados`, `execucao`, `execucao_tabela`, `execucao_lote` e
`tentativa_lote`.

Em executor Windows, informe em `artifact_reader_sids` somente os SIDs
específicos das contas de serviço SQL que realmente precisem ler os mesmos
arquivos. O default é uma lista vazia; executor, SYSTEM e Administradores já são
implícitos. Grupos como Everyone, Authenticated Users e BUILTIN\Users são
rejeitados. Se uma conexão SMB usa uma identidade efetiva diferente do SID do
processo local (por exemplo, conta de máquina), informe exclusivamente esse SID
confiável em `artifact_writer_sids`; ele recebe controle total e passa a compor
a fronteira de confiança. A permissão de share/UNC continua sendo uma
configuração externa, e o diretório pai de `executor_directory` não pode
permitir sua substituição por terceiros.

No POSIX, use um diretório dedicado, pertencente ao executor, sem escrita de
grupo/terceiros. Se o SQL usa outro UID, pré-provisione um GID compartilhado e
`setgid`, por exemplo modo `2750`; arquivos publicados ficam `0440`. O motor
preserva o bit `setgid`, mas não executa `chgrp`. Executor e SQL precisam ver o
mesmo GID/ACL, inclusive através de bind mount. CIFS/NFS que ignore ou recuse
`chmod`, ownership, locks ou semântica de rename falha fechado e deve ser
homologado no ambiente real.

Os launchers não imprimem a lista de argumentos. Mesmo assim, nunca passe senha,
token ou outro segredo pela linha de comando: a linha pode ficar visível no
histórico do shell e na lista de processos. A CLI não possui parâmetro de senha.
Configure apenas uma referência a segredo no JSON (`prompt`, `env` ou
`windows_credential_manager`), conforme os exemplos de autenticação do projeto.
O último provedor é exclusivo do Windows. Variável de ambiente não equivale a
criptografia; limite sua vida útil e remova-a após a execução.

No laboratório, os três endpoints usam `u684`, mas não reutilizam a mesma
referência por contrato: use `BCP_SOURCE_SQL_PASSWORD`,
`BCP_BRONZE_SQL_PASSWORD` e `BCP_LANDING_SQL_PASSWORD`. Em ambientes reais,
cada referência pode resolver para uma senha completamente diferente.

Os códigos são preservados sem conversão:

- `0`: sucesso;
- `1`: falha global ou execução interrompida;
- `2`: resultado parcial, estado ainda não concluído ou argumentos inválidos;
- `127`: o próprio launcher não encontrou o Python ou a entrada da CLI.

Para `prerequisites`, código `2` também indica que o Driver ODBC configurado ou
o BCP compatível não foi encontrado. O comando imprime um relatório JSON e não
abre conexão com SQL Server.

## Ordem operacional

1. Execute `prerequisites`.
2. Execute `plan` e revise função, usuário, instância, porta e banco da Origem,
   de `BD_DESTINO_01` e de `BD_DESTINO_02`, além da estratégia por tabela e do
   destino ativo.
3. Gere e revise `ddl`; aplique-o com `--apply --confirm` apenas nas áreas cujo
   destino tenha função que inclua estrutura.
4. Execute `run --confirm-load` e preserve o `execution_id` impresso.
5. Em caso de pane, use `resume` com o mesmo UUID.

Com `create_structure_if_needed=true` — default — um destino
`structure_and_data` é criado ou completado de forma idempotente durante
`run`; com `false`, o motor apenas valida a estrutura existente. `data_only`
sempre exige layout existente compatível e proíbe criação, evolução e índices.
A etapa DDL explícita é recomendada para todo destino com estrutura. A sequência completa está em
[Ordem do processamento](ORDEM_PROCESSAMENTO.pt-BR.md).

## Comportamentos relevantes à automação

- `enable_cdc=true` é avaliado por tabela. O CDC do banco é
  verificado/habilitado uma única vez e apenas quando ao menos uma tabela o
  solicita. Se o job de cleanup já existir, a retenção é ajustada/confirmada
  nesse preflight. Sem tabela marcada, não há consulta ou alteração de
  CDC/retenção.
- se o job ainda não existir em um banco recém-habilitado, a retenção fica
  pendente até a primeira tabela CDC ser habilitada. O ajuste e a confirmação
  acontecem imediatamente depois e antes de qualquer BCP; as demais tabelas
  usam o resultado em cache.
- quando a retenção muda, o motor chama `sys.sp_cdc_change_job`, reinicia
  somente o job `cleanup` por `sys.sp_cdc_stop_job`/`sys.sp_cdc_start_job` e
  confirma o novo valor antes do BCP. O job `capture` não é reiniciado.
- a CLI registra `cdc_database`, `cdc_retention` e `cdc_table`; o relatório JSON
  mantém a evidência consolidada em `cdc_database`.
- se o preflight CDC ou a confirmação de `cdc_retention_minutes` falhar, as
  tabelas com `enable_cdc=true` afetadas são puladas e registradas, mas as
  tabelas sem CDC continuam sendo processadas. Uma falha isolada na ativação
  de uma tabela pula somente essa tabela.
- `partition_column` é opcional por tabela. A GUI sugere `dh_carga` e deixa a
  opção habilitada ao adicionar uma tabela. Quando presente, gera o contrato
  mensal de particionamento nos DDLs dos destinos com estrutura; quando ausente, nenhum
  objeto de particionamento é criado.
- nos perfis padrão dos destinos, `dh_carga` usa explicitamente o horário
  civil de Brasília (`E. South America Standard Time`) obtido a partir de UTC e
  convertido para `DATETIME2(7)`, sem depender do fuso do SQL Server. Um perfil
  customizado pode definir outra expressão e permanece agnóstico a fuso.
- os defaults operacionais atuais incluem `max_file_bytes=157286400`,
  `control_schema=dbo` (único valor aceito) e
  `structure.secondary_indexes_phase=before_load`.
- `DIRECT_KEYLESS` usa contagem aproximada por metadados para pré-admissão, sem
  `COUNT_BIG` na Origem. O número real do BCP é validado contra o limite global
  antes da importação.
- antes de carregar cada tabela, o motor verifica o espaço dos volumes do
  destino ativo individualmente; dados e log não têm suas capacidades somadas. A menor
  disponibilidade é a limitante e todos os volumes precisam comportar o
  requisito. Insuficiência comprovada registra a tabela como pulada e segue
  para a próxima. Isso também vale para `import --manifest`: somente os bytes
  reais dos blocos ainda não confirmados, acrescidos do fator de segurança, são
  validados antes de criar controle SQL, aplicar DDL ou executar `OPENROWSET`.
- `run`, `resume` e `import` carregam somente o endpoint selecionado por
  `active_destination`; um destino `structure_only` nunca lê arquivos BCP.

## Falha e retomada

Depois de uma falha de `run`, não execute outro `run`: preserve o SQLite, os
artefatos e o controle SQL e use `resume` com o mesmo UUID. O `status` consulta
o controle local; `query_control.sql` comprova os commits no destino ativo.

```powershell
& .\scripts\launchers\invoke-bcp.ps1 status --config .\config.v2.json --execution-id UUID
& .\scripts\launchers\invoke-bcp.ps1 resume --config .\config.v2.json --execution-id UUID --confirm-load
```

```bash
./scripts/launchers/invoke-bcp.sh status --config ./config.v2.json --execution-id UUID
./scripts/launchers/invoke-bcp.sh resume --config ./config.v2.json --execution-id UUID --confirm-load
```

Uma falha no BCP refaz o bloco lógico incompleto; ela não continua de um byte
ou linha dentro do `.partial`. Na carga, dados, registro do bloco e checkpoint
são confirmados ou revertidos juntos. Consulte o runbook completo em
[Falhas, checkpoints e retomada](FALHAS_E_RETOMADA.pt-BR.md).
