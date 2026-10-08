$forwardedArguments = @($args)
$ErrorActionPreference = "Stop"

function Write-LauncherError {
    param([string]$Message)

    # Windows PowerShell 5.1 reads BOM-less UTF-8 through the ANSI code page.
    # Unicode escapes keep this launcher ASCII while rendering readable messages.
    [Console]::Error.WriteLine([regex]::Unescape($Message))
}

function Test-PythonCommand {
    param([string]$Command)

    try {
        & $Command -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" `
            *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Resolve-PythonCommand {
    if (-not [string]::IsNullOrWhiteSpace($env:BCP_PYTHON)) {
        if (Test-Path -LiteralPath $env:BCP_PYTHON -PathType Leaf) {
            return (Resolve-Path -LiteralPath $env:BCP_PYTHON).ProviderPath
        }

        $configuredCommand = Get-Command -Name $env:BCP_PYTHON -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $configuredCommand) {
            return $configuredCommand.Source
        }

        Write-LauncherError(
            "N\u00e3o foi poss\u00edvel localizar o interpretador Python configurado em BCP_PYTHON."
        )
        exit 127
    }

    $candidateNames = if ($PSVersionTable.PSEdition -eq "Desktop" -or $IsWindows) {
        @("python.exe", "python3.exe", "python", "python3")
    }
    else {
        @("python3", "python")
    }

    foreach ($candidateName in $candidateNames) {
        $candidateCommand = Get-Command -Name $candidateName -CommandType Application `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $candidateCommand -and (Test-PythonCommand $candidateCommand.Source)) {
            return $candidateCommand.Source
        }
    }

    Write-LauncherError(
        "N\u00e3o foi poss\u00edvel localizar o Python 3.10 ou superior. Instale-o ou defina BCP_PYTHON."
    )
    exit 127
}

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).ProviderPath
$entryPoint = Join-Path $repositoryRoot "bcp_bronze.py"

if (-not (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    Write-LauncherError("N\u00e3o foi poss\u00edvel localizar a entrada da CLI do BulkFlow.")
    exit 127
}

$pythonCommand = Resolve-PythonCommand
$invocationArguments = @($entryPoint) + $forwardedArguments

# The legacy native binder drops empty strings. Preserve their position without
# reconstructing or joining any other argument. PowerShell 7.3+ does this itself.
if (
    $PSVersionTable.PSEdition -eq "Desktop" -or
    $PSVersionTable.PSVersion -lt [version]"7.3"
) {
    $invocationArguments = @(
        foreach ($argument in $invocationArguments) {
            if ($argument.Length -eq 0) { '""' } else { $argument }
        }
    )
}

try {
    & $pythonCommand @invocationArguments
    $engineExitCode = $LASTEXITCODE
}
catch {
    Write-LauncherError("N\u00e3o foi poss\u00edvel iniciar a CLI do BulkFlow.")
    exit 127
}

if ($null -eq $engineExitCode) {
    Write-LauncherError("A CLI do BulkFlow terminou sem informar um c\u00f3digo de sa\u00edda.")
    exit 1
}

exit [int]$engineExitCode
