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
import tkinter as tk
import wave
from datetime import datetime
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Optional

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
APP_VERSION = "1.0.27"
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
PEER_STALE_SEC = 12.0
BUFFER_SIZE = 4096
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


# ============================================================================
# Microsoft Store subscription gate (Microsoft Store / MSIX builds only)
#
# The Microsoft Store build is "free download, 7-day free trial, $1.99/month
# subscription thereafter". Purchase, billing, renewal, cancellation, refunds,
# and trial-used tracking are all handled by Microsoft Store via an in-app
# subscription Add-on. The app's job is to (a) ask Microsoft whether the
# current Microsoft account has an active subscription (trial counts as
# active), (b) cache that answer for offline tolerance, and (c) gate
# functionality behind it. The 7-day trial period is a Partner Center
# configuration on the Add-on — no code change needed for the gate.
#
# This entire subsystem is a no-op when not running as a frozen Microsoft
# Store build:
#   - Direct-installer users (Windows .ps1, macOS .command, IS_FROZEN=False)
#     bypass the check entirely. Their distribution is free as it always was.
#   - macOS Store has no equivalent path here.
#
# Product ID for the subscription Add-on. When the user creates the
# Subscription Add-on in Partner Center, this exact string MUST be used as
# the Add-on "Product ID" so the app can query its state.
# ============================================================================
SUBSCRIPTION_ADDON_PRODUCT_ID = "ChairsideReadyAlert.Subscription.Monthly"
SUBSCRIPTION_CACHE_FILE = "subscription_cache.json"
SUBSCRIPTION_GRACE_DAYS = 7  # Cached "active" state honored this long without a successful Store ping.


def _subscription_enforced() -> bool:
    """Subscription gate is only enforced for the frozen Microsoft Store build
    on Windows. Everywhere else (dev, direct-installer, macOS) returns False
    and the gate is bypassed."""
    return IS_FROZEN and sys.platform == "win32"


def _subscription_cache_path() -> str:
    return os.path.join(_user_data_dir(), SUBSCRIPTION_CACHE_FILE)


def _read_subscription_cache() -> dict:
    try:
        with open(_subscription_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_subscription_cache(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_subscription_cache_path()), exist_ok=True)
        with open(_subscription_cache_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _is_subscribed_from_cache() -> bool:
    cache = _read_subscription_cache()
    if not cache.get("active"):
        return False
    last_checked = float(cache.get("last_checked", 0) or 0)
    return (time.time() - last_checked) <= (SUBSCRIPTION_GRACE_DAYS * 86400)


def _check_store_subscription_live() -> Optional[bool]:
    """Live query to Microsoft Store. Returns True/False on a successful query,
    or None if the Store services aren't reachable (caller should fall back to
    cache). Only called when _subscription_enforced() is True."""
    try:
        # Imports deferred so non-Windows / non-frozen builds never hit them.
        import asyncio  # noqa: WPS433 — local import is intentional
        from winrt.windows.services.store import StoreContext  # type: ignore
    except Exception as exc:
        _append_startup_log(
            f"_check_store_subscription_live: winrt import failed: "
            f"{type(exc).__name__}: {exc}"
        )
        return None
    try:
        async def _query() -> bool:
            ctx = StoreContext.get_default()
            info = await ctx.get_user_collection_async(["Durable", "Subscription"])
            for item in info.products.values():
                offer_token = getattr(item, "in_app_offer_token", "") or ""
                if offer_token == SUBSCRIPTION_ADDON_PRODUCT_ID and bool(getattr(item, "is_active", False)):
                    return True
            return False
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_query())
        finally:
            loop.close()
    except Exception as exc:
        _append_startup_log(
            f"_check_store_subscription_live: Store query failed: "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        return None


def is_subscribed() -> bool:
    """Synchronous gate query. Tries Microsoft Store live; falls back to cache
    on transient failure. Returns True for non-Store builds (gate disabled)."""
    if not _subscription_enforced():
        return True
    live = _check_store_subscription_live()
    if live is not None:
        _write_subscription_cache({"active": bool(live), "last_checked": time.time()})
        return bool(live)
    return _is_subscribed_from_cache()


def _store_helper_path() -> Optional[str]:
    """Locate StoreHelper.exe sibling to the running EXE in the PyInstaller bundle.
    Returns None for non-frozen builds (where the helper isn't shipped) or if
    the file is missing in a frozen install."""
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(exe_dir, "StoreHelper.exe")
    return candidate if os.path.exists(candidate) else None


def request_subscription_purchase(hwnd: Optional[int] = None) -> bool:
    """Open the Microsoft Store in-app purchase overlay for the subscription
    Add-on. Returns True on completed purchase or already-owned, False otherwise.

    Delegates to the bundled StoreHelper.exe because the Microsoft Store SDK
    requires the host to call IInitializeWithWindow.Initialize(hwnd) before any
    UI-bearing Store call. IInitializeWithWindow is classic COM, not WinRT, so
    the Python winrt projection cannot reach it; calling RequestPurchaseAsync
    directly from Python fails with RPC_E_WRONG_THREAD ("must be called from a
    UI thread") regardless of which thread it actually runs on.
    """
    if not _subscription_enforced():
        return True

    helper = _store_helper_path()
    if helper is None:
        _append_startup_log(
            f"request_subscription_purchase: StoreHelper.exe not found beside "
            f"sys.executable={sys.executable!r}"
        )
        return False

    args = [helper, SUBSCRIPTION_ADDON_PRODUCT_ID]
    if hwnd is not None and int(hwnd) != 0:
        args.append(str(int(hwnd)))

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=300,  # purchase overlay can sit open while the user thinks
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _append_startup_log(
            "request_subscription_purchase: helper timed out after 5 minutes"
        )
        return False
    except Exception as exc:
        _append_startup_log(
            f"request_subscription_purchase: helper invocation failed: "
            f"{type(exc).__name__}: {exc}"
        )
        return False

    _append_startup_log(
        f"request_subscription_purchase: helper exit_code={proc.returncode}, "
        f"stdout={proc.stdout.strip()!r}, stderr={proc.stderr.strip()!r}"
    )
    if proc.returncode == 0:
        _write_subscription_cache({"active": True, "last_checked": time.time()})
        return True
    return False


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

# Layout themes for the main UI.
THEMES: dict[str, dict] = {
    "Modern Blue": {
        "bg": "#f0f4ff", "card_bg": "#ffffff", "accent": "#2563eb", "accent_text": "#ffffff",
        "title": "#1e40af", "text": "#1e293b", "sub": "#64748b", "card_border": "#dde6f5",
        "log_bg": "#f8faff", "log_text": "#475569", "status": "#16a34a",
        "input_bg": "#f8fafc", "input_border": "#cbd5e1", "slider_track": "#dde6f5",
    },
    "Sage Clinic": {
        "bg": "#f2f5ee", "card_bg": "#ffffff", "accent": "#4a7c59", "accent_text": "#ffffff",
        "title": "#2d5a3d", "text": "#2c3e2d", "sub": "#6b8f6c", "card_border": "#d4e4d5",
        "log_bg": "#f7faf7", "log_text": "#5a7a5b", "status": "#2d8a4e",
        "input_bg": "#f7faf7", "input_border": "#c8dfc9", "slider_track": "#c8dfc9",
    },
    "Rose Quartz": {
        "bg": "#fdf2f4", "card_bg": "#ffffff", "accent": "#e11d48", "accent_text": "#ffffff",
        "title": "#be123c", "text": "#3d0a18", "sub": "#9c5a6c", "card_border": "#fbc9d4",
        "log_bg": "#fff5f7", "log_text": "#9c5a6c", "status": "#059669",
        "input_bg": "#fff5f7", "input_border": "#fbc9d4", "slider_track": "#fbc9d4",
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
    """Addresses that identify this machine (skip connecting to ourselves)."""
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


class ConfigStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = {
            "label": "Room 1",
            "server_host": "",
            "server_port": DEFAULT_PORT,
            "is_server": False,
            "manual_peer_ips": "",
            "alert_sound": ALERT_SOUND_OPTIONS[0],
            "alert_volume": 70,
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
        # Shared station-label state piggybacked on the beacon. The App sets these
        # via update_station_lists; LanDiscovery reads them when sending each beacon.
        self._station_labels: list[str] = []
        self._removed_labels: list[str] = []

    def update_label(self, label: str) -> None:
        self.label = label.strip() or "Room 1"

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
                "action": action,
                "label": str(label),
                "ts": time.time(),
            }).encode("utf-8")
            sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
        except Exception:
            pass

    def start(self) -> None:
        self.running = True
        self._l_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._l_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._l_sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            try:
                self._l_sock.close()
            except Exception:
                pass
            self._l_sock = None
            self.ui_queue.put(
                (
                    "status",
                    f"UDP discovery port {DISCOVERY_PORT} busy — use manual peer IPs if others do not appear. ({exc})",
                )
            )
        else:
            self._l_sock.settimeout(1.0)
            threading.Thread(target=self._listen_loop, daemon=True).start()

        self._b_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self._b_sock.bind(("", 0))
        except OSError:
            try:
                self._b_sock.close()
            except Exception:
                pass
            self._b_sock = None
        else:
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

    def _broadcast_loop(self) -> None:
        while self.running and self._b_sock:
            try:
                with self.lock:
                    labels_snap = list(self._station_labels)
                    removed_snap = list(self._removed_labels)
                payload = json.dumps(
                    {
                        "type": "dm_beacon",
                        "v": BEACON_VERSION,
                        "label": self.label,
                        "tcp": self.tcp_port,
                        # Eventual-consistency snapshot so newly-online peers
                        # converge on the same shared list without needing every
                        # add/remove event to have been received.
                        "labels": labels_snap,
                        "removed": removed_snap,
                    }
                ).encode("utf-8")
                # 1500 bytes is the typical Ethernet MTU; UDP fragmentation across
                # a single LAN is fine, so don't bail above 1200 — just send.
                self._b_sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except Exception:
                pass
            time.sleep(BEACON_INTERVAL)

    def _listen_loop(self) -> None:
        assert self._l_sock is not None
        while self.running:
            try:
                data, addr = self._l_sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            if ip in self.local_ips:
                continue
            try:
                obj = json.loads(data.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            if int(obj.get("v", 0)) != BEACON_VERSION:
                continue
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
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(20)
        self.server_socket.settimeout(1.0)
        self.ui_queue.put(("status", f"Listening for peers on {self.host}:{self.port}"))

        while self.running:
            try:
                client_sock, addr = self.server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.client_sockets.add(client_sock)
            threading.Thread(
                target=self._handle_client, args=(client_sock, addr), daemon=True
            ).start()

    def _handle_client(self, client_sock: socket.socket, addr) -> None:
        label = None
        buffer = ""
        client_sock.settimeout(1.0)
        self.ui_queue.put(("status", f"Peer connected from {addr[0]}"))
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
                            self.client_sockets.discard(existing)
                            try:
                                existing.close()
                            except Exception:
                                pass
                        label = proposed
                        self.clients[label] = client_sock
                    self._broadcast_presence()
                    self.ui_queue.put(("status", f"Registered: {label}"))
                elif msg_type == "chat":
                    self._relay_chat(payload)
                elif msg_type == "ping":
                    self._send_json(client_sock, {"type": "pong", "ts": time.time()})

        with self.lock:
            if label and label in self.clients and self.clients[label] is client_sock:
                self.clients.pop(label, None)
            self.client_sockets.discard(client_sock)
        try:
            client_sock.close()
        except Exception:
            pass
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
            "timestamp": now_str(),
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
                with self.lock:
                    self.sock = sock
                self.ui_queue.put(("status", f"Connected to {self.host}:{self.port}"))
                self._send_json({"type": "hello", "label": self.label})
                self._listen_loop(sock)
            except Exception:
                self.ui_queue.put(("status", f"Disconnected from {self.host}:{self.port}. Retrying…"))
                time.sleep(3)

    def _listen_loop(self, sock: socket.socket) -> None:
        buffer = ""
        while self.running:
            try:
                data = sock.recv(BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
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


class RoundedCard(tk.Frame):
    """A Frame whose background is a Canvas-drawn rounded rectangle."""

    def __init__(self, parent, bg: str = "#ffffff", outer_bg: str = "#f0f4ff",
                 radius: int = 12, padding: int = 14, border: str | None = None, **kw) -> None:
        super().__init__(parent, bg=outer_bg, **kw)
        self._card_bg = bg
        self._outer_bg = outer_bg
        self._border = border
        self._r = radius
        self._pad = padding

        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=outer_bg)
        self._cv.pack(fill="both", expand=True)

        self.inner_frame = tk.Frame(self._cv, bg=bg)
        self._win = self._cv.create_window(padding, padding,
                                            window=self.inner_frame, anchor="nw")
        self._cv.bind("<Configure>", self._on_cv_resize)
        self.inner_frame.bind("<Configure>", self._on_inner_resize)

    def _on_cv_resize(self, e: tk.Event) -> None:
        self._draw(e.width, e.height)
        # Stretch inner content to full card width so rows (e.g. Ready) can center in the window.
        iw = max(1, int(e.width) - 2 * self._pad)
        self._cv.itemconfigure(self._win, width=iw)

    def _on_inner_resize(self, e: tk.Event) -> None:
        # Height follows content; width comes from canvas (fill="x") + _on_cv_resize.
        self._cv.configure(height=e.height + self._pad * 2)

    def _draw(self, w: int, h: int) -> None:
        if w < 4 or h < 4:
            return
        self._cv.delete("rr")
        r = min(self._r, w // 2, h // 2)
        pts = [r, 0,  w-r, 0,  w, 0,  w, r,
               w, h-r,  w, h,  w-r, h,  r, h,
               0, h,  0, h-r,  0, r,  0, 0]
        outline = self._border or ""
        ow = 1 if self._border else 0
        self._cv.create_polygon(pts, smooth=True, fill=self._card_bg,
                                outline=outline, width=ow, tags="rr")
        self._cv.tag_lower("rr")

    def update_colors(self, bg: str, outer_bg: str, border: str | None = None) -> None:
        self._card_bg = bg
        self._outer_bg = outer_bg
        if border is not None:
            self._border = border
        self.configure(bg=outer_bg)
        self._cv.configure(bg=outer_bg)
        self.inner_frame.configure(bg=bg)
        self._draw(self._cv.winfo_width(), self._cv.winfo_height())


class RoundedLogPanel(tk.Frame):
    """Ready Messages log: rounded outline (theme card_border), log_bg fill, no focus ring."""

    def __init__(
        self,
        parent,
        *,
        log_bg: str,
        log_fg: str,
        border: str,
        card_bg: str,
        font: tuple,
        height_lines: int = 15,
        radius: int = 6,
        **kw,
    ) -> None:
        super().__init__(parent, bg=card_bg, **kw)
        self._log_bg = log_bg
        self._border = border
        self._card_bg = card_bg
        self._r = radius
        self._inset = 2

        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=card_bg)
        self._cv.pack(fill="both", expand=True)

        self.log = tk.Text(
            self._cv,
            height=height_lines,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=6,
            pady=4,
            bg=log_bg,
            fg=log_fg,
            insertbackground=log_fg,
            font=font,
        )
        self._win = self._cv.create_window(self._inset, self._inset, window=self.log, anchor="nw")
        self._cv.bind("<Configure>", self._on_cv_resize)

    def _on_cv_resize(self, e: tk.Event) -> None:
        if e.widget != self._cv:
            return
        w, h = int(e.width), int(e.height)
        if w < 4 or h < 4:
            return
        self._draw(w, h)
        iw = max(1, w - 2 * self._inset)
        ih = max(1, h - 2 * self._inset)
        self._cv.itemconfigure(self._win, width=iw, height=ih)

    def _draw(self, w: int, h: int) -> None:
        self._cv.delete("logbg")
        r = min(self._r, w // 2, h // 2)
        pts = [
            r, 0,
            w - r, 0,
            w, 0,
            w, r,
            w, h - r,
            w, h,
            w - r, h,
            r, h,
            0, h,
            0, h - r,
            0, r,
            0, 0,
        ]
        outline = self._border or ""
        ow = 1 if self._border else 0
        self._cv.create_polygon(
            pts, smooth=True, fill=self._log_bg, outline=outline, width=ow, tags="logbg"
        )
        self._cv.tag_lower("logbg")

    def update_theme(self, log_bg: str, log_fg: str, border: str | None, card_bg: str) -> None:
        self._log_bg = log_bg
        self._border = border or ""
        self._card_bg = card_bg
        self.configure(bg=card_bg)
        self._cv.configure(bg=card_bg)
        self.log.configure(
            bg=log_bg, fg=log_fg, insertbackground=log_fg, font=_ui_font(10),
            highlightthickness=0, borderwidth=0,
        )
        w, h = self._cv.winfo_width(), self._cv.winfo_height()
        if w > 4 and h > 4:
            self._draw(w, h)


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
        bw = width if width > 0 else tw + padx * 2
        bh = th + pady * 2
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
        self._r = radius
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
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(940, 700)

        self.queue = queue.Queue()
        self.config_store = ConfigStore(_resolve_config_path())
        self.server = None
        self.discovery: Optional[LanDiscovery] = None
        self.peer_clients: dict[str, MessageClient] = {}
        self._network_running = False
        self._sync_after_id: Optional[str] = None
        self.local_ips = local_ipv4_addresses()
        self.online_labels: list[str] = []
        self._duplicate_name_detected = False
        self.target_vars = {}
        self._target_signature = None
        self.local_ip = detect_local_ip()
        self.auto_start_done = False
        self._diag_lines: deque[str] = deque(maxlen=500)
        self._diag_win: Optional[tk.Toplevel] = None
        self._diag_text: Optional[tk.Text] = None
        self._tray_diag_lines: deque[str] = deque(maxlen=300)
        self._tray_diag_win: Optional[tk.Toplevel] = None
        self._tray_diag_text: Optional[tk.Text] = None
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
        self._sync_autostart_state()
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
            # Force the menu bar to render the new item immediately, then keep it
            # alive with periodic pumping.  Tkinter's mainloop drives NSApp events
            # but doesn't automatically drain NSRunLoop sources that AppKit uses to
            # paint status items.
            try:
                from Foundation import NSRunLoop, NSDate  # type: ignore
                NSRunLoop.mainRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            except Exception:
                pass
            self.root.after(200, self._pump_macos_runloop)
            return True
        except Exception as exc:
            self._append_diag(f"Native macOS menu bar icon failed: {exc}")
            self._record_tray_diag(f"Native macOS menu bar icon failed: {exc}")
            _append_startup_log(f"native macOS menu bar icon failed: {exc}")
            return False

    def _pump_macos_runloop(self) -> None:
        """Drive the AppKit run loop so the menu-bar status item stays rendered.

        Tkinter's mainloop processes NSApp events but doesn't guarantee the
        NSRunLoop sources that repaint status items are drained.  Running the
        loop for 0 s is non-blocking and safe to call on every Tk tick.
        """
        if sys.platform != "darwin" or self._quitting or self._mac_status_item is None:
            return
        try:
            from Foundation import NSRunLoop, NSDate  # type: ignore
            NSRunLoop.mainRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.0))
        except Exception:
            pass
        if not self._quitting and self._mac_status_item is not None:
            self.root.after(200, self._pump_macos_runloop)

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
            except OSError:
                continue
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

    def _install_macos_app_delegate(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            import objc  # type: ignore
            from Foundation import NSObject  # type: ignore
            from AppKit import NSApplication  # type: ignore

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

            delegate = _AppDelegate.alloc().initWithApp_(app_ref)
            NSApplication.sharedApplication().setDelegate_(delegate)
            self._mac_app_delegate = delegate  # prevent GC
            self._record_tray_diag("Installed macOS app delegate for Dock reopen.")
            _append_startup_log("Installed macOS app delegate for Dock reopen.")
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
        style.configure("TButton",       font=_ttk_font(10, "bold"), padding=8)
        style.configure("Primary.TButton",  background=t["accent"], foreground=t["accent_text"])
        style.map("Primary.TButton",        background=[("active", t["accent"]), ("pressed", t["accent"])])
        style.configure("Ready.TButton",    background=t["accent"], foreground=t["accent_text"],
                        font=_ttk_font(15, "bold"), padding=(60, 18))
        style.map("Ready.TButton",          background=[("active", t["accent"]), ("pressed", t["accent"])])
        style.configure("Preview.TButton",  background=t["accent"], foreground=t["accent_text"],
                        font=_ttk_font(10), padding=(4, 1))
        style.map("Preview.TButton",        background=[("active", t["accent"])])

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        settings_menu = tk.Menu(menu, tearoff=0)
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
        help_menu.add_command(label="Connection log...", command=self._open_connection_log_window)
        help_menu.add_command(label="Windows Tray Icon Setup...", command=self._show_windows_tray_help)
        help_menu.add_separator()
        help_menu.add_command(label="Stop Network", command=self.stop_network)
        help_menu.add_command(label="Start Network", command=self.start_network)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about_dialog)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def _show_about_dialog(self) -> None:
        messagebox.showinfo(
            f"About {APP_TITLE}",
            f"{APP_TITLE}\n"
            f"Version {APP_VERSION}\n\n"
            f"For support questions, contact:\n"
            f"support@fieldcrestdental.com\n\n"
            f"Please allow 24-48 hours for a response.",
        )

    def _autostart_windows_bat_path(self) -> str:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        else:
            startup_dir = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        return os.path.join(startup_dir, "Start Chairside Ready Alert.bat")

    def _autostart_macos_plist_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.chairside.readyalert.autostart.plist")

    def _is_autostart_enabled(self) -> bool:
        if sys.platform == "darwin":
            return os.path.isfile(self._autostart_macos_plist_path())
        if sys.platform.startswith("win"):
            return os.path.isfile(self._autostart_windows_bat_path())
        return False

    def _sync_autostart_state(self) -> None:
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
        line = f"{now_str()}  {text}"
        self._tray_diag_lines.append(line)
        if self._tray_diag_text is not None and self._tray_diag_text.winfo_exists():
            self._tray_diag_text.configure(state="normal")
            self._tray_diag_text.insert("end", line + "\n")
            self._tray_diag_text.see("end")
            self._tray_diag_text.configure(state="disabled")

    def _open_tray_diagnostics_window(self) -> None:
        if self._tray_diag_win is not None and self._tray_diag_win.winfo_exists():
            self._tray_diag_win.lift()
            self._tray_diag_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("Tray diagnostics")
        win.geometry("700x360")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Tray/menu bar status and fallback controls.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x", pady=(0, 6))
        ttk.Button(btn_row, text="Retry Tray Start", command=self._ensure_tray_icon_started).pack(side="left")
        ttk.Button(btn_row, text="Show Main", command=self._show_main_window).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Hide Main", command=self._hide_main_window).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="Send Ready", command=self._send_ready_from_widget).pack(side="left", padx=(6, 0))
        if not IS_FROZEN:
            ttk.Button(btn_row, text="Check Updates", command=self._check_for_updates_clicked).pack(side="left", padx=(6, 0))

        text = tk.Text(frame, height=14, wrap="word", font=_ui_font(9))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for line in self._tray_diag_lines:
            text.insert("end", line + "\n")
        text.see("end")
        text.configure(state="disabled")

        def on_close() -> None:
            self._tray_diag_win = None
            self._tray_diag_text = None
            win.destroy()

        self._tray_diag_win = win
        self._tray_diag_text = text
        win.protocol("WM_DELETE_WINDOW", on_close)

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
                raise ValueError(f"Unsafe update path in manifest: {rp!r}")
            if rp not in UPDATE_ALLOWED_FILES:
                raise ValueError(f"Update path is not allowed: {rp!r}")
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
        add_frame = ttk.Frame(win, padding=(12, 12, 12, 6))
        add_frame.pack(fill="x")
        ttk.Label(add_frame, text="Computer name:").grid(row=0, column=0, columnspan=2, sticky="w")
        entry_var = tk.StringVar()
        entry = ttk.Entry(add_frame, textvariable=entry_var, width=28)
        entry.grid(row=1, column=0, sticky="we", pady=(4, 0))
        add_btn = ttk.Button(add_frame, text="Add", state="disabled")
        add_btn.grid(row=1, column=1, sticky="e", padx=(8, 0), pady=(4, 0))
        add_frame.columnconfigure(0, weight=1)

        # Middle: listbox of computers.
        list_frame = ttk.Frame(win, padding=(12, 6, 12, 6))
        list_frame.pack(fill="both", expand=True)
        ttk.Label(list_frame, text="Computers:").pack(anchor="w")
        list_inner = ttk.Frame(list_frame)
        list_inner.pack(fill="both", expand=True, pady=(4, 0))
        listbox = tk.Listbox(list_inner, selectmode="single", height=10, exportselection=False,
                             activestyle="dotbox")
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_inner, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        for label in self._effective_station_labels():
            listbox.insert("end", label)

        # Bottom: Remove on the left, Close on the right.
        bottom = ttk.Frame(win, padding=(12, 6, 12, 12))
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
        s.configure("Card.TLabel",    background=card, foreground=text_c, font=_ttk_font(10))
        s.configure("Title.TLabel",   background=bg,   foreground=title_c, font=_ttk_font(13, "bold"))
        s.configure("CardTitle.TLabel", background=card, foreground=text_c, font=_ttk_font(10, "bold"))
        s.configure("Subtitle.TLabel",  background=bg,  foreground=sub_c, font=_ttk_font(10))
        s.configure("TCheckbutton",   background=card, foreground=text_c)
        s.map("TCheckbutton",         background=[("active", card)])
        s.configure("TScale",         background=card, troughcolor=slider)
        s.configure("TCombobox",      fieldbackground=input_bg, foreground=text_c, background=card)
        s.map("TCombobox",
              fieldbackground=[("readonly", input_bg), ("disabled", input_bg)],
              foreground=[("readonly", text_c)])
        s.configure("TEntry",         fieldbackground=input_bg, foreground=text_c)
        s.configure("Primary.TButton",  background=accent, foreground=atext)
        s.map("Primary.TButton",        background=[("active", accent), ("pressed", accent)])
        s.configure("Ready.TButton",    background=accent, foreground=atext)
        s.map("Ready.TButton",          background=[("active", accent), ("pressed", accent)])
        s.configure("Preview.TButton",  background=accent, foreground=atext)
        s.map("Preview.TButton",        background=[("active", accent)])

        if hasattr(self, "_log_panel"):
            self._log_panel.update_theme(
                theme["log_bg"], theme["log_text"], theme.get("card_border"), theme["card_bg"]
            )
        if hasattr(self, "status_label"):
            self.status_label.configure(fg=theme["status"], bg=card, font=_ui_font(9, "bold"))
        if hasattr(self, "duplicate_name_label"):
            warn_color = "#b45309" if self._duplicate_name_detected else theme["sub"]
            self.duplicate_name_label.configure(fg=warn_color, bg=card, font=_ui_font(8))
        if hasattr(self, "_ready_wrap"):
            self._ready_wrap.configure(bg=card)
        if hasattr(self, "_ready_center"):
            self._ready_center.configure(bg=card)
        if self._tray_icon is not None:
            try:
                self._tray_icon.icon = self._create_tray_icon_image()
            except Exception:
                pass
        for c in self._cards:
            c.update_colors(card, bg, cborder)
        for btn in self._buttons:
            btn.update_colors(accent, atext, card)
        # Update sound_row and targets_frame direct-color tk frames
        if hasattr(self, "sound_combo"):
            self.sound_combo.master.configure(bg=card)
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
        self.label_var = tk.StringVar(value="Room 1")
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.alert_sound_var = tk.StringVar(value=ALERT_SOUND_OPTIONS[0])
        self.alert_volume_var = tk.IntVar(value=70)

        t = self._current_theme
        cbg, abg = t["card_bg"], t["bg"]
        cborder = t.get("card_border")

        main = tk.Frame(self.root, bg=abg, padx=16, pady=16)
        main.pack(fill="both", expand=True)

        header = tk.Frame(main, bg=abg)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Chairside Ready Alert", style="Title.TLabel").pack(anchor="w")

        # Top row: Station Setup | Ready Messages
        top_row = tk.Frame(main, bg=abg)
        top_row.pack(fill="x", pady=(0, 10))
        self._main_shell_frames = (main, header, top_row)

        # Station Setup — compact left card
        _lc = RoundedCard(top_row, bg=cbg, outer_bg=abg, radius=12, padding=14, border=cborder)
        _lc.pack(side="left", fill="y", padx=(0, 8))
        self._cards.append(_lc)
        left = _lc.inner_frame

        ttk.Label(left, text="Station Setup", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(left, text="Station name:", style="Card.TLabel").pack(anchor="w", pady=(0, 2))
        # Readonly: free-text typing was redundant with the new "Add or Remove
        # Computers..." dialog and led to drift between local typed names and the
        # network-shared list. Users now pick from the dropdown only.
        self.label_combo = ttk.Combobox(left, textvariable=self.label_var,
                                         values=list(self._effective_station_labels()),
                                         width=22, state="readonly")
        self.label_combo.pack(fill="x")
        self.label_combo.bind("<<ComboboxSelected>>", self._on_station_label_selected)
        self.duplicate_name_var = tk.StringVar(value="")
        self.duplicate_name_label = tk.Label(
            left,
            textvariable=self.duplicate_name_var,
            fg=t["sub"],
            bg=cbg,
            font=_ui_font(8),
            anchor="w",
            justify="left",
        )
        self.duplicate_name_label.pack(anchor="w", pady=(4, 0))

        ttk.Label(left, text="Alert Sound:", style="Card.TLabel").pack(anchor="w", pady=(12, 2))
        sound_row = tk.Frame(left, bg=cbg)
        sound_row.pack(fill="x")
        self.sound_combo = ttk.Combobox(
            sound_row, textvariable=self.alert_sound_var, values=ALERT_SOUND_OPTIONS, state="readonly", width=18
        )
        self.sound_combo.pack(side="left", fill="x", expand=True)
        prev_btn = RoundedButton(
            sound_row, text="▶", bg=t["accent"], fg=t["accent_text"],
            radius=6, padx=9, pady=3, font=_ui_font(10),
            command=lambda: self._play_alert_sound(
                self.alert_sound_var.get(), int(self.alert_volume_var.get())
            )
        )
        prev_btn.pack(side="left", padx=(4, 0))
        self._buttons.append(prev_btn)

        ttk.Label(left, text="Alert Volume:", style="Card.TLabel").pack(anchor="w", pady=(10, 2))
        self._volume_scale = ttk.Scale(left, from_=0, to=100, variable=self.alert_volume_var, orient="horizontal")
        self._volume_scale.pack(fill="x")
        self._volume_scale.bind("<ButtonRelease-1>", lambda _e: self._persist_form())

        # Ready Messages — expanding right card
        _rc = RoundedCard(top_row, bg=cbg, outer_bg=abg, radius=12, padding=14, border=cborder)
        _rc.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._cards.append(_rc)
        right = _rc.inner_frame

        self.status_var = tk.StringVar(value="Starting…")
        self.status_label = tk.Label(right, textvariable=self.status_var,
                                      fg=t["status"], bg=cbg,
                                      font=_ui_font(9, "bold"))
        self.status_label.pack(anchor="w", pady=(0, 6))

        ttk.Label(right, text="Ready Messages", style="CardTitle.TLabel").pack(anchor="w")

        self._log_panel = RoundedLogPanel(
            right,
            log_bg=t["log_bg"],
            log_fg=t["log_text"],
            border=cborder or "",
            card_bg=cbg,
            font=_ui_font(10),
        )
        self._log_panel.pack(fill="both", expand=True, pady=(8, 0))
        self.log = self._log_panel.log
        self.log.configure(state="disabled")

        # Ready button card (layout_preview: .ready-wrap centered, .ready-b ~80% width)
        _rbc = RoundedCard(main, bg=cbg, outer_bg=abg, radius=12, padding=20, border=cborder)
        _rbc.pack(fill="x", pady=(0, 10))
        self._cards.append(_rbc)
        # Full-width row; inner frame pack(anchor="center") centers the button in the window.
        self._ready_wrap = tk.Frame(_rbc.inner_frame, bg=cbg)
        self._ready_wrap.pack(fill="x")
        self._ready_center = tk.Frame(self._ready_wrap, bg=cbg)
        self._ready_center.pack(anchor="center")
        ready_btn = RoundedButton(
            self._ready_center, text="Ready",
            bg=t["accent"], fg=t["accent_text"],
            radius=10, padx=20, pady=12,
            font=_ui_font(15, "bold"),
            command=self.send_ready_selected,
        )
        ready_btn.pack()
        # Taller hit-area: 125% of natural (font + padding) height; width still from row (80% rule).
        _ready_natural_h = int(ready_btn["height"])
        ready_btn_height = max(1, int(round(_ready_natural_h * 1.25)))

        def _size_ready_btn(e: tk.Event) -> None:
            if e.widget != self._ready_wrap:
                return
            rw = int(e.width)
            if rw > 40:
                ready_btn.set_canvas_size(max(160, int(rw * 0.80)), ready_btn_height)

        self._ready_wrap.bind("<Configure>", _size_ready_btn)
        self._buttons.append(ready_btn)

        # Send Message card
        _mc = RoundedCard(main, bg=cbg, outer_bg=abg, radius=12, padding=14, border=cborder)
        _mc.pack(fill="both", expand=True)
        self._cards.append(_mc)
        msg = _mc.inner_frame

        ttk.Label(msg, text="Send Message", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        self.targets_frame = tk.Frame(msg, bg=cbg)
        self.targets_frame.pack(fill="x")
        self._refresh_target_checkboxes([])

    def _refresh_lan_status_banner(self) -> None:
        """Main status line: LAN summary only (not per-socket connect/disconnect noise)."""
        if not self._network_running:
            self.status_var.set("Not connected")
            return
        if self.online_labels:
            self.status_var.set(f"Online: {', '.join(self.online_labels)}")
        else:
            self.status_var.set("Waiting for other workstations…")

    def _manual_peer_ip_list(self) -> list[str]:
        raw = self.config_store.data.get("manual_peer_ips", "")
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    def _load_config_into_form(self) -> None:
        data = self.config_store.data
        label = data.get("label", "Room 1")
        self.label_var.set(label)
        self.port_var.set(str(data.get("server_port", DEFAULT_PORT)))
        theme_name = data.get("theme", DEFAULT_THEME)
        if theme_name not in THEMES:
            self.config_store.data["theme"] = DEFAULT_THEME
            self.config_store.save()
        saved_sound = str(data.get("alert_sound", "") or "").strip()
        default_sound = DEFAULT_STATION_SOUNDS.get(label, ALERT_SOUND_OPTIONS[0])
        if saved_sound and saved_sound in ALERT_SOUND_OPTIONS:
            self.alert_sound_var.set(saved_sound)
        else:
            self.alert_sound_var.set(default_sound)
        self.alert_volume_var.set(int(data.get("alert_volume", 70)))
        self.label_combo["values"] = list(self._effective_station_labels())
        self._target_signature = None
        self._last_committed_label = self.label_var.get().strip()
        self._refresh_target_checkboxes(list(self.online_labels))
        self._apply_default_target_selection(force=True)
        self._apply_theme(data.get("theme", DEFAULT_THEME))

    def _write_form_to_data(self) -> None:
        """Copy current UI values into config_store.data (does not write to disk)."""
        self.config_store.data["label"] = self.label_var.get().strip()
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
        self.start_network()

    def _open_network_settings_window(self) -> None:
        t = self._current_theme
        win = tk.Toplevel(self.root)
        win.title("Network Settings")
        win.geometry("420x260")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.configure(bg=t["bg"])

        frame = tk.Frame(win, bg=t["bg"], padx=12, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text=f"Detected LAN IP: {self.local_ip}",
                 bg=t["bg"], fg=t["sub"], font=_ui_font(9)).pack(anchor="w", pady=(0, 10))

        port_var = tk.StringVar(value=self.port_var.get())
        manual_var = tk.StringVar(value=str(self.config_store.data.get("manual_peer_ips", "")))

        tk.Label(frame, text="TCP port (same on every computer):",
                 bg=t["bg"], fg=t["text"], font=_ui_font(9)).pack(anchor="w", pady=(0, 2))
        ttk.Entry(frame, textvariable=port_var).pack(fill="x", pady=(0, 10))
        tk.Label(
            frame,
            text="Optional: extra peer IP addresses (comma-separated) if LAN discovery is blocked:",
            bg=t["bg"], fg=t["sub"], font=_ui_font(9),
            wraplength=380, justify="left",
        ).pack(anchor="w", pady=(0, 2))
        ttk.Entry(frame, textvariable=manual_var).pack(fill="x", pady=(0, 8))

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
        buttons.pack(fill="x", pady=(12, 0))
        RoundedButton(buttons, text="Save", command=save_settings,
                      bg=t["accent"], fg=t["accent_text"]).pack(side="right")
        RoundedButton(buttons, text="Cancel", command=win.destroy,
                      bg=t["card_bg"], fg=t["text"]).pack(side="right", padx=(0, 8))

    def _on_station_label_selected(self, _event=None) -> None:
        self._commit_station_label_if_changed()

    def _commit_station_label_if_changed(self) -> None:
        # Combobox is readonly — the value is always one of the dropdown entries
        # already in station_labels. Nothing to validate or auto-add.
        new = self.label_var.get().strip()
        if not new:
            return
        if new == self._last_committed_label:
            return
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
        if self._network_running:
            self.stop_network()
            self.start_network()
        else:
            self._set_duplicate_name_warning(False)

    def _set_duplicate_name_warning(self, is_duplicate: bool) -> None:
        self._duplicate_name_detected = bool(is_duplicate)
        if not hasattr(self, "duplicate_name_var"):
            return
        if self._duplicate_name_detected:
            self.duplicate_name_var.set("Another computer may be using the same station name.")
        else:
            self.duplicate_name_var.set("")
        if hasattr(self, "duplicate_name_label"):
            t = self._current_theme
            warn_color = "#b45309" if self._duplicate_name_detected else t["sub"]
            self.duplicate_name_label.configure(fg=warn_color, bg=t["card_bg"], font=_ui_font(8))

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
        labels = sorted({str(x).strip() for x in labels if str(x).strip()})
        old_states = {label: var.get() for label, var in self.target_vars.items()}
        me = self.label_var.get().strip()
        signature = (me, tuple(labels))
        if signature == self._target_signature:
            return
        self._target_signature = signature
        for child in self.targets_frame.winfo_children():
            child.destroy()
        self.target_vars.clear()
        filtered = [label for label in labels if label != me]
        default_targets = set(self.config_store.data.get("default_targets", []))
        cbg = self._current_theme["card_bg"]
        ctxt = self._current_theme["text"]
        csub = self._current_theme["sub"]
        if not filtered:
            tk.Label(
                self.targets_frame,
                text="No other stations online yet. Names appear here after this computer sees them on the LAN.",
                wraplength=520, bg=cbg, fg=csub, font=_ui_font(10),
            ).pack(anchor="w")
            for column in range(3):
                self.targets_frame.grid_columnconfigure(column, weight=1)
            self._rebuild_default_menu()
            return
        column_count = 3
        for i, label in enumerate(filtered):
            checked = (label in default_targets) or old_states.get(label, False)
            var = tk.BooleanVar(value=checked)
            self.target_vars[label] = var
            row = i // column_count
            column = i % column_count
            tk.Checkbutton(
                self.targets_frame, text=label, variable=var,
                bg=cbg, fg=ctxt, activebackground=cbg, activeforeground=ctxt,
                selectcolor=cbg, relief="flat", font=_ui_font(10),
            ).grid(row=row, column=column, sticky="w", padx=8, pady=3)
        for column in range(column_count):
            self.targets_frame.grid_columnconfigure(column, weight=1)
        self._rebuild_default_menu()

    def _append_diag(self, text: str) -> None:
        line = f"[{now_str()}] {text}"
        self._diag_lines.append(line)
        if self._diag_text is not None and self._diag_text.winfo_exists():
            self._diag_text.configure(state="normal")
            self._diag_text.insert("end", line + "\n")
            self._diag_text.see("end")
            self._diag_text.configure(state="disabled")

    def _append_ready_log(self, text: str) -> None:
        """Main panel: only Ready traffic (timestamp + text)."""
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_connection_log_window(self) -> None:
        if self._diag_win is not None and self._diag_win.winfo_exists():
            self._diag_win.lift()
            self._diag_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("Connection log")
        win.geometry("640x320")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Listeners, discovery, and TCP status (not shown on the main screen).",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 6))
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
        self._sync_after_id = None
        if not self._network_running or not self.discovery:
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
        self._schedule_sync_loop()

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
        if not label:
            messagebox.showerror(APP_TITLE, "Please choose a label for this computer.")
            return

        self.local_ips = local_ipv4_addresses()
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
        self._schedule_sync_loop()
        self._refresh_lan_status_banner()

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
        client.send_chat(targets, message, self.alert_sound_var.get(), int(self.alert_volume_var.get()))
        self._append_ready_log(f"[{now_str()}] Ready → {', '.join(targets)}")

    def send_ready_all(self) -> None:
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
        client.send_chat(["ALL"], message)
        self._append_ready_log(f"[{now_str()}] Ready → All")

    def _process_ui_queue(self) -> None:
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                # Routine connect/retry/disconnect stays in Connection log only so the
                # banner stays on "On LAN" / Waiting unless we add explicit error kinds later.
                self._append_diag(str(payload))
            elif kind == "discovery":
                if self.discovery:
                    snap = self.discovery.snapshot()
                    self._merge_online_labels(snap, time.time())
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
        self.root.after(120, self._process_ui_queue)

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
            ts = payload.get("timestamp", now_str())
            me = self.label_var.get()
            targets = payload.get("to", [])
            if "ALL" in targets or me in targets:
                body = str(text).strip()
                if body.lower() != "ready":
                    self._append_diag(f"Chat from {sender} (not logged in main window): {body!r}")
                    return
                self._append_ready_log(f"[{ts}] {sender}: Ready")
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
        threading.Thread(target=self._play_alert_sound_worker, args=(alert_sound, alert_volume), daemon=True).start()

    def _play_alert_sound_worker(self, alert_sound: str = "", alert_volume: int = 70) -> None:
        try:
            sound_name = (alert_sound.strip() if alert_sound.strip() else self.alert_sound_var.get().strip())
            volume = max(0, min(100, int(alert_volume))) / 100.0
        except Exception:
            sound_name = ALERT_SOUND_OPTIONS[0]
            volume = 0.7

        sequence = self._sound_sequence(sound_name)
        if not sequence:
            self.root.bell()
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            temp_wav = tmp.name
        try:
            self._write_wave_file(temp_wav, sequence, volume)
            if sys.platform.startswith("win"):
                import winsound

                winsound.PlaySound(temp_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["afplay", "-v", f"{max(0.0, min(2.0, volume * 2.0)):.2f}", temp_wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self.root.bell()
        except Exception:
            self.root.bell()
        finally:
            threading.Thread(target=self._cleanup_temp_file_later, args=(temp_wav,), daemon=True).start()

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

    def _write_wave_file(self, path: str, sequence: list[tuple[int, int, int]], volume: float) -> None:
        max_amplitude = int(32767 * max(0.0, min(1.0, volume)))
        if max_amplitude <= 0:
            max_amplitude = 500
        samples = []
        for frequency, duration_ms, gap_ms in sequence:
            tone_frames = int(ALERT_SAMPLE_RATE * (duration_ms / 1000.0))
            for i in range(tone_frames):
                t = i / ALERT_SAMPLE_RATE
                value = int(max_amplitude * math.sin(2 * math.pi * frequency * t))
                samples.append(value)
            gap_frames = int(ALERT_SAMPLE_RATE * (gap_ms / 1000.0))
            for _ in range(gap_frames):
                samples.append(0)
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(ALERT_SAMPLE_RATE)
            frame_data = bytearray()
            for sample in samples:
                frame_data.extend(int(sample).to_bytes(2, byteorder="little", signed=True))
            wav.writeframes(bytes(frame_data))

    def _cleanup_temp_file_later(self, path: str) -> None:
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
        with open(_startup_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {line}\n")
    except Exception:
        pass


def _show_already_running_message() -> None:
    msg = (
        "Chairside Ready Alert is already running.\n\n"
        "Use the existing app window or tray/menu-bar icon."
    )
    try:
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


def _run_subscription_paywall(root: tk.Tk) -> bool:
    """Modal welcome / paywall shown before the main UI when no active
    subscription. Themed to match the app's Modern Blue look. Returns True
    if the user successfully started a trial or subscription, False if they
    chose to quit. Only called for the frozen Store build."""
    t = THEMES["Modern Blue"]

    win = tk.Toplevel(root)
    win.title("Welcome to Chairside Ready Alert")
    win.configure(bg=t["bg"])
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: None)  # X disabled — must use a button

    outcome: dict[str, bool] = {"subscribed": False, "quit": False}

    outer = tk.Frame(win, bg=t["bg"], padx=22, pady=22)
    outer.pack(fill="both", expand=True)

    card = RoundedCard(outer, bg=t["card_bg"], outer_bg=t["bg"],
                       radius=14, padding=24, border=t["card_border"])
    card.pack(fill="both", expand=True)
    body = card.inner_frame

    tk.Label(body, text="Welcome to Chairside Ready Alert",
             font=_ui_font(18, "bold"),
             fg=t["title"], bg=t["card_bg"],
             wraplength=500, justify="left").pack(anchor="w")
    tk.Label(body,
             text="Free for 7 days. $1.99 / month after — billed and managed by Microsoft Store.",
             font=_ui_font(11),
             fg=t["sub"], bg=t["card_bg"],
             wraplength=500, justify="left").pack(anchor="w", pady=(4, 16))

    tk.Label(
        body,
        text=("LAN messaging between dental workstations. One Microsoft "
              "account covers up to 10 devices. Cancel any time in Microsoft "
              "Store. Your alert messages stay on your local network — they "
              "never go through any cloud service."),
        font=_ui_font(11), fg=t["text"], bg=t["card_bg"],
        wraplength=500, justify="left",
    ).pack(anchor="w", pady=(0, 18))

    status_var = tk.StringVar(value="")
    status_lbl = tk.Label(body, textvariable=status_var, font=_ui_font(10),
                          fg=t["sub"], bg=t["card_bg"])
    status_lbl.pack(anchor="w", pady=(0, 12))

    btn_row = tk.Frame(body, bg=t["card_bg"])
    btn_row.pack(fill="x", pady=(4, 0))

    sub_btn = RoundedButton(btn_row, text="Start 7-day free trial",
                            bg=t["accent"], fg=t["accent_text"],
                            padx=20, pady=10, font=_ui_font(11, "bold"))
    restore_btn = RoundedButton(btn_row, text="Restore purchases",
                                bg=t["card_bg"], fg=t["text"], padx=14, pady=10)
    quit_btn = RoundedButton(btn_row, text="Quit",
                             bg=t["card_bg"], fg=t["text"], padx=14, pady=10)
    sub_btn.pack(side="left")
    restore_btn.pack(side="left", padx=(8, 0))
    quit_btn.pack(side="right")

    # Disable RoundedButton clicks during async Store calls by clearing
    # _command (the canvas widget has no native disabled state); restore on idle.
    saved_cmds: dict = {}

    def _busy(msg: str) -> None:
        status_var.set(msg)
        for b in (sub_btn, restore_btn, quit_btn):
            if b not in saved_cmds:
                saved_cmds[b] = b._command
            b._command = None
        win.update_idletasks()

    def _idle(msg: str = "") -> None:
        status_var.set(msg)
        for b in list(saved_cmds.keys()):
            b._command = saved_cmds.pop(b)

    def _on_subscribe() -> None:
        # The Microsoft Store purchase overlay anchors itself to the parent
        # window's HWND and requires the parent's message pump to be running
        # while the overlay renders. We MUST NOT block the Tk main thread on
        # the subprocess.run() call — doing so freezes the message pump and
        # the overlay never appears, leading to a 5-minute timeout. Run the
        # subprocess on a worker thread and poll a queue from the Tk thread.
        _busy("Opening Microsoft Store...")
        try:
            paywall_hwnd: Optional[int] = int(win.winfo_id())
        except Exception:
            paywall_hwnd = None

        result_q: queue.Queue = queue.Queue()

        def _worker() -> None:
            try:
                ok = request_subscription_purchase(hwnd=paywall_hwnd)
                result_q.put(("ok" if ok else "fail", None))
            except Exception as exc:  # pragma: no cover — defensive
                result_q.put(("error", exc))

        threading.Thread(target=_worker, daemon=True).start()

        def _poll() -> None:
            try:
                kind, exc = result_q.get_nowait()
            except queue.Empty:
                win.after(100, _poll)
                return
            if kind == "ok":
                _busy("Subscription active. Loading Chairside Ready Alert...")
                outcome["subscribed"] = True
                win.after(150, win.destroy)
            else:
                if kind == "error" and exc is not None:
                    _append_startup_log(
                        f"_on_subscribe worker raised: {type(exc).__name__}: {exc}"
                    )
                _idle("Could not start your free trial. You can try again or quit.")

        win.after(100, _poll)

    def _on_restore() -> None:
        _busy("Checking subscription...")
        if is_subscribed():
            _busy("Subscription active. Loading Chairside Ready Alert...")
            outcome["subscribed"] = True
            win.after(150, win.destroy)
        else:
            _idle("No active subscription found on this Microsoft account.")

    def _on_quit() -> None:
        outcome["quit"] = True
        win.destroy()

    sub_btn._command = _on_subscribe
    restore_btn._command = _on_restore
    quit_btn._command = _on_quit

    # Set an explicit size — RoundedCard sizes its width reactively to its
    # parent, so without this Tk picks a small default that clips the title
    # and wrapped body text. Width is sized to fit the wraplength=500 labels
    # plus outer (22) + card (24) padding on both sides. Height has slack
    # for slightly different font metrics across systems.
    WIN_W, WIN_H = 600, 460
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{WIN_W}x{WIN_H}+{(sw - WIN_W) // 2}+{(sh - WIN_H) // 3}")

    # Force the paywall to the foreground; transient(root) is intentionally
    # NOT called because it would suppress the taskbar entry, leaving users
    # with no way to find the window if it ends up behind another app.
    win.deiconify()
    win.lift()
    try:
        win.focus_force()
    except tk.TclError:
        pass
    win.attributes("-topmost", True)
    win.after(500, lambda: win.attributes("-topmost", False))

    try:
        win.grab_set()
    except tk.TclError:
        pass

    root.wait_window(win)
    return outcome["subscribed"]


def main() -> None:
    _append_startup_log("starting main()")
    lock_path = os.path.join(_user_data_dir(), INSTANCE_LOCK_FILE)
    lock = SingleInstanceLock(lock_path)
    if not lock.acquire():
        _append_startup_log("Second launch blocked by single-instance lock.")
        if _notify_existing_instance_focus():
            return
        # If focus notify fails, lock may be stale after an unclean shutdown.
        _append_startup_log("Focus notify failed; attempting stale-lock recovery.")
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
    root = tk.Tk()
    try:
        # Microsoft Store / MSIX subscription gate. Bypassed for non-Store builds.
        if _subscription_enforced() and not is_subscribed():
            _append_startup_log("No active subscription — showing paywall.")
            root.withdraw()
            if not _run_subscription_paywall(root):
                _append_startup_log("Paywall dismissed without subscription — quitting.")
                return
            root.deiconify()
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
