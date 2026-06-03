# Microsoft Store Submission — MSIX path

This guide covers the **MSIX** submission path (replaces the older Win32 EXE
flow). The build pipeline produces three architecture-specific `.msix` files
on every tagged release (v*); the rest is filled in by hand in Partner
Center.

## What changed vs the old EXE path

- No "hosted installer URL" anymore. Microsoft Store distributes the package.
- Updates are 100% Store-managed. The app's in-built update check is
  already gated off in frozen builds, so this just works.
- First-run firewall prompt is gone — `privateNetworkClientServer` capability
  is honored automatically.
- Apps & Features registration, Start Menu shortcut, and uninstall plumbing
  are all Store-managed too.

The **direct-installer path** (`install_chairside_ready_alert.ps1` +
`Install Chairside Ready Alert.bat` + the macOS `.command`) is unchanged
and continues to work for non-Store deployments.

---

## One-time setup: paste the three Partner Center identity values

MSIX packages are cryptographically bound to your Partner Center reservation.
Three string values must match exactly. They live at:

> **Partner Center → Apps and games → Chairside Ready Alert → Product
> management → App identity** (or "Product Identity" depending on UI variant)

| Partner Center field | Goes into `AppxManifest.xml` as |
|---|---|
| Package/Identity/Name | `<Identity Name="..." />` |
| Package/Identity/Publisher | `<Identity Publisher="..." />` |
| Package/Properties/PublisherDisplayName | `<PublisherDisplayName>...</PublisherDisplayName>` |

Open `Windows Store Submission/AppxManifest.xml`. Find the three
`TODO_*` markers (currently on lines 39, 40, and 46) and replace each
with the matching value from Partner Center. Commit and push. The other
two placeholders (`VERSION_PLACEHOLDER`, `ARCH_PLACEHOLDER`) are filled
in automatically by the build — leave them.

> ⚠️ Do NOT use `®` or `CN=...` strings you make up. Partner Center has
> already generated these for your account; the only acceptable values are
> the ones it shows you.

---

## Build the MSIX files

After committing the three identity values, push a tag:

```bash
git tag v1.0.13     # or whatever the next release number is
git push origin v1.0.13
```

The GitHub Actions workflow `Build Windows EXE` runs and produces, for each
architecture, both:

- `ChairsideReadyAlert-<arch>` (the unchanged onedir folder for the
  direct-installer path)
- `ChairsideReadyAlert-<arch>-msix` (the new MSIX file for Partner Center)

Watch the run at:

> https://github.com/AyoDoood/Chairside-Ready-Alert/actions

When green, the run page shows six artifacts at the bottom (three folders +
three `.msix`). Download the three `-msix` artifacts.

---

## Submit to Partner Center

1. Sign in to Partner Center → **Apps and games** → click **Chairside
   Ready Alert** (your reserved name).
2. Start a new submission. The Partner Center UI has been redesigned and
   no longer has a "Submissions" tab — look for **Create new submission**,
   **Releases**, or a similarly-labeled action on the app's overview page.
3. On the **Packages** step, upload the three `.msix` files (one per
   architecture). Partner Center auto-detects the architecture from each
   manifest.
4. Other sections (Pricing, Properties, Age rating, Store listing, Privacy
   policy URL, Notes for certification) are inherited from the app product
   itself. Fill in any sections marked incomplete using values from
   `STORE_LISTING_DRAFT.md` and `CERTIFICATION_NOTES.md`.
5. Submit for certification. Typical turnaround is 1–3 business days.

---

## What gets bundled into each .msix

- The full PyInstaller `--onedir` output (the EXE plus its `_internal`
  folder of Python runtime + Tcl/Tk + bundled site-packages).
- `Assets/` folder with six logo PNGs renamed to MSIX standards
  (Square44x44Logo, StoreLogo, Square71x71Logo, Square150x150Logo,
  Wide310x150Logo, Square310x310Logo).
- `AppxManifest.xml` with the architecture and version filled in at
  build time.

The package is **unsigned**. Microsoft Store re-signs every submission
with its own certificate during certification — you don't need to provide
or maintain a code-signing certificate for Store submissions. (For
sideload-testing on a dev machine, you'd need a self-signed cert, but
Store submission doesn't.)

---

## Local sideload testing (optional)

If you want to install the .msix on a Windows machine **before** submitting,
you'll need to either self-sign it or enable developer mode and sideload it
unsigned. Both options are documented in the Microsoft sideload-MSIX guide.
For most workflows, just submit and let Microsoft's certification team
verify it runs — that's effectively your test.

---

## Capabilities declared

| Capability | Why |
|---|---|
| `runFullTrust` (restricted) | Required for any PyInstaller-bundled / desktop-bridge app. Auto-approved at submission for personal Microsoft Store accounts. |
| `privateNetworkClientServer` | LAN UDP broadcast (50506) and TCP messaging (50505). |

No `internetClient` capability — the Store build makes no outbound internet
calls of its own (the in-app auto-updater is gated off when frozen). The
Microsoft Store SDK call that reads the app's paid license
(`StoreContext.GetAppLicenseAsync()` via the `winsdk` package) goes through the
local Microsoft Store service, not a third-party HTTPS endpoint, so it doesn't
require declaring internet access.

---

## Pricing & trial (revenue model)

The Microsoft Store build is a **one-time paid app at $14.99 USD** — not a free
download with a subscription. (The earlier Add-on/subscription model was
abandoned in 1.0.29 after an unresolvable `PEX-CatalogAvailabilityDataNotFound`
state on Microsoft's catalog backend; the paywall window, `StoreHelper.exe`, the
`.NET 8` build steps, and the `winrt-*` dependencies were all removed at that
time and no longer exist in the code.)

Set the price in Partner Center on the submission's **Pricing and availability**
step — choose the **$14.99** base price tier. There is **no Add-on** to create.

Trial behavior is enforced **in-app** (frozen builds only), not by a Store
Add-on — see "Trial / paid-license gating" in `CLAUDE.md` for the code map:

1. On first launch the app stamps `first_launch_iso` into its config and grants
   a `TRIAL_DAYS`-day trial (currently 7).
2. Reminder dialogs fire once each at the `TRIAL_REMINDER_DAYS` thresholds.
3. When the trial expires and the app is still unpaid, the Ready button shows a
   "buy now?" prompt instead of sending; the LAN stack keeps running so the user
   still appears online and unlocking needs no restart.
4. The app recognizes a real purchase via
   `Windows.Services.Store.StoreContext.GetAppLicenseAsync()` (the **`winsdk`**
   package). `winsdk` **must** be bundled by the PyInstaller build / present in
   the CI workflow, or paying users get locked out at trial end with no in-app
   unlock path.
5. The Buy button deep-links to
   `ms-windows-store://pdp/?ProductId=<STORE_PRODUCT_ID>`, falling back to
   `STORE_PRODUCT_WEB_URL`.

The "network of N computers" use case needs no per-seat licensing: a Microsoft
account can install Store apps on **up to 10 devices** by default, so a single
$14.99 purchase on a shared office Microsoft account covers the practice's
workstations.
