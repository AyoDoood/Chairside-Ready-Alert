from __future__ import annotations

import os
import sys

# Before importing tkinter (macOS): avoids noisy stderr; helps some Tk setups.
if sys.platform == "darwin":
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

import json
import math
import re
import shutil
import io
import hashlib
import ssl
import urllib.error
import urllib.request
import webbrowser
from collections import deque
import queue
import socket
import stat
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import tkinter as tk
import wave
from datetime import datetime
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Optional

# Whimsy: stick-figure animations after qualifying outgoing Readys. Imported
# lazily-tolerant — if the module is missing (someone deleted it from a
# dev install), main app still launches; the trigger just no-ops.
try:
    import ready_animations as _ready_animations
except Exception:  # pragma: no cover — defensive
    _ready_animations = None  # type: ignore[assignment]

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

def _import_tray_modules() -> bool:
    global pystray, Image, ImageDraw, _TRAY_IMPORT_ERROR
    try:
        import pystray as _pystray
        from PIL import Image as _Image, ImageDraw as _ImageDraw

        pystray = _pystray
        Image = _Image
        ImageDraw = _ImageDraw
        _TRAY_IMPORT_ERROR = ""
        return True
    except Exception:
        pystray = None
        Image = None
        ImageDraw = None
        _TRAY_IMPORT_ERROR = traceback.format_exc()
        return False


def _attempt_macos_tray_dependency_repair() -> None:
    if sys.platform != "darwin":
        return
    # Try to heal missing tray deps in the exact interpreter launching this app.
    try:
        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        subprocess.run(
            [sys.executable, "-s", "-m", "ensurepip", "--upgrade"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=90,
            check=False,
        )
        subprocess.run(
            [
                sys.executable,
                "-s",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                "pystray",
                "pillow",
                "pyobjc-framework-Cocoa",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=180,
            check=False,
        )
    except Exception:
        pass


if not _import_tray_modules() and sys.platform == "darwin":
    _attempt_macos_tray_dependency_repair()
    _import_tray_modules()

try:
    import cairosvg
except Exception:
    cairosvg = None

try:
    import certifi
except Exception:
    certifi = None

_UI_FAMILY: Optional[str] = None


APP_TITLE = "Chairside Ready Alert"
APP_VERSION = "1.0.90"

# Maximum number of Ready-log entries shown in the widget. Older entries
# are dropped from the display (still kept in ready_log.txt for the day).
# Set to a small number so the user never has to scroll the log -- the
# most recent N is what matters; longer history lives on disk only.
MAX_VISIBLE_LOG_ENTRIES = 6
# True for PyInstaller-frozen builds (Microsoft Store EXE). The Store install
# directory is read-only and Store policy prohibits self-update, so the auto-
# update UI and any "spawn python on the .py file" code paths must be gated
# off when this is True.
IS_FROZEN = getattr(sys, "frozen", False)
# Optional default manifest URL baked into your build; usually leave empty and set
# update_manifest_url in chairside_ready_alert_config.json or CHAIRSIDE_UPDATE_MANIFEST_URL.
UPDATE_MANIFEST_URL_BUILTIN = "https://raw.githubusercontent.com/AyoDoood/chairside-ready-alert/main/version.json"
UPDATE_ALLOWED_FILES = {
    "chairside_ready_alert.py",
    "ready_animations.py",
    "ready_animation_picker.py",
    "install_chairside_ready_alert.ps1",
    "Install Chairside Ready Alert.bat",
    "Install Chairside Ready Alert macOS.command",
    "install_chairside_ready_alert_macos.sh",
    "uninstall_chairside_ready_alert.ps1",
    "README-Windows-One-Click.txt",
    "version.json",
    "version.json.example",
}
DEFAULT_PORT = 50505
DISCOVERY_PORT = 50506
BEACON_INTERVAL = 2.5
BEACON_VERSION = 1
# Per-process identifier stamped into every discovery beacon so a machine can
# recognize and ignore its OWN beacons by ID -- independent of IP. This is the
# robust fix for the multi-homed self-collision: a station with two NICs on the
# same subnet (e.g. Ethernet + a still-connected Wi-Fi) would otherwise hear its
# own beacon loop back via the second adapter with a source IP that
# local_ipv4_addresses() never enumerated, fail the "is this me?" IP check, and
# flag itself as a duplicate station ("Another computer may be using the same
# station name"). Filtering on this ID makes self-detection correct no matter
# how many adapters a machine has. Older peers omit the field -- they fall back
# to the IP-based self-filter and ignore the extra field in our beacons, so it
# is fully backward-compatible.
_INSTANCE_ID = uuid.uuid4().hex
PEER_STALE_SEC = 12.0
BUFFER_SIZE = 4096
# --- TCP liveness (fix: "left on overnight -> peers discovered but Ready
# silently not received") ----------------------------------------------------
# A cached peer socket whose far end vanished overnight WITHOUT a FIN/RST (the
# peer slept/hibernated, a NIC power-management setting suspended the adapter,
# or a managed switch aged out the idle TCP flow) becomes a black hole:
# sendall() of the small Ready JSON succeeds into the local kernel buffer and
# the bytes are then dropped on the wire, with no exception and no error -- the
# UI even logged "Sent ->". TCP keepalive was OFF on every socket and the
# ping/pong heartbeat was only half-wired (the server answered pings but
# nothing ever sent one), so neither side ever noticed the dead path. These
# knobs turn that silent half-open into a detected disconnect within seconds so
# the existing 3s-backoff reconnect can rebuild a live socket.
KEEPALIVE_IDLE_SEC = 30        # OS keepalive: start probing after this idle gap
KEEPALIVE_INTVL_SEC = 5        # ...then probe this often until the peer answers
# Application-level heartbeat: each MessageClient sends a tiny {"type":"ping"}
# after this many seconds of socket silence. This is the linchpin -- it runs
# continuously (even overnight), so a dead path raises on the sending socket
# within seconds instead of looping on recv-timeout forever, AND it keeps a
# stateful switch from aging out the flow in the first place. The server
# already answers ping with pong, so no peer-side change is needed.
HEARTBEAT_INTERVAL_SEC = 5.0
# The sync loop rebuilds a peer client (other than the load-bearing 127.0.0.1
# loopback) that still has a socket but has heard nothing -- not even a
# heartbeat pong -- for longer than this. Must exceed HEARTBEAT_INTERVAL_SEC so
# a quiet-but-alive socket is never churned.
CLIENT_STALE_SEC = 30.0
# A normally-1.5s sync tick that arrives more than this late (by the monotonic
# clock) almost certainly means the machine slept/hibernated -- root.after
# timers do not fire while asleep. We then bounce the LAN to rebuild fresh
# sockets, the Windows equivalent of the macOS NSWorkspaceDidWakeNotification
# recovery. Kept well above any plausible busy-workday tick stall (AV scan,
# backup, descheduled pythonw) so it cannot spuriously bounce a healthy LAN.
WAKE_GAP_SEC = 150.0
# How often this machine re-resolves its own LAN IP set. This used to run on
# the UI thread every 1.5s inside the sync tick (a blocking getaddrinfo + a
# fresh UDP socket each time -- a stall risk after a weekend if mDNS/DNS is
# slow, and ~40 socket churns/min for an address that almost never changes).
# Now it runs on a background thread no more than this often.
LOCAL_IP_REFRESH_SEC = 30.0
# Consecutive failed beacon sends before LanDiscovery rebuilds its broadcast
# socket (the bound interface likely vanished on a NIC change / IP renumber).
BROADCAST_FAIL_REBUILD = 3
# Hard ceiling on a single inbound TCP "line" (newline-delimited JSON). Real
# beacons/messages are a few hundred bytes; anything streaming past this with
# no delimiter is a broken or hostile peer and the connection is dropped rather
# than letting the receive buffer grow without bound.
MAX_INBOUND_LINE_BYTES = 65536
# startup_log.txt is rotated once it grows past this so it can't accumulate
# without limit across launches and lifecycle events.
STARTUP_LOG_MAX_BYTES = 512 * 1024
CONFIG_FILE = "chairside_ready_alert_config.json"
# Frozen Store builds get their own lock filename and IPC port so they do not
# collide with a developer's `python chairside_ready_alert.py` run on the same
# machine — both installs share %LOCALAPPDATA%\ChairsideReadyAlert\.
INSTANCE_LOCK_FILE = (
    "chairside_messenger.instance.store.lock" if IS_FROZEN
    else "chairside_messenger.instance.lock"
)
FOCUS_IPC_HOST = "127.0.0.1"
FOCUS_IPC_PORT = 59662 if IS_FROZEN else 59661
FOCUS_IPC_TOKEN = "CHAIRSIDE_FOCUS_V1"

# --- Microsoft Store trial / paid-license gating ---------------------------
# Only enforced on frozen Store builds. Dev runs (`python chairside_ready_alert.py`)
# are always treated as fully licensed so this code never gets in the way locally.
# The trial is anchored on the first-launch timestamp persisted in the config
# (first_launch_iso). The Store's own license API is consulted as the
# authoritative paid-state signal: a True from StoreContext.GetAppLicenseAsync
# (active and not trial) clears the lockout immediately, even if the user
# purchased after the trial elapsed.
TRIAL_DAYS = 7
# Days-remaining thresholds at which the app pops a one-time reminder dialog.
# Each value fires at most once per install (tracked in trial_reminders_shown).
TRIAL_REMINDER_DAYS = (6, 3, 1)
# While the app is unpaid (trial or expired), re-query the Store license on this
# cadence so a purchase made directly in the Store app -- or one that propagates
# a short while after the in-app Buy click -- unlocks WITHOUT a restart. Polling
# stops the moment a purchase is confirmed (paid_unlocked latches True).
LICENSE_POLL_INTERVAL_MS = 60_000
# Single source of truth for the manual refresh button's resting label, shared
# by the banner builder and the post-refresh feedback that restores it.
REFRESH_BTN_LABEL = "I purchased — refresh"
# Microsoft Store ProductId for the deep-link Buy URL ("ms-windows-store://pdp/?ProductId=...").
# Populated from the published Partner Center listing. If ever blanked, the Buy
# button falls back to STORE_PRODUCT_WEB_URL.
STORE_PRODUCT_ID = "9NKDHB58JJC5"
STORE_PRODUCT_WEB_URL = "https://apps.microsoft.com/detail/9NKDHB58JJC5"
# Public FAQ & Troubleshooting page (GitHub Pages). Opened from the Help menu
# and the About dialog. Same page the marketing site's "Read the FAQ" button
# points at (index.html -> ./FAQ.html).
FAQ_URL = "https://ayodoood.github.io/Chairside-Ready-Alert/FAQ.html"


def _trial_days_remaining(first_launch_iso: str) -> Optional[int]:
    """Days left in the install-date trial, or None if first_launch_iso is unset/unparseable.
    Returns 0 (not negative) once the trial has elapsed so callers can treat 0 as 'expired'."""
    if not first_launch_iso:
        return None
    try:
        start = datetime.strptime(first_launch_iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    elapsed = (datetime.now().date() - start).days
    if elapsed < 0:
        # Clock skew or user rolled the system clock back. Don't grant extra
        # trial time — clamp to 0 days remaining (treated as expired).
        return 0
    return max(0, TRIAL_DAYS - elapsed)


def _query_store_license_paid() -> Optional[bool]:
    """Ask the Microsoft Store whether this user has purchased the full app.

    Returns True if the license is active AND not a Store-side trial (i.e. paid),
    False if active-but-trial or inactive, or None if we can't tell (winsdk
    missing, not a Store install, transient API failure). None must be treated
    as 'unknown' — fall back to the cached paid_unlocked value rather than
    locking out a paying user during a flaky Store call.

    Runs synchronously on whatever thread calls it. Do not call from the Tk
    main thread — bounce off a worker and post the result via the UI queue.
    """
    if sys.platform != "win32" or not IS_FROZEN:
        # Dev / non-Windows runs are always treated as licensed at a higher
        # level — callers shouldn't even hit this. Return None to be safe.
        return None
    try:
        import asyncio
        # winsdk is the maintained Python projection of the Windows SDK and is
        # bundled into Store builds (pip install winsdk + --collect-all winsdk).
        # If it is ever dropped from the build this import fails and we report
        # 'unknown' (None), leaving callers to fall back to cached paid_unlocked.
        from winsdk.windows.services.store import StoreContext  # type: ignore
    except Exception:
        return None
    # Two correctness requirements that, if missed, made this silently return
    # None on EVERY frozen build pre-1.0.83 and left paying customers locked:
    #   1. WinRT calls need a COM apartment on the calling thread. This runs on
    #      a fresh daemon worker that has none, so make a best-effort MTA init.
    #      The winsdk apartment API lives in winsdk.system and has shifted
    #      across versions; winsdk ALSO auto-initializes an MTA apartment on the
    #      first WinRT call for a thread with none, so the read still works if
    #      the explicit init below doesn't apply.
    #   2. get_app_license_async() returns a WinRT IAsyncOperation, NOT a native
    #      coroutine. Passing it straight to asyncio.run() raises "a coroutine
    #      was expected"; it must be awaited inside an `async def`.
    # Every failure path returns None (fail-closed) -- never falls open to paid.
    apartment_inited = False
    uninit_fn = None
    try:
        from winsdk.system import init_apartment, uninit_apartment
        try:
            from winsdk.system import ApartmentType
            init_apartment(ApartmentType.MULTI_THREADED)
        except Exception:
            init_apartment()  # wheels where MTA is the default argument
        uninit_fn = uninit_apartment
        apartment_inited = True
    except Exception:
        # Explicit init unavailable on this wheel -- rely on winsdk's auto-MTA
        # init on first use. Don't uninit what we didn't init.
        apartment_inited = False
    try:
        async def _read_license():
            ctx = StoreContext.get_default()
            return await ctx.get_app_license_async()

        # Bound the call so a wedged Store backend can't leave this worker (and
        # the COM apartment) hung, nor strand the 'Checking...' button forever.
        lic = asyncio.run(asyncio.wait_for(_read_license(), timeout=15))
        return bool(lic.is_active and not lic.is_trial)
    except Exception:
        return None
    finally:
        if apartment_inited and uninit_fn is not None:
            try:
                uninit_fn()
            except Exception:
                pass


def _version_tuple(s: str) -> tuple[int, int, int]:
    """Parse a dotted version string into (major, minor, patch) for comparison."""
    nums = [int(x) for x in re.findall(r"\d+", s)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _compare_version_strings(remote: str, current: str) -> int:
    """Return >0 if remote > current, <0 if remote < current, 0 if equal."""
    a, b = _version_tuple(remote), _version_tuple(current)
    if a > b:
        return 1
    if a < b:
        return -1
    return 0


def _fetch_update_manifest(url: str, timeout: float = 18.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_TITLE.replace(' ', '-')}/{APP_VERSION}"},
    )
    ctx = _create_https_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Update manifest must be a JSON object.")
    return data


def _download_bytes(url: str, timeout: float = 35.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_TITLE.replace(' ', '-')}/{APP_VERSION}"},
    )
    ctx = _create_https_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _create_https_context() -> ssl.SSLContext:
    """
    Build an HTTPS context that prefers certifi's CA bundle when available.

    Some Python installs on Windows can have an incomplete trust store, which causes
    CERTIFICATE_VERIFY_FAILED against GitHub even on valid certificates.
    """
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    return ssl.create_default_context()


def _support_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_dir() -> str:
    """Stable per-user folder for settings (same idea as the Windows/macOS installers)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(local, "ChairsideReadyAlert")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ChairsideReadyAlert")
    return os.path.join(os.path.expanduser("~"), ".config", "dentalmessenger")


def _resolve_config_path() -> str:
    """
    Always use the same config file regardless of current working directory.

    Older builds used os.getcwd(), which changes depending on shortcut/terminal/IDE and made
    settings (alert sound, etc.) appear to reset.
    """
    canonical = os.path.join(_user_data_dir(), CONFIG_FILE)
    try:
        os.makedirs(os.path.dirname(canonical), exist_ok=True)
    except OSError:
        pass

    if os.path.isfile(canonical):
        return canonical

    legacy_candidates = [
        os.path.join(_support_dir(), CONFIG_FILE),
        os.path.join(os.getcwd(), CONFIG_FILE),
    ]
    best: Optional[str] = None
    best_mtime = -1.0
    for p in legacy_candidates:
        if not os.path.isfile(p):
            continue
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > best_mtime:
            best_mtime = m
            best = p

    if best:
        try:
            shutil.copy2(best, canonical)
        except OSError:
            # Can't write AppData — keep using the legacy file so saves still work.
            return best

    return canonical




class SingleInstanceLock:
    """Best-effort per-user process lock to prevent multiple app instances."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            self.release()
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if sys.platform == "win32":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


# Displayed in the Station name combobox on a fresh install (and any time the
# saved label is unset). NOT a real station name -- never persisted to config,
# never broadcast in beacons. The networking layer treats it the same as "" via
# ChairsideReadyAlertApp._has_real_station_label, and the Ready button is
# disabled until the user picks a real name. This fixes the long-standing
# fresh-install collision where every new computer joined the LAN broadcasting
# "Room 1" and stomped the existing Room 1 station.
STATION_NAME_PROMPT = "Please choose the name of this Station"

DEFAULT_LABELS = ["Room 1", "Room 2", "Room 3", "Room 4", "Room 5", "Doctor", "Lab"]
DEFAULT_STATION_SOUNDS = {
    "Room 1": "Bright Chime",
    "Room 2": "Soft Ding",
    "Room 3": "Double Ding",
    "Room 4": "Triple Ping",
    "Room 5": "Deep Pulse",
    "Doctor": "Crisp Bell",
    "Lab": "Rising Tone",
}
ALERT_SAMPLE_RATE = 22050
DEFAULT_TARGET_SELECTIONS = {
    "Room 1": ["Doctor", "Room 2"],
    "Room 2": ["Doctor", "Room 1"],
    "Room 3": ["Doctor", "Room 1", "Room 2"],
    "Room 4": ["Doctor", "Room 1", "Room 2"],
}
ALERT_SOUND_OPTIONS = [
    "Bright Chime",
    "Soft Ding",
    "Double Ding",
    "Triple Ping",
    "Deep Pulse",
    "Quick Beep",
    "Steady Beep",
    "Rising Tone",
    "Falling Tone",
    "Crisp Bell",
    "Warm Bell",
    "High Alert",
    "Low Alert",
    "Ripple",
    "Classic Pager",
]

# Layout themes for the main UI (refreshed in 1.0.85: calmer page tints,
# borderless shadowed cards, a two-tone accent for gradients, plus Ocean and
# a dark Midnight theme). Keys:
#   accent / accent2 -- gradient ends for the Ready button, chips, slider, logo
#   card_border      -- soft dividers, chip outlines; card outline on dark only
#   emph_dark        -- the newest incoming Ready's sender name in the log
#   highlight        -- the newest incoming Ready's card color (and its arrival
#                       flash fades into it from a stronger accent tint)
#   ink              -- stick-figure animation strokes
THEMES: dict[str, dict] = {
    "Modern Blue": {
        "bg": "#eef2f8", "card_bg": "#ffffff", "accent": "#2563eb", "accent2": "#4f46e5",
        "accent_text": "#ffffff", "title": "#0f172a", "text": "#1e293b", "sub": "#64748b",
        "card_border": "#e2e8f0", "log_bg": "#f8fafc", "log_text": "#475569",
        "status": "#16a34a", "input_bg": "#f8fafc", "input_border": "#cbd5e1",
        "slider_track": "#e2e8f0", "chip_bg": "#f1f5f9", "emph_dark": "#1e3a8a",
        "highlight": "#e0e7ff", "ink": "#1e293b",
    },
    "Sage Clinic": {
        "bg": "#eef3ec", "card_bg": "#ffffff", "accent": "#3f7d58", "accent2": "#2f6f6a",
        "accent_text": "#ffffff", "title": "#1f3a2b", "text": "#253528", "sub": "#6b7f6d",
        "card_border": "#dfe7da", "log_bg": "#f7faf6", "log_text": "#4d6450",
        "status": "#2d8a4e", "input_bg": "#f7faf6", "input_border": "#c8dfc9",
        "slider_track": "#dfe7da", "chip_bg": "#eef3eb", "emph_dark": "#1b3a26",
        "highlight": "#dcebdf", "ink": "#253528",
    },
    "Rose Quartz": {
        "bg": "#fbf0f3", "card_bg": "#ffffff", "accent": "#e11d48", "accent2": "#db2777",
        "accent_text": "#ffffff", "title": "#4c0519", "text": "#3d0a18", "sub": "#9c5a6c",
        "card_border": "#f6dbe2", "log_bg": "#fff7f8", "log_text": "#7a4252",
        "status": "#059669", "input_bg": "#fff7f8", "input_border": "#fbc9d4",
        "slider_track": "#f6dbe2", "chip_bg": "#fdf0f3", "emph_dark": "#4c0519",
        "highlight": "#ffe4ea", "ink": "#3d0a18",
    },
    "Ocean": {
        "bg": "#eaf6f6", "card_bg": "#ffffff", "accent": "#0d9488", "accent2": "#0284c7",
        "accent_text": "#ffffff", "title": "#0f3d3e", "text": "#16343a", "sub": "#5f7c80",
        "card_border": "#d5ebea", "log_bg": "#f5fbfb", "log_text": "#3f5f63",
        "status": "#059669", "input_bg": "#f5fbfb", "input_border": "#bfe0de",
        "slider_track": "#d5ebea", "chip_bg": "#e9f5f4", "emph_dark": "#134e4a",
        "highlight": "#ccf0ec", "ink": "#16343a",
    },
    "Midnight": {
        "bg": "#0b1120", "card_bg": "#141c2f", "accent": "#3b82f6", "accent2": "#06b6d4",
        "accent_text": "#ffffff", "title": "#f1f5f9", "text": "#e2e8f0", "sub": "#8b9ab3",
        "card_border": "#1f2a40", "log_bg": "#0f172a", "log_text": "#b6c2d6",
        "status": "#34d399", "input_bg": "#1b2540", "input_border": "#2a3a5c",
        "slider_track": "#1f2a40", "chip_bg": "#1b2540", "emph_dark": "#ffffff",
        "highlight": "#1e3a5f", "ink": "#e2e8f0",
    },
}
DEFAULT_THEME = "Modern Blue"


def _init_ui_family(root: tk.Misc) -> None:
    """Pick a real Tk font family once (macOS: prefer system UI fonts, then Helvetica)."""
    global _UI_FAMILY
    if _UI_FAMILY is not None:
        return
    if sys.platform == "win32":
        _UI_FAMILY = "Segoe UI"
    elif sys.platform == "darwin":
        try:
            fams = set(tkfont.families(root))
        except Exception:
            fams = set()
        for name in (".SF NS Text", "SF Pro Text", "Helvetica Neue", "Helvetica"):
            if name in fams:
                _UI_FAMILY = name
                break
        else:
            _UI_FAMILY = "Helvetica"
    else:
        _UI_FAMILY = "DejaVu Sans"


def _ui_family() -> str:
    return _UI_FAMILY or "Segoe UI"


def _ui_font(size: int, weight: str = "normal") -> tuple[str, int, str] | tuple[str, int]:
    fam = _ui_family()
    if weight == "bold":
        return (fam, size, "bold")
    return (fam, size)


def _ttk_font(size: int, weight: str = "normal") -> tuple:
    """Tuple for ttk.Style / tk widget font= with resolved UI family."""
    f = _ui_family()
    if weight == "bold":
        return (f, size, "bold")
    return (f, size)


def now_str() -> str:
    return datetime.now().strftime("%I:%M:%S %p")


def now_str_short() -> str:
    """Same as now_str but without seconds. Used in the Ready Messages log so
    timestamps are easier to read at a glance. now_str is preserved for the
    connection diagnostic log and the peer-message envelope where second-
    precision is still useful for debugging."""
    return datetime.now().strftime("%I:%M %p")


def _migrate_old_log_line(text: str) -> str:
    """Normalize a Ready-log line to the 1.0.73 canonical form
    'body\\t[ts]' (no leading tab).

    The tab before [ts] triggers the right-aligned tab stop so the
    timestamp column sits just past the widest body's right edge.
    The body itself starts at x=0 in the Text widget with no leading
    tab character -- that's what aligns it flush with the 'Ready
    Messages' heading above the log.

    Handles all historical input shapes:
      - Pre-1.0.62 ('[ts] body')               → 'body\\t[ts]'
      - 1.0.62 ('body  [ts]', 2 spaces)        → 'body\\t[ts]'
      - 1.0.64 ('body\\t[ts]', no leading tab) → unchanged (already canonical)
      - 1.0.65-1.0.72 ('\\tbody\\t[ts]')       → 'body\\t[ts]' (strip leading tab)
      - Anything unrecognized                  → unchanged pass-through

    Used by _load_ready_log on the persisted ready_log.txt file. The on-
    disk file is intentionally not rewritten -- the migration runs on every
    launch on whatever pre-canonical lines remain in today's file. Cheap,
    and avoids rewrite-during-load edge cases. Tomorrow's file (after
    midnight rollover) is uniformly canonical from the start."""
    # 1.0.65-1.0.72 format: leading tab -- strip it to get canonical form.
    if text.startswith("\t"):
        return text[1:]
    # 1.0.64 layout / already canonical: 'body\t[ts]' (no leading tab).
    if "\t[" in text:
        return text
    # Pre-1.0.62 layout: '[ts] body'.
    if text.startswith("["):
        bracket_end = text.find("] ")
        if bracket_end >= 0:
            ts = text[1:bracket_end]
            body = text[bracket_end + 2:]
            if ts and body:
                return f"{body}\t[{ts}]"
        return text
    # 1.0.62 layout: 'body  [ts]'. The double-space separator was a
    # transition-period choice; replaced with tabs in 1.0.64+ so tab
    # stops can column-align the body (left) and timestamp (right-aligned).
    sep_idx = text.find("  [")
    if sep_idx >= 0:
        body = text[:sep_idx]
        rest = text[sep_idx + 2:]  # includes the '['
        if body and rest.startswith("[") and rest.endswith("]"):
            return f"{body}\t{rest}"
    return text


def _split_log_line(text: str) -> tuple[str, str]:
    """'body\t[ts]' (any historical layout, see _migrate_old_log_line) ->
    ('body', 'ts'). ts is '' when the line has none."""
    text = _migrate_old_log_line(text.rstrip())
    body, _sep, rest = text.partition("\t")
    ts = rest.strip()
    if ts.startswith("[") and ts.endswith("]"):
        ts = ts[1:-1].strip()
    return body.strip(), ts


def _strip_seconds_from_ts(ts: str) -> str:
    """Normalize a timestamp string by removing the seconds component.
    Handles cross-version compatibility: a peer running an older app version
    may send timestamps like '02:30:45 PM' in its Ready broadcast; this
    receiver displays them as '02:30 PM'. If the input already has no seconds
    (or doesn't match the pattern), returns it unchanged."""
    return re.sub(r"(\d{1,2}:\d{2}):\d{2}(\s*[AaPp][Mm])", r"\1\2", ts)


def _today_iso() -> str:
    """Local date as YYYY-MM-DD. Used as the persistent-log day key."""
    return datetime.now().strftime("%Y-%m-%d")


def detect_local_ip() -> str:
    """
    Best-effort local LAN IP detection without external dependency.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        probe.close()


def local_ipv4_addresses() -> set[str]:
    """Addresses that identify this machine (skip connecting to ourselves).

    NB: the getaddrinfo() call can block (name resolution), so this must NOT be
    called on the Tk UI thread in a hot loop -- see
    ChairsideReadyAlertApp._maybe_refresh_local_ips, which runs it on a worker
    thread no more than every LOCAL_IP_REFRESH_SEC.
    """
    ips: set[str] = {"127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
        for res in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = res[4][0]
            if ip:
                ips.add(ip)
    except Exception:
        pass
    probe_ip = detect_local_ip()
    if probe_ip:
        ips.add(probe_ip)
    return ips


def _directed_broadcasts(local_ips: set[str]) -> list[str]:
    """Derive /24 directed subnet broadcast addresses (a.b.c.255) from this
    machine's IPv4 addresses. A directed broadcast is delivered more reliably
    than the 255.255.255.255 limited broadcast -- the latter egresses only ONE
    interface (chosen by the default route) and is dropped by some managed
    switches and firewall profiles. Sending to both makes re-discovery after a
    Wi-Fi/ethernet failover or DHCP renumber far more robust. Assumes a /24,
    which covers essentially all small-office LANs; 255.255.255.255 remains the
    catch-all for anything else."""
    out: list[str] = []
    for ip in local_ips:
        if ":" in ip or ip.startswith("127."):
            continue
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        try:
            if all(0 <= int(p) <= 255 for p in parts):
                directed = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                if directed not in out:
                    out.append(directed)
        except ValueError:
            continue
    return out


def _read_lock_pid(lock_path: str) -> Optional[int]:
    """PID recorded in the single-instance lock file (written by
    SingleInstanceLock.acquire), or None if unreadable/blank."""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    """True if a process with this PID currently exists. Used by the stale-lock
    recovery in main() so the instance lock is only removed when its recorded
    owner is genuinely gone -- never yanked out from under a live but
    slow-to-start first instance (which on Windows would let two instances run,
    and on POSIX wouldn't release the live flock anyway). On any uncertainty we
    report alive, because a false 'dead' is the dangerous direction."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER -> no such PID (dead).
                # Anything else (e.g. 5 = ACCESS_DENIED) -> exists (alive).
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class ConfigStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = {
            "label": "",
            # server_host is retained only for one-time migration of very old
            # configs into manual_peer_ips (see load()); server_port is the live
            # TCP-port persistence key. The old is_server flag was vestigial (the
            # app is fully peer-to-peer) and has been removed.
            "server_host": "",
            "server_port": DEFAULT_PORT,
            "manual_peer_ips": "",
            "alert_sound": ALERT_SOUND_OPTIONS[0],
            "alert_volume": 50,
            "custom_station_labels": [],
            # User-editable list of station names available for the Station name
            # combobox and the Defaults menu. Authoritative once present; on first
            # launch after upgrade, migrated from DEFAULT_LABELS + custom_station_labels.
            # See ChairsideReadyAlertApp._effective_station_labels.
            "station_labels": [],
            # Tombstones for station_labels. Persisted across restarts so a label that
            # this workstation removed isn't silently resurrected by another peer's
            # beacon. An explicit Add (here or remotely) clears the tombstone.
            "removed_labels": [],
            "default_targets": [],
            "theme": DEFAULT_THEME,
            "tray_visibility_help_shown": False,
            "update_manifest_url": "",
            "autostart_enabled": True,
            # Microsoft Store paid-app trial gating (frozen builds only).
            # first_launch_iso is stamped on the first launch where this field is empty.
            "first_launch_iso": "",
            # Which reminder thresholds (from TRIAL_REMINDER_DAYS) have already
            # been shown to this user, so we never pop the same modal twice.
            "trial_reminders_shown": [],
            # Cached last-known Store license state. Refreshed in the background
            # on launch + on demand. We trust this even if a later Store query
            # returns None (unknown), so a temporarily flaky Store API doesn't
            # lock out a paying user.
            "paid_unlocked": False,
        }
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                incoming = json.load(f)
            if isinstance(incoming, dict):
                self.data.update(incoming)
                self.data.pop("station_setup_done", None)
                # Migrate old single server IP to optional manual peers list.
                if self.data.get("server_host") and not str(self.data.get("manual_peer_ips", "")).strip():
                    self.data["manual_peer_ips"] = str(self.data.get("server_host", "")).strip()
        except Exception:
            pass

    def save(self) -> None:
        """Atomic write so a crash mid-save does not leave a corrupt half-written JSON file."""
        parent = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{CONFIG_FILE}.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


class LanDiscovery:
    """UDP LAN beacons so workstations find each other without a fixed server IP."""

    def __init__(
        self,
        label: str,
        tcp_port: int,
        local_ips: set[str],
        ui_queue: queue.Queue,
    ) -> None:
        self.label = label
        self.tcp_port = tcp_port
        self.local_ips = local_ips
        self.ui_queue = ui_queue
        self.running = False
        self.peers: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._b_sock: Optional[socket.socket] = None
        self._l_sock: Optional[socket.socket] = None
        # Handle to the listen thread so the App's sync tick can detect a
        # silently-dead listener (is_listen_alive) and restart discovery.
        self._listen_thread: Optional[threading.Thread] = None
        # Shared station-label state piggybacked on the beacon. The App sets these
        # via update_station_lists; LanDiscovery reads them when sending each beacon.
        self._station_labels: list[str] = []
        self._removed_labels: list[str] = []

    def update_label(self, label: str) -> None:
        # Caller (ChairsideReadyAlertApp.start_network) refuses to start the
        # network until a real station name is picked, so an empty string here
        # should be unreachable. Kept as "" rather than the old "Room 1"
        # fallback so a regression can't silently revive the fresh-install
        # collision bug -- an empty beacon label is visibly wrong; "Room 1" is
        # invisibly wrong.
        self.label = label.strip()

    def update_tcp_port(self, port: int) -> None:
        self.tcp_port = port

    def update_station_lists(self, labels: list[str], removed: list[str]) -> None:
        with self.lock:
            self._station_labels = list(labels)
            self._removed_labels = list(removed)

    def broadcast_label_event(self, action: str, label: str) -> None:
        """Send an immediate UDP broadcast announcing an add/remove of a station label."""
        if action not in ("add", "remove"):
            return
        sock = self._b_sock
        if sock is None:
            return
        try:
            payload = json.dumps({
                "type": "label_event",
                "v": BEACON_VERSION,
                "iid": _INSTANCE_ID,
                "action": action,
                "label": str(label),
                "ts": time.time(),
            }).encode("utf-8")
            sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
        except Exception:
            pass

    def _make_listen_socket(self) -> Optional[socket.socket]:
        """Create+bind the UDP listen socket on DISCOVERY_PORT, or None on any
        failure (caller backs off and retries). Bound to INADDR_ANY so a plain
        DHCP IP renumber doesn't invalidate it. Fully exception-safe so even fd
        exhaustion makes the loop retry rather than killing the listen thread."""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", DISCOVERY_PORT))
            s.settimeout(1.0)
            return s
        except OSError:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            return None

    def _make_broadcast_socket(self) -> Optional[socket.socket]:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", 0))
            return s
        except OSError:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            return None

    def _sleep_running(self, seconds: float) -> None:
        """Sleep in 100ms slices so stop() (which flips self.running) is honored
        promptly instead of after a full backoff interval."""
        for _ in range(max(1, int(seconds / 0.1))):
            if not self.running:
                return
            time.sleep(0.1)

    def start(self) -> None:
        self.running = True
        # Pre-create both sockets so broadcast_label_event has a socket
        # immediately and the first beacon goes out without waiting for a loop
        # iteration. The loops own re-creation from here on.
        self._l_sock = self._make_listen_socket()
        self._b_sock = self._make_broadcast_socket()
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        for s in (self._b_sock, self._l_sock):
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._b_sock = None
        self._l_sock = None

    def is_listen_alive(self) -> bool:
        """Whether the discovery listener is healthy. False only signals a
        recoverable death (the thread exited unexpectedly while running). A
        never-started listener -- e.g. DISCOVERY_PORT was busy, where a restart
        wouldn't free the port -- reports True so the App's watchdog doesn't
        churn restarting it."""
        if not self.running:
            return True
        t = self._listen_thread
        if t is None:
            return True
        return t.is_alive()

    def _broadcast_loop(self) -> None:
        # Self-healing send loop. The bound interface can vanish mid-session
        # (DHCP renew, Wi-Fi roam, VPN up/down, NIC power-cycle) WITHOUT a
        # >150s sleep gap, after which sendto begins failing permanently. Rather
        # than silently looping forever on a dead socket, count failures and
        # rebuild after BROADCAST_FAIL_REBUILD consecutive misses.
        fails = 0
        while self.running:
            sock = self._b_sock
            if sock is None:
                sock = self._make_broadcast_socket()
                self._b_sock = sock
                if sock is None:
                    self._sleep_running(BEACON_INTERVAL)
                    continue
            try:
                with self.lock:
                    labels_snap = list(self._station_labels)
                    removed_snap = list(self._removed_labels)
                payload = json.dumps(
                    {
                        "type": "dm_beacon",
                        "v": BEACON_VERSION,
                        "iid": _INSTANCE_ID,
                        "label": self.label,
                        "tcp": self.tcp_port,
                        # Eventual-consistency snapshot so newly-online peers
                        # converge on the same shared list without needing every
                        # add/remove event to have been received.
                        "labels": labels_snap,
                        "removed": removed_snap,
                    }
                ).encode("utf-8")
                # Send to the limited broadcast AND each directed /24 broadcast
                # so beacons reach peers even when 255.255.255.255 is dropped or
                # egresses the wrong adapter after a Wi-Fi/ethernet failover.
                sent_any = False
                last_exc: Optional[Exception] = None
                for dest in ["255.255.255.255", *_directed_broadcasts(self.local_ips)]:
                    try:
                        sock.sendto(payload, (dest, DISCOVERY_PORT))
                        sent_any = True
                    except OSError as exc:
                        last_exc = exc
                if sent_any:
                    fails = 0
                elif last_exc is not None:
                    raise last_exc
            except Exception:
                fails += 1
                if fails >= BROADCAST_FAIL_REBUILD:
                    self.ui_queue.put(("status", "Discovery broadcast failing -- rebuilding the socket."))
                    try:
                        sock.close()
                    except Exception:
                        pass
                    self._b_sock = None
                    fails = 0
            self._sleep_running(BEACON_INTERVAL)

    def _listen_loop(self) -> None:
        # Outer re-bind loop mirrors MessageServer._run_server: a single
        # recvfrom OSError (NIC flap, adapter reset, sleep/wake) must NOT kill
        # discovery permanently. The pre-hardening code did `except OSError:
        # break` and the thread exited for good -- the station then went
        # silently offline (no beacons received -> every peer pruned at
        # PEER_STALE_SEC -> appears offline and can't target anyone) until a
        # full LAN bounce. Now we close the dead socket, back off, re-bind on
        # DISCOVERY_PORT, and continue.
        announced_fail = False
        while self.running:
            sock = self._l_sock
            if sock is None:
                sock = self._make_listen_socket()
                self._l_sock = sock
                if sock is None:
                    if not announced_fail:
                        self.ui_queue.put((
                            "status",
                            f"UDP discovery port {DISCOVERY_PORT} unavailable -- use manual peer IPs "
                            "if others do not appear. Retrying.",
                        ))
                        announced_fail = True
                    self._sleep_running(5.0)
                    continue
                announced_fail = False
            while self.running:
                try:
                    data, addr = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break  # drop to outer loop -> close + re-bind
                try:
                    self._handle_discovery_packet(data, addr)
                except Exception:
                    # A single malformed/unexpected packet must never take the
                    # listener down -- skip it and keep receiving.
                    continue
            # Inner loop exited: close the (likely dead) socket so the outer
            # loop binds a fresh one (or exits cleanly if stop() ran).
            try:
                if self._l_sock is not None:
                    self._l_sock.close()
            except Exception:
                pass
            self._l_sock = None

    def _handle_discovery_packet(self, data: bytes, addr) -> None:
        ip = addr[0]
        if ip in self.local_ips:
            return
        try:
            obj = json.loads(data.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return
        if int(obj.get("v", 0)) != BEACON_VERSION:
            return
        # Drop our OWN beacons by instance ID, regardless of source IP. The
        # ip-in-local_ips check above misses a self-beacon that loops back via a
        # second adapter whose address local_ipv4_addresses() never enumerated
        # (a multi-homed Ethernet + Wi-Fi box). Matching our per-process
        # _INSTANCE_ID catches it no matter which interface the echo arrived on,
        # so a machine can never mistake itself for a duplicate station. (Older
        # peers send no "iid"; their beacons never match and process normally.)
        if obj.get("iid") == _INSTANCE_ID:
            return
        msg_type = obj.get("type")
        if msg_type == "dm_beacon":
            label = str(obj.get("label", "")).strip() or "Unknown"
            try:
                tcp = int(obj.get("tcp", DEFAULT_PORT))
            except (TypeError, ValueError):
                tcp = DEFAULT_PORT
            with self.lock:
                self.peers[ip] = {"label": label, "tcp": tcp, "last_seen": time.time()}
            self.ui_queue.put(("discovery", None))
            # Optional shared-list snapshot. Older peers (pre-1.0.10) won't include
            # these fields; they're treated as empty and don't advertise removals.
            inc_labels = obj.get("labels")
            inc_removed = obj.get("removed")
            if isinstance(inc_labels, list) or isinstance(inc_removed, list):
                self.ui_queue.put((
                    "labels_snapshot",
                    ([str(x) for x in (inc_labels or [])],
                     [str(x) for x in (inc_removed or [])]),
                ))
        elif msg_type == "label_event":
            action = str(obj.get("action", "")).lower()
            label = str(obj.get("label", "")).strip()
            if action in ("add", "remove") and label:
                self.ui_queue.put(("label_event", (action, label)))

    def prune_stale(self, max_age: float = PEER_STALE_SEC) -> None:
        now = time.time()
        dead: list[str] = []
        with self.lock:
            for ip, info in list(self.peers.items()):
                if now - info["last_seen"] > max_age:
                    dead.append(ip)
            for ip in dead:
                self.peers.pop(ip, None)
        if dead:
            self.ui_queue.put(("discovery", None))

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self.peers.items()}


def _enable_tcp_keepalive(sock: socket.socket) -> None:
    """Enable aggressive TCP keepalive on a connected socket so an ungraceful
    (no FIN/RST) peer death -- the overnight failure where a peer sleeps, a NIC
    power-saves, or a switch ages out the idle flow -- is detected in about a
    minute instead of the ~2h Windows default. Fully exception-safe: any failure
    leaves the socket usable exactly as before, so it can never introduce a new
    socket-setup crash (which would itself be a silent-failure regression)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass
    # Windows: the SIO_KEEPALIVE_VALS ioctl takes a plain (onoff, idle_ms,
    # interval_ms) int tuple -- no struct.pack needed. The retry COUNT is fixed
    # at 10 by the OS, so a fully dead peer is declared dead about
    # idle + 10*interval seconds (here ~30 + 10*5 = ~80s) after the last byte.
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, KEEPALIVE_IDLE_SEC * 1000, KEEPALIVE_INTVL_SEC * 1000),
            )
        except Exception:
            pass
        return
    # Linux: per-socket idle/interval/count at IPPROTO_TCP (values in seconds).
    if hasattr(socket, "TCP_KEEPIDLE"):
        for opt, val in (
            (socket.TCP_KEEPIDLE, KEEPALIVE_IDLE_SEC),
            (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTVL_SEC),
            (getattr(socket, "TCP_KEEPCNT", None), 4),
        ):
            if opt is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
            except Exception:
                pass
        return
    # macOS: TCP_KEEPALIVE is the idle-seconds option (no per-socket intvl/cnt).
    if hasattr(socket, "TCP_KEEPALIVE"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, KEEPALIVE_IDLE_SEC)
        except Exception:
            pass


class MessageServer:
    def __init__(self, host: str, port: int, ui_queue: queue.Queue) -> None:
        self.host = host
        self.port = port
        self.ui_queue = ui_queue
        self.server_socket = None
        self.running = False
        self.clients = {}
        self.client_sockets = set()
        self.lock = threading.Lock()

    def start(self) -> None:
        self.running = True
        thread = threading.Thread(target=self._run_server, daemon=True)
        thread.start()

    def stop(self) -> None:
        self.running = False
        with self.lock:
            sockets = list(self.client_sockets)
            self.client_sockets.clear()
            self.clients.clear()
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

    def _run_server(self) -> None:
        # Outer loop survives transient bind/accept failures. The classic
        # offender is macOS sleep/wake: a BSD TCP listening socket commonly
        # returns OSError on the first accept() call after the system wakes,
        # with no in-kernel recovery. Pre-1.0.60 the server thread just
        # exited on that OSError and the app silently stopped receiving any
        # incoming Ready alerts (discovery is UDP and behaves differently,
        # so the peer list kept looking healthy) until the user restarted
        # the app. Now we close the dead socket and re-bind on the same
        # port. Fix B (NSWorkspaceDidWakeNotification) handles wake events
        # proactively; this loop is the always-on safety net for any other
        # socket-level surprise.
        while self.running:
            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind((self.host, self.port))
                self.server_socket.listen(20)
                self.server_socket.settimeout(1.0)
            except OSError as exc:
                self.ui_queue.put(("status", f"Could not bind {self.host}:{self.port}: {exc}. Retrying in 5s..."))
                try:
                    if self.server_socket:
                        self.server_socket.close()
                except Exception:
                    pass
                self.server_socket = None
                # Sleep in 100 ms slices so a concurrent stop() returns quickly.
                for _ in range(50):
                    if not self.running:
                        return
                    time.sleep(0.1)
                continue

            self.ui_queue.put(("status", f"Listening for peers on {self.host}:{self.port}"))

            while self.running:
                try:
                    client_sock, addr = self.server_socket.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.ui_queue.put(("status", f"Listener accept() failed ({exc}); re-binding..."))
                    break
                with self.lock:
                    self.client_sockets.add(client_sock)
                threading.Thread(
                    target=self._handle_client, args=(client_sock, addr), daemon=True
                ).start()

            # Inner loop exited — close the (likely dead) socket before the
            # outer loop attempts a fresh bind on the next iteration.
            try:
                if self.server_socket:
                    self.server_socket.close()
            except Exception:
                pass
            self.server_socket = None

    def _handle_client(self, client_sock: socket.socket, addr) -> None:
        label = None
        buffer = ""
        client_sock.settimeout(1.0)
        # Keepalive on the ACCEPTED (inbound) socket. This is a peer's
        # cross-machine connection INTO our server, and it is the socket
        # _relay_chat writes a Ready into when delivering to the label
        # registered on it. If it half-died overnight, that relay sendall would
        # vanish silently; keepalive makes the recv() below raise OSError so we
        # break, run the cleanup that pops the dead label, and the peer's
        # reconnect re-registers a live socket. (NB: the 127.0.0.1 loopback
        # self-connection -- registered under our OWN label -- is load-bearing
        # for receiving our own relayed alerts and must not be removed.)
        _enable_tcp_keepalive(client_sock)
        self.ui_queue.put(("status", f"Peer connected from {addr[0]}"))
        rejected_duplicate = False
        while self.running:
            try:
                data = client_sock.recv(BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buffer += data.decode("utf-8", errors="ignore")
            if len(buffer) > MAX_INBOUND_LINE_BYTES and "\n" not in buffer:
                # A peer streaming a huge payload with no newline delimiter would
                # grow this buffer without bound. Real messages are tiny; treat
                # this as a broken/hostile stream and drop the connection.
                self.ui_queue.put(("status", f"Dropping {addr[0]}: oversized message with no delimiter."))
                break
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = payload.get("type")
                if msg_type == "hello":
                    proposed = payload.get("label", "Unknown")
                    with self.lock:
                        existing = self.clients.get(proposed)
                    if existing is not None and existing is not client_sock:
                        # Another peer already owns this label. The old code
                        # kicked the existing socket and admitted the new one
                        # — on a LAN where multiple machines still held the
                        # default "Room 1" label, that became an unbounded
                        # connect/kick/reconnect storm that pegged the main
                        # thread. Keep the existing socket; refuse this one.
                        # Hold the rejected socket briefly so older peers
                        # without client-side backoff can't retry-flood us.
                        rejected_duplicate = True
                        time.sleep(3)
                        break
                    with self.lock:
                        label = proposed
                        self.clients[label] = client_sock
                    self._broadcast_presence()
                    self.ui_queue.put(("status", f"Registered: {label}"))
                elif msg_type == "chat":
                    self._relay_chat(payload)
                elif msg_type == "ping":
                    self._send_json(client_sock, {"type": "pong", "ts": time.time()})
            if rejected_duplicate:
                break

        with self.lock:
            if label and label in self.clients and self.clients[label] is client_sock:
                self.clients.pop(label, None)
            self.client_sockets.discard(client_sock)
        try:
            client_sock.close()
        except Exception:
            pass
        if rejected_duplicate:
            self.ui_queue.put(("status", f"Rejected duplicate-label hello from {addr[0]} (label already in use)"))
        else:
            self._broadcast_presence()
            if label:
                self.ui_queue.put(("status", f"Disconnected: {label}"))

    def _relay_chat(self, payload: dict) -> None:
        targets = payload.get("to", [])
        msg = payload.get("message", "")
        sender = payload.get("from", "Unknown")
        timestamp = payload.get("timestamp", now_str())
        envelope = {
            "type": "chat",
            "from": sender,
            "to": targets,
            "message": msg,
            "timestamp": timestamp,
            "alert_sound": payload.get("alert_sound", ""),
            "alert_volume": payload.get("alert_volume", 70),
        }
        with self.lock:
            if "ALL" in targets:
                sockets = list(self.client_sockets)
            else:
                sockets = self._resolve_target_sockets(targets)
        for sock in sockets:
            self._send_json(sock, envelope)

    def _resolve_target_sockets(self, targets) -> list[socket.socket]:
        resolved = []
        seen = set()
        for target in targets:
            sock = self.clients.get(target)
            if sock is not None:
                ident = id(sock)
                if ident not in seen:
                    resolved.append(sock)
                    seen.add(ident)
                continue

            target_norm = str(target).strip().lower()
            for label, candidate in self.clients.items():
                label_norm = str(label).strip().lower()
                if label_norm == target_norm or label_norm.startswith(target_norm + "-"):
                    ident = id(candidate)
                    if ident not in seen:
                        resolved.append(candidate)
                        seen.add(ident)
        return resolved

    def _broadcast_presence(self) -> None:
        with self.lock:
            labels = sorted(self.clients.keys())
            sockets = list(self.client_sockets)
        payload = {"type": "presence", "labels": labels}
        for sock in sockets:
            self._send_json(sock, payload)

    def _send_json(self, sock: socket.socket, payload: dict) -> None:
        try:
            line = json.dumps(payload) + "\n"
            sock.sendall(line.encode("utf-8"))
        except Exception:
            pass


class MessageClient:
    def __init__(self, host: str, port: int, label: str, ui_queue: queue.Queue) -> None:
        self.host = host
        self.port = port
        self.label = label
        self.ui_queue = ui_queue
        self.sock = None
        self.running = False
        self.lock = threading.Lock()
        # Monotonic time of the last byte received on self.sock (set on connect,
        # refreshed on every recv-with-data including heartbeat pongs). The sync
        # loop reads it to spot a socket that has gone silent. Only the client
        # thread writes it and only the sync loop reads it -- a single float, so
        # no lock is required and a stale read just delays eviction by one tick.
        self.last_rx = time.monotonic()

    def connect(self) -> None:
        self.running = True
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def disconnect(self) -> None:
        self.running = False
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def send_chat(self, targets, text: str, alert_sound: str = "", alert_volume: int = 70) -> None:
        payload = {
            "type": "chat",
            "from": self.label,
            "to": targets,
            "message": text,
            # Broadcast the short HH:MM timestamp so newer peers display it
            # cleanly. Older peers will accept the shorter string unchanged.
            "timestamp": now_str_short(),
            "alert_sound": alert_sound,
            "alert_volume": alert_volume,
        }
        self._send_json(payload)

    def _run(self) -> None:
        while self.running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((self.host, self.port))
                sock.settimeout(1.0)
                # Arm keepalive so an ungraceful (no FIN/RST) death of THIS
                # outbound socket -- the overnight black hole that silently
                # swallowed Ready alerts -- surfaces as an OSError on recv
                # within ~1 min instead of the ~2h Windows default.
                _enable_tcp_keepalive(sock)
                self.last_rx = time.monotonic()
                with self.lock:
                    self.sock = sock
                self.ui_queue.put(("status", f"Connected to {self.host}:{self.port}"))
                self._send_json({"type": "hello", "label": self.label})
                self._listen_loop(sock)
            except Exception:
                self.ui_queue.put(("status", f"Disconnected from {self.host}:{self.port}. Retrying…"))
            # Always back off before reconnecting. Without this, a server that
            # closes us cleanly (e.g. another peer with the same label took
            # over) is reconnected to instantly — on a LAN where multiple
            # machines still hold the default "Room 1" label, that turned
            # into a connect/kick/reconnect storm that pegged the server's
            # main thread.
            if self.running:
                time.sleep(3)

    def _listen_loop(self, sock: socket.socket) -> None:
        buffer = ""
        # ~1 increment per second (the recv timeout below is 1.0s). When it
        # reaches HEARTBEAT_INTERVAL_SEC we push a ping so a dead path is
        # detected here instead of looping on recv-timeout forever against a
        # silent half-open socket. Reset whenever real data arrives.
        idle_secs = 0
        while self.running:
            try:
                data = sock.recv(BUFFER_SIZE)
            except socket.timeout:
                idle_secs += 1
                if idle_secs >= HEARTBEAT_INTERVAL_SEC:
                    idle_secs = 0
                    # Send DIRECTLY, not via self._send_json -- its bare
                    # except:pass would swallow the very OSError we need to see.
                    # A failed heartbeat send breaks the loop; _run then
                    # reconnects under its existing 3s backoff (so no storm).
                    try:
                        sock.sendall(
                            (json.dumps({"type": "ping", "ts": time.time()}) + "\n").encode("utf-8")
                        )
                    except OSError:
                        break
                continue
            except OSError:
                break
            if not data:
                break
            self.last_rx = time.monotonic()
            idle_secs = 0
            buffer += data.decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.ui_queue.put(("network", payload))
        try:
            sock.close()
        except Exception:
            pass
        with self.lock:
            if self.sock is sock:
                self.sock = None

    def _send_json(self, payload: dict) -> None:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        with self.lock:
            sock = self.sock
        if not sock:
            return
        try:
            sock.sendall(line)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Drawing helpers + custom widgets (1.0.85 visual refresh).
#
# Everything is plain Tk Canvas drawing (smooth polygons, lines, ovals,
# text), so it renders the same on Windows and macOS with no extra
# dependencies. Geometry goes through _px() so it scales with the font's
# pixel size: Tk on Windows draws a point as ~1.33px (96 dpi) vs ~1px on
# macOS, and fixed pixel sizes tuned on one platform would crowd the text
# on the other. Check marks and the play triangle are drawn as shapes, not
# glyphs, so they never depend on a font having the character.
# ---------------------------------------------------------------------------

_UI_SCALE = 1.0
# Windows only: physical pixels per 96-dpi pixel (1.25 at 125% display
# scaling, 1.5 at 150%). The process is DPI-aware (see
# _enable_windows_dpi_awareness), so Tk draws in real pixels and text is
# crisp instead of bitmap-stretched; plain pixel numbers (padding,
# dialog sizes, line widths) go through _dz() so they keep their size.
# _px() sizes already include it via `tk scaling`. Always 1.0 on macOS.
_DPI_ZOOM = 1.0


def _enable_windows_dpi_awareness() -> None:
    """Call BEFORE the first tk.Tk(). System-DPI-aware (not per-monitor:
    Tk 8.6 doesn't handle WM_DPICHANGED), so Windows stops bitmap-
    stretching the window -- the blur on 125%/150% displays."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _dz(n: float) -> int:
    return int(round(n * _DPI_ZOOM))

# Windows only: shrink every font (and the _px() geometry that follows it)
# by this factor. At Tk's default 96-dpi scaling the Windows UI read too
# large next to the Mac version (user request, 1.0.89). Lowering
# `tk scaling` shrinks all point-sized fonts at once, including ttk styles.
WINDOWS_TEXT_SCALE = 0.85


def _init_ui_scale(root: tk.Misc) -> None:
    global _UI_SCALE
    try:
        s = float(root.tk.call("tk", "scaling"))
    except Exception:
        s = 1.0
    if sys.platform.startswith("win"):
        global _DPI_ZOOM
        # tk scaling is pixels per point: 96 dpi -> 1.333.
        _DPI_ZOOM = max(1.0, min(3.0, s * 72.0 / 96.0))
        try:
            _ready_animations.set_ui_zoom(_DPI_ZOOM)
        except Exception:
            pass
        try:
            s *= WINDOWS_TEXT_SCALE
            root.tk.call("tk", "scaling", s)
        except Exception:
            pass
    _UI_SCALE = max(1.0, min(3.0, s))


# Windows only: the light themes' page color (the tint between the cards)
# reads much paler on typical Windows monitors than on a Mac display, so
# deepen it toward the accent there and give the card shadows more weight
# (user request, 1.0.90).
WINDOWS_PAGE_TINT = 0.10      # blend of the accent into the page color
WINDOWS_SHADOW_BOOST = 1.6    # multiplier on RoundedCard shadow strength
_WINDOWS_THEME_TWEAKED = False


def _apply_windows_theme_tweaks() -> None:
    global _WINDOWS_THEME_TWEAKED
    if _WINDOWS_THEME_TWEAKED or not sys.platform.startswith("win"):
        return
    _WINDOWS_THEME_TWEAKED = True
    for th in THEMES.values():
        if _is_dark(th["bg"]):
            continue
        th["bg"] = _mix(_mix(th["bg"], th["accent"], WINDOWS_PAGE_TINT), th["title"], 0.03)


def _usable_screen_size(root: tk.Misc) -> tuple[int, int]:
    """(width, height) in Tk units that a top-level window can use: the
    work area (screen minus taskbar) on Windows, minus room for the title
    bar. Falls back to the full screen minus a margin elsewhere."""
    w, h = root.winfo_screenwidth(), root.winfo_screenheight() - 90
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA; coordinates match Tk's for a non-DPI-aware process.
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                w, h = rect.right - rect.left, rect.bottom - rect.top - _dz(40)
        except Exception:
            pass
    return w, h


def _px(n: float) -> int:
    return int(round(n * _UI_SCALE))


def _mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colors. t=0 -> c1, t=1 -> c2."""
    t = max(0.0, min(1.0, t))
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return c1
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _is_dark(color: str) -> bool:
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 90


def _rrect(cv: tk.Canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def _gradient_rrect(cv: tk.Canvas, x0, y0, x1, y1, r, c1: str, c2: str) -> None:
    """Left-to-right gradient inside a rounded rectangle. The rounded ends
    are filled with 1px lines clipped to the corner arcs; the straight
    middle uses a bounded number of bands so wide buttons stay cheap to
    redraw during hover / press animations."""
    _rrect(cv, x0, y0, x1, y1, r, fill=_mix(c1, c2, 0.5), outline="")
    if c1 == c2:
        _rrect(cv, x0, y0, x1, y1, r, fill=c1, outline="")
        return
    w = x1 - x0
    if w < 3:
        return
    r = min(r, w / 2, (y1 - y0) / 2)

    def col(x):
        return _mix(c1, c2, (x - x0) / w)

    x = x0 + 1
    while x < x0 + r:
        d = x0 + r - x
        inset = r - math.sqrt(max(0.0, r * r - d * d))
        cv.create_line(x, y0 + inset + 1, x, y1 - inset - 1, fill=col(x))
        x += 1
    mid_l, mid_r = x0 + r, x1 - r
    bands = max(1, min(96, int((mid_r - mid_l) / 3)))
    bw = (mid_r - mid_l) / bands
    for i in range(bands):
        bx0 = mid_l + i * bw
        cv.create_rectangle(bx0, y0, bx0 + bw + 1, y1, fill=col(bx0 + bw / 2), outline="")
    x = x1 - r
    while x < x1 - 1:
        d = x - (x1 - r)
        inset = r - math.sqrt(max(0.0, r * r - d * d))
        cv.create_line(x, y0 + inset + 1, x, y1 - inset - 1, fill=col(x))
        x += 1


def _draw_check(cv: tk.Canvas, cx, cy, size, color: str, width: float = 2.0) -> None:
    s = size / 2
    cv.create_line(cx - s, cy, cx - s * 0.25, cy + s * 0.7, cx + s, cy - s * 0.7,
                   fill=color, width=width, capstyle="round", joinstyle="round")


def _initials(name: str) -> str:
    parts = name.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[0][0].upper() + parts[-1][:2]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:1].upper() or "?"


def _elide(font: tkfont.Font, text: str, max_w: int) -> str:
    """Trim `text` with an ellipsis so it fits in max_w pixels."""
    if max_w <= 0 or font.measure(text) <= max_w:
        return text
    while text and font.measure(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


class RoundedCard(tk.Frame):
    """A Frame whose background is a Canvas-drawn rounded card with a soft
    layered drop shadow. The 1px outline is only drawn when `border` is set
    (dark themes, where a shadow alone doesn't separate card from page)."""

    def __init__(self, parent, bg: str = "#ffffff", outer_bg: str = "#f0f4ff",
                 radius: int = 16, padding: int = 14, border: str | None = None, **kw) -> None:
        super().__init__(parent, bg=outer_bg, **kw)
        self._card_bg = bg
        self._outer_bg = outer_bg
        self._border = border
        self._r = _px(radius)
        self._pad = _px(padding)
        # Room around the card for the shadow: left, top, right, bottom.
        self._m = (_px(6), _px(4), _px(6), _px(10))
        # True: the inner frame is stretched to the card's full height (the
        # Ready Messages card, so the log grows with the window). False: the
        # card hugs its content.
        self.fill_height = False

        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=outer_bg)
        self._cv.pack(fill="both", expand=True)

        self.inner_frame = tk.Frame(self._cv, bg=bg)
        self._win = self._cv.create_window(self._m[0] + self._pad, self._m[1] + self._pad,
                                           window=self.inner_frame, anchor="nw")
        self._cv.bind("<Configure>", self._on_cv_resize)
        self.inner_frame.bind("<Configure>", self._on_inner_resize)

    def _on_cv_resize(self, e: tk.Event) -> None:
        self._draw(e.width, e.height)
        # Stretch inner content to full card width so rows (e.g. Ready) can center in the window.
        iw = max(1, int(e.width) - 2 * self._pad - self._m[0] - self._m[2])
        self._cv.itemconfigure(self._win, width=iw)
        if self.fill_height:
            ih = max(1, int(e.height) - 2 * self._pad - self._m[1] - self._m[3])
            self._cv.itemconfigure(self._win, height=ih)

    def _on_inner_resize(self, e: tk.Event) -> None:
        # Height follows content; width comes from canvas (fill="x") + _on_cv_resize.
        # A fill_height card requests its content's NATURAL height (not the
        # stretched height it was just given) so it can still shrink.
        h = self.inner_frame.winfo_reqheight() if self.fill_height else e.height
        self._cv.configure(height=h + self._pad * 2 + self._m[1] + self._m[3])

    def _draw(self, w: int, h: int) -> None:
        if w < 4 or h < 4:
            return
        self._cv.delete("rr")
        x0, y0 = self._m[0], self._m[1]
        x1, y1 = w - self._m[2] - 1, h - self._m[3] - 1
        dark = _is_dark(self._outer_bg)
        shadow = "#000000" if dark else "#0f172a"
        k = 2.2 if dark else 1.0
        if sys.platform.startswith("win"):
            k *= WINDOWS_SHADOW_BOOST
        for grow, a, dy in ((_px(6), 0.025, _px(4)), (_px(4), 0.04, _px(3)),
                            (_px(2), 0.06, _px(2)), (1, 0.08, 1)):
            _rrect(self._cv, x0 - grow, y0 - grow + dy, x1 + grow, y1 + grow + dy,
                   self._r + grow, fill=_mix(self._outer_bg, shadow, a * k),
                   outline="", tags="rr")
        _rrect(self._cv, x0, y0, x1, y1, self._r, fill=self._card_bg,
               outline=self._border or "", width=1 if self._border else 0, tags="rr")
        self._cv.tag_lower("rr")

    def update_colors(self, bg: str, outer_bg: str, border: str | None = None) -> None:
        self._card_bg = bg
        self._outer_bg = outer_bg
        self._border = border
        self.configure(bg=outer_bg)
        self._cv.configure(bg=outer_bg)
        self.inner_frame.configure(bg=bg)
        self._draw(self._cv.winfo_width(), self._cv.winfo_height())


class ReadyLog(tk.Canvas):
    """The Ready Messages log.

    Incoming Readys are the point of the app, so they dominate: the newest
    sits at the very BOTTOM in a large highlighted card (theme-colored avatar,
    "Room 3 / is ready for you", NEW badge, pulse ring on arrival); the
    second-newest is a slightly larger bold row just above it.

    The newest card shows how long ago it arrived ("Just now", "4 min ago")
    rather than a clock time; older rows keep their clock time.

    Outgoing confirmations ("Sent to Room 2, Doctor") are deliberately quiet
    -- small muted text behind a small arrow -- and are always inserted
    ABOVE the two newest incoming Readys (see _insert_outgoing), so a Ready
    alert stands out both visually and by sitting lowest in the box. Same
    ordering rule the app has used since outgoing lines were introduced.

    Entries: ("in", sender, ts) | ("out", targets, ts) | ("raw", text, "").
    """

    ARRIVE_SEC = 1.2

    def __init__(self, parent, theme: dict) -> None:
        super().__init__(parent, highlightthickness=0, bd=0, bg=theme["card_bg"],
                         height=_px(164))  # newest card + 3 rows
        self.th = theme
        self.entries: list[tuple[str, str, str]] = []
        self._arrive_t0 = -10.0
        self._sent_t0 = -10.0
        self._sent_entry: Optional[tuple] = None
        self._after_id: Optional[str] = None
        self._fonts: dict = {}
        # Wall-clock receive time of the newest incoming Ready, for its age.
        self._newest_recv: Optional[float] = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.after(20_000, self._age_tick)

    def _age_tick(self) -> None:
        """Redraw every 20s so the newest card's "N min ago" keeps counting."""
        if self._after_id is None and self._newest_recv is not None:
            self._draw()
        try:
            self.after(20_000, self._age_tick)
        except tk.TclError:
            pass

    @staticmethod
    def _ts_to_epoch(ts: str) -> Optional[float]:
        """"10:47 AM" -> today's epoch at that time (used when replaying the
        saved log, which only has clock times). None if unparseable."""
        try:
            t = datetime.strptime(ts.strip(), "%I:%M %p")
        except ValueError:
            return None
        now = datetime.now()
        when = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return min(when.timestamp(), time.time())

    def _age_text(self, ts: str) -> str:
        if self._newest_recv is None:
            return ts
        mins = int(max(0.0, time.time() - self._newest_recv) // 60)
        if mins < 1:
            return "Just now"
        if mins < 60:
            return f"{mins} min ago"
        hrs, mins = divmod(mins, 60)
        return f"{hrs} hr {mins} min ago" if mins else f"{hrs} hr ago"

    # ---- model ----
    def add_incoming(self, sender: str, ts: str, animate: bool = True) -> None:
        self.entries.append(("in", sender, ts))
        # Live arrivals use OUR receive time (the ts comes from the sender's
        # clock, which may be off); replayed ones only have the clock time.
        self._newest_recv = time.time() if animate else self._ts_to_epoch(ts)
        if animate:
            self._arrive_t0 = time.monotonic()
        self._kick()

    def add_outgoing(self, targets: str, ts: str, animate: bool = True) -> None:
        entry = ("out", targets, ts)
        self._insert_outgoing(entry)
        if animate:
            self._sent_entry, self._sent_t0 = entry, time.monotonic()
        self._kick()

    def add_raw(self, text: str) -> None:
        self._insert_outgoing(("raw", text, ""))
        self._kick()

    def _insert_outgoing(self, entry: tuple) -> None:
        incoming = [i for i, e in enumerate(self.entries) if e[0] == "in"]
        if len(incoming) >= 2:
            self.entries.insert(incoming[-2], entry)
        elif incoming:
            self.entries.insert(incoming[-1], entry)
        else:
            self.entries.append(entry)

    def trim(self, max_entries: int) -> None:
        if len(self.entries) > max_entries:
            del self.entries[:len(self.entries) - max_entries]
            self._draw()

    def clear(self) -> None:
        self.entries.clear()
        self._newest_recv = None
        self._draw()

    def set_theme(self, theme: dict) -> None:
        self.th = theme
        self.configure(bg=theme["card_bg"])
        self._draw()

    # ---- rendering ----
    def _font(self, size: int, weight: str = "normal") -> tkfont.Font:
        key = (size, weight)
        f = self._fonts.get(key)
        if f is None:
            f = tkfont.Font(root=self, family=_ui_family(), size=size, weight=weight)
            self._fonts[key] = f
        return f

    def _kick(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self._tick()

    def _tick(self) -> None:
        self._after_id = None
        self._draw()
        now = time.monotonic()
        if now - self._arrive_t0 < self.ARRIVE_SEC or now - self._sent_t0 < self.ARRIVE_SEC:
            try:
                self._after_id = self.after(16, self._tick)
            except tk.TclError:
                pass

    def _draw(self) -> None:
        try:
            self.delete("all")
            W, H = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        if W < 50:
            return
        th = self.th
        card = th["card_bg"]
        sub = th["sub"]
        border = th.get("card_border", "#e2e8f0")
        right = W - _px(14)
        if not self.entries:
            self.create_text(W / 2, H / 2, text="No Ready alerts yet today",
                             fill=_mix(sub, card, 0.25), font=self._font(11))
            return
        rows = list(self.entries)
        y = H - _px(4)
        if rows[-1][0] == "in":
            _, name, ts = rows.pop()
            k = min(1.0, (time.monotonic() - self._arrive_t0) / 0.9)
            ch = _px(62)
            slide = 1 - (1 - k) ** 3
            y1 = H - _px(4)
            y0 = y1 - ch + (1 - slide) * _px(18)
            bgc = _mix(_mix(th["accent"], card, 0.55), th["highlight"], k)
            _rrect(self, _px(2), y0, W - _px(2), y1, _px(14), fill=bgc, outline="")
            self.create_rectangle(_px(2), y0 + _px(12), _px(6), y1 - _px(12),
                                  fill=th["accent"], outline="")
            ax, ay, ar = _px(34), (y0 + y1) / 2, _px(19)
            if k < 1:
                pr = ar + _px(4) + _px(16) * k
                self.create_oval(ax - pr, ay - pr, ax + pr, ay + pr,
                                 outline=_mix(th["accent"], bgc, k), width=2.5 * _DPI_ZOOM)
            # Avatar follows the theme accent (not a per-station color).
            self.create_oval(ax - ar, ay - ar, ax + ar, ay + ar, fill=th["accent"], outline="")
            self.create_text(ax, ay, text=_initials(name), fill=th["accent_text"],
                             font=self._font(11, "bold"))
            pill_f = self._font(9, "bold")
            pill_w = pill_f.measure("NEW") + _px(22)
            ts_f = self._font(11, "bold")
            name_f = self._font(16, "bold")
            tx = ax + ar + _px(13)  # = _px(66); older rows align to this
            # Age and NEW are right-aligned to the log's edge.
            age = self._age_text(ts)
            gap = _px(10)
            shown = _elide(name_f, name, right - tx - max(pill_w, ts_f.measure(age)) - gap)
            self.create_text(tx, ay - _px(9), text=shown, anchor="w",
                             fill=th["emph_dark"], font=name_f)
            self.create_text(right, ay - _px(8), text=age, anchor="e", fill=sub, font=ts_f)
            self.create_text(tx, ay + _px(12), text="is ready for you", anchor="w",
                             fill=sub, font=self._font(10))
            px0 = right - pill_w
            _rrect(self, px0, ay + _px(3), px0 + pill_w, ay + _px(21), _px(9),
                   fill=th["accent"], outline="")
            self.create_text(px0 + pill_w / 2, ay + _px(12), text="NEW",
                             fill=th["accent_text"], font=pill_f)
            y = y0 - _px(6)
        # Older rows above, newest first going up. Their text lines up with
        # the newest card's name (tx); only sent rows get an icon (an arrow,
        # in the gutter left of tx).
        tx = _px(66)
        first_in = True
        for entry in reversed(rows):
            kind, text, ts = entry
            if kind == "in":
                big = first_in
                first_in = False
                rh = _px(32 if big else 28)
            else:
                rh = _px(26)
            if y - rh < 0:
                break
            cy = y - rh / 2
            ts_f = self._font(10 if kind == "in" else 9)
            ts_w = ts_f.measure(ts) if ts else 0
            gap = _px(10)
            if kind == "in":
                nf = self._font(12 if big else 11, "bold")
                rf = self._font(11)
                shown = _elide(nf, text, right - ts_w - gap - rf.measure(" ready") - tx)
                self.create_text(tx, cy, text=shown, anchor="w", fill=th["text"], font=nf)
                rx = tx + nf.measure(shown) + _px(6)
                self.create_text(rx, cy, text="ready", anchor="w", fill=sub, font=rf)
            else:
                if entry == self._sent_entry:
                    k = min(1.0, (time.monotonic() - self._sent_t0) / 1.0)
                    if k < 1:
                        _rrect(self, _px(14), y - rh + 2, W - _px(10), y - 2, _px(8),
                               fill=_mix(th["highlight"], card, k), outline="")
                if kind == "out":
                    # Drawn arrow (not a glyph) so it looks the same everywhere.
                    x1, x0, hk = tx - _px(8), tx - _px(24), _px(5)
                    self.create_line(x0, cy, x1, cy, fill=sub, width=1.8 * _DPI_ZOOM, capstyle="round")
                    self.create_line(x1 - hk, cy - hk, x1, cy, x1 - hk, cy + hk, fill=sub,
                                     width=1.8 * _DPI_ZOOM, capstyle="round", joinstyle="round")
                f = self._font(10)
                label = ("Sent to " + text) if kind == "out" else text
                shown = _elide(f, label, right - ts_w - gap - tx)
                self.create_text(tx, cy, text=shown, anchor="w", fill=sub, font=f)
            if ts:
                self.create_text(right, cy, text=ts, anchor="e", fill=sub, font=ts_f)
            self.create_line(tx, y - rh, right, y - rh, fill=border)
            y -= rh


class ReadyButton(tk.Canvas):
    """The big Ready button: two-tone gradient, soft colored glow, a gentle
    lift on hover, a press-in on click. After a successful send the app
    calls show_sent(), which turns it green with a ripple and a check for
    ~1.5s before it settles back to "Ready".

    Exposes _bg / _bg2 / _fg / _text / _r / _body_inset so the stick-figure
    animation's peek mask matches the visible button, not the glow margin.

    The button body fills the canvas height (minus the glow margins), so
    packing it with fill="y" makes it as tall as its panel."""

    def __init__(self, parent, theme: dict, text: str = "Ready", command=None) -> None:
        self._gm = (_px(12), _px(1), _px(12), _px(12))  # glow margins l, t, r, b
        super().__init__(parent, height=_px(62), width=_px(300),
                         highlightthickness=0, bd=0, bg=theme["card_bg"], cursor="hand2")
        self._text = text
        self._command = command
        self._r = _px(16)
        self._body_inset = self._gm
        self._enabled = True
        self._state = "idle"
        self._sent_label = ""
        self._sent_until = -10.0
        self._anim_t0 = -10.0
        self._after_id: Optional[str] = None
        self._f_label = tkfont.Font(root=self, family=_ui_family(), size=20, weight="bold")
        self._f_sent = tkfont.Font(root=self, family=_ui_family(), size=12, weight="bold")
        self.set_theme(theme)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_state("hover"))
        self.bind("<Leave>", lambda _e: self._set_state("idle"))
        self.bind("<ButtonPress-1>", lambda _e: self._set_state("press"))
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_theme(self, theme: dict) -> None:
        self.th = theme
        self.configure(bg=theme["card_bg"])
        self._apply_colors()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._apply_colors()

    def _apply_colors(self) -> None:
        if self._enabled:
            self._bg, self._bg2 = self.th["accent"], self.th.get("accent2", self.th["accent"])
        else:
            self._bg = self._bg2 = "#9ca3af"
        self._fg = self.th["accent_text"] if self._enabled else "#ffffff"
        self._draw()

    def show_sent(self, label: str) -> None:
        now = time.monotonic()
        self._sent_label = label
        self._sent_until = now + 1.5
        self._anim_t0 = now
        self._animate()

    def _set_state(self, s: str) -> None:
        if not self._enabled:
            s = "idle"
        if self._state != s:
            self._state = s
            self._draw()

    def _on_release(self, e: tk.Event) -> None:
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self._state = "hover" if inside else "idle"
        self._draw()
        if inside and self._command:
            self._command()

    def _animate(self) -> None:
        self._after_id = None
        self._draw()
        if time.monotonic() < self._sent_until + 0.35:
            try:
                self._after_id = self.after(16, self._animate)
            except tk.TclError:
                pass

    def _draw(self) -> None:
        try:
            self.delete("all")
            W, H = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        if W < 20 or H < 20:
            return
        th = self.th
        card = th["card_bg"]
        l, t, r, b = self._gm
        x0, y0, x1, y1 = l, t, W - r, max(t + _px(30), H - b)
        now = time.monotonic()
        sent = now < self._sent_until
        c1, c2 = self._bg, self._bg2
        if sent:
            c1, c2 = "#16a34a", "#0d9488"
        if self._state == "hover":
            c1, c2 = _mix(c1, "#ffffff", 0.08), _mix(c2, "#ffffff", 0.08)
            y0 -= 1
            y1 -= 1
        elif self._state == "press":
            c1, c2 = _mix(c1, "#000000", 0.12), _mix(c2, "#000000", 0.12)
            x0, y0, x1, y1 = x0 + 3, y0 + 2, x1 - 3, y1 - 1
        if self._enabled or sent:
            glow = 0.55 if self._state != "press" else 0.3
            for grow, a, dy in ((_px(10), 0.07, _px(8)), (_px(7), 0.11, _px(6)),
                                (_px(4), 0.16, _px(4)), (_px(2), 0.22, _px(2))):
                _rrect(self, x0 - grow + 4, y0 - grow + dy + 4, x1 + grow - 4, y1 + grow + dy - 2,
                       self._r + grow, fill=_mix(card, _mix(c1, c2, 0.5), a * glow * 2),
                       outline="")
        k = (now - self._anim_t0) / 0.6
        if 0 <= k <= 1:
            for i in range(2):
                kk = k - i * 0.25
                if 0 <= kk <= 1:
                    g = 3 + _px(12) * (1 - (1 - kk) ** 3)
                    _rrect(self, x0 - g, y0 - g, x1 + g, y1 + g, self._r + g,
                           fill="", outline=_mix(c1, card, kk), width=2 * _DPI_ZOOM)
        _gradient_rrect(self, x0, y0, x1, y1, self._r, c1, c2)
        self.create_line(x0 + self._r, y0 + 2, x1 - self._r, y0 + 2, fill=_mix(c1, "#ffffff", 0.35))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # "Ready" text scales with the button (which scales with the panel).
        want = int(max(16, min(30, min(x1 - x0, (y1 - y0) * 1.4) * 0.15 / _UI_SCALE)))
        if int(self._f_label.cget("size")) != want:
            self._f_label.configure(size=want)
        if sent:
            # Check above, "Sent to Room 2, Doctor" wrapped below it (the
            # button is tall and narrow).
            _draw_check(self, cx, cy - _px(20), _px(14), self._fg, width=_px(3))
            self.create_text(cx, cy + _px(4), text=self._sent_label, fill=self._fg, font=self._f_sent,
                             width=max(20, int(x1 - x0) - _px(16)), justify="center", anchor="n")
        else:
            self.create_text(cx, cy, text=self._text, fill=self._fg, font=self._f_label)


class StationChip(tk.Canvas):
    """A selectable station pill bound to a BooleanVar (one per online
    station in "Send to"). Selected = gradient fill with a check; unselected
    = soft outline with a green online dot."""

    def __init__(self, parent, text: str, var: tk.BooleanVar, theme: dict) -> None:
        self._f = tkfont.Font(root=parent, family=_ui_family(), size=9, weight="bold")
        super().__init__(parent, width=self._f.measure(text) + _px(40), height=StationChip.height(),
                         highlightthickness=0, bd=0, bg=theme["card_bg"], cursor="hand2")
        self.text, self.var, self.th, self._hover = text, var, theme, False
        self._trace = var.trace_add("write", lambda *_: self._draw())
        self.bind("<Button-1>", lambda _e: self.var.set(not self.var.get()))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Destroy>", self._on_destroy)
        self._draw()

    @staticmethod
    def height() -> int:
        return _px(26)

    def _on_destroy(self, e: tk.Event) -> None:
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._draw()

    def _draw(self) -> None:
        try:
            self.delete("all")
            sel = bool(self.var.get())
        except tk.TclError:
            return
        th = self.th
        w, h = int(self["width"]), int(self["height"])
        r = h / 2 - 2
        if sel:
            _gradient_rrect(self, 1, 2, w - 2, h - 3, r, th["accent"], th.get("accent2", th["accent"]))
            fg = th["accent_text"]
            _draw_check(self, _px(14), h / 2, _px(8), fg, width=2 * _DPI_ZOOM)
        else:
            _rrect(self, 1, 2, w - 2, h - 3, r, fill=th.get("chip_bg", th["input_bg"]),
                   outline=th["accent"] if self._hover else th.get("card_border", "#e2e8f0"),
                   width=1.2 * _DPI_ZOOM)
            fg = th["text"]
            d = _px(3.5)
            self.create_oval(_px(14) - d, h / 2 - d, _px(14) + d, h / 2 + d, fill=th["status"],
                             outline="")
        self.create_text(_px(25), h / 2, text=self.text, anchor="w", fill=fg, font=self._f)


class VolumeSlider(tk.Canvas):
    """Alert-volume slider bound to an IntVar (0-100): gradient fill up to
    the thumb, a round thumb, and the value as a percentage on the right."""

    def __init__(self, parent, var: tk.IntVar, theme: dict, on_release=None) -> None:
        super().__init__(parent, height=_px(30), width=_px(200), highlightthickness=0, bd=0,
                         bg=theme["card_bg"], cursor="hand2")
        self.var, self.th, self._on_release_cb = var, theme, on_release
        self._f = tkfont.Font(root=parent, family=_ui_family(), size=10, weight="bold")
        var.trace_add("write", lambda *_: self._draw())
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: self._on_release_cb and self._on_release_cb())

    def set_theme(self, theme: dict) -> None:
        self.th = theme
        self.configure(bg=theme["card_bg"])
        self._draw()

    def _track(self) -> tuple[float, float]:
        w = self.winfo_width() - self._f.measure("100%") - _px(12)
        return _px(10), max(_px(30), w - _px(10))

    def _drag(self, e: tk.Event) -> None:
        x0, x1 = self._track()
        self.var.set(int(max(0, min(100, round((e.x - x0) / max(1, x1 - x0) * 100)))))

    def _draw(self) -> None:
        try:
            self.delete("all")
            v = max(0, min(100, int(self.var.get())))
        except (tk.TclError, ValueError):
            return
        if self.winfo_width() < 40:
            return
        th = self.th
        x0, x1 = self._track()
        y = self.winfo_height() / 2
        xv = x0 + (x1 - x0) * v / 100
        tr = _px(3)
        _rrect(self, x0, y - tr, x1, y + tr, tr, fill=th["slider_track"], outline="")
        if xv > x0 + 2:
            _gradient_rrect(self, x0, y - tr, xv, y + tr, tr, th["accent"], th.get("accent2", th["accent"]))
        kr = _px(9)
        self.create_oval(xv - kr - 1, y - kr + 2, xv + kr + 1, y + kr + 3,
                         fill=_mix(th["card_bg"], "#000000", 0.12), outline="")
        self.create_oval(xv - kr, y - kr, xv + kr, y + kr, fill="#ffffff",
                         outline=_mix(th["accent"], "#ffffff", 0.3), width=2 * _DPI_ZOOM)
        self.create_text(self.winfo_width() - 2, y, text="%d%%" % v, anchor="e", fill=th["sub"],
                         font=self._f)


class IconButton(tk.Canvas):
    """Small round "play" button (alert-sound preview). The triangle is a
    polygon, not a glyph, so it looks identical on every platform."""

    def __init__(self, parent, theme: dict, command=None) -> None:
        s = _px(30)
        super().__init__(parent, width=s, height=s, highlightthickness=0, bd=0,
                         bg=theme["card_bg"], cursor="hand2")
        self.th, self._command, self._hover = theme, command, False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def set_theme(self, theme: dict) -> None:
        self.th = theme
        self.configure(bg=theme["card_bg"])
        self._draw()

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._draw()

    def _on_release(self, e: tk.Event) -> None:
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height() and self._command:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        th = self.th
        s = int(self["width"])
        if self._hover:
            _gradient_rrect(self, 1, 1, s - 1, s - 1, s / 2 - 1, th["accent"], th.get("accent2", th["accent"]))
            fg = th["accent_text"]
        else:
            self.create_oval(1, 1, s - 1, s - 1, fill=_mix(th["card_bg"], th["accent"], 0.12), outline="")
            fg = th["accent"]
        c, k = s / 2, s * 0.17
        self.create_polygon(c - k * 0.8, c - k, c - k * 0.8, c + k, c + k * 1.1, c,
                            fill=fg, outline=fg, joinstyle="round")


class AppLogo(tk.Canvas):
    """The app's speech-bubble + cross mark (Logo.svg) in the theme gradient."""

    def __init__(self, parent, theme: dict, size: int = 40) -> None:
        self._size = _px(size)
        super().__init__(parent, width=self._size, height=self._size, highlightthickness=0, bd=0,
                         bg=theme["bg"])
        self.set_theme(theme)

    def set_theme(self, theme: dict) -> None:
        self.configure(bg=theme["bg"])
        self.delete("all")
        s = self._size / 100.0
        a1, a2 = theme["accent"], theme.get("accent2", theme["accent"])
        _gradient_rrect(self, 10 * s, 15 * s, 90 * s, 70 * s, 15 * s, a1, a2)
        self.create_polygon(30 * s, 69 * s, 46 * s, 69 * s, 30 * s, 86 * s,
                            fill=_mix(a1, a2, 0.25), outline="")
        self.create_rectangle(45 * s, 30 * s, 55 * s, 55 * s, fill="#ffffff", outline="")
        self.create_rectangle(37.5 * s, 37.5 * s, 62.5 * s, 47.5 * s, fill="#ffffff", outline="")


class StatusPill(tk.Canvas):
    """Header pill: "3 stations online" with a softly pulsing green dot, or
    the LAN status ("Waiting for other workstations…") with a still amber /
    gray dot. The pulse only runs while the window is visible, so a window
    hidden to the tray costs nothing."""

    def __init__(self, parent, theme: dict) -> None:
        self._f = tkfont.Font(root=parent, family=_ui_family(), size=11, weight="bold")
        super().__init__(parent, width=_px(120), height=_px(30), highlightthickness=0, bd=0,
                         bg=theme["bg"])
        self.th = theme
        self._text = ""
        self._state = "offline"
        self._after_id: Optional[str] = None

    def set_theme(self, theme: dict) -> None:
        self.th = theme
        self.configure(bg=theme["bg"])
        self._draw()

    def set_status(self, text: str, state: str) -> None:
        if (text, state) == (self._text, self._state):
            return
        self._text, self._state = text, state
        self.configure(width=self._f.measure(text) + _px(44))
        self._draw()
        if state == "online" and self._after_id is None:
            self._pulse()

    def _pulse(self) -> None:
        self._after_id = None
        if self._state != "online":
            return
        try:
            visible = bool(self.winfo_viewable())
        except tk.TclError:
            return
        if visible:
            self._draw()
        try:
            self._after_id = self.after(50 if visible else 1000, self._pulse)
        except tk.TclError:
            pass

    def _draw(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        th = self.th
        w, h = int(self["width"]), int(self["height"])
        _rrect(self, 1, 1, w - 1, h - 1, h / 2 - 1, fill=th["card_bg"],
               outline=th.get("card_border", "#e2e8f0"))
        cx, cy = _px(20), h / 2
        if self._state == "online":
            dot = th["status"]
            p = (time.monotonic() % 1.8) / 1.8
            pr = _px(4) + _px(7) * p
            self.create_oval(cx - pr, cy - pr, cx + pr, cy + pr,
                             fill=_mix(dot, th["card_bg"], 0.55 + 0.45 * p), outline="")
        elif self._state == "waiting":
            dot = "#f59e0b"
        else:
            dot = "#9ca3af"
        d = _px(4)
        self.create_oval(cx - d, cy - d, cx + d, cy + d, fill=dot, outline="")
        self.create_text(_px(32), cy, text=self._text, anchor="w", fill=th["text"], font=self._f)


class RoundedButton(tk.Canvas):
    """Canvas-drawn button with smooth rounded corners, hover, and press states."""

    def __init__(self, parent, text: str = "", command=None,
                 bg: str = "#2563eb", fg: str = "#ffffff",
                 radius: int = 8, padx: int = 16, pady: int = 8,
                 font: tuple | None = None, width: int = 0,
                 cursor: str = "arrow", **kw) -> None:
        if font is None:
            font = _ui_font(10, "bold")
        self._fn = tkfont.Font(
            family=font[0], size=abs(int(font[1])),
            weight=font[2] if len(font) > 2 else "normal",
        )
        tw = self._fn.measure(text)
        th = self._fn.metrics("linespace")
        bw = width if width > 0 else tw + _dz(padx) * 2
        bh = th + _dz(pady) * 2
        try:
            outer = parent.cget("bg")
        except Exception:
            outer = "#f0f4ff"
        super().__init__(parent, width=bw, height=bh,
                         highlightthickness=0, bd=0,
                         bg=outer, cursor=cursor, **kw)
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._r = _dz(radius)
        self._hover = self._shade(bg, -22)
        self._press = self._shade(bg, -50)

        self._draw(bg)
        self.bind("<Enter>",           lambda e: self._draw(self._hover))
        self.bind("<Leave>",           lambda e: self._draw(self._bg))
        self.bind("<Button-1>",        lambda e: self._draw(self._press))
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_canvas_size(self, width: int, height: Optional[int] = None) -> None:
        """Resize the button canvas (e.g. Ready button ~80% of parent width)."""
        if height is None:
            height = int(self["height"])
        self.configure(width=width, height=height)
        self._draw(self._bg)

    @staticmethod
    def _shade(color: str, delta: int) -> str:
        try:
            r = max(0, min(255, int(color[1:3], 16) + delta))
            g = max(0, min(255, int(color[3:5], 16) + delta))
            b = max(0, min(255, int(color[5:7], 16) + delta))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return color

    def _draw(self, bg: str) -> None:
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        r = min(self._r, w // 2, h // 2)
        pts = [r, 0,   w-r, 0,   w, 0,   w, r,
               w, h-r, w,   h,   w-r, h, r, h,
               0, h,   0,   h-r, 0,   r, 0, 0]
        self.create_polygon(pts, smooth=True, fill=bg, outline="")
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=self._fg, font=self._fn)

    def _on_release(self, e: tk.Event) -> None:
        self._draw(self._bg)
        if 0 <= e.x <= int(self["width"]) and 0 <= e.y <= int(self["height"]):
            if self._command:
                self._command()

    def update_colors(self, bg: str, fg: str, outer_bg: str = "") -> None:
        self._bg = bg
        self._fg = fg
        self._hover = self._shade(bg, -22)
        self._press = self._shade(bg, -50)
        if outer_bg:
            self.configure(bg=outer_bg)
        self._draw(bg)


class ChairsideReadyAlertApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        _init_ui_family(root)
        _init_ui_scale(root)
        _apply_windows_theme_tweaks()
        self.root.title(APP_TITLE)
        # Fit the usable screen: Tk on Windows isn't DPI-aware, so at 125-150%
        # display scaling a fixed 760-tall window overflows a 1080p screen.
        _, avail_h = _usable_screen_size(self.root)
        self.root.geometry(f"{_dz(820)}x{max(_dz(480), min(_dz(620), avail_h))}")  # refined by _fit_window_to_content

        self.queue = queue.Queue()
        self.config_store = ConfigStore(_resolve_config_path())
        # Stamp first-launch date for the install-date trial (frozen builds only).
        # Must run before any UI code that consults trial state.
        self._stamp_first_launch_if_needed()
        # Whimsy: stick-figure animation trigger. State (count, target,
        # last-qualifying timestamp) persists across app restarts and
        # computer shutdown via the shared config file — closing the app to
        # bypass the 10-minute spacing rule won't work.
        # None = feature disabled (e.g., ready_animations module missing).
        self._anim_trigger = (
            _ready_animations.AnimationTrigger(load_from_storage=True)
            if _ready_animations is not None
            else None
        )
        self._anim_player: Optional[object] = None  # currently-playing animation
        # Apply the user's chosen character to the renderer once on startup
        # so subsequent triggers don't have to re-read config.
        if _ready_animations is not None:
            try:
                _ready_animations.set_character(_ready_animations.load_animation_character())
            except Exception:
                pass
        self.server = None
        self.discovery: Optional[LanDiscovery] = None
        self.peer_clients: dict[str, MessageClient] = {}
        self._network_running = False
        self._sync_after_id: Optional[str] = None
        # Monotonic timestamp of the previous _sync_peer_clients_tick. A gap far
        # larger than the 1.5s cadence means the machine slept; see the
        # wake-recovery block in that method. Seeded in start_network (NOT here)
        # so a long idle between app launch and Start can't read as a wake.
        self._last_tick_monotonic: Optional[float] = None
        # Monotonic timestamp of the last off-thread local-IP refresh; throttles
        # _maybe_refresh_local_ips to once per LOCAL_IP_REFRESH_SEC.
        self._last_ip_refresh_monotonic: Optional[float] = None
        # Cache of rendered alert-WAV bytes keyed by (sound_name, amplitude) so
        # an identical alert isn't re-synthesized in pure Python on every play.
        self._wav_cache: dict = {}
        self.local_ips = local_ipv4_addresses()
        self.online_labels: list[str] = []
        self._duplicate_name_detected = False
        self.target_vars = {}
        self._target_signature = None
        self.local_ip = detect_local_ip()
        self.auto_start_done = False
        self._diag_lines: deque[str] = deque(maxlen=500)
        # Capture otherwise-invisible Tk callback exceptions (after-callbacks,
        # bindings, commands). Tk's default handler writes to stderr, which does
        # not exist in a frozen Store build, so such failures vanished silently.
        # Installed now that self._diag_lines exists; it's only ever invoked
        # later, on the main thread, during mainloop.
        self.root.report_callback_exception = self._report_tk_callback_exception
        self._diag_win: Optional[tk.Toplevel] = None
        self._diag_text: Optional[tk.Text] = None
        # T+25s post-start sentinel that surfaces a help dialog when no LAN
        # peers have appeared — typically because Windows Firewall has the
        # app's Private-profile rules disabled. See _check_lan_health.
        self._lan_health_check_id: Optional[str] = None
        self._lan_health_dialog: Optional[tk.Toplevel] = None
        self._last_committed_label = ""
        self._default_menu: Optional[tk.Menu] = None
        self._default_menu_vars: dict[str, tk.BooleanVar] = {}
        self._manage_stations_window: Optional[tk.Toplevel] = None
        self._autostart_var = tk.BooleanVar(value=False)
        self._quitting = False
        self._main_hidden = False
        self._tray_icon = None
        self._mac_status_item = None
        self._mac_status_target = None
        self._mac_app_delegate = None
        self._mac_start_monotonic = time.monotonic()
        self._tray_start_attempts = 0
        self._tray_enabled = pystray is not None and Image is not None and ImageDraw is not None
        self._focus_server_sock: Optional[socket.socket] = None
        self._cards: list[RoundedCard] = []
        self._buttons: list[RoundedButton] = []
        _saved_theme = self.config_store.data.get("theme", DEFAULT_THEME)
        if _saved_theme not in THEMES:
            _saved_theme = DEFAULT_THEME
            self.config_store.data["theme"] = _saved_theme
            self.config_store.save()
        self._current_theme: dict = THEMES[_saved_theme]
        self._theme_var = tk.StringVar(value=_saved_theme)
        self.root.configure(bg=self._current_theme["bg"])

        self._build_styles()
        self.root.withdraw()
        self._build_menu()
        self._build_ui()
        self._load_config_into_form()
        self._fit_window_to_content()
        if not self._has_real_station_label():
            self.root.after(800, self._prompt_station_setup_if_needed)
        self._sync_autostart_state()
        # Restore today's Ready log (if any) and start the daily-rollover timer.
        # Must run after _build_ui creates self.log.
        self._load_ready_log()
        self.root.after(60_000, self._check_log_rollover)
        # Delay tray startup on macOS until event loop/UI is settled; early startup can be flaky.
        self.root.after(450, self._ensure_tray_icon_started)
        # Backup persistence: some Windows Tk builds are flaky on <<ComboboxSelected>> for readonly comboboxes.
        self.alert_sound_var.trace_add("write", lambda *_: self._persist_form())
        if sys.platform == "darwin":
            self._set_macos_app_icon()
        self.root.deiconify()
        if sys.platform == "darwin":
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(250, lambda: self.root.attributes("-topmost", False))
        self.root.after(120, self._process_ui_queue)
        self.root.after(260, self._auto_start_network)
        # Trial UI + license refresh (frozen builds only; helpers no-op otherwise).
        self.root.after(300, self._apply_trial_state_to_ui)
        self.root.after(800, self._kick_store_license_refresh)
        # Recurring re-check so a purchase made in the Store app -- or one that
        # propagates after launch -- unlocks without a restart.
        self.root.after(5_000, self._schedule_license_poll)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if sys.platform == "darwin":
            try:
                # Keep app alive when the last window is closed on macOS.
                self.root.tk.call("set", "::tk::mac::QuitOnLastWindowClosed", "0")
                _append_startup_log("Set tk::mac::QuitOnLastWindowClosed=0")
            except Exception as exc:
                _append_startup_log(f"Could not set QuitOnLastWindowClosed: {exc}")
            # Clicking the Dock icon after closing the window (withdraw) fires <<Reopen>> on Aqua Tk.
            self.root.bind("<<Reopen>>", self._on_macos_dock_reopen)
            # Ensure Regular app (menus + Dock) on launch; Accessory is only used while hidden-to-tray.
            self.root.after(100, lambda: self._macos_set_activation_policy_for_main_window(True))
            self._install_macos_app_delegate()
        self._start_focus_server()
        self.root.after(1500, self._maybe_prompt_windows_tray_visibility)

    def _create_tray_icon_image(self):
        custom = self._load_custom_logo_for_tray(size=64)
        if custom is not None:
            return custom
        if Image is None or ImageDraw is None:
            return None
        size = 64
        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        self._draw_brand_chat_icon(draw, size)
        return icon

    def _draw_brand_chat_icon(self, draw, size: int) -> None:
        accent = "#2A7FFF"
        fg = "#FFFFFF"
        # Match Logo.svg proportions so fallback icon stays on-brand.
        x = lambda v: int(round((v / 100.0) * size))
        y = lambda v: int(round((v / 100.0) * size))
        draw.rounded_rectangle((x(10), y(15), x(90), y(70)), radius=max(2, int(round(size * 0.15))), fill=accent)
        draw.polygon([(x(30), y(70)), (x(45), y(70)), (x(30), y(85))], fill=accent)
        draw.rectangle((x(45), y(30), x(55), y(55)), fill=fg)
        draw.rectangle((x(37.5), y(37.5), x(62.5), y(47.5)), fill=fg)

    def _load_custom_logo_for_tray(self, size: int = 64):
        if Image is None:
            return None
        base = _support_dir()
        candidates = [
            os.path.join(base, "logo.ico"),
            os.path.join(base, "Logo.ico"),
            os.path.join(base, "AppIcon.ico"),
            os.path.join(base, "logo.png"),
            os.path.join(base, "Logo.png"),
            os.path.join(base, "logo.svg"),
            os.path.join(base, "Logo.svg"),
        ]
        for path in candidates:
            if not os.path.isfile(path):
                continue
            lower = path.lower()
            try:
                if lower.endswith(".ico") or lower.endswith(".png"):
                    img = Image.open(path).convert("RGBA")
                elif lower.endswith(".svg") and cairosvg is not None:
                    png_bytes = cairosvg.svg2png(url=path, output_width=size, output_height=size)
                    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
                elif lower.endswith(".svg") and sys.platform == "darwin":
                    img = self._render_svg_for_tray_with_sips(path, size)
                    if img is None:
                        continue
                else:
                    continue
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                return img.resize((size, size), resample)
            except Exception as exc:
                self._append_diag(f"Custom tray logo load failed ({os.path.basename(path)}): {exc}")
        return None

    def _render_svg_for_tray_with_sips(self, svg_path: str, size: int):
        if Image is None:
            return None
        tmp_png = ""
        try:
            fd, tmp_png = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            command = ["sips", "-s", "format", "png", svg_path, "--out", tmp_png]
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0 or not os.path.isfile(tmp_png):
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                details = stderr or stdout or f"exit {completed.returncode}"
                self._append_diag(f"SVG conversion via sips failed ({os.path.basename(svg_path)}): {details}")
                return None
            return Image.open(tmp_png).convert("RGBA")
        except Exception as exc:
            self._append_diag(f"SVG conversion via sips error ({os.path.basename(svg_path)}): {exc}")
            return None
        finally:
            if tmp_png:
                try:
                    os.remove(tmp_png)
                except OSError:
                    pass

    def _start_tray_icon(self) -> None:
        self._record_tray_diag("Tray start requested.")
        _append_startup_log("tray start requested")
        if sys.platform == "darwin":
            if self._start_macos_status_item():
                self._record_tray_diag("Native macOS menu bar icon started.")
                _append_startup_log("native macOS menu bar icon started")
                return
            self._append_diag("Native macOS menu bar icon unavailable; falling back to pystray.")
            self._record_tray_diag("Native menu bar icon unavailable; trying pystray fallback.")
            _append_startup_log("native macOS menu bar icon unavailable; fallback to pystray")
        if not self._tray_enabled or pystray is None:
            detail = "pystray/Pillow imports unavailable."
            if _TRAY_IMPORT_ERROR:
                first_line = _TRAY_IMPORT_ERROR.strip().splitlines()[-1]
                detail = f"{detail} {first_line}"
            self._append_diag(f"Tray icon unavailable ({detail})")
            self._record_tray_diag(f"Tray unavailable: {detail}")
            _append_startup_log(f"tray unavailable: {detail}")
            if sys.platform == "darwin":
                self.root.after(
                    300,
                    lambda d=detail: messagebox.showwarning(
                        APP_TITLE,
                        "Menu bar icon could not start because tray dependencies are missing.\n\n"
                        f"Details: {d}\n\n"
                        "Run the macOS installer again to reinstall dependencies, then relaunch the app.",
                    ),
                )
            return
        if self._tray_icon is not None:
            return
        def _show_tray_icon(icon) -> None:
            try:
                icon.visible = True
            except Exception:
                pass
        # Microsoft Store policy 10.8.1 prohibits in-app self-update — hide the
        # menu entry on frozen Store builds. Updates ship via the Store channel.
        items = [
            pystray.MenuItem("Send Ready", self._tray_send_ready),
            # Default menu action maps to tray icon double-click on platforms that support it.
            pystray.MenuItem("Show Main Window", self._tray_show_main, default=True),
            pystray.MenuItem("Hide Main Window", self._tray_hide_main),
        ]
        if not IS_FROZEN:
            items.append(pystray.MenuItem("Check for Updates", self._tray_check_updates))
        items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Close Chairside Ready Alert", self._tray_quit),
        ])
        menu = pystray.Menu(*items)
        self._tray_icon = pystray.Icon(
            "chairside_ready",
            self._create_tray_icon_image(),
            APP_TITLE,
            menu,
        )
        try:
            if sys.platform == "darwin":
                # On macOS, pystray is most reliable when attached to the shared NSApplication.
                # Fallback through detached/threaded variants for broader environment compatibility.
                try:
                    from AppKit import NSApplication  # type: ignore

                    ns_app = NSApplication.sharedApplication()
                    self._tray_icon.run_detached(setup=_show_tray_icon, darwin_nsapplication=ns_app)
                except Exception as exc_nsapp:
                    self._append_diag(
                        f"Tray icon NSApplication mode failed on macOS; retrying fallback modes: {exc_nsapp}"
                    )
                    try:
                        self._tray_icon.run_detached(setup=_show_tray_icon)
                    except Exception as exc_detached:
                        self._append_diag(
                            f"Tray icon detached mode failed on macOS; retrying threaded mode: {exc_detached}"
                        )
                        threading.Thread(target=lambda: self._tray_icon.run(setup=_show_tray_icon), daemon=True).start()
            else:
                threading.Thread(target=lambda: self._tray_icon.run(setup=_show_tray_icon), daemon=True).start()
        except Exception as exc:
            self._append_diag(f"Tray icon failed to start: {exc}")
            self._record_tray_diag(f"Tray failed to start: {exc}")
            _append_startup_log(f"tray icon failed to start: {exc}")
            self._tray_icon = None
            if sys.platform == "darwin":
                self.root.after(
                    300,
                    lambda e=str(exc): messagebox.showwarning(
                        APP_TITLE,
                        "Could not start the macOS menu bar icon.\n\n"
                        f"Details: {e}\n\n"
                        "Open Settings > Connection log... for diagnostics.",
                    ),
                )

    def _ensure_tray_icon_started(self) -> None:
        if self._quitting:
            return
        if self._mac_status_item is not None or self._tray_icon is not None:
            return
        self._tray_start_attempts += 1
        self._record_tray_diag(f"Tray startup attempt {self._tray_start_attempts}.")
        _append_startup_log(f"tray startup attempt {self._tray_start_attempts}")
        self._start_tray_icon()
        if (self._mac_status_item is None and self._tray_icon is None and self._tray_start_attempts < 6):
            self.root.after(900, self._ensure_tray_icon_started)

    def _start_macos_status_item(self) -> bool:
        if sys.platform != "darwin":
            return False
        if self._mac_status_item is not None:
            return True
        try:
            import objc  # type: ignore
            from Foundation import NSObject  # type: ignore
            from AppKit import NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength  # type: ignore

            # Log activation policy so we can see whether the process is registered
            # as a GUI app (0=Regular, 1=Accessory, 2=Prohibited).  Status items
            # silently fail when policy is Prohibited.
            try:
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore
                _ns = NSApplication.sharedApplication()
                _ns.finishLaunching()
                _pol = _ns.activationPolicy()
                self._record_tray_diag(f"NSApp policy at status-item creation: {_pol}  (0=Regular 1=Accessory 2=Prohibited)")
                if _pol == 2:  # Prohibited → force Accessory so item can render
                    _r = _ns.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
                    self._record_tray_diag(f"Forced Accessory policy (was Prohibited): {_r}")
            except Exception as _pe:
                self._record_tray_diag(f"NSApp policy check error: {_pe}")

            app = self

            class _TrayTarget(NSObject):
                def initWithApp_(self, app_ref):
                    self = objc.super(_TrayTarget, self).init()
                    if self is None:
                        return None
                    self._app = app_ref
                    return self

                def sendReady_(self, _sender):
                    self._app.queue.put(("tray_action", "send_ready"))

                def showMain_(self, _sender):
                    self._app.queue.put(("tray_action", "show_main"))

                def hideMain_(self, _sender):
                    self._app.queue.put(("tray_action", "hide_main"))

                def checkUpdates_(self, _sender):
                    self._app.queue.put(("tray_action", "check_updates"))

                def quitApp_(self, _sender):
                    self._app.queue.put(("tray_action", "quit"))

            target = _TrayTarget.alloc().initWithApp_(app)
            # NSSquareStatusItemLength uses a proper square icon slot — the modern
            # macOS approach.  NSVariableStatusItemLength + text silently fails to
            # render on macOS 16 even though all API calls succeed.
            from AppKit import NSImage, NSSquareStatusItemLength  # type: ignore
            from Foundation import NSData  # type: ignore
            status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSSquareStatusItemLength)
            button = status_item.button()
            if button is not None:
                # Build a template NSImage from the PIL icon so the button has content.
                _img_set = False
                try:
                    pil_img = self._create_tray_icon_image()
                    if pil_img is not None:
                        _buf = io.BytesIO()
                        pil_img.save(_buf, format="PNG")
                        _data = NSData.dataWithBytes_length_(_buf.getvalue(), len(_buf.getvalue()))
                        _ns_img = NSImage.alloc().initWithData_(_data)
                        if _ns_img is not None:
                            _ns_img.setTemplate_(True)
                            button.setImage_(_ns_img)
                            _img_set = True
                            self._record_tray_diag("Native status item button: image set (template).")
                except Exception as exc_img:
                    self._record_tray_diag(f"Image set failed ({exc_img}); falling back to text.")
                if not _img_set:
                    button.setTitle_("R")
                    self._record_tray_diag("Native status item button: title='R' (text fallback).")
                button.setToolTip_(APP_TITLE)
            else:
                # button() returned None — deprecated fallback.
                self._record_tray_diag("Native status item button() returned None; trying setTitle_ fallback.")
                try:
                    status_item.setTitle_("R")
                    status_item.setLength_(34.0)
                    self._record_tray_diag("setTitle_ fallback applied.")
                except Exception as exc_t:
                    self._record_tray_diag(f"setTitle_ fallback also failed: {exc_t}")

            menu = NSMenu.alloc().init()

            item_send = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Send Ready", "sendReady:", "")
            item_send.setTarget_(target)
            menu.addItem_(item_send)

            item_show = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show Main Window", "showMain:", "")
            item_show.setTarget_(target)
            menu.addItem_(item_show)

            item_hide = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide Main Window", "hideMain:", "")
            item_hide.setTarget_(target)
            menu.addItem_(item_hide)

            if not IS_FROZEN:
                item_updates = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Check for Updates", "checkUpdates:", "")
                item_updates.setTarget_(target)
                menu.addItem_(item_updates)

            menu.addItem_(NSMenuItem.separatorItem())

            item_quit = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Close Chairside Ready Alert", "quitApp:", "")
            item_quit.setTarget_(target)
            menu.addItem_(item_quit)

            status_item.setMenu_(menu)
            self._mac_status_item = status_item
            self._mac_status_target = target
            self._record_tray_diag("Native status item configured (visible + menu attached).")
            self._record_tray_diag("Native status item attached to system status bar.")
            return True
        except Exception as exc:
            self._append_diag(f"Native macOS menu bar icon failed: {exc}")
            self._record_tray_diag(f"Native macOS menu bar icon failed: {exc}")
            _append_startup_log(f"native macOS menu bar icon failed: {exc}")
            return False

    def _stop_tray_icon(self) -> None:
        if sys.platform == "darwin" and self._mac_status_item is not None:
            try:
                from AppKit import NSStatusBar  # type: ignore

                NSStatusBar.systemStatusBar().removeStatusItem_(self._mac_status_item)
            except Exception:
                pass
            self._mac_status_item = None
            self._mac_status_target = None
        icon = self._tray_icon
        self._tray_icon = None
        if not icon:
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _tray_send_ready(self, _icon, _item) -> None:
        self.queue.put(("tray_action", "send_ready"))

    def _tray_show_main(self, _icon, _item) -> None:
        self.queue.put(("tray_action", "show_main"))

    def _tray_hide_main(self, _icon, _item) -> None:
        self.queue.put(("tray_action", "hide_main"))

    def _tray_check_updates(self, _icon, _item) -> None:
        self.queue.put(("tray_action", "check_updates"))

    def _tray_quit(self, _icon, _item) -> None:
        self.queue.put(("tray_action", "quit"))

    def _start_focus_server(self) -> None:
        if self._focus_server_sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((FOCUS_IPC_HOST, FOCUS_IPC_PORT))
            sock.listen(4)
            sock.settimeout(1.0)
            self._focus_server_sock = sock
            threading.Thread(target=self._focus_server_worker, daemon=True).start()
        except OSError as exc:
            self._append_diag(f"Focus server unavailable: {exc}")

    def _focus_server_worker(self) -> None:
        while not self._quitting:
            sock = self._focus_server_sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                # Normal: the 1.0s accept timeout lets us re-check _quitting.
                continue
            except OSError:
                # A REAL socket error (the listener died, not just an idle
                # timeout). socket.timeout is an OSError subclass, so it must be
                # caught first above; reaching here means accept() would keep
                # failing instantly. Stop the thread rather than spinning at
                # 100% CPU. Focus-from-second-instance is non-critical, and the
                # next launch's stale-lock path already tolerates no server.
                return
            try:
                with conn:
                    data = conn.recv(128).decode("utf-8", errors="ignore").strip()
                    if data == FOCUS_IPC_TOKEN:
                        self.queue.put(("focus_request", None))
            except OSError:
                continue

    def _stop_focus_server(self) -> None:
        sock = self._focus_server_sock
        self._focus_server_sock = None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def _open_windows_tray_settings(self) -> bool:
        if not sys.platform.startswith("win"):
            return False
        try:
            os.startfile("ms-settings:taskbar")  # type: ignore[attr-defined]
            return True
        except Exception:
            try:
                subprocess.Popen(["explorer.exe", "ms-settings:taskbar"])
                return True
            except Exception:
                return False

    def _show_windows_tray_help(self, one_time: bool = False) -> None:
        if not sys.platform.startswith("win"):
            messagebox.showinfo(APP_TITLE, "Tray icon visibility setup is only needed on Windows.")
            return
        msg = (
            "Windows may hide tray icons by default.\n\n"
            "To keep Chairside visible:\n"
            "1) Open Settings > Personalization > Taskbar.\n"
            "2) Open Notification area / Other system tray icons.\n"
            "   (Windows 10 wording: Select which icons appear on the taskbar.)\n"
            "3) Find Chairside Ready Alert (R) and switch it On.\n\n"
            "Tip: You can also drag the icon from ^ onto the taskbar tray."
        )
        open_settings = messagebox.askyesno(APP_TITLE, f"{msg}\n\nOpen Taskbar settings now?")
        if open_settings:
            ok = self._open_windows_tray_settings()
            if not ok:
                messagebox.showwarning(APP_TITLE, "Could not open Windows Settings automatically.")
        if one_time:
            self.config_store.data["tray_visibility_help_shown"] = True
            self.config_store.save()

    def _maybe_prompt_windows_tray_visibility(self) -> None:
        if not sys.platform.startswith("win"):
            return
        if self.config_store.data.get("tray_visibility_help_shown", False):
            return
        self._show_windows_tray_help(one_time=True)

    def _dispatch_tray_action(self, action: str) -> None:
        try:
            self._record_tray_diag(f"Tray action requested: {action}")
            if action == "send_ready":
                self._send_ready_from_widget()
            elif action == "show_main":
                self._show_main_window()
            elif action == "hide_main":
                self._hide_main_window()
            elif action == "check_updates":
                self._check_for_updates_clicked()
            elif action == "quit":
                self._close_from_widget()
        except Exception as exc:
            self._append_diag(f"Tray action failed ({action}): {exc}")
            self._record_tray_diag(f"Tray action failed ({action}): {exc}")

    def _show_main_window(self) -> None:
        self._main_hidden = False
        if sys.platform == "darwin":
            self._macos_set_activation_policy_for_main_window(True)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.root.attributes("-topmost", False))

    def _on_macos_dock_reopen(self, _event: object = None) -> None:
        if self._quitting:
            return
        self._show_main_window()

    def _handle_system_wake(self) -> None:
        """Triggered by an NSWorkspaceDidWakeNotification observer on macOS, and
        on Windows by the monotonic tick-gap detector in _sync_peer_clients_tick
        (root.after timers stall while asleep, so a large gap means we woke).
        macOS sleep-then-wake often leaves the MessageServer's TCP listening
        socket in a state where the next accept() raises OSError, and inbound
        _handle_client recv loops still hold dead sockets that will never see
        another byte. Bounce stop_network/start_network to rebuild a fresh
        listener and shake loose any stale inbound connections. _run_server
        also recovers on its own via its outer re-bind loop, but this catches
        the case proactively so the user does not have to wait for the next
        failed accept(). No-op when the network isn't running (the user
        manually stopped it) — we don't want to silently re-enable it."""
        if self._quitting:
            return
        if not self._network_running:
            return
        self._append_diag("System wake detected — bouncing the LAN to recover from any sleep-broken sockets.")
        try:
            self.stop_network()
        except Exception as exc:
            self._append_diag(f"stop_network() during wake bounce raised: {exc}")
        # Brief delay lets the OS release the listener's port before we
        # try to re-bind it; 500 ms is plenty in practice.
        self.root.after(500, self.start_network)

    def _install_macos_app_delegate(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            import objc  # type: ignore
            from Foundation import NSObject  # type: ignore
            from AppKit import NSApplication, NSWorkspace  # type: ignore

            app_ref = self

            class _AppDelegate(NSObject):
                def initWithApp_(self, app_obj):
                    self = objc.super(_AppDelegate, self).init()
                    if self is None:
                        return None
                    self._app = app_obj
                    return self

                # Called when Dock icon is clicked while app is running.
                def applicationShouldHandleReopen_hasVisibleWindows_(self, _sender, _flag):
                    try:
                        # Queue to Tk thread; avoid touching Tk from Cocoa callback context.
                        self._app.queue.put(("focus_request", None))
                    except Exception:
                        pass
                    return True

                # NSWorkspaceDidWakeNotification — system woke from sleep.
                # macOS TCP listening sockets often return OSError on the
                # first accept() after wake, with no in-kernel recovery,
                # which silently kills incoming Ready alerts. Bounce the
                # network so MessageServer rebinds and any half-dead inbound
                # _handle_client threads are cleared. _run_server's outer
                # re-bind loop is the safety net; this is the proactive path.
                def systemDidWake_(self, _notification):
                    try:
                        self._app.queue.put(("system_wake", None))
                    except Exception:
                        pass

            delegate = _AppDelegate.alloc().initWithApp_(app_ref)
            NSApplication.sharedApplication().setDelegate_(delegate)
            self._mac_app_delegate = delegate  # prevent GC

            # Subscribe to system wake notifications. The NSWorkspace
            # notification center is distinct from NSNotificationCenter —
            # workspace events (sleep/wake/screen lock/user switch) are
            # only posted on NSWorkspace.sharedWorkspace().notificationCenter().
            try:
                ws_nc = NSWorkspace.sharedWorkspace().notificationCenter()
                ws_nc.addObserver_selector_name_object_(
                    delegate,
                    "systemDidWake:",
                    "NSWorkspaceDidWakeNotification",
                    None,
                )
                self._record_tray_diag("Installed macOS app delegate for Dock reopen + system wake.")
                _append_startup_log("Installed macOS app delegate for Dock reopen + system wake.")
            except Exception as wake_exc:
                # Dock reopen still works even if the wake observer failed
                # to register; log and fall through.
                self._record_tray_diag(f"Installed macOS Dock delegate; wake observer failed: {wake_exc}")
                _append_startup_log(f"Installed macOS Dock delegate; wake observer failed: {wake_exc}")
        except Exception as exc:
            self._append_diag(f"Could not install macOS Dock reopen delegate: {exc}")
            _append_startup_log(f"Could not install macOS Dock reopen delegate: {exc}")

    def _set_macos_app_icon(self) -> None:
        """Set the Dock icon from Logo.svg, overriding Python.app's rocket ship."""
        if sys.platform != "darwin":
            return
        try:
            from AppKit import NSApplication, NSImage  # type: ignore
            from Foundation import NSData  # type: ignore
            base = _support_dir()
            img = None
            for name in ("Logo.svg", "logo.svg"):
                path = os.path.join(base, name)
                if not os.path.isfile(path):
                    continue
                if cairosvg is not None:
                    try:
                        png_bytes = cairosvg.svg2png(url=path, output_width=512, output_height=512)
                        data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
                        img = NSImage.alloc().initWithData_(data)
                    except Exception:
                        pass
                if img is None:
                    try:
                        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                        tmp.close()
                        subprocess.run(
                            ["sips", "-s", "format", "png", path, "--out", tmp.name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                        )
                        img = NSImage.alloc().initWithContentsOfFile_(tmp.name)
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                if img is not None:
                    break
            if img is None:
                for name in ("Logo.png", "logo.png"):
                    path = os.path.join(base, name)
                    if os.path.isfile(path):
                        img = NSImage.alloc().initWithContentsOfFile_(path)
                        if img is not None:
                            break
            if img is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(img)
        except Exception:
            pass

    def _macos_set_activation_policy_for_main_window(self, visible: bool) -> None:
        """Dock + application menu bar need Regular; hide Dock tile when window is withdrawn to tray (Accessory)."""
        if sys.platform != "darwin" or self._quitting:
            return
        try:
            from AppKit import (
                NSApplication,
                NSApplicationActivationPolicyAccessory,
                NSApplicationActivationPolicyRegular,
            )
        except ImportError:
            return
        try:
            app = NSApplication.sharedApplication()
            policy = NSApplicationActivationPolicyRegular if visible else NSApplicationActivationPolicyAccessory
            if not app.setActivationPolicy_(policy):
                return
            if visible:
                try:
                    app.activateIgnoringOtherApps_(True)
                except Exception:
                    pass
        except Exception:
            pass

    def _hide_main_window(self) -> None:
        try:
            self._persist_form()
        except Exception:
            pass
        self._main_hidden = True
        self.root.withdraw()
        if sys.platform == "darwin":
            self._macos_set_activation_policy_for_main_window(False)

    def _close_from_widget(self) -> None:
        self._quitting = True
        self.on_close()

    def _send_ready_from_widget(self) -> None:
        # Explicit widget handler so left-click on READY always routes to the same send flow.
        self.send_ready_selected()

    def _build_styles(self) -> None:
        t = THEMES[DEFAULT_THEME]
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame",        background=t["bg"])
        style.configure("Card.TFrame",   background=t["card_bg"])
        style.configure("TLabel",        background=t["bg"],      foreground=t["text"], font=_ttk_font(10))
        style.configure("Card.TLabel",   background=t["card_bg"], foreground=t["text"], font=_ttk_font(10))
        # App title styling uses the active theme title color.
        style.configure("Title.TLabel",  background=t["bg"],      foreground=t["title"], font=_ttk_font(13, "bold"))
        style.configure("CardTitle.TLabel", background=t["card_bg"], foreground=t["text"], font=_ttk_font(10, "bold"))
        style.configure("Subtitle.TLabel",  background=t["bg"],   foreground=t["sub"],  font=_ttk_font(10))
        style.configure("TButton",       font=_ttk_font(10, "bold"), padding=_dz(8))
        style.configure("Primary.TButton",  background=t["accent"], foreground=t["accent_text"])
        style.map("Primary.TButton",        background=[("active", t["accent"]), ("pressed", t["accent"])])
        style.configure("Ready.TButton",    background=t["accent"], foreground=t["accent_text"],
                        font=_ttk_font(15, "bold"), padding=(_dz(60), _dz(18)))
        style.map("Ready.TButton",          background=[("active", t["accent"]), ("pressed", t["accent"])])
        style.configure("Preview.TButton",  background=t["accent"], foreground=t["accent_text"],
                        font=_ttk_font(10), padding=(_dz(4), _dz(1)))
        style.map("Preview.TButton",        background=[("active", t["accent"])])

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        settings_menu = tk.Menu(menu, tearoff=0)
        settings_menu.add_command(label="This Station's Name...", command=self._open_station_name_dialog)
        settings_menu.add_separator()
        if IS_FROZEN and sys.platform.startswith("win"):
            # Store builds use the AppxManifest startupTask extension; the
            # real on/off switch lives in Settings -> Apps -> Startup. A
            # checkbutton here would lie because the AppX runtime owns the
            # state, not our config.
            settings_menu.add_command(
                label="Manage startup apps (Windows Settings)…",
                command=self._open_windows_startup_settings,
            )
        else:
            settings_menu.add_checkbutton(
                label="Start automatically at login",
                variable=self._autostart_var,
                command=self._toggle_autostart_from_menu,
            )
        settings_menu.add_separator()
        settings_menu.add_command(label="Network Settings...", command=self._open_network_settings_window)
        if not IS_FROZEN:
            settings_menu.add_command(label="Check for updates...", command=self._check_for_updates_clicked)
        menu.add_cascade(label="Settings", menu=settings_menu)
        self._default_menu = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="Default", menu=self._default_menu)
        layout_menu = tk.Menu(menu, tearoff=0)
        for theme_name in THEMES:
            layout_menu.add_radiobutton(
                label=theme_name, variable=self._theme_var, value=theme_name,
                command=lambda n=theme_name: self._apply_theme(n),
            )
        menu.add_cascade(label="Layout", menu=layout_menu)
        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="FAQ & Troubleshooting (web)...", command=self._open_faq_page)
        help_menu.add_command(label="Connection log...", command=self._open_connection_log_window)
        help_menu.add_command(label="Windows Tray Icon Setup...", command=self._show_windows_tray_help)
        help_menu.add_separator()
        help_menu.add_command(label="Stop Network", command=self.stop_network)
        help_menu.add_command(label="Start Network", command=self.start_network)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about_dialog)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def _open_faq_page(self) -> None:
        """Open the public FAQ & Troubleshooting page in the default browser.
        Wired to the Help menu and offered from the About dialog."""
        try:
            webbrowser.open(FAQ_URL)
        except Exception as exc:
            self._append_diag(f"Could not open FAQ page: {exc}")
            messagebox.showinfo(
                APP_TITLE,
                f"Couldn't open your browser automatically. The FAQ is at:\n\n{FAQ_URL}",
            )

    def _show_about_dialog(self) -> None:
        # askyesno (not showinfo) so the dialog can offer a one-click path to
        # the FAQ, matching the _show_windows_tray_help "...open settings now?"
        # idiom. The URL is shown in full too, so it's copyable even if the
        # browser can't be launched.
        open_faq = messagebox.askyesno(
            f"About {APP_TITLE}",
            f"{APP_TITLE}\n"
            f"Version {APP_VERSION}\n\n"
            f"For support questions, contact:\n"
            f"support@fieldcrestdental.com\n"
            f"(Please allow 24-48 hours for a response.)\n\n"
            f"FAQ & Troubleshooting:\n"
            f"{FAQ_URL}\n\n"
            f"Open the FAQ in your browser now?",
        )
        if open_faq:
            self._open_faq_page()

    # ---- Trial / paid-license gating (frozen Store builds only) ----------
    # Dev runs are always treated as licensed: every helper below short-circuits
    # to "paid, no enforcement" when IS_FROZEN is False. See the TRIAL_DAYS /
    # _query_store_license_paid block near the top of the file for the policy.

    def _stamp_first_launch_if_needed(self) -> None:
        if not IS_FROZEN:
            return
        if str(self.config_store.data.get("first_launch_iso", "")).strip():
            return
        self.config_store.data["first_launch_iso"] = _today_iso()
        try:
            self.config_store.save()
        except Exception:
            pass

    def _trial_status(self) -> dict:
        """Snapshot of trial state used by both UI and gating checks.
        Keys: enforce (bool), paid (bool), remaining_days (int|None), expired (bool)."""
        if not IS_FROZEN:
            return {"enforce": False, "paid": True, "remaining_days": None, "expired": False}
        paid = bool(self.config_store.data.get("paid_unlocked", False))
        remaining = _trial_days_remaining(
            str(self.config_store.data.get("first_launch_iso", ""))
        )
        # remaining=None means we haven't stamped a first-launch yet — treat as
        # "trial just starting" (TRIAL_DAYS left) so we don't accidentally lock
        # out a user before stamping completes.
        if remaining is None:
            remaining = TRIAL_DAYS
        expired = (remaining <= 0) and not paid
        return {"enforce": True, "paid": paid, "remaining_days": remaining, "expired": expired}

    def _is_locked_out(self) -> bool:
        st = self._trial_status()
        return st["enforce"] and st["expired"] and not st["paid"]

    def _kick_store_license_refresh(self, user_initiated: bool = False) -> None:
        """Background-thread Store license check. Posts ('license', (result,
        user_initiated)) onto the UI queue, where result is True/False/None.
        A background poll with an unknown (None) result simply changes nothing;
        a user-initiated refresh always reports back so the 'I purchased'
        button can show real feedback instead of looking dead."""
        if not IS_FROZEN:
            return

        def _worker():
            result = _query_store_license_paid()
            try:
                self.queue.put(("license", (result, bool(user_initiated))))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_license_refresh_result(self, paid, user_initiated: bool = False) -> None:
        """UI-thread handler for the Store license check. `paid` is the tri-state
        from _query_store_license_paid: True (owned), False (active-but-trial or
        inactive), or None (couldn't reach the Store).

        paid_unlocked is a ONE-WAY LATCH: once the Store confirms a purchase we
        never demote it on a later False or None. A just-converted or flaky read
        that momentarily reports 'trial' must not re-lock a paying customer."""
        # Diagnostic visibility so a real-machine smoke test can tell a Store
        # 'unknown' (None) apart from a definite 'not paid' (False) vs 'paid'.
        # Background polls log only on change to avoid one line per minute;
        # a user-initiated refresh always logs.
        result_word = (
            "paid" if paid is True
            else "not paid (Store trial/inactive)" if paid is False
            else "unknown (Store unreachable or winsdk error)"
        )
        if user_initiated or result_word != getattr(self, "_last_license_log", None):
            self._last_license_log = result_word
            self._append_diag(f"Store license check: {result_word}.")
        prev = bool(self.config_store.data.get("paid_unlocked", False))
        new = prev or (paid is True)
        if new != prev:
            self.config_store.data["paid_unlocked"] = new
            try:
                self.config_store.save()
            except Exception:
                pass
            self._apply_trial_state_to_ui()
            # A paid license just appeared; if the network was held back, start it.
            if new and not self._network_running:
                try:
                    self._auto_start_network()
                except Exception:
                    pass
        if new:
            # Unlocked for good -- stop the recurring unpaid re-check.
            self._cancel_license_poll()
        if user_initiated:
            self._show_license_refresh_feedback(unlocked=new, raw=paid)

    def _show_license_refresh_feedback(self, unlocked: bool, raw) -> None:
        """Restore the manual refresh button and tell the user exactly what the
        Store said, so the button is never a silent no-op."""
        btn = getattr(self, "_trial_banner_refresh_btn", None)
        if btn is not None:
            try:
                btn.configure(text=REFRESH_BTN_LABEL, state="normal")
            except Exception:
                pass
        try:
            if unlocked:
                messagebox.showinfo(
                    f"{APP_TITLE} — Unlocked",
                    "Purchase confirmed. Thank you! Chairside Ready Alert is now "
                    "unlocked on this PC.",
                )
            elif raw is None:
                messagebox.showwarning(
                    f"{APP_TITLE} — Store unavailable",
                    "Couldn't reach the Microsoft Store to verify your purchase "
                    "just now.\n\n"
                    "Check your internet connection and try again in a moment. "
                    "If it keeps failing, fully quit and reopen the app.",
                )
            else:
                messagebox.showwarning(
                    f"{APP_TITLE} — No purchase found yet",
                    "The Microsoft Store doesn't yet show a completed purchase for "
                    "the account signed in on this PC.\n\n"
                    "• If you just bought it, wait a minute and click "
                    "\"I purchased — refresh\" again — purchases can take a short "
                    "while to register.\n"
                    "• Make sure the Microsoft Store is signed in with the SAME "
                    "account you purchased with.\n"
                    "• You can also open Microsoft Store → Library → Get updates, "
                    "then reopen this app.",
                )
        except Exception:
            pass

    def _on_refresh_button_click(self) -> None:
        """Manual 'I purchased — refresh' handler. Shows an immediate 'Checking…'
        state, then surfaces the Store's answer via _show_license_refresh_feedback."""
        btn = getattr(self, "_trial_banner_refresh_btn", None)
        if btn is not None:
            try:
                btn.configure(text="Checking…", state="disabled")
            except Exception:
                pass
        self._kick_store_license_refresh(user_initiated=True)

    def _schedule_license_poll(self) -> None:
        """Arm the recurring unpaid Store-license re-check. Frozen builds only."""
        if not IS_FROZEN:
            return
        self._cancel_license_poll()
        if bool(self.config_store.data.get("paid_unlocked", False)):
            return  # Already unlocked -- nothing to poll for.
        self._license_poll_id = self.root.after(
            LICENSE_POLL_INTERVAL_MS, self._license_poll_tick
        )

    def _license_poll_tick(self) -> None:
        """Fire one background license check while unpaid, then re-arm. Stops
        once paid_unlocked latches True (so a paid user isn't polled forever)."""
        self._license_poll_id = None
        if not IS_FROZEN:
            return
        if bool(self.config_store.data.get("paid_unlocked", False)):
            return
        self._kick_store_license_refresh()
        self._license_poll_id = self.root.after(
            LICENSE_POLL_INTERVAL_MS, self._license_poll_tick
        )

    def _cancel_license_poll(self) -> None:
        poll_id = getattr(self, "_license_poll_id", None)
        if poll_id is None:
            return
        try:
            self.root.after_cancel(poll_id)
        except Exception:
            pass
        self._license_poll_id = None

    def _open_buy_store_listing(self) -> None:
        """Take the user to the Microsoft Store listing to purchase. Prefers the
        ms-windows-store: deep-link (opens the Store app directly) and falls
        back to the apps.microsoft.com web URL if the deep-link or ProductId
        isn't usable."""
        deep_link = (
            f"ms-windows-store://pdp/?ProductId={STORE_PRODUCT_ID}"
            if STORE_PRODUCT_ID
            else ""
        )
        opened = False
        if deep_link and sys.platform == "win32":
            try:
                os.startfile(deep_link)  # type: ignore[attr-defined]
                opened = True
            except Exception:
                opened = False
        if not opened:
            try:
                webbrowser.open(STORE_PRODUCT_WEB_URL)
            except Exception:
                pass
        # After nudging the user toward purchase, schedule a Store-license re-check
        # ~25s later so a completed purchase clears the lockout without restart.
        self.root.after(25_000, self._kick_store_license_refresh)

    def _prompt_trial_expired_dialog(self) -> None:
        """Modal shown when the user clicks Ready (or All) after the trial expired.
        Offers Buy / Refresh paths, and silently kicks a Store-license re-check
        so a purchase completed in another window unlocks without a restart."""
        msg = (
            f"Your free trial of {APP_TITLE} has ended.\n\n"
            f"Sending and receiving Ready alerts is disabled until you purchase "
            f"the app from the Microsoft Store.\n\n"
            f"Open the Microsoft Store now?"
        )
        try:
            if messagebox.askyesno(f"{APP_TITLE} — Free trial ended", msg):
                self._open_buy_store_listing()
            else:
                # User said No; still re-check the license in case they already
                # bought it earlier from outside the app.
                self._kick_store_license_refresh()
        except Exception:
            pass

    def _show_trial_reminder_dialog(self, remaining_days: int) -> None:
        if remaining_days <= 0:
            return
        day_word = "day" if remaining_days == 1 else "days"
        msg = (
            f"Your free trial of {APP_TITLE} ends in {remaining_days} {day_word}.\n\n"
            f"When the trial ends, sending and receiving Ready alerts will be disabled "
            f"until you purchase the app from the Microsoft Store.\n\n"
            f"Open the Microsoft Store now to purchase?"
        )
        try:
            if messagebox.askyesno(f"{APP_TITLE} — Free trial reminder", msg):
                self._open_buy_store_listing()
        except Exception:
            pass

    def _build_trial_banner(self, parent: tk.Misc, bg_color: str) -> None:
        """Create (but do not pack) the expiration / countdown banner shown
        above the main top_row. Idempotent — re-running on theme change replaces
        the existing widget."""
        if getattr(self, "_trial_banner_frame", None) is not None:
            try:
                self._trial_banner_frame.destroy()
            except Exception:
                pass
        self._trial_banner_frame = tk.Frame(parent, bg=bg_color)
        self._trial_banner_inner = tk.Frame(self._trial_banner_frame, bg=bg_color)
        self._trial_banner_inner.pack(fill="x", padx=_dz(4), pady=_dz(4))
        self._trial_banner_text_var = tk.StringVar(value="")
        self._trial_banner_label = tk.Label(
            self._trial_banner_inner,
            textvariable=self._trial_banner_text_var,
            bg=bg_color,
            fg="#5a3a00",
            font=_ui_font(10, "bold"),
            anchor="w",
            justify="left",
            wraplength=_dz(720),
        )
        self._trial_banner_label.pack(side="left", fill="x", expand=True, padx=(_dz(8), _dz(8)))
        self._trial_banner_buy_btn = tk.Button(
            self._trial_banner_inner,
            text="Buy in Microsoft Store",
            command=self._open_buy_store_listing,
            font=_ui_font(10, "bold"),
        )
        self._trial_banner_buy_btn.pack(side="right", padx=(_dz(4), _dz(8)))
        self._trial_banner_refresh_btn = tk.Button(
            self._trial_banner_inner,
            text=REFRESH_BTN_LABEL,
            command=self._on_refresh_button_click,
            font=_ui_font(10),
        )
        self._trial_banner_refresh_btn.pack(side="right", padx=(_dz(4), _dz(4)))

    def _set_trial_banner(self, visible: bool, text: str = "", expired: bool = False) -> None:
        frame = getattr(self, "_trial_banner_frame", None)
        if frame is None:
            return
        if not visible:
            try:
                frame.pack_forget()
            except Exception:
                pass
            return
        # Expired banner uses a red wash; reminder banner uses a soft amber.
        bg = "#f8d2d2" if expired else "#fff3c4"
        fg = "#7a1f1f" if expired else "#5a3a00"
        for w in (frame, self._trial_banner_inner, self._trial_banner_label,
                  self._trial_banner_buy_btn, self._trial_banner_refresh_btn):
            try:
                if isinstance(w, tk.Button):
                    w.configure(bg=bg)
                else:
                    w.configure(bg=bg)
            except Exception:
                pass
        try:
            self._trial_banner_label.configure(fg=fg)
        except Exception:
            pass
        self._trial_banner_text_var.set(text)
        try:
            # Pack above top_row by re-packing before it. We packed top_row with
            # fill=both, expand=True earlier; banner uses fill=x. The pack order
            # in _build_ui placed the banner immediately before top_row, so a
            # plain pack(fill=x) here keeps it on top.
            frame.pack(fill="x", pady=(0, _dz(8)), before=self._top_row_frame)
        except Exception:
            try:
                frame.pack(fill="x", pady=(0, _dz(8)))
            except Exception:
                pass

    def _apply_trial_state_to_ui(self) -> None:
        st = self._trial_status()
        if not st["enforce"] or st["paid"]:
            self._set_trial_banner(visible=False)
            return
        remaining = int(st["remaining_days"])
        if st["expired"]:
            self._set_trial_banner(
                visible=True,
                text=(
                    "Free trial ended. Sending and receiving Ready alerts is disabled. "
                    "Buy Chairside Ready Alert in the Microsoft Store to continue."
                ),
                expired=True,
            )
        elif remaining <= max(TRIAL_REMINDER_DAYS):
            day_word = "day" if remaining == 1 else "days"
            self._set_trial_banner(
                visible=True,
                text=f"Free trial: {remaining} {day_word} remaining. Buy in the Microsoft Store to keep using the app after the trial ends.",
                expired=False,
            )
        else:
            self._set_trial_banner(visible=False)
        # One-time reminder dialog when remaining first hits each threshold.
        if remaining in TRIAL_REMINDER_DAYS:
            shown_raw = self.config_store.data.get("trial_reminders_shown", [])
            try:
                shown = {int(x) for x in shown_raw}
            except (TypeError, ValueError):
                shown = set()
            if remaining not in shown:
                shown.add(remaining)
                self.config_store.data["trial_reminders_shown"] = sorted(shown)
                try:
                    self.config_store.save()
                except Exception:
                    pass
                # Defer the dialog so we don't block startup UI assembly.
                self.root.after(1200, lambda d=remaining: self._show_trial_reminder_dialog(d))

    def _autostart_windows_bat_path(self) -> str:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        else:
            startup_dir = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        return os.path.join(startup_dir, "Start Chairside Ready Alert.bat")

    def _autostart_macos_plist_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.chairside.readyalert.autostart.plist")

    def _migrate_legacy_startup_bat(self) -> None:
        # Pre-1.0.33 Store builds wrote a Startup-folder .bat whose contents
        # hard-coded sys.executable — i.e. a WindowsApps path containing the
        # then-current package version (e.g. ..._1.0.29.0_..._...). The Store
        # invalidates that path on every upgrade, producing a "specified path
        # does not exist" popup at every login. From 1.0.33 onward, autostart
        # on Store builds is owned by the AppxManifest windows.startupTask
        # extension; the legacy .bat is no longer needed and must be removed
        # to clear the broken popup for users already in that state.
        if not (IS_FROZEN and sys.platform.startswith("win")):
            return
        bat_path = self._autostart_windows_bat_path()
        try:
            if os.path.isfile(bat_path):
                os.remove(bat_path)
                _append_startup_log(f"Removed legacy autostart .bat at {bat_path}")
        except Exception as exc:
            _append_startup_log(f"Could not remove legacy autostart .bat: {exc}")

    def _is_autostart_enabled(self) -> bool:
        if sys.platform == "darwin":
            return os.path.isfile(self._autostart_macos_plist_path())
        if sys.platform.startswith("win"):
            return os.path.isfile(self._autostart_windows_bat_path())
        return False

    def _sync_autostart_state(self) -> None:
        # On Store builds, autostart is owned by the AppxManifest
        # windows.startupTask extension and toggled by the user in
        # Settings -> Apps -> Startup. We only clean up the legacy .bat
        # from pre-1.0.33 builds; the in-app var is informational.
        if IS_FROZEN and sys.platform.startswith("win"):
            self._migrate_legacy_startup_bat()
            self._autostart_var.set(True)
            return
        # Default for fresh installs: enable autostart automatically once.
        if "autostart_enabled" not in self.config_store.data:
            ok, _msg = self._set_autostart_enabled(True)
            self.config_store.data["autostart_enabled"] = bool(ok)
            self.config_store.save()
        desired = bool(self.config_store.data.get("autostart_enabled", True))
        current = self._is_autostart_enabled()
        if desired != current:
            ok, _msg = self._set_autostart_enabled(desired)
            if ok:
                current = self._is_autostart_enabled()
        enabled = current
        self._autostart_var.set(enabled)
        self.config_store.data["autostart_enabled"] = bool(enabled)
        self.config_store.save()

    def _set_autostart_enabled(self, enabled: bool) -> tuple[bool, str]:
        try:
            if sys.platform.startswith("win"):
                bat_path = self._autostart_windows_bat_path()
                startup_dir = os.path.dirname(bat_path)
                if enabled:
                    os.makedirs(startup_dir, exist_ok=True)
                    if IS_FROZEN:
                        # In a Store/PyInstaller build there is no .py to launch — exec the EXE directly.
                        exe = sys.executable
                        content = "@echo off\n" + f'start "" "{exe}"\n'
                    else:
                        script_path = os.path.join(_support_dir(), "chairside_ready_alert.py")
                        py = sys.executable
                        pyw = py
                        if os.path.basename(py).lower() == "python.exe":
                            candidate = os.path.join(os.path.dirname(py), "pythonw.exe")
                            if os.path.isfile(candidate):
                                pyw = candidate
                        content = (
                            "@echo off\n"
                            f'cd /d "{_support_dir()}"\n'
                            f'"{pyw}" "{script_path}"\n'
                        )
                    with open(bat_path, "w", encoding="ascii", errors="ignore") as f:
                        f.write(content)
                else:
                    try:
                        os.remove(bat_path)
                    except FileNotFoundError:
                        pass
                return True, "Windows startup setting updated."

            if sys.platform == "darwin":
                plist_path = self._autostart_macos_plist_path()
                launch_agents = os.path.dirname(plist_path)
                if enabled:
                    os.makedirs(launch_agents, exist_ok=True)
                    script_path = os.path.join(_support_dir(), "chairside_ready_alert.py")
                    py = sys.executable
                    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.chairside.readyalert.autostart</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>-s</string>
    <string>-u</string>
    <string>{script_path}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{_support_dir()}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{os.path.join(_support_dir(), "startup_log.txt")}</string>
  <key>StandardErrorPath</key>
  <string>{os.path.join(_support_dir(), "shortcut_stderr.txt")}</string>
</dict>
</plist>
"""
                    with open(plist_path, "w", encoding="utf-8") as f:
                        f.write(plist)
                else:
                    try:
                        os.remove(plist_path)
                    except FileNotFoundError:
                        pass
                return True, "macOS login item setting updated."

            return False, "Autostart is supported only on Windows and macOS."
        except Exception as exc:
            return False, str(exc)

    def _open_windows_startup_settings(self) -> None:
        try:
            os.startfile("ms-settings:startupapps")  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open Windows Settings:\n{exc}")

    def _toggle_autostart_from_menu(self) -> None:
        wanted = bool(self._autostart_var.get())
        ok, msg = self._set_autostart_enabled(wanted)
        if not ok:
            self._autostart_var.set(self._is_autostart_enabled())
            messagebox.showerror(APP_TITLE, f"Could not update startup setting:\n{msg}")
            return
        self._autostart_var.set(self._is_autostart_enabled())
        self.config_store.data["autostart_enabled"] = bool(self._autostart_var.get())
        self.config_store.save()
        self._append_diag(msg)

    def _record_tray_diag(self, text: str) -> None:
        # Tray/menu-bar startup + action diagnostics. Routed to the (rotated)
        # startup log so tray-visibility problems stay diagnosable from a file.
        # The old in-app diagnostics viewer window was unreachable (no caller)
        # and has been removed.
        _append_startup_log(f"[tray] {text}")

    def _manifest_url_candidates(self, base_url: str) -> list[str]:
        base = str(base_url or "").strip()
        if not base:
            return []
        urls = [base]
        lower = base.lower()
        if lower.endswith("/version.json"):
            urls.append(base[:-len("/version.json")] + "/version.json.example")
        elif lower.endswith(".json"):
            urls.append(base + ".example")
        # Preserve order but remove duplicates.
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _get_update_manifest_urls(self) -> list[str]:
        env = os.environ.get("CHAIRSIDE_UPDATE_MANIFEST_URL", "").strip()
        if env:
            return self._manifest_url_candidates(env)
        cfg = str(self.config_store.data.get("update_manifest_url", "") or "").strip()
        if cfg:
            return self._manifest_url_candidates(cfg)
        return self._manifest_url_candidates(str(UPDATE_MANIFEST_URL_BUILTIN or "").strip())

    def _check_for_updates_clicked(self) -> None:
        # Frozen Store builds receive updates through the Store; the in-app
        # download/install path would write to the read-only install dir.
        if IS_FROZEN:
            return
        threading.Thread(target=self._check_for_updates_worker, daemon=True).start()

    def _check_for_updates_worker(self) -> None:
        def ui(fn):
            try:
                self.root.after(0, fn)
            except tk.TclError:
                pass

        try:
            urls = self._get_update_manifest_urls()
            if not urls:
                ui(
                    lambda: messagebox.showinfo(
                        APP_TITLE,
                        "No update manifest URL is configured.\n\n"
                        "Add update_manifest_url to your settings file "
                        f"({CONFIG_FILE}), or set the environment variable "
                        "CHAIRSIDE_UPDATE_MANIFEST_URL.\n\n"
                        "Host a small JSON file (see version.json.example in the installer folder).",
                    )
                )
                return
            manifest = None
            used_url = ""
            last_error: Optional[Exception] = None
            for url in urls:
                try:
                    manifest = _fetch_update_manifest(url)
                    used_url = url
                    break
                except urllib.error.HTTPError as exc:
                    last_error = exc
                    # Try fallback URL (e.g., version.json.example) on 404, otherwise stop.
                    if exc.code != 404:
                        raise
                except urllib.error.URLError as exc:
                    last_error = exc
                    # Network errors could be transient; try next candidate.
                    continue
            if manifest is None:
                if last_error is not None:
                    raise last_error
                raise ValueError("Could not load update manifest.")
            if used_url and str(self.config_store.data.get("update_manifest_url", "") or "").strip() != used_url:
                self.config_store.data["update_manifest_url"] = used_url
                try:
                    self.config_store.save()
                except Exception:
                    pass
            remote = str(manifest.get("version", "")).strip()
            if not remote:
                raise ValueError('Manifest is missing a "version" field.')
            page = (
                str(manifest.get("release_page_url", "") or "").strip()
                or str(manifest.get("download_page_url", "") or "").strip()
            )
            notes = str(manifest.get("release_notes", "") or "").strip()
            cmp = _compare_version_strings(remote, APP_VERSION)

            def present():
                if cmp > 0:
                    body = (
                        f"A newer version is available.\n\n"
                        f"  Latest: {remote}\n"
                        f"  This app: {APP_VERSION}\n"
                    )
                    if notes:
                        body += f"\n{notes}\n"
                    if messagebox.askyesno(
                        APP_TITLE,
                        body + "\nInstall this update now (download + replace update files)?",
                    ):
                        threading.Thread(
                            target=self._download_and_install_update_worker,
                            args=(manifest, remote, page),
                            daemon=True,
                        ).start()
                        return
                    if page and messagebox.askyesno(APP_TITLE, "Open the release page in your browser instead?"):
                        webbrowser.open(page)
                elif cmp < 0:
                    messagebox.showinfo(
                        APP_TITLE,
                        f"This build ({APP_VERSION}) is newer than the published version ({remote}).",
                    )
                else:
                    messagebox.showinfo(APP_TITLE, f"You're up to date.\n\nVersion {APP_VERSION}")

            ui(present)
        except urllib.error.URLError as exc:
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg:
                msg += (
                    "\n\nTLS certificate verification failed.\n"
                    "Try re-running the installer (it now installs certifi),\n"
                    "or run:\n"
                    f'  "{sys.executable}" -m pip install --user --upgrade certifi'
                )
            ui(lambda e=msg: messagebox.showerror(APP_TITLE, f"Update check failed (network):\n{e}"))
        except json.JSONDecodeError as exc:
            ui(lambda e=str(exc): messagebox.showerror(APP_TITLE, f"Update manifest is not valid JSON:\n{e}"))
        except Exception as exc:
            ui(lambda e=str(exc): messagebox.showerror(APP_TITLE, f"Update check failed:\n{e}"))

    def _manifest_file_entries(self, manifest: dict) -> list[dict]:
        """
        Build normalized update entries: [{"path": <relative>, "url": <https>, "sha256": <hex>|""}, ...]
        Supports:
        - Flat fields: download_url/sha256 -> chairside_ready_alert.py
        - files object: {"filename": {"url": "...", "sha256": "..."}, ...}
        """
        entries: list[dict] = []
        flat_url = str(manifest.get("download_url", "") or "").strip()
        flat_sha = str(manifest.get("sha256", "") or "").strip()
        if flat_url:
            entries.append({"path": "chairside_ready_alert.py", "url": flat_url, "sha256": flat_sha})

        files = manifest.get("files")
        if isinstance(files, dict):
            for rel_path, info in files.items():
                if not isinstance(info, dict):
                    continue
                url = str(info.get("url", "") or "").strip()
                if not url:
                    continue
                sha = str(info.get("sha256", "") or "").strip()
                entries.append({"path": str(rel_path), "url": url, "sha256": sha})

        # Deduplicate by path (later entries win).
        latest: dict[str, dict] = {}
        for e in entries:
            latest[e["path"]] = e
        normalized: list[dict] = []
        for rel_path, e in latest.items():
            rp = str(rel_path or "").strip()
            if not rp:
                continue
            if os.path.isabs(rp) or ".." in rp or "/" in rp or "\\" in rp:
                # Unsafe paths still hard-fail — these would let a hostile
                # manifest write outside the user-data directory. No legit
                # release uses them.
                raise ValueError(f"Unsafe update path in manifest: {rp!r}")
            if rp not in UPDATE_ALLOWED_FILES:
                # An UNKNOWN filename in the manifest used to hard-fail and
                # kill the whole update batch, which trapped users on old
                # versions whenever we added a new auxiliary file to the
                # repo (ready_animations.py in 1.0.41 broke every direct
                # installer client until 1.0.47). Now we skip the entry
                # with a startup-log warning and let the rest of the
                # update proceed — including any chairside_ready_alert.py
                # change that broadens the whitelist for next time.
                _append_startup_log(
                    f"Manifest update skipped: '{rp}' is not in this "
                    f"version's UPDATE_ALLOWED_FILES whitelist. The file "
                    f"will be picked up after the next app update."
                )
                continue
            normalized.append({"path": rp, "url": e["url"], "sha256": e["sha256"]})

        # Stable order: keep app first so restart prompt is intuitive.
        normalized.sort(key=lambda x: (0 if x["path"] == "chairside_ready_alert.py" else 1, x["path"]))
        return normalized

    def _download_and_install_update_worker(self, manifest: dict, remote_version: str, release_page_url: str) -> None:
        def ui(fn):
            try:
                self.root.after(0, fn)
            except tk.TclError:
                pass

        try:
            entries = self._manifest_file_entries(manifest)
            if not entries:
                def no_url() -> None:
                    if release_page_url:
                        if messagebox.askyesno(
                            APP_TITLE,
                            "This update manifest has no file URLs for automatic install.\n\n"
                            "Open the release page instead?",
                        ):
                            webbrowser.open(release_page_url)
                    else:
                        messagebox.showerror(
                            APP_TITLE,
                            "This update manifest has no update file URLs.\n"
                            "Add download_url or files.{name}.url (and optional sha256) to enable automatic updates.",
                        )
                ui(no_url)
                return

            target_path = os.path.abspath(__file__)
            target_dir = os.path.dirname(target_path)
            stamp = int(time.time())

            # Download and verify everything first.
            payloads: dict[str, bytes] = {}
            for e in entries:
                payload = _download_bytes(e["url"])
                expected_sha256 = str(e.get("sha256", "") or "").strip()
                if expected_sha256:
                    got = _sha256_hex(payload).lower()
                    expected = expected_sha256.lower()
                    if got != expected:
                        raise ValueError(
                            "Downloaded update hash mismatch.\n"
                            f"File: {e['path']}\n"
                            f"Expected: {expected}\n"
                            f"Received: {got}"
                        )
                payloads[e["path"]] = payload

            replaced: list[tuple[str, str, Optional[int]]] = []  # (target, backup, old_mode)
            created: list[str] = []

            try:
                for e in entries:
                    rel = e["path"]
                    target = os.path.join(target_dir, rel)
                    old_mode: Optional[int] = None
                    backup = target + f".bak-{stamp}"
                    tmp = target + ".update.tmp"

                    if os.path.exists(target):
                        try:
                            old_mode = stat.S_IMODE(os.stat(target).st_mode)
                        except OSError:
                            old_mode = None
                        os.replace(target, backup)
                        replaced.append((target, backup, old_mode))
                    else:
                        created.append(target)

                    with open(tmp, "wb") as f:
                        f.write(payloads[rel])
                    os.replace(tmp, target)

                    # Keep shell installer executable on macOS after replacement.
                    if target.endswith(".command") or target.endswith("install_chairside_ready_alert_macos.sh"):
                        mode = old_mode if old_mode is not None else 0o755
                        try:
                            os.chmod(target, mode | 0o111)
                        except OSError:
                            pass
            except Exception:
                # Roll back changed files.
                for target, backup, _old_mode in reversed(replaced):
                    try:
                        os.replace(backup, target)
                    except OSError:
                        pass
                for target in created:
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except OSError:
                        pass
                raise

            def done() -> None:
                names = ", ".join(e["path"] for e in entries)
                needs_restart = any(e["path"] == "chairside_ready_alert.py" for e in entries)
                if needs_restart:
                    if messagebox.askyesno(
                        APP_TITLE,
                        f"Update installed successfully.\n\n"
                        f"Updated to: {remote_version}\n"
                        f"Files: {names}\n\n"
                        "Restart now to use the new version?",
                    ):
                        self._restart_after_update(target_path, target_dir)
                else:
                    messagebox.showinfo(
                        APP_TITLE,
                        f"Update installed successfully.\n\nUpdated to: {remote_version}\nFiles: {names}",
                    )
            ui(done)
        except urllib.error.URLError as exc:
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg:
                msg += (
                    "\n\nTLS certificate verification failed.\n"
                    "Try re-running the installer (it now installs certifi),\n"
                    "or run:\n"
                    f'  "{sys.executable}" -m pip install --user --upgrade certifi'
                )
            ui(lambda e=msg: messagebox.showerror(APP_TITLE, f"Download failed (network):\n{e}"))
        except Exception as exc:
            ui(lambda e=str(exc): messagebox.showerror(APP_TITLE, f"Automatic update failed:\n{e}"))

    def _restart_after_update(self, script_path: str, working_dir: str) -> None:
        try:
            self._persist_form()
        except Exception:
            pass
        try:
            subprocess.Popen([sys.executable, "-u", script_path], cwd=working_dir)
        except Exception as exc:
            messagebox.showwarning(
                APP_TITLE,
                "Update installed, but automatic restart failed.\n\n"
                f"Please launch Chairside Ready Alert again manually.\n\nDetails: {exc}",
            )
            return
        self._quitting = True
        self.on_close()

    def _effective_station_labels(self) -> list[str]:
        """Authoritative list of station names available in the Station name combobox
        and the Defaults menu. Lazy-migrates from DEFAULT_LABELS + custom_station_labels
        on first read after upgrade."""
        existing = self.config_store.data.get("station_labels")
        if isinstance(existing, list) and existing:
            return [str(x) for x in existing]
        custom = list(self.config_store.data.get("custom_station_labels", []) or [])
        seeded: list[str] = list(DEFAULT_LABELS) + [c for c in custom if c not in DEFAULT_LABELS]
        self.config_store.data["station_labels"] = seeded
        try:
            self.config_store.save()
        except Exception:
            pass
        return list(seeded)

    def _removed_station_labels(self) -> list[str]:
        return [str(x) for x in (self.config_store.data.get("removed_labels", []) or [])]

    def _set_station_labels(self, labels: list[str]) -> None:
        """Persist a new station_labels list and refresh main-window UI that depends on it."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in labels:
            name = str(raw).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        self.config_store.data["station_labels"] = cleaned
        self.config_store.save()
        self._sync_discovery_label_state()
        self._refresh_station_label_widgets()

    def _set_removed_labels(self, removed: list[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in removed:
            name = str(raw).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        self.config_store.data["removed_labels"] = cleaned
        self.config_store.save()
        self._sync_discovery_label_state()

    def _sync_discovery_label_state(self) -> None:
        """Push current station_labels + removed_labels to LanDiscovery so the next
        beacon carries the latest snapshot for newly-online peers."""
        if getattr(self, "discovery", None) is None:
            return
        try:
            self.discovery.update_station_lists(
                self._effective_station_labels(),
                self._removed_station_labels(),
            )
        except Exception:
            pass

    def _broadcast_label_event(self, action: str, label: str) -> None:
        if getattr(self, "discovery", None) is None:
            return
        try:
            self.discovery.broadcast_label_event(action, label)
        except Exception:
            pass

    def _merge_remote_labels_snapshot(self, inc_labels: list[str], inc_removed: list[str]) -> None:
        """A peer sent its current labels + tombstones. Honor remote tombstones
        (apply them locally) and only adopt remote labels we haven't tombstoned
        ourselves."""
        my_labels = self._effective_station_labels()
        my_removed = self._removed_station_labels()
        my_labels_set = set(my_labels)
        my_removed_set = set(my_removed)
        labels_changed = False
        removed_changed = False
        # Remote tombstones — propagate.
        for name in inc_removed:
            if not name:
                continue
            if name in my_labels_set:
                my_labels = [l for l in my_labels if l != name]
                my_labels_set.discard(name)
                labels_changed = True
            if name not in my_removed_set:
                my_removed.append(name)
                my_removed_set.add(name)
                removed_changed = True
        # Remote additions — adopt unless we have a local tombstone.
        for name in inc_labels:
            if not name or name in my_removed_set or name in my_labels_set:
                continue
            my_labels.append(name)
            my_labels_set.add(name)
            labels_changed = True
        if removed_changed:
            self._set_removed_labels(my_removed)
        if labels_changed:
            self._set_station_labels(my_labels)

    def _apply_remote_label_event(self, action: str, label: str) -> None:
        """Immediate add/remove from a peer's broadcast — applies right away."""
        labels = self._effective_station_labels()
        removed = self._removed_station_labels()
        if action == "add":
            changed_r = False
            if label in removed:
                removed = [l for l in removed if l != label]
                changed_r = True
            changed_l = False
            if label not in labels:
                labels.append(label)
                changed_l = True
            if changed_r:
                self._set_removed_labels(removed)
            if changed_l:
                self._set_station_labels(labels)
        elif action == "remove":
            current = self.label_var.get().strip() if hasattr(self, "label_var") else ""
            if label == current:
                # Don't let a remote remove orphan this workstation's own station name.
                return
            changed_l = False
            if label in labels:
                labels = [l for l in labels if l != label]
                changed_l = True
            changed_r = False
            if label not in removed:
                removed.append(label)
                changed_r = True
            if changed_r:
                self._set_removed_labels(removed)
            if changed_l:
                self._set_station_labels(labels)
            # Also drop from default_targets locally.
            defaults = list(self.config_store.data.get("default_targets", []) or [])
            if label in defaults:
                self.config_store.data["default_targets"] = [d for d in defaults if d != label]
                self.config_store.save()

    def _refresh_station_label_widgets(self) -> None:
        """Update the Station name combobox values and the Defaults menu after the
        editable station_labels list changes."""
        labels = self._effective_station_labels()
        if hasattr(self, "label_combo") and self.label_combo is not None:
            try:
                self.label_combo["values"] = list(labels)
            except tk.TclError:
                pass
        self._rebuild_default_menu()

    def _rebuild_default_menu(self) -> None:
        if self._default_menu is None:
            return
        menu = self._default_menu
        menu.delete(0, "end")
        self._default_menu_vars.clear()
        me = self.label_var.get().strip() if hasattr(self, "label_var") else ""
        default_targets = set(self.config_store.data.get("default_targets", []))
        known = [l for l in self._effective_station_labels() if l != me]
        for label in self.online_labels:
            if label not in known and label != me:
                known.append(label)
        if known:
            for label in known:
                var = tk.BooleanVar(value=label in default_targets)
                self._default_menu_vars[label] = var
                menu.add_checkbutton(label=label, variable=var,
                                     command=lambda l=label: self._toggle_default_target(l))
        else:
            menu.add_command(label="(No other stations configured)", state="disabled")
        menu.add_separator()
        menu.add_command(label="Add or Remove Computers...",
                         command=self._open_manage_stations_dialog)

    def _open_manage_stations_dialog(self) -> None:
        """Modal-ish editor for the station_labels list reachable from Defaults menu."""
        existing = getattr(self, "_manage_stations_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_set()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        win.title("Add or Remove Computers")
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        self._manage_stations_window = win

        def _on_close() -> None:
            self._manage_stations_window = None
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # Top: text entry + Add button.
        add_frame = ttk.Frame(win, padding=(_dz(12), _dz(12), _dz(12), _dz(6)))
        add_frame.pack(fill="x")
        ttk.Label(add_frame, text="Computer name:").grid(row=0, column=0, columnspan=2, sticky="w")
        entry_var = tk.StringVar()
        entry = ttk.Entry(add_frame, textvariable=entry_var, width=28)
        entry.grid(row=1, column=0, sticky="we", pady=(_dz(4), 0))
        add_btn = ttk.Button(add_frame, text="Add", state="disabled")
        add_btn.grid(row=1, column=1, sticky="e", padx=(_dz(8), 0), pady=(_dz(4), 0))
        add_frame.columnconfigure(0, weight=1)

        # Middle: listbox of computers.
        list_frame = ttk.Frame(win, padding=(_dz(12), _dz(6), _dz(12), _dz(6)))
        list_frame.pack(fill="both", expand=True)
        ttk.Label(list_frame, text="Computers:").pack(anchor="w")
        list_inner = ttk.Frame(list_frame)
        list_inner.pack(fill="both", expand=True, pady=(_dz(4), 0))
        listbox = tk.Listbox(list_inner, selectmode="single", height=10, exportselection=False,
                             activestyle="dotbox")
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_inner, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        for label in self._effective_station_labels():
            listbox.insert("end", label)

        # Bottom: Remove on the left, Close on the right.
        bottom = ttk.Frame(win, padding=(_dz(12), _dz(6), _dz(12), _dz(12)))
        bottom.pack(fill="x")
        remove_btn = ttk.Button(bottom, text="Remove", state="disabled")
        remove_btn.pack(side="left")
        ttk.Button(bottom, text="Close", command=_on_close).pack(side="right")

        # Enable/disable Add depending on entry contents.
        def _update_add_state(*_args: object) -> None:
            text = entry_var.get().strip()
            add_btn.configure(state=("normal" if text else "disabled"))
        entry_var.trace_add("write", _update_add_state)

        # Enable/disable Remove depending on listbox selection.
        def _update_remove_state(_event: object = None) -> None:
            remove_btn.configure(state=("normal" if listbox.curselection() else "disabled"))
        listbox.bind("<<ListboxSelect>>", _update_remove_state)

        def _do_add() -> None:
            name = entry_var.get().strip()
            if not name:
                return
            labels = self._effective_station_labels()
            if name in labels:
                messagebox.showinfo(APP_TITLE,
                                    f'"{name}" is already in the list.',
                                    parent=win)
                entry.focus_set()
                return
            # Explicit Add overrides any local tombstone for this name.
            removed = self._removed_station_labels()
            if name in removed:
                self._set_removed_labels([r for r in removed if r != name])
            labels.append(name)
            self._set_station_labels(labels)
            listbox.insert("end", name)
            entry_var.set("")
            _update_add_state()
            entry.focus_set()
            # Tell every other workstation immediately. Beacons will reinforce.
            self._broadcast_label_event("add", name)
        add_btn.configure(command=_do_add)
        entry.bind("<Return>", lambda _e: _do_add())

        def _do_remove() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            name = listbox.get(idx)
            current = self.label_var.get().strip() if hasattr(self, "label_var") else ""
            if name == current:
                messagebox.showwarning(APP_TITLE,
                                       f'Cannot remove "{name}" — it is the current station name '
                                       'for this computer. Change this computer\'s station name '
                                       'to something else first, then remove it from the list.',
                                       parent=win)
                return
            labels = self._effective_station_labels()
            if name in labels:
                labels.remove(name)
                self._set_station_labels(labels)
            # Persist a local tombstone so a peer's beacon does not silently bring
            # this label back. The remove event also tells peers to tombstone.
            removed = self._removed_station_labels()
            if name not in removed:
                removed.append(name)
                self._set_removed_labels(removed)
            # Also drop it from default_targets so a stale entry doesn't linger.
            defaults = list(self.config_store.data.get("default_targets", []) or [])
            if name in defaults:
                defaults = [d for d in defaults if d != name]
                self.config_store.data["default_targets"] = defaults
                self.config_store.save()
            listbox.delete(idx)
            _update_remove_state()
            self._broadcast_label_event("remove", name)
        remove_btn.configure(command=_do_remove)

        entry.focus_set()
        win.bind("<Escape>", lambda _e: _on_close())

    def _toggle_default_target(self, label: str) -> None:
        var = self._default_menu_vars.get(label)
        if var is None:
            return
        defaults = set(self.config_store.data.get("default_targets", []))
        if var.get():
            defaults.add(label)
        else:
            defaults.discard(label)
        self.config_store.data["default_targets"] = sorted(defaults)
        self.config_store.save()
        if label in self.target_vars:
            self.target_vars[label].set(var.get())

    def _apply_theme(self, name: str) -> None:
        if name not in THEMES:
            name = DEFAULT_THEME
        theme = THEMES[name]
        self._current_theme = theme
        # Must sync alert sound / volume from the UI before saving; otherwise this save()
        # can overwrite chairside_ready_alert_config.json with a stale alert_sound (often "Bright Chime").
        if hasattr(self, "alert_sound_var"):
            self._write_form_to_data()
        self.config_store.data["theme"] = name
        self.config_store.save()
        self._theme_var.set(name)

        bg       = theme["bg"]
        card     = theme["card_bg"]
        accent   = theme["accent"]
        atext    = theme["accent_text"]
        title_c  = theme["title"]
        text_c   = theme["text"]
        sub_c    = theme["sub"]
        input_bg = theme["input_bg"]
        slider   = theme["slider_track"]
        cborder  = theme.get("card_border")

        self.root.configure(bg=bg)
        # Root bg alone is often hidden: the main shell frames must match the theme background.
        if hasattr(self, "_main_shell_frames"):
            for f in self._main_shell_frames:
                f.configure(bg=bg)

        s = ttk.Style()
        s.configure("TFrame",         background=bg)
        s.configure("Card.TFrame",    background=card)
        s.configure("TLabel",         background=bg,   foreground=text_c, font=_ttk_font(10))
        s.configure("Card.TLabel",    background=card, foreground=sub_c, font=_ttk_font(10))
        s.configure("Title.TLabel",   background=bg,   foreground=title_c, font=_ttk_font(13, "bold"))
        # Section headings: small caps-style uppercase labels in the muted color.
        s.configure("CardTitle.TLabel", background=card, foreground=sub_c, font=_ttk_font(9, "bold"))
        s.configure("Subtitle.TLabel",  background=bg,  foreground=sub_c, font=_ttk_font(10))
        s.configure("TCheckbutton",   background=card, foreground=text_c)
        s.map("TCheckbutton",         background=[("active", card)])
        s.configure("TScale",         background=card, troughcolor=slider)
        s.configure("TCombobox",      fieldbackground=input_bg, foreground=text_c, background=card,
                    arrowcolor=sub_c, bordercolor=theme["input_border"],
                    lightcolor=input_bg, darkcolor=input_bg)
        s.map("TCombobox",
              fieldbackground=[("readonly", input_bg), ("disabled", input_bg)],
              foreground=[("readonly", text_c)],
              # No blue "selected text" band on a focused readonly combobox.
              selectbackground=[("readonly", input_bg)],
              selectforeground=[("readonly", text_c)])
        # Dropdown lists: colors for popdowns created from now on, plus any
        # already created (Tk builds each popdown lazily on first open).
        for opt, val in (("background", input_bg), ("foreground", text_c),
                         ("selectBackground", accent), ("selectForeground", atext)):
            self.root.option_add(f"*TCombobox*Listbox.{opt}", val)
        for combo in (getattr(self, "label_combo", None), getattr(self, "sound_combo", None)):
            if combo is None:
                continue
            try:
                pd = combo.tk.call("ttk::combobox::PopdownWindow", combo)
                combo.tk.call(f"{pd}.f.l", "configure", "-background", input_bg,
                              "-foreground", text_c, "-selectbackground", accent,
                              "-selectforeground", atext)
            except tk.TclError:
                pass
        s.configure("TEntry",         fieldbackground=input_bg, foreground=text_c)
        s.configure("Primary.TButton",  background=accent, foreground=atext)
        s.map("Primary.TButton",        background=[("active", accent), ("pressed", accent)])
        s.configure("Ready.TButton",    background=accent, foreground=atext)
        s.map("Ready.TButton",          background=[("active", accent), ("pressed", accent)])
        s.configure("Preview.TButton",  background=accent, foreground=atext)
        s.map("Preview.TButton",        background=[("active", accent)])

        dark = _is_dark(bg)
        if hasattr(self, "log"):
            self.log.set_theme(theme)
        for w in getattr(self, "_themed_widgets", ()):
            try:
                w.set_theme(theme)
            except tk.TclError:
                pass
        if hasattr(self, "_header_title"):
            self._header_title.configure(bg=bg, fg=title_c)
            self._header_sub.configure(bg=bg, fg=sub_c)
            self._station_edit_link.configure(bg=bg, fg=accent)
        if _ready_animations is not None:
            # Stick figure + effects follow the theme (light ink on Midnight).
            try:
                _ready_animations.set_palette(theme["ink"], accent, card, dark)
            except Exception:
                pass
        if hasattr(self, "status_label"):
            self.status_label.configure(fg=theme["status"], bg=card, font=_ui_font(9, "bold"))
        if hasattr(self, "duplicate_name_label"):
            # Color + text are decided by _refresh_station_name_warning so the
            # red station-unset prompt isn't overwritten by a theme switch.
            self._refresh_station_name_warning()
        if self._tray_icon is not None:
            try:
                self._tray_icon.icon = self._create_tray_icon_image()
            except Exception:
                pass
        for c in self._cards:
            # Cards are borderless with a soft shadow; dark themes add a
            # faint outline since a shadow barely shows on a dark page.
            c.update_colors(card, bg, cborder if dark else None)
        for btn in self._buttons:
            btn.update_colors(accent, atext, card)
        if hasattr(self, "_ready_btn_widget"):
            self._set_ready_button_enabled(getattr(self, "_ready_button_enabled", True))
        # Update sound_row and targets_frame direct-color tk frames
        for f in (getattr(self, "_sound_strip", None), getattr(self, "_send_row", None),
                  getattr(self, "_ready_btn_spacer", None)):
            if f is not None:
                f.configure(bg=card)
        if hasattr(self, "targets_frame"):
            self.targets_frame.configure(bg=card)
        # Force checkboxes to repaint with new card background
        if hasattr(self, "_target_signature"):
            self._target_signature = None
            self._refresh_target_checkboxes(list(self.online_labels))
            self._apply_default_target_selection(force=False)
        self.root.update_idletasks()

    def _build_ui(self) -> None:
        # Initialise all tk variables first — some widgets reference them before their section is built
        self.label_var = tk.StringVar(value=STATION_NAME_PROMPT)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.alert_sound_var = tk.StringVar(value=ALERT_SOUND_OPTIONS[0])
        self.alert_volume_var = tk.IntVar(value=50)

        t = self._current_theme
        cbg, abg = t["card_bg"], t["bg"]
        cborder = t.get("card_border")

        main = tk.Frame(self.root, bg=abg, padx=_dz(10), pady=_dz(8))
        main.pack(fill="both", expand=True)

        # Header, one line: logo + app name on the left; "This station: X
        # Edit" and the live status pill ("3 stations online") on the right.
        header = tk.Frame(main, bg=abg)
        header.pack(fill="x", pady=(0, _dz(4)))
        self._themed_widgets: list = []
        logo = AppLogo(header, t, size=30)
        logo.pack(side="left")
        self._themed_widgets.append(logo)
        title_box = tk.Frame(header, bg=abg)
        title_box.pack(side="left", padx=(_dz(8), 0))
        self._header_title = tk.Label(title_box, text=APP_TITLE, bg=abg, fg=t["title"],
                                      font=_ui_font(14, "bold"))
        self._header_title.pack(anchor="w")
        self._status_pill = StatusPill(header, t)
        self._status_pill.pack(side="right")
        self._themed_widgets.append(self._status_pill)
        # The station name is deliberately not an always-visible dropdown:
        # staff mistook it for "who to send to" and renamed their own
        # station by accident. Edit opens a dialog instead.
        self._station_box = tk.Frame(header, bg=abg)
        self._station_box.pack(side="right", padx=(0, _dz(12)))
        self._header_sub_row = tk.Frame(self._station_box, bg=abg)
        self._header_sub_row.pack(anchor="e")
        self._header_sub = tk.Label(self._header_sub_row, text="", bg=abg, fg=t["sub"], font=_ui_font(10))
        self._header_sub.pack(side="left")
        self._station_edit_link = tk.Label(self._header_sub_row, text="Edit", bg=abg, fg=t["accent"],
                                           font=_ui_font(9, "underline"), cursor="hand2", bd=0, padx=0)
        self._station_edit_link.pack(side="left", padx=(_dz(8), 0))
        self._station_edit_link.bind("<Button-1>", lambda _e: self._open_station_name_dialog())
        self.duplicate_name_var = tk.StringVar(value="")
        self.duplicate_name_label = tk.Label(self._station_box, textvariable=self.duplicate_name_var,
                                             fg=t["sub"], bg=abg, font=_ui_font(8, "bold"), anchor="e",
                                             justify="right", bd=0)
        self.label_var.trace_add("write", lambda *_: self._refresh_header_station())
        self._refresh_header_station()

        # Top row: Ready Messages (full width).
        # Expands vertically so the Ready Messages log absorbs any extra
        # window height that isn't claimed by the Send to card below.
        # The Send to card sizes to its content (see below), so when
        # there are few peers detected the log grows taller; when there are
        # many peers the Send to card grows and the log shrinks.
        top_row = tk.Frame(main, bg=abg)
        # Build (but do not yet show) the trial banner so it is positioned
        # immediately above top_row when _apply_trial_state_to_ui packs it.
        self._build_trial_banner(main, bg_color=abg)
        top_row.pack(fill="both", expand=True, pady=(0, _dz(2)))
        self._top_row_frame = top_row
        # Ready button, Send to and sound cards live in a bottom stack packed
        # ahead of top_row, so on a short screen the Ready Messages log
        # (newest at its bottom) gives up height instead of these panels
        # being cut off.
        bottom = tk.Frame(main, bg=abg)
        bottom.pack(side="bottom", fill="x", before=top_row)
        self._main_shell_frames = (main, header, top_row, title_box, self._station_box,
                                   self._header_sub_row, bottom)

        # Ready Messages — expanding right card
        # Two side-by-side panels with matching tops and bottoms: Ready
        # Messages (left, expands) and the Ready button (right). The button
        # panel is packed first so it keeps its width when space is tight.
        _bp = RoundedCard(top_row, bg=cbg, outer_bg=abg, padding=10)
        _bp.pack(side="right", fill="y", padx=(_dz(4), 0))
        _bp.fill_height = True
        self._cards.append(_bp)
        _rc = RoundedCard(top_row, bg=cbg, outer_bg=abg, padding=10)
        _rc._cv.configure(width=_px(300))  # canvas default (~380) would inflate the min width
        _rc.pack(side="left", fill="both", expand=True)
        self._cards.append(_rc)
        right = _rc.inner_frame

        # LAN status text. Shown in the header pill (see
        # _refresh_status_pill); this label is kept as the status source but
        # never packed.
        self.status_var = tk.StringVar(value="Starting…")
        self.status_label = tk.Label(right, textvariable=self.status_var,
                                      fg=t["status"], bg=cbg,
                                      font=_ui_font(9, "bold"))

        self._ready_messages_header = ttk.Label(right, text="READY MESSAGES",
                                                style="CardTitle.TLabel")
        self._ready_messages_header.pack(anchor="w")

        # The inner frame has a natural height only, so the log gets its
        # height from the card: fill the card canvas vertically.
        self.log = ReadyLog(right, t)
        self.log.pack(fill="both", expand=True, pady=(_dz(4), 0))

        # Ready button panel. A spacer the height of the "READY MESSAGES"
        # title keeps the button's top level with the top of the log.
        bp = _bp.inner_frame
        self._ready_btn_spacer = tk.Frame(
            bp, bg=cbg, height=self._ready_messages_header.winfo_reqheight() + 4)
        self._ready_btn_spacer.pack(fill="x")
        ready_btn = ReadyButton(bp, t, text="Ready", command=self.send_ready_selected)
        ready_btn.configure(width=_px(220), height=_px(60))
        ready_btn.pack(fill="both", expand=True)
        # Stash the Ready button reference so the whimsy animations can
        # anchor "emerge from behind this widget" positioning to it.
        self._ready_btn_widget = ready_btn

        def _size_ready_panel(e: tk.Event) -> None:
            # Button panel is ~34% of the row's width; its height follows the row.
            if e.widget is top_row and e.width > 40:
                w = max(_px(200), min(_px(470), int(e.width * 0.34)))
                if int(_bp._cv.cget("width")) != w:
                    _bp._cv.configure(width=w)

        top_row.bind("<Configure>", _size_ready_panel, add="+")
        _bp._cv.configure(width=_px(240))
        _rc.fill_height = True

        # Bottom panel: Sound + Volume on one line, then the Send to computers.
        _bc = RoundedCard(bottom, bg=cbg, outer_bg=abg, padding=10)
        _bc.pack(fill="x")
        self._cards.append(_bc)
        panel = _bc.inner_frame

        # Sound [combo] (play)    Volume [slider]
        st = tk.Frame(panel, bg=cbg)
        st.pack(fill="x")
        self._sound_strip = st
        ttk.Label(st, text="SOUND", style="CardTitle.TLabel").pack(side="left", padx=(0, _dz(8)))
        self.sound_combo = ttk.Combobox(
            st, textvariable=self.alert_sound_var, values=ALERT_SOUND_OPTIONS, state="readonly", width=16
        )
        self.sound_combo.pack(side="left")
        prev_btn = IconButton(
            st, t,
            command=lambda: self._play_alert_sound(
                self.alert_sound_var.get(), int(self.alert_volume_var.get())
            ),
        )
        prev_btn.pack(side="left", padx=(_dz(6), 0))
        self._themed_widgets.append(prev_btn)
        ttk.Label(st, text="VOLUME", style="CardTitle.TLabel").pack(side="left", padx=(_dz(20), _dz(8)))
        self._volume_scale = VolumeSlider(st, self.alert_volume_var, t, on_release=self._persist_form)
        self._volume_scale.pack(side="left", fill="x", expand=True)
        self._themed_widgets.append(self._volume_scale)

        # Send to: "SEND TO" sits left of the chips, and the chip area has a
        # constant height that fits two rows (_layout_target_chips).
        send_row = tk.Frame(panel, bg=cbg)
        send_row.pack(fill="x", pady=(_dz(8), 0))
        self._send_row = send_row
        ttk.Label(send_row, text="SEND TO", style="CardTitle.TLabel").pack(side="left", anchor="center",
                                                                         padx=(0, _dz(12)))
        self.targets_frame = tk.Frame(send_row, bg=cbg, height=self._send_to_area_height())
        self.targets_frame.pack(side="left", fill="x", expand=True)
        self.targets_frame.pack_propagate(False)
        self._target_chips: list = []
        self._chip_layout_key = None
        self.targets_frame.bind("<Configure>", lambda _e: self._layout_target_chips())
        # The station-name picker only exists while the station dialog is open.
        self.label_combo = None
        self._refresh_target_checkboxes([])
        self._refresh_lan_status_banner()

    def _refresh_header_station(self) -> None:
        if not hasattr(self, "_header_sub"):
            return
        name = self.label_var.get().strip()
        if not name or name == STATION_NAME_PROMPT:
            text = "This station isn't set up yet"
        else:
            text = f"This station: {name}"
        self._header_sub.configure(text=text)
        self._station_edit_link.configure(text="Edit" if self._has_real_station_label() else "Set up")

    def _refresh_status_pill(self) -> None:
        pill = getattr(self, "_status_pill", None)
        if pill is None:
            return
        if self._network_running and self.online_labels:
            me = self.label_var.get().strip()
            n = len([lbl for lbl in self.online_labels if lbl != me]) or len(self.online_labels)
            pill.set_status(f"{n} station{'s' if n != 1 else ''} online", "online")
        elif self._network_running:
            pill.set_status(self.status_var.get(), "waiting")
        else:
            pill.set_status(self.status_var.get(), "offline")

    def _refresh_lan_status_banner(self) -> None:
        """LAN summary for the header status pill: "N stations online" when
        peers are up, otherwise "Not connected" / "Waiting for other
        workstations…" (an empty Send to card alone is ambiguous -- no peers
        yet, or a broken network)."""
        if not self._network_running:
            self.status_var.set("Not connected")
        elif not self.online_labels:
            self.status_var.set("Waiting for other workstations…")
        self._refresh_status_pill()

    def _manual_peer_ip_list(self) -> list[str]:
        raw = self.config_store.data.get("manual_peer_ips", "")
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    def _load_config_into_form(self) -> None:
        data = self.config_store.data
        raw_label = str(data.get("label", "") or "").strip()
        # Fresh-install or otherwise-empty config: show the placeholder prompt
        # in the dropdown instead of silently defaulting to "Room 1". Treat a
        # config that somehow contains the prompt string (downgrade, corrupted
        # save) as unset too.
        if not raw_label or raw_label == STATION_NAME_PROMPT:
            self.label_var.set(STATION_NAME_PROMPT)
            committed_label = ""
        else:
            self.label_var.set(raw_label)
            committed_label = raw_label
        self.port_var.set(str(data.get("server_port", DEFAULT_PORT)))
        theme_name = data.get("theme", DEFAULT_THEME)
        if theme_name not in THEMES:
            self.config_store.data["theme"] = DEFAULT_THEME
            self.config_store.save()
        saved_sound = str(data.get("alert_sound", "") or "").strip()
        default_sound = DEFAULT_STATION_SOUNDS.get(committed_label, ALERT_SOUND_OPTIONS[0])
        if saved_sound and saved_sound in ALERT_SOUND_OPTIONS:
            self.alert_sound_var.set(saved_sound)
        else:
            self.alert_sound_var.set(default_sound)
        self.alert_volume_var.set(int(data.get("alert_volume", 50)))
        if self.label_combo is not None:
            self.label_combo["values"] = list(self._effective_station_labels())
        self._target_signature = None
        self._last_committed_label = committed_label
        self._refresh_target_checkboxes(list(self.online_labels))
        self._apply_default_target_selection(force=True)
        self._apply_theme(data.get("theme", DEFAULT_THEME))
        # _apply_theme repaints the Ready button accent; re-apply the disabled
        # state afterwards so a fresh install lands with Ready visibly off.
        self._refresh_station_name_warning()
        self._set_ready_button_enabled(self._has_real_station_label())

    def _write_form_to_data(self) -> None:
        """Copy current UI values into config_store.data (does not write to disk)."""
        raw_label = self.label_var.get().strip()
        # The placeholder is a UI affordance, not a real station name -- never
        # let _persist_form (called on alert-volume changes, theme changes, etc.)
        # bake it into the saved config.
        self.config_store.data["label"] = "" if raw_label == STATION_NAME_PROMPT else raw_label
        try:
            self.config_store.data["server_port"] = int(self.port_var.get().strip() or DEFAULT_PORT)
        except ValueError:
            self.config_store.data["server_port"] = DEFAULT_PORT
        self.config_store.data["alert_sound"] = self.alert_sound_var.get()
        self.config_store.data["alert_volume"] = int(self.alert_volume_var.get())

    def _persist_form(self) -> None:
        self._write_form_to_data()
        self.config_store.save()

    def _auto_start_network(self) -> None:
        if self.auto_start_done:
            return
        self.auto_start_done = True
        if not self._has_real_station_label():
            # Fresh install: the user hasn't picked a station name yet. Don't
            # start the LAN beacon -- a blank or placeholder beacon would race
            # with the existing real stations on the LAN. start_network is
            # triggered later from _commit_station_label_if_changed once the
            # user picks a real name from the dropdown.
            return
        self.start_network()

    def _open_network_settings_window(self) -> None:
        t = self._current_theme
        win = tk.Toplevel(self.root)
        win.title("Network Settings")
        win.geometry(f"{_dz(420)}x{_dz(260)}")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.configure(bg=t["bg"])

        frame = tk.Frame(win, bg=t["bg"], padx=_dz(12), pady=_dz(12))
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text=f"Detected LAN IP: {self.local_ip}",
                 bg=t["bg"], fg=t["sub"], font=_ui_font(9)).pack(anchor="w", pady=(0, _dz(10)))

        port_var = tk.StringVar(value=self.port_var.get())
        manual_var = tk.StringVar(value=str(self.config_store.data.get("manual_peer_ips", "")))

        tk.Label(frame, text="TCP port (same on every computer):",
                 bg=t["bg"], fg=t["text"], font=_ui_font(9)).pack(anchor="w", pady=(0, _dz(2)))
        ttk.Entry(frame, textvariable=port_var).pack(fill="x", pady=(0, _dz(10)))
        tk.Label(
            frame,
            text="Optional: extra peer IP addresses (comma-separated) if LAN discovery is blocked:",
            bg=t["bg"], fg=t["sub"], font=_ui_font(9),
            wraplength=_dz(380), justify="left",
        ).pack(anchor="w", pady=(0, _dz(2)))
        ttk.Entry(frame, textvariable=manual_var).pack(fill="x", pady=(0, _dz(8)))

        def save_settings() -> None:
            try:
                port = int(port_var.get().strip())
                if port < 1 or port > 65535:
                    raise ValueError()
            except ValueError:
                messagebox.showerror(APP_TITLE, "Port must be a number between 1 and 65535.")
                return
            self.port_var.set(str(port))
            self.config_store.data["manual_peer_ips"] = manual_var.get().strip()
            self.config_store.data["server_port"] = port
            self.config_store.save()
            win.destroy()
            if self._network_running:
                self.stop_network()
                self.start_network()

        buttons = tk.Frame(frame, bg=t["bg"])
        buttons.pack(fill="x", pady=(_dz(12), 0))
        RoundedButton(buttons, text="Save", command=save_settings,
                      bg=t["accent"], fg=t["accent_text"]).pack(side="right")
        RoundedButton(buttons, text="Cancel", command=win.destroy,
                      bg=t["card_bg"], fg=t["text"]).pack(side="right", padx=(0, _dz(8)))

    # ------------------------------------------------------------------
    # LAN health self-check — surfaces the "Windows Firewall has disabled
    # this app's Private-profile rules" failure mode that doesn't show up
    # until two workstations try and fail to discover each other.
    # ------------------------------------------------------------------
    def _schedule_lan_health_check(self) -> None:
        # The failure mode this catches — Windows Firewall leaving the app's
        # Private-profile rules disabled after MSIX install — is Windows-only.
        # macOS discovery doesn't have an equivalent silent block.
        if sys.platform != "win32":
            return
        self._cancel_lan_health_check()
        # 25s gives discovery a full beacon round-trip plus margin; healthy
        # LANs see peers within ~5s, so an empty snapshot at 25s is signal,
        # not noise.
        self._lan_health_check_id = self.root.after(25000, self._check_lan_health)

    def _cancel_lan_health_check(self) -> None:
        if self._lan_health_check_id is None:
            return
        try:
            self.root.after_cancel(self._lan_health_check_id)
        except Exception:
            pass
        self._lan_health_check_id = None

    def _check_lan_health(self) -> None:
        # Recurring (not one-shot): re-arm every cycle so an outage that begins
        # MID-SESSION -- a daytime firewall-policy push that disables the
        # Private-profile rules, a switch that starts dropping broadcasts -- is
        # surfaced too, not only an empty LAN at the 25s startup mark. The
        # earlier one-shot only ever fired once per start_network. The dialog
        # de-dupes itself, and the discovery branch of the queue closes it once
        # a peer beacon arrives, so re-checking is cheap and non-spammy.
        self._lan_health_check_id = None
        try:
            if not self._network_running:
                return
            if self.discovery and self.discovery.snapshot():
                return
            if self._lan_health_dialog is not None:
                try:
                    if self._lan_health_dialog.winfo_exists():
                        return
                except tk.TclError:
                    pass
                self._lan_health_dialog = None
            self._open_lan_health_help_dialog()
        finally:
            # Keep watching while the network is up (Windows-only, like the
            # scheduler). Cancelled by stop_network -> _cancel_lan_health_check.
            if (
                self._network_running
                and not self._quitting
                and sys.platform == "win32"
            ):
                try:
                    self._lan_health_check_id = self.root.after(25000, self._check_lan_health)
                except tk.TclError:
                    pass

    def _open_lan_health_help_dialog(self) -> None:
        t = self._current_theme
        win = tk.Toplevel(self.root)
        win.title(f"{APP_TITLE} — Cannot see other workstations")
        win.geometry(f"{_dz(600)}x{_dz(540)}")
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(bg=t["bg"])
        win.protocol("WM_DELETE_WINDOW", self._close_lan_health_help_dialog)
        self._lan_health_dialog = win

        frame = tk.Frame(win, bg=t["bg"], padx=_dz(14), pady=_dz(14))
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="No other workstations have responded after 25 seconds.",
            bg=t["bg"], fg=t["text"], font=_ui_font(11, "bold"),
            wraplength=_dz(560), justify="left",
        ).pack(anchor="w", pady=(0, _dz(8)))

        tk.Label(
            frame,
            text=(
                "If another computer on the same office network is running "
                "Chairside Ready Alert, the most likely cause is Windows "
                "Firewall blocking inbound traffic to this app on the current "
                "network profile. To fix it, follow the three steps below on "
                "this computer."
            ),
            bg=t["bg"], fg=t["sub"], font=_ui_font(10),
            wraplength=_dz(560), justify="left",
        ).pack(anchor="w", pady=(0, _dz(12)))

        tk.Label(
            frame,
            text=(
                "1.  Open the Start menu, type  PowerShell ,  right-click "
                '"Windows PowerShell",  and choose  "Run as administrator".'
            ),
            bg=t["bg"], fg=t["text"], font=_ui_font(10),
            wraplength=_dz(560), justify="left",
        ).pack(anchor="w", pady=(0, _dz(6)))

        tk.Label(
            frame,
            text="2.  Click  Copy command  below,  then paste it into PowerShell  (right-click in the window),  and press Enter:",
            bg=t["bg"], fg=t["text"], font=_ui_font(10),
            wraplength=_dz(560), justify="left",
        ).pack(anchor="w", pady=(0, _dz(4)))

        fix_cmd = (
            "Get-NetFirewallRule | Where-Object {\n"
            "    ($_.DisplayName -like '*Chairside*' -or\n"
            "     $_.DisplayName -like '*chairsidereadyalert*') -and\n"
            "    $_.Profile -match 'Private'\n"
            "} | Enable-NetFirewallRule"
        )

        text_box = tk.Text(
            frame, height=6, font=_ui_font(9), wrap="none",
            bg=t["card_bg"], fg=t["text"], relief="solid", bd=1, padx=_dz(8), pady=_dz(6),
        )
        text_box.insert("1.0", fix_cmd)
        text_box.configure(state="disabled")
        text_box.pack(fill="x", pady=(0, _dz(10)))

        tk.Label(
            frame,
            text=(
                "3.  Close PowerShell.  This window will close on its own as "
                "soon as another workstation appears — usually within a few "
                "seconds.  If it doesn't, the other computer may need the "
                "same fix, or it may not be running the app yet."
            ),
            bg=t["bg"], fg=t["text"], font=_ui_font(10),
            wraplength=_dz(560), justify="left",
        ).pack(anchor="w", pady=(0, _dz(14)))

        def copy_command() -> None:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(fix_cmd)
                self.root.update()
                self._append_diag("Firewall fix command copied to clipboard.")
            except tk.TclError as exc:
                self._append_diag(f"Could not copy to clipboard: {exc}")

        buttons = tk.Frame(frame, bg=t["bg"])
        buttons.pack(fill="x", pady=(_dz(4), 0))
        RoundedButton(
            buttons, text="Close", command=self._close_lan_health_help_dialog,
            bg=t["card_bg"], fg=t["text"],
        ).pack(side="right")
        RoundedButton(
            buttons, text="Copy command", command=copy_command,
            bg=t["accent"], fg=t["accent_text"],
        ).pack(side="right", padx=(0, _dz(8)))

    def _close_lan_health_help_dialog(self) -> None:
        if self._lan_health_dialog is None:
            return
        try:
            self._lan_health_dialog.destroy()
        except tk.TclError:
            pass
        self._lan_health_dialog = None

    def _on_station_label_selected(self, _event=None) -> None:
        self._commit_station_label_if_changed()

    def _fit_window_to_content(self, initial: bool = True) -> None:
        """Size the window from what the layout actually needs (header, Ready
        log showing the newest card + 3 rows, the bottom panel), capped to
        the usable screen. The minimum height keeps those 4 log lines
        visible unless the screen itself is shorter. Runs once at startup
        and again after the first draw (a few widgets settle late); the
        second pass only raises the minimum / grows a too-short window."""
        try:
            self.root.update_idletasks()
            need_w = self.root.winfo_reqwidth()
            need_h = self.root.winfo_reqheight()
            cur_h = self.root.winfo_height()
        except tk.TclError:
            return
        avail_w, avail_h = _usable_screen_size(self.root)
        min_h = max(_dz(300), min(need_h, avail_h))
        self.root.minsize(min(max(_dz(640), need_w), avail_w), min_h)
        if initial:
            h = min(need_h + _px(30), avail_h)
            w = min(max(_dz(820), need_w), avail_w)
            self.root.geometry(f"{w}x{h}")
            self.root.after(400, lambda: self._fit_window_to_content(initial=False))
        elif cur_h < min_h:
            self.root.geometry(f"{self.root.winfo_width()}x{min(need_h + _px(30), avail_h)}")

    def _prompt_station_setup_if_needed(self) -> None:
        """Fresh install: ask for this station's name once the main window is
        on screen."""
        if self._has_real_station_label():
            return
        try:
            if self.root.state() == "withdrawn":
                self.root.after(500, self._prompt_station_setup_if_needed)
                return
        except tk.TclError:
            return
        self._open_station_name_dialog()

    def _open_station_name_dialog(self) -> None:
        """Pick THIS computer's station name. Reached from the header Edit link
        or Settings -> This Station's Name; prompted automatically on first
        launch. Renaming warns first, since the usual mistake is picking a
        room here when the user meant to send that room a Ready."""
        existing = getattr(self, "_station_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_set()
                    return
            except tk.TclError:
                pass
        t = self._current_theme
        first_time = not self._has_real_station_label()
        current = "" if first_time else self.label_var.get().strip()
        warn_fg = "#f87171" if _is_dark(t["bg"]) else "#b45309"
        win = tk.Toplevel(self.root)
        win.title("This Station's Name")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg=t["bg"])
        self._station_dialog = win
        frame = tk.Frame(win, bg=t["bg"], padx=_dz(20), pady=_dz(16))
        frame.pack(fill="both", expand=True)
        wrap = _dz(420)
        if first_time:
            heading = "Welcome! Which room is this computer in?"
            body = ("Choose the name of THIS computer. Other stations will see this name "
                    "when you send them a Ready alert.")
        else:
            heading = "Change this station's name?"
            body = f"This computer is currently named \"{current}\"."
        tk.Label(frame, text=heading, font=_ui_font(13, "bold"), bg=t["bg"], fg=t["title"],
                 bd=0, wraplength=wrap, justify="left").pack(anchor="w")
        tk.Label(frame, text=body, wraplength=wrap, justify="left", font=_ui_font(10),
                 bg=t["bg"], fg=t["text"], bd=0).pack(anchor="w", pady=(_dz(6), 0))
        if not first_time:
            tk.Label(frame, wraplength=wrap, justify="left", font=_ui_font(10, "bold"), bg=t["bg"],
                     fg=warn_fg, bd=0,
                     text=("You don't need to change this to send a Ready. To alert another room, "
                           "pick it under \"Send to\" on the main screen. Only change this if you "
                           "really want to rename this computer.")).pack(anchor="w", pady=(_dz(8), 0))
        pick_var = tk.StringVar(value=current)
        # Editable: pick an existing name or type a new one. This dialog is
        # the only place the name can change -- the main window has no picker.
        combo = ttk.Combobox(frame, textvariable=pick_var, values=list(self._effective_station_labels()),
                             state="normal", width=34)
        combo.pack(fill="x", pady=(_dz(12), 0))
        self.label_combo = combo
        tk.Label(frame, text="Pick a name from the list, or type a new one.", font=_ui_font(9),
                 bg=t["bg"], fg=t["sub"], bd=0).pack(anchor="w", pady=(_dz(6), 0))

        def close() -> None:
            self.label_combo = None
            self._station_dialog = None
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()

        def save() -> None:
            new = pick_var.get().strip()
            if not new or new == STATION_NAME_PROMPT:
                messagebox.showinfo(APP_TITLE, "Pick or type a name for this computer.", parent=win)
                return
            if len(new) > 40:
                messagebox.showinfo(APP_TITLE, "Please keep the station name to 40 characters or fewer.",
                                    parent=win)
                return
            if new == current:
                close()
                return
            if current and not messagebox.askyesno(
                APP_TITLE,
                f"Rename this computer from \"{current}\" to \"{new}\"?\n\n"
                f"Other stations will see Ready alerts from this computer as \"{new}\".",
                icon="warning", parent=win,
            ):
                return
            close()
            labels = self._effective_station_labels()
            if new not in labels:
                # A typed name joins the shared list, same as Default ->
                # Add or Remove Computers, so every station can send to it.
                removed = self._removed_station_labels()
                if new in removed:
                    self._set_removed_labels([r for r in removed if r != new])
                self._set_station_labels(labels + [new])
                self._broadcast_label_event("add", new)
            self.label_var.set(new)
            self._commit_station_label_if_changed()

        buttons = tk.Frame(frame, bg=t["bg"])
        buttons.pack(fill="x", pady=(_dz(16), 0))
        RoundedButton(buttons, text="Save", command=save,
                      bg=t["accent"], fg=t["accent_text"]).pack(side="right")
        RoundedButton(buttons, text="Cancel", command=close,
                      bg=t["card_bg"], fg=t["text"]).pack(side="right", padx=(0, _dz(8)))
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        win.update_idletasks()
        try:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
            win.grab_set()
        except tk.TclError:
            pass
        combo.focus_set()

    def _commit_station_label_if_changed(self) -> None:
        # Combobox is readonly — the value is always one of the dropdown entries
        # already in station_labels. Nothing to validate or auto-add.
        new = self.label_var.get().strip()
        # Defensive: ignore an empty value or the placeholder ever landing here.
        # Picking from the dropdown can only yield a real label, but a future
        # programmatic set() could in theory pass the placeholder through.
        if not new or new == STATION_NAME_PROMPT:
            return
        if new == self._last_committed_label:
            return
        was_unset = not self._last_committed_label
        self._last_committed_label = new
        self.config_store.data["label"] = new
        if new in DEFAULT_STATION_SOUNDS and not str(
            self.config_store.data.get("alert_sound", "") or ""
        ).strip():
            self.alert_sound_var.set(DEFAULT_STATION_SOUNDS[new])
        self._persist_form()
        self._target_signature = None
        self._refresh_target_checkboxes(list(self.online_labels))
        self._apply_default_target_selection(force=True)
        # A real name is now selected — flip the Ready button on and clear the
        # red "you need to choose a station" prompt under the dropdown.
        self._set_ready_button_enabled(True)
        if self._network_running:
            self.stop_network()
            self.start_network()
        elif was_unset:
            # Fresh-install path: _auto_start_network deferred startup until a
            # station name existed. Now that one does, kick off the LAN.
            self.start_network()
        else:
            self._set_duplicate_name_warning(False)
        self._refresh_station_name_warning()

    def _has_real_station_label(self) -> bool:
        """True once the user has chosen an actual station name from the dropdown.
        Fresh-install state (label unset, or still showing STATION_NAME_PROMPT)
        returns False. Used to gate network startup and Ready-button activation
        so a brand-new install can't broadcast a colliding "Room 1" beacon."""
        if not hasattr(self, "label_var"):
            return False
        raw = self.label_var.get().strip()
        return bool(raw) and raw != STATION_NAME_PROMPT

    def _refresh_station_name_warning(self) -> None:
        """Drive the warning line under "This station: X" in the header. Station-unset
        warning (red) takes priority over duplicate-name warning (amber). Called
        from _set_duplicate_name_warning, _apply_theme, _load_config_into_form,
        and _commit_station_label_if_changed."""
        if not hasattr(self, "duplicate_name_var"):
            return
        t = self._current_theme
        if not self._has_real_station_label():
            self.duplicate_name_var.set("Click \"Set up\" to name this station before sending Ready alerts")
            color = "#f87171" if _is_dark(t["bg"]) else "#dc2626"
        elif self._duplicate_name_detected:
            self.duplicate_name_var.set("Another computer may be using the same station name.")
            color = "#b45309"
        else:
            self.duplicate_name_var.set("")
            color = t["sub"]
        if hasattr(self, "duplicate_name_label"):
            self.duplicate_name_label.configure(fg=color, bg=t["bg"])
            if self.duplicate_name_var.get():
                self.duplicate_name_label.pack(anchor="e")
            else:
                self.duplicate_name_label.pack_forget()

    def _set_ready_button_enabled(self, enabled: bool) -> None:
        """Visually + functionally disable the Ready button until the user picks
        a station. send_ready_selected / send_ready_all also re-check the gate,
        so this is defense-in-depth: a stray click never sends a Ready alert
        from a station whose name is still the placeholder."""
        self._ready_button_enabled = bool(enabled)
        btn = getattr(self, "_ready_btn_widget", None)
        if btn is None:
            return
        btn.set_theme(self._current_theme)
        btn.set_enabled(enabled)

    def _set_duplicate_name_warning(self, is_duplicate: bool) -> None:
        self._duplicate_name_detected = bool(is_duplicate)
        self._refresh_station_name_warning()

    def _refresh_duplicate_name_warning(self, snap: dict[str, dict], now: float) -> None:
        if not self._network_running:
            self._set_duplicate_name_warning(False)
            return
        me = self.label_var.get().strip()
        if not me:
            self._set_duplicate_name_warning(False)
            return
        duplicate_seen = False
        for _ip, info in snap.items():
            if now - float(info.get("last_seen", 0)) > PEER_STALE_SEC:
                continue
            if str(info.get("label", "")).strip() == me:
                duplicate_seen = True
                break
        self._set_duplicate_name_warning(duplicate_seen)

    def _apply_default_target_selection(self, force: bool = False) -> None:
        if self.config_store.data.get("default_targets"):
            return
        my_label = self.label_var.get().strip()
        defaults = DEFAULT_TARGET_SELECTIONS.get(my_label, [])
        if not defaults:
            return
        if not force:
            any_selected = any(var.get() for var in self.target_vars.values())
            if any_selected:
                return
        for target_label, var in self.target_vars.items():
            var.set(target_label in defaults)

    def _refresh_target_checkboxes(self, labels) -> None:
        """Rebuild the Send to station chips (one StationChip per online
        station other than this one, each bound to a BooleanVar in
        self.target_vars). Selection survives rebuilds."""
        labels = sorted({str(x).strip() for x in labels if str(x).strip()})
        old_states = {label: var.get() for label, var in self.target_vars.items()}
        me = self.label_var.get().strip()
        signature = (me, tuple(labels))
        if signature == self._target_signature:
            return
        self._target_signature = signature
        for child in self.targets_frame.winfo_children():
            child.destroy()
        self._target_chips = []
        self._chip_layout_key = None
        self.target_vars.clear()
        filtered = [label for label in labels if label != me]
        default_targets = set(self.config_store.data.get("default_targets", []))
        theme = self._current_theme
        self._refresh_status_pill()
        if not filtered:
            self.targets_frame.configure(height=self._send_to_area_height())
            tk.Label(
                self.targets_frame,
                text="No other stations online yet. Names appear here after this computer sees them on the LAN.",
                wraplength=_px(520), bg=theme["card_bg"], fg=theme["sub"], font=_ui_font(10),
                justify="left",
            ).place(x=0, rely=0.5, anchor="w")
            self._rebuild_default_menu()
            return
        for label in filtered:
            checked = (label in default_targets) or old_states.get(label, False)
            var = tk.BooleanVar(value=checked)
            self.target_vars[label] = var
            self._target_chips.append(StationChip(self.targets_frame, label, var, theme))
        self._layout_target_chips(force=True)
        self._rebuild_default_menu()

    def _layout_target_chips(self, force: bool = False) -> None:
        """Centered, wrapping rows of Send to chips, reflowed whenever the
        card's width changes. Rows are balanced (5 chips -> 3 + 2, not
        4 + 1) by finding the narrowest width that still needs the same
        number of rows. Chips are place()d, so the frame height is set here."""
        chips = [c for c in self._target_chips if c.winfo_exists()]
        if not chips:
            return
        width = self.targets_frame.winfo_width()
        if width <= 1:  # not laid out yet; <Configure> reflows with the real width
            width = _px(600)
        key = (width, tuple(str(c) for c in chips))
        if not force and key == self._chip_layout_key:
            return
        self._chip_layout_key = key
        gap, vgap = _px(6), _px(4)
        sizes = [(c.winfo_reqwidth(), c.winfo_reqheight()) for c in chips]

        def flow(limit: int) -> list[list[int]]:
            rows: list[list[int]] = [[]]
            used = 0
            for i, (w, _h) in enumerate(sizes):
                need = w if not rows[-1] else used + gap + w
                if rows[-1] and need > limit:
                    rows.append([i])
                    used = w
                else:
                    rows[-1].append(i)
                    used = need
            return rows

        rows = flow(width)
        lo = max(w for w, _h in sizes)
        hi = width
        while lo < hi:  # narrowest limit that keeps the same row count
            mid = (lo + hi) // 2
            if len(flow(mid)) <= len(rows):
                hi = mid
            else:
                lo = mid + 1
        rows = flow(hi)
        heights = [max(sizes[i][1] for i in row) for row in rows]
        content_h = sum(heights) + vgap * (len(rows) - 1)
        # Constant box: one row sits centered; a second row pushes the first
        # up. Only a third row (very narrow window, many stations) grows it.
        area_h = max(self._send_to_area_height(), content_h)
        y = (area_h - content_h) // 2
        for row in rows:
            row_w = sum(sizes[i][0] for i in row) + gap * (len(row) - 1)
            row_h = max(sizes[i][1] for i in row)
            x = max(0, (width - row_w) // 2)
            for i in row:
                chips[i].place(x=x, y=y + (row_h - sizes[i][1]) // 2)
                x += sizes[i][0] + gap
            y += row_h + vgap
        if int(self.targets_frame.cget("height")) != area_h:
            self.targets_frame.configure(height=area_h)

    @staticmethod
    def _send_to_area_height() -> int:
        """Fixed height of the Send to chip area: two rows of StationChips."""
        return 2 * StationChip.height() + _px(4)

    def _trim_log_to_max_entries(self) -> None:
        """Cap the visible log at MAX_VISIBLE_LOG_ENTRIES (oldest dropped).
        Persistence (ready_log.txt) is NOT trimmed -- the on-disk record
        stays complete for the day."""
        self.log.trim(MAX_VISIBLE_LOG_ENTRIES)

    def _append_diag(self, text: str) -> None:
        line = f"[{now_str()}] {text}"
        self._diag_lines.append(line)
        if self._diag_text is not None and self._diag_text.winfo_exists():
            self._diag_text.configure(state="normal")
            self._diag_text.insert("end", line + "\n")
            self._diag_text.see("end")
            self._diag_text.configure(state="disabled")

    def _append_ready_log(self, text: str, persist: bool = True) -> None:
        """Outgoing Ready log path ("Sent → Room 2, Doctor\t[10:45 AM]").
        Shown as a small muted line inserted ABOVE the two newest incoming
        Readys (ReadyLog._insert_outgoing), which stay pinned to the bottom of
        the log and visually dominant -- at the cost of putting outgoing
        entries momentarily out of chronological order until another
        incoming Ready arrives.

        persist=False is used by _load_ready_log when replaying entries on
        startup so we don't re-write to disk on every restored line."""
        body, ts = _split_log_line(text)
        if body.startswith("Sent → "):
            body = body[len("Sent → "):]
        self.log.add_outgoing(body, ts, animate=persist)
        self._trim_log_to_max_entries()
        if persist:
            self._persist_ready_log_line(text, direction="OUT")

    # ---------- Persistent Ready log (survives restart, resets daily) ----------
    #
    # On-disk format is a tab-separated text file at <user_data_dir>/ready_log.txt:
    #
    #     DATE:2026-05-16
    #     IN<TAB>Room 1: Ready  [02:30 PM]
    #     IN<TAB>Room 2: Ready  [02:35 PM]
    #     OUT<TAB>Sent → All  [02:40 PM]
    #
    # Before 1.0.62 the timestamp appeared at the start of each line wrapped
    # in brackets; it now sits at the end so the eye reads "what happened"
    # before "when." Lines persisted in the old layout still load cleanly
    # — _split_log_line canonicalizes every historical layout on load.
    #
    # The direction prefix (IN / OUT) lets _load_ready_log replay each line
    # through the same code path that handled it live: incoming lines go through
    # _append_incoming_ready_log (newest incoming becomes the big card), and
    # outgoing lines go through _append_ready_log (inserted above the two
    # newest incoming Readys). The net effect is that the log immediately
    # after startup matches the log immediately before shutdown.
    #
    # Legacy v1.0.38 files (no prefix) are restored as plain text — best we
    # can do without direction info. New entries from v1.0.39 onward use the
    # tab-prefixed format.
    #
    # On launch, the file is read: if the header date matches today's local
    # date, the body lines are restored. If the date differs, the file is
    # removed and the log starts empty. While running, a 60-second timer
    # detects when the local date rolls over (midnight or DST adjustment) and
    # clears both the widget and the file. Works identically on macOS and
    # Windows because it uses only the standard library plus _user_data_dir().

    def _ready_log_path(self) -> str:
        return os.path.join(_user_data_dir(), "ready_log.txt")

    def _persist_ready_log_line(self, text: str, direction: str = "OUT") -> None:
        """Append one log line to ready_log.txt with an IN/OUT prefix, writing
        a fresh DATE header first if the previous file is from an earlier day
        (or doesn't exist). Best-effort: filesystem errors are swallowed."""
        path = self._ready_log_path()
        today = _today_iso()
        line = text.rstrip()
        if not line:
            return
        if direction not in ("IN", "OUT"):
            direction = "OUT"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            existing_date = self._read_ready_log_date(path)
            if existing_date != today:
                # Rewrite with today's header (truncates any stale content).
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"DATE:{today}\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{direction}\t{line}\n")
        except OSError:
            pass

    @staticmethod
    def _read_ready_log_date(path: str) -> Optional[str]:
        """Return the YYYY-MM-DD value from the file's DATE: header, or None
        if the file is missing or malformed."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline()
        except OSError:
            return None
        if not first.startswith("DATE:"):
            return None
        value = first[len("DATE:"):].strip()
        return value or None

    def _load_ready_log(self) -> None:
        """Restore today's persisted Ready log into the Text widget on launch.
        Called once after the UI is built. Stale (older-day) logs are
        deleted so the new session starts clean.

        Each persisted line carries an IN/OUT prefix (since v1.0.39). Replay
        runs them through the same appenders as the live path with animate=
        False / persist=False, so the log after startup matches the log at
        shutdown -- same order, same newest incoming Ready in the big card.
        Lines from a v1.0.38 file (no prefix) restore as plain rows.
        """
        path = self._ready_log_path()
        today = _today_iso()
        self._last_log_date = today  # baseline for the rollover check
        existing_date = self._read_ready_log_date(path)
        if existing_date is None:
            return
        if existing_date != today:
            # Yesterday (or older) — drop it.
            try:
                os.remove(path)
            except OSError:
                pass
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return
        if len(lines) < 2:
            return  # Just a header, no body.
        for line in lines[1:]:
            if not line:
                continue
            if line.startswith("IN\t"):
                # Incoming — replay through the emphasis path with no flash
                # animation and no re-persist (we're loading from disk).
                # _migrate_old_log_line is a no-op for new-format lines and
                # reorders pre-1.0.62 '[ts] body' entries into 'body  [ts]'
                # so the timestamp displays on the right consistently even
                # for today's data persisted before the upgrade.
                self._append_incoming_ready_log(
                    _migrate_old_log_line(line[len("IN\t"):]),
                    animate=False, persist=False,
                )
            elif line.startswith("OUT\t"):
                # Outgoing — replay through the same insert-above-emphasis
                # path that the live send uses, but skip re-persisting.
                # 1.0.78 rename: outgoing entries now read "Sent → ..."
                # instead of "Ready → ...". Rewrite the leading prefix on
                # load so today's pre-rename persisted lines look consistent
                # with new ones until the midnight rollover deletes the file.
                migrated = _migrate_old_log_line(line[len("OUT\t"):])
                if migrated.startswith("Ready → "):
                    migrated = "Sent → " + migrated[len("Ready → "):]
                self._append_ready_log(migrated, persist=False)
            else:
                # Legacy v1.0.38 row (no prefix) -- best-effort plain row.
                body, ts = _split_log_line(line)
                self.log.add_raw(f"{body}  {ts}".strip())
        self._trim_log_to_max_entries()

    def _check_log_rollover(self) -> None:
        """Periodic (every 60s) check that the in-memory log + persistent file
        get wiped clean when the local calendar date changes. Handles both
        'app left running across midnight' and DST adjustments."""
        try:
            today = _today_iso()
            last = getattr(self, "_last_log_date", today)
            if today != last:
                # New day — clear the log.
                self.log.clear()
                # Wipe the persistent file — next append will rewrite the header.
                try:
                    os.remove(self._ready_log_path())
                except OSError:
                    pass
                self._last_log_date = today
        except Exception:
            pass  # Never let a clock glitch crash the UI loop.
        finally:
            try:
                self.root.after(60_000, self._check_log_rollover)
            except tk.TclError:
                pass
    # ---------- end persistent ready log ----------

    def _append_incoming_ready_log(
        self, text: str, animate: bool = True, persist: bool = True,
    ) -> None:
        """Append an INCOMING Ready ("Room 3: Ready\t[10:47 AM]"). It becomes
        the big highlighted card at the bottom of the log; the previous
        newest drops to the bold row just above it, and older ones to plain
        rows.

        animate=True (default) plays the arrival animation (card slides up,
        pulse ring, highlight fade). animate=False is used by _load_ready_log
        to restore the previous session without animating every line.

        persist=True (default) writes the line to ready_log.txt with an IN
        prefix. persist=False suppresses the write; used during restore so we
        don't re-write what we just read."""
        body, ts = _split_log_line(text)
        sender = body[:-len(": Ready")] if body.endswith(": Ready") else body
        self.log.add_incoming(sender, ts, animate=animate)
        self._trim_log_to_max_entries()
        if persist:
            self._persist_ready_log_line(text, direction="IN")

    def _open_connection_log_window(self) -> None:
        if self._diag_win is not None and self._diag_win.winfo_exists():
            self._diag_win.lift()
            self._diag_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("Connection log")
        win.geometry(f"{_dz(640)}x{_dz(320)}")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=_dz(8))
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Listeners, discovery, and TCP status (not shown on the main screen).",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, _dz(6)))
        text = tk.Text(frame, height=14, wrap="word", font=_ui_font(9))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for line in self._diag_lines:
            text.insert("end", line + "\n")
        text.see("end")
        text.configure(state="disabled")

        def on_close() -> None:
            self._diag_win = None
            self._diag_text = None
            win.destroy()

        self._diag_win = win
        self._diag_text = text
        win.protocol("WM_DELETE_WINDOW", on_close)

    def _cancel_sync_loop(self) -> None:
        if self._sync_after_id is not None:
            try:
                self.root.after_cancel(self._sync_after_id)
            except Exception:
                pass
            self._sync_after_id = None

    def _schedule_sync_loop(self) -> None:
        self._cancel_sync_loop()
        self._sync_after_id = self.root.after(1500, self._sync_peer_clients_tick)

    def _sync_peer_clients_tick(self) -> None:
        # This 1.5s tick is the heart of weekend self-healing: it detects the
        # sleep gap, posts system_wake, prunes stale peers, and rebuilds dead
        # sockets. The pre-hardening version put its re-arm (_schedule_sync_loop)
        # as the LAST unguarded statement, so a single transient exception
        # anywhere above it (a TclError during a theme rebuild, an OSError from
        # IP resolution on a NIC that just dropped on wake) left the loop
        # un-rescheduled -- and the very recovery code meant to survive
        # sleep/wake silently stopped running. Now the body runs under a guard
        # and the re-arm lives in a finally, so one bad tick skips work instead
        # of ending the loop.
        self._sync_after_id = None
        if not self._network_running or not self.discovery:
            return
        try:
            self._run_sync_tick_body()
        except Exception as exc:
            self._diag_lines.append(f"[{now_str()}] sync tick body failed: {exc}")
        finally:
            # Re-arm only while the network is still up (stop_network cancels
            # the loop) and we're not quitting, so the finally can't resurrect
            # a deliberately-stopped loop.
            if self._network_running and not self._quitting:
                self._schedule_sync_loop()

    def _run_sync_tick_body(self) -> None:
        # --- Windows sleep/wake recovery (dependency-free) -------------------
        # root.after timers don't fire while the machine is asleep, so a tick
        # arriving far later than its 1.5s cadence means we almost certainly
        # slept/hibernated. On wake our outbound peer_clients are stale
        # half-open sockets that would silently swallow Ready alerts, so bounce
        # the LAN to rebuild fresh server + clients. We reuse the existing macOS
        # wake path: post ('system_wake', None) and let the UI-thread queue
        # drain call _handle_system_wake (honoring the queue-only threading
        # rule). _last_tick_monotonic is seeded in start_network, so the very
        # first tick after Start can't read as a wake event. (Re-arm is handled
        # by the caller's finally; this early return still triggers it.)
        now_mono = time.monotonic()
        if self._last_tick_monotonic is not None and (
            now_mono - self._last_tick_monotonic > WAKE_GAP_SEC
        ):
            gap = now_mono - self._last_tick_monotonic
            self._last_tick_monotonic = now_mono
            self._append_diag(
                f"Sync tick gap {gap:.0f}s -- likely woke from sleep; "
                "bouncing LAN to rebuild any sleep-broken sockets."
            )
            self.queue.put(("system_wake", None))
            return
        self._last_tick_monotonic = now_mono

        # Refresh our own IPs occasionally, on a background thread (NOT the
        # blocking per-tick getaddrinfo this used to do on the UI thread), so an
        # overnight DHCP lease change is still reflected without a full restart.
        self._maybe_refresh_local_ips()

        # Defense-in-depth: if the discovery listen thread silently died (it
        # shouldn't -- its loop now self-heals -- but a never-anticipated
        # exception could), restart discovery so beacons flow again without
        # waiting for a full LAN bounce.
        if not self.discovery.is_listen_alive():
            self._append_diag("Discovery listener was not alive -- restarting discovery.")
            self._restart_discovery()
            return

        self.discovery.prune_stale()
        self.discovery.update_label(self.label_var.get().strip())
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            port = DEFAULT_PORT
        self.discovery.update_tcp_port(port)

        desired: set[str] = {"127.0.0.1"}
        now = time.time()
        snap = self.discovery.snapshot()
        for ip, info in snap.items():
            if now - info["last_seen"] <= PEER_STALE_SEC:
                desired.add(ip)
        for ip in self._manual_peer_ip_list():
            if ip and ip not in self.local_ips:
                desired.add(ip)

        for ip in list(self.peer_clients.keys()):
            if ip not in desired:
                self.peer_clients[ip].disconnect()
                del self.peer_clients[ip]

        # Evict a dead-but-present client whose peer IP never left discovery --
        # the structural reason the overnight half-open socket used to be
        # immortal (the create branch below only fires "if ip not in
        # peer_clients"). Act only when the socket is still present yet has heard
        # nothing for CLIENT_STALE_SEC despite the ~5s heartbeat: that means
        # keepalive AND heartbeat both failed to surface the death, so force a
        # rebuild. A client already reconnecting (sock is None) is left alone --
        # its own _run loop is on it. The 127.0.0.1 loopback is EXEMPT: it
        # legitimately receives nothing for hours, physically cannot half-open,
        # and churning it would open a gap in our own receive path.
        for ip in list(self.peer_clients.keys()):
            if ip == "127.0.0.1":
                continue
            c = self.peer_clients[ip]
            if ip in desired and c.sock is not None and (now_mono - c.last_rx) > CLIENT_STALE_SEC:
                self._append_diag(
                    f"Peer {ip} went silent > {CLIENT_STALE_SEC:.0f}s with a live socket "
                    "-- rebuilding its connection."
                )
                c.disconnect()
                del self.peer_clients[ip]

        label = self.label_var.get().strip()
        for ip in desired:
            if ip not in self.peer_clients:
                peer_port = port
                if ip in snap:
                    peer_port = int(snap[ip].get("tcp", port))
                client = MessageClient(ip, peer_port, label, self.queue)
                client.connect()
                self.peer_clients[ip] = client

        self._merge_online_labels(snap, now)

    def _maybe_refresh_local_ips(self) -> None:
        """Kick off a background refresh of this machine's IP set, at most every
        LOCAL_IP_REFRESH_SEC. local_ipv4_addresses() does a blocking getaddrinfo
        and opens a UDP socket, so running it inline on the UI thread every 1.5s
        (as the old code did) risked freezing the app for seconds when DNS/mDNS
        is slow after a weekend, and churned ~40 sockets/min for an address that
        almost never changes. The result is applied back on the UI thread via
        the queue ('local_ips')."""
        now_mono = time.monotonic()
        last = self._last_ip_refresh_monotonic
        if last is not None and (now_mono - last) < LOCAL_IP_REFRESH_SEC:
            return
        self._last_ip_refresh_monotonic = now_mono
        threading.Thread(target=self._refresh_local_ips_worker, daemon=True).start()

    def _refresh_local_ips_worker(self) -> None:
        try:
            ips = local_ipv4_addresses()
        except Exception:
            return
        self.queue.put(("local_ips", ips))

    def _apply_local_ips(self, ips: set) -> None:
        if not ips:
            return
        self.local_ips = ips
        # Update discovery's OWN reference too. LanDiscovery stored the set it
        # was constructed with; rebinding only our attribute would never reach
        # its listen-loop self-filtering or directed-broadcast derivation.
        if self.discovery is not None:
            self.discovery.local_ips = ips

    def _restart_discovery(self) -> None:
        """Stop and recreate just the LanDiscovery layer (listen + broadcast),
        leaving the TCP server/clients alone. Cheaper than a full LAN bounce and
        used by the listen-thread liveness watchdog. Runs on the UI thread; only
        touches LanDiscovery + Tk vars (no cross-thread Tk)."""
        if not self._network_running:
            return
        old = self.discovery
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        label = self.label_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            port = DEFAULT_PORT
        self.discovery = LanDiscovery(label, port, self.local_ips, self.queue)
        self._sync_discovery_label_state()
        self.discovery.start()

    @staticmethod
    def _udp_labels_from_snap(snap: dict[str, dict], now: float) -> set[str]:
        """Labels from fresh LAN beacons only (not preset names)."""
        found: set[str] = set()
        for _ip, info in snap.items():
            if now - float(info.get("last_seen", 0)) <= PEER_STALE_SEC:
                lab = str(info.get("label", "")).strip()
                if lab:
                    found.add(lab)
        return found

    def _udp_discovered_labels(self, now: float) -> set[str]:
        if not self.discovery:
            return set()
        return self._udp_labels_from_snap(self.discovery.snapshot(), now)

    def _merge_online_labels(self, snap: dict[str, dict], now: float) -> None:
        me = self.label_var.get().strip()
        udp = self._udp_labels_from_snap(snap, now)
        self.online_labels = sorted(l for l in udp if l != me)
        self._refresh_duplicate_name_warning(snap, now)
        # Send targets: only stations currently seen online (same as online_labels)
        self._refresh_target_checkboxes(self.online_labels)
        self._apply_default_target_selection(force=False)
        self._refresh_lan_status_banner()

    def start_network(self) -> None:
        if self._network_running:
            self._append_diag("Start Network ignored (already running).")
            return
        try:
            port = int(self.port_var.get().strip())
            if port < 1 or port > 65535:
                raise ValueError()
        except ValueError:
            messagebox.showerror(APP_TITLE, "Port must be a number between 1 and 65535.")
            return

        label = self.label_var.get().strip()
        if not label or label == STATION_NAME_PROMPT:
            messagebox.showerror(APP_TITLE, "Set up this station's name first (click 'Set up' at the top left).")
            return

        self.local_ips = local_ipv4_addresses()
        # We just resolved IPs synchronously; defer the first background refresh
        # by LOCAL_IP_REFRESH_SEC so the sync tick doesn't redundantly re-resolve
        # on its very first run.
        self._last_ip_refresh_monotonic = time.monotonic()
        try:
            self.server = MessageServer("0.0.0.0", port, self.queue)
            self.server.start()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start network listener: {exc}")
            self.server = None
            return

        self.discovery = LanDiscovery(label, port, self.local_ips, self.queue)
        # Seed the beacon with our current shared-list state so the first beacon
        # already carries it; new peers converge without waiting for the next add/remove.
        self._sync_discovery_label_state()
        self.discovery.start()
        self._network_running = True
        self._set_duplicate_name_warning(False)
        self._persist_form()
        self._append_diag(f"LAN mode: label={label}, port={port}, discovery UDP:{DISCOVERY_PORT}")
        # Seed the wake-detector baseline right before the sync loop starts so
        # its first tick measures a real ~1.5s interval, not the gap since app
        # launch (which would otherwise false-trigger a wake bounce).
        self._last_tick_monotonic = time.monotonic()
        self._schedule_sync_loop()
        self._refresh_lan_status_banner()
        self._schedule_lan_health_check()

    def stop_network(self) -> None:
        self._cancel_sync_loop()
        if not self._network_running:
            return
        for client in list(self.peer_clients.values()):
            client.disconnect()
        self.peer_clients.clear()
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        if self.server:
            self.server.stop()
            self.server = None
        self._network_running = False
        self._set_duplicate_name_warning(False)
        self._refresh_lan_status_banner()
        self._cancel_lan_health_check()
        self._close_lan_health_help_dialog()
        self._append_diag("Network stopped.")

    def _ip_for_label(self, label: str) -> Optional[str]:
        """Pick a peer TCP endpoint that should see all stations in a full mesh."""
        best_ip = None
        best_ts = -1.0
        now = time.time()
        if self.discovery:
            for ip, info in self.discovery.snapshot().items():
                if info.get("label") != label:
                    continue
                ts = float(info.get("last_seen", 0))
                if now - ts <= PEER_STALE_SEC and ts > best_ts:
                    best_ts = ts
                    best_ip = ip
        return best_ip

    def _any_peer_client(self) -> Optional[MessageClient]:
        if "127.0.0.1" in self.peer_clients:
            return self.peer_clients["127.0.0.1"]
        if self.peer_clients:
            return next(iter(self.peer_clients.values()))
        return None

    def send_ready_selected(self) -> None:
        if not self._has_real_station_label():
            # Belt-and-suspenders: the Ready button is also visually disabled
            # by _set_ready_button_enabled, but the menu shortcut and keyboard
            # bindings can still fire send_ready_selected. Block here too.
            messagebox.showwarning(APP_TITLE, "Set up this station's name first (click 'Set up' at the top left) — Ready alerts need a sender name.")
            return
        if self._is_locked_out():
            self._prompt_trial_expired_dialog()
            return
        if not self._network_running or not self.peer_clients:
            messagebox.showwarning(APP_TITLE, "Network is not connected yet.")
            return
        message = "Ready"
        targets = [label for label, var in self.target_vars.items() if var.get()]
        if not targets:
            messagebox.showwarning(APP_TITLE, "Select at least one target.")
            return
        offline = [label for label in targets if label not in self.online_labels]
        if offline:
            messagebox.showwarning(
                APP_TITLE,
                f"These targets are not currently seen on the LAN: {', '.join(offline)}.\n"
                "Wait for them to appear, or add manual IPs under Settings → Network.",
            )
            return
        ip = self._ip_for_label(targets[0])
        if not ip:
            messagebox.showwarning(
                APP_TITLE,
                "Could not locate that station. Check labels are unique and PCs are on the same network.",
            )
            return
        client = self.peer_clients.get(ip)
        if not client:
            messagebox.showwarning(APP_TITLE, "Not connected to that station yet — try again in a few seconds.")
            return
        # Don't fire a Ready into a socket we already know is down. In steady
        # state the heartbeat keeps this socket either live or visibly
        # reconnecting (sock is None during the 3s backoff), so a None here
        # means "mid-reconnect" -- surface that instead of logging a "Sent" that
        # never left the machine. The sync loop / the client's own _run loop
        # will have it back within a few seconds.
        if getattr(client, "sock", None) is None:
            messagebox.showwarning(
                APP_TITLE,
                "Reconnecting to that station after it was idle or asleep — "
                "wait a moment and tap Ready again.",
            )
            return
        client.send_chat(targets, message, self.alert_sound_var.get(), int(self.alert_volume_var.get()))
        self._append_ready_log(f"Sent → {', '.join(targets)}\t[{now_str_short()}]")
        self._show_ready_sent(targets[0] if len(targets) == 1 else f"{len(targets)} stations")
        self._maybe_trigger_whimsy()

    def send_ready_all(self) -> None:
        if not self._has_real_station_label():
            messagebox.showwarning(APP_TITLE, "Set up this station's name first (click 'Set up' at the top left) — Ready alerts need a sender name.")
            return
        if self._is_locked_out():
            self._prompt_trial_expired_dialog()
            return
        if not self._network_running or not self.peer_clients:
            messagebox.showwarning(APP_TITLE, "Network is not connected yet.")
            return
        message = "Ready"
        client = self.peer_clients.get("127.0.0.1")
        if not client:
            client = self._any_peer_client()
        if not client:
            messagebox.showwarning(APP_TITLE, "No peer connection available yet.")
            return
        if getattr(client, "sock", None) is None:
            messagebox.showwarning(
                APP_TITLE,
                "Reconnecting after an idle or sleep gap — wait a moment and tap Ready again.",
            )
            return
        client.send_chat(["ALL"], message)
        self._append_ready_log(f"Sent → All\t[{now_str_short()}]")
        self._show_ready_sent("all stations")
        self._maybe_trigger_whimsy()

    def _show_ready_sent(self, who: str) -> None:
        """Brief green "✓ Sent to ..." confirmation on the Ready button."""
        btn = getattr(self, "_ready_btn_widget", None)
        if btn is not None:
            try:
                btn.show_sent(f"Sent to {who}")
            except tk.TclError:
                pass

    def _maybe_trigger_whimsy(self) -> None:
        """Called after every outgoing Ready. Asks the trigger if this send
        qualifies for a stick-figure animation, and if so, picks a random
        enabled animation and plays it. Tolerant of every error path — the
        whimsy must never disrupt the actual Ready-sending behavior."""
        if _ready_animations is None or self._anim_trigger is None:
            return
        # Don't stack animations — if one is currently playing, skip.
        existing = self._anim_player
        if existing is not None and getattr(existing, "running", False):
            return
        try:
            if not self._anim_trigger.on_ready_sent():
                return
        except Exception:
            return
        # Trigger fired — pick an enabled animation and play it.
        try:
            prefs = _ready_animations.load_animation_prefs()
            anim_id = _ready_animations.pick_random_enabled_animation(prefs)
            if anim_id is None:
                return
            # Apply the user's current chosen character (in case they edited
            # it via the picker since we last loaded it on startup).
            character = _ready_animations.load_animation_character()
            ready_btn = getattr(self, "_ready_btn_widget", None)
            if ready_btn is None:
                return
            self._anim_player = _ready_animations.play_animation(
                anim_id,
                self.root,
                ready_btn,
                character=character,
                on_complete=self._on_whimsy_complete,
            )
        except Exception:
            self._anim_player = None

    def _on_whimsy_complete(self) -> None:
        self._anim_player = None

    def _report_tk_callback_exception(self, exc, val, tb) -> None:
        """Tk routes uncaught exceptions from after-callbacks, bindings, and
        commands here. Capture them durably (the startup log FIRST -- it doesn't
        depend on a healthy UI -- then the in-app diagnostic) so the
        self-healing loops' failure modes leave a forensic trail instead of
        vanishing. This logs; it does not by itself resurrect a loop -- the
        try/finally re-arms in _process_ui_queue and the sync tick do that."""
        try:
            text = "".join(traceback.format_exception(exc, val, tb))
        except Exception:
            text = f"{exc}: {val}"
        _append_startup_log("Tk callback exception:\n" + text)
        try:
            self._diag_lines.append(f"[{now_str()}] Tk callback exception: {val}")
        except Exception:
            pass

    def _process_ui_queue(self) -> None:
        # This pump is the ONLY path background threads use to reach the UI:
        # incoming Ready alerts, peer presence, AND the system_wake recovery
        # signal that powers weekend sleep/wake recovery. It MUST NOT die. The
        # earlier version put the root.after re-arm as the last statement,
        # outside any guard -- so a single uncaught TclError/KeyError from one
        # handler (a malformed payload, a widget destroyed mid-theme-switch)
        # left the pump un-rescheduled and the app silently stopped delivering
        # every incoming alert and never processed another wake event, with no
        # crash and (in a frozen build) no stderr. Now each item is dispatched
        # under its own guard and the re-arm lives in a finally, mirroring
        # _check_log_rollover.
        try:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dispatch_ui_event(kind, payload)
                except Exception as exc:
                    # Append to the in-memory deque directly (pure list op --
                    # cannot itself raise during a teardown race the way the
                    # widget-touching _append_diag could).
                    self._diag_lines.append(f"[{now_str()}] UI queue handler '{kind}' failed: {exc}")
        finally:
            if not self._quitting:
                try:
                    self.root.after(120, self._process_ui_queue)
                except tk.TclError:
                    pass

    def _dispatch_ui_event(self, kind: str, payload) -> None:
        if kind == "status":
            # Routine connect/retry/disconnect stays in Connection log only so the
            # banner stays on "On LAN" / Waiting unless we add explicit error kinds later.
            self._append_diag(str(payload))
        elif kind == "discovery":
            if self.discovery:
                snap = self.discovery.snapshot()
                self._merge_online_labels(snap, time.time())
                if snap and self._lan_health_dialog is not None:
                    self._close_lan_health_help_dialog()
        elif kind == "network":
            self._handle_network_payload(payload)
        elif kind == "tray_action":
            self._dispatch_tray_action(str(payload))
        elif kind == "focus_request":
            self._show_main_window()
        elif kind == "labels_snapshot":
            inc_labels, inc_removed = payload
            self._merge_remote_labels_snapshot(inc_labels, inc_removed)
        elif kind == "label_event":
            action, label = payload
            self._apply_remote_label_event(action, label)
        elif kind == "license":
            if isinstance(payload, tuple) and len(payload) == 2:
                result, user_initiated = payload
            else:
                result, user_initiated = payload, False
            self._on_license_refresh_result(result, user_initiated)
        elif kind == "system_wake":
            self._handle_system_wake()
        elif kind == "local_ips":
            self._apply_local_ips(payload)
        elif kind == "bell":
            # Audible fallback when sound playback fails, routed through the
            # queue so the bell is rung on the MAIN thread (Tk calls from the
            # sound worker thread are the off-main-thread access we must avoid).
            try:
                self.root.bell()
            except tk.TclError:
                pass

    def _handle_network_payload(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "presence":
            labels = payload.get("labels", [])
            me = self.label_var.get().strip()
            now = time.time()
            udp = self._udp_discovered_labels(now)
            tcp = {str(x).strip() for x in labels if str(x).strip()}
            # Online = LAN beacons and/or TCP mesh visibility (no preset list)
            self.online_labels = sorted((udp | tcp) - {me, ""})
            self._refresh_target_checkboxes(self.online_labels)
            self._apply_default_target_selection(force=False)
            self._refresh_lan_status_banner()
            return

        if msg_type == "chat":
            sender = payload.get("from", "Unknown")
            text = payload.get("message", "")
            # Sender's clock provides the timestamp; fall back to ours if missing.
            # Strip any seconds component for the Ready log (older peers on a
            # pre-1.0.38 build still broadcast HH:MM:SS).
            ts = _strip_seconds_from_ts(payload.get("timestamp") or now_str_short())
            me = self.label_var.get()
            targets = payload.get("to", [])
            if "ALL" in targets or me in targets:
                body = str(text).strip()
                if body.lower() != "ready":
                    self._append_diag(f"Chat from {sender} (not logged in main window): {body!r}")
                    return
                # Drop incoming Ready alerts silently when this station's trial
                # has expired without purchase. Other peers don't get a bounce —
                # they shouldn't know the receiver is locked out — and we log
                # only to the diagnostic stream so users see why nothing fired.
                if self._is_locked_out():
                    self._append_diag(f"Incoming Ready from {sender} dropped: trial expired (purchase to re-enable).")
                    return
                self._append_incoming_ready_log(f"{sender}: Ready\t[{ts}]")
                alert_sound = str(payload.get("alert_sound", "")).strip() or self.alert_sound_var.get()
                self._focus_and_alert_main_window(alert_sound=alert_sound, alert_volume=int(self.alert_volume_var.get()))

    def _focus_and_alert_main_window(self, alert_sound: str = "", alert_volume: int = 70) -> None:
        # Update the hide-flag eagerly — whether the window was hidden-to-tray
        # OR minimized via the OS button, we're about to restore it.
        self._main_hidden = False
        if sys.platform == "darwin":
            self._macos_set_activation_policy_for_main_window(True)
        # Always deiconify. Tk's deiconify restores both withdrawn (hidden) AND
        # iconic (minimized) states; calling it on an already-visible window is
        # a no-op. Previously this was gated on _main_hidden, which missed the
        # case where the user used the OS minimize button.
        try:
            self.root.deiconify()
        except tk.TclError:
            pass
        self.root.update_idletasks()
        self._play_alert_sound(alert_sound=alert_sound, alert_volume=alert_volume)
        # On Windows, flash the taskbar entry — that's the OS-native attention
        # pulse users actually see. The bg-color flash is left in for macOS
        # where there's no equivalent native API; it's also a soft visual
        # reinforcement on Windows even if the taskbar flash is doing the
        # heavy lifting.
        if sys.platform == "win32":
            self._flash_windows_taskbar(times=2)
        self._blink_main_window(rounds=4)
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(400, lambda: self.root.attributes("-topmost", False))

    def _flash_windows_taskbar(self, times: int = 2) -> None:
        """Flash the taskbar/titlebar on Windows via FlashWindowEx. Equivalent
        to the attention-pulse Slack/Teams use when a notification arrives but
        focus can't be stolen. No-op on non-Windows platforms."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD),
                ]

            FLASHW_CAPTION = 0x00000001
            FLASHW_TRAY    = 0x00000002
            FLASHW_ALL     = FLASHW_CAPTION | FLASHW_TRAY

            user32 = ctypes.windll.user32
            # winfo_id returns the Tk window's HWND. On some Tk builds it
            # returns the inner client; GetAncestor with GA_ROOT (=2) climbs
            # to the top-level shell so the taskbar entry actually flashes.
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()

            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(info)
            info.hwnd = hwnd
            info.dwFlags = FLASHW_ALL
            info.uCount = max(1, int(times) * 2)  # each visible flash is one on+off pair
            info.dwTimeout = 0
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _blink_main_window(self, rounds: int = 4) -> None:
        # Soft-reinforcement bg flash. On Windows the FlashWindowEx call does
        # the heavy lifting; this is mostly perceptible on macOS.
        reset_bg = self._current_theme.get("bg", "#f8f9fa")
        colors = [reset_bg, "#fbbc04"]

        def step(index: int) -> None:
            if not self.root.winfo_exists():
                return
            if index >= rounds:
                self.root.configure(bg=self._current_theme.get("bg", "#f8f9fa"))
                return
            self.root.configure(bg=colors[index % 2])
            self.root.lift()
            self.root.after(220, lambda: step(index + 1))

        step(0)

    def _play_alert_sound(self, alert_sound: str = "", alert_volume: int = 70) -> None:
        # Resolve the sound name + volume HERE, on the main thread. The worker
        # must not read the alert_sound_var Tk StringVar (off-main-thread Tk
        # access is the documented macOS-crash hazard); pass it concrete values.
        try:
            sound_name = alert_sound.strip() if alert_sound.strip() else self.alert_sound_var.get().strip()
        except Exception:
            sound_name = ""
        if not sound_name:
            sound_name = ALERT_SOUND_OPTIONS[0]
        try:
            volume = max(0, min(100, int(alert_volume)))
        except Exception:
            volume = 70
        threading.Thread(
            target=self._play_alert_sound_worker, args=(sound_name, volume), daemon=True
        ).start()

    def _play_alert_sound_worker(self, sound_name: str, alert_volume: int) -> None:
        volume = max(0, min(100, int(alert_volume))) / 100.0
        try:
            wav_bytes = self._wav_bytes_for(sound_name, volume)
        except Exception:
            self.queue.put(("bell", None))   # ring on the main thread
            return
        if not wav_bytes:
            self.queue.put(("bell", None))
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            temp_wav = tmp.name
            tmp.write(wav_bytes)
        proc = None
        try:
            if sys.platform.startswith("win"):
                import winsound

                winsound.PlaySound(temp_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                proc = subprocess.Popen(
                    ["afplay", "-v", f"{max(0.0, min(2.0, volume * 2.0)):.2f}", temp_wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self.queue.put(("bell", None))
        except Exception:
            self.queue.put(("bell", None))
        finally:
            threading.Thread(
                target=self._cleanup_temp_file_later, args=(temp_wav, proc), daemon=True
            ).start()

    def _sound_sequence(self, sound_name: str) -> list[tuple[int, int, int]]:
        sounds = {
            "Bright Chime": [(988, 160, 80), (1318, 220, 0)],
            "Soft Ding": [(740, 150, 0)],
            "Double Ding": [(830, 130, 80), (830, 130, 0)],
            "Triple Ping": [(1100, 90, 70), (1200, 90, 70), (1300, 120, 0)],
            "Deep Pulse": [(420, 220, 120), (420, 220, 0)],
            "Quick Beep": [(1200, 80, 0)],
            "Steady Beep": [(900, 320, 0)],
            "Rising Tone": [(620, 100, 50), (740, 100, 50), (880, 140, 0)],
            "Falling Tone": [(1000, 100, 50), (820, 100, 50), (660, 140, 0)],
            "Crisp Bell": [(1320, 140, 70), (990, 170, 0)],
            "Warm Bell": [(660, 180, 80), (760, 210, 0)],
            "High Alert": [(1450, 120, 70), (1450, 120, 70), (1450, 150, 0)],
            "Low Alert": [(520, 200, 80), (500, 200, 0)],
            "Ripple": [(700, 100, 40), (900, 100, 40), (700, 100, 40), (900, 130, 0)],
            "Classic Pager": [(980, 120, 60), (980, 120, 160), (980, 120, 0)],
        }
        return sounds.get(sound_name, sounds[ALERT_SOUND_OPTIONS[0]])

    def _wav_bytes_for(self, sound_name: str, volume: float) -> bytes:
        """Rendered WAV bytes for (sound, volume), memoized. The waveform was
        previously re-synthesized in pure Python on EVERY play; an identical
        alert (same sound, same volume -- the common case) now hits the cache."""
        max_amplitude = int(32767 * max(0.0, min(1.0, volume)))
        if max_amplitude <= 0:
            max_amplitude = 500
        key = (sound_name, max_amplitude)
        cached = self._wav_cache.get(key)
        if cached is not None:
            return cached
        data = self._render_wav_bytes(self._sound_sequence(sound_name), max_amplitude)
        # 15 sounds x a few volumes is tiny; bound it anyway so it can't grow.
        if len(self._wav_cache) > 64:
            self._wav_cache.clear()
        self._wav_cache[key] = data
        return data

    def _render_wav_bytes(self, sequence: list[tuple[int, int, int]], max_amplitude: int) -> bytes:
        frame_data = bytearray()
        for frequency, duration_ms, gap_ms in sequence:
            tone_frames = int(ALERT_SAMPLE_RATE * (duration_ms / 1000.0))
            for i in range(tone_frames):
                t = i / ALERT_SAMPLE_RATE
                value = int(max_amplitude * math.sin(2 * math.pi * frequency * t))
                frame_data.extend(value.to_bytes(2, byteorder="little", signed=True))
            gap_frames = int(ALERT_SAMPLE_RATE * (gap_ms / 1000.0))
            frame_data.extend(b"\x00\x00" * gap_frames)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(ALERT_SAMPLE_RATE)
            wav.writeframes(bytes(frame_data))
        return buf.getvalue()

    def _cleanup_temp_file_later(self, path: str, proc: "Optional[subprocess.Popen]" = None) -> None:
        # Wait for the player to finish before deleting the temp WAV. For the
        # macOS afplay child this also REAPS it (proc.wait), so repeated alerts
        # over a long uptime don't leave a trail of zombie processes. Bounded so
        # a hung player can't pin this thread (or a zombie) forever.
        if proc is not None:
            try:
                proc.wait(timeout=30)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        else:
            time.sleep(3)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def on_close(self) -> None:
        if not self._quitting:
            if sys.platform == "darwin":
                # macOS: hide the app via NSApplication so Dock click reopens the same
                # running process (no relaunch feel, no separate minimized tile).
                self._main_hidden = True
                self._macos_set_activation_policy_for_main_window(True)
                try:
                    from AppKit import NSApplication  # type: ignore

                    NSApplication.sharedApplication().hide_(None)
                except Exception:
                    # Fallback for environments where AppKit hide is unavailable.
                    self.root.iconify()
                return
            if self._tray_icon is not None:
                self._hide_main_window()
                return
        try:
            self._persist_form()
        except Exception:
            pass
        self.stop_network()
        self._stop_tray_icon()
        self._stop_focus_server()
        self.root.destroy()


def _startup_log_path() -> str:
    # Always write logs to the user data dir (writable on Store and dev installs).
    d = _user_data_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, "startup_log.txt")


def _append_startup_log(line: str) -> None:
    try:
        path = _startup_log_path()
        try:
            if os.path.getsize(path) > STARTUP_LOG_MAX_BYTES:
                # Rotate (keep one previous generation) so the log can't grow
                # without bound across launches and lifecycle events.
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {line}\n")
    except Exception:
        pass


def _show_already_running_message() -> None:
    msg = (
        "Chairside Ready Alert is already running.\n\n"
        "Use the existing app window or tray/menu-bar icon."
    )
    try:
        _enable_windows_dpi_awareness()
        popup_root = tk.Tk()
        popup_root.withdraw()
        messagebox.showinfo(APP_TITLE, msg)
        popup_root.destroy()
    except Exception:
        _append_startup_log("Second launch blocked (message box unavailable).")


def _notify_existing_instance_focus() -> bool:
    try:
        with socket.create_connection((FOCUS_IPC_HOST, FOCUS_IPC_PORT), timeout=0.8) as s:
            s.sendall(FOCUS_IPC_TOKEN.encode("utf-8"))
        return True
    except OSError:
        return False




def main() -> None:
    _append_startup_log("starting main()")
    lock_path = os.path.join(_user_data_dir(), INSTANCE_LOCK_FILE)
    lock = SingleInstanceLock(lock_path)
    if not lock.acquire():
        _append_startup_log("Second launch blocked by single-instance lock.")
        if _notify_existing_instance_focus():
            return
        # Focus notify failed -- the lock MIGHT be stale after an unclean
        # shutdown, OR a healthy first instance is simply slow to start its
        # focus server (a ~1.5s window after launch). Before removing the lock,
        # verify the recorded owner PID is actually dead. Removing a live
        # owner's lock is the bug we must avoid: on POSIX it wouldn't release
        # the live flock anyway, and on Windows it would let two instances run.
        owner_pid = _read_lock_pid(lock_path)
        if owner_pid is not None and owner_pid != os.getpid() and _pid_is_alive(owner_pid):
            _append_startup_log(
                f"Focus notify failed but lock owner PID {owner_pid} is alive; "
                "not removing lock (likely a slow-starting first instance)."
            )
            _show_already_running_message()
            return
        _append_startup_log("Focus notify failed and lock owner is gone; attempting stale-lock recovery.")
        try:
            os.remove(lock_path)
            _append_startup_log("Removed stale instance lock; retrying acquire.")
        except OSError:
            _append_startup_log("Stale-lock removal failed; showing already-running message.")
            _show_already_running_message()
            return
        lock = SingleInstanceLock(lock_path)
        if not lock.acquire():
            _append_startup_log("Re-acquire after stale-lock cleanup failed; showing already-running message.")
            _show_already_running_message()
            return
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    try:
        _append_startup_log("Tk() created, building app")
        ChairsideReadyAlertApp(root)
        _append_startup_log("entering mainloop()")
        root.mainloop()
        _append_startup_log("mainloop exited")
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            import traceback

            with open(_startup_log_path(), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
                f.write("\n")
        except Exception:
            pass
        raise
