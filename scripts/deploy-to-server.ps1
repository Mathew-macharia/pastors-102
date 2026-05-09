<#
.SYNOPSIS
  Stage pastors-102-v2 (excluding heavy/sensitive noise), then scp to your VPS.

.PARAMETER ServerHost
  VPS IPv4 or DNS (e.g. 178.105.92.47).

.PARAMETER User
  SSH login (Hetzner Ubuntu image often uses "root").

.PARAMETER RemotePath
  Absolute path on the server parent directory; project lands in RemotePath/pastors-102-v2

.PARAMETER IdentityFile
  Path to private key (e.g. $env:USERPROFILE\.ssh\id_ed25519). If omitted, ssh uses default agent/keys.

.NOTES
  - Does NOT delete your local repo.
  - Copies .git, source, configs, *.example — still copy real .env and wallets/private yourself if not present locally.
  - Excludes: **\target, **\.venv, **\__pycache__, .cursor, mcps, Thumbs.db, agent-tools, assets (editor noise).
  - Requires OpenSSH client (scp) and SSH key auth to the server (BatchMode: no password prompt).
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $ServerHost,

    [string] $User = "root",

    [string] $RemotePath = "/root",

    [string] $IdentityFile = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pastors-102-deploy-" + [Guid]::NewGuid().ToString("N"))
$stagingProject = Join-Path $stagingRoot "pastors-102-v2"

Write-Host "Repo:       $repoRoot"
Write-Host "Staging:    $stagingProject"
Write-Host "Target:     ${User}@${ServerHost}:${RemotePath}/pastors-102-v2"
Write-Host ""

New-Item -ItemType Directory -Path $stagingProject -Force | Out-Null

# /E all subdirs; /XD excludes any directory with these names in the tree
$robolog = Join-Path $env:TEMP "robocopy-deploy.log"
robocopy $repoRoot $stagingProject /E `
    /XD target .venv __pycache__ .cursor node_modules agent-tools assets .idea `
    /XF *.pyc *.pyo Thumbs.db /NFL /NDL /NJH /NJS | Out-File $robolog
# robocopy: 0-7 = success (with different meanings); 8+ = failure
if ($LASTEXITCODE -ge 8) {
    Get-Content $robolog
    throw "robocopy failed with exit code $LASTEXITCODE"
}

# robocopy: 0-7 = success; 1 = copied files; 2 = extra dirs/files only; see `robocopy /?`
Write-Host "robocopy finished (exit $LASTEXITCODE). Starting scp (this can take a minute)..."

$remoteSpec = "${User}@${ServerHost}:${RemotePath}/"
$scpArgs = @("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new")
if ($IdentityFile) {
    $scpArgs += "-i"
    $scpArgs += $IdentityFile
}
$scpArgs += "-r", $stagingProject, $remoteSpec
& scp @scpArgs
if ($LASTEXITCODE -ne 0) {
    Write-Warning "scp failed. Common causes: no SSH key for this host, wrong user, or host key prompt disabled."
    Write-Host "Staging folder left at: $stagingRoot"
    exit $LASTEXITCODE
}

Remove-Item -Recurse -Force $stagingRoot
Write-Host "Done. On the server: cd ${RemotePath}/pastors-102-v2 && ls -la"
