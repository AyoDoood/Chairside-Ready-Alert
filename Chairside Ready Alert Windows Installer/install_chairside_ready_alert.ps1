$ErrorActionPreference = "Stop"

function Write-Info($message) {
    Write-Host "[Chairside Ready Alert] $message"
}

function Get-PythonwPath {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "$env:ProgramFiles\Python311\pythonw.exe",
        "$env:ProgramFiles(x86)\Python311\pythonw.exe"
    )

    foreach ($path in $candidates) {
        if (Test-Path $path) {
            return $path
        }
    }

    try {
        $probe = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $pyExe = $probe.Trim()
            if ($pyExe) {
                $pyw = $pyExe -replace "python\.exe$", "pythonw.exe"
                if (Test-Path $pyw) {
                    return $pyw
                }
            }
        }
    } catch {
        # Ignore and continue.
    }

    return $null
}

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceScript = Join-Path $packageDir "chairside_ready_alert.py"
$pythonInstaller = Join-Path $packageDir "python-3.11.9-amd64.exe"
$installDir = Join-Path $env:LOCALAPPDATA "ChairsideReadyAlert"
$launcherBat = Join-Path $installDir "Start Chairside Ready Alert.bat"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Chairside Ready Alert.lnk"

if (-not (Test-Path $sourceScript)) {
    throw "Missing chairside_ready_alert.py in installer package."
}

$pythonwPath = Get-PythonwPath
if (-not $pythonwPath) {
    if (-not (Test-Path $pythonInstaller)) {
        throw "Missing python-3.11.9-amd64.exe in installer package."
    }

    Write-Info "Installing Python 3.11 silently..."
    $args = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_launcher=1",
        "Include_pip=1",
        "Include_test=0",
        "SimpleInstall=1"
    )
    $process = Start-Process -FilePath $pythonInstaller -ArgumentList $args -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "Python installer failed with exit code $($process.ExitCode)."
    }

    $pythonwPath = Get-PythonwPath
    if (-not $pythonwPath) {
        throw "Python 3.11 install finished but pythonw.exe was not found."
    }
}

Write-Info "Installing Chairside Ready Alert files..."
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -Path $sourceScript -Destination (Join-Path $installDir "chairside_ready_alert.py") -Force

$launcherContent = @"
@echo off
cd /d "%~dp0"
"$pythonwPath" "%~dp0chairside_ready_alert.py"
"@
Set-Content -Path $launcherBat -Value $launcherContent -Encoding ASCII

Write-Info "Creating desktop shortcut..."
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($desktopShortcut)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = "`"$installDir\chairside_ready_alert.py`""
$shortcut.WorkingDirectory = $installDir
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
$shortcut.Save()

Write-Info "Launching Chairside Ready Alert..."
Start-Process -FilePath $pythonwPath -ArgumentList "`"$installDir\chairside_ready_alert.py`"" -WorkingDirectory $installDir

Write-Info "Install complete. Use Desktop shortcut: Chairside Ready Alert"
