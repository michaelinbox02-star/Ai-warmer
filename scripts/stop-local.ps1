# Stop the local console processes started by scripts\start-local.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\stop-local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\stop-local.ps1 -KeepDatabase

param(
    [switch]$KeepDatabase
)

$ErrorActionPreference = "SilentlyContinue"

$Root = Split-Path -Parent $PSScriptRoot
$Run = Join-Path $Root "var\run"

foreach ($name in @("web", "worker", "api")) {
    $pidFile = Join-Path $Run "$name.pid"
    if (Test-Path $pidFile) {
        $processId = [int](Get-Content $pidFile)
        Write-Host "Stopping $name (PID $processId) ..."
        & taskkill.exe /PID $processId /T /F *> $null
        Remove-Item $pidFile -Force
    }
}

if (-not $KeepDatabase) {
    & (Join-Path $PSScriptRoot "local-db.ps1") stop
}

Write-Host "Local console stopped."
