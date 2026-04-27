Chairside Ready Alert - One-Click Install (Windows + macOS)

If you run into trouble during install or use, check the FAQ first:
  https://ayodoood.github.io/Chairside-Ready-Alert/FAQ.html

License / ownership notice:
- This software and project branding are proprietary to Fieldcrest Dental PC unless otherwise stated.
- Third-party components and license summary: see THIRD_PARTY_NOTICES.md in this repository.
- For license/permission requests: support@fieldcrestdental.com

Safety / intended-use notice:
- This app is an operational convenience messaging tool only.
- Not intended for emergencies, life-safety alerts, patient monitoring, or clinical decision support.
- Always maintain independent clinical and emergency communication procedures.

Main behavior after install:
- No fixed server IP or subnet: works with any typical LAN addresses (192.168.1.x, 192.168.5.x, 10.x.x.x, etc.) — each PC listens and finds others via UDP discovery on the same Wi-Fi / Ethernet segment.
- Every computer runs the same app (Windows or Mac); give each machine a unique station name on the main screen under Station Setup (e.g. Room 1, Doctor). There is no separate first-run wizard.
- Optional manual peer IP addresses in Settings if your network blocks broadcast discovery.
- TCP port (default 50505) can be changed in Settings — use the same port on every machine.
- Start/Stop controls are in the Settings menu (not on the main screen).
- The sender controls which alert sound plays on the receiving computer.
- The receiver controls the alert volume locally.
- 15 alert sounds are available per workstation.
- Alert volume slider is available per workstation.
- Default targets can be configured per workstation via the Default menu.
- Incoming messages bring the main window to front + blink + sound (no extra popup windows).
- Quick Ready access is in the system tray/menu bar icon ("R"): use it to send Ready, show/hide main window, or close the app.
- If two computers use the same station name, a small inline warning appears under Station name to help avoid target-list confusion.

Where settings are saved (station name, alert sound, volume, theme, etc.):
- Windows: %LOCALAPPDATA%\ChairsideReadyAlert\chairside_ready_alert_config.json
  (Full path example: C:\Users\<YourName>\AppData\Local\ChairsideReadyAlert\chairside_ready_alert_config.json)
  This is the same folder the installer uses for chairside_ready_alert.py, so settings persist when you re-run the installer to update the app.
- macOS: ~/Library/Application Support/ChairsideReadyAlert/chairside_ready_alert_config.json

Automatic update setup (optional):
- In each machine's config file, add:
  "update_manifest_url": "https://your-hosted-url/version.json"
- Or set environment variable CHAIRSIDE_UPDATE_MANIFEST_URL to that URL.
- In the app, use Settings > Check for updates...
- Manifest format: see version.json.example in this folder.
- For automatic install, manifest can include either:
  - top-level download_url (+ optional sha256) for chairside_ready_alert.py only, or
  - files.{filename}.url (+ optional sha256) to update multiple files.
- If download_url is missing, the app can still open release_page_url.

Windows install files:
- Install Chairside Ready Alert.bat
- install_chairside_ready_alert.ps1
- chairside_ready_alert.py
- Optional custom tray icon: logo.svg (or logo.png) in the same folder as installer files
- Optional offline Python installer: python-3.12.8-amd64.exe (installer auto-downloads from python.org when missing)

Windows install steps:
1) Put all Windows install files in one folder (the .bat, install_chairside_ready_alert.ps1, and chairside_ready_alert.py must stay together — do not copy only the .bat to the Desktop).
2) Double-click: Install Chairside Ready Alert.bat
3) Installer automatically closes any running Chairside Ready Alert instance (including tray) before updating.
4) Wait for setup to complete.
5) Launch from any of:
   - Desktop shortcut: Chairside Ready Alert
   - Start Menu: Chairside Ready Alert
   - Search: type "Chairside" and press Enter

To uninstall on Windows:
- Settings -> Apps -> Installed apps -> Chairside Ready Alert -> Uninstall
- Settings (chairside_ready_alert_config.json) are preserved by default. To remove them too, run uninstall_chairside_ready_alert.ps1 manually with the -RemoveSettings flag.

macOS install files:
- Install Chairside Ready Alert macOS.command (or run install_chairside_ready_alert_macos.sh — works when ZIP strips +x, see below)
- install_chairside_ready_alert_macos.sh (use this if the .command file will not open from the Desktop after unzipping)
- chairside_ready_alert.py
- Optional custom tray icon: logo.svg (or logo.png) in the same folder as installer files
- Optional (offline): python-3.12.8-macos11.pkg — same file as on python.org; place next to the installer to skip the download.

macOS install steps:
1) Put the macOS install files in one folder.
2) Right-click Install Chairside Ready Alert macOS.command and choose Open. If the .command was downloaded as a GitHub ZIP and will not run (missing “execute” permission, or macOS will not open it), open Terminal, use cd to the folder, then run:  bash install_chairside_ready_alert_macos.sh
3) If you prefer fixing permissions instead:  chmod +x "Install Chairside Ready Alert macOS.command"  then run it (double-click or: open "Install Chairside Ready Alert macOS.command").
4) If macOS blocks it, open System Settings > Privacy & Security and allow it.
5) If no suitable Python with Tkinter is found, the installer downloads the official python.org macOS package (includes Tcl/Tk) or uses the optional .pkg next to it, then installs it — you will be asked for an administrator password once.
6) Installer automatically closes any running Chairside Ready Alert instance (including menu-bar/tray) before updating.
7) Launch from any of:
   - ~/Applications/Chairside Ready Alert.app (the canonical install location — also visible in Launchpad)
   - Spotlight: Cmd+Space, type "Chairside"
   - Desktop: "Chairside Ready Alert" (a Finder alias to the canonical bundle)

To uninstall on macOS:
- Drag ~/Applications/Chairside Ready Alert.app to the Trash.
- Optionally also delete ~/Library/Application Support/ChairsideReadyAlert/ to remove settings.
