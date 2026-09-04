[CmdletBinding()]
param(
    [switch]$Resume
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StateDir = Join-Path $env:LOCALAPPDATA "HelixGrid"
$LogFile = Join-Path $StateDir "install.log"
$Desktop = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$Workspace = Join-Path $RepoRoot "workspace"
$Results = Join-Path $RepoRoot "helix-results"

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
New-Item -ItemType Directory -Force -Path $Results | Out-Null
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

function Test-Command([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
    if (Test-Command "winget.exe") {
        return
    }
    throw "Windows Package Manager (winget) blev ikke fundet. Installer eller opdater App Installer fra Microsoft Store og koer windows-install.bat igen."
}

function Package-Installed([string]$Id) {
    & winget.exe list --exact --id $Id *> $null
    return $LASTEXITCODE -eq 0
}

function Install-Package([string]$Id, [string]$Name) {
    Step "Kontrollerer $Name"
    if (Package-Installed $Id) {
        Write-Host "$Name er allerede installeret." -ForegroundColor Green
        return
    }

    Write-Host "Installerer $Name..."
    & winget.exe install --exact --id $Id --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        throw "$Name kunne ikke installeres. Winget fejlkode: $LASTEXITCODE"
    }
    Refresh-Path
}

function Test-WslReady {
    try {
        & wsl.exe --status *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Register-Resume {
    $batch = Join-Path $RepoRoot "windows-install.bat"
    $command = 'cmd.exe /c ""' + $batch + '" -Resume"'
    New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce" -Name "HelixGridInstall" -Value $command -PropertyType String -Force | Out-Null
}

function Request-Restart([string]$Reason) {
    Register-Resume
    Add-Type -AssemblyName PresentationFramework
    $choice = [System.Windows.MessageBox]::Show(
        "$Reason\n\nInstallationen fortsaetter automatisk efter genstart.\n\nVil du genstarte Windows nu?",
        "HelixGrid installation",
        [System.Windows.MessageBoxButton]::YesNo,
        [System.Windows.MessageBoxImage]::Information
    )

    if ($choice -eq [System.Windows.MessageBoxResult]::Yes) {
        Stop-Transcript | Out-Null
        shutdown.exe /r /t 5 /c "HelixGrid installation fortsaetter efter genstart"
        exit 0
    }

    Write-Host ""
    Write-Host "Genstart Windows senere. Installationen fortsaetter automatisk efter login." -ForegroundColor Yellow
    Stop-Transcript | Out-Null
    exit 0
}

function Docker-Exe {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $known = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    if (Test-Path $known) {
        return $known
    }
    return $null
}

function Docker-DesktopExe {
    $known = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    foreach ($item in $known) {
        if (Test-Path $item) {
            return $item
        }
    }
    return $null
}

function Python-Exe {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $path = (& $py.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($path -and (Test-Path $path)) {
                return $path
            }
        }
        catch {}
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

function Wait-Docker([int]$Seconds = 150) {
    $docker = Docker-Exe
    if (-not $docker) {
        return $false
    }

    & $docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }

    $desktop = Docker-DesktopExe
    if ($desktop) {
        Step "Starter Docker Desktop"
        Start-Process -FilePath $desktop | Out-Null
    }

    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        & $docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    return $false
}

function Create-Shortcut([string]$Path, [string]$Description) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = Join-Path $RepoRoot "start.bat"
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.Description = $Description
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,44"
    $shortcut.Save()
}

try {
    Clear-Host
    Write-Host ""
    Write-Host "  HELIXGRID WINDOWS INSTALLER" -ForegroundColor Cyan
    Write-Host "  Alt bliver sat op automatisk." -ForegroundColor Gray
    Write-Host ""

    Ensure-Winget

    Step "Kontrollerer WSL 2"
    if (-not (Test-WslReady)) {
        Write-Host "WSL mangler. Installerer WSL 2..."
        & wsl.exe --install --no-distribution
        $wslCode = $LASTEXITCODE

        if ($wslCode -ne 0 -and $wslCode -ne 3010) {
            Write-Host "Proever standard WSL-installation..."
            & wsl.exe --install
            $wslCode = $LASTEXITCODE
        }

        if ($wslCode -eq 3010 -or -not (Test-WslReady)) {
            Request-Restart "WSL 2 er installeret, men Windows skal genstartes for at aktivere det."
        }
    }

    & wsl.exe --update | Out-Host

    Install-Package "Git.Git" "Git"
    Install-Package "Python.Python.3.13" "Python 3.13"
    Install-Package "Docker.DockerDesktop" "Docker Desktop"

    Refresh-Path

    $python = Python-Exe
    if (-not $python) {
        Request-Restart "Python er installeret, men Windows skal genindlaese miljoet."
    }

    Step "Kontrollerer dashboard-komponenter"
    & $python -c "import tkinter; import json; import urllib.request"
    if ($LASTEXITCODE -ne 0) {
        throw "Python blev fundet, men Tkinter-dashboardet kunne ikke indlaeses."
    }

    Step "Installerer HelixGrid Python-vaerktoejer"
    & $python -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip kunne ikke opdateres."
    }

    & $python -m pip install --disable-pip-version-check -e (Join-Path $RepoRoot "sdk\python")
    if ($LASTEXITCODE -ne 0) {
        throw "HelixGrid Python-vaerktoejerne kunne ikke installeres."
    }

    Step "Kontrollerer Docker Engine"
    if (-not (Wait-Docker 150)) {
        Request-Restart "Docker Desktop er installeret, men Docker Engine kunne ikke starte endnu."
    }

    $docker = Docker-Exe
    if (-not $docker) {
        throw "docker.exe blev ikke fundet efter installation."
    }

    $env:HELIX_WORKSPACE = $Workspace
    $env:HELIX_RESULTS = $Results

    Step "Bygger og starter HelixGrid"
    Push-Location $RepoRoot
    try {
        & $docker compose config -q
        if ($LASTEXITCODE -ne 0) {
            throw "docker-compose.yml er ugyldig."
        }

        & $docker compose up -d --build --scale worker=3
        if ($LASTEXITCODE -ne 0) {
            throw "HelixGrid Docker-containerne kunne ikke starte."
        }
    }
    finally {
        Pop-Location
    }

    Step "Opretter genveje"
    Create-Shortcut (Join-Path $Desktop "HelixGrid.lnk") "Start HelixGrid"
    Create-Shortcut (Join-Path $StartMenu "HelixGrid.lnk") "Start HelixGrid"

    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce" -Name "HelixGridInstall" -ErrorAction SilentlyContinue

    Step "Installation faerdig"
    Write-Host "HelixGrid er installeret og koerer." -ForegroundColor Green
    Write-Host "Du kan fremover dobbeltklikke HelixGrid paa skrivebordet." -ForegroundColor Green
    Write-Host ""
    Write-Host "Starter dashboardet..."

    Start-Process -FilePath (Join-Path $RepoRoot "start.bat")
    Start-Sleep -Seconds 2
    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host ""
    Write-Host "INSTALLATIONSFEJL" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Logfil: $LogFile"
    try {
        Stop-Transcript | Out-Null
    }
    catch {}
    exit 1
}
