param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$Ca,
    [Parameter(Mandatory = $true)][string]$AdbDir,
    [Parameter(Mandatory = $true)][string]$Output,
    [string]$Version = "0.1.0",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Package version must contain three numeric fields."
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeviceAgentRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$SpecPath = Join-Path $DeviceAgentRoot "packaging\common\perfpilot-agent.spec"
$Validator = Join-Path $DeviceAgentRoot "packaging\common\validate_bootstrap.py"
$ServiceSource = Join-Path $ScriptDir "service.py"
$WixSource = Join-Path $ScriptDir "PerfPilotAgent.wxs"
$Uninstaller = Join-Path $ScriptDir "uninstall.ps1"

foreach ($Path in @($Config, $Ca, (Join-Path $AdbDir "adb.exe"), (Join-Path $AdbDir "AdbWinApi.dll"), (Join-Path $AdbDir "AdbWinUsbApi.dll"))) {
    if (-not [IO.Path]::IsPathFullyQualified($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Package input is missing or is not an absolute file path."
    }
}
$Output = [IO.Path]::GetFullPath($Output)
$OutputDirectory = Split-Path -Parent $Output
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$BuildRoot = Join-Path ([IO.Path]::GetTempPath()) ("perfpilot-agent-windows-" + [guid]::NewGuid().ToString("N"))
[IO.Directory]::CreateDirectory($BuildRoot) | Out-Null

try {
    & $Python $Validator --platform windows --config $Config --ca $Ca
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap validation failed." }

    $DistPath = Join-Path $BuildRoot "dist"
    & $Python -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath (Join-Path $BuildRoot "agent-work") $SpecPath
    if ($LASTEXITCODE -ne 0) { throw "Agent executable build failed." }
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name perfpilot-agent-service `
        --distpath $DistPath `
        --workpath (Join-Path $BuildRoot "service-work") `
        --paths (Join-Path $DeviceAgentRoot "src") `
        $ServiceSource
    if ($LASTEXITCODE -ne 0) { throw "Service executable build failed." }

    $Payload = Join-Path $BuildRoot "payload"
    $Bin = Join-Path $Payload "bin"
    $ConfigDirectory = Join-Path $Payload "config"
    $PlatformTools = Join-Path $Payload "platform-tools"
    foreach ($Directory in @($Bin, $ConfigDirectory, $PlatformTools)) {
        [IO.Directory]::CreateDirectory($Directory) | Out-Null
    }
    Copy-Item -LiteralPath (Join-Path $DistPath "perfpilot-agent.exe") -Destination $Bin
    Copy-Item -LiteralPath (Join-Path $DistPath "perfpilot-agent-service.exe") -Destination $Bin
    Copy-Item -LiteralPath $Uninstaller -Destination $Bin
    Copy-Item -LiteralPath $Config -Destination (Join-Path $ConfigDirectory "config.json")
    Copy-Item -LiteralPath $Ca -Destination (Join-Path $ConfigDirectory "perfpilot-ca.crt")
    foreach ($File in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")) {
        Copy-Item -LiteralPath (Join-Path $AdbDir $File) -Destination $PlatformTools
    }

    & wix build `
        -arch x64 `
        -d "PayloadDir=$Payload" `
        -d "ProductVersion=$Version" `
        -o $Output `
        $WixSource
    if ($LASTEXITCODE -ne 0) { throw "MSI build failed." }
}
finally {
    if (Test-Path -LiteralPath $BuildRoot) {
        Remove-Item -LiteralPath $BuildRoot -Recurse -Force
    }
}
