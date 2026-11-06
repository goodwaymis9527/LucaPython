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

function Show-Health {
    param([string]$Label, [string]$Url)
    try {
        # -UseBasicParsing: PS 5.1's Invoke-RestMethod needs the IE engine otherwise it can fail headless.
        # The payload is {ok, db:{driver,region,env,host,database,pool,prepare}, error} - the old code
        # read $r.status / $r.host / $r.database, which do not exist, so it printed empty values.
        $r = Invoke-RestMethod $Url -TimeoutSec 15 -UseBasicParsing
        Write-Host ('  {0}: ok={1} env={2} host={3} db={4} prepare={5}' -f `
            $Label, $r.ok, $r.db.env, $r.db.host, $r.db.database, $r.db.prepare)
        if ($r.error) { Write-Host ('    error: ' + $r.error) }
    } catch {
        Write-Host ('  {0}: not reachable - {1}' -f $Label, $_.Exception.Message)
    }
}

function Health {
    Line
    Write-Host 'Health check'
    $cfg = $null
    if (Test-Path .aws-state/config.json) {
        $cfg = Get-Content .aws-state/config.json -Raw | ConvertFrom-Json
    }
    if ($cfg -and $cfg.albDns) {
        Show-Health 'remote (ALB)' ('http://' + $cfg.albDns + '/api/health')
    } else {
        Write-Host '  remote: no albDns in .aws-state/config.json (run: python aws/bootstrap.py)'
    }
    # 8090 is this project; host port 8000 belongs to another app (ai-service),
    # which also answers /api/health - that is a known false positive, so never probe 8000 here.
    Show-Health 'local container (127.0.0.1:8090)' 'http://127.0.0.1:8090/api/health'
}

function Logs { python aws/status.py --logs 30 }

function Migrate {
    Line
    Write-Host 'Migrate'
    if ($Remote) {
        # exec.py only opens an interactive session (boto3 cannot read its stdout, and this machine
        # has no session-manager-plugin), so run the command as a one-shot task and read CloudWatch.
        python aws/run_once.py 'python scripts/migrate.py' --stop
    } else {
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
