# Run database migrations against the configured DATABASE_URL.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "api\.venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Virtualenv not found at $Python. Run scripts\dev.ps1 first."
}

Push-Location (Join-Path $Root "api")
try {
    & $Python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
