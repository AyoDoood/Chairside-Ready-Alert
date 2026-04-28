# Uninstaller for the direct-installer (non-Store) build of Chairside Ready Alert.
# Removes: install dir, Desktop + Start Menu shortcuts, autostart .bat, registry entry.
# Preserves: settings (chairside_ready_alert_config.json) unless -RemoveSettings is given.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File uninstall_chairside_ready_alert.ps1
#   powershell -ExecutionPolicy Bypass -File uninstall_chairside_ready_alert.ps1 -RemoveSettings
#   powershell -ExecutionPolicy Bypass -File uninstall_chairside_ready_alert.ps1 -Force        # skip confirmation
#
# Apps & Features ("Uninstall" button) calls this with -Force.

param(
    [switch]$RemoveSettings,
    [switch]$Force
)

$ErrorActionPreference = "Continue"

$installDir         = Join-Path $env:LOCALAPPDATA "ChairsideReadyAlert"
$desktopShortcut    = Join-Path ([Environment]::GetFolderPath("Desktop")) "Chairside Ready Alert.lnk"
$startMenuFolder    = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Chairside Ready Alert"
$autostartBat       = Join-Path ([Environment]::GetFolderPath("Startup")) "Chairside Ready Alert.bat"
$regKey             = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ChairsideReadyAlert"

function Write-Info($msg) { Write-Host "[Uninstall] $msg" }

# When triggered by Apps & Features, this script is running FROM inside the install
# dir we're about to delete — Windows won't let us delete an executing file. Relocate
# to %TEMP% on first run, then re-exec from there.
if ($PSCommandPath -and ($PSCommandPath -like "$installDir\*")) {
    $tempScript = Join-Path $env:TEMP ("uninstall_chairside_ready_alert_" + [System.Guid]::NewGuid().ToString() + ".ps1")
    Copy-Item -LiteralPath $PSCommandPath -Destination $tempScript -Force
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$tempScript`"", "-Force")
    if ($RemoveSettings) { $argList += "-RemoveSettings" }
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList
    exit 0
}

if (-not $Force) {
    $msg = "Uninstall Chairside Ready Alert?"
    if (-not $RemoveSettings) {
        $msg += " (settings will be kept)"
    } else {
        $msg += " (settings will be removed)"
    }
    $confirm = Read-Host "$msg (y/N)"
    if ($confirm -notmatch '^[yY]') {
        Write-Info "Cancelled."
        exit 0
    }
}

Write-Info "Stopping any running Chairside Ready Alert instance..."
try {
    Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "chairside_ready_alert" } |
        ForEach-Object {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
} catch {}

Start-Sleep -Milliseconds 500

Write-Info "Removing Desktop shortcut..."
Remove-Item -LiteralPath $desktopShortcut -Force -ErrorAction SilentlyContinue

Write-Info "Removing Start Menu folder..."
if (Test-Path -LiteralPath $startMenuFolder) {
    Remove-Item -LiteralPath $startMenuFolder -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Info "Removing autostart entry..."
Remove-Item -LiteralPath $autostartBat -Force -ErrorAction SilentlyContinue

Write-Info "Removing Apps & Features registry entry..."
Remove-Item -Path $regKey -Recurse -Force -ErrorAction SilentlyContinue

Write-Info "Removing Windows Firewall rules..."
$fwScript = @"
`$ErrorActionPreference = 'Continue'
Get-NetFirewallRule -Group 'Chairside Ready Alert' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName 'Chairside Ready Alert (Inbound TCP 50505)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName 'Chairside Ready Alert (Inbound UDP 50506)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
"@
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    try { Invoke-Expression $fwScript } catch {}
} else {
    $tempFw = Join-Path $env:TEMP ("chairside_fw_uninstall_" + [System.Guid]::NewGuid().ToString() + ".ps1")
    try {
        Set-Content -LiteralPath $tempFw -Value $fwScript -Encoding UTF8
        Start-Process -FilePath "powershell.exe" `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$tempFw`"") `
            -Verb RunAs -Wait -ErrorAction Stop | Out-Null
    } catch {
        Write-Info "Skipped firewall rule cleanup (UAC declined). Remove them manually from 'Windows Defender Firewall with Advanced Security' if desired."
    } finally {
        Remove-Item -LiteralPath $tempFw -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $installDir)) {
    Write-Info "Install directory is already gone."
} elseif ($RemoveSettings) {
    Write-Info "Removing install directory and settings..."
    Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Write-Info "Removing install directory contents (keeping settings)..."
    Get-ChildItem -LiteralPath $installDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne "chairside_ready_alert_config.json" } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Info "Done."
if (-not $RemoveSettings -and (Test-Path -LiteralPath $installDir)) {
    Write-Info "Settings preserved at: $installDir\chairside_ready_alert_config.json"
    Write-Info "To remove them too, re-run with -RemoveSettings."
}
