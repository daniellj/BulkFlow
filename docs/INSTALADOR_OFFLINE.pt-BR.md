[English](INSTALADOR_OFFLINE.md) | **Português (Brasil)**

# Instalador offline para Windows

## Resultado para o usuário final

`Setup-BulkFlow.exe` é o pacote recomendado para uma estação Windows x64
nova ou sem os clientes SQL necessários. Ele reúne, em um único arquivo:

- `BulkFlowGUI.exe` e `BulkFlowCLI.exe`, já com Python, Tk e as bibliotecas
  Python do projeto;
- Microsoft ODBC Driver 18 for SQL Server;
- Microsoft Command Line Utilities, que fornece o utilitário `bcp`.

O instalador funciona sem acesso à internet. Os componentes Microsoft ficam
incorporados como instaladores oficiais e são instalados no Windows; eles não
são copiados para dentro dos executáveis da aplicação nem alterados pelo
projeto.

Depois da instalação, o operador não precisa instalar Python, executar `pip`
ou localizar manualmente o BCP. O Driver ODBC e o BCP continuam sendo
componentes nativos do sistema, agora provisionados pelo próprio pacote
offline.

Os dois EXEs avulsos continuam disponíveis para distribuição portátil. Nessa
modalidade, o administrador ainda precisa instalar o Driver ODBC e o BCP por
outro meio.

## Componentes fixados nesta entrega

| Componente | Versão do pacote offline |
|---|---:|
| BulkFlow | 2.0.0 |
| Microsoft ODBC Driver 18 for SQL Server (x64) | 18.7.1.1 |
| Microsoft Command Line Utilities/BCP (x64) | 17.0.4055.5 |

A build 17 do BCP incluída nesta entrega foi selecionada porque sua ajuda
comprova os controles de segurança TLS `-Y` e `-u` exigidos pelo motor. O
instalador não baixa versões mais recentes durante a execução. Atualizar um
componente exige gerar e homologar uma nova entrega.

Fontes oficiais:

- [Download do Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server?view=sql-server-ver17);
- [requisitos, instalação, licença e propriedade `APPGUID` do Driver ODBC](https://learn.microsoft.com/en-us/sql/connect/odbc/windows/system-requirements-installation-and-driver-files?view=sql-server-ver17);
- [download e instalação do utilitário BCP](https://learn.microsoft.com/en-us/sql/tools/bcp/bcp-download-install?view=sql-server-ver17).

## Instalação interativa

1. Copie `Setup-BulkFlow.exe` para a estação Windows x64 por um canal
   controlado.
2. Confira o hash SHA-256 publicado com a entrega.
3. Execute o instalador e confirme a solicitação do Controle de Conta de
   Usuário (UAC). A elevação é necessária para instalar em `Program Files` e
   registrar os componentes Microsoft para a máquina.
4. Leia e aceite os termos de licença exibidos pelo instalador.
5. Selecione **Instalar** e aguarde a confirmação de conclusão.
6. Abra a interface pelo atalho **BulkFlow** ou use `BulkFlowCLI.exe` em um
   terminal.
7. Antes do primeiro planejamento, execute **Verificar pré-requisitos** na GUI
   ou o comando `prerequisites` da CLI.

Nenhuma senha de banco é pedida ou armazenada pelo instalador. As credenciais
de Origem, `BD_DESTINO_01` e `BD_DESTINO_02` continuam sendo solicitadas em tempo de operação,
com a senha mascarada e independente para cada conexão.

## Instalação silenciosa

Abra PowerShell ou o prompt de comando com a política de elevação apropriada e
execute:

```powershell
New-Item -ItemType Directory -Force C:\Logs\BulkFlow | Out-Null
& .\Setup-BulkFlow.exe /install /quiet /norestart `
    /log "C:\Logs\BulkFlow\instalacao.log"
$exitCode = $LASTEXITCODE
```

Interprete os códigos de retorno do instalador e dos pacotes MSI:

- `0`: concluído sem solicitação de reinicialização;
- `3010`: concluído; reinicialização necessária;
- `1641`: concluído; reinicialização iniciada pelo instalador;
- qualquer outro valor: falha; consulte o log indicado.

Em implantação silenciosa, a organização que distribui o pacote é responsável
por revisar e aceitar previamente os termos de licença dos componentes
Microsoft. Não use o modo silencioso como forma de omitir essa aprovação.

O instalador é integralmente offline: ausência de rede não muda o conteúdo nem
faz a instalação procurar pacotes externos.

## Diretórios e dados preservados

Arquivos imutáveis da aplicação são instalados, por padrão, em:

```text
C:\Program Files\BulkFlow\
```

Arquivos mutáveis do usuário ficam fora de `Program Files`, por padrão em:

```text
%LOCALAPPDATA%\BulkFlow\
├── bcp-data\
├── .bcp-control\
├── ddl\
└── config\
```

Esse layout vale para a aplicação instalada ou para uma cópia congelada
isolada. Ao executar a GUI diretamente pelo código-fonte — ou o EXE portátil
pela pasta `release` dessa árvore — os três diretórios operacionais equivalentes
ficam em `<raiz do projeto>\Local\BulkFlow`. Assim, o executável instalado não
tenta gravar em `Program Files`.

Essa separação permite executar a aplicação sem conceder escrita na pasta do
programa. A configuração ainda pode apontar diretórios operacionais para outro
local autorizado, inclusive o compartilhamento exigido pelo SQL Server do
destino de dados ativo.

Reparo, atualização e desinstalação preservam os artefatos operacionais em
`%LOCALAPPDATA%\BulkFlow`, inclusive configurações, manifestos, arquivos BCP
e checkpoints. A desinstalação remove a aplicação, mas não remove
automaticamente o Driver ODBC e as Command Line Utilities compartilhadas com
outros programas. Para apagar dados de trabalho, faça uma revisão explícita e
uma remoção separada depois de confirmar que não existe execução retomável.

## Reparo, atualização e desinstalação

- **Reparo:** execute novamente o mesmo `Setup-BulkFlow.exe` ou use
  **Aplicativos instalados** no Windows. O reparo recompõe arquivos da
  aplicação sem apagar os dados do usuário.
- **Atualização:** execute o instalador da nova entrega. O pacote aplica as
  regras de versão e não deve permitir substituir silenciosamente uma versão
  mais nova por uma antiga.
- **Desinstalação:** use **Configurações > Aplicativos > Aplicativos
  instalados**. Os clientes Microsoft compartilhados e os dados operacionais
  são preservados conforme descrito acima.

Homologue explicitamente os três caminhos antes de distribuir uma atualização.
Não considere a simples sobreposição manual dos EXEs uma atualização do produto
instalado.

## Logs e diagnóstico

Para uma instalação assistida, o bootstrapper cria seus logs no diretório
temporário do Windows. Para suporte e automação, prefira informar `/log` e um
caminho protegido e persistente, como no exemplo de instalação silenciosa.

Um diagnóstico mínimo deve registrar:

```powershell
Get-OdbcDriver | Where-Object Name -eq 'ODBC Driver 18 for SQL Server'
Get-Command bcp -All
bcp -v
bcp -?
& "$env:ProgramFiles\BulkFlow\BulkFlowCLI.exe" prerequisites `
    --config .\config.v2.json
```

O instalador registra no sistema o caminho do BCP provisionado. O motor prefere
esse caminho conhecido quando a configuração usa apenas `bcp`; um caminho
absoluto explicitamente configurado continua tendo precedência.

## Segurança e autenticidade

- Distribua o setup e seu arquivo de hashes pelo repositório corporativo ou
  outro canal autenticado.
- Verifique SHA-256 antes de executar. Um hash confirma integridade apenas
  quando o valor de referência veio de um canal confiável.
- Os MSIs Microsoft incorporados devem manter assinatura Authenticode válida da
  Microsoft Corporation. A rotina de build recusa pacotes com hash diferente
  do valor fixado.
- Não inclua senhas, arquivos de configuração com segredos, certificados
  privados ou artefatos BCP no instalador.
- Restrinja os diretórios operacionais conforme o modelo descrito em
  [Pré-requisitos](PRE_REQUISITOS.pt-BR.md); a instalação não concede permissões de
  banco, não abre firewall e não cria compartilhamentos.

O artefato produzido neste laboratório não possui certificado de assinatura de
código do publicador. Por isso, o Windows pode mostrar **Publicador
desconhecido** no UAC ou no Microsoft Defender SmartScreen. Isso não deve ser
ocultado: para distribuição de produção, assine o EXE do instalador, o MSI da
aplicação e os EXEs da GUI/CLI com um certificado corporativo de assinatura de
código e valide a assinatura depois do empacotamento. Até lá, use somente o
hash publicado por um canal confiável e restrinja a entrega à homologação.

## Checklist de homologação em VM limpa e offline

Use uma VM Windows x64 suportada, com snapshot anterior ao teste e rede
desconectada. Registre evidências de cada item.

- [ ] Confirmar que Python, ODBC Driver 18 e BCP não estão instalados.
- [ ] Validar o SHA-256 do `Setup-BulkFlow.exe` antes da execução.
- [ ] Instalar interativamente, aceitar o UAC/licenças e confirmar conclusão sem
      download ou acesso à internet.
- [ ] Confirmar a instalação em `C:\Program Files\BulkFlow` e os atalhos.
- [ ] Abrir GUI e CLI sem Python no `PATH`.
- [ ] Confirmar `ODBC Driver 18 for SQL Server` e executar `bcp -v`/`bcp -?`.
- [ ] Executar `prerequisites`, `plan`, geração/aplicação de DDL e uma carga de
      teste com Origem, `BD_DESTINO_01` e `BD_DESTINO_02` configurados; conferir
      que no máximo uma função de destino inclua dados e que exatamente uma o
      faça quando o teste importar dados.
- [ ] Confirmar que os arquivos mutáveis são criados em `%LOCALAPPDATA%` ou nos
      caminhos configurados, nunca em `Program Files`.
- [ ] Repetir o setup com as mesmas versões e comprovar idempotência/reparo.
- [ ] Testar sobre uma VM que já tenha as mesmas versões dos componentes
      Microsoft.
- [ ] Testar sobre uma VM que já tenha versões compatíveis mais novas; nenhuma
      versão deve ser rebaixada.
- [ ] Corromper uma cópia de laboratório de um pacote e comprovar que a
      validação de integridade impede a entrega/instalação.
- [ ] Testar `/quiet /norestart /log`, capturar o código de saída e revisar os
      logs.
- [ ] Simular falta de espaço e uma falha de pacote; confirmar mensagem, código
      não zero e ausência de estado parcial enganoso.
- [ ] Executar reparo e confirmar que configurações/checkpoints permanecem.
- [ ] Atualizar a aplicação e comprovar preservação dos dados e bloqueio de
      downgrade.
- [ ] Desinstalar e confirmar remoção do aplicativo, preservação dos dados do
      usuário e dos clientes Microsoft compartilhados.
- [ ] Validar uma retomada real depois de reinstalação/reparo.
- [ ] Confirmar que logs, registro do Windows, atalhos e diretórios instalados
      não contêm senhas.

Somente promova o pacote depois de concluir esse roteiro em uma VM realmente
limpa. Extração administrativa, inspeção estática do MSI e testes na estação de
build são complementares, mas não substituem essa homologação.
