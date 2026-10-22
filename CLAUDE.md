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
- Custom Tk widgets (1.0.85 refresh) — all plain Canvas drawing, no extra deps: `RoundedCard` (soft-shadow card), `ReadyLog` (the Ready Messages log), `ReadyButton` (gradient Ready button with glow + green "Sent to …" confirmation), `StationChip` (Send to selection, bound to `target_vars` BooleanVars), `VolumeSlider`, `IconButton`, `AppLogo`, `StatusPill` (header "N stations online"), plus `RoundedButton` for dialog buttons. Theme dicts in `THEMES` drive their colors; geometry goes through `_px()` so it scales with font pixel size (Tk draws 1pt ≈ 1.33px on Windows vs ≈ 1px on macOS — fixed pixel sizes tuned on a Mac crowd text on Windows).
- **Ready log ordering rule (user requirement):** incoming Readys always sit at the BOTTOM of the log — the newest as a large highlighted card, the second-newest as a bold row above it. Outgoing "Sent to …" confirmations (drawn arrow, no avatar) are small muted rows inserted ABOVE the two newest incoming Readys (`ReadyLog._insert_outgoing`), never below them. Keep both the visual and the spatial separation. Persistence (`ready_log.txt`, `IN`/`OUT` prefixed lines) is unchanged and replays through the same appenders on launch.
- **Station name (user requirement):** there is NO station-name picker in the main window -- staff mistook it for "who to send to". The header shows "This station: X  Edit"; Edit / Settings -> This Station's Name opens `_open_station_name_dialog` (editable combobox: pick or type; a typed name is added to the shared list like Add or Remove Computers). Fresh installs get the dialog automatically. `self.label_combo` is None except while that dialog is open.
- **Layout (user requirements, 1.0.87):** one-line header (logo + app name left; "This station: X  Edit" + status pill right). TOP row = two separate side-by-side cards with matching tops/bottoms (user requirement): Ready Messages card (title + log, expands) and a Ready button card (~34% of the row width, packed first so it keeps its width). Inside the button card a spacer the height of the "READY MESSAGES" title keeps the button's top level with the log's top (NOT the title); the button fills the rest, so it scales with the panel and its label font scales too. BOTTOM panel = Sound + Volume on one line, then Send to chips. Ready log times / the newest card's age are right-aligned to the log's edge (user tried inline times and rejected them as too close). Send to chips are `place()`d in centered, balanced, wrapping rows by `_layout_target_chips` inside a FIXED-height area that fits two rows (`_send_to_area_height`; one row is vertically centered).
- **Windows DPI awareness (1.0.90):** `main()` calls `_enable_windows_dpi_awareness()` (system-aware, not per-monitor) before `tk.Tk()`, so text is crisp instead of bitmap-stretched at 125%/150%. Tk then draws in physical pixels: `_px()` sizes scale automatically via `tk scaling`, but any PLAIN pixel number (padx/pady/padding, wraplength, dialog geometry, canvas line widths, window min/default sizes) must go through `_dz()` / `* _DPI_ZOOM` or it shrinks on scaled displays. Animations are drawn in 96-dpi units and scaled up per frame by `ready_animations.set_ui_zoom` / `_zoom_frame`.
- **Window sizing:** before 1.0.90 Tk on Windows was not DPI-aware, so 125-150% display scaling enlarged (and blurred) the window. `_fit_window_to_content` sizes the window from its real requested size (log request = newest card + 3 rows), capped by `_usable_screen_size` (work area), and sets minsize so 4 log lines stay visible. The bottom panel lives in a `bottom` frame packed `before=top_row` so the log shrinks first on screens too short even for the minimum. Measured requirement: ~511 Tk units tall at Windows scaling (was 786 in 1.0.86). **Windows text size (user requirement):** the user prefers the Mac look; on Windows `_init_ui_scale` multiplies `tk scaling` by `WINDOWS_TEXT_SCALE` (0.85) so every font and `_px()` size is ~15% smaller (window min ~640x442). Change that one constant to tune Windows text size; Mac is untouched.
- System tray — `pystray` + `pillow`; loaded lazily with a fallback repair path if missing.

**Threading model (important).** The Tkinter UI runs on the main thread. `LanDiscovery`, `MessageServer`, and `MessageClient` each run on background threads and post events through a `queue.Queue` drained by a `root.after(...)` poll on the UI thread. Any new background work must use the same queue — direct Tk calls from a non-main thread will crash on macOS.

**Themes**: `Modern Blue`, `Sage Clinic`, `Rose Quartz`, `Ocean`, `Midnight` (dark) — defined as dicts in `THEMES`. `_apply_theme` also recolors combobox dropdown lists (needed for Midnight) and calls `ready_animations.set_palette()` so the stick figure uses the theme's `ink`.

**Stick-figure animations** (`ready_animations.py`): every animation draws through `draw_figure_pose()` (tapered "ink" style, ground shadow, squash & stretch, expressive face; `_draw_figure_pose_classic` is the pre-1.0.85 wireframe) plus an `FX` particle system drawn once per frame by `AnimationPlayer`. The player runs at ~60fps and drops to ~30fps for the rest of a play if frames are slow. The overlay is a transparent Toplevel (`-transparentcolor magenta` on Windows, `systemTransparent` on macOS).

**Alert sounds**: 15 synthesized sounds generated at runtime (no audio files needed). Sample rate: 22050 Hz, output via `wave` + platform audio.

**Auto-update**: checks `version.json` on GitHub, downloads individual files listed in `UPDATE_ALLOWED_FILES` (chairside_ready_alert.py:125). Only those files can be updated; arbitrary files are rejected. The manifest URL can be overridden by setting the `CHAIRSIDE_UPDATE_MANIFEST_URL` env var or by adding `update_manifest_url` to the config file; otherwise `UPDATE_MANIFEST_URL_BUILTIN` (chairside_ready_alert.py:124) is used. The in-app updater is gated off in frozen/Store builds (`IS_FROZEN`, chairside_ready_alert.py:121) — Store users get updates from the Microsoft Store instead.

**Frozen-build divergence**: when `IS_FROZEN` is true (PyInstaller Store build), the lock file becomes `chairside_messenger.instance.store.lock` and the focus IPC port becomes `59662` (vs. `59661` for dev). This deliberately lets a developer run `python chairside_ready_alert.py` on the same machine as an installed Store build without the two fighting over the same lock/port — they share `%LOCALAPPDATA%\ChairsideReadyAlert\` for config, but not for instance state.

**Single-instance lock**: `chairside_messenger.instance.lock` (or `.store.lock` when frozen) in the user data directory, using `fcntl.flock` (macOS/Linux) or `msvcrt.locking` (Windows). A second launch focuses the existing window via a local TCP IPC on port `FOCUS_IPC_PORT` (59661 dev / 59662 frozen).

## Repo layout traps

- **`dental_messenger.py`** (the pre-rename legacy copy) and `dental_messenger_config.json` were **deleted in 1.0.82** along with the stray tracked `__pycache__/dental_messenger.cpython-311.pyc`. They were never referenced by installers or `version.json`. All app logic lives in `chairside_ready_alert.py`; if you see references to `dental_messenger` in old commits, they are historical.
- **Duplicated Windows installer.** `Chairside Ready Alert Windows Installer/` contains its own copies of `install_chairside_ready_alert.ps1`, `Install Chairside Ready Alert.bat`, and a sample `chairside_ready_alert_config.json`. The **root copies are canonical** (referenced by `version.json`); the subfolder is a packaging bundle. When changing the installer, update both or the bundle will drift.
- **`index.html`** at the repo root is the public GitHub Pages landing page (`https://ayodoood.github.io/Chairside-Ready-Alert/`). It is unrelated to the app — do not confuse it for app UI. The previous `index.md` was replaced by this hand-written HTML page.

## Installers

- **macOS**: `install_chairside_ready_alert_macos.sh` (called by `Install Chairside Ready Alert macOS.command`). Installs to `~/Library/Application Support/ChairsideReadyAlert/`, creates a `.app` bundle on the Desktop. Targets python.org Python 3.12.
- **Windows**: `install_chairside_ready_alert.ps1` (called by `Install Chairside Ready Alert.bat`). Installs to `%LOCALAPPDATA%\ChairsideReadyAlert\`, creates a Desktop shortcut.
- **Windows EXE build** (`Windows Store Submission/`): PyInstaller `--onedir` build. Run `build_windows_exe.bat` on Windows. Output: `dist\ChairsideReadyAlert\ChairsideReadyAlert.exe`.
- **Hosted Windows EXE build** (`.github/workflows/build-windows.yml`): builds all three Microsoft Store architectures (x64, x86, ARM64) on GitHub-hosted runners. Triggered by pushing a `v*` tag or via `workflow_dispatch` from the Actions tab. Each build is uploaded as an artifact named `ChairsideReadyAlert-<arch>`. The ARM64 leg runs on `windows-11-arm`, which is free for public repos but may incur runner-minute charges on private repos.
- **MSIX packaging** (same workflow): after each per-arch EXE build, the workflow stages `Windows Store Submission/AppxManifest.xml`, substitutes `VERSION_PLACEHOLDER` with the 4-segment MSIX version derived from `version.json`, and runs `MakeAppx pack` to produce `ChairsideReadyAlert-<arch>.msix`. If `AppxManifest.xml` still contains any `TODO_*` identity placeholders, the MSIX leg is **skipped with a warning** and only the EXE artifact ships — the three Partner Center identity values (`Identity Name`, `Identity Publisher`, `PublisherDisplayName`) must be pasted in for Store-ready builds. See `Windows Store Submission/MSIX_SUBMISSION.md` for the full submission walk-through.

## Windows direct-installer (PowerShell) gotchas

Two bugs in the GitHub direct-installer path (`install_chairside_ready_alert.ps1`, `uninstall_chairside_ready_alert.ps1`, and their bundled copies under `Chairside Ready Alert Windows Installer/`) each shipped, broke an end-user install/uninstall, and required a follow-up release. The relevant code has no automated test, so the safeguards live here.

**Keep `.ps1` files pure ASCII.** Use `--` instead of `—`, `...` instead of `…`, straight quotes instead of curly quotes. The `.ps1` files are saved as BOM-less UTF-8, but Windows PowerShell 5.1 reads BOM-less `.ps1` files in the **system default codepage** (Windows-1252 on US installs) — not UTF-8. The 3-byte UTF-8 sequence for `—` (`E2 80 94`) gets misdecoded as three Windows-1252 characters, the last of which is U+201D (curly right-double-quote). That stray quote drops the tokenizer into an unterminated-string state, and any later here-string opener (`@"..."@`) stops being recognized — the script bails with `Missing using directive` / `A 'using' statement must appear before any other statements in a script.` and the install never runs. (1.0.34 shipped this bug in two comment lines deep in `install_chairside_ready_alert.ps1`; 1.0.35 made the files pure ASCII.) Adding a UTF-8 BOM would also fix it, but editors quietly strip BOMs on save — we'd be one bad save away from regressing. ASCII is the durable answer. This rule applies only to `.ps1`; Python source, `version.json`, Markdown, and `.bat` files all read with explicit/forced encodings and are unaffected.

**Uninstaller "kill running app" step must anchor on `chairside_ready_alert.py`, not the loose substring.** When Apps & Features invokes `uninstall_chairside_ready_alert.ps1`, the script copies itself to `%TEMP%` and spawns a child PowerShell against the copy (so the install dir can be deleted while the script is running — see the relocation block near the top of the file). That child's command line is `powershell.exe -File "...\uninstall_chairside_ready_alert.ps1" -Force`, which contains the substring `chairside_ready_alert`. A `Where-Object { $_.CommandLine -match "chairside_ready_alert" }` filter therefore matches the uninstaller's own process; `Stop-Process -Force` then kills the uninstaller before any cleanup step runs. Apps & Features sees the relocation parent exit `0` and considers the uninstall a success, but the `HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ChairsideReadyAlert` registry key (plus everything else) persists forever, so the entry never disappears from "Installed apps". (1.0.34 shipped this bug; 1.0.35 fixed it.) The current filter anchors on `'chairside_ready_alert\.py'` — only the actual Python process has the `.py` extension — plus `-notmatch 'uninstall_chairside_ready_alert'` and `$_.ProcessId -ne $PID` as belt-and-suspenders. Do not loosen back to a substring match. The general rule for process matching anywhere in this repo: never use a substring filter when an anchored filter works.

## Firewall rules (Windows Store build)

The `windows.firewallRules` extension in `AppxManifest.xml` (under `desktop2`) is **not** redundant with the `privateNetworkClientServer` capability and must not be stripped. The capability asks Windows to create best-effort firewall rules; those auto-created rules can land disabled on the Private profile if the user dismisses the "Allow this app?" prompt at first launch, breaking LAN discovery + messaging silently. The explicit `<desktop2:FirewallRules>` block makes the rules deterministic — created enabled, on every install/upgrade, for all profiles. Requires Windows 10 build 19041+; ignored on older Windows (which gets capability-only behavior). If you need to change the LAN ports, update **three** places in lockstep: `DEFAULT_PORT` / `DISCOVERY_PORT` constants in `chairside_ready_alert.py`, and the matching `LocalPortMin`/`LocalPortMax` attributes in the manifest extension.

**Placement gotcha**: `windows.firewallRules` is a **package-level** extension and must sit as a direct child of `<Package>` (after `</Applications>`, before `<Capabilities>`). Nesting it inside `<Application>` is rejected by `MakeAppx` with error `0x80080204` — "The 'windows.firewallRules' extension should be declared under <Package> extensions." (The 1.0.30 build hit this and never produced MSIX artifacts; 1.0.31 is the fix-up release.)

The app also has a defense-in-depth self-check: `_check_lan_health` (chairside_ready_alert.py, scheduled by `_schedule_lan_health_check` at the end of `start_network`) fires 25 seconds after the LAN starts. If `LanDiscovery` still has zero peers, it opens `_open_lan_health_help_dialog` — a three-step recovery guide with a copy-to-clipboard PowerShell snippet the user can paste into an Administrator PowerShell to re-enable disabled Private-profile rules. (Earlier prototypes used a UAC-elevated `ShellExecuteW`/`runas` button to run the fix in-process; it didn't reliably trigger UAC across all Windows installs, so the dialog is now copy-paste only.) The dialog auto-dismisses when a peer beacon finally arrives — handled inside the `"discovery"` branch of `_process_ui_queue`.

## Microsoft Store distribution model

As of 1.0.29, the Store distribution is a **one-time paid app at $14.99 USD**, not a free download with subscription. Background: the previous Add-on-based subscription model was stuck in a persistent `PEX-CatalogAvailabilityDataNotFound` state on Microsoft's catalog backend that could not be resolved through any configuration. The pivot removed:

- The in-app welcome / paywall window
- `StoreHelper.exe` (a C# helper binary used to invoke the Store purchase flow with the correct window owner)
- `.NET 8` build steps from CI
- The `winrt-*` Python dependencies

If you find any commit, comment, or doc still referencing `StoreHelper`, paywall windows, or subscription Add-ons, it is stale — those code paths have been deleted. The Store purchase is still handled entirely by the Store's standard Get/Buy flow at the listing level; the app itself never invokes a purchase API. **However**, as of the trial-gating change, the app does enforce a 7-day in-app trial when `IS_FROZEN` is True and consults `Windows.Services.Store.StoreContext.GetAppLicenseAsync()` (via the `winsdk` package) to recognize a real purchase — see "Trial / paid-license gating" below. Dev runs are always treated as licensed.

## Trial / paid-license gating (frozen Store builds only)

Frozen builds (`IS_FROZEN == True`) enforce a `TRIAL_DAYS`-day in-app trial anchored on `first_launch_iso`, a date string stamped into the config the first time the app launches without one. Helpers `_trial_days_remaining()` and `_query_store_license_paid()` near the top of `chairside_ready_alert.py` are the policy; the App-level methods (`_trial_status`, `_is_locked_out`, `_apply_trial_state_to_ui`, `_kick_store_license_refresh`, etc., grouped under `_show_about_dialog`) are the wiring. Dev runs short-circuit every helper to "no enforcement, paid=True", so `python chairside_ready_alert.py` is unaffected.

- Reminder dialogs fire **once** per threshold in `TRIAL_REMINDER_DAYS` (currently 6/3/1 days). Each fire is recorded in `trial_reminders_shown` in the config so it can't pop again on the next launch.
- Lockout, when expired and unpaid: the Ready button shows a "buy now?" modal (`_prompt_trial_expired_dialog`) instead of sending; incoming Ready chats are dropped silently and recorded only in the diagnostic log (peers should not see a bounce that reveals the receiver is unlicensed). The LAN stack (discovery + TCP mesh) keeps running so the user still appears online and the unlock path doesn't need a restart.
- The Store license check (`_query_store_license_paid`) requires the **`winsdk`** Python package. It is imported lazily and tolerantly: if missing, the check returns `None` (unknown) and the cached `paid_unlocked` flag is kept — **fail-closed** (an unpaid user stays locked; an unknown/flaky Store call never falls open to paid). `winsdk` **is** bundled: `Windows Store Submission/build_windows_exe.ps1` pip-installs it and passes `--collect-all winsdk`, and the GitHub Actions workflow (`.github/workflows/build-windows.yml`) invokes that script for all three arches. Verified in the v1.0.82 CI build — the `winsdk\windows\services\store` module + native `_winrt.pyd` shipped inside the x64/x86/arm64 MSIX. Do **not** drop the `winsdk` pip install or the `--collect-all winsdk` flag: without it, `_query_store_license_paid` always returns `None`, `paid_unlocked` never flips True, and **even paying users get locked out at trial end with no in-app unlock path**.
- The Buy button prefers `ms-windows-store://pdp/?ProductId=…` when `STORE_PRODUCT_ID` is filled in; it falls back to `STORE_PRODUCT_WEB_URL` while the constant is empty. Currently `STORE_PRODUCT_ID = "9NKDHB58JJC5"`.
- **Keep the Partner Center listing's free trial ENABLED — do not switch it to one-time-paid-with-no-trial.** A Store free trial is the only way to let customers install a *paid* app without paying up front; the app's in-app `TRIAL_DAYS` countdown is what actually *enforces* the time limit, and the Store free trial just permits the free download. The license check is `is_active and not is_trial`: during the Store free trial `is_trial` is `True`, so the app treats the user as unpaid and the in-app countdown governs; when the user buys, the Store auto-converts the license to `is_trial=False`, so the app reads them as paid and unlocks live. A Store free trial therefore does **not** wrongly lock out buyers. If Partner Center exposes a trial duration, set it to `TRIAL_DAYS` (7) so the Store's messaging matches what the app enforces. (Removing the free trial would force upfront payment and kill the try-before-buy funnel — verified against Microsoft's UWP monetization docs.)
- Trial state is config-backed (deleting the config or reinstalling resets the trial). This is intentional for the chosen "install-date + Store API fallback" design — the Store API is the authoritative paid-state signal; the install-date is just a countdown. Don't add hidden-file or registry-based tamper resistance without explicit ask.

## Releasing — version sync checklist

When cutting a release, these three must agree:

1. `APP_VERSION` in `chairside_ready_alert.py` (line 116).
2. `version` in `version.json`.
3. `release_notes` in `version.json` (updated to describe this release).

The `sha256` fields in `version.json` are intentionally empty (verified by HTTPS, not hash). `version.json.example` is a template — do not publish credentials or real URLs there.

After bumping these three, push a `v<version>` tag (e.g. `v1.0.29`) to trigger `.github/workflows/build-windows.yml` and produce x64/x86/ARM64 EXE artifacts plus matching `.msix` packages (when Partner Center identity is filled in) for Microsoft Store submission. The MSIX leg derives its 4-segment version as `<version>.0`, so `version.json` must remain 3-segment SemVer.

## Ownership

Proprietary to Fieldcrest Dental PC. Not intended for emergencies or clinical decision support.
