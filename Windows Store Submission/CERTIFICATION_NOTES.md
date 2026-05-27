# Notes for Microsoft Store Certification

This file contains the exact text to paste into the **"Notes for certification"**
field of the Partner Center submission, plus context for what the certification
team should and should not expect to see.

The app is a LAN messaging tool sold as a **one-time paid app** at $14.99 USD
on the Microsoft Store with a 7-day free trial configured at the listing
level. There is no subscription, no in-app purchase API call, no paywall
window inside the app. Microsoft Store handles the Get/Try/Buy flow at the
listing level.

The LAN messaging core feature requires TWO OR MORE DEVICES on the same local
subnet and cannot be exercised on a single test VM. Reviewers can only
confirm the app launches, renders, and quits cleanly in a single-VM
environment.

The current submission (1.0.60) is the first Store update since 1.0.31 and
contains 29 patch releases worth of fixes, UI polish, a stick-figure
animation feature, the enforcement of the 7-day trial in-app, and a macOS
sleep-recovery fix. See the "What changed since 1.0.31" section further
down for the full summary. The macOS sleep fix is platform-specific (no
visible difference for a Store reviewer testing on Windows) but it's part
of the same submission package.

---

## Paste-into-Partner-Center text (1.0.60 submission)

```
Chairside Ready Alert is a LAN-based messaging tool for dental practices, sold as a one-time paid app at USD 14.99 with a 7-day free trial configured at the Store listing level. There is no subscription, no in-app purchase API call, and no paywall window inside the app. Microsoft Store handles all commerce via the standard Get / Try / Buy flow at the listing level.

This is the first update since 1.0.31 (29 patch releases). The most reviewer-relevant change is that 1.0.59 enforces the 7-day free trial in-app. Releases 1.0.29 - 1.0.58 treated every running instance as licensed and did not enforce the Store-side trial period; 1.0.59 stamps a first-launch date into the local user-data config and counts down 7 days from there.

A reviewer running the app for the first time will see no countdown banner and no lockout - the trial window stays well above the 6-day reminder threshold for the duration of any reasonable review. Nothing the reviewer needs to do beyond the usual launch / render / exit smoke test.

LAN messaging - same since 1.0.20:
- UDP broadcast on port 50506 for peer discovery
- TCP on port 50505 for the Ready alert messages
- Requires two or more devices on the same subnet
- Single-VM test will see an empty peer list and cannot exercise send / receive. This is EXPECTED and not a bug.

In a single-VM test environment, please confirm only:
1. The app launches and the main window renders.
2. The tray icon appears in the notification area.
3. The Settings menu opens.
4. Right-click on the tray icon shows: Send Ready, Show Main Window, Hide Main Window, Close.
5. Selecting Close from the tray menu shuts the app down cleanly.

License detection - new in 1.0.59:
The app calls Windows.Services.Store.StoreContext.GetAppLicenseAsync() via the bundled winsdk Python package to detect a real purchase. This is an in-process WinRT call, NOT an outbound internet connection. When the API reports an active non-trial license (i.e. the customer has purchased), the app clears the in-app trial lockout immediately. A Buy completed in another window unlocks the app within ~25 seconds without needing a restart. An "I purchased - refresh" button on the expiration banner re-runs the check on demand.

If the reviewer test account has not purchased, GetAppLicenseAsync returns is_trial=True and the app remains in the in-app trial state. If the reviewer's account simply does not have a license at all (e.g. internal test fixture), GetAppLicenseAsync returns is_active=False and the app behaves the same as an unpurchased trial user. Neither state blocks the smoke test above.

Stick-figure cameo animation - new since 1.0.41:
A small stick-figure may briefly cross the Ready button area after a qualifying outgoing Ready send. This is a whimsy/easter-egg feature with no audio, rate-limited at the production trigger level. It never blocks UI input. State persists across restarts so closing/reopening the app cannot bypass the rate limit.

Fresh-install station prompt - new in 1.0.58:
On a brand-new install, the Station name dropdown shows "Please choose the name of this Station" and the Ready button stays disabled until a name is picked. This is intentional - preventing every fresh install from broadcasting the same default name and colliding with the office's existing station of that name on the LAN. The placeholder is never written to config or broadcast.

Capabilities declared in the AppxManifest:
- privateNetworkClientServer (required for LAN UDP broadcast + TCP listen)
- runFullTrust (PyInstaller Python/Tkinter desktop bridge - standard)
- windows.firewallRules extension under desktop2 (declares LAN rules deterministically so they are enabled on all profiles regardless of the first-launch "Allow this app?" prompt outcome)

Network activity:
- LAN UDP/TCP on ports 50505 and 50506
- Microsoft Store license check via in-process WinRT (no network)
- NO outbound internet connection in normal operation
- The Store EXE has no in-app self-update; updates ship through the Store channel only (the in-app updater is gated off when IS_FROZEN is true)

New runtime dependency bundled in this package:
- winsdk (Microsoft's official Python projection of the Windows SDK). Used only for Windows.Services.Store.StoreContext.GetAppLicenseAsync. Arch-specific wheels for x64/x86/arm64. Bundled by PyInstaller --collect-all winsdk.

Privacy policy: https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html
Support:        support@fieldcrestdental.com
```

---

## What changed since 1.0.31 (context for the reviewer)

1.0.31 was the last package submitted to the Store (the firewall-rules placement
fix that finally produced shippable MSIX artifacts). The 28 releases between
1.0.31 and 1.0.59 fall into five buckets:

**1.0.32 - 1.0.35 - small fixes:**
- 1.0.32: fix macOS autorelease-pool leak in the menu-bar pump
- 1.0.33: fix Windows Store autostart broken by versioned package path
- 1.0.34/1.0.35: direct-installer (non-Store) install + uninstall path fixes
  - Not user-visible on a Store install
- 1.0.36 - 1.0.40: Ready-message log polish (incoming-message emphasis, smooth
  bg-color flash, persistent daily Ready log, Send Message card auto-size)

**1.0.41 - 1.0.56 - stick-figure cameo whimsy feature:**
A new lightweight animation that briefly plays a stick figure walking across
the Ready button area after a qualifying outgoing Ready send. 10+ character
options (top hat, no-face, etc.), multi-waypoint paths, peek-out + wave
animations, production-rate-limited so it cannot spam during a busy office
day. State persists across restarts. Purely cosmetic, no audio, never blocks
UI input. Configurable / can be effectively disabled by toggling characters
in the picker.

**1.0.57 - critical reliability fix:**
A duplicate "Room 1" station name (the old default) could trigger a sub-
millisecond connect/kick/reconnect cycle between peers that spawned hundreds
of `_handle_client` threads and pegged CPU within ~15 seconds of launch.
MessageServer now refuses the duplicate hello rather than kicking the
existing socket, and MessageClient sleeps 3 seconds before every reconnect
(was only on exception). Also removed a redundant NSRunLoop pump on macOS
that triggered a fatal `PyEval_RestoreThread` assertion on some macOS 15.x
installs.

**1.0.58 - prevent the duplicate-label scenario at the source:**
Fresh installs no longer auto-broadcast "Room 1". The Station name dropdown
shows "Please choose the name of this Station" as a placeholder, and the
LAN beacon + Ready button stay off until a real name is picked. Existing
installs whose config already holds a real label are unaffected.

**1.0.59 - in-app enforcement of the 7-day Store free trial:**
The most reviewer-relevant change since 1.0.31. Until now, the app
deliberately delegated all license handling to the Store and treated every
running instance as licensed (see the 1.0.29 history below). With 1.0.59:

- Frozen builds (`IS_FROZEN == True`) stamp `first_launch_iso` into the
  local user-data config on first launch.
- 7-day countdown from that date. A banner appears above the main UI when
  6 or fewer days remain. One-time reminder dialogs fire at 6 / 3 / 1
  days remaining (each fires at most once per install, tracked in
  `trial_reminders_shown`).
- When the trial elapses without purchase: the Ready button shows a "Buy in
  Microsoft Store?" modal instead of sending, and incoming Ready alerts
  from peers are dropped silently (logged only to the diagnostic stream so
  peers do not see a bounce that would reveal the receiver is unlicensed).
  LAN discovery and the TCP mesh keep running so the user still appears
  online and an unlock does not require a restart.
- Purchase detection via `Windows.Services.Store.StoreContext.GetAppLicenseAsync`
  through the bundled `winsdk` Python package. An active non-trial license
  unlocks the app within ~25 seconds of a Buy completed in another window.
  Manual "I purchased - refresh" button on the banner re-runs the check on
  demand.
- The Buy button deep-links to `ms-windows-store://pdp/?ProductId=9NKDHB58JJC5`,
  falling back to `https://apps.microsoft.com/detail/9NKDHB58JJC5` if the
  deep-link can't be opened.
- Dev runs (running the .py directly outside the frozen Store package) are
  untouched: every trial helper short-circuits to "licensed, not enforced"
  when `IS_FROZEN` is False.

**1.0.60 - macOS sleep/wake recovery (not visible to a Windows reviewer):**
Fixes a long-standing macOS bug where the app stopped receiving incoming
Ready alerts after an overnight sleep cycle and had to be restarted every
morning. macOS routinely returns OSError from `accept()` on a TCP listening
socket that lived through a sleep, with no in-kernel recovery; the
MessageServer's accept loop exited on that error and never re-bound.
1.0.60 wraps the bind/listen/accept code in an outer re-bind loop and
also subscribes to `NSWorkspaceDidWakeNotification` to proactively
restart the network stack on wake. Windows behavior is unchanged.

**A REVIEWER WILL NOT SEE THE LOCKOUT** under normal review conditions —
the 6-day reminder threshold is the earliest the user-visible banner
appears, and the lockout itself only triggers at day 7. The full 7-day
countdown is well outside the review window.

If a reviewer somehow gets the app into an expired state during testing
(e.g. by rolling the system clock forward), the expected behavior is: a
red banner across the top of the main window, a modal popping up when
Ready is clicked, and the Ready log no longer recording incoming alerts.
The app does NOT crash, NOT quit, and NOT prevent the user from browsing
the rest of the UI.

## What changed vs. previous submissions (context for reviewers)

Earlier submissions (v1.0.20 – v1.0.28) used a "free download + subscription
Add-on" model. The Add-on commerce path was stuck in a persistent
`PEX-CatalogAvailabilityDataNotFound` backend state in Microsoft's commerce
catalog that did not respond to any configuration attempted (multiple
visibility / audience settings on the parent app, two separately-published
Add-ons with distinct Product IDs, two Microsoft accounts, full clock and
cache resets). Microsoft Q&A / Partner Center support was unable to be
reached due to Engage Center routing.

v1.0.29 pivots to **one-time paid at $14.99**. This uses the standard
Microsoft Store listing-level pricing (the Get/Buy button on the listing),
not an Add-on. The app no longer has a paywall window, no longer ships a
StoreHelper.exe binary, and no longer calls
`Windows.Services.Store.StoreContext.RequestPurchaseAsync`. It is
functionally identical to the previously-approved v1.0.20 build with the
welcome / paywall removed and updates / bug fixes applied.

The two existing in-app subscription Add-ons under this app
(`ChairsideReadyAlert.Subscription.Monthly` and
`ChairsideReadyAlert.SubV2.Monthly`) will be retired (Hidden visibility,
Stop Acquisition off — Stop Acquisition was observed to interfere with
catalog reconciliation in earlier debugging) once v1.0.29 is approved
and live.

## What you fill in elsewhere on the submission form

| Field | Value |
|---|---|
| App name | Chairside Ready Alert |
| Category | Productivity |
| Subcategory | Communication (closest match) |
| Privacy policy URL | https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html |
| Website URL | https://ayodoood.github.io/Chairside-Ready-Alert/ |
| Support contact | support@fieldcrestdental.com |
| System requirements | Windows 10 version 17763.0 or higher |
| Architectures submitted | x64, x86, arm64 (separate packages — do NOT mark as `neutral`) |
| Age rating | Submit IARC questionnaire; expect ~3+ (no objectionable content) |
| **Pricing** | **$14.99 USD, one-time purchase** |
| Free trial | **7 days, configured at the Store listing level.** Mirrored in-app by 1.0.59 (`TRIAL_DAYS = 7`). Keep these two in sync — if you change one, change the other. |
| Markets | All available worldwide |
| Audience / Visibility | Public (or Hidden if you still want soft-launch) |

## Capabilities to declare in the manifest

For an EXE/onedir Win32 submission:

- **internetClient** — NOT required. The Store EXE does not make outbound
  internet connections. Declaring it would needlessly trigger more
  permissions warnings.
- **privateNetworkClientServer** — REQUIRED. The app sends UDP broadcasts
  and accepts inbound TCP connections on the local network.

If Partner Center asks about specific capabilities at submission time,
declare only `privateNetworkClientServer` (and no internet capability).
