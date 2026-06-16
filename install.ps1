# Windows Installer for Claude Code Multi-Provider Proxy
# ----------------------------------------------------

$ErrorActionPreference = "Stop"

Write-Host "=== Starting Claude Code Proxy Setup ===" -ForegroundColor Blue

# Check for uv, install if missing
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv package manager..." -ForegroundColor Blue
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path += ";$env:USERPROFILE\.local\bin"
}

# Install Python 3.14.0 stable
Write-Host "Setting up Python 3.14.0..." -ForegroundColor Blue
try {
    uv python install 3.14.0
} catch {
    Write-Host "Warning: Python 3.14 installation encountered an issue, proceeding with default Python." -ForegroundColor Yellow
}

# Install package
Write-Host "Installing free-claude-code package..." -ForegroundColor Blue
uv tool install --force --editable .

# Setup Directories
Write-Host "Configuring environment templates..." -ForegroundColor Blue
$fccDir = Join-Path $env:USERPROFILE ".fcc"
$profilesDir = Join-Path $fccDir "profiles"
$logsDir = Join-Path $fccDir "logs"

New-Item -ItemType Directory -Force -Path $profilesDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$envPath = Join-Path $fccDir ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item -Path ".env.example" -Destination $envPath -Force
    Write-Host "Created new config at $envPath. Edit this file to add your API keys." -ForegroundColor Green
} else {
    Write-Host "Config file already exists at $envPath (skipping)"
}

Write-Host ""
Write-Host "=== Setup Completed Successfully! ===" -ForegroundColor Green
Write-Host "To start the local proxy server:"
Write-Host "  fcc-server" -ForegroundColor Blue
Write-Host ""
Write-Host "Open the Admin UI dashboard at:"
Write-Host "  http://127.0.0.1:8082/admin" -ForegroundColor Blue
Write-Host ""
Write-Host "To launch Claude Code using this proxy:"
Write-Host "  fcc-claude" -ForegroundColor Blue
Write-Host "----------------------------------------------------"
