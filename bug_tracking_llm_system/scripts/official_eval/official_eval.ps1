<#
.SYNOPSIS
  Run the official SWE-bench harness locally on workspace patches (needs Docker Desktop).

.DESCRIPTION
  Commands (run from anywhere; paths are resolved from this script's location):
    setup               build the runner image (python 3.11 + swebench 5.0.2)
    fetch [-Ids ...]    download dataset rows + official tests into data\official_eval
    pull                docker pull the evaluation image of every fetched instance
    save                docker save runner + evaluation images into data\official_eval\images
    load                docker load every .tar in data\official_eval\images (offline restore)
    gold -InstanceId X  sanity check: the reference patch must be resolved
    run  -Ticket X      evaluate the latest generated patch of workspace ticket X
    run-all             'run' for every fetched instance that has a generated patch

.EXAMPLE
  .\scripts\official_eval\official_eval.ps1 run -Ticket astropy__astropy-7336
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('setup', 'fetch', 'pull', 'save', 'load', 'gold', 'run', 'run-all')]
    [string]$Command,
    [string]$Ticket,
    [string]$InstanceId,
    [string[]]$Ids
)

# Not 'Stop': in Windows PowerShell 5.1 that turns native stderr into terminating errors.
$ErrorActionPreference = 'Continue'

$RunnerImage = 'bugflow/swebench-runner:5.0.2'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path      # bug_tracking_llm_system
$EvalRoot = Join-Path $Root 'data\official_eval'
$ImagesDir = Join-Path $EvalRoot 'images'
$Manifest = Join-Path $EvalRoot 'manifest.json'

function Assert-Docker {
    docker version --format '{{.Server.Version}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker is not running. Start Docker Desktop (Linux containers) and retry.'
    }
}

function Invoke-Runner {
    param([string[]]$PyArgs)
    & docker run --rm `
        -v /var/run/docker.sock:/var/run/docker.sock `
        -v "${Root}:/repo" `
        -w /repo `
        $RunnerImage python @PyArgs | Out-Host   # keep output out of the return value
    return $LASTEXITCODE
}

function Get-ManifestData {
    if (-not (Test-Path $Manifest)) { throw "No $Manifest. Run 'fetch' first." }
    return (Get-Content -Raw -Encoding UTF8 $Manifest | ConvertFrom-Json)
}

function Get-ImageTarName([string]$Image) {
    return (($Image -replace '[/:]', '_') + '.tar')
}

Assert-Docker

switch ($Command) {
    'setup' {
        & docker build -t $RunnerImage -f (Join-Path $PSScriptRoot 'Dockerfile.runner') $PSScriptRoot
        if ($LASTEXITCODE -ne 0) { throw 'docker build failed' }
        Write-Host "Runner image ready: $RunnerImage"
    }
    'fetch' {
        $pyArgs = @('scripts/official_eval/fetch_instances.py')
        if ($Ids) { $pyArgs += '--ids'; $pyArgs += $Ids }
        $code = Invoke-Runner $pyArgs
        if ($code -ne 0) { throw "fetch failed ($code)" }
    }
    'pull' {
        $m = Get-ManifestData
        foreach ($p in $m.images.PSObject.Properties) {
            Write-Host "== $($p.Name): $($p.Value)"
            & docker pull $p.Value
            if ($LASTEXITCODE -ne 0) { Write-Warning "pull failed for $($p.Value)" }
        }
    }
    'save' {
        $m = Get-ManifestData
        New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null
        $all = @($RunnerImage) + @($m.images.PSObject.Properties | ForEach-Object { $_.Value })
        foreach ($img in ($all | Select-Object -Unique)) {
            $tar = Join-Path $ImagesDir (Get-ImageTarName $img)
            Write-Host "== save $img -> $tar"
            & docker save -o $tar $img
            if ($LASTEXITCODE -ne 0) { Write-Warning "save failed for $img" }
        }
    }
    'load' {
        Get-ChildItem -Path $ImagesDir -Filter '*.tar' | ForEach-Object {
            Write-Host "== load $($_.Name)"
            & docker load -i $_.FullName
        }
    }
    'gold' {
        if (-not $InstanceId) { throw 'gold needs -InstanceId' }
        $code = Invoke-Runner @('scripts/official_eval/run_local.py', '--gold', '--instance-id', $InstanceId)
        exit $code
    }
    'run' {
        if (-not $Ticket) { throw 'run needs -Ticket' }
        $code = Invoke-Runner @('scripts/official_eval/run_local.py', '--ticket', $Ticket)
        exit $code
    }
    'run-all' {
        $m = Get-ManifestData
        $failed = 0
        foreach ($id in $m.instance_ids) {
            Write-Host "== $id"
            $code = Invoke-Runner @('scripts/official_eval/run_local.py', '--ticket', $id)
            if ($code -ne 0) { $failed++ }
        }
        Write-Host "Done. Non-zero exits (no patch or harness error): $failed"
        Write-Host "Summary: $(Join-Path $EvalRoot 'local_verdicts.jsonl')"
    }
}
