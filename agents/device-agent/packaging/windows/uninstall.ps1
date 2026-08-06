param([switch]$RemoveData)

$ErrorActionPreference = "Stop"
$Service = Get-Service -Name "PerfPilotAgent" -ErrorAction SilentlyContinue
if ($null -ne $Service) {
    if ($Service.Status -ne "Stopped") {
        Stop-Service -Name "PerfPilotAgent" -Force -ErrorAction SilentlyContinue
    }
    & sc.exe delete "PerfPilotAgent" | Out-Null
}
if ($RemoveData) {
    $StateDirectory = Join-Path $env:ProgramData "PerfPilot\Agent"
    if (Test-Path -LiteralPath $StateDirectory) {
        Remove-Item -LiteralPath $StateDirectory -Recurse -Force
    }
}
