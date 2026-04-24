Chairside Ready Alert - One-Click Install (Windows + macOS)

Main behavior after install:
- No fixed server IP or subnet: works with any typical LAN addresses (192.168.1.x, 192.168.5.x, 10.x.x.x, etc.) — each PC listens and finds others via UDP discovery on the same Wi-Fi / Ethernet segment.
- Every computer runs the same app (Windows or Mac); give each machine a unique label (e.g. Room 1, Doctor).
- Optional manual peer IP addresses in Settings if your network blocks broadcast discovery.
- TCP port (default 50505) can be changed in Settings — use the same port on every machine.
- Start/Stop controls are in the Settings menu (not on the main screen).
- The sender controls which alert sound plays on the receiving computer.
- The receiver controls the alert volume locally.
- 15 alert sounds are available per workstation.
- Alert volume slider is available per workstation.
- Default targets can be configured per workstation via the Default menu.
- Incoming messages bring the main window to front + blink + sound (no extra popup windows).

Windows install files:
- Install Chairside Ready Alert.bat
- install_chairside_ready_alert.ps1
- chairside_ready_alert.py
- python-3.11.9-amd64.exe  (download from python.org if not included)

Windows install steps:
1) Put all Windows install files in one folder.
2) Double-click: Install Chairside Ready Alert.bat
3) Wait for setup to complete.
4) Open Desktop shortcut: Chairside Ready Alert

macOS install files:
- Install Chairside Ready Alert macOS.command
- install_chairside_ready_alert_macos.sh (use with: bash install_chairside_ready_alert_macos.sh  if the .command from a GitHub ZIP will not run)
- chairside_ready_alert.py
- Optional (offline): python-3.12.8-macos11.pkg — same file as on python.org; place next to the installer to skip the download.

macOS install steps:
1) Put the macOS install files in one folder.
2) Open Install Chairside Ready Alert macOS.command (right-click, Open) — or, from Terminal in that folder:  bash install_chairside_ready_alert_macos.sh
3) If macOS blocks it, open System Settings > Privacy & Security and allow it.
4) If no suitable Python with Tkinter is found, the installer downloads the official python.org macOS package (includes Tcl/Tk) or uses the optional .pkg next to it, then installs it — you will be asked for an administrator password once.
5) Launch from Desktop: Chairside Ready Alert.app (double-click the icon).
