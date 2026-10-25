# Generate a complete `.env` for a Docker Compose deployment.
#
#   powershell -ExecutionPolicy Bypass -File scripts\init-env.ps1
#
# The script prompts for the single operator password, derives its PBKDF2 hash,
# and fills every secret in `.env.example`. It never prints the password.
# Re-run with -Force to replace an existing `.env` (the old file is backed up).

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Example = Join-Path $Root ".env.example"
$Target = Join-Path $Root ".env"

if (-not (Test-Path $Example)) {
    throw ".env.example not found at $Example"
}

if ((Test-Path $Target) -and -not $Force) {
    throw ".env already exists. Move it aside or pass -Force."
}

function New-RandomBytes([int]$Length) {
    $buffer = [byte[]]::new($Length)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($buffer)
    }
    finally {
        $rng.Dispose()
    }
    return ,$buffer
}

function ConvertTo-Base64Url([byte[]]$Bytes, [switch]$KeepPadding) {
    $encoded = [Convert]::ToBase64String($Bytes).Replace('+', '-').Replace('/', '_')
    if (-not $KeepPadding) {
        $encoded = $encoded.TrimEnd('=')
    }
    return $encoded
}

function Read-OperatorPassword {
    $secure = Read-Host -AsSecureString "Operator password (at least 12 characters)"
    $confirm = Read-Host -AsSecureString "Confirm operator password"
    $ptr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $ptr2 = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($confirm)
    try {
        $password = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        $again = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr2)
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr2)
    }
    if ($password -ne $again) {
        throw "Passwords do not match."
    }
    if ($password.Length -lt 12) {
        throw "The operator password must be at least 12 characters."
    }
    return $password
}

Write-Host "Generating deployment secrets..." -ForegroundColor Cyan

$password = Read-OperatorPassword
$salt = New-RandomBytes 16
$kdf = [System.Security.Cryptography.Rfc2898DeriveBytes]::new(
    $password,
    $salt,
    600000,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256
)
try {
    $digest = $kdf.GetBytes(32)
}
finally {
    $kdf.Dispose()
}

$adminHash = 'pbkdf2_sha256$600000$' + (ConvertTo-Base64Url $salt) + '$' + (ConvertTo-Base64Url $digest)
$postgresPassword = ConvertTo-Base64Url (New-RandomBytes 32)
$sessionSecret = ConvertTo-Base64Url (New-RandomBytes 48)
$vaultKey = ConvertTo-Base64Url (New-RandomBytes 32) -KeepPadding
$apiToken = ConvertTo-Base64Url (New-RandomBytes 32)
$eventToken = ConvertTo-Base64Url (New-RandomBytes 32)

$values = @{
    "POSTGRES_PASSWORD"  = $postgresPassword
    "DATABASE_URL"       = "postgresql+asyncpg://deliverability:$postgresPassword@localhost:5432/deliverability"
    "ADMIN_PASSWORD_HASH" = $adminHash
    "ADMIN_PASSWORD"     = ""
    "SESSION_SECRET"     = $sessionSecret
    "API_TOKEN"          = $apiToken
    "EVENT_INGEST_TOKEN" = $eventToken
    "VAULT_MASTER_KEY"   = $vaultKey
}

$lines = Get-Content -LiteralPath $Example | ForEach-Object {
    if ($_ -match '^([A-Z0-9_]+)=') {
        $key = $matches[1]
        if ($values.ContainsKey($key)) {
            "$key=$($values[$key])"
        }
        else {
            $_
        }
    }
    else {
        $_
    }
}

if (Test-Path $Target) {
    $backup = "$Target.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    Move-Item -LiteralPath $Target -Destination $backup
    Write-Host "Existing .env moved to $backup" -ForegroundColor Yellow
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($Target, [string[]]$lines, $utf8)

Write-Host ""
Write-Host "Wrote $Target" -ForegroundColor Green
Write-Host "Store the operator password and VAULT_MASTER_KEY in a password manager."
Write-Host "The vault key is required to decrypt stored provider tokens; losing it"
Write-Host "means reconnecting every mailbox."
Write-Host ""
Write-Host "Next: docker compose up -d --build"
Write-Host "Then: http://localhost:3000"
