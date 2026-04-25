# Notes for Microsoft Store Certification

This file contains the exact text to paste into the **"Notes for certification"**
field of the Partner Center submission, plus context for what the certification
team should and should not expect to see.

---

## Paste-into-Partner-Center text

```
Chairside Ready Alert is a LAN-only peer-to-peer messaging tool for dental practices. Its core feature — one-click "Ready" alerts between workstations — REQUIRES TWO OR MORE DEVICES on the same local subnet and cannot be exercised on a single test machine.

In a single-VM test environment, please confirm only:

1. The app launches and the main window renders.
2. The tray icon appears in the notification area.
3. The Settings menu opens.
4. Right-click on the tray icon shows: Send Ready, Show Main Window, Hide Main Window, Close.
5. Selecting "Close" from the tray menu shuts the app down cleanly.

An empty peer list and inability to send/receive alerts are EXPECTED on a single-machine test — no peer device, nothing to message.

Network: the app uses LAN UDP broadcast on port 50506 and LAN TCP on 50505. It makes no outbound internet connections in normal operation. The Store build has no in-app self-update; updates are delivered through the Store channel only.

Privacy policy: https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html
Support: support@fieldcrestdental.com
```

---

## Why this matters

The Store certification team tests submissions inside an isolated VM with
no peer devices. Apps whose primary feature is peer-to-peer LAN
communication regularly fail certification because reviewers see "an app
that doesn't seem to do anything" — they click around the empty peer
list, can't generate any traffic, and conclude the app is broken.

Calling this out up front prevents the most common rejection reason for
LAN-only apps and gives reviewers a concrete, completable test plan that
they can use to confirm the app launches, renders, and quits cleanly.

## What you fill in elsewhere on the submission form

| Field | Value |
|---|---|
| App name | Chairside Ready Alert |
| Category | Productivity |
| Subcategory | Communication (closest match) |
| Privacy policy URL | https://ayodoood.github.io/Chairside-Ready-Alert/PRIVACY_POLICY.html |
| Support contact | support@fieldcrestdental.com |
| System requirements | Windows 10 version 17763.0 or higher |
| Architectures submitted | x64, x86, arm64 (separate packages — do NOT mark as `neutral`) |
| Age rating | Submit IARC questionnaire; expect ~3+ (no objectionable content) |
| Pricing | (your decision — free or paid) |

## Capabilities to declare in the manifest

For an EXE/onedir Win32 submission:

- **internetClient** — NOT required. The Store EXE does not make outbound
  internet connections. Declaring it would needlessly trigger more
  permissions warnings.
- **privateNetworkClientServer** — REQUIRED. The app sends UDP broadcasts
  and accepts inbound TCP connections on the local network.

If Partner Center asks about specific capabilities at submission time,
declare only `privateNetworkClientServer` (and no internet capability).
