---
title: Chairside Ready Alert — FAQ & Troubleshooting
---

# Chairside Ready Alert — FAQ & Troubleshooting

A reference for the most common setup, networking, and behavior questions.
Issues are grouped by topic. If your problem isn't here, email
[support@fieldcrestdental.com](mailto:support@fieldcrestdental.com).

---

## Installation Instructions

**Read this first — getting the network right is what makes the app
work.** Chairside Ready Alert has no central server and nothing in the
cloud. Each computer talks **directly** to the others over your office
network, so the single most important thing is that every computer that
needs to alert the others is on the **same local network**.

**What is a LAN?**
LAN stands for **Local Area Network** — it's simply the private network
inside your office that your router creates to connect your computers,
printers, and Wi-Fi together. Every device on the same LAN is given an
**IP address** in the same range — for example `192.168.1.10`,
`192.168.1.11`, `192.168.1.12` (notice the first three numbers match).
Chairside Ready Alert uses those IP addresses to let the workstations
find and message each other. There's no account to create and nothing
leaves the building — it all stays on your own network.

**The basic setup — do this on every workstation:**

1. Install the app on each computer (Section 1 below covers the install
   steps and the Windows / macOS security prompts you'll see).
2. Connect every computer to the **same** office network — the same
   router — whether by Ethernet cable or Wi-Fi.
3. Launch the app on each one and give it a unique station name
   (Room 1, Room 2, Doctor, Lab, and so on).
4. Within a few seconds each workstation should appear in the others'
   lists. You're done.

**If any computers connect over Wi-Fi — please read this:**
For everything to find each other, your **wired** computers and your
**Wi-Fi** computers must be on the **same** network, with IP addresses
in the same range. The most common problem is a **second Wi-Fi router**
plugged in behind the main one: it creates its *own* separate network, so
the Wi-Fi computers get IP addresses in a different range
(e.g. `192.168.0.x`) than the wired computers (`192.168.1.x`), and the
two groups can't see each other.

The fix is to set that Wi-Fi router to **"Bridge" mode** (some brands
call it **"Access Point" or "AP" mode**). Bridge mode tells the Wi-Fi
router to stop handing out its own separate IP addresses and instead just
extend your main network — so every device, wired or wireless, lands in
the **same IP range** and can find the others. The "Bridge" / "AP Mode"
setting lives in the Wi-Fi router's admin page; if you're not sure how to
change it, your IT person or internet provider can usually flip it in a
couple of minutes.

Two more Wi-Fi settings to avoid:

- **Don't put the workstations on a "Guest" Wi-Fi network** — guest
  networks are deliberately designed to keep devices from seeing each
  other.
- **Turn off "AP isolation" / "client isolation"** if your router has it
  — it blocks devices on the same Wi-Fi from talking to each other.

**How to check it's right:** on two of the computers, look up the IP
address (Windows: open Command Prompt and type `ipconfig`; Mac:
**System Settings → Wi-Fi → Details**). If the **first three numbers**
match — for example both start `192.168.1.` — you're on the same network
and the app will work. If they're different, you have the two-network
problem above: switch the Wi-Fi router to Bridge mode, or see Section 6
for a manual workaround.

---

## 1. Installing and first launch

**Q: Windows shows a "Microsoft Defender SmartScreen prevented an unrecognized app from starting" warning. Is the app safe?**
This warning appears for any app downloaded from the internet that isn't yet
widely installed (it's based on download reputation, not actual scanning).
Click **More info** → **Run anyway**. If you obtained the app from the
Microsoft Store, you will not see this warning.

**Q: macOS says "Chairside Ready Alert.app cannot be opened because Apple cannot check it for malicious software."**
The app is not notarized. Right-click the `.app` (or `.command` installer),
choose **Open**, then click **Open** in the dialog that appears. macOS
remembers the choice for future launches. If macOS still refuses, go to
**System Settings → Privacy & Security** and click **Open Anyway** in the
section near the bottom.

**Q: macOS asks for permission to find devices on the local network.**
Allow it. This permission is what lets the app discover other workstations
running Chairside Ready Alert. If you previously denied it, re-enable it
under **System Settings → Privacy & Security → Local Network**.

**Q: Windows Firewall pops up on first launch.**
Allow access on **Private networks** (this is your office LAN). If you
accidentally clicked Block, open **Control Panel → Windows Defender Firewall →
Allow an app or feature** and tick **Private** for Chairside Ready Alert
(or for the Python interpreter, depending on installer).

**Q: The macOS installer says it needs to download Python and install it.**
That's expected the first time you run the installer on a Mac that doesn't
already have python.org Python 3.12. The installer downloads the official
Python 3.12 package from python.org, requires a one-time admin password,
and uses it for the app. macOS's bundled `/usr/bin/python3` does **not**
include Tkinter, which is why the app needs python.org Python.

**Q: Where is the app actually installed?**
- **Windows direct installer:** `%LOCALAPPDATA%\ChairsideReadyAlert\`
- **macOS:** `~/Applications/Chairside Ready Alert.app` (the bundle), with support files at `~/Library/Application Support/ChairsideReadyAlert/`. A Finder alias named **Chairside Ready Alert** is placed on the Desktop and points at the canonical bundle.
- **Microsoft Store version:** managed by Windows; you don't need to know the path.

**Q: How do I uninstall the app?**
- **Microsoft Store version (Windows):** **Settings → Apps → Installed apps**, find Chairside Ready Alert, click the three dots → **Uninstall**. The Store handles everything.
- **Direct installer (Windows):** **Settings → Apps → Installed apps**, find Chairside Ready Alert, click the three dots → **Uninstall**. (The installer registers an Apps & Features entry, same as any standard Windows app.) Settings are kept by default; to remove them too, run the uninstaller manually with `-RemoveSettings` from `%LOCALAPPDATA%\ChairsideReadyAlert\uninstall_chairside_ready_alert.ps1`.
- **macOS:** drag `Chairside Ready Alert.app` from `~/Applications/` to the Trash. Optionally also delete `~/Library/Application Support/ChairsideReadyAlert/` to remove settings.

---

## 2. Other workstations don't show up

**Q: I installed the app on two PCs but they don't see each other.**
Most common causes, in order:

1. **Wrong network/subnet.** Both machines must be on the same LAN
   subnet (e.g., both on `192.168.1.x`). Wi-Fi guest networks are
   usually isolated from the wired LAN. Move both to the staff network.
2. **Different ports.** Each machine's TCP port must match. Open
   **Settings → Network Settings…** on both machines and confirm the
   port number is identical (default `50505`).
3. **Firewall blocking UDP 50506 or TCP 50505.** Allow both Chairside
   Ready Alert and the Python interpreter through Windows Firewall on
   the **Private** profile. On business networks, your IT may also
   need to whitelist UDP broadcast on port `50506`.
4. **Switch or AP blocking broadcast.** Some managed switches and many
   guest Wi-Fi networks drop UDP broadcast traffic. Workaround: in
   Settings, fill in **Manual peer IPs** with a comma-separated list
   of the other workstations' IP addresses (e.g.,
   `192.168.1.41, 192.168.1.42`).
5. **VPN.** If a VPN is active on either machine, it may route LAN
   traffic somewhere else. Disconnect the VPN.

**Q: How do I find a workstation's IP address?**
- **Windows:** open Command Prompt and run `ipconfig`. Look for the
  IPv4 Address under your active adapter (usually starts `192.168.` or
  `10.`).
- **macOS:** **System Settings → Wi-Fi** (or Network) → click the
  active connection → IP address is shown there.

**Q: Two workstations have the same station label and it's confusing.**
Rename one. The app shows an inline duplicate-label warning under
the station name field when this happens.

---

## 3. Sending and receiving alerts

**Q: I send a Ready alert but the other workstation doesn't react.**
- Check the recipient is in the peer list on your screen. If the peer
  list is empty, see Section 2 above.
- Make sure the recipient PC isn't sleeping or locked. Some Windows
  power plans suspend networking during sleep.
- On the recipient PC, confirm the volume isn't muted (system volume
  AND the per-station volume slider in the app).

**Q: The app pops to the front but no sound plays.**
The recipient controls the local alert volume. Open the app on the
recipient and raise the **Alert volume** slider. Confirm Windows /
macOS system volume is also up.

**Q: I want to send to multiple workstations at once.**
Open the **Default** menu and configure default targets for your
station. Now one tap of the Ready button goes to every default target.

**Q: I want a different sound for alerts from a specific station.**
Sender controls which sound plays on the receiver. The sender's
**Alert sound** dropdown selects the sound played at the receiver.
The receiver's volume slider controls how loud it plays locally.

**Q: Why is there a stick figure coming on screen randomly?**
That's a small built-in bit of fun, not a bug. Every so often, after
you've sent several Ready alerts, a little stick figure animates across
your screen — it strolls out from behind the Ready button, wanders
around, and disappears on its own after a second or two. It only shows
on the screen of the person **sending** the alert, it never makes a
sound, and it has no effect on whether your alerts are delivered.

It is deliberately occasional: it appears only once every several alerts
(and never from rapidly tapping the button — qualifying sends have to be
at least 10 minutes apart), so it stays a pleasant surprise rather than a
distraction.

If you'd like to change the character or turn the animations off
completely, the app ships with an **Animation Picker**. Open the app's
install folder (Section 1 has the path) and run
`ready_animation_picker.py` — in the picker you can choose a different
stick figure or untick every animation to switch the feature off
entirely. If you're not comfortable doing that, email support and we'll
help you turn it off.

---

## 4. Sounds and volume

**Q: No sound plays at all, even though the window flashes.**
- Confirm system volume is up and not muted.
- On Windows, check **Sound mixer** (right-click speaker icon →
  Open Volume mixer) — Chairside Ready Alert may have been muted
  individually.
- On macOS, check **System Settings → Sound → Output** is on the
  speakers you expect.

**Q: Can I add my own custom alert sounds?**
Not currently. The 15 built-in sounds are synthesized at runtime to
keep the app self-contained (no audio file licensing concerns, no
extra files to lose). Custom sounds are on the future-features list.

---

## 5. System tray / menu bar

**Q: The tray icon is missing on Windows.**
Windows 11 hides "extra" tray icons by default. Click the **^**
(caret) next to the clock and drag the **R** Chairside Ready Alert
icon onto the always-visible tray area. Or open
**Settings → Personalization → Taskbar → Other system tray icons**
and toggle Chairside Ready Alert to **On**.

**Q: The tray icon is missing on macOS.**
macOS Sonoma and later sometimes hide menu bar items behind the
notch on MacBooks. Use **Bartender**, **Hidden Bar**, or
**System Settings → Control Center** to manage what's visible.
The app also exposes the same actions in its **Settings** menu, so
you can control the app without the menu bar icon if needed.

**Q: How do I send a Ready alert from the tray?**
Right-click the tray/menu-bar **R** icon → **Send Ready**. Or
double-click the icon to bring the main window to the front.

---

## 6. Network configuration

**Q: Default ports?**
- **TCP 50505** for messaging (changeable in Settings).
- **UDP 50506** for peer discovery broadcast (not changeable).

**Q: Our IT department wants a list of network behavior to whitelist.**
Direct them to the FAQ entry above plus the privacy policy. Concretely:

| Direction | Protocol | Port | Purpose |
|---|---|---|---|
| Outbound + inbound | UDP broadcast (255.255.255.255) | 50506 | Peer discovery beacons every 2.5s |
| Outbound + inbound | TCP | 50505 (configurable) | Alert message payload |
| Outbound only | HTTPS (443) | — | **Direct-installer build only:** manual update check (user-initiated). The Microsoft Store build makes no outbound internet calls. |

The app does not connect to any cloud service, telemetry endpoint,
or analytics service at any time.

**Q: Can two computers on different subnets talk to each other?**
Not via the default broadcast discovery (broadcast doesn't cross
subnets). If your network routes between subnets, configure
**Manual peer IPs** in Settings on each machine listing the IP
addresses of the others. UDP discovery will be skipped and TCP
messaging will work directly.

**Q: Will the app work if some workstations are on Wi-Fi and others on Ethernet?**
It depends on whether Wi-Fi and wired share the **same subnet**:

- **Same subnet (works perfectly).** Typical modern small-office
  setup: one router/firewall does DHCP, Wi-Fi access points run in
  bridge / AP mode, switches just forward Ethernet frames. Every
  device — wired or wireless — gets an IP in the same range
  (e.g., everything is `192.168.1.x`). The app discovers peers
  automatically with no configuration.
- **Two separate subnets (breaks discovery).** A wired switch with
  one DHCP source plus a Wi-Fi router plugged in via its WAN port
  doing its *own* DHCP. Wired devices end up on (say) `192.168.1.x`
  and Wi-Fi devices on `192.168.0.x`. UDP broadcast does not cross
  routers/NAT, so the peer list will look empty on both sides.

**How to tell which case you have:** check `ipconfig` (Windows) or
**System Settings → Wi-Fi / Network** (macOS) on two devices. If
the first three octets of the IPv4 addresses match, you're on the
same subnet. If they differ, you have two subnets.

**Fix for the two-subnet case:**

1. *Best:* reconfigure the Wi-Fi router into AP / bridge mode so it
   stops doing its own DHCP and just acts as a Wi-Fi extension of
   the wired network. Most consumer routers have a "Bridge" or "AP
   Mode" toggle in their admin UI.
2. *Workaround:* fill in **Manual peer IPs** in Settings on every
   workstation. If a Wi-Fi PC is behind NAT, the wired PCs also
   need a TCP port forward (`50505` → the Wi-Fi PC) configured on
   the Wi-Fi router. This works but is fragile and harder to
   maintain than fixing the topology.

The app does not crash or get confused on mixed setups — it simply
won't see peers it can't reach. Peers idle for more than 12 seconds
are pruned automatically.

**Q: Can it work over the internet / between offices?**
Not by design. The app is intentionally LAN-only — there is no
relay server, no NAT traversal, and no end-to-end encryption.
For multi-office communication, use a tool built for that
(VOIP, secure messaging, etc.). Running Chairside Ready Alert
over a public network is **not safe** — see the privacy policy
for details.

---

## 7. Settings and data

**Q: Where are my settings stored?**
- **Windows:** `%LOCALAPPDATA%\ChairsideReadyAlert\chairside_ready_alert_config.json`
- **macOS:** `~/Library/Application Support/ChairsideReadyAlert/chairside_ready_alert_config.json`

**Q: My settings disappeared after reinstalling.**
Settings persist across reinstalls because the installer reuses the
same data folder above. If they are gone, the data folder was
deleted (manually, by another user, or by an antivirus tool). There
is no automatic backup.

**Q: How do I reset to defaults?**
Quit the app, delete the config file at the path above, then
relaunch. The app recreates the file with defaults on next launch.

**Q: Can I copy my settings to another computer?**
Yes — copy `chairside_ready_alert_config.json` to the same path on
the other machine **before launching the app there**. Edit the
`label` field to give the new machine a unique station name.

---

## 8. Updates

**Q: How do I update the app?**
- **Microsoft Store version:** updates are automatic via the Store.
  The in-app **"Check for updates…"** menu item is intentionally
  hidden on Store builds.
- **Windows direct installer:** re-run `Install Chairside Ready
  Alert.bat` from the latest installer download. It detects the
  existing install and updates in place. Or use **Settings → Check
  for updates…** in the running app.
- **macOS:** re-run `Install Chairside Ready Alert macOS.command`
  or use **Settings → Check for updates…**.

**Q: The macOS "Check for updates" returns an SSL certificate error.**
This was fixed in version **1.0.5** — re-run the macOS installer
to pick up the fix. (The fix corrects how the app launcher exposes
the Python venv; older 1.0.4 installs hide the certifi CA bundle
from Python and the SSL handshake fails.)

**Q: How do I see the current version?**
The version is shown when **Settings → Check for updates…** reports
"You're up to date" — and in the title bar on some platforms.

---

## 9. Privacy and security

**Q: Does the app send any data to a cloud service?**
No. The app does not contain analytics SDKs, advertising SDKs, or
crash reporting that contacts external servers. The only outbound
internet traffic is the **manual** update check on direct-installer
builds (Microsoft Store builds confirm the user's one-time purchase
via the Microsoft Store licensing SDK; this query goes through the
local Windows Microsoft Store service, not a third-party endpoint).

**Q: How much does it cost, and how does buying it work?**
The Microsoft Store version is a **one-time purchase** (currently
**$14.99 USD** — check the Store listing for the latest price). There
is **no subscription** and no recurring charge. You can try it free for
**7 days**, then buy it once through the Store's normal **Get / Buy**
button and it's yours for good. A purchase is tied to your **Microsoft
account**, so under Microsoft's standard policy you can install it on the
devices signed in to that account (up to 10) — enough for a typical
dental practice from a single purchase.

**Q: Is it a recurring charge? How do I cancel?**
It isn't recurring — it's a one-time purchase, so there is nothing to
cancel and you will never be billed again. (The old **$1.99 / month**
subscription was retired; if you see older instructions mentioning a
monthly fee or a "Subscribe" window, they are out of date.)

**Q: What happens when the 7-day free trial ends?**
On the Microsoft Store version the app reminds you as the trial winds
down (around 6, 3, and 1 days left). If the trial ends and you haven't
purchased, the Ready button stops sending and shows a **Buy** option
instead — but the app stays open and still appears online to your peers,
so buying unlocks it instantly with no reinstall or restart. (The free
direct-installer version has no trial and no purchase — see below.)

**Q: Does the app work offline?**
Yes. The 7-day trial counts down by calendar date whether or not you're
online. Once you've purchased, the app remembers you're licensed and
keeps working offline; it quietly re-confirms the purchase with the
Microsoft Store in the background the next time it has internet. There is
no periodic "phone home" that locks a paying user out for being offline.

**Q: I bought it on another computer — how do I get it on this one?**
Sign in to the Microsoft Store on the new computer with the **same**
Microsoft account you used to buy it, then install and launch Chairside
Ready Alert. It recognizes the purchase on that account automatically —
there's no separate activation step or unlock code.

**Q: I'm using the direct-installer (.ps1 / .command) version — do I need to buy it?**
No. The purchase and the trial apply only to the Microsoft Store version.
The direct-installer flow (Windows .ps1 / .bat, macOS .command) ships
free as it always has.

**Q: Is alert traffic encrypted on the LAN?**
No. Messages and peer-discovery beacons are sent in cleartext across
the local network. Anyone with packet-capture access on the same
subnet can read them. This is documented in the privacy policy.
**Use only on trusted internal networks.**

**Q: What information does the app store about me?**
The settings file (path in Section 7) holds: your station label,
preferred alert sound and volume, theme, list of default targets,
and any manual peer IPs you configured. None of this leaves your
device except as part of normal LAN messaging (the station label
is broadcast to the LAN every ~2.5 seconds; alert messages contain
the sender label and timestamp).

**Q: Where is the privacy policy?**
[https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html](https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html)

---

## 10. Common error messages

**Q: "Update check failed (network): SSL: CERTIFICATE_VERIFY_FAILED"**
Run the latest installer (≥ 1.0.5). Older builds hid the certifi CA
bundle from the running Python and TLS verification failed.

**Q: "Could not bind UDP 50506 — port already in use"**
Another app on the same machine is using UDP 50506, or a previous
Chairside Ready Alert process didn't exit cleanly. Quit any running
copy from the tray menu (Close Chairside Ready Alert), wait a few
seconds, and relaunch. If a non-Chairside app legitimately needs
50506, contact support — the discovery port is not currently
configurable.

**Q: "Chairside Ready Alert is already running."**
The single-instance lock detected another copy. Click the existing
window or the tray icon to bring it forward. If no copy is visible,
quit it from the tray menu, or — as a last resort — reboot.

**Q: "Cannot open Chairside Ready Alert — file is in use."**
On Windows, this usually means an installer is trying to update
the app while it's running. Close the app first (right-click tray
→ Close Chairside Ready Alert), then re-run the installer.

---

## Still stuck?

Email [support@fieldcrestdental.com](mailto:support@fieldcrestdental.com)
with:

- The version number (Settings → Check for updates… reports it).
- Your operating system and version.
- The contents of `startup_log.txt` from the data folder shown in
  Section 7. The log is plain text — feel free to read it before
  sending.
- A short description of what you tried and what happened.
