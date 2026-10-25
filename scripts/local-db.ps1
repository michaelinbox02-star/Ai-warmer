# Start/stop the isolated PostgreSQL 16 server used by the local console.
#
#   powershell -ExecutionPolicy Bypass -File scripts\local-db.ps1 start
#   powershell -ExecutionPolicy Bypass -File scripts\local-db.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts\local-db.ps1 stop
#
# It uses the PostgreSQL binaries bundled with the `pgserver` Python package in
# api\.venv, listens only on 127.0.0.1:55432, and trusts local connections.
# This is for local use only; use docker-compose for a real deployment.

param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Bin = Join-Path $Root "api\.venv\Lib\site-packages\pgserver\pginstall\bin"
$Data = Join-Path $Root "var\pgdata"
$Log = Join-Path $Root "var\postgres.log"
$Port = 55432
$PgCtl = Join-Path $Bin "pg_ctl.exe"
$PgIsReady = Join-Path $Bin "pg_isready.exe"
$InitDb = Join-Path $Bin "initdb.exe"

if (-not (Test-Path $PgCtl)) {
    throw "PostgreSQL binaries not found. Run: cd api; .\.venv\Scripts\python.exe -m pip install pgserver"
}

function Test-Ready {
    & $PgIsReady -h 127.0.0.1 -p $Port *> $null
    return $LASTEXITCODE -eq 0
}

switch ($Action) {
    "status" {
        if (Test-Ready) {
            Write-Host "PostgreSQL is running on 127.0.0.1:$Port" -ForegroundColor Green
            exit 0
        }
        Write-Host "PostgreSQL is not running on 127.0.0.1:$Port" -ForegroundColor Yellow
        exit 1
    }
    "stop" {
        if (-not (Test-Ready)) {
            Write-Host "PostgreSQL is already stopped."
            exit 0
        }
        & $PgCtl -D $Data stop -m fast
        exit $LASTEXITCODE
    }
    "start" {
        if (Test-Ready) {
            Write-Host "PostgreSQL is already running on 127.0.0.1:$Port" -ForegroundColor Green
            exit 0
        }

        if (-not (Test-Path (Join-Path $Data "PG_VERSION"))) {
            Write-Host "Initializing PostgreSQL data directory at $Data ..." -ForegroundColor Cyan
            New-Item -ItemType Directory -Force (Split-Path -Parent $Data) | Out-Null
            & $InitDb -D $Data -U postgres -A trust --encoding=UTF8 --no-locale
            if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
            Add-Content -Path (Join-Path $Data "postgresql.conf") -Value @"

# Local console: fixed loopback port
port = $Port
listen_addresses = '127.0.0.1'
"@
        }

        Write-Host "Starting PostgreSQL on 127.0.0.1:$Port ..." -ForegroundColor Cyan
        & $PgCtl -D $Data -l $Log start
        if ($LASTEXITCODE -ne 0) {
            Write-Host "See $Log for details." -ForegroundColor Yellow
            exit $LASTEXITCODE
        }
        Start-Sleep -Seconds 1
        if (Test-Ready) {
            Write-Host "PostgreSQL is ready." -ForegroundColor Green
        }
        else {
            throw "PostgreSQL did not become ready; see $Log"
        }
    }
}
