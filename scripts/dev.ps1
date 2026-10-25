# Install local development dependencies for the API and the web console.
param(
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Api = Join-Path $Root "api"
$Web = Join-Path $Root "web"

Write-Host "Setting up the API virtualenv…" -ForegroundColor Cyan
$Venv = Join-Path $Api ".venv"
if (-not (Test-Path $Venv)) {
    python -m venv $Venv
}
$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --upgrade pip
Push-Location $Api
try {
    & $Python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

if (-not $SkipWeb) {
    Write-Host "Installing web dependencies…" -ForegroundColor Cyan
    Push-Location $Web
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. Copy .env.example to .env and set APP_ENV=local for development."
Write-Host "  2. Provide a PostgreSQL 16 database and DATABASE_URL."
Write-Host "  3. Run scripts\migrate.ps1."
Write-Host "  4. Start the API:  cd api; .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
Write-Host "  5. Start the worker: cd api; .\.venv\Scripts\python.exe -m app.worker"
Write-Host "  6. Start the UI:   cd web; npm run dev"
