"""
Whimsical stick-figure animations for Chairside Ready Alert.

Triggered randomly on the SENDING workstation after a few qualifying outgoing
Ready messages — a small reward for the staff using the app. The figure
appears to emerge from behind the Ready button, wanders to a random spot on
the app's main window, performs a brief whimsical act, and returns behind
the button. Pure cosmetic feature; no networking, no persistence beyond a
per-animation on/off flag in the existing config file.

Architecture (so the picker preview is pixel-identical to production):

  - StickFigureOverlay manages a transparent Tk Toplevel that floats above
    the parent window. Cross-platform: Windows uses -transparentcolor,
    macOS uses -transparent, fallback is an opaque overlay matching the
    parent's background color.
  - AnimationPlayer drives a ~30fps frame loop via root.after(), calling
    the chosen animation's draw_fn(canvas, t, w, h, button_x, button_y)
    each frame. t is normalized 0..1; button_x/y are the Ready button's
    center in overlay-canvas coordinates.
  - ANIMATIONS is a registry mapping each animation's stable id to its
    (name, duration_ms, draw_fn) tuple. Both the picker and the main app
    iterate this dict; there is no separate "production" animation set.
  - AnimationTrigger is a tiny in-memory state machine: it counts
    qualifying outgoing Ready sends (a send only qualifies if at least
    10 minutes have elapsed since the previous qualifying one) and fires
    after a randomly chosen target in [4, 8].
  - Config helpers (_user_data_dir, load_animation_prefs,
    save_animation_prefs) read and write the SAME config file the main
    app uses (chairside_ready_alert_config.json), under the
    "animation_preferences" key. Atomic temp+rename writes.

The stick figure is built from a single helper, draw_figure_pose, that
takes joint angles in world-space degrees (0=right, 90=down, 180=left,
270=up). All animations stick to this convention so the visual style is
uniform.

Run the picker separately:
    python3 ready_animation_picker.py
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import tkinter as tk
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Config helpers — must match the main app's user-data dir / config file.
# Duplicated rather than imported so the picker stays usable as a stand-alone
# script even when the main app's module has issues (e.g., missing deps).
# ---------------------------------------------------------------------------

CONFIG_FILE = "chairside_ready_alert_config.json"


def _user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/ChairsideReadyAlert"
        )
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ChairsideReadyAlert")
    # Linux / other — best-effort
    return os.path.expanduser("~/.local/share/chairside-ready-alert")


def _config_path() -> str:
    return os.path.join(_user_data_dir(), CONFIG_FILE)


def _read_full_config() -> dict:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_full_config(data: dict) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_animation_prefs() -> dict:
    """Read animation_preferences from the shared config file. Returns {}
    if the file doesn't exist or has no preferences yet — caller treats
    missing keys as 'enabled' by default."""
    prefs = _read_full_config().get("animation_preferences", {})
    return prefs if isinstance(prefs, dict) else {}


def save_animation_prefs(prefs: dict) -> None:
    """Atomically write animation_preferences to the shared config file,
    preserving all other top-level keys. Tolerant of a missing file."""
    data = _read_full_config()
    data["animation_preferences"] = prefs
    _write_full_config(data)


def load_animation_character() -> str:
    """Read the user's chosen stick-figure character id. Falls back to
    'top_hat' (the default character) when the key is missing or unknown.
    Default was 'plain' until 1.0.53, but Fieldcrest's preferred figure
    is the top-hat one and configs that haven't been touched (e.g. fresh
    Windows installs) should match what the Mac install already shows."""
    char_id = _read_full_config().get("animation_character", "top_hat")
    if not isinstance(char_id, str) or char_id not in CHARACTERS:
        return "top_hat"
    return char_id


def save_animation_character(char_id: str) -> None:
    """Write the chosen character id to the shared config file."""
    if char_id not in CHARACTERS:
        char_id = "top_hat"
    data = _read_full_config()
    data["animation_character"] = char_id
    _write_full_config(data)


def load_animation_trigger_state() -> dict:
    """Read AnimationTrigger persisted state. Returns {} when missing —
    callers treat that as a cold start."""
    state = _read_full_config().get("anim_trigger_state", {})
    return state if isinstance(state, dict) else {}


def save_animation_trigger_state(state: dict) -> None:
    """Atomically write AnimationTrigger state to the shared config file,
    preserving all other top-level keys."""
    data = _read_full_config()
    data["anim_trigger_state"] = state
    _write_full_config(data)


# ---------------------------------------------------------------------------
# Stick figure drawing primitives.
# All angles in WORLD-SPACE degrees: 0=right, 90=down, 180=left, 270=up.
# This matches Tk canvas coordinates (Y increases downward).
# ---------------------------------------------------------------------------

STROKE = 3
COLOR = "#000000"

# Skeleton segment lengths (at scale=1.0)
LEN_BODY = 32
LEN_UPPER_ARM = 18
LEN_FOREARM = 17
LEN_UPPER_LEG = 22
LEN_LOWER_LEG = 20
HEAD_R = 10


def _project(x: float, y: float, length: float, angle_deg: float) -> tuple[float, float]:
    """Endpoint of a segment starting at (x, y), going `length` pixels in
    direction `angle_deg` (world-space)."""
    rad = math.radians(angle_deg)
    return x + length * math.cos(rad), y + length * math.sin(rad)


def _segment(canvas: tk.Canvas, x1, y1, x2, y2, *, stroke=STROKE, color=COLOR):
    canvas.create_line(
        x1, y1, x2, y2,
        fill=color, width=stroke, capstyle="round",
    )


def _circle(canvas: tk.Canvas, cx, cy, r, *, stroke=STROKE, color=COLOR, fill=""):
    canvas.create_oval(
        cx - r, cy - r, cx + r, cy + r,
        outline=color, width=stroke, fill=fill,
    )


# ---------------------------------------------------------------------------
# Face: every character gets eyes + a smile drawn on the head, oriented so
# the face sits on the side of the head opposite the neck (i.e., "forward").
# When the figure rotates during a cartwheel or lies down to sleep, the face
# rotates with the body. Drawn by draw_figure_pose just before the per-
# character decoration so things like glasses overlay the eye dots correctly.
# ---------------------------------------------------------------------------


def _draw_face(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Two eye dots and a smile arc on the head."""
    # Eye positions: slightly toward the top of the head (along body_angle)
    # and symmetrical on either side of the body axis.
    up_x, up_y = _project(0, 0, head_r * 0.18, body_angle)
    eye_center_x = head_x + up_x
    eye_center_y = head_y + up_y
    eye_offset = head_r * 0.36
    eye_r = max(1.4, head_r * 0.13 * scale)
    for side in (+1, -1):
        ex, ey = _project(
            eye_center_x, eye_center_y, eye_offset, body_angle + 90 * side,
        )
        canvas.create_oval(
            ex - eye_r, ey - eye_r, ex + eye_r, ey + eye_r,
            outline=color, fill=color, width=0,
        )
    # Smile: a smooth three-point arc curving toward the chin (i.e., toward
    # the body, opposite body_angle). Tk's create_arc is screen-axis aligned
    # which wouldn't rotate with the figure, so we approximate the curve
    # with create_line(smooth=True) through three control points.
    down_x, down_y = _project(0, 0, head_r * 0.28, (body_angle + 180) % 360)
    mouth_cx = head_x + down_x
    mouth_cy = head_y + down_y
    smile_half_w = head_r * 0.34
    smile_depth = head_r * 0.18
    left = _project(mouth_cx, mouth_cy, smile_half_w, body_angle + 90)
    right = _project(mouth_cx, mouth_cy, smile_half_w, body_angle - 90)
    bot = _project(mouth_cx, mouth_cy, smile_depth, (body_angle + 180) % 360)
    canvas.create_line(
        left[0], left[1], bot[0], bot[1], right[0], right[1],
        fill=color, width=max(1, stroke - 1),
        smooth=True, capstyle="round",
    )


# ---------------------------------------------------------------------------
# Character decorations — drawn on top of the bare stick figure (and on top
# of the face) to give it personality. Each function takes the head position,
# head radius, body angle (so decorations rotate correctly during cartwheels
# etc.), and the same scale/stroke/color as the base figure.
# ---------------------------------------------------------------------------


def _decor_plain(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """No decoration — the unadorned base stick figure."""
    return


def _decor_wavy_hair(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Three wavy strands streaming away from the top of the head in the
    body_angle direction (so when standing they go up; when cartwheeling
    they whip around with the figure)."""
    strand_count = 5
    wave_len = 22 * scale
    wave_amp = 7 * scale
    steps = 7
    for i in range(strand_count):
        offset_deg = (i - (strand_count - 1) / 2) * 14
        start_x, start_y = _project(head_x, head_y, head_r * 0.6, body_angle + offset_deg)
        pts = []
        for j in range(steps + 1):
            d = (j / steps) * wave_len
            wave_off = wave_amp * math.sin(j * 1.3 + i * 0.6)
            tip_x, tip_y = _project(start_x, start_y, d, body_angle + offset_deg)
            tip_x, tip_y = _project(tip_x, tip_y, wave_off, body_angle + offset_deg + 90)
            pts.append((tip_x, tip_y))
        flat = [c for pt in pts for c in pt]
        canvas.create_line(
            *flat,
            fill=color, width=max(1, stroke - 1),
            smooth=True, capstyle="round",
        )


def _decor_top_hat(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """A cylindrical top hat that sits on top of the head, oriented with the
    body axis."""
    brim_half = head_r * 1.2
    hat_h = head_r * 1.7
    top_half = head_r * 0.85
    # Brim line: perpendicular to body_angle, at the top of the head
    base_x, base_y = _project(head_x, head_y, head_r * 0.85, body_angle)
    bl_x, bl_y = _project(base_x, base_y, brim_half, body_angle + 90)
    br_x, br_y = _project(base_x, base_y, brim_half, body_angle - 90)
    canvas.create_line(bl_x, bl_y, br_x, br_y, fill=color, width=stroke, capstyle="round")
    # Sides + top of the cylinder
    top_x, top_y = _project(base_x, base_y, hat_h, body_angle)
    side_l_b = _project(base_x, base_y, top_half, body_angle + 90)
    side_r_b = _project(base_x, base_y, top_half, body_angle - 90)
    side_l_t = _project(top_x, top_y, top_half, body_angle + 90)
    side_r_t = _project(top_x, top_y, top_half, body_angle - 90)
    canvas.create_line(side_l_b[0], side_l_b[1], side_l_t[0], side_l_t[1],
                       fill=color, width=stroke, capstyle="round")
    canvas.create_line(side_r_b[0], side_r_b[1], side_r_t[0], side_r_t[1],
                       fill=color, width=stroke, capstyle="round")
    canvas.create_line(side_l_t[0], side_l_t[1], side_r_t[0], side_r_t[1],
                       fill=color, width=stroke, capstyle="round")


def _decor_mohawk(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Three spikes radiating away from the head along the body axis — like
    an upturned-fan look. Rotates with the figure."""
    for i, (offset_deg, height_mul) in enumerate(
        ((-20, 0.9), (-7, 1.4), (7, 1.4), (20, 0.9)),
    ):
        base_x, base_y = _project(head_x, head_y, head_r, body_angle + offset_deg)
        tip_x, tip_y = _project(
            base_x, base_y, head_r * 1.4 * height_mul,
            body_angle + offset_deg * 0.4,   # spikes lean slightly outward
        )
        canvas.create_line(
            base_x, base_y, tip_x, tip_y,
            fill=color, width=stroke, capstyle="round",
        )


def _decor_glasses(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Two small spectacle lenses on the head, perpendicular to the body axis
    (so they read as eyes on the face)."""
    eye_offset = head_r * 0.45
    lens_r = head_r * 0.32
    lx, ly = _project(head_x, head_y, eye_offset, body_angle + 90)
    rx, ry = _project(head_x, head_y, eye_offset, body_angle - 90)
    sw = max(1, stroke - 1)
    canvas.create_oval(lx - lens_r, ly - lens_r, lx + lens_r, ly + lens_r,
                       outline=color, width=sw, fill="")
    canvas.create_oval(rx - lens_r, ry - lens_r, rx + lens_r, ry + lens_r,
                       outline=color, width=sw, fill="")
    # Bridge between the two lenses
    canvas.create_line(lx, ly, rx, ry, fill=color, width=sw)


def _decor_beard(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """A triangular beard hanging from the chin (the side of the head closer
    to the body)."""
    beard_dir = (body_angle + 180) % 360
    tip_x, tip_y = _project(head_x, head_y, head_r * 2.2, beard_dir)
    base_x, base_y = _project(head_x, head_y, head_r * 0.85, beard_dir)
    edge_l = _project(base_x, base_y, head_r * 0.8, beard_dir + 90)
    edge_r = _project(base_x, base_y, head_r * 0.8, beard_dir - 90)
    canvas.create_line(edge_l[0], edge_l[1], tip_x, tip_y,
                       fill=color, width=stroke, capstyle="round", smooth=True)
    canvas.create_line(edge_r[0], edge_r[1], tip_x, tip_y,
                       fill=color, width=stroke, capstyle="round", smooth=True)
    # A few short whisker lines
    for off in (-0.4, 0.0, 0.4):
        mid_x, mid_y = _project(head_x, head_y, head_r * 1.4, beard_dir)
        whisk_x, whisk_y = _project(mid_x, mid_y, head_r * 0.5, beard_dir + off * 40)
        canvas.create_line(mid_x, mid_y, whisk_x, whisk_y,
                           fill=color, width=max(1, stroke - 1), capstyle="round")


def _decor_bow_tie(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """A small bow tie at the neck — the position just past the chin toward
    the body. Two triangles joined at a centre knot."""
    # Tie sits at the chin (head edge closest to body)
    chin_x, chin_y = _project(head_x, head_y, head_r, (body_angle + 180) % 360)
    # Two triangles flanking a small central oval
    half_w = head_r * 1.2
    half_h = head_r * 0.45
    # Left wing
    lp1 = _project(chin_x, chin_y, half_w, body_angle + 90)
    lp_top = _project(lp1[0], lp1[1], half_h, body_angle)
    lp_bot = _project(lp1[0], lp1[1], half_h, (body_angle + 180) % 360)
    # Right wing
    rp1 = _project(chin_x, chin_y, half_w, body_angle - 90)
    rp_top = _project(rp1[0], rp1[1], half_h, body_angle)
    rp_bot = _project(rp1[0], rp1[1], half_h, (body_angle + 180) % 360)
    sw = max(1, stroke - 1)
    canvas.create_polygon(
        chin_x, chin_y, lp_top[0], lp_top[1], lp_bot[0], lp_bot[1],
        outline=color, width=sw, fill="",
    )
    canvas.create_polygon(
        chin_x, chin_y, rp_top[0], rp_top[1], rp_bot[0], rp_bot[1],
        outline=color, width=sw, fill="",
    )
    knot_r = head_r * 0.18
    canvas.create_oval(
        chin_x - knot_r, chin_y - knot_r, chin_x + knot_r, chin_y + knot_r,
        outline=color, width=sw, fill="",
    )


def _decor_sunglasses(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Solid sunglasses: filled circular lenses + a bridge across the nose."""
    up_x, up_y = _project(0, 0, head_r * 0.18, body_angle)
    eye_center_x = head_x + up_x
    eye_center_y = head_y + up_y
    eye_offset = head_r * 0.40
    lens_r = head_r * 0.32
    for side in (+1, -1):
        cx, cy = _project(eye_center_x, eye_center_y, eye_offset, body_angle + 90 * side)
        canvas.create_oval(
            cx - lens_r, cy - lens_r, cx + lens_r, cy + lens_r,
            outline=color, fill=color, width=1,
        )
    # Bridge between the lenses (perpendicular to body axis)
    bridge_y = head_r * 0.05
    lb = _project(eye_center_x, eye_center_y, eye_offset - lens_r, body_angle + 90)
    rb = _project(eye_center_x, eye_center_y, eye_offset - lens_r, body_angle - 90)
    canvas.create_line(lb[0], lb[1], rb[0], rb[1],
                       fill=color, width=stroke, capstyle="round")


def _decor_pigtails(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Two pigtail bunches on either side of the head, each made of four
    short strokes fanning outward and slightly downward."""
    for side in (+1, -1):
        anchor = _project(head_x, head_y, head_r * 0.95, body_angle + 90 * side)
        for j in range(4):
            offset_deg = (j - 1.5) * 10
            # Pigtails fall slightly toward the body (away from body_angle).
            tip = _project(
                anchor[0], anchor[1], head_r * 1.4,
                body_angle + 90 * side + 18 * side + offset_deg,
            )
            canvas.create_line(
                anchor[0], anchor[1], tip[0], tip[1],
                fill=color, width=stroke, capstyle="round",
            )


def _decor_curly(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Tight curls along the top of the head — five small unfilled circles."""
    curl_r = head_r * 0.20
    for angle_off in (-55, -28, 0, 28, 55):
        cx, cy = _project(head_x, head_y, head_r * 1.05, body_angle + angle_off)
        canvas.create_oval(
            cx - curl_r, cy - curl_r, cx + curl_r, cy + curl_r,
            outline=color, width=max(1, stroke - 1), fill="",
        )


def _decor_baseball_cap(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Baseball cap: domed crown over the top of the head plus a brim
    sticking out to one side (the 'forward' direction of the face)."""
    # Dome: a smooth polyline approximation of the upper half-circle of the
    # head. Drawing as a smoothed line means it rotates cleanly.
    n_pts = 10
    points = []
    for i in range(n_pts + 1):
        t = i / n_pts
        angle = body_angle + (t - 0.5) * 170
        pos = _project(head_x, head_y, head_r * 1.08, angle)
        points.append(pos)
    flat = [c for p in points for c in p]
    canvas.create_line(*flat, fill=color, width=stroke, smooth=True, capstyle="round")
    # Brim: small triangular wedge pointing to one side ("front of head")
    brim_dir = body_angle + 80
    brim_anchor = _project(head_x, head_y, head_r * 0.95, brim_dir)
    t1 = _project(brim_anchor[0], brim_anchor[1], head_r * 0.9, brim_dir)
    t2 = _project(brim_anchor[0], brim_anchor[1], head_r * 0.4, brim_dir - 20)
    canvas.create_line(brim_anchor[0], brim_anchor[1], t1[0], t1[1],
                       fill=color, width=stroke, capstyle="round")
    canvas.create_line(t1[0], t1[1], t2[0], t2[1],
                       fill=color, width=stroke, capstyle="round")


def _decor_wizard_hat(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Tall pointy wizard hat with a small star on the side."""
    # Brim line perpendicular to body axis
    brim_pos = _project(head_x, head_y, head_r * 0.85, body_angle)
    brim_half = head_r * 1.3
    bl = _project(brim_pos[0], brim_pos[1], brim_half, body_angle + 90)
    br = _project(brim_pos[0], brim_pos[1], brim_half, body_angle - 90)
    canvas.create_line(bl[0], bl[1], br[0], br[1],
                       fill=color, width=stroke, capstyle="round")
    # Triangle from the brim to the tip
    tri_half = head_r * 0.75
    base_l = _project(brim_pos[0], brim_pos[1], tri_half, body_angle + 90)
    base_r = _project(brim_pos[0], brim_pos[1], tri_half, body_angle - 90)
    tip = _project(brim_pos[0], brim_pos[1], head_r * 2.4, body_angle)
    canvas.create_polygon(
        base_l[0], base_l[1], tip[0], tip[1], base_r[0], base_r[1],
        outline=color, width=stroke, fill="",
    )
    # Star (4-point cross) near the tip
    star_center = _project(tip[0], tip[1], head_r * 0.5, (body_angle + 180) % 360)
    star_r = head_r * 0.18
    sw = max(1, stroke - 1)
    s1 = _project(star_center[0], star_center[1], star_r, body_angle)
    s2 = _project(star_center[0], star_center[1], star_r, (body_angle + 180) % 360)
    s3 = _project(star_center[0], star_center[1], star_r, body_angle + 90)
    s4 = _project(star_center[0], star_center[1], star_r, body_angle - 90)
    canvas.create_line(s1[0], s1[1], s2[0], s2[1], fill=color, width=sw, capstyle="round")
    canvas.create_line(s3[0], s3[1], s4[0], s4[1], fill=color, width=sw, capstyle="round")


def _decor_cowboy_hat(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Cowboy hat: wide brim that curls up at the sides + short rounded crown."""
    # Wide brim with subtle curl-up at the edges
    brim_pos = _project(head_x, head_y, head_r * 0.85, body_angle)
    brim_half = head_r * 1.5
    n = 9
    points = []
    for i in range(n + 1):
        t = i / n
        side_amount = (t - 0.5) * 2 * brim_half
        pos = _project(brim_pos[0], brim_pos[1], side_amount, body_angle + 90)
        # Edges curl UP (toward body_angle direction), middle stays flat
        curl = max(0.0, abs(t - 0.5) - 0.30) * head_r * 0.8
        up_off = _project(0, 0, curl, body_angle)
        points.append((pos[0] + up_off[0], pos[1] + up_off[1]))
    flat = [c for p in points for c in p]
    canvas.create_line(*flat, fill=color, width=stroke, smooth=True, capstyle="round")
    # Crown: rounded dome
    crown_half = head_r * 0.75
    crown_top_offset = head_r * 0.7
    cl = _project(brim_pos[0], brim_pos[1], crown_half, body_angle + 90)
    cr = _project(brim_pos[0], brim_pos[1], crown_half, body_angle - 90)
    crown_top = _project(brim_pos[0], brim_pos[1], crown_top_offset, body_angle)
    ctl = _project(crown_top[0], crown_top[1], crown_half * 0.65, body_angle + 90)
    ctr = _project(crown_top[0], crown_top[1], crown_half * 0.65, body_angle - 90)
    canvas.create_line(
        cl[0], cl[1], ctl[0], ctl[1], ctr[0], ctr[1], cr[0], cr[1],
        fill=color, width=stroke, smooth=True, capstyle="round",
    )


def _decor_mustache(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Handlebar mustache between the eyes and the smile."""
    # Position: just above the smile, below the eye line
    pos_x, pos_y = _project(head_x, head_y, head_r * 0.06, (body_angle + 180) % 360)
    half_w = head_r * 0.42
    # Two curls — one on each side, each a smooth 3-point line ending with
    # a small upward flick away from the body.
    for side in (+1, -1):
        outer = _project(pos_x, pos_y, half_w, body_angle + 90 * side)
        # Flick up (toward body_angle) at the end
        flick = _project(outer[0], outer[1], head_r * 0.18, body_angle)
        canvas.create_line(
            pos_x, pos_y, outer[0], outer[1], flick[0], flick[1],
            fill=color, width=stroke, smooth=True, capstyle="round",
        )


def _decor_crown(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Royal crown: zigzag of three peaks across the top of the head."""
    base_pos = _project(head_x, head_y, head_r * 0.7, body_angle)
    base_half = head_r * 1.0
    peak_h = head_r * 0.7
    n_peaks = 3
    # 2*n_peaks + 1 points: alternating bottom-of-valley and top-of-peak
    points = []
    for i in range(2 * n_peaks + 1):
        t = i / (2 * n_peaks)
        side_amount = (t - 0.5) * 2 * base_half
        pt = _project(base_pos[0], base_pos[1], side_amount, body_angle + 90)
        if i % 2 == 0:
            points.append(pt)  # at the base
        else:
            tip = _project(pt[0], pt[1], peak_h, body_angle)
            points.append(tip)
    flat = [c for p in points for c in p]
    canvas.create_line(*flat, fill=color, width=stroke, capstyle="round")
    # Small jewel (filled circle) on the middle peak
    middle_peak = points[n_peaks]  # the middle peak index
    jewel_r = head_r * 0.10
    canvas.create_oval(
        middle_peak[0] - jewel_r, middle_peak[1] - jewel_r,
        middle_peak[0] + jewel_r, middle_peak[1] + jewel_r,
        outline=color, fill=color, width=0,
    )


def _decor_bunny_ears(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Two long curved bunny ears extending up from the head."""
    for side in (-1, +1):
        # Ear anchor: on top of the head, slightly to the side
        anchor = _project(head_x, head_y, head_r * 0.7, body_angle + 18 * side)
        outer_mid = _project(anchor[0], anchor[1], head_r * 1.0, body_angle + 22 * side)
        outer_tip = _project(outer_mid[0], outer_mid[1], head_r * 1.0, body_angle + 6 * side)
        # Inner side comes from a slightly different head anchor and meets the same tip
        inner_anchor = _project(head_x, head_y, head_r * 0.85, body_angle + 5 * side)
        inner_mid = _project(inner_anchor[0], inner_anchor[1], head_r * 0.9, body_angle)
        # Draw outer + tip + back along inner side as one smooth polyline
        canvas.create_line(
            anchor[0], anchor[1],
            outer_mid[0], outer_mid[1],
            outer_tip[0], outer_tip[1],
            inner_mid[0], inner_mid[1],
            inner_anchor[0], inner_anchor[1],
            fill=color, width=stroke, smooth=True, capstyle="round",
        )


def _decor_headphones(canvas, head_x, head_y, head_r, body_angle, *, scale, stroke, color):
    """Headphones: a band across the top of the head and two ear cups on
    either side."""
    # Band: smooth arc over the top of the head
    n = 7
    points = []
    for i in range(n + 1):
        t = i / n
        angle = body_angle + (t - 0.5) * 130
        pos = _project(head_x, head_y, head_r * 1.05, angle)
        points.append(pos)
    flat = [c for p in points for c in p]
    canvas.create_line(*flat, fill=color, width=stroke, smooth=True, capstyle="round")
    # Ear cups on either side of the head, perpendicular to body axis
    cup_r = head_r * 0.32
    for side in (-1, +1):
        cup_pos = _project(head_x, head_y, head_r * 0.92, body_angle + 90 * side)
        canvas.create_oval(
            cup_pos[0] - cup_r, cup_pos[1] - cup_r,
            cup_pos[0] + cup_r, cup_pos[1] + cup_r,
            outline=color, width=stroke, fill="",
        )


# Registry of characters. The key is the stable id used in the config file.
# Renaming a key is a breaking change for the user's saved preference.
CHARACTERS: dict = {
    "plain":         {"name": "Plain (no extras)", "draw_extra": _decor_plain},
    "wavy_hair":     {"name": "Wavy hair",          "draw_extra": _decor_wavy_hair},
    "curly":         {"name": "Curly hair",         "draw_extra": _decor_curly},
    "pigtails":      {"name": "Pigtails",           "draw_extra": _decor_pigtails},
    "mohawk":        {"name": "Mohawk",             "draw_extra": _decor_mohawk},
    "bunny_ears":    {"name": "Bunny ears",         "draw_extra": _decor_bunny_ears},
    "top_hat":       {"name": "Top hat",            "draw_extra": _decor_top_hat},
    "baseball_cap":  {"name": "Baseball cap",       "draw_extra": _decor_baseball_cap},
    "cowboy_hat":    {"name": "Cowboy hat",         "draw_extra": _decor_cowboy_hat},
    "wizard_hat":    {"name": "Wizard hat",         "draw_extra": _decor_wizard_hat},
    "crown":         {"name": "Crown",              "draw_extra": _decor_crown},
    "headphones":    {"name": "Headphones",         "draw_extra": _decor_headphones},
    "glasses":       {"name": "Glasses",            "draw_extra": _decor_glasses},
    "sunglasses":    {"name": "Sunglasses",         "draw_extra": _decor_sunglasses},
    "mustache":      {"name": "Mustache",           "draw_extra": _decor_mustache},
    "beard":         {"name": "Long beard",         "draw_extra": _decor_beard},
    "bow_tie":       {"name": "Bow tie",            "draw_extra": _decor_bow_tie},
}


# Module-level state: which character the renderer should apply. The player
# (and the picker) sets this once before each animation starts; every call
# to draw_figure_pose during that animation reads from here. Single-threaded
# Tk + at-most-one-animation-at-a-time means we don't need a lock.
_current_character_id: str = "top_hat"


def set_character(char_id: str) -> None:
    """Set the character whose decorations will be applied to subsequent
    draw_figure_pose calls. Unknown ids fall back to 'top_hat'."""
    global _current_character_id
    _current_character_id = char_id if char_id in CHARACTERS else "top_hat"


def get_character() -> str:
    """Return the currently active character id."""
    return _current_character_id


def _draw_figure_pose_classic(
    canvas: tk.Canvas,
    hip_x: float,
    hip_y: float,
    *,
    body_angle: float = 270.0,          # hip → neck direction. 270=up (default)
    left_arm: tuple[float, float] = (90.0, 90.0),   # (upper-arm dir, forearm dir)
    right_arm: tuple[float, float] = (90.0, 90.0),
    left_leg: tuple[float, float] = (90.0, 90.0),   # (upper-leg dir, lower-leg dir)
    right_leg: tuple[float, float] = (90.0, 90.0),
    head_r: float = HEAD_R,
    scale: float = 1.0,
    stroke: float = STROKE,
    color: str = COLOR,
):
    """Draw a stick figure at (hip_x, hip_y). Angles are absolute world-space
    degrees in the convention noted at the top of this section. All segment
    lengths scale uniformly with `scale`. Returns nothing — caller is expected
    to canvas.delete('all') between frames to clear the previous pose."""
    body_len = LEN_BODY * scale
    ua = LEN_UPPER_ARM * scale
    fa = LEN_FOREARM * scale
    ul = LEN_UPPER_LEG * scale
    ll = LEN_LOWER_LEG * scale
    hr = head_r * scale

    # Body (hip → neck)
    neck_x, neck_y = _project(hip_x, hip_y, body_len, body_angle)
    _segment(canvas, hip_x, hip_y, neck_x, neck_y, stroke=stroke, color=color)

    # Head: extends beyond neck in the same direction as the body
    head_x, head_y = _project(neck_x, neck_y, hr + 2, body_angle)
    _circle(canvas, head_x, head_y, hr, stroke=stroke, color=color)

    # Arms from neck (treated as a single shoulder anchor point — the visual
    # difference of separating shoulders is negligible at this scale)
    for upper_angle, lower_angle in (left_arm, right_arm):
        elbow_x, elbow_y = _project(neck_x, neck_y, ua, upper_angle)
        hand_x, hand_y = _project(elbow_x, elbow_y, fa, lower_angle)
        _segment(canvas, neck_x, neck_y, elbow_x, elbow_y, stroke=stroke, color=color)
        _segment(canvas, elbow_x, elbow_y, hand_x, hand_y, stroke=stroke, color=color)

    # Legs from hip
    for upper_angle, lower_angle in (left_leg, right_leg):
        knee_x, knee_y = _project(hip_x, hip_y, ul, upper_angle)
        foot_x, foot_y = _project(knee_x, knee_y, ll, lower_angle)
        _segment(canvas, hip_x, hip_y, knee_x, knee_y, stroke=stroke, color=color)
        _segment(canvas, knee_x, knee_y, foot_x, foot_y, stroke=stroke, color=color)

    # Face first: every character gets eyes + a smile on the head. The face
    # rotates with body_angle so cartwheels look right.
    try:
        _draw_face(canvas, head_x, head_y, hr, body_angle,
                   scale=scale, stroke=stroke, color=color)
    except Exception:
        pass

    # Apply the active character's decoration on top (hair, hat, glasses,
    # beard, etc.). Decoration draws AFTER the face so that things like
    # glasses correctly cover the eye dots underneath.
    decorator = CHARACTERS.get(_current_character_id, CHARACTERS["plain"])["draw_extra"]
    try:
        decorator(canvas, head_x, head_y, hr, body_angle,
                  scale=scale, stroke=stroke, color=color)
    except Exception:
        # Never let a malformed decoration crash an animation frame.
        pass

    return head_x, head_y, hr  # useful for animations that draw things near the head


# Convenience: smooth easing for "ease-in-out" segments inside animations
def _ease_in_out(t: float) -> float:
    """Cubic ease-in-out. t in [0, 1] -> output in [0, 1]."""
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_angle(start: float, end: float, t: float) -> float:
    """Interpolate between two angles (degrees) along the SHORTEST arc.
    Plain _lerp on angles wraps the wrong way around the circle when the
    endpoints span more than 180° (e.g., 270 → 0 lerped goes through 135,
    flipping the figure through upside-down). _lerp_angle goes the short way."""
    diff = ((end - start + 540) % 360) - 180
    return (start + diff * t) % 360


# Walking gait: returns a tuple (left_leg, right_leg, left_arm, right_arm)
# of joint-angle tuples for a single phase of walking. `phase` is a continuous
# value (radians). `intensity` scales how much the limbs swing.
#
# Pre-1.0.45 this was a plain symmetric sine wave on every joint, which made
# the figure look mechanical and "frantic". Real walking has:
#   - Knees that bend more on the FORWARD swing (the foot lifts) and stay
#     close to straight on the BACK push (planted foot).
#   - Arms swing OPPOSITE to legs (left arm forward with right leg forward).
#   - A subtle elbow bend that breathes with the cycle.
#   - Smaller amplitudes than my previous version had.
# The function name and return shape are unchanged so existing callers don't
# need to be touched.
def _walk_pose(phase: float, intensity: float = 1.0):
    s = math.sin(phase)
    UPPER_SWING = 16 * intensity   # was 22
    ARM_SWING = 11 * intensity     # was 18
    KNEE_BEND = 18 * intensity     # new: knee flexion on the forward swing
    ELBOW_BEND = 5 * intensity     # new: subtle elbow swing

    # Legs swing on opposite phases. Positive s = left leg is forward.
    left_upper = 90 - s * UPPER_SWING
    right_upper = 90 + s * UPPER_SWING

    # Knee bend: only on the forward-swinging leg (the foot is off the
    # ground and the lower leg rotates back from the knee).
    # Adding to the upper angle rotates the lower leg further from vertical
    # in the direction that "tucks" the foot up under the body — the visual
    # cue of a knee bend mid-step.
    left_bend = max(0.0, s) * KNEE_BEND
    right_bend = max(0.0, -s) * KNEE_BEND
    left_lower = left_upper + left_bend
    right_lower = right_upper + right_bend

    # Arms swing opposite to legs.
    left_arm_upper = 90 + s * ARM_SWING        # opposite of left leg
    right_arm_upper = 90 - s * ARM_SWING
    # Elbow bend follows the same phase but smaller.
    left_arm_lower = left_arm_upper + s * ELBOW_BEND
    right_arm_lower = right_arm_upper - s * ELBOW_BEND

    return (
        (left_upper, left_lower),
        (right_upper, right_lower),
        (left_arm_upper, left_arm_lower),
        (right_arm_upper, right_arm_lower),
    )


# A small wave gesture used by several animations just before the figure
# walks back to the button. Breaks the fourth wall a little — the figure
# is acknowledging the user before leaving. `side`=+1 raises the right
# hand; `side`=-1 raises the left.
def _wave_pose(canvas, x, y, local_t, side):
    # Hand sweeps back and forth at ~2 cycles over the gesture's duration
    # (was 3 in 1.0.45 — felt too fast/jittery). 2 full cycles = 4π rad.
    wave_phase = local_t * 4 * math.pi
    wave_off = math.sin(wave_phase) * 14
    # Raised arm: upper angled 20° off vertical so it clears the head and
    # face entirely. The forearm sweeps side-to-side from that anchor.
    raise_upper = 250 if side > 0 else 290
    raise_lower = raise_upper + (wave_off * side)
    # Resting arm: hangs naturally
    rest_upper = 90 + 5 * (-side)
    rest_lower = 90 + 3 * (-side)
    if side > 0:
        # +1 = wave the figure's "left arm" in our parameter scheme
        # (left/right here are drawing labels, not the figure's anatomical
        # sides — they just pair which limb gets which pose)
        draw_figure_pose(
            canvas, x, y,
            body_angle=270,
            left_arm=(raise_upper, raise_lower),
            right_arm=(rest_upper, rest_lower),
        )
    else:
        draw_figure_pose(
            canvas, x, y,
            body_angle=270,
            left_arm=(rest_upper, rest_lower),
            right_arm=(raise_upper, raise_lower),
        )


# ---------------------------------------------------------------------------
# Phase + walk helpers — used by every animation to slice the [0, 1] global
# time into named segments and to draw the figure walking from A to B.
# ---------------------------------------------------------------------------


def _phase(t: float, t_start: float, t_end: float):
    """If t falls inside [t_start, t_end], return a local 0..1 value within
    that phase. Otherwise return None. Lets each animation be structured as a
    flat sequence of `if local is not None: …` blocks instead of nested ifs."""
    if t < t_start or t > t_end:
        return None
    if t_end <= t_start:
        return 1.0
    return (t - t_start) / (t_end - t_start)


def _walk_classic(canvas, from_xy, to_xy, local_t, *, gait_speed: float = 7.0):
    """Draw a walking figure transitioning from from_xy to to_xy as local_t
    goes 0→1. Eased; the legs and arms swing at gait_speed (radians per
    full local_t). Default was 18 in 1.0.43 which made the figure look
    frantic; 7 is closer to a comfortable real-world stride rate given
    that most walk phases are around 0.05–0.15 of a 7–10s animation."""
    fx, fy = from_xy
    tx, ty = to_xy
    eased = _ease_in_out(local_t)
    hx = _lerp(fx, tx, eased)
    hy = _lerp(fy, ty, eased)
    # Arrange the gait phase so opposite limbs swing alternately and the
    # phase increments smoothly across a walk (instead of resetting per call).
    gait_phase = local_t * gait_speed
    ll, rl, la, ra = _walk_pose(gait_phase)
    draw_figure_pose(
        canvas, hx, hy,
        left_leg=ll, right_leg=rl,
        left_arm=la, right_arm=ra,
    )


# ---------------------------------------------------------------------------
# Button mask: drawn on top of the figure every frame, this replicates the
# Ready button visually so figure parts that overlap the button get hidden.
# When the figure is well clear of the button, the mask still draws (just
# looks like the button itself, which is what we want — the user can't tell
# the mask apart from the real button underneath).
# Drives the peek-out and peek-back-in effects: as the figure slides between
# fully-hidden (hip on button center) and fully-visible (hip well outside the
# button rectangle), more or less of it appears from beside the button edge.
# ---------------------------------------------------------------------------


def _draw_button_mask(canvas: tk.Canvas, button: dict) -> None:
    """Draw a button-shaped mask: a rounded rectangle in the button's color
    plus its label text. Sits above anything drawn earlier in the same frame."""
    x, y, w, h = button["x"], button["y"], button["w"], button["h"]
    color = button.get("color", "#2563eb")
    text_color = button.get("text_color", "#ffffff")
    label = button.get("label", "Ready")
    radius = button.get("radius", 10)
    x0, y0 = x - w / 2, y - h / 2
    x1, y1 = x + w / 2, y + h / 2
    r = min(radius, w / 2, h / 2)
    # Rounded rectangle as a smoothed polygon (same shape primitive
    # chairside_ready_alert.RoundedButton uses).
    pts = [
        x0 + r, y0,  x1 - r, y0,  x1, y0,  x1, y0 + r,
        x1, y1 - r,  x1, y1,  x1 - r, y1,  x0 + r, y1,
        x0, y1,     x0, y1 - r,  x0, y0 + r,  x0, y0,
    ]
    color2 = button.get("color2")
    canvas.create_polygon(pts, smooth=True,
                          fill=mix(color, color2, 0.5) if color2 else color, outline="")
    if color2 and color2 != color:
        # Left-to-right gradient matching the 1.0.85 Ready button: 1px
        # vertical lines clipped to the rounded corners. Drawn every frame
        # only during the brief emerge / hide phases.
        n = int(x1 - x0)
        for i in range(1, n - 1, 2):
            x = x0 + i + 0.5
            d = (x0 + r - x) if x < x0 + r else (x - (x1 - r)) if x > x1 - r else 0.0
            inset = r - math.sqrt(max(0.0, r * r - d * d)) if d > 0 else 0.0
            canvas.create_line(x, y0 + inset + 1, x, y1 - inset - 1,
                               fill=mix(color, color2, i / max(1, n)), width=2)
    canvas.create_text(
        x, y, text=label, fill=text_color,
        font=_button_label_font(int(h)),
    )


def _button_label_font(button_h: int) -> tuple:
    """Pick a plausible bold font for the button label given the button's
    rendered height. The exact font doesn't have to match the main app
    pixel-perfect — what matters is that the mask reads as a 'button' to the
    eye while the figure is peeking from behind it."""
    if sys.platform == "win32":
        family = "Segoe UI"
    else:
        family = "Helvetica"
    size = max(10, min(18, int(button_h * 0.40)))
    return (family, size, "bold")


def _emerge_position(button: dict, side: int, hidden_amount: float):
    """Return the hip (x, y) for the figure at a given emergence amount.

    `side`: +1 = emerge from the button's right edge, -1 = emerge from its left.
    `hidden_amount`: 1.0 = hip at button center (figure fully behind mask),
                     0.0 = hip clear of the button, figure fully visible.

    The X interpolation moves the hip from the button's center out to its
    edge + 30px clearance.

    The Y interpolation LIFTS the hip upward as the figure emerges. At
    fully-hidden the hip sits at the button's vertical center (so the
    horizontal figure fits entirely within the button mask). At fully-
    emerged the hip rises by exactly enough that the figure's feet land
    at the button's bottom edge — preventing the previous bug where mid-
    emerge leg rotations caused the foot to dip below the button.
    """
    bx, by = button["x"], button["y"]
    bw = button["w"]
    bh = button["h"]
    clear_offset_x = bw / 2 + 30
    target_x_offset = _lerp(clear_offset_x, 0.0, hidden_amount)

    # How far the figure's feet extend below the hip when standing.
    leg_length = LEN_UPPER_LEG + LEN_LOWER_LEG
    # Lift the hip so feet land at button bottom when emerged. Clamped at 0
    # for buttons taller than the leg span (won't happen in practice).
    lift = max(0.0, leg_length - bh / 2)
    target_y = by - _lerp(lift, 0.0, hidden_amount)

    return (bx + side * target_x_offset, target_y)


def _get_rng(button: dict) -> random.Random:
    """Return a stable random.Random for the current play. AnimationPlayer
    stamps a `_seed` onto the button dict at play() time, so every frame of
    a single play sees the same sequence — but consecutive plays get
    different sequences."""
    return random.Random(button.get("_seed", 12345))


def _y_jitter(button: dict, amp: float = 30.0) -> float:
    """Stable per-play vertical offset in roughly [-amp, +amp]. Used so
    consecutive runs of the same animation don't land in identical pixels."""
    return _get_rng(button).uniform(-amp, amp)


def _frac_jitter(button: dict, amp: float = 0.07) -> float:
    """Stable per-play horizontal-fraction offset. Applied to spot fractions
    passed into _wander_xy so consecutive runs slide left/right of their
    canonical waypoint."""
    # Use a separate rng "stream" so x jitter is uncorrelated with y jitter.
    rng = random.Random(button.get("_seed", 12345) ^ 0xA5A5A5)
    return rng.uniform(-amp, amp)


def _rng_seq(button: dict, n: int, amp: float, stream: int = 0) -> list[float]:
    """Return `n` stable per-play uniform values in [-amp, +amp]. Different
    `stream` integers produce independent sequences so callers can request
    uncorrelated x/y/etc jitters. Same seed + same stream + same n always
    returns the same list across all frames of one play."""
    rng = random.Random((button.get("_seed", 12345) ^ (stream * 0x9E3779B1)) & 0xFFFFFFFF)
    return [rng.uniform(-amp, amp) for _ in range(n)]


def _random_spots(button: dict, w: int, h: int, n: int, side: int, *,
                  same_y: bool = False) -> list[tuple[float, float]]:
    """Pick `n` random spots distributed across the WHOLE canvas (corners
    + mid-edges), in a different random order each play (via the button's
    _seed). The figure traverses them in the returned order, so each play
    produces a different cross-canvas sequence — top-right, bottom-left,
    top-left, etc.

    Spots are sampled without replacement from a fixed candidate set
    spread around the canvas perimeter; per-spot jitter (±30px x, ±25px y)
    means even repeated region picks aren't pixel-identical.

    With `same_y=True` all spots share one randomly-chosen Y band. Used
    for cartwheel where the motion is inherently horizontal.

    Walks between distant spots may cross behind the Ready button; the
    button mask briefly hides the figure in transit, which reads as the
    figure ducking behind the button rather than as a glitch.
    """
    rng = random.Random((button.get("_seed", 12345) ^ 0xC0FFEE) & 0xFFFFFFFF)

    # Candidate regions cover the four corners, top/bottom centers, and
    # the two horizontal mid-points. Y values are chosen so that the
    # button (typically at center-X, ~0.40h) doesn't sit inside any
    # region's natural area.
    regions = [
        (0.12, 0.15),  # top-left
        (0.50, 0.10),  # top-center
        (0.88, 0.15),  # top-right
        (0.10, 0.55),  # left-mid (below button-Y)
        (0.90, 0.55),  # right-mid (below button-Y)
        (0.15, 0.82),  # bottom-left
        (0.50, 0.85),  # bottom-center
        (0.85, 0.82),  # bottom-right
    ]
    chosen = rng.sample(regions, min(n, len(regions)))

    if same_y:
        y_level = rng.choice([0.18, 0.55, 0.82]) * h
        spots = []
        for xf, _yf in chosen:
            jx = rng.uniform(-30, 30)
            sx = max(60.0, min(w - 60.0, xf * w + jx))
            spots.append((sx, y_level))
        return spots

    spots = []
    for xf, yf in chosen:
        jx = rng.uniform(-30, 30)
        jy = rng.uniform(-25, 25)
        sx = max(60.0, min(w - 60.0, xf * w + jx))
        sy = max(60.0, min(h - 60.0, yf * h + jy))
        spots.append((sx, sy))
    return spots


def _drift(button: dict, t: float, amp_x: float = 35.0, amp_y: float = 20.0) -> tuple[float, float]:
    """Smooth pseudo-random position drift to add to a stationary figure so
    it wanders gently around its spot instead of standing perfectly still.

    `t` is the global animation time 0..1 (NOT phase-local). Returns (dx, dy)
    offsets in pixels. The drift is the sum of two sin waves per axis at
    randomized but slow frequencies; the seed determines the wave phases and
    rates, so each play produces a different but smooth wander pattern that
    stays consistent frame-to-frame within one play.

    Designed to be unobtrusive — peak amplitude is ~amp_x/amp_y but the
    typical instantaneous drift is roughly 60-70% of that."""
    rng = random.Random((button.get("_seed", 12345) ^ 0xBADC0DE) & 0xFFFFFFFF)
    pa, pb, pc, pd = (rng.uniform(0, 2 * math.pi) for _ in range(4))
    fx1, fx2 = rng.uniform(0.6, 1.1), rng.uniform(0.25, 0.5)
    fy1, fy2 = rng.uniform(0.5, 0.9), rng.uniform(0.2, 0.4)
    dx = (math.sin(t * 2 * math.pi * fx1 + pa) * 0.55 +
          math.sin(t * 2 * math.pi * fx2 + pb) * 0.45) * amp_x
    dy = (math.sin(t * 2 * math.pi * fy1 + pc) * 0.55 +
          math.sin(t * 2 * math.pi * fy2 + pd) * 0.45) * amp_y
    return (dx, dy)


def _pick_emerge_side(button: dict, canvas_w: int) -> int:
    """Pick which side of the button the figure peeks out from. +1=right,
    -1=left. When both sides have at least 120px of usable room, randomize
    per-play so consecutive Ready clicks alternate sides instead of always
    going the same way. When one side is cramped (button near a window edge)
    fall back to picking the side with more space."""
    bx = button["x"]
    bw = button["w"]
    space_right = canvas_w - (bx + bw / 2)
    space_left = bx - bw / 2
    if space_right >= 120 and space_left >= 120:
        return _get_rng(button).choice([+1, -1])
    return +1 if space_right >= space_left else -1


def _wander_xy(button: dict, side: int, canvas_w: int, fraction: float,
               y: float) -> tuple[float, float]:
    """Compute a target spot for the figure to wander to, anchored to the
    available distance between the button edge and the canvas edge instead
    of using fixed pixel offsets.

    `fraction` is 0..1 along the available horizontal travel:
        0.0  ~= just past the button edge
        1.0  ~= near the canvas edge (with a 60px limb-extension margin)
    Clamped to [0, 1] so callers adding per-play jitter can't overshoot.
    """
    fraction = max(0.0, min(1.0, fraction))
    edge_x = button["x"] + side * (button["w"] / 2 + 30)
    if side > 0:
        avail = max(40.0, canvas_w - edge_x - 60.0)
    else:
        avail = max(40.0, edge_x - 60.0)
    target_x = edge_x + side * avail * fraction
    return (target_x, y)


# ---------------------------------------------------------------------------
# The overlay window and the per-animation player.
# ---------------------------------------------------------------------------


class StickFigureOverlay:
    """A transparent Tk Toplevel that floats above the parent window. The
    canvas inside is where animations draw. Hidden when no animation is
    running. Picker and main app both instantiate this exactly the same
    way — that's what guarantees visual fidelity between them."""

    def __init__(self, parent: tk.Misc):
        self.parent = parent
        self.overlay = tk.Toplevel(parent)
        self.overlay.overrideredirect(True)        # no chrome
        self.overlay.attributes("-topmost", True)
        # Try to make the overlay click-through so the user can keep
        # interacting with the app underneath. Best-effort per platform.
        self._bg_color = self._setup_transparency()
        self.canvas = tk.Canvas(
            self.overlay,
            bg=self._bg_color,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.overlay.withdraw()

    def _setup_transparency(self) -> str:
        """Pick a background color that the OS will render as transparent
        (so only the stick figure strokes are visible) and apply the
        platform-specific window attributes. Returns the chosen bg color
        for the canvas to use."""
        try:
            if sys.platform == "win32":
                # Magenta is uncommon in UIs — safe as a transparency key.
                self.overlay.attributes("-transparentcolor", "magenta")
                return "magenta"
            if sys.platform == "darwin":
                # Aqua Tk supports -transparent on the window itself.
                # When set, the bg color "systemTransparent" renders as
                # see-through. If the attribute isn't supported we fall
                # through to the opaque fallback.
                self.overlay.attributes("-transparent", True)
                return "systemTransparent"
        except tk.TclError:
            pass
        # Fallback: opaque overlay matching the parent's bg color so it at
        # least visually blends with the app's main background. Stick
        # figure strokes will still be visible.
        try:
            bg = self.parent.cget("bg")  # type: ignore[arg-type]
        except Exception:
            bg = "#f0f4ff"
        return bg or "#f0f4ff"

    def show(self, x: int, y: int, w: int, h: int) -> None:
        self.overlay.geometry(f"{w}x{h}+{x}+{y}")
        self.overlay.deiconify()
        self.overlay.lift()
        try:
            self.overlay.attributes("-topmost", True)
        except tk.TclError:
            pass

    def hide(self) -> None:
        try:
            self.overlay.withdraw()
        except tk.TclError:
            pass

    def clear(self) -> None:
        try:
            self.canvas.delete("all")
        except tk.TclError:
            pass

    def destroy(self) -> None:
        try:
            self.overlay.destroy()
        except tk.TclError:
            pass


class AnimationPlayer:
    """Drives a single animation through its frames. One player can replay
    different animations sequentially; only one animation runs at a time."""

    FRAME_MS = 16  # ~60fps (falls back to ~30fps on slow machines, see _tick)
    # If a frame takes longer than this to draw, drop to 30fps for the rest
    # of the play so a slow PC never stutters or lags the app.
    SLOW_FRAME_SEC = 0.011
    # After the animation ends, keep drawing until in-flight particles
    # (confetti, sparkles) have finished -- capped so nothing lingers.
    PARTICLE_TAIL_SEC = 2.5

    def __init__(
        self,
        root: tk.Misc,
        overlay: StickFigureOverlay,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        self.root = root
        self.overlay = overlay
        self.on_complete = on_complete
        self._anim_id: Optional[str] = None
        self._start_time: float = 0.0
        self._button: dict = {}
        self._after_id: Optional[str] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def play(self, anim_id: str, button: dict) -> None:
        """Play `anim_id` with `button` describing the Ready button's position,
        size, and color. The button dict's required keys are:
            x, y       — center, in overlay-canvas coordinates
            w, h       — button width / height in pixels
            color      — fill color (matches the button's rendered color)
            text_color — label color
            label      — text rendered on the button (default 'Ready')
            radius     — corner radius (default 10)

        A `_seed` value is added to the button dict so animations can derive
        per-play randomness (side, y offset, spot jitter) that stays stable
        across all frames of this play but varies across consecutive plays.
        """
        if anim_id not in ANIMATIONS:
            return
        self.stop()  # cancel any in-flight animation
        self._anim_id = anim_id
        self._button = dict(button)
        self._button["_seed"] = random.randint(0, 10**9)
        self._start_time = time.monotonic()
        self._frame_ms = self.FRAME_MS if OPTIONS.get("fps60", True) else 33
        self._slow_frames = 0
        reset_play()
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.overlay.clear()

    def _tick(self) -> None:
        if not self._running or self._anim_id is None:
            return
        anim = ANIMATIONS[self._anim_id]
        elapsed_ms = (time.monotonic() - self._start_time) * 1000.0
        duration = float(anim["duration_ms"])
        t = min(1.0, elapsed_ms / duration)

        canvas = self.overlay.canvas
        now = time.monotonic()
        try:
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            FX.begin_frame(now)
            # Animation draws the figure. The button-mask used to be drawn
            # here unconditionally to clip the figure where it overlapped
            # the Ready button, but that meant the figure was hidden any
            # time it walked over the button. Now _emerge and _hide_back
            # draw their own mask AFTER the figure (producing the peek
            # effect at the start and end), and all other phases skip it
            # entirely — so during the random cross-canvas movement the
            # figure walks ON TOP of the real Ready button beneath the
            # transparent overlay.
            if t < 1.0:
                anim["draw_fn"](canvas, t, w, h, self._button)
            FX.draw(canvas)
        except tk.TclError:
            self.stop()
            return

        if self._frame_ms < 33:
            if time.monotonic() - now > self.SLOW_FRAME_SEC:
                self._slow_frames += 1
                if self._slow_frames >= 6:
                    self._frame_ms = 33
            else:
                self._slow_frames = 0

        tail_over = elapsed_ms / 1000.0 > duration / 1000.0 + self.PARTICLE_TAIL_SEC
        if t >= 1.0 and (not FX.parts or tail_over):
            self._running = False
            self.overlay.clear()
            if self.on_complete:
                try:
                    self.on_complete()
                except Exception:
                    pass
            return

        try:
            self._after_id = self.root.after(self._frame_ms, self._tick)
        except tk.TclError:
            self.stop()


def play_animation(
    anim_id: str,
    parent_window: tk.Misc,
    button_widget: tk.Misc,
    on_complete: Optional[Callable[[], None]] = None,
    character: Optional[str] = None,
) -> Optional[AnimationPlayer]:
    """Top-level convenience: create an overlay sized to the parent window,
    compute the button's position in overlay coordinates, play the animation,
    tear the overlay down when done. Returns the AnimationPlayer so the caller
    can .stop() it early if needed.

    parent_window: a tk.Tk or tk.Toplevel — the window the figure will appear
                   to be moving over.
    button_widget: the widget whose center is "behind the button" for
                   emergence/return. Typically the Ready button.
    character:     character id from CHARACTERS. If None, leaves the current
                   character setting alone (the caller may have set it
                   already via set_character() or load_animation_character()).
    """
    if anim_id not in ANIMATIONS:
        return None

    if character is not None:
        set_character(character)

    try:
        parent_window.update_idletasks()
        button_widget.update_idletasks()
    except tk.TclError:
        return None

    try:
        px = parent_window.winfo_rootx()      # type: ignore[attr-defined]
        py = parent_window.winfo_rooty()      # type: ignore[attr-defined]
        pw = parent_window.winfo_width()      # type: ignore[attr-defined]
        ph = parent_window.winfo_height()     # type: ignore[attr-defined]

        btn_w = int(button_widget.winfo_width())      # type: ignore[attr-defined]
        btn_h = int(button_widget.winfo_height())     # type: ignore[attr-defined]
        bx_screen = button_widget.winfo_rootx() + btn_w // 2  # type: ignore[attr-defined]
        by_screen = button_widget.winfo_rooty() + btn_h // 2  # type: ignore[attr-defined]
    except tk.TclError:
        return None

    overlay = StickFigureOverlay(parent_window)
    overlay.show(px, py, pw, ph)

    # Translate button center from screen → overlay-canvas coordinates.
    button_x = bx_screen - px
    button_y = by_screen - py

    # Pull the button's color + label off the widget so the mask can match.
    # RoundedButton (chairside_ready_alert) stores these as _bg / _fg / _text;
    # MockReadyButton (picker) does the same. If a widget doesn't expose them
    # we fall back to the Modern Blue defaults — better to have a slightly
    # mismatched mask than no animation at all.
    btn_color = getattr(button_widget, "_bg", "#2563eb")
    btn_text_color = getattr(button_widget, "_fg", "#ffffff")
    btn_label = getattr(button_widget, "_text", "Ready")
    btn_radius = getattr(button_widget, "_r", 10)
    btn_color2 = getattr(button_widget, "_bg2", None)
    # Widgets with a glow/shadow margin (the 1.0.85 Ready button) report how
    # far the visible button sits inside the widget, so the peek mask lines
    # up with the button itself rather than the whole canvas.
    il, it, ir, ib = getattr(button_widget, "_body_inset", (0, 0, 0, 0))
    btn_w -= il + ir
    btn_h -= it + ib
    bx_screen += (il - ir) // 2
    by_screen += (it - ib) // 2

    button_info = {
        "x": button_x,
        "y": button_y,
        "w": btn_w,
        "h": btn_h,
        "color": btn_color,
        "text_color": btn_text_color,
        "label": btn_label,
        "radius": btn_radius,
        "color2": btn_color2,
    }

    def _done() -> None:
        overlay.hide()
        overlay.destroy()
        if on_complete:
            try:
                on_complete()
            except Exception:
                pass

    player = AnimationPlayer(parent_window, overlay, on_complete=_done)
    player.play(anim_id, button_info)
    return player


# ---------------------------------------------------------------------------
# The 9 animations. Each `draw_anim_*` has the signature
#   def draw_anim_xxx(canvas, t, w, h, button):
# where `t` is [0, 1] global time, w/h are the overlay canvas dimensions,
# and `button` is the dict described on AnimationPlayer.play().
#
# Each animation is structured as a sequence of named phases via _phase().
# Every animation begins with a peek-out from beside the button and ends
# with a matching peek-in. Between, the figure visits 2-3 different
# locations in the window, doing something whimsical at each. Durations
# vary (6-15s) so back-to-back triggers don't feel repetitive.
#
# Coordinate convention: the figure's "hip" is its drawing anchor. The
# button mask is drawn on top by the player every frame, so figure parts
# that overlap the button rectangle disappear behind it — this is what
# produces the side-peek emerge / hide effect.
# ---------------------------------------------------------------------------


def _emerge_base(canvas, button, local_t, side):
    """Standardized 'figure peeks out from beside the button' phase.

    At local_t=0 the figure is in a HORIZONTAL pose (body, arms, and legs all
    aligned along the body axis) with the hip at the button center. In that
    pose the entire figure — head, feet, all of it — fits inside the button's
    rectangle, so the button mask hides every part of the figure. As local_t
    advances to 1 the body rotates from horizontal to vertical (shortest
    arc) while the hip slides outward to the button's edge plus a small
    clearance. Limbs gradually rotate from along-body to natural standing
    angles. The net effect is the figure rising up out from behind the
    button's side, head first."""
    eased = _ease_in_out(local_t)
    hidden_amount = 1.0 - eased
    hx, hy = _emerge_position(button, side, hidden_amount)

    # Body axis rotates from horizontal-pointing-out (toward the peek side)
    # to vertical-up. Uses shortest-arc lerp so the figure doesn't go
    # through upside-down.
    body_init = 0.0 if side > 0 else 180.0
    body_angle = _lerp_angle(body_init, 270.0, eased)

    # Limbs: while horizontal the limbs lie ALONG the body so the figure is
    # a thin horizontal stick (head r + stroke wide vertically). Arms point
    # in the body direction (forward); legs point in the opposite direction
    # (so the figure has feet at the "back end" of the horizontal stick).
    # As emergence completes, arms swing down to 90° (hanging) and legs
    # rotate to 90° (standing).
    arm_init = body_init                       # along body
    leg_init = (body_init + 180.0) % 360.0     # opposite of body
    arm = _lerp_angle(arm_init, 90.0, eased)
    leg = _lerp_angle(leg_init, 90.0, eased)

    draw_figure_pose(
        canvas, hx, hy,
        body_angle=body_angle,
        left_arm=(arm, arm), right_arm=(arm, arm),
        left_leg=(leg, leg), right_leg=(leg, leg),
    )
    # Mask is drawn ONLY during emerge/hide_back so the peek effect works.
    # During the rest of the animation (walks and activity phases) the mask
    # is omitted, leaving the figure visible on top of the real Ready
    # button beneath the transparent overlay.
    _draw_button_mask(canvas, button)


def _hide_back_base(canvas, button, local_t, side):
    """Mirror of _emerge: as local_t goes 0→1 the figure transitions from
    standing-and-visible (at button edge + clearance) to flat-and-hidden
    (horizontal at button center). Same rotation pattern in reverse."""
    eased = _ease_in_out(local_t)
    hidden_amount = eased
    hx, hy = _emerge_position(button, side, hidden_amount)

    body_final = 0.0 if side > 0 else 180.0
    body_angle = _lerp_angle(270.0, body_final, eased)

    arm_final = body_final
    leg_final = (body_final + 180.0) % 360.0
    arm = _lerp_angle(90.0, arm_final, eased)
    leg = _lerp_angle(90.0, leg_final, eased)

    draw_figure_pose(
        canvas, hx, hy,
        body_angle=body_angle,
        left_arm=(arm, arm), right_arm=(arm, arm),
        left_leg=(leg, leg), right_leg=(leg, leg),
    )
    # See note above _emerge — mask is drawn here so the figure visibly
    # slides back behind the button at the end of the animation.
    _draw_button_mask(canvas, button)


# ---- Animation 1: Surprise — single spot, 5s ----
# Total 5s. Walks out, hands fly up with "!" floating well above, walks back.
# Arms angled out to a wide V so they don't cross the head/face line.
def draw_anim_surprise(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    fj = _frac_jitter(button)
    yj = _y_jitter(button, 20)
    spot = _wander_xy(button, side, w, 0.30 + fj, button["y"] + 5 + yj)

    if (l := _phase(t, 0.00, 0.10)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.10, 0.25)) is not None:
        _walk(canvas, edge_xy, spot, l); return
    if (l := _phase(t, 0.25, 0.85)) is not None:
        # Hands up in a WIDE V so they're clear of the head. Slight jiggle.
        jiggle = math.sin(l * 12) * 1.5
        draw_figure_pose(
            canvas, spot[0], spot[1] + jiggle,
            left_arm=(235, 230), right_arm=(305, 310),
        )
        # "!" floats well above the highest reach of the V'd hands. Hands
        # peak at neck_y - sin(45°) * 35 ≈ neck_y - 25 above the neck. Put
        # the "!" comfortably above that, leaving daylight between the
        # punctuation and the fingertips.
        excl_top = spot[1] - LEN_BODY - 50
        canvas.create_line(spot[0], excl_top, spot[0], excl_top + 18,
                           fill=COLOR, width=STROKE, capstyle="round")
        canvas.create_oval(
            spot[0] - 2, excl_top + 22, spot[0] + 2, excl_top + 26,
            outline=COLOR, fill=COLOR, width=STROKE,
        )
        return
    if (l := _phase(t, 0.85, 0.95)) is not None:
        _walk(canvas, spot, edge_xy, l); return
    if (l := _phase(t, 0.95, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 3: Stretches — three-spot routine ----
# Total ~11s. Toes / overhead reach / side-twist at three different spots.
def draw_anim_stretches(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    spot1, spot2, spot3 = _random_spots(button, w, h, 3, side)

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.13)) is not None:
        _walk(canvas, edge_xy, spot1, l); return
    if (l := _phase(t, 0.13, 0.30)) is not None:
        # Touch toes
        bend = _ease_in_out(min(1.0, l * 1.5))
        body = _lerp(270, 200, bend)
        arm = _lerp(90, 130, bend)
        draw_figure_pose(canvas, spot1[0], spot1[1],
                         body_angle=body,
                         left_arm=(arm, arm), right_arm=(arm, arm))
        return
    if (l := _phase(t, 0.30, 0.38)) is not None:
        _walk(canvas, spot1, spot2, l); return
    if (l := _phase(t, 0.38, 0.55)) is not None:
        # Side lunge: one leg straight, other deeply bent at the knee,
        # arms reaching diagonally up over the bent-leg side. Reads cleanly
        # because the body's silhouette is asymmetric instead of all
        # vertical (which is what made the overhead reach disappear visually).
        lean_amount = _ease_in_out(min(1.0, l * 1.4))
        body_lean = lean_amount * 18 * side
        # Bent (lead) leg: bent knee on the side opposite the body lean
        # so the figure looks like it's stepping into the lunge.
        bend = lean_amount * 60
        lead_upper = 90 - bend * 0.4 * side
        lead_lower = 90 + bend * 0.6 * side
        trail_upper = 90 + 18 * side
        trail_lower = 90 + 18 * side
        # Arms reach diagonally up
        arm_a = 270 - 35 * side
        arm_b = 270 + 35 * side
        draw_figure_pose(
            canvas, spot2[0], spot2[1] + 6,
            body_angle=270 + body_lean,
            left_leg=(lead_upper, lead_lower),
            right_leg=(trail_upper, trail_lower),
            left_arm=(arm_a, arm_a),
            right_arm=(arm_b, arm_b),
        )
        return
    if (l := _phase(t, 0.55, 0.63)) is not None:
        _walk(canvas, spot2, spot3, l); return
    if (l := _phase(t, 0.63, 0.80)) is not None:
        # Side twist: lean L/R over a 3-cycle wave
        twist_phase = l * 3 * math.pi * 2
        lean = math.sin(twist_phase) * 18
        draw_figure_pose(canvas, spot3[0], spot3[1],
                         body_angle=270 + lean,
                         left_arm=(180, 180), right_arm=(0, 0))
        return
    if (l := _phase(t, 0.80, 0.88)) is not None:
        _wave_pose(canvas, spot3[0], spot3[1], l, side); return
    if (l := _phase(t, 0.88, 0.94)) is not None:
        _walk(canvas, spot3, edge_xy, l); return
    if (l := _phase(t, 0.94, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 4 (REPLACED): Juggle — single spot, 3 balls in the air ----
# Total ~9s. Walks to one spot, juggles three balls in a clear cascade
# pattern, takes a small bow, walks back.
def draw_anim_juggle(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    fj = _frac_jitter(button)
    yj = _y_jitter(button, 25)
    spot = _wander_xy(button, side, w, 0.45 + fj, h * 0.58 + yj)

    BALL_R = 6
    HAND_OFFSET_X = 14  # how far apart the hands are when juggling
    HAND_Y_BELOW_NECK = LEN_UPPER_ARM + LEN_FOREARM * 0.6
    ARC_HEIGHT = 80    # peak height of each ball above the hands
    ARC_WIDTH = 28     # horizontal span of each ball's arc

    def _draw_juggler(local_t, n_balls):
        # The figure stands with arms forward-and-up, palms turned upward,
        # ready to catch. Small vertical bob in time with the throws.
        bob_phase = local_t * 6 * math.pi
        hip_bob = math.sin(bob_phase) * 2
        draw_figure_pose(
            canvas, spot[0], spot[1] + hip_bob,
            left_arm=(60, 50), right_arm=(120, 130),
        )
        neck_y = spot[1] + hip_bob - LEN_BODY
        hand_y = neck_y + HAND_Y_BELOW_NECK
        left_hand = (spot[0] - HAND_OFFSET_X, hand_y)
        right_hand = (spot[0] + HAND_OFFSET_X, hand_y)
        # Three balls in a cascade: each is on its own offset of a single
        # parabola going from one hand → peak → other hand → … back.
        # We just stagger their phases.
        ball_phases = [0.0, 1.0 / 3.0, 2.0 / 3.0]
        for i in range(n_balls):
            # Each ball's local phase 0..1 traces one arc from one hand to
            # the other and is repeated.
            bp = (local_t * 1.6 + ball_phases[i]) % 1.0
            # The ball alternates which hand is the START of its arc each cycle.
            start_left = (int(local_t * 1.6 + ball_phases[i]) % 2 == 0)
            start = left_hand if start_left else right_hand
            end   = right_hand if start_left else left_hand
            # Parabolic trajectory between start and end
            x = _lerp(start[0], end[0], bp)
            # Parabola: y(t) = start_y - 4 * H * t * (1-t)  (peak at t=0.5)
            y = start[1] - 4 * ARC_HEIGHT * bp * (1 - bp)
            canvas.create_oval(
                x - BALL_R, y - BALL_R, x + BALL_R, y + BALL_R,
                outline=COLOR, fill=COLOR, width=STROKE,
            )

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.13)) is not None:
        _walk(canvas, edge_xy, spot, l); return
    if (l := _phase(t, 0.13, 0.22)) is not None:
        # Pull out the balls — start with just 1 ball, working up to 3
        n = 1 if l < 0.4 else (2 if l < 0.75 else 3)
        _draw_juggler(l * 0.3, n); return
    if (l := _phase(t, 0.22, 0.86)) is not None:
        _draw_juggler(l, 3); return
    if (l := _phase(t, 0.86, 0.92)) is not None:
        # Wave at the user before walking back
        _wave_pose(canvas, spot[0], spot[1], l, side)
        return
    if (l := _phase(t, 0.92, 0.97)) is not None:
        _walk(canvas, spot, edge_xy, l); return
    if (l := _phase(t, 0.97, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 5: Jumping jacks — two-spot workout ----
# Total ~10s. Several jacks at spot 1, walk over, more jacks at spot 2.
def draw_anim_jacks(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    spot1, spot2 = _random_spots(button, w, h, 2, side)

    def _jack(canvas, x, y, l_in_phase, cycles):
        # Phase goes 0..1 within a single rep; we run `cycles` reps total.
        rep = (1 - math.cos(l_in_phase * cycles * 2 * math.pi)) / 2  # 0..1..0 cycle
        bounce = rep * -10
        # Legs spread OUT when arms come up.
        leg_l = _lerp(90, 130, rep)
        leg_r = _lerp(90, 50, rep)
        # Arms START on opposite sides (T-pose, 180° and 0°) and swing UP
        # toward each other, stopping at a wide V (225° and 315°) so they
        # never come close to the face/head line. Uses shortest-arc lerp
        # so the path goes through the correct direction.
        arm_l = _lerp_angle(180, 225, rep)   # left side: T → upper-left V leg
        arm_r = _lerp_angle(0,   315, rep)   # right side: T → upper-right V leg
        draw_figure_pose(
            canvas, x, y + bounce,
            left_arm=(arm_l, arm_l), right_arm=(arm_r, arm_r),
            left_leg=(leg_l, leg_l), right_leg=(leg_r, leg_r),
        )

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.13)) is not None:
        _walk(canvas, edge_xy, spot1, l); return
    if (l := _phase(t, 0.13, 0.42)) is not None:
        _jack(canvas, spot1[0], spot1[1], l, 4); return
    if (l := _phase(t, 0.42, 0.50)) is not None:
        _walk(canvas, spot1, spot2, l); return
    if (l := _phase(t, 0.50, 0.82)) is not None:
        _jack(canvas, spot2[0], spot2[1], l, 4); return
    if (l := _phase(t, 0.82, 0.90)) is not None:
        _wave_pose(canvas, spot2[0], spot2[1], l, side); return
    if (l := _phase(t, 0.90, 0.96)) is not None:
        _walk(canvas, spot2, edge_xy, l); return
    if (l := _phase(t, 0.96, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 6: Sleep — single-spot nap with floating Zs ----
# Total 10s. Yawn → lay down → snore (Zs float up) → wake → walk back.
# Laying-down pose: arms hugging the sides of the body and legs straight
# along the body axis, but each limb is slightly splayed off the body
# line so they're visible separately instead of collapsing onto it.
def draw_anim_sleep(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    fj = _frac_jitter(button)
    yj = _y_jitter(button, 15)
    bed = _wander_xy(button, side, w, 0.60 + fj, h * 0.70 + yj)

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.15)) is not None:
        _walk(canvas, edge_xy, bed, l); return
    if (l := _phase(t, 0.15, 0.22)) is not None:
        # Yawn: arms come up in a wide V (clear of head/face), body
        # tilts back slightly.
        rise = _ease_in_out(l)
        body = _lerp(270, 265, rise)
        l_arm = _lerp_angle(90, 240, rise)
        r_arm = _lerp_angle(90, 300, rise)
        draw_figure_pose(canvas, bed[0], bed[1],
                         body_angle=body,
                         left_arm=(l_arm, l_arm),
                         right_arm=(r_arm, r_arm))
        return
    if (l := _phase(t, 0.22, 0.30)) is not None:
        # Lay down: body rotates from vertical (270°) to horizontal via the
        # SHORTEST arc so the figure tilts smoothly instead of flipping
        # upside-down. Limbs travel from their standing positions to the
        # slightly-splayed sleeping pose (see below).
        rise = _ease_in_out(l)
        target_body = 180.0 if side > 0 else 0.0
        angle = _lerp_angle(270.0, target_body, rise)
        hip_y = _lerp(bed[1], bed[1] + 30, rise)
        # Splay offsets for the laying pose: arms angle slightly away from
        # the body, legs slightly the same way so the figure isn't a single
        # thick stroke.
        # Body axis when laying = `target_body`. The splayed arm angles are
        # `target_body ± 12` (arms offset above and below the body line).
        # Legs at `target_body ± 8` (smaller splay to keep them clearly
        # 'underneath' the body).
        end_l_arm = (target_body + 12) % 360
        end_r_arm = (target_body - 12) % 360
        end_l_leg = (target_body + 8) % 360
        end_r_leg = (target_body - 8) % 360
        l_arm_now = _lerp_angle(90, end_l_arm, rise)
        r_arm_now = _lerp_angle(90, end_r_arm, rise)
        l_leg_now = _lerp_angle(90, end_l_leg, rise)
        r_leg_now = _lerp_angle(90, end_r_leg, rise)
        draw_figure_pose(canvas, bed[0], hip_y,
                         body_angle=angle,
                         left_arm=(l_arm_now, l_arm_now),
                         right_arm=(r_arm_now, r_arm_now),
                         left_leg=(l_leg_now, l_leg_now),
                         right_leg=(r_leg_now, r_leg_now))
        return
    if (l := _phase(t, 0.30, 0.75)) is not None:
        # Sleeping: horizontal body, splayed limbs, Zs floating up.
        body_angle = 180.0 if side > 0 else 0.0
        l_arm = (body_angle + 12) % 360
        r_arm = (body_angle - 12) % 360
        l_leg = (body_angle + 8) % 360
        r_leg = (body_angle - 8) % 360
        hip_y = bed[1] + 30
        draw_figure_pose(canvas, bed[0], hip_y,
                         body_angle=body_angle,
                         left_arm=(l_arm, l_arm), right_arm=(r_arm, r_arm),
                         left_leg=(l_leg, l_leg), right_leg=(r_leg, r_leg))
        # Head position: end of body line from hip
        head_x = bed[0] + (LEN_BODY + HEAD_R + 2) * (1 if side > 0 else -1)
        head_y = hip_y
        for i in range(3):
            z_local = (l * 1.6 - i * 0.32) % 1.0
            if z_local < 0.05:
                continue
            zx = head_x + (1 if side > 0 else -1) * (12 + z_local * 26)
            zy = head_y - 12 - z_local * 60
            size = 9 + i * 2
            fade = max(0.05, 1 - z_local)
            stroke = max(1, int(STROKE * fade + 0.5))
            canvas.create_line(zx, zy, zx + size, zy,
                               fill=COLOR, width=stroke)
            canvas.create_line(zx + size, zy, zx, zy + size,
                               fill=COLOR, width=stroke)
            canvas.create_line(zx, zy + size, zx + size, zy + size,
                               fill=COLOR, width=stroke)
        return
    if (l := _phase(t, 0.75, 0.83)) is not None:
        # Wake / stand: reverse the lay-down transition
        rise = _ease_in_out(l)
        start_body = 180.0 if side > 0 else 0.0
        angle = _lerp_angle(start_body, 270.0, rise)
        hip_y = _lerp(bed[1] + 30, bed[1], rise)
        # Reverse the splay back to standing
        start_l_arm = (start_body + 12) % 360
        start_r_arm = (start_body - 12) % 360
        start_l_leg = (start_body + 8) % 360
        start_r_leg = (start_body - 8) % 360
        l_arm_now = _lerp_angle(start_l_arm, 90, rise)
        r_arm_now = _lerp_angle(start_r_arm, 90, rise)
        l_leg_now = _lerp_angle(start_l_leg, 90, rise)
        r_leg_now = _lerp_angle(start_r_leg, 90, rise)
        draw_figure_pose(canvas, bed[0], hip_y,
                         body_angle=angle,
                         left_arm=(l_arm_now, l_arm_now),
                         right_arm=(r_arm_now, r_arm_now),
                         left_leg=(l_leg_now, l_leg_now),
                         right_leg=(r_leg_now, r_leg_now))
        return
    if (l := _phase(t, 0.83, 0.90)) is not None:
        _wave_pose(canvas, bed[0], bed[1], l, side); return
    if (l := _phase(t, 0.90, 0.96)) is not None:
        _walk(canvas, bed, edge_xy, l); return
    if (l := _phase(t, 0.96, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 7: Lifts weights — two-spot reps with travel between ----
# Total ~11s. Picks up at spot 1, several presses, walks to spot 2 with the
# bar overhead, more presses, then drops + wipes brow.
def draw_anim_weights(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    spot1, spot2 = _random_spots(button, w, h, 2, side)

    def _press(canvas, x, y, opened):
        body_lean = _lerp(20, 0, opened)
        squat = _lerp(15, 0, opened)
        arm = _lerp(120, 290, opened)
        leg_l = _lerp(70, 90, opened)
        leg_r = _lerp(110, 90, opened)
        hy = y + squat
        draw_figure_pose(
            canvas, x, hy,
            body_angle=270 - body_lean,
            left_arm=(arm, arm), right_arm=(arm, arm),
            left_leg=(leg_l, leg_l), right_leg=(leg_r, leg_r),
        )
        # Barbell at the hands' overhead position
        bar_x = x
        # Empirical: hands at this distance from neck along arm angle
        neck_x, neck_y = _project(x, hy, LEN_BODY, 270 - body_lean)
        hand_x, hand_y = _project(neck_x, neck_y,
                                  LEN_UPPER_ARM + LEN_FOREARM, arm)
        canvas.create_line(hand_x - 28, hand_y, hand_x + 28, hand_y,
                           fill=COLOR, width=STROKE + 1, capstyle="round")
        canvas.create_oval(hand_x - 34, hand_y - 6,
                           hand_x - 24, hand_y + 6,
                           outline=COLOR, width=STROKE, fill="")
        canvas.create_oval(hand_x + 24, hand_y - 6,
                           hand_x + 34, hand_y + 6,
                           outline=COLOR, width=STROKE, fill="")

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.13)) is not None:
        _walk(canvas, edge_xy, spot1, l); return
    if (l := _phase(t, 0.13, 0.42)) is not None:
        rep_phase = l * 3 * math.pi
        opened = (math.sin(rep_phase) + 1.0) * 0.5
        _press(canvas, spot1[0], spot1[1], opened); return
    if (l := _phase(t, 0.42, 0.52)) is not None:
        # Walk between with arms locked at shoulder level (carrying the bar
        # across the chest for the curl set at spot 2).
        eased = _ease_in_out(l)
        x = _lerp(spot1[0], spot2[0], eased)
        gait = l * 12
        ll, rl, _, _ = _walk_pose(gait, intensity=0.6)
        # Arms bent: upper arm down, forearm horizontal forward (bar across chest)
        draw_figure_pose(canvas, x, spot1[1],
                         left_arm=(90, 0), right_arm=(90, 180),
                         left_leg=ll, right_leg=rl)
        # Barbell at chest level — roughly between the two forearm endpoints.
        neck_x, neck_y = _project(x, spot1[1], LEN_BODY, 270)
        bar_y = neck_y + LEN_UPPER_ARM
        canvas.create_line(x - 28, bar_y, x + 28, bar_y,
                           fill=COLOR, width=STROKE + 1, capstyle="round")
        canvas.create_oval(x - 34, bar_y - 6, x - 24, bar_y + 6,
                           outline=COLOR, width=STROKE, fill="")
        canvas.create_oval(x + 24, bar_y - 6, x + 34, bar_y + 6,
                           outline=COLOR, width=STROKE, fill="")
        return
    if (l := _phase(t, 0.52, 0.82)) is not None:
        # Spot 2: BICEP CURLS instead of more overhead presses. Different
        # lift type so the second location actually feels new.
        rep_phase = l * 3 * math.pi
        opened = (math.sin(rep_phase) + 1.0) * 0.5
        x, y = spot2[0], spot2[1]
        # Upper arm pinned down at the sides (90°). Forearm rotates from
        # extended-straight-down (90°) up to bent-toward-shoulders (270°-ish).
        # Shoulders stay level, body upright.
        forearm = _lerp(90, 250, opened)     # left arm forearm curls inward
        forearm_r = _lerp(90, 290, opened)   # right arm forearm curls inward
        draw_figure_pose(
            canvas, x, y,
            body_angle=270,
            left_arm=(90, forearm), right_arm=(90, forearm_r),
        )
        # Bar across the wrists. As the forearms curl up, the bar rises with
        # the hands. Compute hand positions via the same projection used by
        # draw_figure_pose.
        neck_x, neck_y = _project(x, y, LEN_BODY, 270)
        elbow_l = _project(neck_x, neck_y, LEN_UPPER_ARM, 90)
        elbow_r = _project(neck_x, neck_y, LEN_UPPER_ARM, 90)
        hand_l = _project(elbow_l[0], elbow_l[1], LEN_FOREARM, forearm)
        hand_r = _project(elbow_r[0], elbow_r[1], LEN_FOREARM, forearm_r)
        bar_y = (hand_l[1] + hand_r[1]) / 2
        canvas.create_line(x - 22, bar_y, x + 22, bar_y,
                           fill=COLOR, width=STROKE + 1, capstyle="round")
        canvas.create_oval(x - 28, bar_y - 5, x - 18, bar_y + 5,
                           outline=COLOR, width=STROKE, fill="")
        canvas.create_oval(x + 18, bar_y - 5, x + 28, bar_y + 5,
                           outline=COLOR, width=STROKE, fill="")
        return
    if (l := _phase(t, 0.82, 0.88)) is not None:
        # Wipe brow — one hand near head
        draw_figure_pose(canvas, spot2[0], spot2[1],
                         left_arm=(220, 280), right_arm=(80, 90))
        return
    if (l := _phase(t, 0.88, 0.94)) is not None:
        _walk(canvas, spot2, edge_xy, l); return
    if (l := _phase(t, 0.94, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 8: Little dance — two-spot tour ----
# Total 10s. Side-step at spot 1 (with legs splayed for stability/style),
# kick-line at spot 2, then a wave goodbye. Spin removed — the
# horizontal-squish-as-rotation effect read as a glitch.
def draw_anim_dance(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    spot1, spot2 = _random_spots(button, w, h, 2, side)

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.15)) is not None:
        _walk(canvas, edge_xy, spot1, l); return
    if (l := _phase(t, 0.15, 0.40)) is not None:
        # Side-step with splayed legs (stance: legs angled outward to either
        # side instead of straight down — gives the figure a more grounded
        # dance-y posture per user feedback).
        beat = math.sin(l * 6 * math.pi)
        off = beat * 22
        arm = math.sin(l * 6 * math.pi + 1.0) * 60
        bounce = -abs(beat) * 5
        draw_figure_pose(
            canvas, spot1[0] + off, spot1[1] + bounce,
            left_arm=(90 + arm, 90 + arm * 0.6),
            right_arm=(90 - arm, 90 - arm * 0.6),
            # Legs splayed: one leans down-and-to-the-right, the other
            # down-and-to-the-left. Stable wide stance.
            left_leg=(105, 105),
            right_leg=(75, 75),
        )
        return
    if (l := _phase(t, 0.40, 0.48)) is not None:
        _walk(canvas, spot1, spot2, l); return
    if (l := _phase(t, 0.48, 0.85)) is not None:
        # Kick-line: alternating leg lifts
        kick = math.sin(l * 5 * math.pi)
        leg_lift = max(0, kick) * 35
        opposite = max(0, -kick) * 35
        draw_figure_pose(
            canvas, spot2[0], spot2[1],
            left_arm=(60, 60), right_arm=(120, 120),
            left_leg=(90 - leg_lift, 90 - leg_lift * 0.8),
            right_leg=(90 + opposite, 90 + opposite * 0.8),
        )
        return
    if (l := _phase(t, 0.85, 0.92)) is not None:
        _wave_pose(canvas, spot2[0], spot2[1], l, side); return
    if (l := _phase(t, 0.92, 0.97)) is not None:
        _walk(canvas, spot2, edge_xy, l); return
    if (l := _phase(t, 0.97, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 9: Cartwheels — fast, big lateral travel ----
# Total ~8s. Two full cartwheels out, recovery, two cartwheels back.
def draw_anim_cartwheel(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    launch, far = _random_spots(button, w, h, 2, side, same_y=True)
    # Cartwheel rotation direction follows the actual outbound motion
    # (launch → far) rather than the emerge side, otherwise the figure
    # can end up rolling backwards if _random_spots places `far` on the
    # opposite side of the canvas from `launch`.
    cart_dir = +1 if far[0] > launch[0] else -1

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.10)) is not None:
        _walk(canvas, edge_xy, launch, l); return
    if (l := _phase(t, 0.10, 0.45)) is not None:
        eased = l   # near-linear cartwheel pace
        x = _lerp(launch[0], far[0], eased)
        rot = l * 4 * math.pi * cart_dir
        body_angle = (270 + math.degrees(rot)) % 360
        # Arms extend PERPENDICULAR to the body axis (one each side, T-pose
        # style) rather than along it — this keeps them away from the head's
        # area at every rotation phase, which is what the user requested.
        # Legs splay outward from the feet end of the body, like a real
        # cartwheel where they trail behind in a wide V.
        l_arm_u = (body_angle - 90) % 360
        r_arm_u = (body_angle + 90) % 360
        # Slight forearm flare for visual interest
        l_arm_lo = (body_angle - 100) % 360
        r_arm_lo = (body_angle + 100) % 360
        # Legs at feet-end of body, splayed wide in a V
        leg_base = (body_angle + 180) % 360
        LEG_SPLAY = 25
        l_leg_u = (leg_base - LEG_SPLAY) % 360
        r_leg_u = (leg_base + LEG_SPLAY) % 360
        l_leg_lo = (leg_base - LEG_SPLAY * 1.3) % 360
        r_leg_lo = (leg_base + LEG_SPLAY * 1.3) % 360
        draw_figure_pose(canvas, x, launch[1],
                         body_angle=body_angle,
                         left_arm=(l_arm_u, l_arm_lo),
                         right_arm=(r_arm_u, r_arm_lo),
                         left_leg=(l_leg_u, l_leg_lo),
                         right_leg=(r_leg_u, r_leg_lo))
        return
    if (l := _phase(t, 0.45, 0.55)) is not None:
        # Recover at far side — stand with hands on hips, slight bounce
        bounce = math.sin(l * 6) * 2
        draw_figure_pose(canvas, far[0], far[1] + bounce,
                         left_arm=(150, 240), right_arm=(30, 300))
        return
    if (l := _phase(t, 0.55, 0.90)) is not None:
        x = _lerp(far[0], launch[0], l)
        rot = l * 4 * math.pi * (-cart_dir)
        body_angle = (270 + math.degrees(rot)) % 360
        # Same pose family as the outbound cartwheel — arms perpendicular
        # to body, legs splayed in a V at the feet end.
        l_arm_u = (body_angle - 90) % 360
        r_arm_u = (body_angle + 90) % 360
        l_arm_lo = (body_angle - 100) % 360
        r_arm_lo = (body_angle + 100) % 360
        leg_base = (body_angle + 180) % 360
        LEG_SPLAY = 25
        l_leg_u = (leg_base - LEG_SPLAY) % 360
        r_leg_u = (leg_base + LEG_SPLAY) % 360
        l_leg_lo = (leg_base - LEG_SPLAY * 1.3) % 360
        r_leg_lo = (leg_base + LEG_SPLAY * 1.3) % 360
        draw_figure_pose(canvas, x, launch[1],
                         body_angle=body_angle,
                         left_arm=(l_arm_u, l_arm_lo),
                         right_arm=(r_arm_u, r_arm_lo),
                         left_leg=(l_leg_u, l_leg_lo),
                         right_leg=(r_leg_u, r_leg_lo))
        return
    if (l := _phase(t, 0.90, 0.95)) is not None:
        _walk(canvas, launch, edge_xy, l); return
    if (l := _phase(t, 0.95, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


# ---- Animation 10: Yoga — three poses at three spots ----
# Total ~13s. Tree → Warrior II → Mountain (reach up). Long holds, slow
# transitions; the figure breathes (subtle vertical bob) during each hold.
def draw_anim_yoga(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge_xy = _emerge_position(button, side, 0.0)
    fj = _frac_jitter(button)
    yj = _y_jitter(button, 30)
    spot1 = _wander_xy(button, side, w, 0.25 + fj, h * 0.55 + yj)
    spot2 = _wander_xy(button, side, w, 0.55 + fj, h * 0.55 + yj)
    spot3 = _wander_xy(button, side, w, 0.90 + fj, h * 0.55 + yj)

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.13)) is not None:
        _walk(canvas, edge_xy, spot1, l); return
    if (l := _phase(t, 0.13, 0.32)) is not None:
        # Tree pose — arms in a wide V over the head (NOT straight up, so they
        # don't visually cross the face/head line).
        breath = math.sin(l * 4) * 2
        draw_figure_pose(canvas, spot1[0], spot1[1] + breath,
                         left_leg=(90, 90),
                         right_leg=(150, 60),
                         left_arm=(240, 240), right_arm=(300, 300))
        return
    if (l := _phase(t, 0.32, 0.40)) is not None:
        _walk(canvas, spot1, spot2, l); return
    if (l := _phase(t, 0.40, 0.62)) is not None:
        # Side stretch: body leans hard to one side, the OPPOSITE arm reaches
        # overhead in the direction of the lean (so the figure forms a clear
        # crescent shape). Reads much better in 2D than Warrior II did —
        # there's an obvious asymmetric silhouette.
        breath = math.sin(l * 4) * 2
        lean_amount = _ease_in_out(min(1.0, l * 1.4))
        lean = lean_amount * 25 * side
        body_angle = 270 + lean
        # Reaching arm: extends overhead in the direction the body is leaning
        reach_angle = body_angle  # along body axis, so it continues the lean
        # Resting arm: hand on hip on the OPPOSITE side from the reach
        rest_upper = 90 + side * 25
        rest_lower = 90 - side * 60
        # Both legs stay planted but the figure stands tall
        draw_figure_pose(
            canvas, spot2[0], spot2[1] + breath,
            body_angle=body_angle,
            left_leg=(90, 90), right_leg=(90, 90),
            left_arm=(reach_angle, reach_angle),
            right_arm=(rest_upper, rest_lower),
        )
        return
    if (l := _phase(t, 0.62, 0.70)) is not None:
        _walk(canvas, spot2, spot3, l); return
    if (l := _phase(t, 0.70, 0.83)) is not None:
        # Mountain / sun-salutation reach: arms in a wide V so they clear
        # the head/face.
        breath = math.sin(l * 4) * 3
        draw_figure_pose(canvas, spot3[0], spot3[1] + breath,
                         left_arm=(240, 240), right_arm=(300, 300))
        return
    if (l := _phase(t, 0.83, 0.90)) is not None:
        _wave_pose(canvas, spot3[0], spot3[1], l, side); return
    if (l := _phase(t, 0.90, 0.96)) is not None:
        _walk(canvas, spot3, edge_xy, l); return
    if (l := _phase(t, 0.96, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return





# ===========================================================================
# 1.0.85 renderer: tapered "ink" figure, ground shadow, squash & stretch,
# motion trails, expressive face, particle effects (sparkles, dust,
# confetti, notes), plus three new animations. Every animation draws through
# draw_figure_pose(), so all of them get the new look. Plain Tk canvas
# primitives only -- renders the same on Windows and macOS.
# ===========================================================================

# Music-note / heart glyphs. Tk maps "Helvetica" to Arial on Windows, whose
# glyph coverage for these symbols is unreliable; Segoe UI Symbol has them.
_SYMBOL_FONT = "Segoe UI Symbol" if sys.platform == "win32" else "Helvetica"

# ---------------------------------------------------------------------------
# Options -- every visual upgrade can be toggled independently.
# ---------------------------------------------------------------------------

OPTIONS = {
    "style": "ink",        # "classic" | "ink" | "toon"
    "shadow": True,        # soft ground shadow that shrinks when airborne
    "particles": True,     # sparkles, dust, confetti, music notes
    "squash": True,        # squash on landing, stretch when moving fast
    "trails": True,        # motion smear during fast spins (cartwheels)
    "eyes": True,          # blinking, gaze direction, expressions
    "walk_bob": True,      # hip bob + forward lean while walking
    "fps60": True,         # 60 fps instead of 30
    # Colors -- set_palette() overwrites these from the app's theme
    "ink": "#1e293b",
    "accent": "#2563eb",
    "skin": "#ffffff",
    "page": "#ffffff",     # what the figure is drawn over (for fades)
    "ground": "#dfe5ee",   # shadow color (blended toward the page bg)
    "puff": "#e2e8f0",
    "ghost1": "#cbd5e1",
    "ghost2": "#e2e8f0",
}

CONFETTI = ["#f43f5e", "#f59e0b", "#10b981", "#3b82f6", "#a855f7", "#06b6d4", "#facc15"]


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colors. t=0 -> c1, t=1 -> c2."""
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _ease_out_back(t: float, s: float = 1.7) -> float:
    t -= 1.0
    return t * t * ((s + 1) * t + s) + 1.0


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


# ---------------------------------------------------------------------------
# Particle system
# ---------------------------------------------------------------------------


class _Particle:
    __slots__ = ("kind", "x", "y", "vx", "vy", "t0", "life", "size", "color",
                 "g", "spin", "drag", "phase", "text")

    def __init__(self, kind, x, y, vx, vy, t0, life, size, color,
                 g=0.0, spin=0.0, drag=0.0, phase=0.0, text=""):
        self.kind, self.x, self.y, self.vx, self.vy = kind, x, y, vx, vy
        self.t0, self.life, self.size, self.color = t0, life, size, color
        self.g, self.spin, self.drag, self.phase, self.text = g, spin, drag, phase, text


class FXSystem:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.parts: list = []
        self.keys: set = set()
        self.now = time.monotonic()
        self.frame = 0
        self.ground_hint = None
        self.rng = random.Random()

    def begin_frame(self, now: float) -> None:
        self.now = now
        self.frame += 1
        self.ground_hint = None

    def once(self, key: str) -> bool:
        """True the first time `key` is seen during this play."""
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def _add(self, *a, **kw) -> None:
        if OPTIONS["particles"]:
            self.parts.append(_Particle(*a, **kw))

    # ---- emitters ----
    def sparkle(self, x, y, n=10, speed=150, colors=None, size=7.0):
        colors = colors or [OPTIONS["accent"], "#f59e0b", "#facc15"]
        for i in range(n):
            a = (i / n) * 2 * math.pi + self.rng.uniform(-0.3, 0.3)
            sp = speed * self.rng.uniform(0.55, 1.0)
            self._add("spark", x, y, math.cos(a) * sp, math.sin(a) * sp,
                      self.now, self.rng.uniform(0.45, 0.75),
                      size * self.rng.uniform(0.7, 1.15),
                      self.rng.choice(colors), g=120, spin=self.rng.uniform(-6, 6),
                      drag=3.2)

    def puff(self, x, y, n=3, size=5.0, spread=26):
        for i in range(n):
            d = -1 if i % 2 == 0 else 1
            self._add("puff", x + d * self.rng.uniform(1, 5), y,
                      d * self.rng.uniform(spread * 0.5, spread), -self.rng.uniform(6, 22),
                      self.now, self.rng.uniform(0.35, 0.55),
                      size * self.rng.uniform(0.8, 1.3), OPTIONS["puff"], drag=4.0)

    def confetti(self, x, y, n=44, speed=260):
        for _ in range(n):
            a = self.rng.uniform(-math.pi * 0.95, -math.pi * 0.05)
            sp = speed * self.rng.uniform(0.45, 1.0)
            self._add("confetti", x, y, math.cos(a) * sp, math.sin(a) * sp,
                      self.now, self.rng.uniform(1.4, 2.2), self.rng.uniform(5, 8),
                      self.rng.choice(CONFETTI), g=420, spin=self.rng.uniform(6, 14),
                      drag=2.2, phase=self.rng.uniform(0, 6.28))

    def ring(self, x, y, r0=6.0, r1=34.0, life=0.45, color=None):
        self._add("ring", x, y, r0, r1, self.now, life, 3.0,
                  color or OPTIONS["accent"])

    def pop_lines(self, x, y, n=7, color=None):
        """Little cartoon 'surprise' dashes bursting outward."""
        for i in range(n):
            a = -math.pi / 2 + (i - (n - 1) / 2) * 0.42
            self._add("dash", x, y, math.cos(a) * 120, math.sin(a) * 120,
                      self.now, 0.42, 9.0, color or OPTIONS["ink"], drag=5.0)

    def note(self, x, y):
        self._add("note", x, y, self.rng.uniform(-18, 18), -self.rng.uniform(40, 60),
                  self.now, 1.4, self.rng.choice([13, 15, 17]),
                  self.rng.choice([OPTIONS["accent"], "#a855f7", "#f43f5e"]),
                  phase=self.rng.uniform(0, 6.28), text=self.rng.choice(["♪", "♫"]))

    def heart(self, x, y):
        self._add("note", x, y, self.rng.uniform(-10, 10), -45, self.now, 1.3, 15,
                  "#f43f5e", phase=self.rng.uniform(0, 6.28), text="♥")

    # ---- render ----
    def draw(self, canvas) -> None:
        now = self.now
        alive = []
        for p in self.parts:
            age = now - p.t0
            if age < 0 or age > p.life:
                continue
            alive.append(p)
            k = age / p.life
            if p.drag > 0:
                f = (1.0 - math.exp(-p.drag * age)) / p.drag
            else:
                f = age
            x = p.x + p.vx * f
            y = p.y + p.vy * f + 0.5 * p.g * age * age
            if p.kind == "spark":
                r = p.size * (1.0 - k) ** 0.8
                if r < 0.6:
                    continue
                rot = p.spin * age
                pts = []
                for j in range(8):
                    rr = r if j % 2 == 0 else r * 0.38
                    a = rot + j * math.pi / 4
                    pts += [x + math.cos(a) * rr, y + math.sin(a) * rr]
                canvas.create_polygon(pts, fill=p.color, outline="")
            elif p.kind == "puff":
                r = p.size * (0.6 + 0.9 * k) * (1.0 - k ** 3)
                canvas.create_oval(x - r, y - r, x + r, y + r, fill=p.color, outline="")
            elif p.kind == "confetti":
                x += math.sin(age * 7 + p.phase) * 10 * k
                flip = abs(math.cos(age * p.spin + p.phase))
                hw, hh = p.size * 0.5, max(0.8, p.size * 0.28 * flip + 0.6)
                a = age * p.spin * 0.6 + p.phase
                ca, sa = math.cos(a), math.sin(a)
                pts = []
                for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
                    pts += [x + dx * ca - dy * sa, y + dx * sa + dy * ca]
                canvas.create_polygon(pts, fill=p.color, outline="")
            elif p.kind == "ring":
                r = p.vx + (p.vy - p.vx) * _ease_out(k)
                wdt = max(0.6, p.size * (1.0 - k))
                col = mix(p.color, OPTIONS["page"], k * 0.85)
                canvas.create_oval(p.x - r, p.y - r, p.x + r, p.y + r, outline=col, width=wdt)
            elif p.kind == "dash":
                ln = p.size * (1.0 - k)
                if ln < 1:
                    continue
                n = math.hypot(p.vx, p.vy) or 1.0
                ux, uy = p.vx / n, p.vy / n
                canvas.create_line(x, y, x + ux * ln, y + uy * ln, fill=p.color,
                                   width=2.5, capstyle="round")
            elif p.kind == "note":
                x += math.sin(age * 4 + p.phase) * 8
                sz = max(6, int(p.size * (1.0 - k * 0.6)))
                canvas.create_text(x, y, text=p.text, fill=p.color,
                                   font=(_SYMBOL_FONT, sz, "bold"))
        self.parts = alive


FX = FXSystem()


# ---------------------------------------------------------------------------
# Renderer state (velocity tracking for squash/stretch, trails, gaze, mood)
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_frame = -1
        self.last_hip = None
        self.last_t = 0.0
        self.last_angle = 270.0
        self.vy = 0.0
        self.prev_vy = 0.0
        self.spin = 0.0
        self.squash_t0 = -10.0
        self.last_land = -10.0
        self.history: list = []
        self.gaze = 0.0
        self.expression = "smile"
        self.last_head = (0.0, 0.0)
        self.ground = None


ST = _State()


def kick_squash() -> None:
    """Animations call this on a hard landing to force a squash."""
    ST.squash_t0 = FX.now


def _track_motion(hip_x, hip_y, body_angle) -> None:
    if ST.last_frame == FX.frame:
        return
    now = FX.now
    if ST.last_hip is not None and now > ST.last_t:
        dt = now - ST.last_t
        dx, dy = hip_x - ST.last_hip[0], hip_y - ST.last_hip[1]
        if math.hypot(dx, dy) > 60 or dt > 0.2:  # phase jump / teleport
            ST.vy = ST.prev_vy = 0.0
            ST.spin = 0.0
            ST.ground = None
        else:
            raw = dy / dt
            ST.prev_vy = ST.vy
            ST.vy = ST.vy * 0.45 + raw * 0.55
            da = ((body_angle - ST.last_angle + 540) % 360) - 180
            ST.spin = ST.spin * 0.5 + (da / dt) * 0.5
            upright = abs(((body_angle - 270 + 540) % 360) - 180) < 35
            if (upright and ST.prev_vy > 45 and ST.vy < 12
                    and now - ST.last_land > 0.28):
                ST.last_land = now
                ST.squash_t0 = now
    ST.last_hip = (hip_x, hip_y)
    ST.last_t = now
    ST.last_angle = body_angle
    ST.last_frame = FX.frame


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _taper(canvas, x1, y1, x2, y2, w1, w2, fill):
    """A limb segment that's thicker at the root than at the tip, with
    rounded ends. Reads as a drawn ink stroke instead of a uniform wire."""
    dx, dy = x2 - x1, y2 - y1
    ln = math.hypot(dx, dy)
    if ln > 0.5:
        nx, ny = -dy / ln, dx / ln
        canvas.create_polygon(
            x1 + nx * w1 / 2, y1 + ny * w1 / 2,
            x2 + nx * w2 / 2, y2 + ny * w2 / 2,
            x2 - nx * w2 / 2, y2 - ny * w2 / 2,
            x1 - nx * w1 / 2, y1 - ny * w1 / 2,
            fill=fill, outline="",
        )
    for (cx, cy, r) in ((x1, y1, w1 / 2), (x2, y2, w2 / 2)):
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="")


def _dot(canvas, x, y, r, fill):
    canvas.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline="")


def _face_next(canvas, hx, hy, hr, body_angle, ink, style):
    """Expressive face: blinking eyes that look where the figure is going,
    and a mouth that changes with the figure's mood."""
    expr = ST.expression
    up = _project(0, 0, hr * 0.12, body_angle)
    gz = _project(0, 0, ST.gaze * hr * 0.16, body_angle + 90)
    ecx, ecy = hx + up[0] + gz[0], hy + up[1] + gz[1]
    blink = (FX.now % 3.7) < 0.13 or (2.1 < (FX.now % 7.3) < 2.22)
    closed = blink or expr in ("sleep", "calm")
    erx, ery = hr * 0.12, hr * 0.19
    for s in (+1, -1):
        ex, ey = _project(ecx, ecy, hr * 0.36, body_angle + 90 * s)
        if closed:
            if expr == "calm":   # happy closed eyes: little upside-down arcs
                a = _project(ex, ey, erx * 1.4, body_angle + 90)
                b = _project(ex, ey, erx * 1.4, body_angle - 90)
                m = _project(ex, ey, ery * 0.8, body_angle)
                canvas.create_line(a[0], a[1], m[0], m[1], b[0], b[1], smooth=True,
                                   fill=ink, width=1.6, capstyle="round")
            else:
                a = _project(ex, ey, erx * 1.5, body_angle + 90)
                b = _project(ex, ey, erx * 1.5, body_angle - 90)
                canvas.create_line(a[0], a[1], b[0], b[1], fill=ink, width=1.6,
                                   capstyle="round")
        else:
            r = ery if expr != "o" else ery * 1.15
            canvas.create_oval(ex - erx, ey - r, ex + erx, ey + r, fill=ink, outline="")
    if style == "toon":
        for s in (+1, -1):
            cx, cy = _project(hx, hy, hr * 0.55, body_angle + 90 * s)
            cx, cy = _project(cx, cy, hr * 0.22, body_angle + 180)
            canvas.create_oval(cx - hr * 0.16, cy - hr * 0.09, cx + hr * 0.16, cy + hr * 0.09,
                               fill="#fda4af", outline="")
    down = body_angle + 180
    mc = _project(hx, hy, hr * 0.36, down)
    if expr == "o":
        r = hr * 0.17
        canvas.create_oval(mc[0] - r * 0.8, mc[1] - r, mc[0] + r * 0.8, mc[1] + r,
                           fill=ink, outline="")
    elif expr == "grin":
        l = _project(mc[0], mc[1], hr * 0.36, body_angle + 90)
        rr = _project(mc[0], mc[1], hr * 0.36, body_angle - 90)
        b = _project(mc[0], mc[1], hr * 0.3, down)
        canvas.create_polygon(l[0], l[1], b[0], b[1], rr[0], rr[1], smooth=True,
                              fill=ink, outline=ink, width=1)
    elif expr == "effort":
        l = _project(mc[0], mc[1], hr * 0.3, body_angle + 90)
        rr = _project(mc[0], mc[1], hr * 0.3, body_angle - 90)
        canvas.create_line(l[0], l[1], rr[0], rr[1], fill=ink, width=1.8, capstyle="round")
    elif expr == "sleep":
        r = hr * 0.09
        canvas.create_oval(mc[0] - r, mc[1] - r, mc[0] + r, mc[1] + r, outline=ink, width=1.4)
    else:  # smile / calm
        l = _project(mc[0], mc[1], hr * 0.33, body_angle + 90)
        rr = _project(mc[0], mc[1], hr * 0.33, body_angle - 90)
        b = _project(mc[0], mc[1], hr * 0.2, down)
        canvas.create_line(l[0], l[1], b[0], b[1], rr[0], rr[1], smooth=True,
                           fill=ink, width=1.8, capstyle="round")


# ---------------------------------------------------------------------------
# The renderer (since 1.0.85; the pre-1.0.85 wireframe look is
# _draw_figure_pose_classic, still used for style='classic')
# ---------------------------------------------------------------------------


def draw_figure_pose(
    canvas, hip_x, hip_y, *,
    body_angle=270.0,
    left_arm=(90.0, 90.0), right_arm=(90.0, 90.0),
    left_leg=(90.0, 90.0), right_leg=(90.0, 90.0),
    head_r=HEAD_R, scale=1.0, stroke=STROKE, color=COLOR,
):
    style = OPTIONS["style"]
    ink = OPTIONS["ink"]
    _track_motion(hip_x, hip_y, body_angle)

    # ---- squash & stretch (torso only, so feet stay planted) ----
    torso_k = 1.0
    upright = abs(((body_angle - 270 + 540) % 360) - 180) < 35
    if OPTIONS["squash"] and upright:
        stretch = min(0.13, abs(ST.vy) / 650.0)
        sq = max(0.0, 1.0 - (FX.now - ST.squash_t0) / 0.18)
        torso_k = 1.0 + stretch - 0.17 * sq * sq

    body_len = LEN_BODY * scale * torso_k
    ua, fa = LEN_UPPER_ARM * scale, LEN_FOREARM * scale
    ul, ll = LEN_UPPER_LEG * scale, LEN_LOWER_LEG * scale
    hr = head_r * scale

    neck = _project(hip_x, hip_y, body_len, body_angle)
    head = _project(neck[0], neck[1], hr + 2, body_angle)
    arms = []
    for ua_ang, fa_ang in (left_arm, right_arm):
        e = _project(neck[0], neck[1], ua, ua_ang)
        h = _project(e[0], e[1], fa, fa_ang)
        arms.append((e, h))
    legs = []
    for ul_ang, ll_ang in (left_leg, right_leg):
        k = _project(hip_x, hip_y, ul, ul_ang)
        f = _project(k[0], k[1], ll, ll_ang)
        legs.append((k, f))

    # ---- ground shadow ----
    if OPTIONS["shadow"]:
        lowest = max([f[1] for _k, f in legs] + [hip_y, head[1] + hr])
        if FX.ground_hint is not None:
            ground = FX.ground_hint
        elif upright and ST.vy < -40 and ST.ground is not None:
            ground = ST.ground          # airborne: shadow stays on the floor
        elif not upright and abs(ST.spin) > 120 and ST.ground is not None:
            ground = max(lowest, ST.ground)   # spinning: keep the floor level
        else:
            ground = lowest
        ST.ground = ground
        xs = [f[0] for _k, f in legs] + [hip_x, head[0]]
        spread = max(26.0 * scale, (max(xs) - min(xs)) + 10)
        air = max(0.0, ground - lowest)
        shrink = max(0.35, 1.0 - air / 110.0)
        rx = spread / 2 * shrink
        ry = 3.6 * scale * shrink
        sx = (hip_x + sum(f[0] for _k, f in legs) / 2) / 2
        canvas.create_oval(sx - rx, ground + 1 - ry, sx + rx, ground + 1 + ry,
                           fill=mix(OPTIONS["ground"], OPTIONS["page"], 1 - shrink), outline="")

    # ---- motion smear for fast spins ----
    if OPTIONS["trails"]:
        if abs(ST.spin) > 260:
            ghosts = ST.history[-3:]
            cols = [OPTIONS["ghost2"], OPTIONS["ghost1"], OPTIONS["ghost1"]][-len(ghosts):]
            for g, col in zip(ghosts, cols):
                for (x1, y1, x2, y2) in g["segs"]:
                    canvas.create_line(x1, y1, x2, y2, fill=col, width=4, capstyle="round")
                gx, gy = g["head"]
                canvas.create_oval(gx - hr, gy - hr, gx + hr, gy + hr, outline=col, width=3)
        segs = [(hip_x, hip_y, neck[0], neck[1])]
        for (e, h) in arms:
            segs += [(neck[0], neck[1], e[0], e[1]), (e[0], e[1], h[0], h[1])]
        for (k, f) in legs:
            segs += [(hip_x, hip_y, k[0], k[1]), (k[0], k[1], f[0], f[1])]
        if not ST.history or ST.history[-1]["frame"] != FX.frame:
            ST.history.append({"frame": FX.frame, "segs": segs, "head": head})
            ST.history = ST.history[-4:]

    if style == "classic":
        _paint_classic(canvas, hip_x, hip_y, neck, head, hr, arms, legs, ink, scale)
    else:
        _paint_ink(canvas, hip_x, hip_y, neck, head, hr, arms, legs, ink, scale, style)

    # ---- face + character decoration ----
    try:
        if OPTIONS["eyes"]:
            _face_next(canvas, head[0], head[1], hr, body_angle, ink, style)
        else:
            _draw_face(canvas, head[0], head[1], hr, body_angle,
                          scale=scale, stroke=STROKE, color=ink)
    except Exception:
        pass
    deco = CHARACTERS.get(get_character(), CHARACTERS["plain"])["draw_extra"]
    try:
        deco(canvas, head[0], head[1], hr, body_angle,
             scale=scale, stroke=2.6 if style != "classic" else STROKE, color=ink)
    except Exception:
        pass

    ST.last_head = head
    return head[0], head[1], hr


def _paint_classic(canvas, hip_x, hip_y, neck, head, hr, arms, legs, ink, scale):
    w = STROKE
    canvas.create_line(hip_x, hip_y, neck[0], neck[1], fill=ink, width=w, capstyle="round")
    for (e, h) in arms:
        canvas.create_line(neck[0], neck[1], e[0], e[1], h[0], h[1], fill=ink, width=w,
                           capstyle="round", joinstyle="round")
    for (k, f) in legs:
        canvas.create_line(hip_x, hip_y, k[0], k[1], f[0], f[1], fill=ink, width=w,
                           capstyle="round", joinstyle="round")
    canvas.create_oval(head[0] - hr, head[1] - hr, head[0] + hr, head[1] + hr,
                       outline=ink, width=w, fill=OPTIONS["skin"])


def _paint_ink(canvas, hip_x, hip_y, neck, head, hr, arms, legs, ink, scale, style):
    b = 3.0 * scale
    if style == "toon":
        body_col = OPTIONS["accent"]
        passes = [(ink, 3.2), (body_col, 0.0)]
    else:
        passes = [(ink, 0.0)]
    for col, extra in passes:
        for (k, f) in legs:
            _taper(canvas, hip_x, hip_y, k[0], k[1], 1.55 * b + extra, 1.25 * b + extra, col)
            _taper(canvas, k[0], k[1], f[0], f[1], 1.25 * b + extra, 1.0 * b + extra, col)
        _taper(canvas, hip_x, hip_y, neck[0], neck[1], 1.7 * b + extra, 2.15 * b + extra, col)
        for (e, h) in arms:
            _taper(canvas, neck[0], neck[1], e[0], e[1], 1.4 * b + extra, 1.15 * b + extra, col)
            _taper(canvas, e[0], e[1], h[0], h[1], 1.15 * b + extra, 0.95 * b + extra, col)
        for (e, h) in arms:
            _dot(canvas, h[0], h[1], 0.95 * b + extra / 2, col)
        for (k, f) in legs:
            _dot(canvas, f[0], f[1], 1.0 * b + extra / 2, col)
    # Head: solid fill so the face pops, with a confident outline.
    canvas.create_oval(head[0] - hr, head[1] - hr, head[0] + hr, head[1] + hr,
                       fill=OPTIONS["skin"], outline=ink, width=2.6 if style == "ink" else 3.0)


# ---------------------------------------------------------------------------
# Motion helpers: walk with hip bob + lean; emerge / hide add a sparkle
# ---------------------------------------------------------------------------

def _walk(canvas, from_xy, to_xy, local_t, *, gait_speed: float = 7.0):
    fx, fy = from_xy
    tx, ty = to_xy
    eased = _ease_in_out(local_t)
    hx = _lerp(fx, tx, eased)
    hy = _lerp(fy, ty, eased)
    gait_phase = local_t * gait_speed
    ll, rl, la, rar = _walk_pose(gait_phase)
    d = 0 if abs(tx - fx) < 4 else (1 if tx > fx else -1)
    body = 270.0
    if OPTIONS["walk_bob"]:
        s = math.sin(gait_phase)
        hy -= (1.0 - abs(s)) * 3.0           # rise over the planted foot
        body += d * 6.0 * math.sin(math.pi * local_t)   # lean into the walk
    # Footstep dust each time a foot plants.
    plant = int(math.floor((gait_phase - math.pi / 2) / math.pi))
    if plant >= 0 and FX.once("step%d_%d_%d" % (int(fx), int(tx), plant)):
        FX.puff(hx - d * 6, hy + LEN_UPPER_LEG + LEN_LOWER_LEG, n=2, size=3.5, spread=16)
    ST.gaze = d
    draw_figure_pose(canvas, hx, hy, body_angle=body,
                        left_leg=ll, right_leg=rl, left_arm=la, right_arm=rar)
    ST.gaze = 0.0


def _emerge(canvas, button, local_t, side):
    _emerge_base(canvas, button, local_t, side)
    if local_t > 0.82 and FX.once("emerge"):
        hx, hy = _emerge_position(button, side, 0.0)
        FX.sparkle(hx, hy - 52, n=9, speed=130)
        FX.ring(hx, hy - 52, 4, 26, 0.4)


def _hide_back(canvas, button, local_t, side):
    _hide_back_base(canvas, button, local_t, side)
    if local_t > 0.85 and FX.once("hide"):
        FX.sparkle(button["x"] + side * button["w"] / 2, button["y"] - button["h"] / 2,
                   n=6, speed=90, size=5)


def reset_play() -> None:
    FX.reset()
    ST.reset()


# ---------------------------------------------------------------------------
# Moods + extra effects layered onto the nine existing animations
# ---------------------------------------------------------------------------


def _in(t, a, b):
    return a <= t <= b


def _mood_wrapper(anim_id, fn, duration_ms):
    def wrapped(canvas, t, w, h, button):
        expr = "smile"
        after = None
        if anim_id == "surprise":
            if _in(t, 0.25, 0.85):
                expr = "o"
                after = "surprise"
        elif anim_id == "sleep":
            if _in(t, 0.15, 0.22):
                expr = "o"
            elif _in(t, 0.24, 0.77):
                expr = "sleep"
        elif anim_id == "weights":
            if _in(t, 0.13, 0.42) or _in(t, 0.52, 0.82):
                expr = "effort"
        elif anim_id == "dance":
            if _in(t, 0.15, 0.40) or _in(t, 0.48, 0.85):
                expr = "grin"
                after = "notes"
        elif anim_id == "yoga":
            if _in(t, 0.13, 0.32) or _in(t, 0.40, 0.62) or _in(t, 0.70, 0.83):
                expr = "calm"
        elif anim_id in ("jacks", "cartwheel", "juggle"):
            if _in(t, 0.10, 0.90):
                expr = "grin"
        ST.expression = expr
        fn(canvas, t, w, h, button)
        ST.expression = "smile"
        hx, hy = ST.last_head
        if after == "surprise" and FX.once("pop"):
            FX.pop_lines(hx, hy - 16)
        elif after == "notes":
            slot = int(t * duration_ms / 420)
            if FX.once("note%d" % slot):
                FX.note(hx + FX.rng.uniform(-18, 18), hy - 20)
        if anim_id == "weights" and (_in(t, 0.40, 0.42) or _in(t, 0.80, 0.82)):
            if FX.once("lift%d" % int(t * 10)):
                FX.sparkle(hx, hy - 30, n=8, speed=110)
        if anim_id == "yoga" and (_in(t, 0.30, 0.32) or _in(t, 0.60, 0.62) or _in(t, 0.81, 0.83)):
            if FX.once("yoga%d" % int(t * 10)):
                FX.heart(hx + 14, hy - 18)
    return wrapped


# ---------------------------------------------------------------------------
# New animations
# ---------------------------------------------------------------------------

LEG = LEN_UPPER_LEG + LEN_LOWER_LEG


def _depth(leg):
    """Vertical distance hip -> foot for a (upper, lower) leg angle pair."""
    return (LEN_UPPER_LEG * math.sin(math.radians(leg[0]))
            + LEN_LOWER_LEG * math.sin(math.radians(leg[1])))


def _crouch_legs(c):
    return (90 + 32 * c, 90 - 24 * c), (90 - 32 * c, 90 + 24 * c)


def _stand_on(ground, l_leg, r_leg):
    return ground - max(_depth(l_leg), _depth(r_leg))


def draw_anim_celebrate(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge = _emerge_position(button, side, 0.0)
    (spot,) = _random_spots(button, w, h, 1, side)
    gx = spot[0]
    ground = spot[1] + LEG

    if (l := _phase(t, 0.00, 0.06)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.06, 0.20)) is not None:
        _walk(canvas, edge, spot, l); return
    if (l := _phase(t, 0.20, 0.29)) is not None:
        # Anticipation: sink down, arms swing back.
        c = _ease_in_out(l)
        lleg, rleg = _crouch_legs(c)
        ST.expression = "grin"
        draw_figure_pose(canvas, gx, _stand_on(ground, lleg, rleg),
                            body_angle=270 - side * 6 * c,
                            left_leg=lleg, right_leg=rleg,
                            left_arm=(95 + 40 * c, 95 + 30 * c),
                            right_arm=(85 - 40 * c, 85 - 30 * c))
        ST.expression = "smile"
        return
    if (l := _phase(t, 0.29, 0.47)) is not None:
        # Big jump with a fist-pump star pose; confetti at the apex.
        arc = math.sin(math.pi * l)
        tuck = arc
        lleg = (90 + 18 * tuck, 90 - 40 * tuck)
        rleg = (90 - 18 * tuck, 90 + 40 * tuck)
        FX.ground_hint = ground
        hip_y = _stand_on(ground, lleg, rleg) - 78 * arc
        up = min(1.0, l * 3.0)
        la = _lerp_angle(135, 228, _ease_out_back(up))
        rarm = _lerp_angle(45, 312, _ease_out_back(up))
        ST.expression = "grin"
        draw_figure_pose(canvas, gx, hip_y, left_leg=lleg, right_leg=rleg,
                            left_arm=(la, la - 8), right_arm=(rarm, rarm + 8))
        ST.expression = "smile"
        if l > 0.42 and FX.once("confetti"):
            FX.confetti(gx, hip_y - 60, n=56)
            FX.ring(gx, hip_y - 40, 8, 60, 0.5)
        return
    if (l := _phase(t, 0.47, 0.54)) is not None:
        # Land: squash + dust.
        if FX.once("land"):
            kick_squash()
            FX.puff(gx, ground, n=4, size=5, spread=34)
        c = 0.85 * (1 - _ease_in_out(l))
        lleg, rleg = _crouch_legs(c)
        FX.ground_hint = ground
        draw_figure_pose(canvas, gx, _stand_on(ground, lleg, rleg),
                            left_leg=lleg, right_leg=rleg,
                            left_arm=(228 - 60 * l, 220), right_arm=(312 + 60 * l, 320))
        return
    if (l := _phase(t, 0.54, 0.74)) is not None:
        # Victory fist pumps with little hops.
        pump = math.sin(l * 6 * math.pi)
        hop = abs(math.sin(l * 3 * math.pi)) * 10
        FX.ground_hint = ground
        ST.expression = "grin"
        arm_up = 282 + 14 * pump
        draw_figure_pose(canvas, gx, ground - LEG - hop,
                            left_arm=(128, 40), right_arm=(arm_up, arm_up - 10 - 8 * pump))
        ST.expression = "smile"
        if FX.once("pump%d" % int(l * 3)):
            hx, hy = ST.last_head
            FX.sparkle(hx + 22, hy - 30, n=5, speed=80, size=5)
        return
    if (l := _phase(t, 0.74, 0.84)) is not None:
        _wave_pose(canvas, spot[0], spot[1], l, side); return
    if (l := _phase(t, 0.84, 0.95)) is not None:
        _walk(canvas, spot, edge, l); return
    if (l := _phase(t, 0.95, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


def _moonwalk_legs(p, face, travel):
    s = math.sin(2 * math.pi * p)
    out = []
    for b in (max(0.0, s), max(0.0, -s)):
        # Bent leg: knee pushed forward, heel up, toe on the floor.
        # Straight leg: slides back flat toward the direction of travel.
        upper = 90 - face * 38 * b - travel * 16 * (1 - b)
        lower = upper + face * 74 * b
        out.append((upper, lower))
    return out


def draw_anim_moonwalk(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge = _emerge_position(button, side, 0.0)
    (spot1,) = _random_spots(button, w, h, 1, side)
    room_r = w - 70 - spot1[0]
    room_l = spot1[0] - 70
    travel = 1 if room_r > room_l else -1
    dist = min(240.0, max(room_r, room_l))
    spot2 = (spot1[0] + travel * dist, spot1[1])
    face = -travel
    ground = spot1[1] + LEG

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.17)) is not None:
        _walk(canvas, edge, spot1, l); return
    if (l := _phase(t, 0.17, 0.62)) is not None:
        # The glide: perfectly linear travel while the legs do the
        # heel-toe slide facing the other way.
        x = _lerp(spot1[0], spot2[0], l)
        p = l * 5.0
        lleg, rleg = _moonwalk_legs(p, face, travel)
        FX.ground_hint = ground
        sway = math.sin(2 * math.pi * p)
        ST.gaze = face
        draw_figure_pose(canvas, x, _stand_on(ground, lleg, rleg),
                            body_angle=270 + face * 8,
                            left_leg=lleg, right_leg=rleg,
                            left_arm=(90 + face * (10 + 8 * sway), 90 - face * 25),
                            right_arm=(90 - face * (6 + 8 * sway), 90 - face * 35))
        ST.gaze = 0.0
        slot = int(p * 2)
        if FX.once("glide%d" % slot):
            FX.puff(x + travel * 10, ground, n=1, size=3, spread=10)
        return
    if (l := _phase(t, 0.62, 0.76)) is not None:
        # Up on the toes, hand to the hat. Hee-hee!
        rise = _ease_out_back(min(1.0, l * 2.5))
        FX.ground_hint = ground
        ST.expression = "o"
        draw_figure_pose(canvas, spot2[0], ground - LEG - 6 * rise,
                            body_angle=270 - face * 4 * rise,
                            left_leg=(92 + face * 3, 92 + face * 6),
                            right_leg=(88 + face * 3, 88 + face * 6),
                            left_arm=(_lerp_angle(95, 245 if face < 0 else 295, rise),
                                      _lerp_angle(90, 330 if face < 0 else 210, rise)),
                            right_arm=(70 if face < 0 else 110, 60 if face < 0 else 120))
        ST.expression = "smile"
        if l > 0.3 and FX.once("hee"):
            hx, hy = ST.last_head
            FX.sparkle(hx, hy - 14, n=10, speed=120)
        return
    if (l := _phase(t, 0.76, 0.84)) is not None:
        _wave_pose(canvas, spot2[0], spot2[1], l, side); return
    if (l := _phase(t, 0.84, 0.95)) is not None:
        _walk(canvas, spot2, edge, l); return
    if (l := _phase(t, 0.95, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


def _draw_board(canvas, cx, top_y, angle):
    ink, acc = OPTIONS["ink"], OPTIONS["accent"]
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)

    def rot(px, py):
        return cx + px * ca - py * sa, top_y + px * sa + py * ca

    deck = [rot(-29, -5), rot(-21, 0), rot(21, 0), rot(29, -5)]
    flat = [c for p in deck for c in p]
    canvas.create_line(*flat, fill=ink, width=7, capstyle="round", joinstyle="round", smooth=True)
    canvas.create_line(*flat, fill=acc, width=4, capstyle="round", joinstyle="round", smooth=True)
    for wx in (-14, 14):
        x, y = rot(wx, 5.5)
        canvas.create_oval(x - 3.6, y - 3.6, x + 3.6, y + 3.6, fill=ink, outline="")
        canvas.create_oval(x - 1.2, y - 1.2, x + 1.2, y + 1.2, fill="#f8fafc", outline="")


def draw_anim_skateboard(canvas, t, w, h, button):
    side = _pick_emerge_side(button, w)
    edge = _emerge_position(button, side, 0.0)
    (spot1,) = _random_spots(button, w, h, 1, side)
    room_r = w - 80 - spot1[0]
    room_l = spot1[0] - 80
    travel = 1 if room_r > room_l else -1
    dist = max(room_r, room_l)
    spot2 = (spot1[0] + travel * dist, spot1[1])
    ground = spot1[1] + LEG
    deck_top = ground - 10
    ride_l, ride_r = (115, 95), (65, 85)

    if (l := _phase(t, 0.00, 0.05)) is not None:
        _emerge(canvas, button, l, side); return
    if (l := _phase(t, 0.05, 0.16)) is not None:
        _walk(canvas, edge, spot1, l); return
    if (l := _phase(t, 0.16, 0.22)) is not None:
        # Board rolls in, figure hops on.
        e = _ease_out(min(1.0, l * 1.6))
        bx = spot1[0] - travel * 70 * (1 - e)
        _draw_board(canvas, bx, deck_top, 0)
        hop = math.sin(math.pi * l) * 16
        c = min(1.0, l * 1.4)
        lleg = (_lerp(90, ride_l[0], c), _lerp(90, ride_l[1], c))
        rleg = (_lerp(90, ride_r[0], c), _lerp(90, ride_r[1], c))
        base = _lerp(ground, deck_top, c)
        FX.ground_hint = ground
        draw_figure_pose(canvas, spot1[0], _stand_on(base, lleg, rleg) - hop,
                            left_leg=lleg, right_leg=rleg,
                            left_arm=(150, 160), right_arm=(30, 20))
        if l > 0.6 and FX.once("board_on"):
            FX.puff(spot1[0], ground, n=3, size=4)
        return
    if (l := _phase(t, 0.22, 0.64)) is not None:
        e = _ease_in_out(l)
        x = _lerp(spot1[0], spot2[0], e)
        speed = math.sin(math.pi * l)
        ollie = _phase(l, 0.42, 0.62)
        jump = math.sin(math.pi * ollie) * 40 if ollie is not None else 0.0
        tilt = (-travel * 16 * math.sin(math.pi * min(1.0, ollie * 1.6))
                if ollie is not None else 0.0)
        if ollie is not None and ollie > 0.95 and FX.once("ollie_land"):
            kick_squash()
            FX.puff(x, ground, n=4, size=5, spread=30)
        if ollie is not None and ollie > 0.45 and FX.once("ollie_top"):
            FX.sparkle(x, deck_top - 90, n=8, speed=110)
        FX.ground_hint = ground
        # speed lines
        if speed > 0.35:
            for i, (dy, ln) in enumerate(((-58, 26), (-38, 38), (-18, 22))):
                off = (FX.now * 300 + i * 40) % 30
                x0 = x - travel * (40 + off)
                canvas.create_line(x0, deck_top + dy - jump, x0 - travel * ln * speed,
                                   deck_top + dy - jump, fill=OPTIONS["ghost1"],
                                   width=2.5, capstyle="round")
        _draw_board(canvas, x, deck_top - jump, tilt)
        sway = math.sin(l * 9) * 10
        ST.gaze = travel
        ST.expression = "grin"
        draw_figure_pose(canvas, x, _stand_on(deck_top - jump, ride_l, ride_r),
                            body_angle=270 + travel * 7 * speed,
                            left_leg=ride_l, right_leg=ride_r,
                            left_arm=(170 + sway, 160 + sway), right_arm=(10 + sway, 20 + sway))
        ST.gaze = 0.0
        ST.expression = "smile"
        return
    if (l := _phase(t, 0.64, 0.72)) is not None:
        # Step off, kick the board up -- it vanishes in a poof.
        FX.ground_hint = ground
        if l < 0.55:
            k = l / 0.55
            _draw_board(canvas, spot2[0] + travel * 10 * k, deck_top - 50 * k * k,
                        -travel * 80 * k)
        elif FX.once("poof"):
            FX.puff(spot2[0] + travel * 10, deck_top - 50, n=6, size=7, spread=30)
            FX.sparkle(spot2[0] + travel * 10, deck_top - 50, n=8, speed=120)
        c = 1 - min(1.0, l * 2)
        lleg = (_lerp(90, ride_l[0], c), _lerp(90, ride_l[1], c))
        rleg = (_lerp(90, ride_r[0], c), _lerp(90, ride_r[1], c))
        draw_figure_pose(canvas, spot2[0] - travel * 6, _stand_on(ground, lleg, rleg),
                            left_leg=lleg, right_leg=rleg,
                            left_arm=(120, 110), right_arm=(60, 70))
        return
    if (l := _phase(t, 0.72, 0.81)) is not None:
        _wave_pose(canvas, spot2[0] - travel * 6, spot2[1], l, side); return
    if (l := _phase(t, 0.81, 0.95)) is not None:
        _walk(canvas, (spot2[0] - travel * 6, spot2[1]), edge, l); return
    if (l := _phase(t, 0.95, 1.00)) is not None:
        _hide_back(canvas, button, l, side); return


def set_palette(ink: str, accent: str, page: str, dark: bool = False) -> None:
    """Match the figure and its effects to the app theme. `page` is the
    color the figure mostly walks over (the card color); shadows, dust and
    motion trails are blended from it so they read as soft tints."""
    global COLOR
    OPTIONS.update(
        ink=ink, accent=accent,
        skin=page if dark else "#ffffff",
        page=page,
        ground=mix(page, ink, 0.16),
        puff=mix(page, ink, 0.13),
        ghost1=mix(page, ink, 0.28),
        ghost2=mix(page, ink, 0.15),
    )
    # Props drawn by the animations themselves (juggling balls, weights,
    # "Zzz", "!") read the module-level COLOR at draw time.
    COLOR = ink


# ---------------------------------------------------------------------------
# Registry — keep in iteration order so the picker lists them stably.
# Each entry's key is the stable id used in the config file's preferences
# dict. Renaming a key is a breaking change for user preferences.
# ---------------------------------------------------------------------------

ANIMATIONS: dict = {
    "surprise":  {"name": "Surprised!",        "duration_ms": 5000,  "draw_fn": draw_anim_surprise},
    "stretches": {"name": "Stretches",         "duration_ms": 10000, "draw_fn": draw_anim_stretches},
    "juggle":    {"name": "Juggling",          "duration_ms": 9000,  "draw_fn": draw_anim_juggle},
    "jacks":     {"name": "Jumping jacks",     "duration_ms": 9000,  "draw_fn": draw_anim_jacks},
    "sleep":     {"name": "Power nap",         "duration_ms": 10000, "draw_fn": draw_anim_sleep},
    "weights":   {"name": "Lifts weights",     "duration_ms": 10000, "draw_fn": draw_anim_weights},
    "dance":     {"name": "Little dance",      "duration_ms": 10000, "draw_fn": draw_anim_dance},
    "cartwheel": {"name": "Cartwheels",        "duration_ms": 8000,  "draw_fn": draw_anim_cartwheel},
    "yoga":      {"name": "Yoga poses",        "duration_ms": 10000, "draw_fn": draw_anim_yoga},
}
# The original nine get facial expressions and a few effects layered on
# (surprised "O" + pop lines, music notes while dancing, effort face while
# lifting, hearts during yoga, ...) without touching their choreography.
for _aid, _entry in ANIMATIONS.items():
    _entry["draw_fn"] = _mood_wrapper(_aid, _entry["draw_fn"], _entry["duration_ms"])
del _aid, _entry
ANIMATIONS.update({
    "celebrate":  {"name": "Celebrate!",  "duration_ms": 7500, "draw_fn": draw_anim_celebrate},
    "moonwalk":   {"name": "Moonwalk",    "duration_ms": 8000, "draw_fn": draw_anim_moonwalk},
    "skateboard": {"name": "Skateboard",  "duration_ms": 8500, "draw_fn": draw_anim_skateboard},
})


# ---------------------------------------------------------------------------
# Trigger state machine — used by the main app only. Per-session memory.
# ---------------------------------------------------------------------------


class AnimationTrigger:
    """Decides when an outgoing Ready should fire an animation.

    A send only 'qualifies' if at least MIN_SPACING_SEC has elapsed since the
    previous qualifying send. Rapid clicks all collapse into a single
    qualifying send — preventing someone from triggering the bonus by
    spamming the Ready button. Once the qualifying-send count reaches a
    randomly chosen target in [MIN_COUNT, MAX_COUNT], an animation fires
    and the counter resets with a fresh random target.

    When constructed with load_from_storage=True (the main app does this),
    the count, target, and last-qualifying timestamp survive app restarts
    and computer shutdown via the shared config file. Wall-clock time is
    used for the spacing check so the 10-minute floor still applies after
    the user closes and reopens the app — otherwise quitting after a
    qualifying send would reset the spacing rule and defeat the anti-spam
    intent. Defaults to in-memory-only when load_from_storage is False so
    unit tests stay deterministic."""

    MIN_SPACING_SEC = 10 * 60
    MIN_COUNT = 4
    MAX_COUNT = 8

    # When True, on_ready_sent() always returns True regardless of the
    # spacing/count rules. Useful while iterating on animation choreography —
    # the user can mash the Ready button and see something every time. Flip
    # to False before publishing a build for actual customers.
    TEST_MODE = False

    def __init__(
        self,
        rng: Optional[random.Random] = None,
        *,
        load_from_storage: bool = False,
    ):
        self._rng = rng or random.Random()
        self._persist = load_from_storage
        self._last_qualifying_epoch: float = -float("inf")
        self._qualifying_count: int = 0
        self._target: int = self._pick_target()
        if load_from_storage:
            self._load()

    def _pick_target(self) -> int:
        return self._rng.randint(self.MIN_COUNT, self.MAX_COUNT)

    def _load(self) -> None:
        state = load_animation_trigger_state()
        target = state.get("target")
        if isinstance(target, int) and self.MIN_COUNT <= target <= self.MAX_COUNT:
            self._target = target
        count = state.get("count")
        if isinstance(count, int) and 0 <= count < self._target:
            self._qualifying_count = count
        last = state.get("last_qualifying_epoch")
        if isinstance(last, (int, float)):
            self._last_qualifying_epoch = float(last)

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            save_animation_trigger_state({
                "count": self._qualifying_count,
                "target": self._target,
                "last_qualifying_epoch": self._last_qualifying_epoch,
            })
        except OSError:
            pass  # cosmetic feature — disk errors must not surface

    def on_ready_sent(self) -> bool:
        """Call once per outgoing Ready broadcast. Returns True if the caller
        should fire an animation now."""
        if self.TEST_MODE:
            return True
        now = time.time()
        if now - self._last_qualifying_epoch < self.MIN_SPACING_SEC:
            return False  # too soon since last qualifying send
        self._last_qualifying_epoch = now
        self._qualifying_count += 1
        fired = False
        if self._qualifying_count >= self._target:
            self._qualifying_count = 0
            self._target = self._pick_target()
            fired = True
        self._save()
        return fired


# ---------------------------------------------------------------------------
# Convenience for the main app's "fire a random enabled animation" path.
# ---------------------------------------------------------------------------


# Default enabled state per animation. Used when the user has NOT explicitly
# toggled an entry via the picker (the picker writes True/False per id into
# the config file). Animations with a False default still appear in the
# picker — the user can opt them in by checking the box.
#
# Current production set per user feedback (1.0.46): Stretches, Jumping jacks,
# Lifts weights, Little dance, Cartwheels — the five the user considers
# polished enough to ship. Surprise / Juggling / Power nap / Yoga poses
# default off but are kept available for future tuning.
DEFAULT_ENABLED: dict[str, bool] = {
    "surprise":  False,
    "stretches": True,
    "juggle":    False,
    "jacks":     True,
    "sleep":     False,
    "weights":   True,
    "dance":     True,
    "cartwheel": True,
    "yoga":      False,
    # 1.0.85 additions.
    "celebrate":  True,
    "moonwalk":   True,
    "skateboard": True,
}


def is_animation_enabled(anim_id: str, prefs: Optional[dict] = None) -> bool:
    """True if the user has either explicitly enabled this animation in the
    config file, or if it defaults to enabled and the user hasn't opted out.
    Single source of truth so the picker UI and the live trigger agree."""
    if prefs is None:
        prefs = load_animation_prefs()
    return bool(prefs.get(anim_id, DEFAULT_ENABLED.get(anim_id, True)))


def pick_random_enabled_animation(
    prefs: Optional[dict] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Return a random animation id from the set that the user has enabled,
    or None if all are disabled."""
    if prefs is None:
        prefs = load_animation_prefs()
    if rng is None:
        rng = random
    enabled = [aid for aid in ANIMATIONS if is_animation_enabled(aid, prefs)]
    if not enabled:
        return None
    return rng.choice(enabled)
