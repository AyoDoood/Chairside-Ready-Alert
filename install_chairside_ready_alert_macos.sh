#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd -P)"
# GitHub / Safari "Open" on ZIP: this folder is often quarantined and the .command bit is
# lost — clear quarantine (best effort) and restore +x for double-click.
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$SCRIPT_DIR" 2>/dev/null || true
fi
chmod +x \
  "$SCRIPT_DIR/Install Chairside Ready Alert macOS.command" \
  "$SCRIPT_DIR/install_chairside_ready_alert_macos.sh" \
  2>/dev/null || true
SOURCE_PY="$SCRIPT_DIR/chairside_ready_alert.py"
INSTALL_DIR="$HOME/Library/Application Support/ChairsideReadyAlert"
DESKTOP_APP="$HOME/Desktop/Chairside Ready Alert.app"

# Official python.org macOS universal2 installer — includes Tcl/Tk (Tkinter works).
# Bump these when you refresh the bundled/offline package name in docs.
PY_FULL="3.12.8"
PY_MM="${PY_FULL%.*}"
# This is the interpreter we standardize on (matches the .pkg above).
PYORG_HOME="/Library/Frameworks/Python.framework/Versions/${PY_MM}"
PYORG_PYTHON3="${PYORG_HOME}/bin/python3"
PKG_BASENAME="python-${PY_FULL}-macos11.pkg"
PKG_URL="https://www.python.org/ftp/python/${PY_FULL}/${PKG_BASENAME}"
CACHE_DIR="$HOME/Library/Caches/ChairsideReadyAlert"

echo "[Chairside Ready Alert] Installer"
echo ""

if [[ ! -f "$SOURCE_PY" ]]; then
  echo "Error: chairside_ready_alert.py not found next to this installer."
  echo "Keep this .command file in the same folder as chairside_ready_alert.py."
  read -r
  exit 1
fi

TK_SMOKE='import tkinter as tk
r = tk.Tk()
r.withdraw()
r.update_idletasks()
r.destroy()'

is_apple_clt_python() {
  case "$1" in
    *CommandLineTools*|*/Library/Developer/*|*Xcode.app/Contents/Developer*)
      return 0
      ;;
  esac
  return 1
}

try_python() {
  local candidate="$1"
  [[ -z "$candidate" || ! -x "$candidate" ]] && return 1
  local real
  real="$("$candidate" -c "import sys; print(sys.executable)" 2>/dev/null)" || return 1
  [[ -z "$real" || ! -x "$real" ]] && return 1
  if is_apple_clt_python "$real"; then
    return 1
  fi
  if ! "$real" -c "$TK_SMOKE" &>/dev/null; then
    return 1
  fi
  PYTHON3="$real"
  return 0
}

find_working_python() {
  PYTHON3=""

  # Prefer the python.org framework for PY_MM (same family as PKG_BASENAME / reliable Tk).
  if [[ -x "$PYORG_PYTHON3" ]]; then
    if try_python "$PYORG_PYTHON3"; then
      return 0
    fi
  fi

  if command -v brew &>/dev/null; then
    for v in 3.13 3.12 3.11 3.10; do
      bp="$(brew --prefix "python@${v}" 2>/dev/null)" || continue
      [[ -d "$bp" ]] || continue
      for name in "python${v}" python3; do
        p="${bp}/bin/${name}"
        if try_python "$p"; then
          return 0
        fi
      done
    done
  fi

  for v in 3.13 3.12 3.11; do
    for candidate in \
      "/opt/homebrew/opt/python@${v}/bin/python${v}" \
      "/opt/homebrew/opt/python@${v}/bin/python3" \
      "/usr/local/opt/python@${v}/bin/python${v}" \
      "/usr/local/opt/python@${v}/bin/python3"; do
      if try_python "$candidate"; then
        return 0
      fi
    done
  done

  for candidate in \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 \
    /usr/local/bin/python3; do
    if try_python "$candidate"; then
      return 0
    fi
  done

  for cmd in python3.13 python3.12 python3.11 python3.10; do
    p="$(command -v "$cmd" 2>/dev/null || true)"
    if try_python "$p"; then
      return 0
    fi
  done

  shopt -s nullglob
  pyorg=(/Library/Frameworks/Python.framework/Versions/*/bin/python3)
  shopt -u nullglob
  for (( i = ${#pyorg[@]} - 1; i >= 0; i-- )); do
    [[ "${pyorg[$i]}" == "$PYORG_PYTHON3" ]] && continue
    if try_python "${pyorg[$i]}"; then
      return 0
    fi
  done

  path_py="$(command -v python3 2>/dev/null || true)"
  if try_python "$path_py"; then
    return 0
  fi

  return 1
}

# Return 0 if interpreter reports the expected minor line (e.g. 3.12.x matches PY_MM), else 1.
python_reports_expected_minor() {
  local got
  got="$("$1" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null)" || return 1
  [[ "$got" == "$PY_MM" ]]
}

install_python_org() {
  local PKG_SRC=""

  if [[ -f "$SCRIPT_DIR/$PKG_BASENAME" ]]; then
    PKG_SRC="$SCRIPT_DIR/$PKG_BASENAME"
    echo "Using installer package next to this script (offline)."
  elif [[ -f "$CACHE_DIR/$PKG_BASENAME" ]]; then
    PKG_SRC="$CACHE_DIR/$PKG_BASENAME"
    echo "Using cached installer: $PKG_SRC"
  else
    echo ""
    echo "No usable Python with Tkinter was found."
    echo "Downloading official Python ${PY_FULL} for macOS (~45 MB) from python.org …"
    echo "(This build includes Tcl/Tk — required for this app.)"
    echo ""
    mkdir -p "$CACHE_DIR"
    local tmp="${CACHE_DIR}/${PKG_BASENAME}.part"
    rm -f "$tmp"
    if ! curl -fL --progress-bar -o "$tmp" "$PKG_URL"; then
      rm -f "$tmp"
      echo ""
      echo "Download failed. Check your network, or place this file next to the installer:"
      echo "  $PKG_BASENAME"
      echo "Download from: https://www.python.org/downloads/macos/"
      return 1
    fi
    mv -f "$tmp" "$CACHE_DIR/$PKG_BASENAME"
    PKG_SRC="$CACHE_DIR/$PKG_BASENAME"
  fi

  echo ""
  echo "Installing Python into /Library/Frameworks/Python.framework/ (Apple standard)."
  echo "You will be prompted for your Mac administrator password."
  echo ""
  if ! sudo installer -pkg "$PKG_SRC" -target /; then
    echo ""
    echo "Python installation was cancelled or failed."
    return 1
  fi
  return 0
}

stop_running_chairside() {
  echo "Checking for running Chairside Ready Alert..."
  local pids
  pids="$(pgrep -f "chairside_ready_alert.py" || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi

  echo "Closing running Chairside Ready Alert before install..."
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    kill -TERM "$pid" >/dev/null 2>&1 || true
  done <<< "$pids"

  local deadline=$((SECONDS + 12))
  while [[ $SECONDS -lt $deadline ]]; do
    if ! pgrep -f "chairside_ready_alert.py" >/dev/null 2>&1; then
      echo "Chairside Ready Alert closed."
      return 0
    fi
    sleep 0.3
  done

  echo "Warning: app still running after 12s; forcing close..."
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    kill -KILL "$pid" >/dev/null 2>&1 || true
  done <<< "$(pgrep -f "chairside_ready_alert.py" || true)"
  sleep 0.5
}

stop_running_chairside

find_working_python || true

# Install python.org into /Library/Frameworks when it is missing, even if Homebrew Python
# would work — same Tk + same minor everywhere, and the Desktop .app points at one path.
need_python_org=false
if [[ -z "${PYTHON3:-}" ]]; then
  need_python_org=true
elif [[ "$PYTHON3" != "$PYORG_PYTHON3" ]] && [[ ! -x "$PYORG_PYTHON3" ]]; then
  need_python_org=true
fi

if [[ "$need_python_org" == true ]]; then
  echo ""
  echo "This app is tested with official Python ${PY_FULL} from python.org (in ${PYORG_HOME})."
  echo "Installing or completing that install avoids wrong interpreters (e.g. Homebrew-only)."
  echo ""
  if ! install_python_org; then
    if [[ -z "${PYTHON3:-}" ]]; then
      read -r
      exit 1
    fi
    echo ""
    echo "Continuing with: $PYTHON3 (official Python ${PY_FULL} was not installed)."
  else
    PYTHON3=""
    if ! find_working_python; then
      echo ""
      echo "Python was installed, but Tkinter still did not pass the self-test."
      echo "Try logging out and back in, then run this installer again."
      read -r
      exit 1
    fi
  fi
fi

# If Python is not ${PY_MM}.x (e.g. 3.11), offer the official .pkg. We do not force an exact patch (3.12.7 vs 3.12.8).
if [[ -n "${PYTHON3:-}" ]] && ! python_reports_expected_minor "$PYTHON3"; then
  echo ""
  echo "The active Python is not ${PY_MM}.x (this app targets python.org ${PY_MM} from ${PYORG_HOME})."
  echo "Installing or updating via the official package …"
  if install_python_org; then
    PYTHON3=""
    if ! find_working_python; then
      echo ""
      echo "Python was updated, but Tkinter did not pass the self-test."
      echo "Try logging out and back in, then run this installer again."
      read -r
      exit 1
    fi
  else
    echo ""
    echo "Could not install ${PY_MM}.x. Continuing with: $PYTHON3"
  fi
fi

echo "Using Python (Tk OK): $PYTHON3"
py_report="$("$PYTHON3" -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>/dev/null || echo "unknown")"
echo "Python reports: $py_report (want ${PY_MM}.x; installer ships ${PY_FULL})"
case "$py_report" in
  unknown) ;;
  "$PY_MM".*|"$PY_MM")
    ;;
  *)
    echo ""
    echo "Warning: Expected ${PY_MM}.x from python.org — if the app fails, install Python ${PY_FULL} or run this installer again."
    ;;
esac
echo "Installing / verifying app dependencies (pystray, pillow, cairosvg, certifi) ..."
if ! "$PYTHON3" -m pip install --disable-pip-version-check --user --upgrade pystray pillow cairosvg certifi >/dev/null 2>&1; then
  echo "Warning: could not install pystray/pillow/cairosvg/certifi; app may still run with reduced features."
fi

if "$PYTHON3" -c "from AppKit import NSApplication" >/dev/null 2>&1; then
  echo "pyobjc-framework-Cocoa: already present."
else
  echo "pyobjc-framework-Cocoa not found — installing (macOS Dock + menu bar integration) ..."
  if ! "$PYTHON3" -m pip install --disable-pip-version-check --user --upgrade pyobjc-framework-Cocoa; then
    echo "Warning: could not install pyobjc-framework-Cocoa."
    echo "         Install manually: \"$PYTHON3\" -m pip install --user pyobjc-framework-Cocoa"
  elif ! "$PYTHON3" -c "from AppKit import NSApplication" >/dev/null 2>&1; then
    echo "Warning: pyobjc-framework-Cocoa installed but AppKit import still failed."
  fi
fi

echo "Installing app to: $INSTALL_DIR"

mkdir -p "$INSTALL_DIR"
cp -f "$SOURCE_PY" "$INSTALL_DIR/chairside_ready_alert.py"
for logo in logo.svg Logo.svg logo.png Logo.png; do
  if [[ -f "$SCRIPT_DIR/$logo" ]]; then
    cp -f "$SCRIPT_DIR/$logo" "$INSTALL_DIR/$logo"
  fi
done
for support in \
  "Install Chairside Ready Alert macOS.command" \
  "install_chairside_ready_alert_macos.sh" \
  "Install Chairside Ready Alert.bat" \
  "install_chairside_ready_alert.ps1" \
  "README-Windows-One-Click.txt" \
  "version.json" \
  "version.json.example"; do
  if [[ -f "$SCRIPT_DIR/$support" ]]; then
    cp -f "$SCRIPT_DIR/$support" "$INSTALL_DIR/$support"
  fi
done
printf '%s\n' "$PYTHON3" > "$INSTALL_DIR/python_interpreter_path.txt"
printf '%s\n' "$PY_FULL" > "$INSTALL_DIR/python_expected_version.txt"
printf '%s\n' "$py_report" > "$INSTALL_DIR/python_installed_version.txt"

# Desktop .app: always exec the framework bin/python3 (or chosen PYTHON3) with -u — no "open",
# no Python.app/MacOS/Python (those caused flash-quit and inconsistent behavior).
LAUNCH_BIN="$PYTHON3"
case "$PYTHON3" in
  "${PYORG_HOME}/bin/"*)
    LAUNCH_BIN="$PYORG_PYTHON3"
    ;;
esac
printf '%s\n' "$LAUNCH_BIN" > "$INSTALL_DIR/python_desktop_launcher_path.txt"

# Remove older Desktop launchers from previous installs (avoid duplicates).
rm -f "$HOME/Desktop/Chairside Ready Alert.command"
rm -rf "$HOME/Desktop/Chairside Ready Alert Shortcut.app"
rm -rf "$DESKTOP_APP"

# Minimal .app bundle: double-click shows a normal app icon (no Terminal window).
APP_MACOS="$DESKTOP_APP/Contents/MacOS"
APP_RES="$DESKTOP_APP/Contents/Resources"
mkdir -p "$APP_MACOS" "$APP_RES"

generate_chairside_icns() {
  local out_icns="$1"
  local tmp_dir iconset src_png
  tmp_dir="$(mktemp -d 2>/dev/null || true)"
  [[ -n "$tmp_dir" && -d "$tmp_dir" ]] || return 1
  iconset="$tmp_dir/AppIcon.iconset"
  src_png="$tmp_dir/source.png"
  mkdir -p "$iconset" || { rm -rf "$tmp_dir"; return 1; }

  if ! "$PYTHON3" - "$src_png" <<'PY'
import sys
from PIL import Image, ImageDraw

out = sys.argv[1]
size = 1024
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
accent = "#2563eb"
fg = "#ffffff"
pad = 120
d.rounded_rectangle((pad, pad, size - pad, size - pad), radius=180, fill=accent)
d.text((size // 2, size // 2), "R", fill=fg, anchor="mm")
img.save(out, "PNG")
PY
  then
    rm -rf "$tmp_dir"
    return 1
  fi

  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$src_png" --out "$iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
  done
  sips -z 32 32   "$src_png" --out "$iconset/icon_16x16@2x.png" >/dev/null 2>&1 || true
  sips -z 64 64   "$src_png" --out "$iconset/icon_32x32@2x.png" >/dev/null 2>&1 || true
  sips -z 256 256 "$src_png" --out "$iconset/icon_128x128@2x.png" >/dev/null 2>&1 || true
  sips -z 512 512 "$src_png" --out "$iconset/icon_256x256@2x.png" >/dev/null 2>&1 || true
  cp -f "$src_png" "$iconset/icon_512x512@2x.png" >/dev/null 2>&1 || true

  if ! iconutil -c icns "$iconset" -o "$out_icns" >/dev/null 2>&1; then
    rm -rf "$tmp_dir"
    return 1
  fi
  rm -rf "$tmp_dir"
  return 0
}

generate_icns_from_svg() {
  local svg_path="$1"
  local out_icns="$2"
  local tmp_dir iconset src_png
  tmp_dir="$(mktemp -d 2>/dev/null || true)"
  [[ -n "$tmp_dir" && -d "$tmp_dir" ]] || return 1
  iconset="$tmp_dir/AppIcon.iconset"
  src_png="$tmp_dir/source.png"
  mkdir -p "$iconset" || { rm -rf "$tmp_dir"; return 1; }

  # sips can rasterize SVG on macOS.
  if ! sips -s format png "$svg_path" --out "$src_png" >/dev/null 2>&1; then
    rm -rf "$tmp_dir"
    return 1
  fi

  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$src_png" --out "$iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
  done
  sips -z 32 32   "$src_png" --out "$iconset/icon_16x16@2x.png" >/dev/null 2>&1 || true
  sips -z 64 64   "$src_png" --out "$iconset/icon_32x32@2x.png" >/dev/null 2>&1 || true
  sips -z 256 256 "$src_png" --out "$iconset/icon_128x128@2x.png" >/dev/null 2>&1 || true
  sips -z 512 512 "$src_png" --out "$iconset/icon_256x256@2x.png" >/dev/null 2>&1 || true
  cp -f "$src_png" "$iconset/icon_512x512@2x.png" >/dev/null 2>&1 || true

  if ! iconutil -c icns "$iconset" -o "$out_icns" >/dev/null 2>&1; then
    rm -rf "$tmp_dir"
    return 1
  fi
  rm -rf "$tmp_dir"
  return 0
}

# Finder launches .app bundles with a minimal environment. Homebrew Python + Tkinter
# usually needs brew shellenv and Tcl/Tk library paths; python.org still benefits from
# framework lib on DYLD path when not started from a login shell.
{
  echo '#!/bin/bash'
  echo 'export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:${PATH:-}"'
  echo 'if [[ -d "/Library/Frameworks/Python.framework/Versions/'"${PY_MM}"'/lib" ]]; then'
  echo '  _pfl="/Library/Frameworks/Python.framework/Versions/'"${PY_MM}"'/lib"'
  echo '  export DYLD_FALLBACK_LIBRARY_PATH="${_pfl}${DYLD_FALLBACK_LIBRARY_PATH:+:${DYLD_FALLBACK_LIBRARY_PATH:-}}"'
  echo 'fi'
  echo 'if [[ -x /opt/homebrew/bin/brew ]]; then'
  echo '  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" || true'
  echo 'fi'
  echo 'if [[ -x /usr/local/bin/brew ]]; then'
  echo '  eval "$(/usr/local/bin/brew shellenv 2>/dev/null)" || true'
  echo 'fi'
  echo 'for _tk in /opt/homebrew/opt/tcl-tk/lib /usr/local/opt/tcl-tk/lib; do'
  echo '  if [[ -d "${_tk}" ]]; then'
  echo '    export DYLD_FALLBACK_LIBRARY_PATH="${_tk}${DYLD_FALLBACK_LIBRARY_PATH:+:${DYLD_FALLBACK_LIBRARY_PATH:-}}"'
  echo '  fi'
  echo 'done'
  echo 'unset PYTHONHOME 2>/dev/null || true'
  echo 'export PYTHONUNBUFFERED=1'
  echo 'export TK_SILENCE_DEPRECATION=1'
  case "$PYTHON3" in
    "${PYORG_HOME}/bin/"*)
      printf 'export PYTHONEXECUTABLE=%q\n' "${PYORG_HOME}/bin/python3"
      ;;
  esac
  printf 'cd %q\n' "$INSTALL_DIR"
  printf 'LOG=%q\n' "$INSTALL_DIR/shortcut_stderr.txt"
  echo '{ date; echo "Chairside Ready Alert — errors: shortcut_stderr.txt and startup_log.txt"; } >>"$LOG" 2>/dev/null || true'
  printf 'exec %q -u chairside_ready_alert.py 2>>"$LOG"\n' "$LAUNCH_BIN"
} > "$APP_MACOS/launcher"
chmod +x "$APP_MACOS/launcher"

# Icon: prefer custom logo.svg/Logo.svg next to installer; fallback to generated Chairside R.
ICON_DST="$APP_RES/AppIcon.icns"
CUSTOM_LOGO=""
if [[ -f "$SCRIPT_DIR/logo.svg" ]]; then
  CUSTOM_LOGO="$SCRIPT_DIR/logo.svg"
elif [[ -f "$SCRIPT_DIR/Logo.svg" ]]; then
  CUSTOM_LOGO="$SCRIPT_DIR/Logo.svg"
fi
if [[ -n "$CUSTOM_LOGO" ]]; then
  echo "Using custom logo file: $(basename "$CUSTOM_LOGO")"
  if ! generate_icns_from_svg "$CUSTOM_LOGO" "$ICON_DST"; then
    echo "Warning: custom logo conversion failed; trying generated Chairside icon."
    generate_chairside_icns "$ICON_DST" || true
  fi
else
  generate_chairside_icns "$ICON_DST" || true
fi
BUNDLED_ICON="$SCRIPT_DIR/AppIcon.icns"
PYORG_ICON="/Library/Frameworks/Python.framework/Versions/Current/Resources/Python.app/Contents/Resources/PythonInterpreter.icns"
GENERIC_ICON="/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericApplicationIcon.icns"
if [[ -f "$ICON_DST" ]]; then
  :
elif [[ -f "$BUNDLED_ICON" ]]; then
  cp -f "$BUNDLED_ICON" "$ICON_DST"
elif [[ -f "$PYORG_ICON" ]]; then
  cp -f "$PYORG_ICON" "$ICON_DST"
elif [[ -f "$GENERIC_ICON" ]]; then
  cp -f "$GENERIC_ICON" "$ICON_DST"
else
  ICON_DST=""
fi

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0">'
  echo '<dict>'
  echo '  <key>CFBundleExecutable</key>'
  echo '  <string>launcher</string>'
  echo '  <key>CFBundleIdentifier</key>'
  echo '  <string>com.chairside.messenger</string>'
  echo '  <key>CFBundleName</key>'
  echo '  <string>Chairside Ready Alert</string>'
  echo '  <key>CFBundleDisplayName</key>'
  echo '  <string>Chairside Ready Alert</string>'
  echo '  <key>CFBundlePackageType</key>'
  echo '  <string>APPL</string>'
  echo '  <key>CFBundleShortVersionString</key>'
  echo '  <string>1.0</string>'
  echo '  <key>LSMinimumSystemVersion</key>'
  echo '  <string>11.0</string>'
  echo '  <key>NSHighResolutionCapable</key>'
  echo '  <true/>'
  if [[ -n "$ICON_DST" && -f "$ICON_DST" ]]; then
    echo '  <key>CFBundleIconFile</key>'
    echo '  <string>AppIcon</string>'
  fi
  echo '</dict>'
  echo '</plist>'
} > "$DESKTOP_APP/Contents/Info.plist"

echo ""
echo "Install complete."
echo "Desktop shortcut: Chairside Ready Alert.app"
echo ""
echo "Launching Chairside Ready Alert..."
open "$DESKTOP_APP"
echo "Done."
read -rp "Press Enter to close this window... " _
