Chairside Messenger - One-Click Install (Windows + macOS)

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

Where settings are saved (station name, alert sound, volume, theme, etc.):
- Windows: %LOCALAPPDATA%\DentalMessenger\dental_messenger_config.json
  (Full path example: C:\Users\<YourName>\AppData\Local\DentalMessenger\dental_messenger_config.json)
  This is the same folder the installer uses for dental_messenger.py, so settings persist when you re-run the installer to update the app.
- macOS: ~/Library/Application Support/DentalMessenger/dental_messenger_config.json

Automatic update setup (optional):
- In each machine's config file, add:
  "update_manifest_url": "https://your-hosted-url/version.json"
- Or set environment variable CHAIRSIDE_UPDATE_MANIFEST_URL to that URL.
- In the app, use Settings > Check for updates...
- Manifest format: see version.json.example in this folder.
- For automatic install, manifest can include either:
  - top-level download_url (+ optional sha256) for dental_messenger.py only, or
  - files.{filename}.url (+ optional sha256) to update multiple files.
- If download_url is missing, the app can still open release_page_url.

Windows install files:
- Install Dental Messenger.bat
- install_dental_messenger.ps1
- dental_messenger.py
- Optional custom tray icon: logo.svg (or logo.png) in the same folder as installer files
- Optional offline Python installer: python-3.12.8-amd64.exe (installer auto-downloads from python.org when missing)

Windows install steps:
1) Put all Windows install files in one folder (the .bat, install_dental_messenger.ps1, and dental_messenger.py must stay together — do not copy only the .bat to the Desktop).
2) Double-click: Install Dental Messenger.bat
3) Installer automatically closes any running Chairside Messenger instance (including tray) before updating.
4) Wait for setup to complete.
5) Open Desktop shortcut: Chairside Messenger

macOS install files:
- Install Dental Messenger macOS.command
- dental_messenger.py
- Optional custom tray icon: logo.svg (or logo.png) in the same folder as installer files
- Optional (offline): python-3.12.8-macos11.pkg — same file as on python.org; place next to the installer to skip the download.

macOS install steps:
1) Put the macOS install files in one folder.
2) Right-click Install Dental Messenger macOS.command and choose Open.
3) If macOS says you do not have permission, open Terminal and run: chmod +x "/path/to/Install Dental Messenger macOS.command" then run it again.
4) If macOS blocks it, open System Settings > Privacy & Security and allow it.
5) If no suitable Python with Tkinter is found, the installer downloads the official python.org macOS package (includes Tcl/Tk) or uses the optional .pkg next to it, then installs it — you will be asked for an administrator password once.
6) Installer automatically closes any running Chairside Messenger instance (including menu-bar/tray) before updating.
7) Launch from Desktop: Chairside Messenger.app (double-click the icon).
