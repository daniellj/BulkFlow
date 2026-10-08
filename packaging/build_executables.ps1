[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$releaseRoot = Join-Path $projectRoot $OutputDirectory
$workRoot = Join-Path $projectRoot "build\pyinstaller"
$separator = [IO.Path]::PathSeparator
$cliVersionFile = Join-Path $PSScriptRoot "version_info_cli.txt"
$guiVersionFile = Join-Path $PSScriptRoot "version_info_gui.txt"

foreach ($requiredFile in @($cliVersionFile, $guiVersionFile)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Arquivo de metadados de versão não encontrado: $requiredFile"
    }
}

& $Python -c "import PyInstaller, pyodbc, ttkbootstrap, winpty" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Dependências de empacotamento ausentes. Instale requirements-build.txt."
}

$common = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--paths", $projectRoot,
    "--workpath", $workRoot,
    "--distpath", $releaseRoot,
    "--specpath", (Join-Path $projectRoot "build"),
    "--add-data", ((Join-Path $projectRoot "templates") + $separator + "templates"),
    "--add-data", ((Join-Path $projectRoot "schemas") + $separator + "schemas"),
    "--hidden-import", "pyodbc",
    "--hidden-import", "winpty"
)

& $Python -m PyInstaller @common `
    --version-file $cliVersionFile `
    --name "BulkFlowCLI" `
    (Join-Path $projectRoot "bcp_bronze.py")
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar BulkFlowCLI.exe" }

& $Python -m PyInstaller @common `
    --windowed `
    --collect-all "ttkbootstrap" `
    --hidden-import "bcp_engine.gui" `
    --version-file $guiVersionFile `
    --name "BulkFlowGUI" `
    (Join-Path $projectRoot "bcp_gui.py")
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar BulkFlowGUI.exe" }

$cli = Join-Path $releaseRoot "BulkFlowCLI.exe"
$gui = Join-Path $releaseRoot "BulkFlowGUI.exe"
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    throw "Executável CLI não foi encontrado após o build."
}
if (-not (Test-Path -LiteralPath $gui -PathType Leaf)) {
    throw "Executável GUI não foi encontrado após o build."
}

Write-Host "Executáveis gerados:"
Write-Host "  $gui"
Write-Host "  $cli"
Write-Host "Python e pacotes do projeto estão incorporados. Driver ODBC e BCP são componentes Microsoft do sistema e permanecem pré-requisitos."
