# Chairside Ready Alert Privacy Policy

**Effective date:** April 24, 2026  
**Last updated:** April 24, 2026

This Privacy Policy explains how Chairside Ready Alert ("the App", "we", "our", "us") handles information when you use the app on Windows and macOS.

## 1. Summary

- Chairside Ready Alert is designed for communication on a local area network (LAN).
- The App does not require user accounts and does not sell personal data.
- Most data stays on your device and on your local network.
- The App does not include third-party analytics or advertising SDKs.

## 2. Information We Process

The App processes the following categories of information to function:

- **User-provided workstation settings:** station label, selected alert sound, volume, theme, default targets, network settings (such as manual peer IPs and port), and optional update manifest URL.
- **LAN communication data:** station labels, "Ready" messages, selected recipients, and message timestamps.
- **Network metadata:** local IP/peer IP addresses and connection state information needed to discover and connect devices on the LAN.
- **Local diagnostic logs:** startup and connection-status log entries stored locally to help troubleshooting.

## 3. Where Data Is Stored

Configuration and logs are stored locally on your device, for example:

- **Windows:** `%LOCALAPPDATA%\ChairsideReadyAlert\`
- **macOS:** `~/Library/Application Support/ChairsideReadyAlert/`

This may include:

- `chairside_ready_alert_config.json` (settings)
- `startup_log.txt` and in-app connection diagnostics

The App does not operate a cloud database for your app usage data.

## 4. Network Behavior and LAN Risks

Chairside Ready Alert is intended for trusted local networks.

- The App uses **UDP broadcast** for peer discovery (default UDP port 50506).
- The App uses **TCP** for messaging between peers (default TCP port 50505).
- Message payloads and discovery beacons are designed for local network operation and are **not end-to-end encrypted by the App**.
- Device and network administrators are responsible for network controls (segmentation, firewall rules, VLANs, VPNs, endpoint protection) appropriate for their environment.

### Important risk notice

If used on an untrusted, misconfigured, or publicly accessible network, other parties on that network may be able to observe or inject local traffic. Use only on secured internal networks managed by your organization.

## 5. Internet Access and Updates

The App may access the internet only for update-related functions:

- Checking a configured update manifest URL.
- Downloading update files when you choose to install updates.
- Optionally opening a release page in your browser.

Update endpoints are defined by the app configuration or built-in defaults. If your organization hosts its own update manifest, your organization controls that endpoint.

## 6. Data Sharing and Disclosure

We do not sell personal data.  
We do not share usage data with advertising networks.

Data may be shared only:

- Across devices on your LAN as part of normal app operation.
- If required by law, regulation, legal process, or enforceable government request.
- If your organization configures update infrastructure operated by third parties.

## 7. Children

The App is intended for workplace/clinical operational use and is not directed to children.

## 8. Security

We implement reasonable technical measures in the App, but no method of storage or transmission is completely secure. You are responsible for securing devices and networks where the App is deployed.

## 9. Your Choices

You can:

- Disable or avoid update checks by not configuring update URLs and not initiating update checks.
- Remove local app data by uninstalling the App and deleting its local application data folder.
- Control LAN exposure through system firewall/network policy and by stopping network mode in the app.

## 10. Microsoft Store and Platform Processing

Microsoft may independently collect telemetry, diagnostics, or transaction data when you acquire or use apps through Microsoft Store, under Microsoft's own policies. This Privacy Policy covers data handling by Chairside Ready Alert itself.

## 11. Changes to This Policy

We may update this Privacy Policy from time to time. We will update the "Last updated" date above when changes are made.

## 12. Contact

For privacy questions or requests, contact:  
   Fieldcrest Dental PC  
   support@fieldcrestdental.com 


