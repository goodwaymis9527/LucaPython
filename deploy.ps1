<#
    LucaPython deploy helper (PowerShell)

    ASCII-only comments on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI
    unless the file has a BOM, so Chinese comments would be garbled.

    Quick one-liners (paste straight into PowerShell):
      cd D:\goodwaymis9527\LucaPython; docker build -t lucapython:dev .
      docker compose --profile external-db up -d --build   # reads .env for DB_USER / DB_PASSWORD
      .\deploy.ps1 -Step deploy

    Steps:
      preflight  check python/boto3/docker/AWS credentials
      local      run migrate + smoke test against container postgres17
      build      docker build
      push       docker build + push to ECR (needs bootstrap done)
      deploy     build -> push -> register revision -> update service
      health     curl /api/health on the ALB (or on the local container)
      logs       show ECS task logs
      migrate    run scripts/migrate.py (local, or inside the task with -Remote)
      status     print AWS resources from .aws-state/config.json
#>
[CmdletBinding()]
param(
    [ValidateSet('preflight', 'local', 'build', 'push', 'deploy', 'health', 'logs', 'migrate', 'status', 'all')]
    [string]$Step = 'all',
    [switch]$Remote   # use for -migrate: run inside the ECS task instead of locally
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'   # python scripts print ✔/✘; PowerShell console default is cp950
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Line { Write-Host ('-' * 60) }

function Preflight {
    Line
    Write-Host '[1/4] Environment'
    python --version
    python -c "import boto3; print('boto3', boto3.__version__)"
    docker --version
    python aws/status.py 2>&1 | Select-Object -First 12
}

function Load-DotEnv {
    # Read .env (gitignored) so no password is hard-coded in this script.
    $envFile = Join-Path $Root '.env'
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
            }
        }
    }
    if (-not $env:DB_USER) { $env:DB_USER = 'admin' }
    if (-not $env:DB_PASSWORD) { throw 'DB_PASSWORD not found. Create .env (see readme) or set it yourself.' }
}

function LocalUrl {
    # Running from the host: postgres17 is only resolvable inside the docker network,
    # so use localhost (mapped port 5432). Inside a container use DB_HOST=postgres17.
    # Use -f instead of "$env:X:$env:Y" — in a double-quoted string PowerShell eats the colon.
    $pgHost = if ($env:DB_HOST) { $env:DB_HOST } else { 'localhost' }
    'postgresql://{0}:{1}@{2}:5432/awsLucaPysn' -f $env:DB_USER, $env:DB_PASSWORD, $pgHost
}

function LocalTest {
    Line
    Write-Host '[2/4] Local test (needs container postgres17 running)'
    Load-DotEnv
    $env:DATABASE_URL = LocalUrl
    python scripts/migrate.py
    python scripts/smoke_test.py --base=http://127.0.0.1:8090
}

function Build {
    Line
    Write-Host '[3/4] Docker build'
    docker build -t lucapython:dev .
}

function PushImage {
    Line
    Write-Host '[4/4] Push to ECR'
    $uri = python aws/deploy.py --print-ecr-uri
    if (-not $uri) { throw 'Cannot resolve ECR URI. Run: python aws/bootstrap.py' }
    docker build -t $uri:latest .
    docker push $uri:latest
}

function Deploy {
    Line
    Write-Host 'Deploy to ECS (build -> push -> new revision -> update service)'
    python aws/deploy.py
}

function Health {
    Line
    Write-Host 'Health check'
    $cfg = Get-Content .aws-state/config.json -Raw
    if ($cfg -match '"alb_dns_name":\s*"([^"]+)"') {
        $dns = $Matches[1]
        Write-Host "ALB: http://$dns/api/health"
        try {
            $r = Invoke-RestMethod "http://$dns/api/health"
            Write-Host ("  status={0} host={1} db={2} items={3}" -f $r.status, $r.host, $r.database, $r.item_count)
        } catch { Write-Host "  ALB not reachable yet: $($_.Exception.Message)" }
    }
    try {
        $r = Invoke-RestMethod 'http://127.0.0.1:8090/api/health'
        Write-Host ("  local container: status={0} host={1} db={2}" -f $r.status, $r.host, $r.database)
    } catch { Write-Host "  local container not running: $($_.Exception.Message)" }
}

function Logs { python aws/status.py --logs 30 }

function Migrate {
    Line
    Write-Host 'Migrate'
    if ($Remote) { python aws/exec.py 'python scripts/migrate.py' }
    else {
        Load-DotEnv
        $env:DATABASE_URL = LocalUrl
        python scripts/migrate.py
    }
}

switch ($Step) {
    'preflight' { Preflight }
    'local'     { LocalTest }
    'build'     { Build }
    'push'      { PushImage }
    'deploy'    { Deploy }
    'health'    { Health }
    'logs'      { Logs }
    'migrate'   { Migrate }
    'status'    { python aws/status.py }
    'all'       { Preflight; Build; Health }
}
