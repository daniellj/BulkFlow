[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "2.0.0",

    [switch]$RebuildExecutables,
    [string]$Python = "python",

    # When supplied, the two application executables, the application MSI and
    # the final bundle are signed in that order. Microsoft payloads retain their
    # original Microsoft signatures and are never modified.
    [string]$SigningCertificateThumbprint = "",
    [string]$SignToolPath = "",
    [string]$TimestampUrl = "https://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Version -ne "2.0.0") {
    throw "Esta definição usa o ProductCode imutável da versão 2.0.0. Para outra versão, crie um mapeamento explícito de ProductCode antes do build."
}

$appProductCode = "{808419BB-B33B-4F75-95D6-273B00D4B16F}"
$appUpgradeCode = "{C391D478-887C-4E8D-A864-CB0C30AB8F8A}"
$bundleUpgradeCode = "{05D7D472-6DD3-4308-BA49-7C651EE2C1FE}"
$bundleVersion = "$Version.0"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$releaseRoot = Join-Path $projectRoot "release"
$redistRoot = Join-Path $PSScriptRoot "redist"
$cacheRoot = Join-Path $PSScriptRoot "cache"
$wixSourceRoot = Join-Path $PSScriptRoot "wix"
$buildRoot = Join-Path $projectRoot "build\installer"
$toolRoot = Join-Path $buildRoot "tools\wix314-$PID"

$odbcPayload = [pscustomobject]@{
    Name = "Microsoft ODBC Driver 18 for SQL Server"
    FileName = "msodbcsql-18.7.1.1-x64-en-us.msi"
    Version = "18.7.1.1"
    ProductCode = "{3BDB4B75-1142-441B-9313-FFE275EFEB34}"
    UpgradeCode = "{ADA68B65-BFF8-4E6A-B082-CC6682D425B8}"
    Sha256 = "21EF69E4B942F18ACED55FA7C3A8D7263004E113A96690BAC7509645E1B1A310"
    Url = "https://download.microsoft.com/download/d624e1c6-293b-4d6f-91b8-6e515a5d6a77/amd64/1033/msodbcsql.msi"
}

$bcpPayload = [pscustomobject]@{
    Name = "Microsoft Command Line Utilities 17 for SQL Server"
    FileName = "MsSqlCmdLnUtils-17.0.4055.5-x64-en-us.msi"
    Version = "17.0.4055.5"
    ProductCode = "{C517BD2F-80FB-4694-B357-F9CD1D307E5F}"
    UpgradeCode = "{0DAB50C8-811C-4D77-873C-C63A8DFDDC02}"
    Sha256 = "25C7208CD5BA98BEAE4F1A464625A78D3607498E990FDDE40FE59E747F2B8BAC"
    Url = "https://download.microsoft.com/download/6f8fa386-26c0-4376-b779-66d2e22378e8/SqlCmdLnUtils17.0.4055.5/amd64/1033/MsSqlCmdLnUtils.msi"
}

$wixPayload = [pscustomobject]@{
    FileName = "wix314-binaries.zip"
    Sha256 = "6AC824E1642D6F7277D0ED7EA09411A508F6116BA6FAE0AA5F2C7DAA2FF43D31"
    Url = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"
}

function Assert-WindowsBuildHost {
    if ($env:OS -ne "Windows_NT") {
        throw "O instalador MSI/Burn deve ser compilado em Windows."
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "O instalador é x64 e exige um host de build Windows 64 bits."
    }
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Content
    )
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Get-Sha256 {
    param([Parameter(Mandatory)] [string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Assert-ExpectedHash {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Expected
    )
    $actual = Get-Sha256 -Path $Path
    if ($actual -ne $Expected.ToUpperInvariant()) {
        throw "SHA-256 inválido para '$Path'. Esperado: $Expected. Obtido: $actual. O arquivo não será usado."
    }
}

function Get-OrDownloadPayload {
    param(
        [Parameter(Mandatory)] [string]$Destination,
        [Parameter(Mandatory)] [string]$Url,
        [Parameter(Mandatory)] [string]$Sha256
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Assert-ExpectedHash -Path $Destination -Expected $Sha256
        return
    }

    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$Destination.download"
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }

    Write-Host "Baixando payload oficial: $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $temporary
    try {
        Assert-ExpectedHash -Path $temporary -Expected $Sha256
        Move-Item -LiteralPath $temporary -Destination $Destination
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Assert-MicrosoftSignature {
    param([Parameter(Mandatory)] [string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "A assinatura Authenticode Microsoft de '$Path' não é válida: $($signature.StatusMessage)"
    }
    if ($null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
        throw "O payload '$Path' não foi assinado por Microsoft Corporation."
    }
    return $signature
}

function Get-MsiDatabase {
    param([Parameter(Mandatory)] [string]$Path)
    $installer = New-Object -ComObject WindowsInstaller.Installer
    return $installer.OpenDatabase($Path, 0)
}

function Get-MsiPropertyValue {
    param(
        [Parameter(Mandatory)] $Database,
        [Parameter(Mandatory)] [string]$Name
    )
    $escapedName = $Name.Replace("'", "''")
    $view = $Database.OpenView("SELECT ``Value`` FROM ``Property`` WHERE ``Property``='$escapedName'")
    $null = $view.Execute()
    $record = $view.Fetch()
    if ($null -eq $record) {
        return $null
    }
    return $record.StringData(1)
}

function Assert-MsiIdentity {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] $Payload
    )
    $database = Get-MsiDatabase -Path $Path
    $actualProductCode = Get-MsiPropertyValue -Database $database -Name "ProductCode"
    $actualUpgradeCode = Get-MsiPropertyValue -Database $database -Name "UpgradeCode"
    $actualVersion = Get-MsiPropertyValue -Database $database -Name "ProductVersion"

    if ($actualProductCode -ne $Payload.ProductCode -or
        $actualUpgradeCode -ne $Payload.UpgradeCode -or
        $actualVersion -ne $Payload.Version) {
        throw "Identidade MSI inesperada em '$Path'. ProductCode=$actualProductCode; UpgradeCode=$actualUpgradeCode; ProductVersion=$actualVersion."
    }
}

function Get-MsiLicenseRtf {
    param([Parameter(Mandatory)] [string]$Path)
    $database = Get-MsiDatabase -Path $Path
    $view = $database.OpenView(
        "SELECT ``Text`` FROM ``Control`` WHERE ``Dialog_``='LicenseAgreementDlg' AND ``Control``='Memo'"
    )
    $null = $view.Execute()
    $record = $view.Fetch()
    if ($null -eq $record) {
        throw "O texto integral da licença não foi encontrado na tabela Control de '$Path'."
    }
    $rtf = $record.StringData(1)
    if ([string]::IsNullOrWhiteSpace($rtf) -or -not $rtf.TrimStart().StartsWith('{\rtf')) {
        throw "O conteúdo da licença em '$Path' não é um RTF válido."
    }
    return $rtf
}

function New-CombinedLicenseRtf {
    param(
        [Parameter(Mandatory)] [string]$OdbcMsi,
        [Parameter(Mandatory)] [string]$BcpMsi,
        [Parameter(Mandatory)] [string]$OutputPath
    )

    Add-Type -AssemblyName System.Windows.Forms
    $odbcReader = New-Object System.Windows.Forms.RichTextBox
    $bcpReader = New-Object System.Windows.Forms.RichTextBox
    $writer = New-Object System.Windows.Forms.RichTextBox
    $validator = New-Object System.Windows.Forms.RichTextBox
    try {
        $odbcRtf = [string](Get-MsiLicenseRtf -Path $OdbcMsi)
        $bcpRtf = [string](Get-MsiLicenseRtf -Path $BcpMsi)
        $odbcReader.Rtf = $odbcRtf
        $bcpReader.Rtf = $bcpRtf

        $odbcText = $odbcReader.Text
        $bcpText = $bcpReader.Text
        if ($odbcText.Length -lt 1000 -or $bcpText.Length -lt 1000) {
            throw "Um dos termos de licença extraídos é inesperadamente curto."
        }

        $writer.Text = @"
TERMOS DE LICENÇA DOS COMPONENTES MICROSOFT INCLUÍDOS

Os textos abaixo foram extraídos integralmente dos respectivos pacotes MSI oficiais incorporados a este instalador.

MICROSOFT ODBC DRIVER 18 FOR SQL SERVER ($($odbcPayload.Version))

$odbcText


MICROSOFT COMMAND LINE UTILITIES 17 FOR SQL SERVER ($($bcpPayload.Version))

$bcpText
"@
        # RichTextBox.SaveFile can silently create an empty file in some
        # non-interactive PowerShell hosts. Persist the validated RTF property
        # directly; RichTextBox serializes non-ASCII characters as RTF escapes.
        [IO.File]::WriteAllText($OutputPath, $writer.Rtf, [Text.Encoding]::ASCII)
        $validator.Rtf = [IO.File]::ReadAllText($OutputPath, [Text.Encoding]::ASCII)
        if (-not $validator.Text.Contains($odbcText) -or -not $validator.Text.Contains($bcpText)) {
            throw "A validação da licença combinada falhou; o RTF não contém integralmente os dois textos de origem."
        }
    }
    finally {
        $odbcReader.Dispose()
        $bcpReader.Dispose()
        $writer.Dispose()
        $validator.Dispose()
    }
}

function Get-AuthenticodeMetadata {
    param([Parameter(Mandatory)] [string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    return [ordered]@{
        status = [string]$signature.Status
        signer = if ($null -ne $signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
        timestampSigner = if ($null -ne $signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { $null }
    }
}

function Find-SignTool {
    param([string]$RequestedPath)
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "signtool.exe não foi encontrado em '$RequestedPath'."
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kitsRoot -PathType Container) {
        $candidate = Get-ChildItem -LiteralPath $kitsRoot -Filter signtool.exe -File -Recurse |
            Where-Object { $_.DirectoryName -match '\\x64$' } |
            Sort-Object { [version]($_.Directory.Parent.Name -replace '[^0-9\.]', '') } -Descending |
            Select-Object -First 1
        if ($null -ne $candidate) {
            return $candidate.FullName
        }
    }
    throw "signtool.exe não foi encontrado. Instale o Windows SDK ou informe -SignToolPath."
}

function Invoke-CodeSigning {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Thumbprint,
        [Parameter(Mandatory)] [string]$ToolPath
    )

    $normalizedThumbprint = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    $storeArguments = @()
    $currentUserCertificate = Get-ChildItem -LiteralPath "Cert:\CurrentUser\My\$normalizedThumbprint" -ErrorAction SilentlyContinue
    $localMachineCertificate = Get-ChildItem -LiteralPath "Cert:\LocalMachine\My\$normalizedThumbprint" -ErrorAction SilentlyContinue
    if ($null -eq $currentUserCertificate -and $null -eq $localMachineCertificate) {
        throw "Certificado de assinatura '$normalizedThumbprint' não encontrado em CurrentUser\My nem LocalMachine\My."
    }
    if ($null -eq $currentUserCertificate) {
        $storeArguments += "/sm"
    }

    $arguments = @(
        "sign"
        "/sha1", $normalizedThumbprint
        "/fd", "SHA256"
        "/tr", $TimestampUrl
        "/td", "SHA256"
    ) + $storeArguments + @($Path)

    & $ToolPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao assinar '$Path' (signtool exit code $LASTEXITCODE)."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "A assinatura produzida para '$Path' não foi validada: $($signature.StatusMessage)"
    }
}

function Publish-ArtifactAtomically {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Destination
    )
    $temporary = "$Destination.publishing"
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
    Copy-Item -LiteralPath $Source -Destination $temporary
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        # Windows PowerShell 5.1 can coerce a PowerShell $null passed to
        # File.Replace into an empty backup path, which raises
        # "The path is not of a legal form".  A real, unique backup path
        # preserves the atomic same-volume replacement semantics on every
        # supported build host.  It is removed only after replacement has
        # succeeded; on failure it deliberately remains recoverable.
        $backup = "$Destination.backup-$([guid]::NewGuid().ToString('N'))"
        [IO.File]::Replace($temporary, $Destination, $backup, $true)
        Remove-Item -LiteralPath $backup -Force
    }
    else {
        Move-Item -LiteralPath $temporary -Destination $Destination
    }
}

function Invoke-NativeTool {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string[]]$Arguments,
        [Parameter(Mandatory)] [string]$Description
    )
    Write-Host $Description
    & $Path @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description falhou (exit code $LASTEXITCODE)."
    }
}

Assert-WindowsBuildHost
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Path $releaseRoot, $redistRoot, $cacheRoot, $buildRoot -Force | Out-Null

if ($RebuildExecutables) {
    & (Join-Path $PSScriptRoot "build_executables.ps1") -Python $Python -OutputDirectory "release"
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao recompilar os executáveis da aplicação."
    }
}

$guiExecutable = Join-Path $releaseRoot "BulkFlowGUI.exe"
$cliExecutable = Join-Path $releaseRoot "BulkFlowCLI.exe"
foreach ($path in @($guiExecutable, $cliExecutable)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Payload da aplicação ausente: '$path'. Execute packaging\build_executables.ps1 ou use -RebuildExecutables."
    }
}

$odbcMsi = Join-Path $redistRoot $odbcPayload.FileName
$bcpMsi = Join-Path $redistRoot $bcpPayload.FileName
$wixArchive = Join-Path $cacheRoot $wixPayload.FileName

Get-OrDownloadPayload -Destination $odbcMsi -Url $odbcPayload.Url -Sha256 $odbcPayload.Sha256
Get-OrDownloadPayload -Destination $bcpMsi -Url $bcpPayload.Url -Sha256 $bcpPayload.Sha256
Get-OrDownloadPayload -Destination $wixArchive -Url $wixPayload.Url -Sha256 $wixPayload.Sha256

$odbcSignature = Assert-MicrosoftSignature -Path $odbcMsi
$bcpSignature = Assert-MicrosoftSignature -Path $bcpMsi
Assert-MsiIdentity -Path $odbcMsi -Payload $odbcPayload
Assert-MsiIdentity -Path $bcpMsi -Payload $bcpPayload

# Extract the compiler from the verified archive on every build. This avoids
# trusting mutable files left in an earlier tool directory.
if (Test-Path -LiteralPath $toolRoot -PathType Container) {
    $resolvedBuildRoot = [IO.Path]::GetFullPath($buildRoot).TrimEnd('\') + '\'
    $resolvedToolRoot = [IO.Path]::GetFullPath($toolRoot).TrimEnd('\') + '\'
    if (-not $resolvedToolRoot.StartsWith($resolvedBuildRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Diretório de ferramentas fora da raiz de build: '$resolvedToolRoot'."
    }
    Remove-Item -LiteralPath $toolRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
Expand-Archive -LiteralPath $wixArchive -DestinationPath $toolRoot -Force

$candle = Join-Path $toolRoot "candle.exe"
$light = Join-Path $toolRoot "light.exe"
$insignia = Join-Path $toolRoot "insignia.exe"
foreach ($tool in @($candle, $light, $insignia)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Ferramenta WiX esperada não encontrada após extrair o arquivo verificado: '$tool'."
    }
}

$signTool = $null
if (-not [string]::IsNullOrWhiteSpace($SigningCertificateThumbprint)) {
    $signTool = Find-SignTool -RequestedPath $SignToolPath
    Invoke-CodeSigning -Path $guiExecutable -Thumbprint $SigningCertificateThumbprint -ToolPath $signTool
    Invoke-CodeSigning -Path $cliExecutable -Thumbprint $SigningCertificateThumbprint -ToolPath $signTool
}
else {
    Write-Warning "Build sem certificado: os executáveis próprios, o MSI próprio e Setup-BulkFlow.exe não terão assinatura Authenticode."
}

$licenseRtf = Join-Path $buildRoot "Microsoft-License-Terms.rtf"
New-CombinedLicenseRtf -OdbcMsi $odbcMsi -BcpMsi $bcpMsi -OutputPath $licenseRtf

$thirdPartyNotices = Join-Path $buildRoot "THIRD-PARTY-NOTICES.txt"
$thirdPartyText = @"
AVISOS DE COMPONENTES DE TERCEIROS

Este pacote offline incorpora, sem modificação:

- Microsoft ODBC Driver 18 for SQL Server, versão $($odbcPayload.Version)
  Origem: $($odbcPayload.Url)
- Microsoft Command Line Utilities 17 for SQL Server, versão $($bcpPayload.Version)
  Origem: $($bcpPayload.Url)

Os termos integrais fornecidos nos pacotes MSI oficiais são exibidos pelo instalador antes da instalação interativa. Os arquivos originais são validados por SHA-256 e assinatura Authenticode da Microsoft antes da montagem do pacote.

Referências oficiais:
- https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
- https://learn.microsoft.com/sql/connect/odbc/windows/system-requirements-installation-and-driver-files
- https://learn.microsoft.com/sql/tools/bcp/bcp-download-install
"@
Write-Utf8File -Path $thirdPartyNotices -Content $thirdPartyText

$payloadManifest = Join-Path $buildRoot "payload-manifest.json"
$manifest = [ordered]@{
    schemaVersion = 1
    application = [ordered]@{
        name = "BulkFlow"
        version = $Version
        productCode = $appProductCode
        upgradeCode = $appUpgradeCode
        bundleUpgradeCode = $bundleUpgradeCode
        architecture = "x64"
    }
    toolchain = [ordered]@{
        wixVersion = "3.14.1.8722"
        archive = $wixPayload.FileName
        sha256 = $wixPayload.Sha256
        sourceUrl = $wixPayload.Url
    }
    payloads = @(
        [ordered]@{
            fileName = "BulkFlowGUI.exe"
            role = "application-gui"
            sizeBytes = (Get-Item -LiteralPath $guiExecutable).Length
            sha256 = Get-Sha256 -Path $guiExecutable
            authenticode = Get-AuthenticodeMetadata -Path $guiExecutable
        },
        [ordered]@{
            fileName = "BulkFlowCLI.exe"
            role = "application-cli"
            sizeBytes = (Get-Item -LiteralPath $cliExecutable).Length
            sha256 = Get-Sha256 -Path $cliExecutable
            authenticode = Get-AuthenticodeMetadata -Path $cliExecutable
        },
        [ordered]@{
            fileName = $odbcPayload.FileName
            role = "microsoft-odbc-driver"
            version = $odbcPayload.Version
            productCode = $odbcPayload.ProductCode
            upgradeCode = $odbcPayload.UpgradeCode
            sizeBytes = (Get-Item -LiteralPath $odbcMsi).Length
            sha256 = Get-Sha256 -Path $odbcMsi
            sourceUrl = $odbcPayload.Url
            authenticode = [ordered]@{
                status = [string]$odbcSignature.Status
                signer = $odbcSignature.SignerCertificate.Subject
                timestampSigner = $odbcSignature.TimeStamperCertificate.Subject
            }
        },
        [ordered]@{
            fileName = $bcpPayload.FileName
            role = "microsoft-bcp-command-line-utilities"
            version = $bcpPayload.Version
            productCode = $bcpPayload.ProductCode
            upgradeCode = $bcpPayload.UpgradeCode
            sizeBytes = (Get-Item -LiteralPath $bcpMsi).Length
            sha256 = Get-Sha256 -Path $bcpMsi
            sourceUrl = $bcpPayload.Url
            authenticode = [ordered]@{
                status = [string]$bcpSignature.Status
                signer = $bcpSignature.SignerCertificate.Subject
                timestampSigner = $bcpSignature.TimeStamperCertificate.Subject
            }
        }
    )
}
Write-Utf8File -Path $payloadManifest -Content ($manifest | ConvertTo-Json -Depth 8)

$productObject = Join-Path $buildRoot "Product.wixobj"
$bundleObject = Join-Path $buildRoot "Bundle.wixobj"
$stagingRoot = Join-Path $buildRoot "out"
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
$applicationMsi = Join-Path $stagingRoot "BulkFlow.msi"
$bundleExecutable = Join-Path $stagingRoot "Setup-BulkFlow.exe"
$publishedApplicationMsi = Join-Path $releaseRoot "BulkFlow.msi"
$publishedBundleExecutable = Join-Path $releaseRoot "Setup-BulkFlow.exe"

foreach ($output in @($productObject, $bundleObject, $applicationMsi, $bundleExecutable)) {
    if (Test-Path -LiteralPath $output -PathType Leaf) {
        Remove-Item -LiteralPath $output -Force
    }
}

$productSource = Join-Path $wixSourceRoot "Product.wxs"
$bundleSource = Join-Path $wixSourceRoot "Bundle.wxs"
$localizationFile = Join-Path $wixSourceRoot "Bundle.pt-BR.wxl"

Invoke-NativeTool -Path $candle -Description "Compilando o MSI da aplicação" -Arguments @(
    "-nologo", "-arch", "x64",
    "-dAppVersion=$Version",
    "-dAppProductCode=$appProductCode",
    "-dProjectRoot=$projectRoot",
    "-dReleaseRoot=$releaseRoot",
    "-dPayloadManifest=$payloadManifest",
    "-dThirdPartyNotices=$thirdPartyNotices",
    "-dLicenseRtfPath=$licenseRtf",
    "-out", $productObject,
    $productSource
)

Invoke-NativeTool -Path $light -Description "Vinculando o MSI da aplicação" -Arguments @(
    "-nologo", "-spdb",
    "-out", $applicationMsi,
    $productObject
)

$applicationDatabase = Get-MsiDatabase -Path $applicationMsi
if ((Get-MsiPropertyValue -Database $applicationDatabase -Name "ProductCode") -ne $appProductCode -or
    (Get-MsiPropertyValue -Database $applicationDatabase -Name "UpgradeCode") -ne $appUpgradeCode -or
    (Get-MsiPropertyValue -Database $applicationDatabase -Name "ProductVersion") -ne $Version) {
    throw "A identidade do MSI da aplicação compilado não corresponde aos valores esperados."
}

if ($null -ne $signTool) {
    Invoke-CodeSigning -Path $applicationMsi -Thumbprint $SigningCertificateThumbprint -ToolPath $signTool
}

Invoke-NativeTool -Path $candle -Description "Compilando o bundle offline" -Arguments @(
    "-nologo", "-arch", "x64",
    "-ext", (Join-Path $toolRoot "WixBalExtension.dll"),
    "-ext", (Join-Path $toolRoot "WixUtilExtension.dll"),
    "-dBundleVersion=$bundleVersion",
    "-dAppProductCode=$appProductCode",
    "-dOdbcMsiPath=$odbcMsi",
    "-dBcpMsiPath=$bcpMsi",
    "-dAppMsiPath=$applicationMsi",
    "-dLicenseRtfPath=$licenseRtf",
    "-dLocalizationFile=$localizationFile",
    "-out", $bundleObject,
    $bundleSource
)

Invoke-NativeTool -Path $light -Description "Vinculando Setup-BulkFlow.exe" -Arguments @(
    "-nologo", "-spdb",
    "-ext", (Join-Path $toolRoot "WixBalExtension.dll"),
    "-ext", (Join-Path $toolRoot "WixUtilExtension.dll"),
    "-out", $bundleExecutable,
    $bundleObject
)

if ($null -ne $signTool) {
    $detachedEngine = Join-Path $buildRoot "Setup-BulkFlow-engine.exe"
    $reattachedBundle = Join-Path $buildRoot "Setup-BulkFlow-reattached.exe"
    if (Test-Path -LiteralPath $detachedEngine) {
        Remove-Item -LiteralPath $detachedEngine -Force
    }
    if (Test-Path -LiteralPath $reattachedBundle) {
        Remove-Item -LiteralPath $reattachedBundle -Force
    }
    Invoke-NativeTool -Path $insignia -Description "Extraindo o mecanismo Burn para assinatura" -Arguments @(
        "-nologo", "-ib", $bundleExecutable, "-out", $detachedEngine
    )
    Invoke-CodeSigning -Path $detachedEngine -Thumbprint $SigningCertificateThumbprint -ToolPath $signTool
    Invoke-NativeTool -Path $insignia -Description "Reanexando o mecanismo Burn assinado" -Arguments @(
        "-nologo", "-ab", $detachedEngine, $bundleExecutable, "-out", $reattachedBundle
    )
    Move-Item -LiteralPath $reattachedBundle -Destination $bundleExecutable -Force
    Invoke-CodeSigning -Path $bundleExecutable -Thumbprint $SigningCertificateThumbprint -ToolPath $signTool
}

foreach ($artifact in @($applicationMsi, $bundleExecutable)) {
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf) -or (Get-Item -LiteralPath $artifact).Length -eq 0) {
        throw "Artefato final inválido ou ausente: '$artifact'."
    }
}

Publish-ArtifactAtomically -Source $applicationMsi -Destination $publishedApplicationMsi
Publish-ArtifactAtomically -Source $bundleExecutable -Destination $publishedBundleExecutable

$publishedBundleHash = Get-Sha256 -Path $publishedBundleExecutable
$checksumFile = Join-Path $releaseRoot "Setup-BulkFlow.exe.sha256"
$checksumStaging = Join-Path $buildRoot "Setup-BulkFlow.exe.sha256"
Write-Utf8File -Path $checksumStaging -Content "$publishedBundleHash *Setup-BulkFlow.exe`r`n"
Publish-ArtifactAtomically -Source $checksumStaging -Destination $checksumFile

Write-Host ""
Write-Host "Instalador offline gerado com sucesso:"
Write-Host "  $publishedBundleExecutable"
Write-Host "  SHA-256: $publishedBundleHash"
Write-Host "  $checksumFile"
Write-Host "MSI da aplicação (também incorporado ao bundle):"
Write-Host "  $publishedApplicationMsi"
Write-Host "  SHA-256: $(Get-Sha256 -Path $publishedApplicationMsi)"
Write-Host "Manifesto dos payloads incorporado ao MSI: $payloadManifest"
if ($null -eq $signTool) {
    Write-Warning "Os artefatos próprios estão sem assinatura. Use -SigningCertificateThumbprint em um build de distribuição."
}
