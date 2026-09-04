[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $env:LOCALAPPDATA "HelixGrid"
$LogFile = Join-Path $StateDir "update.log"
$ConfigFile = Join-Path $StateDir "dashboard.json"

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Start-Transcript -Path $LogFile -Append | Out-Null

function Step([string]$Text) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkGray
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor DarkGray
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Find-Python {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $path = (& $py.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($path -and (Test-Path $path)) {
                return $path
            }
        } catch {}
    }

    $known = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:ProgramFiles "Python313\python.exe")
    )
    foreach ($item in $known) {
        if (Test-Path $item) {
            return $item
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return $python.Source
    }
    return $null
}

function Load-DashboardConfig {
    $defaultWorkspace = Join-Path $RepoRoot "workspace"
    $defaultResults = Join-Path $RepoRoot "helix-results"
    $workers = 3

    if (Test-Path $ConfigFile) {
        try {
            $config = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
            if ($config.workspace) { $defaultWorkspace = [string]$config.workspace }
            if ($config.results) { $defaultResults = [string]$config.results }
            if ($config.workers) {
                $workers = [Math]::Max(1, [Math]::Min(16, [int]$config.workers))
            }
        } catch {
            Write-Warning "Kunne ikke laese dashboard.json. Bruger standardindstillinger."
        }
    }

    return @{
        Workspace = $defaultWorkspace
        Results = $defaultResults
        Workers = $workers
    }
}

try {
    Clear-Host
    Write-Host ""
    Write-Host "  HELIXGRID UPDATER" -ForegroundColor Cyan
    Write-Host "  Henter den nyeste version fra GitHub." -ForegroundColor Gray
    Write-Host ""

    Refresh-Path

    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) {
        throw "Git blev ikke fundet. Koer windows-install.bat foerst."
    }

    Push-Location $RepoRoot
    try {
        Step "Kontrollerer lokale aendringer"
        $dirty = @(& $git.Source status --porcelain --untracked-files=no)
        if ($LASTEXITCODE -ne 0) {
            throw "Kunne ikke kontrollere Git-status."
        }
        if ($dirty.Count -gt 0) {
            Write-Host "Updateren stoppede for at beskytte dine egne kodeaendringer:" -ForegroundColor Yellow
            $dirty | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
            throw "Gem/commit/revert dine lokale tracked aendringer og koer update.bat igen."
        }

        Step "Henter nyeste HelixGrid"
        & $git.Source fetch origin main --prune
        if ($LASTEXITCODE -ne 0) {
            throw "Kunne ikke hente opdateringen fra GitHub."
        }

        $before = (& $git.Source rev-parse HEAD).Trim()
        $remote = (& $git.Source rev-parse origin/main).Trim()

        if ($before -eq $remote) {
            Write-Host "Du har allerede den nyeste version." -ForegroundColor Green
        }
        else {
            & $git.Source pull --ff-only origin main
            if ($LASTEXITCODE -ne 0) {
                throw "Git kunne ikke lave en sikker fast-forward update."
            }
            $after = (& $git.Source rev-parse HEAD).Trim()
            Write-Host "Opdateret:" -ForegroundColor Green
            Write-Host "  Foer: $before"
            Write-Host "  Nu:   $after"
        }
    }
    finally {
        Pop-Location
    }

    Step "Opdaterer Python-delen"
    $python = Find-Python
    if (-not $python) {
        throw "Python blev ikke fundet. Koer windows-install.bat."
    }
    & $python -m pip install --disable-pip-version-check -e (Join-Path $RepoRoot "sdk\python")
    if ($LASTEXITCODE -ne 0) {
        throw "Python-delen kunne ikke opdateres."
    }

    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw "Docker blev ikke fundet. Koer windows-install.bat."
    }

    $settings = Load-DashboardConfig
    New-Item -ItemType Directory -Force -Path $settings.Workspace | Out-Null
    New-Item -ItemType Directory -Force -Path $settings.Results | Out-Null
    $env:HELIX_WORKSPACE = $settings.Workspace
    $env:HELIX_RESULTS = $settings.Results

    Step "Opdaterer Docker-services"
    Push-Location $RepoRoot
    try {
        & $docker.Source compose up -d --build --scale "worker=$($settings.Workers)"
        if ($LASTEXITCODE -ne 0) {
            throw "Docker-services kunne ikke rebuildes."
        }
    }
    finally {
        Pop-Location
    }

    Step "Faerdig"
    Write-Host "HelixGrid er opdateret." -ForegroundColor Green
    Write-Host "Dine dashboard-indstillinger og resultatfiler er bevaret." -ForegroundColor Green
    Write-Host ""
    Write-Host "Starter dashboardet..."

    Start-Process -FilePath (Join-Path $RepoRoot "start.bat")
    Start-Sleep -Seconds 1
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host ""
    Write-Host "UPDATE-FEJL" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Logfil: $LogFile"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
