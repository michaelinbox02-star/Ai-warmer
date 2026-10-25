# Start the isolated local stack: PostgreSQL, API, worker, and web console.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1
#
# The first run builds the web console and creates .env if it is missing.
# Logs and PIDs live under var\. Open http://localhost:3000 when it finishes.

param(
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Api = Join-Path $Root "api"
$Web = Join-Path $Root "web"
$Python = Join-Path $Api ".venv\Scripts\python.exe"
$Var = Join-Path $Root "var"
$Logs = Join-Path $Var "logs"
$Run = Join-Path $Var "run"

function Test-Listening([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        return $connection.Wait(500) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Wait-Http([string]$Url, [int]$Seconds = 45) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -lt 500) { return $true }
        }
        catch {
            # Not ready yet.
        }
        Start-Sleep -Milliseconds 750
    } while ((Get-Date) -lt $deadline)
    return $false
}

if (-not (Test-Path $Python)) {
    throw "API virtualenv missing. Run scripts\dev.ps1 first."
}

New-Item -ItemType Directory -Force $Logs, $Run | Out-Null

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Host "No .env found; creating the local database and secrets..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "local-db.ps1") start
    & $Python (Join-Path $PSScriptRoot "local_setup.py")
    if ($LASTEXITCODE -ne 0) { throw "local_setup.py failed" }
}

if (-not (Test-Listening 55432)) {
    & (Join-Path $PSScriptRoot "local-db.ps1") start
}

Write-Host "Applying database migrations ..." -ForegroundColor Cyan
Push-Location $Api
try {
    & $Python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed" }
}
finally {
    Pop-Location
}

if (-not (Test-Listening 8000)) {
    Write-Host "Starting API on 127.0.0.1:8000 ..." -ForegroundColor Cyan
    $apiProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $Api `
        -RedirectStandardOutput (Join-Path $Logs "api.out.log") `
        -RedirectStandardError (Join-Path $Logs "api.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $Run "api.pid") -Value $apiProcess.Id
}

if (-not (Test-Path (Join-Path $Run "worker.pid")) -or
    -not (Get-Process -Id (Get-Content (Join-Path $Run "worker.pid")) -ErrorAction SilentlyContinue)) {
    Write-Host "Starting sender worker ..." -ForegroundColor Cyan
    $workerProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "app.worker") `
        -WorkingDirectory $Api `
        -RedirectStandardOutput (Join-Path $Logs "worker.out.log") `
        -RedirectStandardError (Join-Path $Logs "worker.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $Run "worker.pid") -Value $workerProcess.Id
}

if (-not $SkipWeb) {
    if (-not (Test-Path (Join-Path $Web ".next\BUILD_ID"))) {
        Write-Host "Building the web console (first run only) ..." -ForegroundColor Cyan
        Push-Location $Web
        try {
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw "Web build failed" }
        }
        finally {
            Pop-Location
        }
    }
    if (-not (Test-Listening 3000)) {
        Write-Host "Starting web console on 127.0.0.1:3000 ..." -ForegroundColor Cyan
        $webLog = Join-Path $Logs "web.out.log"
        $webErr = Join-Path $Logs "web.err.log"
        $command = "npm run start 1>`"$webLog`" 2>`"$webErr`""
        $webProcess = Start-Process -FilePath "cmd.exe" `
            -ArgumentList @("/c", $command) `
            -WorkingDirectory $Web `
            -WindowStyle Hidden -PassThru
        Set-Content -Path (Join-Path $Run "web.pid") -Value $webProcess.Id
    }
}

$apiReady = Wait-Http "http://127.0.0.1:8000/readyz" 45
$webReady = $SkipWeb -or (Wait-Http "http://127.0.0.1:3000/login" 45)

Write-Host ""
if ($apiReady) {
    Write-Host "API ready:  http://127.0.0.1:8000/readyz" -ForegroundColor Green
}
else {
    Write-Host "API did not become ready. See var\logs\api.err.log" -ForegroundColor Red
}
if (-not $SkipWeb) {
    if ($webReady) {
        Write-Host "Console:    http://localhost:3000" -ForegroundColor Green
    }
    else {
        Write-Host "Web did not become ready. See var\logs\web.err.log" -ForegroundColor Red
    }
}

$Login = Join-Path $Var "LOCAL_LOGIN.txt"
if (Test-Path $Login) {
    Write-Host ""
    Write-Host "Local operator credentials:" -ForegroundColor Cyan
    Get-Content $Login | ForEach-Object { Write-Host "  $_" }
}

Write-Host ""
Write-Host "Logs: var\logs   Stop: scripts\stop-local.ps1"
