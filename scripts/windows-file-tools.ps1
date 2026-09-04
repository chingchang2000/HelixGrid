param(
    [Parameter(Mandatory = $true)]
    [string]$Folder,

    [ValidateSet("audit", "backup")]
    [string]$Mode = "audit",

    [ValidateRange(1, 16)]
    [int]$Workers = 3
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FolderPath = (Resolve-Path $Folder).Path
$ResultsPath = Join-Path $RepoRoot "helix-results"
New-Item -ItemType Directory -Force -Path $ResultsPath | Out-Null

$env:HELIX_WORKSPACE = $FolderPath
$env:HELIX_RESULTS = $ResultsPath

Write-Host ""
Write-Host "HelixGrid File Tools" -ForegroundColor Cyan
Write-Host "Mappe: $FolderPath"
Write-Host "Mode: $Mode"
Write-Host "Workers: $Workers"
Write-Host "Dine filer er READ-ONLY for workers." -ForegroundColor Green
Write-Host ""

Push-Location $RepoRoot
try {
    docker compose up -d --build --scale worker=$Workers
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose kunne ikke starte." }

    Write-Host "Venter paa coordinator..."
    $Ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/healthz" -TimeoutSec 2
            if ($Health.status -eq "ok") { $Ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $Ready) { throw "Coordinator blev ikke klar. Koer: docker compose logs coordinator" }

    if ($Mode -eq "backup") {
        $WorkflowFile = Join-Path $RepoRoot "examples\windows-file-backup.json"
    } else {
        $WorkflowFile = Join-Path $RepoRoot "examples\windows-file-audit.json"
    }

    $Json = Get-Content -LiteralPath $WorkflowFile -Raw
    $Response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/workflows" -ContentType "application/json" -Body $Json -TimeoutSec 10
    $WorkflowId = $Response.data.id
    Write-Host "Workflow: $WorkflowId"

    while ($true) {
        Start-Sleep -Milliseconds 750
        $State = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8080/v1/workflows/$WorkflowId" -TimeoutSec 10
        $WorkflowState = $State.data.state
        $Parts = @($State.data.runtime.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value.state)" })
        Write-Host "[$WorkflowState] $($Parts -join ', ')"
        if ($WorkflowState -in @("SUCCEEDED", "FAILED", "CANCELLED")) { break }
    }

    if ($WorkflowState -ne "SUCCEEDED") {
        Write-Host "Workflow fejlede. Se: docker compose logs worker" -ForegroundColor Red
        exit 2
    }

    Write-Host ""
    Write-Host "FAERDIG!" -ForegroundColor Green
    Write-Host "Resultater: $ResultsPath"

    if ($Mode -eq "audit") {
        $Summary = Join-Path $ResultsPath "summary.txt"
        if (Test-Path $Summary) {
            Write-Host ""
            Get-Content $Summary
        }
        Write-Host ""
        Write-Host "summary.txt = nem rapport"
        Write-Host "duplicates.json = dubletter"
        Write-Host "inventory.json = stoerrelser og filtyper"
        Write-Host "checksums.csv = SHA-256 for alle filer"
    } else {
        Write-Host "backup.tar.gz = backup"
        Write-Host "backup.json = checksum og info"
    }
}
finally {
    Pop-Location
}
