# Creates %USERPROFILE%\.ssh\id_ed25519 + .pub for Hetzner SSH login (empty passphrase).
# Run:  powershell -ExecutionPolicy Bypass -File .\scripts\generate-ssh-key-for-hetzner.ps1
# If keys already exist, does nothing (no overwrite).

$ErrorActionPreference = "Stop"
$sshDir = Join-Path $env:USERPROFILE ".ssh"
$priv = Join-Path $sshDir "id_ed25519"
$pub = Join-Path $sshDir "id_ed25519.pub"

if (Test-Path $priv) {
    Write-Host "Key already exists (not overwriting): $priv"
    Write-Host "Public key to paste in Hetzner:"
    Get-Content $pub
    exit 0
}

New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
ssh-keygen -t ed25519 -f $priv -N [string]::Empty -C "hetzner-pastors-102-v2"

Write-Host ""
Write-Host "Created:"
Write-Host "  Private (keep secret): $priv"
Write-Host "  Public (add in Hetzner): $pub"
Write-Host ""
Write-Host "=== Copy everything below into Hetzner -> Security -> SSH keys ==="
Get-Content $pub
Write-Host "==================================================================="
