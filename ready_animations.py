"""
Whimsical stick-figure animations for Chairside Ready Alert.

Triggered randomly on the SENDING workstation after a few qualifying outgoing
Ready messages — a small reward for the staff using the app. A little
character peeks over (or pops up from behind) the Ready button, hops onto
its top edge, plays a short scene with real emotions, and hops back behind
the button. Pure cosmetic feature; no networking, no persistence beyond a
per-animation on/off flag in the existing config file.

Architecture (so the picker preview is pixel-identical to production):

  - StickFigureOverlay: a transparent, click-through-ish Toplevel above the
    parent window (Windows -transparentcolor, macOS -transparent, opaque
    fallback elsewhere). Never draw pure magenta: it is the Windows key.
  - AnimationPlayer drives a Scene at ~60fps with real elapsed time.
  - Scene = a timeline of beats (fn(scene, beat) called every frame). Beats
    only set *targets*; the Actor rig and its Face follow them through
    damped springs, so every transition overshoots and settles naturally
    instead of snapping. Each play gets a fresh seed: scenes pick their
    intro/outro, moves, outcomes and little surprises (sneezes, hiccups,
    winks) at random, and the figure glances at the mouse when it's near.
  - Actor: two-bone IK limbs, planted-foot walking gait, auto-stepping
    balance, squash & stretch, head lag, 3/4 head turns, character
    decorations from CHARACTERS.
  - Face: lid-clipped eyes with highlights, blinks/saccades, brows, mouth
    with teeth/tongue, blush, cheek puff, and special eyes (hearts, stars,
    spirals) — see EXPRESSIONS.
  - ANIMATIONS maps a stable id to {name, duration_ms, scene}. Both the
    picker and the main app iterate it.
  - AnimationTrigger counts qualifying outgoing Ready sends (10-minute
    spacing) and fires after a random 3-5. Config helpers read/write the
    SAME config file as the main app ("animation_preferences").

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
    canvas.create_polygon(pts, smooth=True, fill=color, outline="")
    color2 = button.get("color2")
    if color2 and color2 != color:
        _gradient_strips(canvas, x0, y0, x1, y1, r, color, color2)
    font = button.get("font") or _button_label_font(int(h))
    subtitle = button.get("subtitle") or ""
    if subtitle:
        canvas.create_text(x, y - h * 0.15, text=label, fill=text_color, font=font)
        canvas.create_text(x, y + h * 0.25, text=subtitle, fill=text_color,
                           font=(font[0], max(8, int(font[1]) // 2)))
    else:
        canvas.create_text(x, y, text=label, fill=text_color, font=font)


def _gradient_strips(canvas: tk.Canvas, x0, y0, x1, y1, r, c1: str, c2: str) -> None:
    """Left-to-right gradient fill clipped to a rounded rect (matches the
    ReadyButton body in chairside_ready_alert)."""
    def rgb(c):
        c = c.lstrip("#")
        return [int(c[i:i + 2], 16) for i in (0, 2, 4)]
    try:
        a, b = rgb(c1), rgb(c2)
    except (ValueError, IndexError):
        return
    w = int(x1 - x0)
    for i in range(0, w, 2):
        x = x0 + i + 1
        d = (x0 + r - x) if x < x0 + r else (x - (x1 - r)) if x > x1 - r else 0.0
        inset = r - math.sqrt(max(0.0, r * r - d * d)) if d > 0 else 0.0
        if y1 - y0 - 2 * inset < 1:
            continue
        t = i / max(1, w)
        col = "#%02x%02x%02x" % tuple(int(p + (q - p) * t) for p, q in zip(a, b))
        canvas.create_line(x, y0 + inset, x, y1 - inset, width=2, fill=col)


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
    """Runs one Scene at ~60fps on the overlay canvas. Time is measured with
    a monotonic clock so a slow frame never speeds up or skips choreography;
    only one animation plays at a time."""

    FRAME_MS = 16

    def __init__(
        self,
        root: tk.Misc,
        overlay: StickFigureOverlay,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        self.root = root
        self.overlay = overlay
        self.on_complete = on_complete
        self._scene = None
        self._last = 0.0
        self._after_id: Optional[str] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def play(self, anim_id: str, button: dict) -> None:
        """Play `anim_id`. `button` describes the Ready button in overlay
        coordinates: x, y (centre), w, h, color, color2, text_color, label,
        radius, subtitle, font, plus page (window colour) / dark / canvas_w /
        canvas_h. Each play draws a fresh random seed, so the same animation
        never plays out the same way twice."""
        if anim_id not in ANIMATIONS:
            return
        self.stop()
        seed = random.randint(0, 10**9)
        b = dict(button)
        b["_seed"] = seed
        canvas = self.overlay.canvas
        w = int(b.get("canvas_w") or canvas.winfo_width())
        h = int(b.get("canvas_h") or canvas.winfo_height())
        try:
            self._scene = ANIMATIONS[anim_id]["scene"](b, w, h, random.Random(seed))
        except Exception:
            self._scene = None
            return
        self._scene.pointer = self._pointer
        self._last = time.monotonic()
        self._running = True
        self._tick()

    def _pointer(self):
        """Mouse position in canvas coords while it's over the window (the
        figure glances at it), else None (the figure looks at the viewer)."""
        c = self.overlay.canvas
        try:
            x = c.winfo_pointerx() - c.winfo_rootx()
            y = c.winfo_pointery() - c.winfo_rooty()
            if 0 <= x <= c.winfo_width() and 0 <= y <= c.winfo_height():
                return (x, y)
        except tk.TclError:
            pass
        return None

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
        self._after_id = None
        if not self._running or self._scene is None:
            return
        now = time.monotonic()
        dt = min(0.05, max(0.0, now - self._last))
        self._last = now
        canvas = self.overlay.canvas
        try:
            self._scene.update(dt)
            canvas.delete("all")
            self._scene.draw(canvas)
        except tk.TclError:
            self.stop()
            return
        except Exception:
            # Cosmetic feature: a bug in a scene must never surface.
            self._scene.done = True
        if self._scene.done:
            self._running = False
            self.overlay.clear()
            if self.on_complete:
                try:
                    self.on_complete()
                except Exception:
                    pass
            return
        try:
            self._after_id = self.root.after(self.FRAME_MS, self._tick)
        except tk.TclError:
            self.stop()


def _resolve_hex(widget: tk.Misc, color: str, default: str) -> str:
    try:
        r, g, b = widget.winfo_rgb(color)
        return "#%02x%02x%02x" % (r // 257, g // 257, b // 257)
    except (tk.TclError, TypeError):
        return default


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

    parent_window: a tk.Tk or tk.Toplevel — the window the figure appears on.
    button_widget: the Ready button. The figure lives on its top edge and
                   hides behind it at the start and end.
    character:     character id from CHARACTERS. If None, leaves the current
                   character setting alone.
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
        # Buttons that draw their own shadow (ReadyButton) report the margin
        # around the visible body so the mask lines up with the body itself.
        inset = getattr(button_widget, "_body_inset", None) or (0, 0, 0, 0)
        btn_w = max(1, btn_w - inset[0] - inset[2])
        btn_h = max(1, btn_h - inset[1] - inset[3])
        bx_screen = button_widget.winfo_rootx() + inset[0] + btn_w // 2  # type: ignore[attr-defined]
        by_screen = button_widget.winfo_rooty() + inset[1] + btn_h // 2  # type: ignore[attr-defined]
    except tk.TclError:
        return None

    overlay = StickFigureOverlay(parent_window)
    overlay.show(px, py, pw, ph)

    try:
        page_raw = parent_window.cget("bg")  # type: ignore[attr-defined]
    except tk.TclError:
        page_raw = "#edf1f8"
    page = _resolve_hex(parent_window, page_raw, "#edf1f8")

    # Pull the button's look off the widget so the mask matches it.
    # ReadyButton / RoundedButton / the picker's mock all expose these.
    button_info = {
        "x": bx_screen - px,
        "y": by_screen - py,
        "w": btn_w,
        "h": btn_h,
        "color": getattr(button_widget, "_bg", "#2563eb"),
        "text_color": getattr(button_widget, "_fg", "#ffffff"),
        "label": getattr(button_widget, "_text", "Ready"),
        "radius": getattr(button_widget, "_r", 10),
        "color2": getattr(button_widget, "_bg2", None),
        "subtitle": getattr(button_widget, "_subtitle", ""),
        "font": getattr(button_widget, "_label_font", None),
        "page": page,
        "dark": _is_dark_color(page),
        "canvas_w": pw,
        "canvas_h": ph,
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
    if not player.running:
        overlay.hide()
        overlay.destroy()
        return None
    return player


# ===========================================================================
# Scene engine: spring-driven rig, expressive face, particles, choreography.
# Everything visible follows a damped spring toward targets that the current
# beat sets, so switching beats never pops — motion overshoots and settles
# the way real bodies (and cartoon ones) do.
# ===========================================================================


def _clamp(v, a, b):
    return a if v < a else b if v > b else v


def _smooth(t):
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _smoother(t):
    t = _clamp(t, 0.0, 1.0)
    return t * t * t * (t * (6 * t - 15) + 10)


def _ease_out(t):
    return 1 - (1 - _clamp(t, 0.0, 1.0)) ** 3


def _ease_in(t):
    t = _clamp(t, 0.0, 1.0)
    return t * t * t


def _ease_out_back(t, s=1.70158):
    t = _clamp(t, 0.0, 1.0) - 1
    return 1 + t * t * ((s + 1) * t + s)


def _seg(p, a, b):
    """Map p in [a, b] onto 0..1 (clamped)."""
    if b <= a:
        return 1.0 if p >= b else 0.0
    return _clamp((p - a) / (b - a), 0.0, 1.0)


def _hexrgb(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return [int(c[i:i + 2], 16) for i in (0, 2, 4)]


def _mix(c1, c2, t):
    try:
        a, b = _hexrgb(c1), _hexrgb(c2)
    except (ValueError, IndexError):
        return c1
    t = _clamp(t, 0.0, 1.0)
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _is_dark_color(c):
    try:
        r, g, b = _hexrgb(c)
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def make_palette(page="#edf1f8", dark=None, accent="#2563eb"):
    """Colors for the figure. Never returns magenta (Windows transparency key)."""
    if not (isinstance(page, str) and page.startswith("#")):
        page = "#edf1f8"
    if dark is None:
        dark = _is_dark_color(page)
    ink = "#e2e8f0" if dark else "#1e293b"
    head = _mix(page, ink, 0.16) if dark else "#ffffff"
    return {
        "page": page, "dark": dark, "ink": ink, "head": head,
        "mouth": "#7f1d2d", "tongue": "#fb7185", "teeth": "#ffffff",
        "blush": "#fb7185", "accent": accent,
        "shadow": _mix(page, "#000000", 0.38 if dark else 0.13),
        "dust": _mix(page, ink, 0.22),
        "sweat": "#60a5fa", "gold": "#facc15", "heart": "#f43f5e",
    }


class _Spring:
    """Second-order damped spring. f = natural frequency (Hz), z = damping
    ratio (<1 overshoots, 1 = critically damped)."""
    __slots__ = ("x", "v", "f", "z")

    def __init__(self, x=0.0, f=4.0, z=0.8):
        self.x, self.v, self.f, self.z = float(x), 0.0, f, z

    def step(self, target, dt, f=None, z=None):
        w = 2 * math.pi * (self.f if f is None else f)
        k, c = w * w, 2 * (self.z if z is None else z) * w
        n = int(dt / 0.004) + 1
        h = dt / n
        x, v = self.x, self.v
        for _ in range(n):
            v += (k * (target - x) - c * v) * h
            x += v * h
        self.x, self.v = x, v
        return x

    def snap(self, x):
        self.x, self.v = float(x), 0.0


def _rot(x, y, deg):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return x * ca - y * sa, x * sa + y * ca


def _ik(rx, ry, tx, ty, a, b, px, py):
    """Two-bone IK. Root (rx,ry), target (tx,ty), bone lengths a,b. The joint
    bends toward pole direction (px,py). Returns (joint, end)."""
    dx, dy = tx - rx, ty - ry
    d = math.hypot(dx, dy)
    if d < 1e-6:
        dx, dy, d = 0.0, 1e-6, 1e-6
    ux, uy = dx / d, dy / d
    dc = _clamp(d, abs(a - b) + 0.01, a + b - 0.01)
    cos_al = (a * a + dc * dc - b * b) / (2 * a * dc)
    al = math.acos(_clamp(cos_al, -1.0, 1.0))
    best = None
    for sg in (1, -1):
        c, s = math.cos(sg * al), math.sin(sg * al)
        jx = rx + a * (ux * c - uy * s)
        jy = ry + a * (ux * s + uy * c)
        score = (jx - rx) * px + (jy - ry) * py
        if best is None or score > best[0]:
            best = (score, jx, jy)
    ex, ey = rx + ux * dc, ry + uy * dc
    if d <= a + b - 0.01:
        ex, ey = tx, ty
    return (best[1], best[2]), (ex, ey)


# ---------------------------------------------------------------------------
# Face
# ---------------------------------------------------------------------------

_NEUTRAL = dict(brow=0.0, tilt=0.0, brow_l=0.0, brow_r=0.0, lid=0.1, lid_lo=0.05,
                eye=1.0, smile=0.35, open=0.0, width=0.8, smirk=0.0, blush=0.12,
                teeth=0.0, puff=0.0, wink=0.0, tongue=0.0, cross=0.0)

# Spring (frequency, damping) per channel: lids snap, mouths overshoot a
# touch, brows are lively, blush creeps in slowly.
_FACE_SPRING = dict(brow=(5.5, 0.5), tilt=(5.0, 0.6), brow_l=(5.5, 0.5), brow_r=(5.5, 0.5),
                    lid=(8.0, 0.9), lid_lo=(6.0, 0.8), eye=(5.0, 0.45), smile=(4.5, 0.6),
                    open=(6.5, 0.55), width=(5.0, 0.6), smirk=(4.0, 0.7), blush=(1.3, 1.0),
                    teeth=(6.0, 0.9), puff=(4.0, 0.5), wink=(10.0, 0.9), tongue=(5.0, 0.8),
                    cross=(4.0, 0.8))

EXPRESSIONS = {
    "neutral": {},
    "happy": dict(brow=0.15, lid_lo=0.35, smile=0.8, open=0.12, width=1.0, blush=0.2),
    "joy": dict(brow=0.45, lid_lo=0.5, smile=1.0, open=0.7, width=1.15, blush=0.45, teeth=0.6, tongue=0.6),
    "delight": dict(mode="happy", brow=0.4, smile=1.0, open=0.55, width=1.1, blush=0.55, tongue=0.6),
    "laugh": dict(mode="happy", brow=0.5, tilt=0.3, smile=1.0, open=0.85, width=1.0, blush=0.6, tongue=0.7),
    "surprise": dict(brow=1.0, lid=0.0, lid_lo=0.0, eye=1.25, smile=0.0, open=0.8, width=0.42),
    "curious": dict(brow_l=0.6, brow_r=0.05, eye=1.08, smile=0.15, open=0.05, width=0.55),
    "mischief": dict(brow_l=0.55, brow_r=-0.25, tilt=-0.3, lid=0.38, smile=0.65, smirk=0.75, width=0.85),
    "determined": dict(brow=-0.25, tilt=-0.85, lid=0.3, smile=-0.05, width=0.6),
    "effort": dict(brow=-0.3, tilt=-1.0, lid=0.55, lid_lo=0.45, smile=-0.25, open=0.35, width=1.0, teeth=1.0, blush=0.5),
    "sleepy": dict(brow=-0.05, tilt=0.35, lid=0.72, lid_lo=0.1, smile=0.1, width=0.6),
    "asleep": dict(mode="closed", brow=-0.1, smile=0.2, open=0.1, width=0.35, blush=0.25),
    "worried": dict(brow=0.4, tilt=0.95, eye=1.05, smile=-0.35, open=0.08, width=0.6),
    "sad": dict(brow=0.2, tilt=1.0, lid=0.3, smile=-0.6, width=0.6),
    "embarrassed": dict(brow=0.3, tilt=0.55, lid=0.2, smile=0.4, smirk=0.3, width=0.6, blush=1.0),
    "proud": dict(brow=0.3, lid=0.4, lid_lo=0.3, smile=0.9, width=1.0, blush=0.25),
    "smitten": dict(mode="heart", brow=0.4, smile=0.9, open=0.2, width=0.9, blush=0.85),
    "starry": dict(mode="star", brow=0.6, smile=1.0, open=0.6, width=1.1, blush=0.5, tongue=0.5),
    "dizzy": dict(mode="spiral", brow=0.3, tilt=0.5, smile=-0.1, open=0.3, width=0.6, smirk=0.5),
    "yawn": dict(brow=0.35, tilt=0.6, lid=0.85, lid_lo=0.35, smile=0.0, open=1.0, width=0.55, tongue=0.5),
    "puff": dict(brow=0.2, lid=0.25, smile=0.0, open=0.0, width=0.3, puff=1.0, blush=0.6),
    "thinking": dict(brow_l=0.55, brow_r=-0.1, smile=0.05, smirk=-0.5, width=0.5),
    "whistle": dict(brow=0.3, lid=0.2, smile=0.25, open=0.3, width=0.25),
    "calm": dict(mode="closed", brow=0.1, smile=0.55, width=0.8, blush=0.2),
    "knocked": dict(mode="x", brow=0.2, smile=-0.2, open=0.25, width=0.5),
}


class Face:
    def __init__(self, rng):
        self.rng = rng
        self.s = {k: _Spring(v, *_FACE_SPRING[k]) for k, v in _NEUTRAL.items()}
        self.target = dict(_NEUTRAL)
        self.expr = "neutral"
        self.mode = "normal"
        self.pending = None
        self.blink_t = None
        self.next_blink = rng.uniform(0.8, 2.5)
        self.time = 0.0
        self.lid_now = 0.1
        self.gx, self.gy = _Spring(0, 9, 0.95), _Spring(0, 9, 0.95)
        self.gaze = (0.0, 0.0)
        self.sacc = (0.0, 0.0)
        self.sacc_timer = 0.5
        self.saccades = True
        self.talk = 0.0        # 0..1 mouth chatter (singing, giggling)
        self.talk_hz = 7.0

    def set(self, name, **over):
        e = EXPRESSIONS.get(name, {})
        tgt = dict(_NEUTRAL)
        tgt.update({k: v for k, v in e.items() if k != "mode"})
        tgt.update(over)
        mode = e.get("mode", "normal")
        if name != self.expr and self.rng.random() < 0.45:
            self.blink()
        self.expr = name
        self.target = tgt
        if mode != self.mode and mode != self.pending:
            self.pending = mode
            self.blink()

    def nudge(self, **over):
        self.target.update(over)

    def blink(self):
        if self.blink_t is None:
            self.blink_t = 0.0

    def look(self, gx, gy):
        self.gaze = (_clamp(gx, -1, 1), _clamp(gy, -1, 1))

    def update(self, dt):
        self.time += dt
        if self.blink_t is None:
            self.next_blink -= dt
            if self.next_blink <= 0:
                self.blink_t = 0.0
                self.next_blink = 0.22 if self.rng.random() < 0.15 else self.rng.uniform(1.8, 4.8)
        extra = 0.0
        if self.blink_t is not None:
            self.blink_t += dt
            bt = self.blink_t
            if bt < 0.055:
                extra = bt / 0.055
            elif bt < 0.09:
                extra = 1.0
                if self.pending is not None:
                    self.mode, self.pending = self.pending, None
            elif bt < 0.21:
                extra = 1.0 - _smooth((bt - 0.09) / 0.12)
            else:
                self.blink_t = None
                if self.pending is not None:
                    self.mode, self.pending = self.pending, None
        for k, sp in self.s.items():
            sp.step(self.target[k], dt)
        self.lid_now = max(_clamp(self.s["lid"].x, 0, 1), extra)
        self.sacc_timer -= dt
        if self.sacc_timer <= 0:
            self.sacc_timer = self.rng.uniform(0.35, 1.5)
            self.sacc = (self.rng.gauss(0, 0.16), self.rng.gauss(0, 0.1)) if self.saccades else (0.0, 0.0)
        self.gx.step(self.gaze[0] + self.sacc[0], dt)
        self.gy.step(self.gaze[1] + self.sacc[1], dt)

    # -- drawing -----------------------------------------------------------

    def draw(self, cv, hx, hy, r, ang, turn, pal, sx=1.0, sy=1.0):
        p = {k: sp.x for k, sp in self.s.items()}
        if self.talk > 0:
            p["open"] = max(p["open"], self.talk * (0.5 + 0.5 * math.sin(self.time * self.talk_hz * 2 * math.pi)))
        a = math.radians(ang)
        ca, sa = math.cos(a), math.sin(a)

        def T(u, v):
            u *= sx
            v *= sy
            return (hx + u * ca - v * sa, hy + u * sa + v * ca)

        ink = pal["ink"]
        fx = turn * 0.30 * r
        lw = max(1.3, 0.085 * r)
        # Cheeks
        if p["blush"] > 0.06:
            col = _mix(pal["head"], pal["blush"], 0.15 + 0.6 * _clamp(p["blush"], 0, 1))
            for side in (-1, 1):
                cx = fx + side * 0.56 * r * (1 - 0.25 * max(0.0, side * turn))
                pts = []
                for i in range(12):
                    th = i / 12 * 2 * math.pi
                    pts += T(cx + 0.17 * r * math.cos(th), 0.24 * r + 0.09 * r * math.sin(th))
                cv.create_polygon(pts, fill=col, outline="", smooth=True)
        gx, gy = self.gx.x, self.gy.x
        mode = self.mode
        eye_s = _clamp(p["eye"], 0.6, 1.5)
        for side in (-1, 1):
            near = side * turn
            ex = fx + side * 0.35 * r * (1 - 0.16 * abs(turn))
            ey = -0.07 * r
            rx = 0.15 * r * (1 - 0.32 * max(0.0, near)) * eye_s
            ry = 0.22 * r * eye_s
            # brows
            bh = p["brow"] + (p["brow_l"] if side < 0 else p["brow_r"])
            base = ey - ry - 0.15 * r - bh * 0.13 * r
            tilt = p["tilt"]
            iu = ex - side * 0.12 * r
            ou = ex + side * 0.15 * r * (1 - 0.3 * max(0.0, near))
            iv = base - tilt * 0.10 * r
            ov = base + tilt * 0.035 * r
            mv = (iv + ov) / 2 - 0.045 * r * (1 - 0.5 * abs(tilt))
            cv.create_line(*T(iu, iv), *T((iu + ou) / 2, mv), *T(ou, ov), smooth=True,
                           fill=ink, width=lw * 1.15, capstyle="round")
            # eyes
            cross = p["cross"] * -side * 0.9
            egx = _clamp(gx + cross, -1.2, 1.2)
            pcx, pcy = ex + egx * 0.09 * r, ey + gy * 0.07 * r
            if mode in ("heart", "star", "spiral", "x"):
                self._special_eye(cv, T, pcx, pcy, r, mode, pal, side)
                continue
            lid = self.lid_now
            if side < 0:
                lid = max(lid, _clamp(p["wink"], 0, 1))
            lo = _clamp(p["lid_lo"], 0, 1)
            angry = _clamp(-tilt, 0, 1)
            sad = _clamp(tilt, 0, 1)

            def ytop(u):
                du = _clamp((u - pcx) / rx, -1, 1)
                inner = -side * du
                return (pcy - ry + 2 * ry * lid + angry * 0.55 * ry * (inner + 1) / 2
                        + sad * 0.4 * ry * (1 - inner) / 2)

            def ybot(u):
                du = _clamp((u - pcx) / rx, -1, 1)
                return pcy + ry - lo * 1.15 * ry * (1 - 0.55 * du * du)

            closed = mode in ("happy", "closed") or (ybot(pcx) - ytop(pcx)) < 0.24 * ry
            if closed:
                happy = mode == "happy" or (mode == "normal" and lo > 0.3) or (side < 0 and p["wink"] > 0.5 and p["smile"] > 0.3)
                w2 = rx * 1.25
                if happy:
                    pts = (*T(pcx - w2, pcy + 0.15 * ry), *T(pcx, pcy - 0.5 * ry), *T(pcx + w2, pcy + 0.15 * ry))
                else:
                    yl = pcy + 0.15 * ry
                    pts = (*T(pcx - w2, yl), *T(pcx, yl + 0.35 * ry), *T(pcx + w2, yl))
                cv.create_line(*pts, smooth=True, fill=ink, width=lw * 1.2, capstyle="round")
                continue
            pts = []
            for i in range(22):
                th = i / 22 * 2 * math.pi
                u = pcx + rx * math.cos(th)
                v = pcy + ry * math.sin(th)
                v = _clamp(v, ytop(u), max(ytop(u), ybot(u)))
                pts += T(u, v)
            cv.create_polygon(pts, fill=ink, outline=ink, width=1)
            hr_ = 0.36 * rx
            hxu, hyv = pcx - 0.3 * rx, pcy - 0.4 * ry
            if hyv - hr_ > ytop(hxu) and hyv + hr_ < ybot(hxu):
                hp = T(hxu, hyv)
                cv.create_oval(hp[0] - hr_, hp[1] - hr_, hp[0] + hr_, hp[1] + hr_, fill=pal["head"], outline="")
                h2 = T(pcx + 0.35 * rx, pcy + 0.3 * ry)
                if pcy + 0.3 * ry < ybot(pcx):
                    rr = hr_ * 0.45
                    cv.create_oval(h2[0] - rr, h2[1] - rr, h2[0] + rr, h2[1] + rr, fill=pal["head"], outline="")
        self._mouth(cv, T, p, r, fx, turn, pal, lw)

    def _special_eye(self, cv, T, cx, cy, r, mode, pal, side):
        ink = pal["ink"]
        if mode == "heart":
            k = 0.27 * r * (1 + 0.12 * math.sin(self.time * 9))
            pts = []
            for i in range(24):
                th = i / 24 * 2 * math.pi
                x = 16 * math.sin(th) ** 3
                y = -(13 * math.cos(th) - 5 * math.cos(2 * th) - 2 * math.cos(3 * th) - math.cos(4 * th))
                pts += T(cx + x / 16 * k, cy + y / 16 * k)
            cv.create_polygon(pts, fill=pal["heart"], outline=_mix(pal["heart"], "#000000", 0.3), smooth=True)
        elif mode == "star":
            k = 0.3 * r * (1 + 0.1 * math.sin(self.time * 7))
            rot = self.time * 1.5 * side
            pts = []
            for i in range(10):
                rr = k if i % 2 == 0 else k * 0.45
                th = rot - math.pi / 2 + i * math.pi / 5
                pts += T(cx + rr * math.cos(th), cy + rr * math.sin(th))
            cv.create_polygon(pts, fill=pal["gold"], outline=_mix(pal["gold"], "#000000", 0.35), width=1)
        elif mode == "spiral":
            pts = []
            rot = self.time * 7 * side
            for i in range(26):
                f = i / 25
                th = rot + f * 3.2 * math.pi
                pts += T(cx + 0.22 * r * f * math.cos(th), cy + 0.22 * r * f * math.sin(th))
            cv.create_line(pts, fill=ink, width=max(1.2, 0.07 * r), smooth=True)
        else:  # x
            k = 0.15 * r
            cv.create_line(*T(cx - k, cy - k), *T(cx + k, cy + k), fill=ink, width=max(1.4, 0.09 * r), capstyle="round")
            cv.create_line(*T(cx - k, cy + k), *T(cx + k, cy - k), fill=ink, width=max(1.4, 0.09 * r), capstyle="round")

    def _mouth(self, cv, T, p, r, fx, turn, pal, lw):
        ink = pal["ink"]
        s, o, k = _clamp(p["smile"], -1, 1), _clamp(p["open"], 0, 1.1), _clamp(p["smirk"], -1, 1)
        cx, cy = fx * 1.12, 0.40 * r
        hw = 0.30 * r * _clamp(p["width"], 0.15, 1.4) * (1 - 0.18 * abs(turn))
        if p["puff"] > 0.3:
            hw *= 1 - 0.5 * _clamp(p["puff"], 0, 1)
        lx, ly = cx - hw, cy - s * 0.13 * r + k * 0.06 * r
        rx_, ry_ = cx + hw, cy - s * 0.13 * r - k * 0.10 * r
        if o < 0.06:
            mid = (cx + k * 0.04 * r, cy + s * 0.11 * r)
            cv.create_line(*T(lx, ly), *T(*mid), *T(rx_, ry_), smooth=True, fill=ink, width=lw * 1.15, capstyle="round")
            return
        top_y = cy - o * 0.05 * r + s * 0.03 * r
        bot_y = cy + s * 0.10 * r + o * 0.36 * r
        L, R = T(lx, ly), T(rx_, ry_)
        pts = [*L, *L,
               *T(cx - hw * 0.5, (ly + top_y) / 2 + 0.01 * r), *T(cx, top_y), *T(cx + hw * 0.5, (ry_ + top_y) / 2 + 0.01 * r),
               *R, *R,
               *T(cx + hw * 0.62, bot_y - 0.12 * r * o), *T(cx, bot_y), *T(cx - hw * 0.62, bot_y - 0.12 * r * o)]
        cv.create_polygon(pts, fill=pal["mouth"], outline=ink, width=max(1.0, lw * 0.8), smooth=True)
        if p["tongue"] > 0.2 and o > 0.3:
            tw, th = hw * 0.55, 0.09 * r * o
            tcx, tcy = cx, bot_y - th * 1.25
            tp = []
            for i in range(14):
                a_ = i / 14 * 2 * math.pi
                tp += T(tcx + tw * math.cos(a_), tcy + th * math.sin(a_))
            cv.create_polygon(tp, fill=pal["tongue"], outline="", smooth=True)
        if p["teeth"] > 0.1 and o > 0.15:
            band = min(0.09 * r, (bot_y - top_y) * 0.42) * _clamp(p["teeth"], 0, 1)
            tp = [*T(cx - hw * 0.55, (ly + top_y) / 2 + 0.02 * r), *T(cx, top_y + 0.02 * r),
                  *T(cx + hw * 0.55, (ry_ + top_y) / 2 + 0.02 * r),
                  *T(cx + hw * 0.5, (ry_ + top_y) / 2 + band + 0.02 * r), *T(cx, top_y + band + 0.03 * r),
                  *T(cx - hw * 0.5, (ly + top_y) / 2 + band + 0.02 * r)]
            cv.create_polygon(tp, fill=pal["teeth"], outline="", smooth=True)


# ---------------------------------------------------------------------------
# Particles
# ---------------------------------------------------------------------------


class FX:
    def __init__(self, rng, pal, S):
        self.rng, self.pal, self.S = rng, pal, S
        self.parts = []

    def add(self, kind, x, y, vx=0.0, vy=0.0, life=1.0, size=6.0, color=None, g=0.0, drag=0.0, spin=0.0, text=""):
        self.parts.append(dict(kind=kind, x=x, y=y, vx=vx, vy=vy, age=0.0, life=life, size=size * self.S,
                               color=color, g=g, drag=drag, spin=spin, ph=self.rng.uniform(0, 6.28), text=text))

    def sparkle(self, x, y, n=8, speed=120, colors=None):
        colors = colors or [self.pal["gold"], self.pal["accent"], "#fde68a"]
        for i in range(n):
            a = i / n * 2 * math.pi + self.rng.uniform(-0.3, 0.3)
            sp = speed * self.rng.uniform(0.5, 1.0) * self.S
            self.add("spark", x, y, math.cos(a) * sp, math.sin(a) * sp, self.rng.uniform(0.45, 0.8),
                     self.rng.uniform(4, 7), self.rng.choice(colors), g=60, drag=3.0, spin=self.rng.uniform(-5, 5))

    def confetti(self, x, y, n=36, speed=240):
        cols = ["#f43f5e", "#f59e0b", "#10b981", "#3b82f6", "#a855f7", "#06b6d4", "#facc15"]
        for _ in range(n):
            a = self.rng.uniform(-math.pi * 0.92, -math.pi * 0.08)
            sp = speed * self.rng.uniform(0.4, 1.0) * self.S
            self.add("confetti", x, y, math.cos(a) * sp, math.sin(a) * sp, self.rng.uniform(1.4, 2.2),
                     self.rng.uniform(4, 6.5), self.rng.choice(cols), g=380 * self.S, drag=2.0, spin=self.rng.uniform(5, 13))

    def dust(self, x, y, n=4, spread=24):
        for i in range(n):
            d = -1 if i % 2 == 0 else 1
            self.add("dust", x + d * self.rng.uniform(1, 4) * self.S, y, d * self.rng.uniform(0.5, 1) * spread * self.S,
                     -self.rng.uniform(5, 18) * self.S, self.rng.uniform(0.35, 0.6), self.rng.uniform(3, 5),
                     self.pal["dust"], drag=4.0)

    def symbol(self, kind, x, y, vx=0.0, vy=-30.0, life=1.4, size=8.0, color=None, text=""):
        self.add(kind, x, y, vx * self.S, vy * self.S, life, size, color, text=text)

    def update(self, dt):
        alive = []
        for p in self.parts:
            p["age"] += dt
            if p["age"] >= p["life"]:
                continue
            if p["drag"]:
                k = math.exp(-p["drag"] * dt)
                p["vx"] *= k
                p["vy"] *= k
            p["vy"] += p["g"] * dt
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            alive.append(p)
        self.parts = alive

    def draw(self, cv):
        ink = self.pal["ink"]
        for p in self.parts:
            k = p["age"] / p["life"]
            x, y, s, col = p["x"], p["y"], p["size"], p["color"] or ink
            kind = p["kind"]
            pop = _ease_out_back(min(1.0, p["age"] / 0.18))
            fade = 1.0 - _smooth(_seg(k, 0.7, 1.0))
            if kind == "spark":
                rr = s * (1 - k) ** 0.7
                rot = p["spin"] * p["age"]
                pts = []
                for j in range(8):
                    q = rr if j % 2 == 0 else rr * 0.35
                    a = rot + j * math.pi / 4
                    pts += [x + math.cos(a) * q, y + math.sin(a) * q]
                if rr > 0.6:
                    cv.create_polygon(pts, fill=col, outline="")
            elif kind == "confetti":
                x += math.sin(p["age"] * 6 + p["ph"]) * 8 * k * self.S
                flip = abs(math.cos(p["age"] * p["spin"] + p["ph"]))
                hw, hh = s * 0.5, max(0.8, s * 0.3 * flip + 0.5)
                a = p["age"] * p["spin"] * 0.5 + p["ph"]
                pts = []
                for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
                    rx, ry = _rot(dx, dy, math.degrees(a))
                    pts += [x + rx, y + ry]
                cv.create_polygon(pts, fill=col, outline="")
            elif kind == "bubble":
                rr = s * (0.5 + 0.5 * pop) * (1 - 0.3 * k)
                cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=col or "#f0f9ff",
                               outline=_mix(self.pal["ink"], self.pal["page"], 0.55), width=1)
            elif kind == "dust":
                rr = s * (0.6 + 0.9 * k) * (1 - k ** 2)
                cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=col, outline="")
            elif kind == "heart":
                rr = s * pop * fade
                x += math.sin(p["age"] * 4 + p["ph"]) * 5 * self.S
                pts = []
                for i in range(20):
                    th = i / 20 * 2 * math.pi
                    hx = 16 * math.sin(th) ** 3
                    hy = -(13 * math.cos(th) - 5 * math.cos(2 * th) - 2 * math.cos(3 * th) - math.cos(4 * th))
                    pts += [x + hx / 16 * rr, y + hy / 16 * rr]
                if rr > 0.8:
                    cv.create_polygon(pts, fill=self.pal["heart"], outline="", smooth=True)
            elif kind == "note":
                rr = s * 0.42 * pop * fade
                x += math.sin(p["age"] * 4 + p["ph"]) * 6 * self.S
                if rr > 0.6:
                    cv.create_oval(x - rr * 1.25, y - rr, x + rr * 1.25, y + rr, fill=col, outline="")
                    sx = x + rr * 1.1
                    cv.create_line(sx, y, sx, y - s * 1.5 * pop * fade, fill=col, width=max(1.2, 0.2 * s))
                    cv.create_line(sx, y - s * 1.5 * pop * fade, sx + s * 0.55, y - s * 1.05 * pop * fade,
                                   fill=col, width=max(1.2, 0.2 * s))
            elif kind == "drop":
                rr = s * 0.5 * fade
                if rr > 0.5:
                    cv.create_polygon(x, y - rr * 2.2, x + rr, y, x, y + rr, x - rr, y, fill=col or self.pal["sweat"],
                                      outline="", smooth=True)
            elif kind == "star":
                rr = s * pop * fade
                rot = p["age"] * 4
                pts = []
                for i in range(10):
                    q = rr if i % 2 == 0 else rr * 0.45
                    th = rot - math.pi / 2 + i * math.pi / 5
                    pts += [x + q * math.cos(th), y + q * math.sin(th)]
                if rr > 0.6:
                    cv.create_polygon(pts, fill=col or self.pal["gold"], outline="")
            elif kind == "text":
                sz = max(6, int(s * (0.6 + 0.4 * pop) * (0.4 + 0.6 * fade)))
                cv.create_text(x, y, text=p["text"], fill=col, font=("Helvetica", -sz, "bold"))
            elif kind == "ring":
                rr = s * (0.3 + 2.2 * _ease_out(k))
                cv.create_oval(x - rr, y - rr, x + rr, y + rr, outline=col or self.pal["accent"],
                               width=max(0.8, 2.5 * (1 - k) * self.S))


# ---------------------------------------------------------------------------
# The character rig
# ---------------------------------------------------------------------------

# Decorations that belong on the face (move with the 3/4 head turn) rather
# than on the skull.
_FACE_DECOR = {"glasses", "sunglasses", "mustache"}


class Actor:
    """A spring-driven stick figure. Beats set the *_tgt fields; update()
    eases every channel toward them; draw() solves IK and paints."""

    def __init__(self, sc):
        S = sc.S
        self.S, self.pal, self.rng = S, sc.pal, sc.rng
        self.torso, self.ua, self.fa = 29 * S, 15 * S, 14 * S
        self.th, self.sh, self.r, self.gap = 19 * S, 18 * S, 15 * S, 1.5 * S
        self.leg = self.th + self.sh
        self.lw, self.tw = max(2.0, 2.7 * S), max(2.4, 3.3 * S)
        self.ground = sc.top
        x = sc.cx
        y = self.stand_y()
        self.hx, self.hy = _Spring(x, 7, 1.0), _Spring(y, 7, 1.0)
        self.hip_tgt = [x, y]
        self.hip_f = 7.0
        self.lean, self.lean_tgt = _Spring(0, 3, 0.55), 0.0
        self.bend, self.bend_tgt = _Spring(0, 3, 0.6), 0.0
        self.tilt, self.tilt_tgt = _Spring(0, 3.2, 0.42), 0.0
        self.turn, self.turn_tgt = _Spring(0, 3.5, 0.8), 0.0
        self.squash = _Spring(1, 4.5, 0.28)
        self.spin, self.spin_tgt, self.spin_snap = _Spring(0, 3, 0.8), 0.0, False
        self.hand_tgt = [[-6 * S, 26 * S], [6 * S, 26 * S]]
        self.hand = [[_Spring(v, 5, 0.55) for v in h] for h in self.hand_tgt]
        self.hand_fz = [(5.0, 0.55), (5.0, 0.55)]
        self.arm_front = [False, False]
        self.stance = 5.5 * S
        self.feet = [dict(x=x - self.stance, y=self.ground, step=None),
                     dict(x=x + self.stance, y=self.ground, step=None)]
        self.feet_mode = "stand"
        self.foot_rel_tgt = [[-5 * S, self.leg * 0.95], [5 * S, self.leg * 0.95]]
        self.foot_spr = [[_Spring(0, 8, 0.8), _Spring(0, 8, 0.8)] for _ in range(2)]
        self.face = Face(sc.rng)
        self.visible = True
        self.shadow = True
        self.hand_items = [None, None]   # callables(cv, x, y, actor)
        self.vx = 0.0
        self.time = 0.0
        self.landed = False
        self.geo = None
        self.clip_y = None   # while behind the button, feet stay above its bottom edge

    # -- helpers for beats ---------------------------------------------------

    def stand_y(self, crouch=0.0, ground=None):
        g = self.ground if ground is None else ground
        return g - self.leg * (0.965 - crouch)

    def set_hip(self, x=None, y=None):
        if x is not None:
            self.hip_tgt[0] = x
        if y is not None:
            self.hip_tgt[1] = y

    def snap_hip(self, x, y):
        self.hip_tgt = [x, y]
        self.hx.snap(x)
        self.hy.snap(y)

    def hands(self, left=None, right=None):
        if left is not None:
            self.hand_tgt[0] = list(left)
        if right is not None:
            self.hand_tgt[1] = list(right)

    def hang(self, i=None, swing=0.0):
        S = self.S
        for k in ((0, 1) if i is None else (i,)):
            side = -1 if k == 0 else 1
            self.hand_tgt[k] = [side * 6.5 * S + swing, 25.5 * S]

    def hand_world(self, i, wx, wy):
        """Aim hand i at a world point (uses hip/lean targets so it's stable)."""
        ln = math.radians(self.lean_tgt)
        sx = self.hip_tgt[0] + math.sin(ln) * self.torso * 0.84
        sy = self.hip_tgt[1] - math.cos(ln) * self.torso * 0.84
        self.hand_tgt[i] = list(_rot(wx - sx, wy - sy, -self.lean_tgt))

    def set_feet_mode(self, mode):
        if mode == self.feet_mode:
            return
        if mode == "free":
            for i, f in enumerate(self.feet):
                f["step"] = None
                self.foot_spr[i][0].snap(f["x"] - self.hx.x)
                self.foot_spr[i][1].snap(f["y"] - self.hy.x)
        self.feet_mode = mode

    def tuck(self, amount=1.0, spread=5.0):
        S = self.S
        y = self.leg * (0.95 - 0.38 * amount)
        self.foot_rel_tgt = [[-spread * S, y], [spread * S, y]]

    def land(self, strength=1.0, dust=True, fx=None):
        self.squash.v -= 3.2 * strength
        self.set_feet_mode("stand")
        if fx is not None and dust:
            fx.dust(self.hx.x, self.ground, n=4 if strength > 0.6 else 2)

    # -- simulation ----------------------------------------------------------

    def update(self, dt, fx=None):
        if dt <= 0:
            return
        self.time += dt
        ox = self.hx.x
        self.hx.step(self.hip_tgt[0], dt, f=self.hip_f)
        self.hy.step(self.hip_tgt[1], dt, f=self.hip_f)
        vx = (self.hx.x - ox) / dt
        ax = (vx - self.vx) / dt
        self.vx = vx
        S = self.S
        self.lean.step(self.lean_tgt + _clamp(vx * 0.035 / S, -9, 9), dt)
        self.tilt.step(self.tilt_tgt - _clamp(ax * 0.0035 / S, -9, 9), dt)
        self.turn.step(self.turn_tgt, dt)
        self.bend.step(self.bend_tgt, dt)
        self.squash.step(1.0, dt)
        if self.spin_snap:
            self.spin.snap(self.spin_tgt)
        else:
            self.spin.step(self.spin_tgt, dt)
        for i in (0, 1):
            f, z = self.hand_fz[i]
            self.hand[i][0].step(self.hand_tgt[i][0], dt, f, z)
            self.hand[i][1].step(self.hand_tgt[i][1], dt, f, z)
        if self.feet_mode == "free":
            for i, ft in enumerate(self.feet):
                ft["x"] = self.hx.x + self.foot_spr[i][0].step(self.foot_rel_tgt[i][0], dt)
                ft["y"] = self.hy.x + self.foot_spr[i][1].step(self.foot_rel_tgt[i][1], dt)
                if self.clip_y is not None:
                    ft["y"] = min(ft["y"], self.clip_y)
        elif self.feet_mode == "stand":
            for i, ft in enumerate(self.feet):
                other = self.feet[1 - i]
                side = -1 if i == 0 else 1
                desired = self.hip_tgt[0] + side * self.stance
                st = ft["step"]
                if st is None:
                    off = abs(ft["x"] - desired) > 6 * S or abs(ft["y"] - self.ground) > 0.5
                    if off and (other["step"] is None or abs(ft["y"] - self.ground) > 0.5):
                        lift = 4 * S if abs(ft["y"] - self.ground) < 1 else 0.0
                        ft["step"] = dict(t=0.0, dur=0.17, fx=ft["x"], fy=ft["y"], tx=desired, h=lift)
                    st = ft["step"]
                if st is not None:
                    st["t"] += dt
                    q = _clamp(st["t"] / st["dur"], 0, 1)
                    e = _smooth(q)
                    ft["x"] = st["fx"] + (st["tx"] - st["fx"]) * e
                    ft["y"] = st["fy"] + (self.ground - st["fy"]) * e - math.sin(math.pi * q) * st["h"]
                    if q >= 1:
                        ft["step"] = None
                        ft["y"] = self.ground
                        if fx is not None and st["h"] == 0.0:
                            fx.dust(ft["x"], self.ground, n=2, spread=14)
        self.face.update(dt)

    # -- geometry + drawing --------------------------------------------------

    def geometry(self):
        hx, hy = self.hx.x, self.hy.x
        spin = self.spin.x
        lean = self.lean.x
        breath = 1 + 0.014 * math.sin(self.time * 2 * math.pi * 0.32)
        tl = self.torso * _clamp(self.squash.x, 0.6, 1.35) * breath
        up = math.radians(-90 + lean)
        ux, uy = math.cos(up), math.sin(up)
        bend = self.bend.x
        loc = {
            "neck": (ux * tl, uy * tl),
            "mid": (ux * tl * 0.5 - uy * bend, uy * tl * 0.5 + ux * bend),
            "shoulder": (ux * tl * 0.84, uy * tl * 0.84),
        }
        head_dir = -90 + lean + self.tilt.x
        hd = math.radians(head_dir)
        nx, ny = loc["neck"]
        loc["head"] = (nx + math.cos(hd) * (self.gap + self.r), ny + math.sin(hd) * (self.gap + self.r))
        sx, sy = loc["shoulder"]
        for i in (0, 1):
            rx, ry = _rot(self.hand[i][0].x, self.hand[i][1].x, lean)
            loc["hand%d" % i] = (sx + rx, sy + ry)
        free = self.feet_mode == "free"
        if free:
            for i, ft in enumerate(self.feet):
                loc["foot%d" % i] = (ft["x"] - hx, ft["y"] - hy)
        W = {}
        for k, (x, y) in loc.items():
            if not free and k.startswith("foot"):
                continue
            rx, ry = _rot(x, y, spin)
            W[k] = (hx + rx, hy + ry)
        if not free:
            for i, ft in enumerate(self.feet):
                W["foot%d" % i] = (ft["x"], ft["y"])
        turn = _clamp(self.turn.x, -1, 1)
        g = {"hip": (hx, hy), "head_dir": head_dir + spin, "turn": turn, "spin": spin}
        g.update(W)
        for i in (0, 1):
            side = -1 if i == 0 else 1
            px, py = _rot(turn * 1.0 + side * 0.45 * (1 - abs(turn)), -0.15, spin)
            g["knee%d" % i], g["foot%d" % i] = _ik(hx, hy, *W["foot%d" % i], self.th, self.sh, px, py)
            ax_, ay_ = _rot(side * 1.0 - turn * 0.4, 0.55, spin + lean)
            g["elbow%d" % i], g["hand%d" % i] = _ik(*W["shoulder"], *W["hand%d" % i], self.ua, self.fa, ax_, ay_)
        self.geo = g
        return g

    def draw(self, cv):
        if not self.visible:
            return
        g = self.geometry()
        S, pal, ink, r = self.S, self.pal, self.pal["ink"], self.r
        turn = g["turn"]
        if self.shadow:
            low = max(g["foot0"][1], g["foot1"][1])
            air = max(0.0, self.ground - low)
            k = max(0.25, 1 - air / (90 * S))
            cx = (g["foot0"][0] + g["foot1"][0] + 2 * g["hip"][0]) / 4
            rx, ry = 17 * S * k, 3.2 * S * k
            cv.create_oval(cx - rx, self.ground - ry + 1, cx + rx, self.ground + ry + 1,
                           fill=pal["shadow"], outline="")
        for i in (0, 1):
            side = -1 if i == 0 else 1
            hip, kn, ft = g["hip"], g["knee%d" % i], g["foot%d" % i]
            cv.create_line(*hip, *kn, *ft, smooth=True, width=self.lw, fill=ink, capstyle="round", joinstyle="round")
            if abs(turn) > 0.3:
                tx, ty = (4.2 * S if turn > 0 else -4.2 * S), 0.0
            else:
                tx, ty = side * 2.6 * S, 0.4 * S
            tx, ty = _rot(tx, ty, g["spin"])
            cv.create_line(ft[0], ft[1], ft[0] + tx, ft[1] + ty, width=max(2.6, 3.8 * S), fill=ink, capstyle="round")
        cv.create_line(*g["hip"], *g["mid"], *g["neck"], smooth=True, width=self.tw, fill=ink, capstyle="round")
        for i in (0, 1):
            if not self.arm_front[i]:
                self._arm(cv, g, i)
        # Head (a touch of squash on hard landings)
        q = self.squash.x
        hsx, hsy = 1 + 0.45 * (1 - q), 1 - 0.45 * (1 - q)
        puff = self.face.s["puff"].x
        hsx *= 1 + 0.1 * _clamp(puff, 0, 1)
        hx, hy = g["head"]
        ang = g["head_dir"] + 90
        pts = []
        for j in range(30):
            th = j / 30 * 2 * math.pi
            px, py = _rot(r * hsx * math.cos(th), r * hsy * math.sin(th), ang)
            pts += [hx + px, hy + py]
        cv.create_polygon(pts, fill=pal["head"], outline=ink, width=max(1.6, 0.12 * r), smooth=True)
        try:
            self.face.draw(cv, hx, hy, r, ang, turn, pal, hsx, hsy)
        except Exception:
            pass
        cid = _current_character_id
        deco = CHARACTERS.get(cid, CHARACTERS["plain"])["draw_extra"]
        dx, dy = hx, hy
        if cid in _FACE_DECOR:
            ox, oy = _rot(turn * 0.30 * r, 0, ang)
            dx, dy = hx + ox, hy + oy
        try:
            deco(cv, dx, dy, r, (g["head_dir"]) % 360, scale=r / HEAD_R, stroke=max(2.0, 0.14 * r), color=ink)
        except Exception:
            pass
        for i in (0, 1):
            if self.arm_front[i]:
                self._arm(cv, g, i)
        for i in (0, 1):
            item = self.hand_items[i]
            if item is not None:
                try:
                    item(cv, *g["hand%d" % i], self)
                except Exception:
                    pass

    def _arm(self, cv, g, i):
        ink = self.pal["ink"]
        sh, el, hd = g["shoulder"], g["elbow%d" % i], g["hand%d" % i]
        cv.create_line(*sh, *el, *hd, smooth=True, width=self.lw * 0.92, fill=ink, capstyle="round", joinstyle="round")
        rr = 2.5 * self.S
        cv.create_oval(hd[0] - rr, hd[1] - rr, hd[0] + rr, hd[1] + rr, fill=ink, outline="")

    def look_at(self, x, y):
        """Point the eyes (and a little of the head) at a world point."""
        g = self.geo or self.geometry()
        hx, hy = g["head"]
        dx, dy = x - hx, y - hy
        d = math.hypot(dx, dy) or 1.0
        k = min(1.0, d / (70 * self.S))
        self.face.look(dx / d * k, dy / d * k)
        self.turn_tgt = _clamp(dx / d * k * 0.45, -0.45, 0.45)


# ---------------------------------------------------------------------------
# Scenes: a timeline of beats. Each beat is fn(scene, beat) called every
# frame; beat.p runs 0..1, beat.t is seconds in, beat.first marks frame 1.
# ---------------------------------------------------------------------------


class _Beat:
    __slots__ = ("fn", "dur", "start", "data", "t", "p", "first", "st")

    def __init__(self, fn, dur, start, data):
        self.fn, self.dur, self.start, self.data = fn, dur, start, data
        self.t, self.p, self.first, self.st = 0.0, 0.0, True, {}


class Scene:
    def __init__(self, button, w, h, rng):
        self.button, self.w, self.h, self.rng = dict(button), w, h, rng
        self.pal = make_palette(button.get("page") or "#edf1f8", button.get("dark"),
                                button.get("color") or "#2563eb")
        bx, by, bw, bh = button["x"], button["y"], button["w"], button["h"]
        self.x0, self.x1, self.top, self.bot = bx - bw / 2, bx + bw / 2, by - bh / 2, by + bh / 2
        self.S = float(button.get("scale") or _clamp(bh / 72.0, 0.8, 1.8))
        m = 70 * self.S
        self.lo, self.hi = self.x0 + m, self.x1 - m
        if self.hi < self.lo:
            self.lo = self.hi = bx
        self.cx = rng.uniform(self.lo, self.hi)
        self.px = self.cx
        self.fx = FX(rng, self.pal, self.S)
        self.actor = Actor(self)
        self.props = []
        self.beats = []
        self.duration = 0.0
        self.t = 0.0
        self.done = False
        self.mask = False
        self.pointer = None
        self._bi = -1

    def beat(self, dur, fn, **data):
        self.beats.append(_Beat(fn, float(dur), self.duration, data))
        self.duration += float(dur)
        return self

    def pick_x(self, near=None, min_d=90, max_d=320):
        """A stage x some distance from `near` (default: the planned x)."""
        near = self.px if near is None else near
        S = self.S
        for _ in range(12):
            d = self.rng.uniform(min_d, max_d) * S * self.rng.choice((-1, 1))
            x = near + d
            if self.lo <= x <= self.hi:
                return x
        return _clamp(near, self.lo, self.hi)

    def walk(self, to, speed=95.0, **data):
        dist = abs(to - self.px)
        dur = max(0.5, dist / (speed * self.S) + 0.35)
        self.beat(dur, _b_walk, to=to, **data)
        self.px = to

    def ptr(self):
        if self.pointer is None:
            return None
        try:
            return self.pointer()
        except Exception:
            return None

    def update(self, dt):
        if self.done or not self.beats:
            self.done = True
            return
        self.t += dt
        while self._bi + 1 < len(self.beats) and self.t >= self.beats[self._bi + 1].start:
            if self._bi >= 0:
                b = self.beats[self._bi]
                b.t, b.p = b.dur, 1.0
                b.fn(self, b)
                b.first = False
            self._bi += 1
            nb = self.beats[self._bi]
            if nb.fn is not _b_walk and self.actor.feet_mode == "gait":
                self.actor.set_feet_mode("stand")
        b = self.beats[self._bi]
        b.t = self.t - b.start
        b.p = _clamp(b.t / b.dur, 0.0, 1.0) if b.dur > 0 else 1.0
        b.fn(self, b)
        b.first = False
        self.actor.clip_y = self.bot - 2 * self.S if self.mask else None
        self.actor.update(dt, self.fx)
        alive = []
        for pr in self.props:
            if pr.update(dt, self) is not False:
                alive.append(pr)
        self.props = alive
        self.fx.update(dt)
        if self.t >= self.duration:
            self.done = True

    def draw(self, cv):
        for pr in self.props:
            if getattr(pr, "layer", "front") == "back":
                pr.draw(cv, self)
        self.actor.draw(cv)
        for pr in self.props:
            if getattr(pr, "layer", "front") == "front":
                pr.draw(cv, self)
        if self.mask:
            _draw_button_mask(cv, self.button)
        for pr in self.props:
            if getattr(pr, "layer", "front") == "top":
                pr.draw(cv, self)
        self.fx.draw(cv)

    # helpers for beats
    def head(self):
        g = self.actor.geo or self.actor.geometry()
        return g["head"]

    def say(self, text, side=1, size=15, color=None):
        hx, hy = self.head()
        S = self.S
        self.fx.symbol("text", hx + side * 22 * S, hy - 24 * S, vy=-10, life=1.3, size=size,
                       color=color or self.pal["ink"], text=text)

    def hidden_hip(self, hc_off):
        """Hip y so the head centre sits hc_off*r below the button's top edge."""
        a = self.actor
        # Never lower than the button's bottom edge, or the legs would show
        # beneath it; anything that still pokes above is hidden by `visible`.
        return min(self.top + hc_off * a.r + a.gap + a.r + a.torso, self.bot - 4 * self.S)


def _look_viewer(sc):
    a = sc.actor
    p = sc.ptr()
    if p is not None:
        a.look_at(*p)
    else:
        a.face.look(0.0, 0.08)
        a.turn_tgt = 0.0


def _trap(p, k=0.22):
    vmax = 1.0 / (1.0 - k)
    if p < k:
        return vmax * p * p / (2 * k)
    if p > 1 - k:
        return 1.0 - vmax * (1 - p) ** 2 / (2 * k)
    return vmax * (p - k / 2)


# ---- common beats ---------------------------------------------------------

def _b_peek(sc, b):
    """Hands grip the top edge, then the head rises until the eyes clear it;
    looks one way, the other, then at you."""
    a, S, r = sc.actor, sc.S, sc.actor.r
    x = b.data.get("x", sc.px)
    hide = sc.hidden_hip(3.4)
    peek = sc.hidden_hip(-0.32)
    if b.first:
        sc.mask = True
        a.ground = sc.bot
        if not b.data.get("resume"):
            a.snap_hip(x, hide + 0.5 * r)
            a.set_feet_mode("free")
            a.tuck(0.0)
            for i in (0, 1):
                a.hand[i][0].snap(a.hand_tgt[i][0])
                a.hand[i][1].snap(a.hand_tgt[i][1])
        a.face.set(b.data.get("start", "curious"))
        b.st["look"] = sc.rng.choice((-1, 1))
    p = b.p
    k = b.data.get("speed", 1.0)
    a.visible = p >= 0.15
    if p < 0.16 / k and not b.data.get("resume"):
        a.hang()
    else:
        a.hand_world(0, x - 0.95 * r, sc.top - 3.2 * S)
        a.hand_world(1, x + 0.95 * r, sc.top - 3.2 * S)
    rise = _smooth(_seg(p, 0.18, 0.42 if k == 1.0 else 0.6))
    a.set_hip(x, hide + (peek - hide) * rise)
    L = b.st["look"]
    if b.data.get("shy"):
        a.face.look(0.3 * L, 0.35)
        a.tilt_tgt = 8 * L
        if p > 0.75:
            _look_viewer(sc)
            if "met" not in b.st:
                b.st["met"] = 1
                a.face.set(b.data.get("meet", "embarrassed"))
        return
    if p < 0.45:
        a.face.look(0, -0.4)
    elif p < 0.62:
        a.face.look(L * 0.95, 0)
        a.turn_tgt, a.tilt_tgt = L * 0.35, -L * 4
    elif p < 0.78:
        a.face.look(-L * 0.95, 0)
        a.turn_tgt, a.tilt_tgt = -L * 0.35, L * 4
    else:
        a.tilt_tgt = b.data.get("tilt_end", 5 * L)
        _look_viewer(sc)
        if "met" not in b.st:
            b.st["met"] = 1
            a.face.set(b.data.get("meet", "happy"))


def _b_duck(sc, b):
    """Startled: drop out of sight fast."""
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("surprise")
        hx, hy = sc.head()
        sc.fx.symbol("text", hx, hy - 30 * S, vy=-25, life=0.9, size=22, color=sc.pal["heart"], text="!")
        a.hip_f = 16
    a.set_hip(None, sc.hidden_hip(3.4))
    a.visible = b.p < 0.35
    if b.p > 0.95:
        a.hip_f = 7


def _b_climb(sc, b):
    """From a peek, hop up onto the top edge of the button."""
    a, S = sc.actor, sc.S
    if b.first:
        b.st["x"], b.st["y0"] = a.hip_tgt
        b.st["y1"] = a.stand_y(ground=sc.top)
        a.set_feet_mode("free")
        a.face.set(b.data.get("expr", "happy"))
        a.hip_f = 15
    p, x = b.p, b.st["x"]
    y0, y1 = b.st["y0"] + 6 * S, b.st["y1"]
    if p < 0.3:
        a.set_hip(x, b.st["y0"] + 6 * S * _smooth(p / 0.3))
        a.hand_world(0, x - 0.95 * a.r, sc.top - 3.2 * S)
        a.hand_world(1, x + 0.95 * a.r, sc.top - 3.2 * S)
        a.tuck(0.3)
    elif p < 0.72:
        q = _seg(p, 0.3, 0.72)
        a.set_hip(x, y0 + (y1 - y0) * _smooth(q) - math.sin(math.pi * q) * 18 * S)
        a.tuck(0.85 if q < 0.65 else 0.15)
        a.hands((-13 * S, -15 * S), (13 * S, -15 * S))
        a.face.look(0, -0.3)
    else:
        if "landed" not in b.st:
            b.st["landed"] = 1
            a.ground = sc.top
            a.hip_f = 9
            a.land(1.0, fx=sc.fx)
            sc.mask = False
        a.set_hip(x, a.stand_y(0.12 * (1 - _seg(p, 0.72, 1.0))))
        a.hang()
        _look_viewer(sc)


def _b_popup(sc, b):
    """Jack-in-the-box entrance: bursts up from behind with arms wide."""
    a, S = sc.actor, sc.S
    x = b.data.get("x", sc.px)
    if b.first:
        sc.mask = True
        a.ground = sc.bot
        a.snap_hip(x, sc.hidden_hip(3.4))
        a.set_feet_mode("free")
        a.tuck(0.2)
        a.face.set("mischief")
        a.hip_f = 18
        b.st["y1"] = a.stand_y(ground=sc.top)
    p = b.p
    a.visible = p >= 0.25
    if p < 0.25:
        a.set_hip(x, sc.hidden_hip(3.4))
    elif p < 0.7:
        q = _seg(p, 0.25, 0.7)
        a.set_hip(x, sc.hidden_hip(3.4) + (b.st["y1"] - sc.hidden_hip(3.4)) * _ease_out(q) - math.sin(math.pi * q) * 30 * S)
        a.tuck(0.9)
        a.hands((-20 * S, -14 * S), (20 * S, -14 * S))
        if q > 0.3 and "ta" not in b.st:
            b.st["ta"] = 1
            a.face.set("joy")
            hx, hy = sc.head()
            sc.fx.sparkle(hx, hy, n=10, speed=150)
    else:
        if "landed" not in b.st:
            b.st["landed"] = 1
            a.ground = sc.top
            a.hip_f = 9
            a.land(1.0, fx=sc.fx)
            sc.mask = False
        a.set_hip(x, a.stand_y(0.1 * (1 - _seg(p, 0.7, 1.0))))
        a.hands((-19 * S, -10 * S), (19 * S, -10 * S))
        _look_viewer(sc)


def _b_walk(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        b.st["x0"] = a.hip_tgt[0]
        b.st["d"] = 1 if b.data["to"] >= b.st["x0"] else -1
        a.set_feet_mode("gait")
        a.face.set(b.data.get("expr", "happy"))
        a.hip_f = 12
    x0, x1, d = b.st["x0"], b.data["to"], b.st["d"]
    stride = b.data.get("stride", 26.0) * S
    x = x0 + (x1 - x0) * _trap(b.p)
    ph = abs(x - x0) / stride
    offs = []
    for i, ft in enumerate(a.feet):
        u = (ph + (0.0 if i == 0 else 0.5)) % 1.0
        if u < 0.5:
            off, lift = stride / 4 - u * stride, 0.0
        else:
            s = (u - 0.5) / 0.5
            off, lift = -stride / 4 + _smooth(s) * stride / 2, math.sin(math.pi * s) * 5 * S
        ft["x"], ft["y"], ft["step"] = x + d * off, a.ground - lift, None
        offs.append(off)
    bob = 1.8 * S * math.cos(4 * math.pi * (ph - 0.25))
    hop = b.data.get("bounce", 0.0) * S * abs(math.sin(2 * math.pi * ph))
    a.set_hip(x, a.stand_y(0.04) - bob - hop)
    for i in (0, 1):
        side = -1 if i == 0 else 1
        a.hand_tgt[i] = [side * 5 * S - d * offs[i] * 0.7, 25 * S]
    a.turn_tgt, a.lean_tgt, a.tilt_tgt = d * 0.9, d * 3.0, 0.0
    a.face.look(d * 0.7, 0.05)


def _b_idle(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        if b.data.get("expr"):
            a.face.set(b.data["expr"])
        b.st["x"] = a.hip_tgt[0]
        a.turn_tgt = b.data.get("turn", 0.0)
        a.lean_tgt, a.tilt_tgt, a.hip_f = 0.0, b.data.get("tilt", 0.0), 7
        b.st["g"] = (0.0, 0.0)
        b.st["next"] = 0.0
    sway = math.sin(b.t * 1.3 + b.start) * 1.1 * S
    a.set_hip(b.st["x"] + sway, a.stand_y(0.02))
    a.hang(swing=sway * 0.3)
    look = b.data.get("look", "viewer")
    if look == "viewer":
        _look_viewer(sc)
    elif look == "around":
        if b.t >= b.st["next"]:
            b.st["next"] = b.t + sc.rng.uniform(0.5, 1.1)
            b.st["g"] = (sc.rng.uniform(-1, 1), sc.rng.uniform(-0.5, 0.3))
        a.face.look(*b.st["g"])
        a.turn_tgt = b.st["g"][0] * 0.4
    elif isinstance(look, tuple):
        a.look_at(*look)


def _b_wave(sc, b):
    a, S = sc.actor, sc.S
    i = b.data.get("hand", 1)
    side = -1 if i == 0 else 1
    if b.first:
        a.face.set(b.data.get("expr", "joy"))
        a.tilt_tgt = side * 7
        a.hand_fz[i] = (6.0, 0.4)
        if b.data.get("say"):
            sc.say(b.data["say"], side)
    w = math.sin(b.t * 2 * math.pi * 2.4)
    a.hand_tgt[i] = [side * (12 * S + 6 * S * w), -24 * S + 1.5 * S * abs(w)]
    a.hang(1 - i)
    a.set_hip(None, a.stand_y(0.02))
    _look_viewer(sc)
    if b.p >= 1.0:
        a.hand_fz[i] = (5.0, 0.55)
        a.tilt_tgt = 0.0


def _b_hop_down(sc, b):
    """Outro: hop back behind the button; sometimes pop up once more."""
    a, S = sc.actor, sc.S
    if b.first:
        b.st["x"] = a.hip_tgt[0]
        a.face.set(b.data.get("expr", "happy"))
        b.st["y0"] = a.stand_y(0.18)
    p, x = b.p, b.st["x"]
    hid = sc.hidden_hip(3.4)
    encore = b.data.get("encore")
    jump_end = 0.5 if encore else 0.8
    if p < 0.2:
        a.set_hip(x, b.st["y0"])
        a.hands((-12 * S, 16 * S), (12 * S, 16 * S))
        _look_viewer(sc)
    elif p < jump_end:
        if "go" not in b.st:
            b.st["go"] = 1
            a.set_feet_mode("free")
            a.hip_f = 18
            sc.mask = True
            a.ground = sc.bot
        q = _seg(p, 0.2, jump_end)
        y0 = b.st["y0"]
        a.set_hip(x, y0 + (hid - y0) * q * q - 24 * S * math.sin(math.pi * min(1.0, q * 1.25)) * (1 - q))
        a.tuck(0.6)
        a.hands((-14 * S, -18 * S), (14 * S, -18 * S))
    elif encore:
        q = _seg(p, jump_end, 1.0)
        up = _smooth(_seg(q, 0.15, 0.4)) * (1 - _smooth(_seg(q, 0.82, 1.0)))
        a.hip_f = 10
        a.set_hip(x, hid + (sc.hidden_hip(-0.3) - hid) * up)
        a.visible = q < 0.12 or up > 0.04
        a.hang()
        _look_viewer(sc)
        if q > 0.4 and "e" not in b.st:
            b.st["e"] = 1
            if encore == "wink":
                a.face.set("happy", wink=1.0, smirk=0.4)
            else:
                a.face.set("delight")
            hx, hy = sc.head()
            sc.fx.sparkle(hx + 10 * S, hy - 12 * S, n=5, speed=70)
    else:
        a.set_hip(x, hid)
        a.visible = p < jump_end + 0.06


# ---- little surprises that can drop into any idle --------------------------

def _b_sneeze(sc, b):
    a, S = sc.actor, sc.S
    p = b.p
    if b.first:
        a.face.set("yawn", lid=0.6, open=0.35, width=0.5)
        sc.say("ah…", 1, 12)
    if p < 0.55:
        q = _smooth(p / 0.55)
        a.lean_tgt, a.tilt_tgt = -5 * q, -10 * q
        a.face.nudge(open=0.3 + 0.4 * q, lid=0.5 + 0.35 * q, brow=0.5 * q, tilt=0.6)
        a.set_hip(None, a.stand_y(0.0) - 2 * S * q)
    elif p < 0.72:
        if "c" not in b.st:
            b.st["c"] = 1
            a.face.set("neutral", lid=1.0, lid_lo=1.0, open=0.35, width=0.45, brow=-0.3, tilt=-0.5)
            a.squash.v -= 2.5
            hx, hy = sc.head()
            d = 1 if a.turn.x >= 0 else -1
            for _ in range(5):
                sc.fx.add("drop", hx + d * 10 * S, hy + 6 * S, d * sc.rng.uniform(60, 140) * S,
                          sc.rng.uniform(-50, 10) * S, 0.5, 3, sc.pal["sweat"], g=300 * S)
            sc.say("CHOO!", d, 14)
        a.lean_tgt, a.tilt_tgt = 11, 14
        a.set_hip(None, a.stand_y(0.1))
    else:
        if "r" not in b.st:
            b.st["r"] = 1
            a.face.set("embarrassed")
        a.lean_tgt, a.tilt_tgt = 0, 4
        a.set_hip(None, a.stand_y(0.02))
        _look_viewer(sc)


def _b_hiccup(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("neutral", smile=0.1)
    for k, at in enumerate((0.1, 0.55)):
        key = "h%d" % k
        if b.p >= at and key not in b.st:
            b.st[key] = 1
            a.hy.v -= 140 * S
            a.squash.v += 2.0
            a.face.set("surprise", open=0.25)
            sc.say("hic!", sc.rng.choice((-1, 1)), 12)
        if at + 0.12 < b.p < at + 0.3:
            a.face.set("worried" if k == 0 else "embarrassed")
    a.set_hip(None, a.stand_y(0.02))
    a.hang()
    _look_viewer(sc)


def _b_wink(sc, b):
    a, S = sc.actor, sc.S
    _look_viewer(sc)
    if b.first:
        a.face.set("mischief", smile=0.8)
    if 0.25 < b.p < 0.75:
        a.face.nudge(wink=1.0)
        if "s" not in b.st:
            b.st["s"] = 1
            hx, hy = sc.head()
            sc.fx.sparkle(hx - 8 * S, hy - 6 * S, n=5, speed=60)
    else:
        a.face.nudge(wink=0.0)
    a.tilt_tgt = -6


def _b_scratch(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("thinking")
        a.arm_front[1] = True
        hx, hy = sc.head()
        sc.fx.symbol("text", hx + 18 * S, hy - 24 * S, vy=-8, life=1.3, size=16, color=sc.pal["ink"], text="?")
    a.hand_tgt[1] = [9 * S + 2 * S * math.sin(b.t * 30), -30 * S]
    a.hand_fz[1] = (9.0, 0.6)
    a.tilt_tgt = 9
    a.face.look(0.5, -0.6)
    if b.p >= 1.0:
        a.arm_front[1] = False
        a.hand_fz[1] = (5.0, 0.55)
        a.hang()


_SPICE = [_b_sneeze, _b_hiccup, _b_wink, _b_scratch]
_SPICE_DUR = {_b_sneeze: 2.0, _b_hiccup: 1.7, _b_wink: 1.1, _b_scratch: 1.6}


def _spice(sc, chance=0.35):
    if sc.rng.random() < chance:
        fn = sc.rng.choice(_SPICE)
        sc.beat(_SPICE_DUR[fn], fn)


def _intro(sc, meet="happy"):
    r = sc.rng.random()
    if r < 0.7:
        sc.beat(2.4, _b_peek, x=sc.px, meet=meet)
        sc.beat(0.9, _b_climb)
    else:
        sc.beat(1.1, _b_popup, x=sc.px)


def _outro(sc):
    if sc.rng.random() < 0.45:
        sc.beat(1.7, _b_wave, hand=sc.rng.choice((0, 1)),
                say=sc.rng.choice(("bye!", "bye!", "see ya!", "toodles!", "")))
    enc = sc.rng.choice((None, None, "wink", "delight"))
    sc.beat(2.0 if enc else 1.1, _b_hop_down, encore=enc)


# ---------------------------------------------------------------------------
# The scenes
# ---------------------------------------------------------------------------


def _b_kiss(sc, b):
    a, S = sc.actor, sc.S
    i = b.data.get("hand", 1)
    side = -1 if i == 0 else 1
    if b.first:
        a.face.set("smitten" if sc.rng.random() < 0.5 else "happy", smile=0.2, open=0.15, width=0.3)
        a.arm_front[i] = True
    p = b.p
    if p < 0.45:
        a.hand_tgt[i] = [side * 2 * S, -12 * S]
        a.tilt_tgt = side * 5
    else:
        if "throw" not in b.st:
            b.st["throw"] = 1
            a.face.set("delight")
            hx, hy = sc.head()
            for k in range(3):
                sc.fx.add("heart", hx + side * 14 * S, hy + 4 * S, side * (40 + 25 * k) * S, -(35 + 15 * k) * S,
                          1.5, 7 - k, None, drag=0.8)
        a.hand_tgt[i] = [side * 22 * S, -14 * S]
        a.tilt_tgt = -side * 4
    a.hang(1 - i)
    _look_viewer(sc)
    if p >= 1.0:
        a.arm_front[i] = False


def _b_jump_joy(sc, b):
    """A few bouncy hops of joy, arms flung up."""
    a, S = sc.actor, sc.S
    n = b.data.get("n", 2)
    if b.first:
        b.st["x"] = a.hip_tgt[0]
        a.face.set(b.data.get("expr", "starry" if sc.rng.random() < 0.4 else "joy"))
        a.hip_f = 16
    q = (b.p * n) % 1.0
    k = int(b.p * n)
    base = a.stand_y(0.02)
    if q < 0.22:
        a.set_feet_mode("stand")
        a.set_hip(b.st["x"], a.stand_y(0.16))
        a.hands((-9 * S, 14 * S), (9 * S, 14 * S))
    else:
        u = _seg(q, 0.22, 1.0)
        a.set_feet_mode("free")
        a.tuck(0.55 * math.sin(math.pi * u))
        a.set_hip(b.st["x"], base - math.sin(math.pi * u) * 26 * S)
        a.hands((-18 * S, -20 * S), (18 * S, -20 * S))
        key = "l%d" % k
        if u > 0.9 and key not in b.st:
            b.st[key] = 1
            a.land(0.8, fx=sc.fx)
            if b.data.get("confetti") and k == 0:
                hx, hy = sc.head()
                sc.fx.confetti(hx, hy - 10 * S, n=30)
    _look_viewer(sc)
    if b.p >= 1.0:
        a.set_feet_mode("stand")
        a.hip_f = 7


def _scene_hello(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    sc.beat(2.4, _b_peek, x=sc.px, meet="surprise")
    sc.beat(0.9, _b_duck)
    sc.beat(2.2, _b_peek, x=sc.px, shy=True, resume=True, meet="embarrassed", speed=0.7)
    sc.beat(0.9, _b_climb, expr="happy")
    sc.beat(0.9, _b_idle, expr="embarrassed")
    sc.beat(1.9, _b_wave, hand=rng.choice((0, 1)), say=rng.choice(("hi!", "hello!", "hiya!", "oh, hi!")))
    fin = rng.choice(("kiss", "wink", "joy"))
    if fin == "kiss":
        sc.beat(1.6, _b_kiss, hand=rng.choice((0, 1)))
    elif fin == "wink":
        sc.beat(1.1, _b_wink)
    else:
        sc.beat(1.5, _b_jump_joy, n=2, confetti=True)
    sc.beat(0.6, _b_idle, expr="happy")
    _outro(sc)
    return sc


# ---- dance ------------------------------------------------------------------

def _notes(sc, b, every=0.45):
    k = int(b.t / every)
    key = "n%d" % k
    if key not in b.st:
        b.st[key] = 1
        hx, hy = sc.head()
        col = sc.rng.choice((sc.pal["accent"], "#a855f7", "#f43f5e", "#10b981"))
        sc.fx.symbol("note", hx + sc.rng.uniform(-22, 22) * sc.S, hy - 20 * sc.S,
                     vx=sc.rng.uniform(-15, 15), vy=-38, life=1.5, size=sc.rng.choice((7, 8, 9)), color=col)


def _b_dance(sc, b):
    a, S = sc.actor, sc.S
    mv = b.data["move"]
    t = b.t
    if b.first:
        b.st["x"] = a.hip_tgt[0]
        a.set_feet_mode("stand")
        a.face.set(b.data.get("expr", "delight"))
        a.face.talk = 0.0
        a.hip_f = 9
        a.arm_front = [False, False]
        a.hand_fz = [(6.0, 0.45), (6.0, 0.45)]
    x = b.st["x"]
    bpm = b.data.get("bpm", 2.2)
    beat = math.sin(t * 2 * math.pi * bpm / 2)
    bounce = abs(math.sin(t * math.pi * bpm)) * 2.2 * S
    a.turn_tgt, a.lean_tgt, a.tilt_tgt = 0.0, 0.0, 0.0
    y = a.stand_y(0.06) + bounce
    if mv == "floss":
        a.set_hip(x + 4 * S * beat, y)
        hx = -15 * S * beat
        a.hands((hx - 3 * S, 18 * S), (hx + 3 * S, 18 * S))
        a.arm_front = [beat > 0, beat < 0]
        a.tilt_tgt = 6 * beat
    elif mv == "clap":
        a.set_hip(x + 3 * S * beat, y)
        q = (t * bpm) % 1.0
        sp = 3 * S if q < 0.25 else 13 * S
        a.hands((-sp, -6 * S), (sp, -6 * S))
        key = "c%d" % int(t * bpm)
        if q < 0.25 and key not in b.st:
            b.st[key] = 1
            g = a.geo or a.geometry()
            sc.fx.sparkle((g["hand0"][0] + g["hand1"][0]) / 2, g["hand0"][1], n=4, speed=60)
        a.tilt_tgt = 5 * beat
    elif mv == "disco":
        up = math.sin(t * 2 * math.pi * bpm / 2) > 0
        i = 1 if b.data.get("hand", 1) else 0
        side = -1 if i == 0 else 1
        a.hand_tgt[i] = [side * 16 * S, -24 * S] if up else [-side * 6 * S, 22 * S]
        a.hand_tgt[1 - i] = [-side * 6 * S, 8 * S]
        a.arm_front[i] = not up
        a.set_hip(x + (side * 3 * S if up else -side * 3 * S), y)
        a.lean_tgt = side * 4 if up else -side * 3
        a.face.look(side * 0.8 if up else 0.0, -0.6 if up else 0.2)
    elif mv == "shimmy":
        sh = math.sin(t * 2 * math.pi * 6.0)
        a.set_hip(x, a.stand_y(0.14) + bounce * 0.5)
        a.lean_tgt = 4 * sh
        a.hands((-15 * S, 3 * S + 2 * S * sh), (15 * S, 3 * S - 2 * S * sh))
        a.tilt_tgt = -3 * sh
    elif mv == "spin":
        q = _seg(t, 0.2, b.dur - 0.3)
        a.turn_tgt = math.sin(q * 2 * math.pi * 2) * (1 if q < 1 else 0)
        a.set_hip(x, a.stand_y(0.02) - math.sin(math.pi * q) * 6 * S)
        a.hands((-12 * S, -24 * S), (12 * S, -24 * S))
        if 0.1 < q < 0.9 and int(t * 6) % 2 == 0 and ("s%d" % int(t * 6)) not in b.st:
            b.st["s%d" % int(t * 6)] = 1
            hx, hy = sc.head()
            sc.fx.sparkle(hx, hy - 20 * S, n=3, speed=90)
    elif mv == "sprinkler":
        i = b.data.get("hand", 1)
        side = -1 if i == 0 else 1
        a.arm_front[i] = True
        a.hand_tgt[1 - i] = [side * 3 * S, -31 * S]
        step = int(t * bpm * 2) % 8
        ang = -40 + (step if step < 5 else 8 - step) * 18
        dx, dy = _rot(side * 27 * S, 0, -side * ang * 0.0 + ang * side)
        a.hand_tgt[i] = [dx, dy * 0.6 - 4 * S]
        a.set_hip(x, y)
        a.tilt_tgt = -side * 5
    a.face.talk = 0.35 if b.data.get("sing") else 0.0
    a.face.talk_hz = bpm
    _notes(sc, b)
    if b.p >= 1.0:
        a.face.talk = 0.0
        a.arm_front = [False, False]
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _b_ta_da(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set(b.data.get("expr", "proud" if sc.rng.random() < 0.5 else "joy"))
        hx, hy = sc.head()
        sc.fx.sparkle(hx, hy - 6 * S, n=12, speed=150)
        a.squash.v += 1.5
    a.hands((-20 * S, -18 * S), (20 * S, -18 * S))
    a.set_hip(None, a.stand_y(0.03))
    a.tilt_tgt = b.data.get("tilt", 6)
    _look_viewer(sc)


def _scene_dance(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc, meet="mischief")
    sc.walk(sc.pick_x(), speed=110, bounce=2.0, expr="happy")
    if rng.random() < 0.5:
        sc.beat(1.6, _b_idle, expr="mischief", look="around")
        sc.beat(0.5, _b_wink)
    moves = rng.sample(["floss", "clap", "disco", "shimmy", "spin", "sprinkler"], 3 + (rng.random() < 0.4))
    bpm = rng.choice((2.0, 2.2, 2.4))
    for mv in moves:
        sc.beat(rng.uniform(1.9, 2.5), _b_dance, move=mv, bpm=bpm, hand=rng.choice((0, 1)),
                sing=rng.random() < 0.5, expr=rng.choice(("delight", "joy", "calm", "happy")))
    sc.beat(1.3, _b_ta_da)
    _spice(sc, 0.25)
    _outro(sc)
    return sc


# ---- juggling ---------------------------------------------------------------

class _Balls:
    layer = "front"

    def __init__(self, n, colors):
        self.n, self.colors = n, colors
        self.mode = "hold"        # hold | juggle | fall
        self.rate = 1.0
        self.t0 = 0.0
        self.fall = []            # [x, y, vx, vy] per ball when falling
        self.high = -1            # index of a ball tossed extra high
        self.hide = set()

    def update(self, dt, sc):
        if self.mode == "fall":
            for f in self.fall:
                f[3] += 900 * sc.S * dt
                f[0] += f[2] * dt
                f[1] += f[3] * dt
                if f[1] > sc.top - 4 * sc.S:
                    f[1] = sc.top - 4 * sc.S
                    f[3] = -abs(f[3]) * 0.45
                    f[2] *= 0.7
        return True

    def draw(self, cv, sc):
        a, S = sc.actor, sc.S
        g = a.geo or a.geometry()
        L, R = g["hand0"], g["hand1"]
        rr = 3.6 * S
        pts = []
        if self.mode == "hold":
            pts = [(L[0], L[1] - rr), (R[0], R[1] - rr), (R[0] + 2 * S, R[1] - 2.2 * rr)][:self.n]
        elif self.mode == "juggle":
            T = 0.42 / self.rate
            per = self.n * T
            for k in range(self.n):
                u = ((sc.t - self.t0) + k * T * 1.0) % per
                throw = int(u / T)
                q = (u % T) / T
                fr, to = (L, R) if (throw + k) % 2 == 0 else (R, L)
                H = 60 * S * (1.8 if k == self.high else 1.0)
                x = fr[0] + (to[0] - fr[0]) * q
                y = fr[1] + (to[1] - fr[1]) * q - H * 4 * q * (1 - q) - rr
                pts.append((x, y))
        else:
            pts = [(f[0], f[1]) for f in self.fall]
        self.last = pts
        for k, (x, y) in enumerate(pts):
            if k in self.hide:
                continue
            col = self.colors[k % len(self.colors)]
            cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=col, outline=_mix(col, "#000000", 0.35))
            cv.create_oval(x - rr * 0.55, y - rr * 0.6, x - rr * 0.05, y - rr * 0.15, fill=_mix(col, "#ffffff", 0.55), outline="")


def _b_juggle(sc, b):
    a, S = sc.actor, sc.S
    balls = b.data["balls"]
    if b.first:
        if balls not in sc.props:
            sc.props.append(balls)
        balls.mode = "juggle"
        balls.t0 = sc.t
        balls.rate = b.data.get("rate", 1.0)
        a.face.set(b.data.get("expr", "determined"))
        a.hand_fz = [(7.0, 0.7), (7.0, 0.7)]
        b.st["x"] = a.hip_tgt[0]
    w = sc.t * 2 * math.pi / (0.84 / balls.rate)
    a.hands((-9 * S + 3 * S * math.cos(w), 8 * S + 3 * S * math.sin(w)),
            (9 * S - 3 * S * math.cos(w + math.pi), 8 * S + 3 * S * math.sin(w + math.pi)))
    a.set_hip(b.st["x"], a.stand_y(0.05))
    a.face.look(0, -0.7)
    a.tilt_tgt = 2 * math.sin(sc.t * 3)
    if b.data.get("sweat") and int(b.t * 2) != b.st.get("sw"):
        b.st["sw"] = int(b.t * 2)
        hx, hy = sc.head()
        sc.fx.add("drop", hx + sc.rng.choice((-1, 1)) * 13 * S, hy - 8 * S, sc.rng.uniform(-20, 20) * S, -30 * S,
                  0.7, 3.2, sc.pal["sweat"], g=260 * S)


def _b_bonk(sc, b):
    a, S = sc.actor, sc.S
    balls = b.data["balls"]
    if b.first:
        balls.mode = "fall"
        pts = getattr(balls, "last", [])
        balls.fall = [[x, y, sc.rng.uniform(-90, 90) * S, -sc.rng.uniform(80, 200) * S] for x, y in pts]
        hx, hy = sc.head()
        if balls.fall:
            balls.fall[0] = [hx, hy - a.r - 4 * S, 60 * S, -260 * S]
        sc.fx.symbol("star", hx, hy - a.r, vx=0, vy=-10, life=0.6, size=9)
        sc.fx.symbol("text", hx + 16 * S, hy - 28 * S, vy=-20, life=0.9, size=14, color=sc.pal["ink"], text="bonk!")
        a.face.set("dizzy")
        a.squash.v -= 2.5
        a.hang()
    for k in range(3):
        ang = sc.t * 5 + k * 2.09
        hx, hy = sc.head()
        if int(sc.t * 30) % 3 == k:
            sc.fx.add("star", hx + math.cos(ang) * 18 * S, hy - a.r - 4 * S + math.sin(ang) * 5 * S, life=0.12, size=4,
                      color=sc.pal["gold"])
    wob = math.sin(b.t * 7) * (1 - b.p)
    a.lean_tgt, a.tilt_tgt = 6 * wob, 14 * wob
    a.set_hip(None, a.stand_y(0.06))
    if b.p > 0.75 and "e" not in b.st:
        b.st["e"] = 1
        a.face.set("laugh")
        a.face.talk = 0.4
        a.face.talk_hz = 6
    if b.p >= 1.0:
        a.face.talk = 0.0
        balls.hide = set(range(balls.n))


def _b_catch(sc, b):
    a, S = sc.actor, sc.S
    balls = b.data["balls"]
    if b.first:
        balls.mode = "hold"
        a.face.set("proud")
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]
    p = b.p
    if p < 0.35:
        a.hands((-12 * S, 6 * S), (12 * S, 6 * S))
    else:
        q = _smooth(_seg(p, 0.35, 0.7)) * (1 - _smooth(_seg(p, 0.8, 1.0)))
        a.lean_tgt = 28 * q * (1 if sc.rng.random() < 2 else -1)
        a.tilt_tgt = 10 * q
        a.hands((-10 * S, 4 * S), (14 * S, 12 * S))
        if p > 0.5 and "s" not in b.st:
            b.st["s"] = 1
            hx, hy = sc.head()
            sc.fx.sparkle(hx, hy - 10 * S, n=8, speed=110)
    a.set_hip(None, a.stand_y(0.03))
    if p > 0.8:
        _look_viewer(sc)
    if p >= 1.0:
        balls.hide = set(range(balls.n))


def _b_extra_ball(sc, b):
    a, S = sc.actor, sc.S
    balls = b.data["balls"]
    if b.first:
        balls.n = 4
        a.face.set("worried")
        hx, hy = sc.head()
        sc.fx.symbol("text", hx + 18 * S, hy - 30 * S, vy=-15, life=1.0, size=18, color=sc.pal["heart"], text="!?")
    _b_juggle(sc, b)
    a.face.set("worried") if b.p < 0.5 else None


def _scene_juggle(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    sc.walk(sc.pick_x(), expr="happy")
    pal = rng.choice((["#f43f5e", "#f59e0b", "#10b981", "#3b82f6"], ["#a855f7", "#06b6d4", "#facc15", "#f43f5e"],
                      ["#ef4444", "#ffffff", "#3b82f6", "#22c55e"]))
    balls = _Balls(3, pal)
    sc.beat(0.8, _b_idle, expr="mischief")
    sc.beat(rng.uniform(2.6, 3.4), _b_juggle, balls=balls, expr=rng.choice(("determined", "happy")))
    end = rng.choice(("perfect", "bonk", "extra"))
    if end == "bonk":
        sc.beat(1.0, _b_juggle, balls=balls, expr="worried")
        sc.beat(2.4, _b_bonk, balls=balls)
        sc.beat(0.9, _b_idle, expr="embarrassed")
    elif end == "extra":
        sc.beat(2.2, _b_extra_ball, balls=balls, rate=1.25, expr="worried", sweat=True)
        sc.beat(1.0, _b_juggle, balls=balls, rate=1.25, expr="joy")
        sc.beat(1.6, _b_catch, balls=balls)
    else:
        sc.beat(1.4, _b_juggle, balls=balls, expr="joy")
        sc.beat(1.6, _b_catch, balls=balls)
    _outro(sc)
    return sc


# ---- acrobatics -------------------------------------------------------------

def _b_backflip(sc, b):
    a, S = sc.actor, sc.S
    d = b.data.get("d", 1)
    if b.first:
        b.st["x"] = a.hip_tgt[0]
        a.face.set("determined")
        a.turn_tgt = d * 0.6
    p, x = b.p, b.st["x"]
    if p < 0.3:
        q = _smooth(p / 0.3)
        a.set_hip(x, a.stand_y(0.3 * q))
        a.hands((-6 * S - d * 10 * S * q, 18 * S), (6 * S - d * 10 * S * q, 18 * S))
        a.lean_tgt = d * 8 * q
    elif p < 0.78:
        q = _seg(p, 0.3, 0.78)
        if "go" not in b.st:
            b.st["go"] = 1
            a.set_feet_mode("free")
            a.hip_f = 22
            a.face.set("joy")
        a.spin_snap = True
        a.spin_tgt = -d * 360 * _smoother(q)
        a.set_hip(x - d * 12 * S * q, a.stand_y(0.0) - math.sin(math.pi * q) * 48 * S)
        a.tuck(math.sin(math.pi * min(1, q * 1.15)))
        a.hands((-10 * S, 10 * S), (10 * S, 10 * S))
        a.lean_tgt = 0
    else:
        if "land" not in b.st:
            b.st["land"] = 1
            a.spin_snap = True
            a.spin_tgt = 0.0
            a.spin.snap(0.0)
            a.hip_f = 9
            a.land(1.2, fx=sc.fx)
            b.st["xl"] = a.hip_tgt[0]
        a.spin_snap = False
        a.set_hip(b.st["xl"], a.stand_y(0.2 * (1 - _seg(p, 0.78, 1.0))))
        a.hang()


def _b_cartwheel(sc, b):
    a, S = sc.actor, sc.S
    d = b.data.get("d", 1)
    if b.first:
        b.st["x"] = a.hip_tgt[0]
        a.face.set("joy")
        a.turn_tgt = 0.0
        a.set_feet_mode("free")
        a.hip_f = 22
    q = b.p
    ang = 360 * _smooth(q) * d
    a.spin_snap = True
    a.spin_tgt = ang
    span = abs(math.sin(math.radians(ang) / 2))
    a.set_hip(b.st["x"] + d * 70 * S * _smooth(q), a.stand_y(0.0) - span * 20 * S)
    a.hands((-7 * S, -27 * S), (7 * S, -27 * S))
    sp = 15 * S * math.sin(math.pi * q)
    a.foot_rel_tgt = [[-5 * S - sp, a.leg * 0.92], [5 * S + sp, a.leg * 0.92]]
    if q >= 1.0:
        a.spin.snap(0.0)
        a.spin_tgt = 0.0
        a.spin_snap = False
        a.hip_f = 9
        a.land(0.7, fx=sc.fx)
        a.hang()


def _b_wobble(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("worried")
        hx, hy = sc.head()
        sc.fx.add("drop", hx + 13 * S, hy - 8 * S, 20 * S, -30 * S, 0.8, 3.4, sc.pal["sweat"], g=260 * S)
    k = 1 - b.p
    w = math.sin(b.t * 9)
    a.lean_tgt = 14 * w * k
    a.tilt_tgt = -10 * w * k
    ang = b.t * 16
    a.hands((-14 * S + 9 * S * math.cos(ang), -6 * S + 9 * S * math.sin(ang)),
            (14 * S + 9 * S * math.cos(ang + 1), -6 * S + 9 * S * math.sin(ang + 1)))
    a.set_hip(None, a.stand_y(0.05))
    if b.p > 0.7 and "r" not in b.st:
        b.st["r"] = 1
        a.face.set("laugh")


def _scene_acro(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    sc.walk(sc.pick_x(), expr="happy")
    sc.beat(0.8, _b_idle, expr="determined")
    if rng.random() < 0.5:
        d = 1 if sc.px < (sc.lo + sc.hi) / 2 else -1
        n = rng.choice((1, 2, 2, 3))
        for _ in range(n):
            sc.beat(0.85, _b_cartwheel, d=d)
            sc.px += d * 70 * sc.S
    else:
        sc.beat(1.5, _b_backflip, d=rng.choice((-1, 1)))
    if rng.random() < 0.45:
        sc.beat(1.8, _b_wobble)
    else:
        sc.beat(1.4, _b_ta_da)
    _spice(sc, 0.3)
    _outro(sc)
    return sc


# ---- power nap --------------------------------------------------------------

class _Bubble:
    layer = "front"

    def __init__(self):
        self.on = False
        self.pop = None

    def update(self, dt, sc):
        return True

    def draw(self, cv, sc):
        if not self.on:
            return
        a, S = sc.actor, sc.S
        g = a.geo or a.geometry()
        hx, hy = g["head"]
        d = 1 if a.turn.x >= 0 else -1
        k = 0.5 + 0.5 * math.sin(sc.t * 2 * math.pi * 0.45)
        rr = (2 + 7 * k) * S
        x, y = hx + d * (6 * S + rr * 0.8), hy + 5 * S
        cv.create_oval(x - rr, y - rr, x + rr, y + rr, fill=_mix(sc.pal["head"], "#93c5fd", 0.35),
                       outline=_mix(sc.pal["ink"], sc.pal["page"], 0.4), width=1)
        cv.create_oval(x - rr * 0.5, y - rr * 0.55, x - rr * 0.15, y - rr * 0.2, fill="#ffffff", outline="")
        self.xy = (x, y)


def _b_yawn(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("yawn")
        a.hand_fz = [(3.0, 0.7), (3.0, 0.7)]
    q = math.sin(math.pi * b.p)
    a.hands((-14 * S, -26 * S * q + 20 * S * (1 - q)), (14 * S, -26 * S * q + 20 * S * (1 - q)))
    a.lean_tgt = -4 * q
    a.tilt_tgt = -8 * q
    a.set_hip(None, a.stand_y(0.0) - 2 * S * q)
    if b.p > 0.8 and "s" not in b.st:
        b.st["s"] = 1
        a.face.set("sleepy")
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _b_sit(sc, b):
    a, S = sc.actor, sc.S
    d = b.data.get("d", 1)
    if b.first:
        a.set_feet_mode("free")
        a.hip_f = 4
        a.face.set("sleepy")
        a.turn_tgt = d * 0.5
        b.st["x"] = a.hip_tgt[0]
    q = _smooth(b.p)
    a.set_hip(b.st["x"], a.stand_y(0.0) + (a.leg * 0.93 - 3 * S) * q)
    a.foot_rel_tgt = [[d * (8 + 14 * q) * S, (a.leg * 0.95) * (1 - q) + 2 * S * q],
                      [d * (14 + 14 * q) * S, (a.leg * 0.95) * (1 - q) + 2 * S * q]]
    a.hands((-d * 10 * S, 24 * S * (1 - q) + 22 * S * q), (d * 12 * S, 22 * S))
    a.lean_tgt = -d * 8 * q


def _b_snooze(sc, b):
    a, S = sc.actor, sc.S
    bub = b.data["bubble"]
    d = b.data.get("d", 1)
    if b.first:
        a.face.set("asleep")
        a.face.saccades = False
        bub.on = True
        if bub not in sc.props:
            sc.props.append(bub)
    br = math.sin(sc.t * 2 * math.pi * 0.45)
    a.tilt_tgt = d * 22 + 3 * br
    a.lean_tgt = -d * 6 + 1.5 * br
    a.face.nudge(open=0.06 + 0.1 * (br + 1) / 2)
    k = int(b.t / 0.9)
    if ("z%d" % k) not in b.st:
        b.st["z%d" % k] = 1
        hx, hy = sc.head()
        sc.fx.symbol("text", hx + d * 12 * S, hy - 16 * S, vx=d * 14, vy=-26, life=1.8,
                     size=sc.rng.choice((11, 13, 15)), color=_mix(sc.pal["ink"], sc.pal["page"], 0.25), text="z")


def _b_wake(sc, b):
    a, S = sc.actor, sc.S
    bub = b.data["bubble"]
    startled = b.data.get("startled", True)
    if b.first:
        a.face.saccades = True
        if startled:
            bub.on = False
            xy = getattr(bub, "xy", sc.head())
            sc.fx.add("ring", xy[0], xy[1], life=0.35, size=5, color="#93c5fd")
            sc.fx.sparkle(xy[0], xy[1], n=6, speed=90, colors=["#93c5fd", "#ffffff"])
            a.face.set("surprise")
            a.squash.v += 3
            hx, hy = sc.head()
            sc.fx.symbol("text", hx, hy - 30 * S, vy=-25, life=0.9, size=22, color=sc.pal["heart"], text="!")
        else:
            a.face.set("sleepy")
    a.tilt_tgt = 0
    a.lean_tgt = 0
    if not startled:
        if b.p > 0.4 and bub.on:
            bub.on = False
            hx, hy = sc.head()
            sc.fx.add("ring", hx + 12 * S, hy + 5 * S, life=0.3, size=4, color="#93c5fd")
        a.face.nudge(lid=0.8 - 0.6 * b.p)
    if b.p > 0.6 and "e" not in b.st:
        b.st["e"] = 1
        a.face.set("embarrassed")
        _look_viewer(sc)


def _b_stand_up(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.hip_f = 6
        b.st["x"] = a.hip_tgt[0]
    q = _smooth(b.p)
    a.set_hip(b.st["x"], a.stand_y(0.0) + (a.leg * 0.93 - 3 * S) * (1 - q))
    a.foot_rel_tgt = [[-5 * S * q + 10 * S * (1 - q), a.leg * 0.95 * q + 2 * S * (1 - q)],
                      [5 * S * q + 16 * S * (1 - q), a.leg * 0.95 * q + 2 * S * (1 - q)]]
    a.hang()
    if b.p >= 1.0:
        a.set_feet_mode("stand")
        a.hip_f = 7
    _look_viewer(sc)


def _scene_nap(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc, meet="sleepy")
    sc.walk(sc.pick_x(), speed=70, expr="sleepy")
    sc.beat(1.8, _b_yawn)
    d = rng.choice((-1, 1))
    bub = _Bubble()
    sc.beat(1.3, _b_sit, d=d)
    sc.beat(rng.uniform(3.0, 4.0), _b_snooze, bubble=bub, d=d)
    sc.beat(1.4, _b_wake, bubble=bub, startled=rng.random() < 0.6)
    sc.beat(1.0, _b_stand_up)
    sc.beat(0.7, _b_idle, expr="embarrassed")
    _outro(sc)
    return sc


# ---- butterfly --------------------------------------------------------------

class _Butterfly:
    layer = "front"

    def __init__(self, sc, color):
        self.color = color
        self.x, self.y = _Spring(sc.x0 - 30, 0.9, 0.6), _Spring(sc.top - 140 * sc.S, 0.9, 0.6)
        self.tx, self.ty = self.x.x, self.y.x
        self.mode = "fly"      # fly | nose | gone
        self.flap = 9.0

    def update(self, dt, sc):
        if self.mode != "nose":
            S = sc.S
            wob = (math.sin(sc.t * 3.1) * 14 * S, math.sin(sc.t * 4.3 + 1) * 10 * S)
            self.x.step(self.tx + wob[0], dt)
            self.y.step(self.ty + wob[1], dt)
        return True

    def pos(self, sc):
        if self.mode == "nose":
            a = sc.actor
            g = a.geo or a.geometry()
            hx, hy = g["head"]
            ox, oy = _rot(g["turn"] * 0.33 * a.r, 0.18 * a.r, g["head_dir"] + 90)
            return hx + ox, hy + oy - 3 * sc.S
        return self.x.x, self.y.x

    def draw(self, cv, sc):
        if self.mode == "gone":
            return
        S = sc.S
        x, y = self.pos(sc)
        hz = 2.0 if self.mode == "nose" else self.flap
        f = 0.25 + 0.75 * abs(math.cos(sc.t * 2 * math.pi * hz))
        edge = _mix(self.color, "#000000", 0.4)
        for side in (-1, 1):
            for (ox, oy, rx, ry) in ((7.0, -4.0, 7.0, 5.5), (4.5, 3.5, 4.5, 3.8)):
                cx = x + side * ox * S * f
                pts = []
                for i in range(14):
                    th = i / 14 * 2 * math.pi
                    pts += [cx + side * rx * S * f * math.cos(th) * 0.9, y + oy * S + ry * S * math.sin(th)]
                cv.create_polygon(pts, fill=self.color, outline=edge, smooth=True, width=1)
            cv.create_oval(x + side * 6 * S * f - 1.4 * S, y - 5 * S, x + side * 6 * S * f + 1.4 * S, y - 2.4 * S,
                           fill="#ffffff", outline="")
        ink = sc.pal["ink"]
        cv.create_line(x, y - 4 * S, x, y + 5 * S, fill=ink, width=max(1.5, 2 * S), capstyle="round")
        for side in (-1, 1):
            cv.create_line(x, y - 4 * S, x + side * 3 * S, y - 9 * S, x + side * 5 * S, y - 9.5 * S,
                           fill=ink, width=1, smooth=True)


def _b_bfly_arrive(sc, b):
    a, S = sc.actor, sc.S
    bf = b.data["bf"]
    if b.first:
        if bf not in sc.props:
            sc.props.append(bf)
        side = 1 if a.hip_tgt[0] < sc.w / 2 else -1
        bf.x.snap(a.hip_tgt[0] + side * 260 * S)
        bf.y.snap(sc.top - 150 * S)
        a.face.set("curious")
    hx, hy = sc.head()
    bf.tx, bf.ty = a.hip_tgt[0] + 40 * S * math.cos(b.t * 1.3), hy - 40 * S
    a.look_at(*bf.pos(sc))
    a.tilt_tgt = 8 * math.sin(b.t * 1.5)
    if b.p > 0.55 and "d" not in b.st:
        b.st["d"] = 1
        a.face.set("delight")


def _b_bfly_chase(sc, b):
    a, S = sc.actor, sc.S
    bf = b.data["bf"]
    if b.first:
        a.face.set("happy", eye=1.1)
        b.st["x"] = a.hip_tgt[0]
    n = 2
    q = (b.p * n) % 1.0
    hx, hy = sc.head()
    bf.tx = b.st["x"] + 50 * S * math.sin(b.t * 2.2)
    bf.ty = hy - 55 * S
    bx, by = bf.pos(sc)
    a.look_at(bx, by)
    if 0.3 < q < 0.9:
        a.set_feet_mode("free")
        a.tuck(0.3)
        u = _seg(q, 0.3, 0.9)
        a.hip_f = 14
        a.set_hip(b.st["x"] + (bx - b.st["x"]) * 0.2, a.stand_y() - math.sin(math.pi * u) * 22 * S)
        a.hand_world(1, bx, by + 6 * S)
        a.hand_world(0, bx - 8 * S, by + 10 * S)
        a.face.set("joy")
    else:
        if a.feet_mode == "free":
            a.land(0.7, fx=sc.fx)
            a.face.set("laugh")
        a.set_hip(b.st["x"], a.stand_y(0.1))
        a.hang()


def _b_bfly_land(sc, b):
    a, S = sc.actor, sc.S
    bf = b.data["bf"]
    if b.first:
        a.set_feet_mode("stand")
        a.hip_f = 7
        hx, hy = sc.head()
        bf.tx, bf.ty = hx, hy + 2 * S
        a.face.set("surprise", eye=1.15)
        a.turn_tgt = 0.0
        a.tilt_tgt = 0.0
    a.hang()
    a.set_hip(None, a.stand_y(0.02))
    if b.p < 0.3:
        a.face.look(0, -0.2)
        hx, hy = sc.head()
        bf.tx, bf.ty = hx, hy + 2 * S
    else:
        if bf.mode != "nose":
            bf.mode = "nose"
        a.face.look(0, 0.35)
        a.face.nudge(cross=1.0)
        if b.p > 0.55 and "d" not in b.st:
            b.st["d"] = 1
            a.face.set(b.data.get("then", "smitten"), cross=1.0)


def _b_bfly_leave(sc, b):
    a, S = sc.actor, sc.S
    bf = b.data["bf"]
    sneeze = b.data.get("sneeze")
    if b.first:
        a.face.nudge(cross=0.0)
        if not sneeze:
            x, y = bf.pos(sc)
            bf.mode = "fly"
            bf.x.snap(x)
            bf.y.snap(y)
            a.face.set("happy")
        b.st["dir"] = sc.rng.choice((-1, 1))
    if sneeze and b.p < 0.55:
        _b_sneeze(sc, b) if False else None
        q = _smooth(b.p / 0.55)
        a.face.set("yawn", lid=0.5 + 0.4 * q, open=0.3 + 0.4 * q, cross=1.0) if "ah" not in b.st else None
        if "ah" not in b.st:
            b.st["ah"] = 1
            sc.say("ah…", 1, 12)
        a.lean_tgt, a.tilt_tgt = -5 * q, -10 * q
        return
    if sneeze and "choo" not in b.st:
        b.st["choo"] = 1
        x, y = bf.pos(sc)
        bf.mode = "fly"
        bf.x.snap(x)
        bf.y.snap(y)
        bf.x.v, bf.y.v = b.st["dir"] * 400 * S, -500 * S
        bf.flap = 14
        a.face.set("neutral", lid=1.0, lid_lo=1.0, open=0.35, width=0.45, brow=-0.3)
        a.squash.v -= 2.5
        sc.say("CHOO!", b.st["dir"], 14)
        a.lean_tgt, a.tilt_tgt = 11, 14
    if sneeze and b.p > 0.75 and "oops" not in b.st:
        b.st["oops"] = 1
        a.face.set("embarrassed")
        a.lean_tgt, a.tilt_tgt = 0, 4
    hx, hy = sc.head()
    bf.tx = hx + b.st["dir"] * 400 * S
    bf.ty = hy - 220 * S
    if not sneeze:
        a.look_at(*bf.pos(sc))
        w = math.sin(b.t * 2 * math.pi * 2.4)
        i = 1 if b.st["dir"] > 0 else 0
        side = -1 if i == 0 else 1
        a.hand_tgt[i] = [side * (12 * S + 5 * S * w), -24 * S]
        a.hang(1 - i)
        if b.p > 0.3 and "bye" not in b.st:
            b.st["bye"] = 1
            sc.say("bye bye!", side, 12)
    a.set_hip(None, a.stand_y(0.02))


def _scene_butterfly(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    sc.walk(sc.pick_x(), expr="happy")
    bf = _Butterfly(sc, rng.choice(("#f59e0b", "#60a5fa", "#f472b6", "#a78bfa", "#34d399")))
    sc.beat(2.4, _b_bfly_arrive, bf=bf)
    sc.beat(2.2, _b_bfly_chase, bf=bf)
    sc.beat(2.4, _b_bfly_land, bf=bf, then=rng.choice(("smitten", "delight", "joy")))
    sc.beat(2.0, _b_bfly_leave, bf=bf, sneeze=rng.random() < 0.45)
    sc.beat(0.6, _b_idle, expr="happy")
    _outro(sc)
    return sc


# ---- balloon ----------------------------------------------------------------

class _Balloon:
    layer = "front"

    def __init__(self, color):
        self.color = color
        self.r = _Spring(0.0, 3.0, 0.35)
        self.r_tgt = 0.0
        self.mode = "mouth"    # mouth | held | free | popped
        self.free_xy = None
        self.vy = 0.0

    def update(self, dt, sc):
        self.r.step(self.r_tgt, dt)
        if self.mode == "free" and self.free_xy:
            x, y = self.free_xy
            self.vy = max(self.vy - 60 * sc.S * dt, -90 * sc.S)
            self.free_xy = (x + math.sin(sc.t * 2) * 12 * sc.S * dt, y + self.vy * dt)
        return True

    def center(self, sc):
        a, S = sc.actor, sc.S
        g = a.geo or a.geometry()
        rb = max(0.5, self.r.x) * S
        if self.mode == "mouth":
            hx, hy = g["head"]
            d = 1 if g["turn"] >= 0 else -1
            return hx + d * (a.r * 0.75 + rb * 0.9), hy + a.r * 0.4, rb, None
        hand = g["hand1"]
        if self.mode == "held":
            sway = math.sin(sc.t * 1.6) * 6 * S
            return hand[0] + sway, hand[1] - 30 * S - rb, rb, hand
        if self.mode == "free" and self.free_xy:
            return self.free_xy[0], self.free_xy[1], rb, None
        return None

    def draw(self, cv, sc):
        if self.mode == "popped" or self.r.x < 0.4:
            return
        c = self.center(sc)
        if c is None:
            return
        x, y, rb, hand = c
        S = sc.S
        ink = sc.pal["ink"]
        bot = (x, y + rb * 1.15)
        if hand is not None:
            cv.create_line(hand[0], hand[1], (hand[0] + bot[0]) / 2 + 5 * S * math.sin(sc.t * 2.4), (hand[1] + bot[1]) / 2,
                           bot[0], bot[1], fill=ink, width=1, smooth=True)
        elif self.mode == "free":
            cv.create_line(bot[0], bot[1], bot[0] + 4 * S * math.sin(sc.t * 3), bot[1] + 14 * S,
                           bot[0] - 2 * S, bot[1] + 26 * S, fill=ink, width=1, smooth=True)
        cv.create_oval(x - rb, y - rb * 1.15, x + rb, y + rb * 1.15, fill=self.color, outline=_mix(self.color, "#000000", 0.3))
        cv.create_polygon(bot[0], bot[1] - 1, bot[0] - 2.2 * S, bot[1] + 3 * S, bot[0] + 2.2 * S, bot[1] + 3 * S,
                          fill=_mix(self.color, "#000000", 0.2), outline="")
        cv.create_oval(x - rb * 0.55, y - rb * 0.8, x - rb * 0.2, y - rb * 0.35, fill=_mix(self.color, "#ffffff", 0.6), outline="")


def _b_blow(sc, b):
    a, S = sc.actor, sc.S
    bl = b.data["bl"]
    if b.first:
        if bl not in sc.props:
            sc.props.append(bl)
        a.turn_tgt = 0.55
        a.arm_front[1] = True
        b.st["n"] = -1
    n = b.data.get("breaths", 3)
    k = min(n - 1, int(b.p * n))
    q = (b.p * n) % 1.0
    a.hand_tgt[1] = [7 * S, -8 * S]
    a.hang(0)
    if q < 0.35:
        a.face.set("neutral", open=0.45, width=0.4, brow=0.3, lid=0.2) if a.face.expr != "neutral" else None
        a.lean_tgt = -3
        a.set_hip(None, a.stand_y(0.0) - 1.5 * S)
    else:
        if b.st["n"] != k:
            b.st["n"] = k
            a.face.set("puff")
            bl.r_tgt = 4 + 7.5 * (k + 1) / n
        a.lean_tgt = 4
        a.face.nudge(blush=0.3 + 0.5 * (k + 1) / n)
        a.set_hip(None, a.stand_y(0.04))
    a.face.look(0.7, 0.1)


def _b_tie(sc, b):
    a, S = sc.actor, sc.S
    bl = b.data["bl"]
    if b.first:
        bl.mode = "held"
        a.arm_front[1] = False
        a.face.set("joy")
        a.turn_tgt = 0.0
        hx, hy = sc.head()
        sc.fx.sparkle(hx + 20 * S, hy - 30 * S, n=6, speed=80)
    a.hand_tgt[1] = [10 * S, -22 * S]
    a.hang(0)
    c = bl.center(sc)
    if c:
        a.look_at(c[0], c[1])


def _b_float(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.set_feet_mode("free")
        a.hip_f = 1.6
        b.st["x"], b.st["y"] = a.hip_tgt
        a.face.set("starry" if sc.rng.random() < 0.4 else "joy")
    q = _smooth(_seg(b.p, 0.0, 0.7))
    a.set_hip(b.st["x"] + 18 * S * math.sin(b.t * 1.2) * q, b.st["y"] - 70 * S * q)
    sw = math.sin(b.t * 3)
    a.foot_rel_tgt = [[-4 * S + 3 * S * sw, a.leg * 0.93], [4 * S - 3 * S * sw, a.leg * 0.9]]
    a.hand_tgt[1] = [6 * S, -27 * S]
    a.hand_tgt[0] = [-16 * S, 6 * S + 4 * S * sw]
    a.lean_tgt = 4 * sw
    _look_viewer(sc)
    if b.p > 0.5 and int(b.t * 3) % 2 == 0 and ("h%d" % int(b.t * 3)) not in b.st:
        b.st["h%d" % int(b.t * 3)] = 1
        hx, hy = sc.head()
        sc.fx.symbol("heart", hx + sc.rng.uniform(-14, 14) * S, hy - 14 * S, vy=-30, life=1.2, size=6)


def _b_pop(sc, b):
    a, S = sc.actor, sc.S
    bl = b.data["bl"]
    if b.first:
        c = bl.center(sc)
        bl.mode = "popped"
        if c:
            sc.fx.add("ring", c[0], c[1], life=0.3, size=8, color=bl.color)
            for _ in range(10):
                ang = sc.rng.uniform(0, 2 * math.pi)
                sc.fx.add("confetti", c[0], c[1], math.cos(ang) * 160 * S, math.sin(ang) * 160 * S, 0.9, 4, bl.color,
                          g=300 * S, drag=2.5, spin=9)
            sc.fx.symbol("text", c[0], c[1] - 10 * S, vy=-20, life=0.8, size=17, color=sc.pal["ink"], text="POP!")
        a.face.set("surprise")
        a.hip_f = 12
        b.st["x"] = a.hip_tgt[0]
    q = _seg(b.p, 0.15, 0.55)
    y0 = a.stand_y() - 70 * S
    if b.p < 0.15:
        a.set_hip(None, y0 - 4 * S)
        a.hands((-14 * S, -20 * S), (14 * S, -20 * S))
    elif b.p < 0.55:
        a.set_hip(b.st["x"], y0 + (a.stand_y(0.15) - y0) * q * q)
        a.tuck(0.2)
        a.hands((-18 * S, -16 * S), (18 * S, -16 * S))
        a.face.set("worried") if q > 0.3 and a.face.expr != "worried" else None
    else:
        if "land" not in b.st:
            b.st["land"] = 1
            a.hip_f = 9
            a.land(1.3, fx=sc.fx)
            a.face.set("dizzy")
        a.set_hip(b.st["x"], a.stand_y(0.08))
        a.hang()
        if b.p > 0.8 and "l" not in b.st:
            b.st["l"] = 1
            a.face.set("laugh")


def _b_descend(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.hip_f = 2.0
        b.st["x"] = a.hip_tgt[0]
    a.set_hip(b.st["x"], a.stand_y())
    a.hand_tgt[1] = [6 * S, -27 * S]
    a.hang(0)
    if b.p > 0.75 and a.feet_mode == "free":
        a.hip_f = 7
        a.land(0.4, fx=sc.fx)


def _b_let_go(sc, b):
    a, S = sc.actor, sc.S
    bl = b.data["bl"]
    if b.first:
        c = bl.center(sc)
        if c:
            bl.free_xy = (c[0], c[1])
        bl.mode = "free"
        a.face.set("surprise")
        a.hang()
    c = bl.center(sc)
    if c:
        a.look_at(c[0], c[1])
    if 0.25 < b.p < 0.6:
        a.face.set("sad") if a.face.expr != "sad" else None
        a.hand_tgt[1] = [14 * S, -22 * S]
    elif b.p >= 0.6:
        if "ok" not in b.st:
            b.st["ok"] = 1
            a.face.set("happy")
            a.hand_fz[1] = (6.0, 0.4)
        w = math.sin(b.t * 2 * math.pi * 2.4)
        a.hand_tgt[1] = [12 * S + 5 * S * w, -24 * S]


def _scene_balloon(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    sc.walk(sc.pick_x(), expr="happy")
    bl = _Balloon(rng.choice(("#f43f5e", "#f59e0b", "#22c55e", "#3b82f6", "#a855f7", "#ec4899")))
    sc.beat(0.6, _b_idle, expr="mischief")
    sc.beat(rng.uniform(2.6, 3.2), _b_blow, bl=bl, breaths=3)
    sc.beat(1.0, _b_tie, bl=bl)
    sc.beat(2.6, _b_float, bl=bl)
    if rng.random() < 0.55:
        sc.beat(2.2, _b_pop, bl=bl)
    else:
        sc.beat(1.8, _b_descend, bl=bl)
        sc.beat(2.6, _b_let_go, bl=bl)
    _outro(sc)
    return sc


# ---- stretches ----------------------------------------------------------------

def _b_stretch(sc, b):
    a, S = sc.actor, sc.S
    mv = b.data["move"]
    q = math.sin(math.pi * b.p)
    if b.first:
        a.face.set(b.data.get("expr", "calm"))
        a.hand_fz = [(3.0, 0.75), (3.0, 0.75)]
        a.set_feet_mode("stand")
        b.st["x"] = a.hip_tgt[0]
        if b.data.get("say"):
            sc.say(b.data["say"], 1, 12)
    x = b.st["x"]
    if mv == "reach":
        a.hands((-5 * S, -29 * S * q + 22 * S * (1 - q)), (5 * S, -29 * S * q + 22 * S * (1 - q)))
        a.set_hip(x, a.stand_y() - 4 * S * q)
    elif mv == "side":
        d = b.data.get("d", 1)
        a.lean_tgt = d * 22 * q
        a.bend_tgt = -d * 5 * S * q
        a.hands((-8 * S + d * 10 * S * q, -26 * S), (8 * S + d * 10 * S * q, -26 * S))
        a.tilt_tgt = d * 10 * q
        a.set_hip(x - d * 3 * S * q, a.stand_y())
    elif mv == "toes":
        d = b.data.get("d", 1)
        a.turn_tgt = d * 0.95
        a.lean_tgt = d * 78 * q
        a.hands((d * 4 * S, 26 * S + 4 * S * q), (d * 6 * S, 26 * S + 4 * S * q))
        a.set_hip(x - d * 6 * S * q, a.stand_y(0.02))
        a.face.set("effort") if q > 0.8 and a.face.expr != "effort" else None
    elif mv == "neck":
        ang = b.p * 2 * math.pi
        a.tilt_tgt = 16 * math.sin(ang)
        a.face.look(math.sin(ang) * 0.6, -math.cos(ang) * 0.5)
        a.hang()
        a.set_hip(x, a.stand_y())
    elif mv == "tree":
        d = b.data.get("d", 1)
        a.set_feet_mode("free")
        wob = math.sin(b.t * 2.3) * (0.6 + 0.4 * math.sin(b.t * 5.1))
        a.foot_rel_tgt = [[-d * 1 * S, a.leg * 0.97], [d * 12 * S, a.leg * 0.55]] if d > 0 else \
                         [[d * 12 * S * -1, a.leg * 0.55], [1 * S, a.leg * 0.97]]
        a.hands((-1.5 * S, -30 * S), (1.5 * S, -30 * S))
        a.lean_tgt = 5 * wob
        a.set_hip(x + 2 * S * wob, a.stand_y(-0.01))
        if abs(wob) > 0.85 and "w" not in b.st:
            b.st["w"] = 1
            a.face.set("worried")
        elif b.p > 0.75 and "c" not in b.st:
            b.st["c"] = 1
            a.face.set("calm")
        if b.p >= 1.0:
            a.set_feet_mode("stand")
    if b.p >= 1.0:
        a.lean_tgt = a.bend_tgt = a.tilt_tgt = 0.0
        a.turn_tgt = 0.0
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _scene_stretch(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc, meet="sleepy")
    sc.walk(sc.pick_x(), speed=80, expr="happy")
    sc.beat(1.8, _b_stretch, move="reach", expr="yawn", say="mmmh…")
    seq = rng.sample(["side", "toes", "neck", "tree"], 3)
    for mv in seq:
        if mv == "side":
            d = rng.choice((-1, 1))
            sc.beat(1.6, _b_stretch, move="side", d=d)
            sc.beat(1.6, _b_stretch, move="side", d=-d)
        else:
            sc.beat(2.4 if mv == "tree" else 1.9, _b_stretch, move=mv, d=rng.choice((-1, 1)))
    sc.beat(1.2, _b_idle, expr="proud")
    _spice(sc, 0.35)
    _outro(sc)
    return sc


# ---- jumping jacks + flex -------------------------------------------------------

def _b_jacks(sc, b):
    a, S = sc.actor, sc.S
    n = b.data.get("n", 5)
    if b.first:
        a.set_feet_mode("free")
        a.hip_f = 16
        a.face.set("determined")
        a.hand_fz = [(8.0, 0.6), (8.0, 0.6)]
        b.st["x"] = a.hip_tgt[0]
    ph = b.p * n
    k = min(n - 1, int(ph))
    q = ph - int(ph)
    op = math.sin(math.pi * q)
    a.foot_rel_tgt = [[-(4 + 11 * op) * S, a.leg * (0.96 - 0.04 * op)], [(4 + 11 * op) * S, a.leg * (0.96 - 0.04 * op)]]
    a.hands((-(6 + 12 * op) * S, (24 - 50 * op) * S), ((6 + 12 * op) * S, (24 - 50 * op) * S))
    air = math.sin(math.pi * ((q * 2) % 1.0))
    a.set_hip(b.st["x"], a.stand_y(0.02) - air * 7 * S)
    key = "k%d" % k
    if q > 0.45 and key not in b.st:
        b.st[key] = 1
        hx, hy = sc.head()
        sc.fx.symbol("text", hx + 20 * S, hy - 20 * S, vy=-20, life=0.8, size=12, color=sc.pal["ink"], text=str(k + 1))
        if k == n // 2:
            a.face.set("effort")
        if k >= n // 2:
            sc.fx.add("drop", hx + sc.rng.choice((-1, 1)) * 13 * S, hy - 8 * S, sc.rng.uniform(-30, 30) * S, -40 * S,
                      0.7, 3.2, sc.pal["sweat"], g=260 * S)
    if b.p >= 1.0:
        a.set_feet_mode("stand")
        a.hip_f = 7
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _b_flex(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("proud")
        a.hand_fz = [(6.0, 0.4), (6.0, 0.4)]
        hx, hy = sc.head()
        sc.fx.sparkle(hx, hy, n=10, speed=140)
        a.squash.v += 1.0
    pump = math.sin(b.t * 2 * math.pi * 1.6)
    a.hands((-15 * S, -12 * S - 2 * S * pump), (15 * S, -12 * S + 2 * S * pump))
    a.set_hip(None, a.stand_y(0.06))
    a.tilt_tgt = 5 * pump
    _look_viewer(sc)
    if b.p > 0.5 and "w" not in b.st and sc.rng.random() < 0.5:
        b.st["w"] = 1
        a.face.nudge(wink=1.0)
    if b.p >= 1.0:
        a.face.nudge(wink=0.0)
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _b_phew(sc, b):
    a, S = sc.actor, sc.S
    if b.first:
        a.face.set("happy", lid=0.4)
        a.arm_front[1] = True
        sc.say("phew!", 1, 12)
    a.hand_tgt[1] = [-8 * S + 12 * S * _smooth(b.p), -31 * S]
    a.hang(0)
    if b.p >= 1.0:
        a.arm_front[1] = False
        a.hang()


def _scene_jacks(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc, meet="determined")
    sc.walk(sc.pick_x(), speed=110, expr="determined")
    n = rng.choice((4, 5, 6))
    sc.beat(n * 0.62, _b_jacks, n=n)
    if rng.random() < 0.5:
        sc.beat(1.2, _b_phew)
    sc.beat(1.8, _b_flex)
    _spice(sc, 0.3)
    _outro(sc)
    return sc


# ---- weights ------------------------------------------------------------------

def _barbell(cv, x, y, a):
    g = a.geo
    if not g:
        return
    S = a.S
    L, R = g["hand0"], g["hand1"]
    dx, dy = R[0] - L[0], R[1] - L[1]
    d = math.hypot(dx, dy) or 1.0
    ux, uy = dx / d, dy / d
    ext = 14 * S
    p0 = (L[0] - ux * ext, L[1] - uy * ext)
    p1 = (R[0] + ux * ext, R[1] + uy * ext)
    cv.create_line(*p0, *p1, fill="#64748b", width=max(2, 2.6 * S), capstyle="round")
    for (px, py) in (p0, p1):
        for k, rr in ((0.0, 8.5), (3.2, 6.5)):
            cx, cy = px - ux * k * S * (1 if (px, py) == p1 else -1), py
            cv.create_oval(cx - 2.6 * S, cy - rr * S, cx + 2.6 * S, cy + rr * S, fill="#334155", outline="#0f172a")


def _b_lift(sc, b):
    a, S = sc.actor, sc.S
    hard = b.data.get("hard", False)
    if b.first:
        a.hand_items = [None, _barbell]
        a.face.set("determined")
        b.st["x"] = a.hip_tgt[0]
        a.hand_fz = [(6.0, 0.8), (6.0, 0.8)]
    p, x = b.p, b.st["x"]
    if p < 0.2:
        q = _smooth(p / 0.2)
        a.set_hip(x, a.stand_y(0.38 * q))
        a.hands((-15 * S, (24 + 10 * q) * S), (15 * S, (24 + 10 * q) * S))
    elif p < 0.42:
        q = _smooth(_seg(p, 0.2, 0.42))
        a.set_hip(x, a.stand_y(0.38 * (1 - q)))
        a.hands((-15 * S, (34 - 34 * q) * S), (15 * S, (34 - 34 * q) * S))
        if "e" not in b.st:
            b.st["e"] = 1
            a.face.set("effort")
    elif p < 0.62:
        q = _smooth(_seg(p, 0.42, 0.62))
        shake = (math.sin(b.t * 45) * 1.6 * S) if hard else 0.0
        a.hands((-15 * S + shake, (0 - 30 * q) * S), (15 * S + shake, (0 - 30 * q) * S))
        a.set_hip(x, a.stand_y(0.06))
        if hard and int(b.t * 3) != b.st.get("sw"):
            b.st["sw"] = int(b.t * 3)
            hx, hy = sc.head()
            sc.fx.add("drop", hx + sc.rng.choice((-1, 1)) * 13 * S, hy - 8 * S, sc.rng.uniform(-30, 30) * S, -40 * S,
                      0.7, 3.2, sc.pal["sweat"], g=260 * S)
    elif p < 0.85:
        if "top" not in b.st:
            b.st["top"] = 1
            a.face.set("starry" if sc.rng.random() < 0.4 else "joy")
            hx, hy = sc.head()
            sc.fx.sparkle(hx, hy - 34 * S, n=12, speed=160)
        a.hands((-15 * S, -30 * S), (15 * S, -30 * S))
        a.set_hip(x, a.stand_y(0.02))
    else:
        q = _smooth(_seg(p, 0.85, 1.0))
        a.hands((-15 * S, (-30 + 56 * q) * S), (15 * S, (-30 + 56 * q) * S))
        if q > 0.9 and "drop" not in b.st:
            b.st["drop"] = 1
            sc.fx.dust(x, sc.top, n=6, spread=40)
            a.squash.v -= 1.5
            a.face.set("proud")
    if p >= 1.0:
        a.hand_items = [None, None]
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]
        a.hang()


def _scene_weights(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    sc.walk(sc.pick_x(), speed=110, expr="determined")
    sc.beat(0.8, _b_idle, expr="determined")
    hard = rng.random() < 0.55
    sc.beat(4.2 if hard else 3.4, _b_lift, hard=hard)
    if rng.random() < 0.5:
        sc.beat(1.6, _b_flex)
    else:
        sc.beat(1.0, _b_phew)
    _outro(sc)
    return sc


# ---- toothbrush (a very clean tooth) -------------------------------------------

class _Tooth:
    layer = "back"

    def __init__(self, sc, x):
        self.x = x
        self.rise = _Spring(0.0, 2.5, 0.55)
        self.rise_tgt = 0.0
        self.dirt = 1.0
        self.mood = "grumpy"
        self.hop = _Spring(0.0, 4.0, 0.3)
        self.spots = [(sc.rng.uniform(-0.3, 0.3), sc.rng.uniform(0.35, 0.75), sc.rng.uniform(0.07, 0.11)) for _ in range(4)]

    def update(self, dt, sc):
        self.rise.step(self.rise_tgt, dt)
        self.hop.step(0.0, dt)
        return True

    def draw(self, cv, sc):
        S = sc.S
        H, W = 46 * S, 38 * S
        base = sc.top + H * (1 - self.rise.x) + self.hop.x * S
        x = self.x
        ink = sc.pal["ink"]
        shape = [(-0.5, 0.55), (-0.5, 0.9), (-0.26, 1.03), (0, 0.9), (0.26, 1.03), (0.5, 0.9), (0.5, 0.55),
                 (0.38, 0.1), (0.3, 0.0), (0.12, 0.05), (0.0, 0.3), (-0.12, 0.05), (-0.3, 0.0), (-0.38, 0.1)]
        pts = []
        for u, v in shape:
            pts += [x + u * W, base - v * H]
        fill = _mix("#fde68a", "#ffffff", 1 - 0.6 * self.dirt)
        cv.create_polygon(pts, fill=fill, outline=ink, width=max(1.6, 2 * S), smooth=True)
        for (u, v, r) in self.spots:
            if self.dirt > 0.08:
                cx, cy = x + u * W, base - v * H
                rr = r * W * (0.4 + 0.6 * self.dirt)
                cv.create_oval(cx - rr, cy - rr * 0.8, cx + rr, cy + rr * 0.8,
                               fill=_mix(fill, "#a16207", 0.75 * self.dirt), outline="")
        ey = base - 0.68 * H
        for side in (-1, 1):
            ex = x + side * 0.15 * W
            if self.mood == "happy":
                cv.create_line(ex - 3 * S, ey + 1 * S, ex, ey - 2.5 * S, ex + 3 * S, ey + 1 * S, smooth=True, fill=ink,
                               width=max(1.4, 1.8 * S), capstyle="round")
            else:
                ry = 3.2 * S if self.mood == "surprised" else 2.6 * S
                cv.create_oval(ex - 2 * S, ey - ry, ex + 2 * S, ey + ry, fill=ink, outline="")
            if self.mood == "grumpy":
                cv.create_line(ex - side * 4 * S, ey - 6 * S, ex + side * 3 * S, ey - 3.5 * S, fill=ink,
                               width=max(1.4, 1.8 * S), capstyle="round")
        my = base - 0.5 * H
        if self.mood == "grumpy":
            cv.create_line(x - 5 * S, my + 2 * S, x, my - 1 * S, x + 5 * S, my + 2 * S, smooth=True, fill=ink,
                           width=max(1.4, 1.8 * S), capstyle="round")
        elif self.mood == "surprised":
            cv.create_oval(x - 2.5 * S, my - 2.5 * S, x + 2.5 * S, my + 3 * S, fill=sc.pal["mouth"], outline=ink)
        else:
            cv.create_polygon(x - 7 * S, my - 1 * S, x - 7 * S, my - 1 * S, x, my + 1 * S, x + 7 * S, my - 1 * S,
                              x + 7 * S, my - 1 * S, x + 4 * S, my + 5 * S, x - 4 * S, my + 5 * S,
                              fill=sc.pal["mouth"], outline=ink, smooth=True)
            for side in (-1, 1):
                cx = x + side * 0.33 * W
                cv.create_oval(cx - 3.5 * S, my - 3 * S, cx + 3.5 * S, my, fill=_mix(fill, "#fb7185", 0.5), outline="")


def _toothbrush(cv, x, y, a):
    S = a.S
    d = getattr(a, "_brush_dir", 1)
    ex, ey = x + d * 15 * S, y - 3 * S
    cv.create_line(x - d * 3 * S, y + 1 * S, ex, ey, fill=a.pal["accent"], width=max(2.4, 3.2 * S), capstyle="round")
    hx0, hx1 = ex - d * 1 * S, ex + d * 5 * S
    cv.create_rectangle(min(hx0, hx1), ey - 5 * S, max(hx0, hx1), ey - 1.2 * S, fill="#ffffff", outline=a.pal["ink"])


def _b_tooth_up(sc, b):
    a, S = sc.actor, sc.S
    tooth = b.data["tooth"]
    if b.first:
        if tooth not in sc.props:
            sc.props.append(tooth)
        tooth.rise_tgt = 1.0
        a.face.set("surprise")
        sc.mask = True
    if b.p > 0.4:
        a.look_at(tooth.x, sc.top - 30 * S)
    if b.p > 0.6 and "w" not in b.st:
        b.st["w"] = 1
        a.face.set("worried")
        sc.fx.symbol("text", tooth.x + 24 * S, sc.top - 56 * S, vy=-10, life=1.2, size=13, color="#a16207", text="hmph")
    if b.p >= 1.0:
        sc.mask = False


def _b_brush(sc, b):
    a, S = sc.actor, sc.S
    tooth = b.data["tooth"]
    d = 1 if tooth.x > a.hip_tgt[0] else -1
    if b.first:
        a._brush_dir = d
        a.hand_items = [None, _toothbrush] if d > 0 else [_toothbrush, None]
        a.face.set("determined")
        a.turn_tgt = d * 0.6
        a.hand_fz = [(9.0, 0.7), (9.0, 0.7)]
    i = 1 if d > 0 else 0
    H, W = 46 * S, 38 * S
    ph = b.t * 2 * math.pi * 3.0
    hy = sc.top - H * (0.45 + 0.25 * math.sin(b.t * 2.1))
    hx = tooth.x - d * (W * 0.62 + 8 * S) + d * 4 * S * math.sin(ph)
    a.hand_world(i, hx, hy)
    a.hang(1 - i)
    a.set_hip(None, a.stand_y(0.05))
    tooth.dirt = max(0.0, 1.0 - b.p * 1.1)
    if int(b.t * 7) != b.st.get("f"):
        b.st["f"] = int(b.t * 7)
        sc.fx.add("bubble", hx + d * 16 * S, hy - 4 * S, sc.rng.uniform(-30, 30) * S, -sc.rng.uniform(20, 50) * S,
                  sc.rng.uniform(0.6, 1.1), sc.rng.uniform(2.2, 3.6), "#f0f9ff", drag=1.5)
    if b.p > 0.45 and tooth.mood == "grumpy":
        tooth.mood = "surprised"
        a.face.set("happy")
    if b.p >= 1.0:
        a.hand_items = [None, None]
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _b_shine(sc, b):
    a, S = sc.actor, sc.S
    tooth = b.data["tooth"]
    if b.first:
        tooth.mood = "happy"
        tooth.dirt = 0.0
        tooth.hop.v = -120
        sc.fx.sparkle(tooth.x + 10 * S, sc.top - 40 * S, n=12, speed=150, colors=["#ffffff", "#bae6fd", sc.pal["gold"]])
        sc.fx.symbol("star", tooth.x + 14 * S, sc.top - 44 * S, vy=0, life=0.9, size=10, color="#fde68a")
        a.face.set("starry" if sc.rng.random() < 0.5 else "joy")
        sc.say("ding!", 1 if tooth.x > a.hip_tgt[0] else -1, 13)
        a.turn_tgt = 0.0
    if 0.45 < b.p < 0.5 and "hop2" not in b.st:
        b.st["hop2"] = 1
        tooth.hop.v = -90
    d = 1 if tooth.x > a.hip_tgt[0] else -1
    i = 1 if d > 0 else 0
    if b.p < 0.6:
        a.hand_world(i, tooth.x - d * 14 * S, sc.top - 52 * S)
        a.arm_front[i] = True
    else:
        a.arm_front[i] = False
        a.hands((-18 * S, -16 * S), (18 * S, -16 * S))
    a.set_hip(None, a.stand_y(0.03))
    _look_viewer(sc)


def _b_tooth_down(sc, b):
    tooth = b.data["tooth"]
    if b.first:
        tooth.rise_tgt = 0.0
        sc.mask = True
        sc.actor.face.set("happy")
    sc.actor.hang()
    _look_viewer(sc)
    if b.p >= 1.0:
        sc.mask = False
        if tooth in sc.props:
            sc.props.remove(tooth)


def _scene_tooth(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc)
    x = sc.pick_x()
    d = 1 if x + 60 * sc.S < sc.hi else -1
    sc.walk(x, expr="happy")
    tooth = _Tooth(sc, x + d * 52 * sc.S)
    sc.beat(1.8, _b_tooth_up, tooth=tooth)
    sc.beat(rng.uniform(2.6, 3.2), _b_brush, tooth=tooth)
    sc.beat(2.0, _b_shine, tooth=tooth)
    sc.beat(1.1, _b_tooth_down, tooth=tooth)
    _outro(sc)
    return sc


# ---- celebrate -------------------------------------------------------------------

def _b_fist_pump(sc, b):
    a, S = sc.actor, sc.S
    n = b.data.get("n", 3)
    if b.first:
        a.face.set("joy")
        a.hand_fz = [(8.0, 0.5), (8.0, 0.5)]
    q = (b.p * n) % 1.0
    k = int(b.p * n)
    i = k % 2
    side = -1 if i == 0 else 1
    up = math.sin(math.pi * q)
    a.hand_tgt[i] = [side * 8 * S, (10 - 40 * up) * S]
    a.hand_tgt[1 - i] = [-side * 9 * S, 10 * S]
    a.set_hip(None, a.stand_y(0.06) - up * 3 * S)
    key = "y%d" % k
    if q > 0.4 and key not in b.st and k < n:
        b.st[key] = 1
        sc.say(sc.rng.choice(("yes!", "woo!", "yay!")), side, 13)
    _look_viewer(sc)
    if b.p >= 1.0:
        a.hand_fz = [(5.0, 0.55), (5.0, 0.55)]


def _scene_celebrate(button, w, h, rng):
    sc = Scene(button, w, h, rng)
    _intro(sc, meet="joy")
    if rng.random() < 0.5:
        sc.walk(sc.pick_x(min_d=60, max_d=200), speed=120, bounce=3.0, expr="joy")
    sc.beat(1.8, _b_jump_joy, n=2, confetti=True)
    sc.beat(1.8, _b_fist_pump, n=3)
    sc.beat(1.8, _b_dance, move="spin", bpm=2.2)
    sc.beat(1.3, _b_ta_da, expr="starry" if rng.random() < 0.5 else "proud")
    _outro(sc)
    return sc


_SCENES = {
    "surprise": ("Peekaboo hello", _scene_hello),
    "dance": ("Little dance", _scene_dance),
    "juggle": ("Juggling", _scene_juggle),
    "cartwheel": ("Acrobatics", _scene_acro),
    "sleep": ("Power nap", _scene_nap),
    "butterfly": ("Butterfly friend", _scene_butterfly),
    "balloon": ("Balloon ride", _scene_balloon),
    "stretches": ("Stretch break", _scene_stretch),
    "jacks": ("Jumping jacks", _scene_jacks),
    "weights": ("Lifts weights", _scene_weights),
    "toothbrush": ("Sparkly tooth", _scene_tooth),
    "celebrate": ("Celebration", _scene_celebrate),
}

# Registry — iteration order is the picker's list order. Keys are stable ids
# stored in the config file (several reuse the old ids so saved on/off
# choices carry over); renaming one is a breaking change.
ANIMATIONS: dict = {
    _id: {"name": _name, "duration_ms": 15000, "scene": _fn}
    for _id, (_name, _fn) in _SCENES.items()
}


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
    MIN_COUNT = 3
    MAX_COUNT = 5

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


# Default enabled state per animation when the user hasn't toggled it in the
# picker. Every scene ships enabled.
DEFAULT_ENABLED: dict[str, bool] = {anim_id: True for anim_id in ANIMATIONS}

_last_played: Optional[str] = None


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
    global _last_played
    enabled = [aid for aid in ANIMATIONS if is_animation_enabled(aid, prefs)]
    if not enabled:
        return None
    # Never the same scene twice in a row (when there's a choice).
    fresh = [aid for aid in enabled if aid != _last_played] or enabled
    _last_played = rng.choice(fresh)
    return _last_played

