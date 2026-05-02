param(
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @(),
    [string]$StoreHelperExe = ""
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
# winrt-* packages are needed by the Microsoft Store subscription gate. Pip-install
# is best-effort: not all of these are guaranteed to wheel cleanly on all Windows
# Python ABIs, but pyinstaller's --collect-submodules below picks up whatever is
# importable. The subscription module itself imports lazily and falls back to
# offline cache if the import fails.
& $PythonExe @PythonArgs -m pip install --upgrade `
    "winrt-runtime" `
    "winrt-Windows.Foundation" `
    "winrt-Windows.Foundation.Collections" `
    "winrt-Windows.Services.Store"

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
  --collect-submodules winrt `
  --collect-submodules "winrt.windows.services.store" `
  --add-data "$licensesDir;licenses" `
  "$appScript"

$exePath = Join-Path $scriptDir "dist\ChairsideReadyAlert\ChairsideReadyAlert.exe"
if (-not (Test-Path $exePath)) {
    throw "Build finished but EXE was not found at: $exePath"
}

# StoreHelper.exe is a tiny C# binary that does the IInitializeWithWindow handshake
# the Microsoft Store SDK requires for purchase-overlay calls (see StoreHelper/Program.cs).
# Python's winrt projection cannot reach IInitializeWithWindow because it is classic COM,
# not WinRT — without this shim the purchase call fails with RPC_E_WRONG_THREAD.
if ($StoreHelperExe) {
    if (-not (Test-Path $StoreHelperExe)) {
        throw "StoreHelperExe was specified but the file does not exist: $StoreHelperExe"
    }
    $distDir = Split-Path -Parent $exePath
    $destExe = Join-Path $distDir "StoreHelper.exe"
    Copy-Item -Path $StoreHelperExe -Destination $destExe -Force
    Write-Info "Copied StoreHelper.exe ($((Get-Item $destExe).Length) bytes) into dist bundle: $destExe"
} else {
    Write-Warning "StoreHelperExe not provided — Microsoft Store purchase calls will fail in the resulting build. This is fine for non-Store builds, but a Store/MSIX submission needs the helper bundled."
}

Write-Info "Build complete."
Write-Host "EXE: $exePath" -ForegroundColor Green
