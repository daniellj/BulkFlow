# Migração não destrutiva do controle SQLite legado

O controle local vigente é persistente e usa o arquivo
`controle_transferencia.sqlite3`. Sua assinatura física contém somente nomes em
português:

- `metadados`;
- `execucao`;
- `execucao_tabela`;
- `execucao_lote`;
- `tentativa_lote`.

A versão local é `PRAGMA user_version=5`. `versao_esquema` é a tabela técnica
de versão do controle SQL Server e não uma tabela nem uma chave do SQLite.

## Migração automática do legado v4

Ao abrir o controle, o motor reconhece somente a assinatura v4 exata. Se o
arquivo legado `bcp_control_v2.sqlite3` estiver no diretório, publica uma cópia
v5 validada como `controle_transferencia.sqlite3` e preserva o legado intacto.
Se o layout v4 já estiver no nome novo, a rotina cria
`controle_transferencia.sqlite3.v4.backup` antes da substituição atômica.

A reabertura do layout v5 é idempotente. Versões 0, 1 e 2, versão futura,
estrutura desconhecida, parcial ou adulterada falham fechadas sem publicar nem
alterar um controle.

## Migração explícita do legado v3

Para v3, pare todos os processos que usam o diretório e execute:

```powershell
python .\bcp_bronze.py migrate-control `
  --control-directory C:\caminho\controle
```

```bash
python3 ./bcp_bronze.py migrate-control \
  --control-directory /caminho/controle
```

Para escolher o local do backup v3, acrescente `--backup-path CAMINHO`. O
destino não pode ser o arquivo de origem nem `controle_transferencia.sqlite3` e
nunca é sobrescrito.

O comando `migrate-control` converte v3 diretamente para v5 e preserva o backup
v3 completo, inclusive a tabela legada `index_states`. O backup nunca é
sobrescrito; se o caminho escolhido já contiver outro arquivo, a operação falha
sem modificar a origem.

## Garantias comuns

A rotina reconhece a assinatura legada suportada, copia todos os registros para
um candidato novo, valida a equivalência lógica e somente então publica
`controle_transferencia.sqlite3`. A origem legada não é apagada nem
sobrescrita. Estrutura desconhecida, parcial ou adulterada falha fechada, sem
publicar um destino incompleto.

Antes da publicação, a rotina:

1. abre o legado em modo controlado e valida sua assinatura integral;
2. cria o candidato com `metadados`, `execucao`, `execucao_tabela`,
   `execucao_lote` e `tentativa_lote`;
3. converte nomes e relacionamentos sem alterar UUIDs, checkpoints, contagens,
   cursores, tentativas ou estados;
4. executa `PRAGMA integrity_check` e compara as quantidades e chaves entre
   origem e candidato;
5. publica o novo arquivo por substituição atômica e mantém o legado para
   auditoria/recuperação.

Se `controle_transferencia.sqlite3` já existir, ele deve possuir a assinatura
v5 exata e representar o mesmo estado. Uma segunda execução sobre uma migração
concluída é idempotente; divergência entre legado e destino é erro e nenhum
arquivo é corrigido por suposição.

## Nomes legados

Somente esta seção cita os identificadores anteriores para permitir localizar e
auditar uma instalação antiga. O arquivo era
`bcp_control_v2.sqlite3`, com tabelas internas `meta`, `executions`,
`table_runs`, `blocks` e `attempts`; versões ainda mais antigas podiam conter
`index_states`. Esses nomes não são usados na nova estrutura e não devem ser
criados manualmente.

Depois da migração, preserve o arquivo legado durante o período de homologação.
Não o renomeie para o nome novo: o motor diferencia os contratos pela assinatura
física, não apenas pelo nome do arquivo.

## Configurações anteriores

O comando migra somente o estado SQLite local. Ele não converte arquivos JSON
de configuração nem os objetos SQL persistentes do destino. Recrie
configurações anteriores a partir dos exemplos atuais e valide-as com `plan`.
No SQL Server Bronze, o contrato vigente usa exclusivamente
`DBRO684.dbo.execucao`, `DBRO684.dbo.execucao_tabela`,
`DBRO684.dbo.execucao_lote` e a tabela técnica
`DBRO684.dbo.versao_esquema`. A remoção de um schema SQL legado deve ocorrer
somente em uma migração administrativa explícita; o motor não apaga histórico
automaticamente. A regra de preservação desta página continua valendo para os
arquivos SQLite, inclusive para o legado `bcp_control_v2.sqlite3`. A Landing não
recebe tabelas de controle.
