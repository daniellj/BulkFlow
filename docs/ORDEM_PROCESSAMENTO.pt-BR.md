[English](ORDEM_PROCESSAMENTO.md) | **Português (Brasil)**

# Ordem do processamento

Este é o fluxo operacional do **BulkFlow**. A
mesma ordem vale para a interface gráfica, a CLI Python e o executável
`BulkFlowCLI.exe`. Docker é usado somente pelo laboratório local.

## Ordem recomendada para o operador

### 1. Atender e validar os pré-requisitos

Na máquina que executará o motor, instale o Microsoft ODBC Driver indicado por
`odbc_driver` e o utilitário Microsoft `bcp` indicado por `bcp_executable`.
Valide ambos antes de abrir conexões de negócio:

```powershell
.\release\BulkFlowCLI.exe prerequisites --config .\config.v2.json
```

```bash
./scripts/launchers/invoke-bcp.sh prerequisites --config ./config.v2.json
```

Os EXEs Windows incorporam Python e os pacotes do projeto, mas não podem
incorporar nem instalar esses componentes nativos da Microsoft.

### 2. Planejar

Informe as credenciais de Origem, `BD_DESTINO_01` e `BD_DESTINO_02`. Cada
endpoint possui função, instância, porta, banco, schema, tipo de autenticação,
usuário e segredo independentes. A Origem deve usar `data_provider`; no máximo
um destino pode usar `structure_and_data` ou `data_only`. Exatamente um é
obrigatório quando `execute_import=true` e torna-se `active_destination`. A senha é capturada de forma mascarada e não é gravada
na configuração, argv, manifesto ou log.

Os bancos exibidos em cada tabela herdam os endpoints. Nesta versão não são
rotas adicionais: o banco de origem da tabela deve coincidir com o endpoint
Origem, e o banco de destino com o destino de dados ativo. Para outro par de bancos,
use outra configuração/execução.

`plan` é somente leitura. Ele valida configuração, conexões, identidade
efetiva, catálogo de origem, estratégia de chave, compatibilidade dos perfis,
estimativas e espaço do executor. Depois do planejamento, a GUI mostra para
cada ambiente o usuário, a instância, a porta e o banco efetivamente usados.

### 3. Gerar, revisar e aplicar DDL

Gere o DDL de `bronze`, `landing` ou `both`; revise o script e então aplique-o.
Esses nomes são IDs internos compatíveis: `bronze` identifica
`bronze_destination` (`BD_DESTINO_01`) e `landing` identifica
`landing_destination` (`BD_DESTINO_02`); as funções determinam o comportamento.
Um destino `structure_only` aceita DDL/evolução manual e nunca dados;
`data_only` rejeita DDL, evolução e criação de índices; e
`structure_and_data` permite ambos. `create_structure_if_needed` vem habilitado por padrão e controla o
provisionamento **automático** feito por `run`, `resume` e importação de
manifestos. Quando desabilitado, essas operações apenas validam o que já existe
e não criam objetos ausentes. O comando explícito `ddl --apply --confirm` (e o
botão **Aplicar DDL**) é uma autorização separada: ele aplica os scripts
selecionados mesmo com o checkbox desmarcado. `allow_schema_evolution` continua
sendo uma decisão distinta: controla se colunas de negócio novas podem ser
adicionadas a tabelas já existentes.

```powershell
.\release\BulkFlowCLI.exe ddl --config .\config.v2.json `
  --area both --output .\ddl

.\release\BulkFlowCLI.exe ddl --config .\config.v2.json `
  --area both --output .\ddl --apply --confirm
```

Quando a importação está habilitada, exatamente um destino configurado inclui
dados. Ele recebe as linhas e os objetos persistentes de controle em `dbo`; um destino `structure_only` nunca
lê artefatos BCP.

### 4. Executar a exportação/importação

```powershell
.\release\BulkFlowCLI.exe run --config .\config.v2.json --confirm-load
```

O comando cria e exibe um UUID. Preserve esse UUID, o SQLite local, os
artefatos e o controle SQL do destino ativo. Se houver falha, use `resume` com o mesmo
UUID; não inicie outro `run` para tentar continuar.

## Ordem interna de uma execução

1. Validar o contrato V2 e calcular as impressões digitais estrutural e
   operacional.
2. Abrir o controle local `controle_transferencia.sqlite3` e obter o lease do
   UUID.
3. Validar o diretório de artefatos, conectar à Origem e testar versão e
   capacidades do BCP.
4. Quando `execute_import=true`, conectar ao destino de dados ativo, provar que o SQL Server vê
   os mesmos bytes, e validar/criar `dbo.versao_esquema`, `dbo.execucao`,
   `dbo.execucao_tabela` e `dbo.execucao_lote` nesse banco.
5. Se ao menos uma tabela tiver `enable_cdc=true`, verificar/habilitar o CDC do
   banco de Origem uma única vez. Se o job de cleanup já existir, verificar,
   ajustar e confirmar sua retenção nesse momento e guardar o resultado em
   cache. A retenção usa `cdc_retention_minutes`, cujo default é `262800`
   minutos (seis meses, aproximadamente 182,5 dias). Em um banco
   recém-habilitado, o SQL Server pode criar o job apenas depois da primeira
   tabela CDC; sua ausência inicial fica marcada como pendente. Se nenhuma
   tabela pedir CDC, o motor não consulta nem altera o CDC ou sua retenção.
6. Percorrer as tabelas na ordem da configuração. Para cada tabela:

   1. quando sinalizada, verificar o preflight CDC e habilitar/confirmar o CDC
      na tabela. Se a retenção estiver pendente, verificar, ajustar e confirmar
      o job de cleanup imediatamente depois dessa ativação e **antes de
      qualquer BCP**. Quando o valor muda, executar `sys.sp_cdc_change_job`,
      parar e iniciar somente o job `cleanup` por `sys.sp_cdc_stop_job` e
      `sys.sp_cdc_start_job`, e confirmar novamente a retenção. O job `capture`
      não é reiniciado. O resultado confirmado ou a falha fica em cache para
      as demais tabelas. Falha na ativação da tabela pula somente essa tabela;
      uma falha do CDC do banco ou do cleanup/retenção bloqueia as tabelas CDC
      seguintes, sem impedir as tabelas que não solicitaram CDC;
   2. validar catálogo, tipos especiais, PK/UNIQUE ou a marca d'água explícita;
   3. capturar/reutilizar o teto e o checkpoint da tabela;
   4. calcular estimativas e verificar espaço do executor;
   5. quando há estimativa de bytes, verificar separadamente cada volume do
      destino ativo, usando o menor espaço disponível como capacidade limitante;
      insuficiência comprovada registra
      `SKIPPED_DESTINATION_INSUFFICIENT_SPACE` e segue para a próxima tabela;
   6. criar/revalidar/evoluir a estrutura ativa somente para
      `structure_and_data`; validar estritamente um layout existente para `data_only`;
   7. reconciliar e importar primeiro qualquer manifesto final pendente;
   8. exportar o próximo bloco com `bcp queryout` para `.partial`;
   9. conferir quantidade real, limites e tamanho, calcular SHA-256 e publicar
      dado e manifesto atomicamente;
   10. importar no destino ativo por `INSERT ... SELECT ... OPENROWSET(BULK...)`;
       nesse mesmo `INSERT`, preencher `bi_lsn_evento` e
       `bi_sequencia_evento` com zero em `BINARY(10)`, sem `UPDATE` posterior;
   11. confirmar dados, `dbo.execucao_lote` e checkpoint SQL
       na mesma transação;
   12. confirmar o bloco no SQLite e, se configurado, apagar somente o arquivo
       de dados já confirmado;
   13. repetir até o teto, validar a cardinalidade do destino e concluir índices
       secundários somente quando a função permitir sua criação.
7. Gravar os relatórios JSON e CSV e retornar o código de saída consolidado.

O JSON da execução registra a prova consolidada em `cdc_database`. Quando o
valor muda, `retention_changed=true`; quando o restart é confirmado,
`retention_restarted=true`. Uma corrida transitória do SQL Server Agent no
restart é tentada novamente por até 30 segundos antes de falhar fechada.

Uma falha no preflight do banco ou na confirmação da retenção não encerra as
tabelas que não solicitaram CDC: as tabelas com `enable_cdc=true` afetadas são
puladas com log, enquanto as tabelas com `enable_cdc=false` continuam na ordem
configurada. A ausência do job antes da primeira tabela CDC não é falha por si
só; ela se torna falha se o job continuar ausente após uma tabela ser
habilitada/confirmada.

Para `structure_and_data`, o `run` também provisiona idempotentemente a
estrutura ativa antes de carregar cada tabela. Para `data_only`, ele valida um
layout existente compatível e nunca cria/evolui objetos ou índices. A etapa DDL
explícita continua recomendada para todo destino cuja função inclua estrutura.

## Estratégia de cursor e tabela sem chave

A precedência é marca d'água explícita comprovada, PK elegível, UNIQUE elegível
e, por último, `DIRECT_KEYLESS`. O motor não descobre uma combinação de colunas
por tentativa.

Para `DIRECT_KEYLESS`, a pré-admissão usa a contagem aproximada dos metadados,
sem `COUNT_BIG` na Origem. O BCP exporta a tabela como um único bloco e a
quantidade real copiada é comparada com `keyless_direct_load_max_rows` antes da
publicação/importação. Esse modo usa `TABLOCK,HOLDLOCK`, pode bloquear escritores
durante a leitura e não permite retomada dentro do arquivo; uma tentativa
interrompida refaz a tabela inteira.

## Retomada

`resume` percorre novamente a ordem da configuração para revalidar contratos,
mas não reexporta nem reinsere blocos já confirmados. Em uma pane na 11ª de 20
tabelas, as dez anteriores são reconciliadas, a 11ª volta ao último bloco
durável e, depois, o fluxo segue para as restantes. Consulte
[Falhas, checkpoints e retomada](FALHAS_E_RETOMADA.pt-BR.md) para a matriz completa.
