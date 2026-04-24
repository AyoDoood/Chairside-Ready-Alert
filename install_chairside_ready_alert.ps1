$ErrorActionPreference = "Stop"

function Write-Info($message) {
    Write-Host "[Chairside Ready Alert] $message"
}

function Get-PythonwPath {
    $pf86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:ProgramFiles\Python312\pythonw.exe",
        (Join-Path $pf86 "Python312\pythonw.exe"),
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "$env:ProgramFiles\Python311\pythonw.exe",
        (Join-Path $pf86 "Python311\pythonw.exe")
    )

    foreach ($path in $candidates) {
        if (Test-Path $path) {
            return $path
        }
    }

    foreach ($pyVer in @("-3.12", "-3.11")) {
        try {
            $probe = & py $pyVer -c "import sys; print(sys.executable)" 2>$null
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
    }

    return $null
}

function Get-BundledPythonInstaller {
    param([string]$PackageDir)
    foreach ($name in @(
        "python-3.12.8-amd64.exe",
        "python-3.12.7-amd64.exe",
        "python-3.11.9-amd64.exe"
    )) {
        $p = Join-Path $PackageDir $name
        if (Test-Path $p) {
            return $p
        }
    }
    return $null
}

function Get-PreferredPythonInstallerName {
    return "python-3.12.8-amd64.exe"
}

function Get-PreferredPythonInstallerUrl {
    $name = Get-PreferredPythonInstallerName
    return "https://www.python.org/ftp/python/3.12.8/$name"
}

function Get-DownloadedPythonInstallerPath {
    $cacheDir = Join-Path $env:LOCALAPPDATA "ChairsideReadyAlert\cache"
    New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
    return (Join-Path $cacheDir (Get-PreferredPythonInstallerName))
}

function Download-PythonInstallerIfNeeded {
    $downloadPath = Get-DownloadedPythonInstallerPath
    if (Test-Path -LiteralPath $downloadPath) {
        Write-Info "Using cached Python installer: $(Split-Path -Leaf $downloadPath)"
        return $downloadPath
    }

    $url = Get-PreferredPythonInstallerUrl
    Write-Info "No bundled Python installer found. Downloading from python.org..."
    Write-Host "[Chairside Ready Alert] URL: $url"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    } catch {
        # Older/locked-down systems may reject setting this; continue with defaults.
    }
    try {
        Invoke-WebRequest -Uri $url -OutFile $downloadPath -UseBasicParsing
        if (-not (Test-Path -LiteralPath $downloadPath)) {
            throw "Download completed but installer file is missing."
        }
        return $downloadPath
    } catch {
        try {
            if (Test-Path -LiteralPath $downloadPath) {
                Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
            }
        } catch {}
        throw "Could not download Python installer from python.org. Check internet access or place $(Get-PreferredPythonInstallerName) next to the installer files."
    }
}

function Ensure-PythonInstalled {
    param(
        [string]$PythonInstallerPath
    )
    Write-Info "Installing Python silently: $(Split-Path -Leaf $PythonInstallerPath)"
    $args = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_launcher=1",
        "Include_pip=1",
        "Include_test=0",
        "SimpleInstall=1"
    )
    $process = Start-Process -FilePath $PythonInstallerPath -ArgumentList $args -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "Python installer failed with exit code $($process.ExitCode)."
    }
}

function Get-InstallerRoot {
    # $PSScriptRoot is set when this file is run with -File (folder containing this .ps1)
    if ($PSScriptRoot) {
        return $PSScriptRoot
    }
    $p = $MyInvocation.MyCommand.Path
    if ($p) {
        return Split-Path -Parent $p
    }
    return (Get-Location).Path
}

function Resolve-ChairsideReadyAlertPy {
    param([string]$InstallerRoot)
    $primary = Join-Path $InstallerRoot "chairside_ready_alert.py"
    if (Test-Path -LiteralPath $primary) {
        return $primary
    }
    # Fallback: current directory (after .bat does "cd /d" to the installer folder)
    $cwd = (Get-Location).Path
    $fallback = Join-Path $cwd "chairside_ready_alert.py"
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }
    return $null
}

function Install-TrayDependencies {
    param([string]$PythonwPath)
    if (-not $PythonwPath) {
        return
    }
    $pythonExe = $PythonwPath -replace "pythonw\.exe$", "python.exe"
    if (-not (Test-Path $pythonExe)) {
        return
    }
    Write-Info "Installing app dependencies (pystray, pillow, cairosvg, certifi)..."
    try {
        & $pythonExe -m pip install --disable-pip-version-check --user --upgrade pystray pillow cairosvg certifi
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[Chairside Ready Alert] Warning: could not install tray dependencies; app will still run." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[Chairside Ready Alert] Warning: tray dependency install failed; app will still run." -ForegroundColor Yellow
    }
}

function Stop-RunningChairsideReadyAlert {
    param([int]$GraceSeconds = 12)
    Write-Info "Checking for running Chairside Ready Alert..."
    $targets = @()
    try {
        $targets = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.CommandLine -and $_.CommandLine -match "chairside_ready_alert\.py" -and $_.ProcessId -ne $PID
        }
    } catch {
        # If process enumeration fails, continue install flow.
        return
    }
    if (-not $targets -or $targets.Count -eq 0) {
        return
    }
    Write-Info "Closing running Chairside Ready Alert before install..."
    foreach ($proc in $targets) {
        try {
            Stop-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
        } catch {}
    }

    $deadline = (Get-Date).AddSeconds($GraceSeconds)
    while ((Get-Date) -lt $deadline) {
        $alive = @()
        foreach ($proc in $targets) {
            if (Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue) {
                $alive += $proc
            }
        }
        if ($alive.Count -eq 0) {
            Write-Info "Chairside Ready Alert closed."
            return
        }
        Start-Sleep -Milliseconds 300
    }

    Write-Host "[Chairside Ready Alert] Warning: app is still running after ${GraceSeconds}s; forcing close..." -ForegroundColor Yellow
    foreach ($proc in $targets) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    Start-Sleep -Milliseconds 500
}

function Build-BrandingIconWithDotNet {
    param(
        [string]$InstallerRoot,
        [string]$InstallDir
    )
    $outIco = Join-Path $InstallDir "AppIcon.ico"
    $outPng = Join-Path $InstallDir "Logo.png"
    try {
        Add-Type -AssemblyName System.Drawing
        Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class IconUtil {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool DestroyIcon(IntPtr handle);
}
"@ -ErrorAction SilentlyContinue

        $size = 256
        $bitmap = New-Object System.Drawing.Bitmap $size, $size
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)

        $pngCandidates = @(
            (Join-Path $InstallDir "Logo.png"),
            (Join-Path $InstallDir "logo.png"),
            (Join-Path $InstallerRoot "Logo.png"),
            (Join-Path $InstallerRoot "logo.png")
        )
        $sourcePng = $pngCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

        if ($sourcePng) {
            $src = [System.Drawing.Image]::FromFile($sourcePng)
            $graphics.DrawImage($src, 0, 0, $size, $size)
            $src.Dispose()
        } else {
            $accent = [System.Drawing.ColorTranslator]::FromHtml("#2A7FFF")
            $white = [System.Drawing.Color]::White

            $bubbleX = [int]([Math]::Round($size * 0.10))
            $bubbleY = [int]([Math]::Round($size * 0.15))
            $bubbleW = [int]([Math]::Round($size * 0.80))
            $bubbleH = [int]([Math]::Round($size * 0.55))
            $radius = [single]([Math]::Round($size * 0.15))

            $rectPath = New-Object System.Drawing.Drawing2D.GraphicsPath
            $diameter = $radius * 2
            $rectPath.AddArc($bubbleX, $bubbleY, $diameter, $diameter, 180, 90)
            $rectPath.AddArc($bubbleX + $bubbleW - $diameter, $bubbleY, $diameter, $diameter, 270, 90)
            $rectPath.AddArc($bubbleX + $bubbleW - $diameter, $bubbleY + $bubbleH - $diameter, $diameter, $diameter, 0, 90)
            $rectPath.AddArc($bubbleX, $bubbleY + $bubbleH - $diameter, $diameter, $diameter, 90, 90)
            $rectPath.CloseFigure()

            $accentBrush = New-Object System.Drawing.SolidBrush $accent
            $graphics.FillPath($accentBrush, $rectPath)

            $tailPoints = @(
                (New-Object System.Drawing.Point ([int]([Math]::Round($size * 0.30)), [int]([Math]::Round($size * 0.70)))),
                (New-Object System.Drawing.Point ([int]([Math]::Round($size * 0.45)), [int]([Math]::Round($size * 0.70)))),
                (New-Object System.Drawing.Point ([int]([Math]::Round($size * 0.30)), [int]([Math]::Round($size * 0.85))))
            )
            $graphics.FillPolygon($accentBrush, $tailPoints)

            $whiteBrush = New-Object System.Drawing.SolidBrush $white
            $graphics.FillRectangle(
                $whiteBrush,
                [int]([Math]::Round($size * 0.45)),
                [int]([Math]::Round($size * 0.30)),
                [int]([Math]::Round($size * 0.10)),
                [int]([Math]::Round($size * 0.25))
            )
            $graphics.FillRectangle(
                $whiteBrush,
                [int]([Math]::Round($size * 0.375)),
                [int]([Math]::Round($size * 0.375)),
                [int]([Math]::Round($size * 0.25)),
                [int]([Math]::Round($size * 0.10))
            )

            $whiteBrush.Dispose()
            $accentBrush.Dispose()
            $rectPath.Dispose()
        }

        try {
            $bitmap.Save($outPng, [System.Drawing.Imaging.ImageFormat]::Png)
        } catch {
            # Optional side output only.
        }

        $iconHandle = $bitmap.GetHicon()
        $icon = [System.Drawing.Icon]::FromHandle($iconHandle)
        $stream = [System.IO.File]::Open($outIco, [System.IO.FileMode]::Create)
        $icon.Save($stream)
        $stream.Dispose()
        $icon.Dispose()
        [IconUtil]::DestroyIcon($iconHandle) | Out-Null
        $graphics.Dispose()
        $bitmap.Dispose()

        if (Test-Path -LiteralPath $outIco) {
            return $outIco
        }
    } catch {
        # Ignore and let caller fall back.
    }
    return $null
}

function Build-BrandingIcons {
    param(
        [string]$PythonwPath,
        [string]$InstallerRoot,
        [string]$InstallDir
    )
    foreach ($existing in @(
        (Join-Path $InstallDir "AppIcon.ico"),
        (Join-Path $InstallDir "Logo.ico"),
        (Join-Path $InstallDir "logo.ico")
    )) {
        if (Test-Path -LiteralPath $existing) {
            return $existing
        }
    }

    if (-not $PythonwPath) {
        return $null
    }
    $pythonExe = $PythonwPath -replace "pythonw\.exe$", "python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        return $null
    }

    $logoPng = Join-Path $InstallDir "Logo.png"
    $logoIco = Join-Path $InstallDir "AppIcon.ico"
    $inline = @"
import os
import sys

installer_root = sys.argv[1]
install_dir = sys.argv[2]
out_png = os.path.join(install_dir, "Logo.png")
out_ico = os.path.join(install_dir, "AppIcon.ico")

try:
    from PIL import Image
except Exception:
    raise SystemExit(1)

png_candidates = [
    os.path.join(install_dir, "Logo.png"),
    os.path.join(install_dir, "logo.png"),
    os.path.join(installer_root, "Logo.png"),
    os.path.join(installer_root, "logo.png"),
]
svg_candidates = [
    os.path.join(install_dir, "Logo.svg"),
    os.path.join(install_dir, "logo.svg"),
    os.path.join(installer_root, "Logo.svg"),
    os.path.join(installer_root, "logo.svg"),
]

img = None
for p in png_candidates:
    if os.path.isfile(p):
        img = Image.open(p).convert("RGBA")
        break

if img is None:
    for p in svg_candidates:
        if not os.path.isfile(p):
            continue
        try:
            import cairosvg
            import io
            png_bytes = cairosvg.svg2png(url=p, output_width=512, output_height=512)
            img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            break
        except Exception:
            continue

if img is None:
    # Final fallback: draw the same branded chat bubble + white cross.
    from PIL import ImageDraw
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    accent = "#2A7FFF"
    fg = "#FFFFFF"
    x = lambda v: int(round((v / 100.0) * size))
    y = lambda v: int(round((v / 100.0) * size))
    d.rounded_rectangle((x(10), y(15), x(90), y(70)), radius=int(round(size * 0.15)), fill=accent)
    d.polygon([(x(30), y(70)), (x(45), y(70)), (x(30), y(85))], fill=accent)
    d.rectangle((x(45), y(30), x(55), y(55)), fill=fg)
    d.rectangle((x(37.5), y(37.5), x(62.5), y(47.5)), fill=fg)

resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
img.save(out_png, "PNG")
img.resize((256, 256), resample).save(out_ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out_ico)
"@
    try {
        $result = & $pythonExe -c $inline $InstallerRoot $InstallDir 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $logoIco)) {
            return $logoIco
        }
    } catch {
        # Ignore and continue to fallback logic.
    }
    return (Build-BrandingIconWithDotNet -InstallerRoot $InstallerRoot -InstallDir $InstallDir)
}

try {
    $packageDir = Get-InstallerRoot
    $sourceScript = Resolve-ChairsideReadyAlertPy -InstallerRoot $packageDir
    $pythonInstaller = Get-BundledPythonInstaller -PackageDir $packageDir
    $installDir = Join-Path $env:LOCALAPPDATA "ChairsideReadyAlert"
    $launcherBat = Join-Path $installDir "Start Chairside Ready Alert.bat"
    $desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Chairside Ready Alert.lnk"

    if (-not $sourceScript) {
        $cwd = (Get-Location).Path
        throw @"
Could not find chairside_ready_alert.py.

Keep these files together in ONE folder (do not move only the .bat):
  - Install Chairside Ready Alert.bat
  - install_chairside_ready_alert.ps1
  - chairside_ready_alert.py

Searched:
  $packageDir
  $cwd
"@
    }

    Stop-RunningChairsideReadyAlert

    $pythonwPath = Get-PythonwPath
    if (-not $pythonwPath) {
        if (-not $pythonInstaller) {
            $pythonInstaller = Download-PythonInstallerIfNeeded
        }
        Ensure-PythonInstalled -PythonInstallerPath $pythonInstaller

        $pythonwPath = Get-PythonwPath
        if (-not $pythonwPath) {
            throw "Python install finished but pythonw.exe was not found. Restart the PC or run the installer again."
        }
    }

    Install-TrayDependencies -PythonwPath $pythonwPath

    Write-Info "Installing Chairside Ready Alert files..."
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    $destPy = Join-Path $installDir "chairside_ready_alert.py"
    try {
        Copy-Item -Path $sourceScript -Destination $destPy -Force
    } catch {
        throw "Could not copy chairside_ready_alert.py to $installDir. Close Chairside Ready Alert completely (check the system tray too), then run this installer again."
    }
    foreach ($logoName in @("logo.svg", "Logo.svg", "logo.png", "Logo.png", "logo.ico", "Logo.ico", "AppIcon.ico")) {
        $srcLogo = Join-Path $packageDir $logoName
        if (Test-Path -LiteralPath $srcLogo) {
            try {
                Copy-Item -Path $srcLogo -Destination (Join-Path $installDir $logoName) -Force
            } catch {
                Write-Host "[Chairside Ready Alert] Warning: could not copy $logoName to $installDir." -ForegroundColor Yellow
            }
        }
    }
    foreach ($supportFile in @(
        "install_chairside_ready_alert.ps1",
        "Install Chairside Ready Alert.bat",
        "Install Chairside Ready Alert macOS.command",
        "install_chairside_ready_alert_macos.sh",
        "README-Windows-One-Click.txt",
        "version.json",
        "version.json.example"
    )) {
        $srcSupport = Join-Path $packageDir $supportFile
        if (-not (Test-Path -LiteralPath $srcSupport)) {
            continue
        }
        try {
            Copy-Item -Path $srcSupport -Destination (Join-Path $installDir $supportFile) -Force
        } catch {
            Write-Host "[Chairside Ready Alert] Warning: could not copy $supportFile to $installDir." -ForegroundColor Yellow
        }
    }
    $iconPath = Build-BrandingIcons -PythonwPath $pythonwPath -InstallerRoot $packageDir -InstallDir $installDir

    $launcherContent = @"
@echo off
cd /d "%~dp0"
"$pythonwPath" "%~dp0chairside_ready_alert.py"
"@
    Set-Content -Path $launcherBat -Value $launcherContent -Encoding ASCII

    Write-Info "Creating desktop shortcut..."
    if (Test-Path -LiteralPath $desktopShortcut) {
        Remove-Item -LiteralPath $desktopShortcut -Force -ErrorAction SilentlyContinue
    }
    $shortcutIconLocation = "$pythonwPath,0"
    if ($iconPath -and (Test-Path -LiteralPath $iconPath)) {
        $iconStamp = Get-Date -Format "yyyyMMddHHmmss"
        $stampedIcon = Join-Path $installDir "AppIcon-$iconStamp.ico"
        try {
            Copy-Item -Path $iconPath -Destination $stampedIcon -Force
            $shortcutIconLocation = "$stampedIcon,0"
            Get-ChildItem -Path $installDir -Filter "AppIcon-*.ico" -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -ne $stampedIcon } |
                Remove-Item -Force -ErrorAction SilentlyContinue
        } catch {
            $shortcutIconLocation = "$iconPath,0"
        }
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($desktopShortcut)
    $shortcut.TargetPath = $pythonwPath
    $shortcut.Arguments = "`"$installDir\chairside_ready_alert.py`""
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = $shortcutIconLocation
    $shortcut.Save()

    Write-Info "Launching Chairside Ready Alert..."
    Start-Process -FilePath $pythonwPath -ArgumentList "`"$installDir\chairside_ready_alert.py`"" -WorkingDirectory $installDir

    Write-Info "Install complete. Use Desktop shortcut: Chairside Ready Alert"
} catch {
    Write-Host ""
    Write-Host "[Chairside Ready Alert] Installation failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Tips: Run Install Chairside Ready Alert.bat from the folder that contains chairside_ready_alert.py. If the app is open, close it and try again."
    exit 1
}
