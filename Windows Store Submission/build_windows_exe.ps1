param(
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @()
)

$ErrorActionPreference = "Stop"

function Write-Info([string]$Message) {
    Write-Host "[Chairside Ready Alert Build] $Message" -ForegroundColor Cyan
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$localAppScript = Join-Path $scriptDir "chairside_ready_alert.py"
$repoRootAppScript = Join-Path (Split-Path $scriptDir -Parent) "chairside_ready_alert.py"
$appScript = if (Test-Path $localAppScript) { $localAppScript } elseif (Test-Path $repoRootAppScript) { $repoRootAppScript } else { $null }
if (-not $appScript) { throw "Could not find chairside_ready_alert.py in this folder or parent folder." }

# licenses/ folder (third-party license texts). Ships next to the EXE so the
# EXE bundle is self-contained for compliance.
$repoRoot = Split-Path $scriptDir -Parent
$licensesDir = Join-Path $repoRoot "licenses"
if (-not (Test-Path $licensesDir)) { throw "licenses/ folder not found at: $licensesDir" }

Write-Info "Using working directory: $scriptDir"
Set-Location $scriptDir
Write-Info "App script: $appScript"
Write-Info "Python command: $PythonExe $($PythonArgs -join ' ')"

Write-Info "Checking Python launcher..."
& $PythonExe @PythonArgs --version

Write-Info "Installing/updating build dependencies..."
& $PythonExe @PythonArgs -m pip install --upgrade pip
& $PythonExe @PythonArgs -m pip install --upgrade pyinstaller pystray pillow cairosvg certifi

Write-Info "Cleaning previous build artifacts..."
if (Test-Path ".\build") { Remove-Item ".\build" -Recurse -Force }
if (Test-Path ".\dist") { Remove-Item ".\dist" -Recurse -Force }
if (Test-Path ".\ChairsideReadyAlert.spec") { Remove-Item ".\ChairsideReadyAlert.spec" -Force }

Write-Info "Building Windows executable (onedir)..."
& $PythonExe @PythonArgs -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --name "ChairsideReadyAlert" `
  --hidden-import pystray._win32 `
  --hidden-import PIL._tkinter_finder `
  --collect-submodules pystray `
  --collect-submodules PIL `
  --collect-data certifi `
  --add-data "$licensesDir;licenses" `
  "$appScript"

$exePath = Join-Path $scriptDir "dist\ChairsideReadyAlert\ChairsideReadyAlert.exe"
if (-not (Test-Path $exePath)) {
    throw "Build finished but EXE was not found at: $exePath"
}

Write-Info "Build complete."
Write-Host "EXE: $exePath" -ForegroundColor Green
