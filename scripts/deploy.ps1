# deploy.ps1 - Despliega la app al servidor intranet (hook auto-deploy) y respaldo en GitHub.
# Uso: powershell -File scripts\deploy.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$sucio = git -C $root status --porcelain
if ($sucio) {
    Write-Host "Hay cambios sin commitear:"
    $sucio
    $resp = Read-Host "¿Commitear todo y desplegar? (s/N)"
    if ($resp -notmatch '^[sS]$') { Write-Host "Deploy cancelado."; exit 1 }
    git -C $root add -A
    git -C $root commit -m "Deploy: $($sucio -join ', ')"
}

git -C $root push origin master
git -C $root push deploy master
Write-Host "Deploy OK: GitHub + servidor (hook post-receive). Revisa /var/log/monitor-post-hana-deploy.log"
