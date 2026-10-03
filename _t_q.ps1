$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cmd = @'
set -e
cd /root/akali
cp -n .env .env.bak-20261003c 2>/dev/null || true
echo '=== .env quota lines (limits, not secrets) ==='
grep -E '^FX_QUOTA_(FREE|PAID|WHITELIST)=' .env
docker compose up -d --force-recreate akali >/dev/null 2>&1
sleep 8
echo '=== container injected env (should match .env) ==='
docker compose exec -T akali env | grep -E 'FX_QUOTA_(FREE|PAID|WHITELIST)='
echo '=== ps ==='
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
echo '=== auth ==='
curl -s -m 10 http://127.0.0.1/api/auth/status
echo
echo '=== DONE ==='
'@

$cmd = $cmd -replace "`r`n", "`n" -replace "`r", "`n"
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($cmd))
$remote = 'echo ' + $b64 + ' | base64 -d | bash'
& ssh -o BatchMode=yes -o ConnectTimeout=15 root@43.142.42.138 $remote 2>&1
Write-Host ('SSH_EXIT=' + $LASTEXITCODE)
