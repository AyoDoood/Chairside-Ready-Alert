# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this app is

Chairside Ready Alert is a LAN-based alert/messaging tool for dental offices. Staff at different workstations (Room 1, Room 2, Doctor, Lab, etc.) send one-click alerts to each other. It is a single-file Python GUI app (`chairside_ready_alert.py`) using Tkinter for the UI and raw TCP/UDP sockets for peer-to-peer networking — no server required.

## Running the app

```bash
python3 chairside_ready_alert.py
```

Requires Python 3.11+ with Tkinter. Optional dependencies (for system tray): `pystray`, `pillow`, `pyobjc-framework-Cocoa` (macOS), `cairosvg`, `certifi`.

There is **no test suite, no linter config, and no CI for verifying code correctness** in this repo. Verification is manual: run the app, ideally on two machines (or two loopback instances on different ports) and exchange alerts. The CI that does exist (`.github/workflows/build-windows.yml`) only produces Windows release artifacts on tag push — it does not lint or test.

## Architecture

**Single-file app.** All application logic lives in `chairside_ready_alert.py`. Key classes (line numbers drift quickly — re-grep for `^class ` if they look off):

- `ChairsideReadyAlertApp` (chairside_ready_alert.py:1210) — the Tkinter main window and top-level controller.
- `ConfigStore` (chairside_ready_alert.py:456) — reads/writes `chairside_ready_alert_config.json` from a platform-appropriate user data directory (`~/Library/Application Support/ChairsideReadyAlert/` on macOS, `%LOCALAPPDATA%\ChairsideReadyAlert\` on Windows). Atomic writes via temp file + `os.replace`. See this class for the canonical config shape and defaults.
- `LanDiscovery` (chairside_ready_alert.py:526) — UDP broadcast/listen on port 50506 (`DISCOVERY_PORT`) for zero-config peer discovery. Peers broadcast JSON beacons every 2.5s; stale after 12s.
- `MessageServer` (chairside_ready_alert.py:713) and `MessageClient` (chairside_ready_alert.py:883) — TCP messaging layer; peers connect to each other on port 50505 (`DEFAULT_PORT`) to send alert messages. The `is_server` config flag is vestigial; the app is fully peer-to-peer.
- `SingleInstanceLock` (chairside_ready_alert.py:275) — guards against double-launch (see "Single-instance lock" below).
- Custom Tk widgets — `RoundedCard` (:978), `RoundedLogPanel` (:1034), `RoundedButton` (:1127). These are the visual primitives; theme dicts in `THEMES` (chairside_ready_alert.py:357) drive their colors.
- System tray — `pystray` + `pillow`; loaded lazily with a fallback repair path if missing.

**Threading model (important).** The Tkinter UI runs on the main thread. `LanDiscovery`, `MessageServer`, and `MessageClient` each run on background threads and post events through a `queue.Queue` drained by a `root.after(...)` poll on the UI thread. Any new background work must use the same queue — direct Tk calls from a non-main thread will crash on macOS.

**Themes**: `Modern Blue`, `Sage Clinic`, `Rose Quartz` — defined as dicts in `THEMES`.

**Alert sounds**: 15 synthesized sounds generated at runtime (no audio files needed). Sample rate: 22050 Hz, output via `wave` + platform audio.

**Auto-update**: checks `version.json` on GitHub, downloads individual files listed in `UPDATE_ALLOWED_FILES` (chairside_ready_alert.py:125). Only those files can be updated; arbitrary files are rejected. The manifest URL can be overridden by setting the `CHAIRSIDE_UPDATE_MANIFEST_URL` env var or by adding `update_manifest_url` to the config file; otherwise `UPDATE_MANIFEST_URL_BUILTIN` (chairside_ready_alert.py:124) is used. The in-app updater is gated off in frozen/Store builds (`IS_FROZEN`, chairside_ready_alert.py:121) — Store users get updates from the Microsoft Store instead.

**Frozen-build divergence**: when `IS_FROZEN` is true (PyInstaller Store build), the lock file becomes `chairside_messenger.instance.store.lock` and the focus IPC port becomes `59662` (vs. `59661` for dev). This deliberately lets a developer run `python chairside_ready_alert.py` on the same machine as an installed Store build without the two fighting over the same lock/port — they share `%LOCALAPPDATA%\ChairsideReadyAlert\` for config, but not for instance state.

**Single-instance lock**: `chairside_messenger.instance.lock` (or `.store.lock` when frozen) in the user data directory, using `fcntl.flock` (macOS/Linux) or `msvcrt.locking` (Windows). A second launch focuses the existing window via a local TCP IPC on port `FOCUS_IPC_PORT` (59661 dev / 59662 frozen).

## Repo layout traps

- **`dental_messenger.py`** still sits in the repo root — it is the pre-rename legacy copy and is not referenced by installers or `version.json`. Do not edit it; it is slated for deletion. All changes belong in `chairside_ready_alert.py`.
- **Duplicated Windows installer.** `Chairside Ready Alert Windows Installer/` contains its own copies of `install_chairside_ready_alert.ps1`, `Install Chairside Ready Alert.bat`, and a sample `chairside_ready_alert_config.json`. The **root copies are canonical** (referenced by `version.json`); the subfolder is a packaging bundle. When changing the installer, update both or the bundle will drift.
- **`index.html`** at the repo root is the public GitHub Pages landing page (`https://ayodoood.github.io/Chairside-Ready-Alert/`). It is unrelated to the app — do not confuse it for app UI. The previous `index.md` was replaced by this hand-written HTML page.

## Installers

- **macOS**: `install_chairside_ready_alert_macos.sh` (called by `Install Chairside Ready Alert macOS.command`). Installs to `~/Library/Application Support/ChairsideReadyAlert/`, creates a `.app` bundle on the Desktop. Targets python.org Python 3.12.
- **Windows**: `install_chairside_ready_alert.ps1` (called by `Install Chairside Ready Alert.bat`). Installs to `%LOCALAPPDATA%\ChairsideReadyAlert\`, creates a Desktop shortcut.
- **Windows EXE build** (`Windows Store Submission/`): PyInstaller `--onedir` build. Run `build_windows_exe.bat` on Windows. Output: `dist\ChairsideReadyAlert\ChairsideReadyAlert.exe`.
- **Hosted Windows EXE build** (`.github/workflows/build-windows.yml`): builds all three Microsoft Store architectures (x64, x86, ARM64) on GitHub-hosted runners. Triggered by pushing a `v*` tag or via `workflow_dispatch` from the Actions tab. Each build is uploaded as an artifact named `ChairsideReadyAlert-<arch>`. The ARM64 leg runs on `windows-11-arm`, which is free for public repos but may incur runner-minute charges on private repos.
- **MSIX packaging** (same workflow): after each per-arch EXE build, the workflow stages `Windows Store Submission/AppxManifest.xml`, substitutes `VERSION_PLACEHOLDER` with the 4-segment MSIX version derived from `version.json`, and runs `MakeAppx pack` to produce `ChairsideReadyAlert-<arch>.msix`. If `AppxManifest.xml` still contains any `TODO_*` identity placeholders, the MSIX leg is **skipped with a warning** and only the EXE artifact ships — the three Partner Center identity values (`Identity Name`, `Identity Publisher`, `PublisherDisplayName`) must be pasted in for Store-ready builds. See `Windows Store Submission/MSIX_SUBMISSION.md` for the full submission walk-through.

## Firewall rules (Windows Store build)

The `windows.firewallRules` extension in `AppxManifest.xml` (under `desktop2`) is **not** redundant with the `privateNetworkClientServer` capability and must not be stripped. The capability asks Windows to create best-effort firewall rules; those auto-created rules can land disabled on the Private profile if the user dismisses the "Allow this app?" prompt at first launch, breaking LAN discovery + messaging silently. The explicit `<desktop2:FirewallRules>` block makes the rules deterministic — created enabled, on every install/upgrade, for all profiles. Requires Windows 10 build 19041+; ignored on older Windows (which gets capability-only behavior). If you need to change the LAN ports, update **three** places in lockstep: `DEFAULT_PORT` / `DISCOVERY_PORT` constants in `chairside_ready_alert.py`, and the matching `LocalPortMin`/`LocalPortMax` attributes in the manifest extension.

The app also has a defense-in-depth self-check: `_check_lan_health` (chairside_ready_alert.py, scheduled by `_schedule_lan_health_check` at the end of `start_network`) fires 25 seconds after the LAN starts. If `LanDiscovery` still has zero peers, it opens `_open_lan_health_help_dialog` — a three-step recovery guide with a copy-to-clipboard PowerShell snippet the user can paste into an Administrator PowerShell to re-enable disabled Private-profile rules. (Earlier prototypes used a UAC-elevated `ShellExecuteW`/`runas` button to run the fix in-process; it didn't reliably trigger UAC across all Windows installs, so the dialog is now copy-paste only.) The dialog auto-dismisses when a peer beacon finally arrives — handled inside the `"discovery"` branch of `_process_ui_queue`.

## Microsoft Store distribution model

As of 1.0.29, the Store distribution is a **one-time paid app at $14.99 USD**, not a free download with subscription. Background: the previous Add-on-based subscription model was stuck in a persistent `PEX-CatalogAvailabilityDataNotFound` state on Microsoft's catalog backend that could not be resolved through any configuration. The pivot removed:

- The in-app welcome / paywall window
- `StoreHelper.exe` (a C# helper binary used to invoke the Store purchase flow with the correct window owner)
- `.NET 8` build steps from CI
- The `winrt-*` Python dependencies

If you find any commit, comment, or doc still referencing `StoreHelper`, paywall windows, or subscription Add-ons, it is stale — those code paths have been deleted. The Store purchase is now handled entirely by the Store's standard Get/Buy flow at the listing level; the app itself contains no purchase code and treats every running instance as already-licensed.

## Releasing — version sync checklist

When cutting a release, these three must agree:

1. `APP_VERSION` in `chairside_ready_alert.py` (line 116).
2. `version` in `version.json`.
3. `release_notes` in `version.json` (updated to describe this release).

The `sha256` fields in `version.json` are intentionally empty (verified by HTTPS, not hash). `version.json.example` is a template — do not publish credentials or real URLs there.

After bumping these three, push a `v<version>` tag (e.g. `v1.0.29`) to trigger `.github/workflows/build-windows.yml` and produce x64/x86/ARM64 EXE artifacts plus matching `.msix` packages (when Partner Center identity is filled in) for Microsoft Store submission. The MSIX leg derives its 4-segment version as `<version>.0`, so `version.json` must remain 3-segment SemVer.

## Ownership

Proprietary to Fieldcrest Dental PC. Not intended for emergencies or clinical decision support.
