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
    'plain' (the default character) when the key is missing or unknown."""
    char_id = _read_full_config().get("animation_character", "plain")
    if not isinstance(char_id, str) or char_id not in CHARACTERS:
        return "plain"
    return char_id


def save_animation_character(char_id: str) -> None:
    """Write the chosen character id to the shared config file."""
    if char_id not in CHARACTERS:
        char_id = "plain"
    data = _read_full_config()
    data["animation_character"] = char_id
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
# Character decorations — drawn on top of the bare stick figure to give it
# a bit of personality. Each function takes the head position, head radius,
# body angle (so decorations rotate correctly during cartwheels etc.), and
# the same scale/stroke/color as the base figure. The function paints
# whatever sits on top of the head, on its face, or hanging from the neck.
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


# Registry of characters. The key is the stable id used in the config file.
# Renaming a key is a breaking change for the user's saved preference.
CHARACTERS: dict = {
    "plain":     {"name": "Plain (no extras)",   "draw_extra": _decor_plain},
    "wavy_hair": {"name": "Wavy hair",            "draw_extra": _decor_wavy_hair},
    "top_hat":   {"name": "Top hat",              "draw_extra": _decor_top_hat},
    "mohawk":    {"name": "Mohawk",               "draw_extra": _decor_mohawk},
    "glasses":   {"name": "Glasses",              "draw_extra": _decor_glasses},
    "beard":     {"name": "Long beard",           "draw_extra": _decor_beard},
    "bow_tie":   {"name": "Bow tie",              "draw_extra": _decor_bow_tie},
}


# Module-level state: which character the renderer should apply. The player
# (and the picker) sets this once before each animation starts; every call
# to draw_figure_pose during that animation reads from here. Single-threaded
# Tk + at-most-one-animation-at-a-time means we don't need a lock.
_current_character_id: str = "plain"


def set_character(char_id: str) -> None:
    """Set the character whose decorations will be applied to subsequent
    draw_figure_pose calls. Unknown ids fall back to 'plain'."""
    global _current_character_id
    _current_character_id = char_id if char_id in CHARACTERS else "plain"


def get_character() -> str:
    """Return the currently active character id."""
    return _current_character_id


def draw_figure_pose(
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

    # Apply the active character's decoration on top of the basic figure
    # (hair, hat, glasses, beard, etc.). The decoration uses body_angle so
    # it rotates correctly when the figure cartwheels or lies down.
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


# Walking gait: returns a tuple (left_leg, right_leg, left_arm, right_arm)
# of joint-angle tuples for a single phase of walking. `phase` is a continuous
# value (radians). `intensity` scales how much the limbs swing.
def _walk_pose(phase: float, intensity: float = 1.0):
    swing = math.sin(phase) * 22 * intensity
    arm_swing = math.sin(phase) * 18 * intensity
    left_leg = (90 - swing, 90 - swing * 0.6)
    right_leg = (90 + swing, 90 + swing * 0.6)
    left_arm = (90 + arm_swing, 90 + arm_swing * 0.7)
    right_arm = (90 - arm_swing, 90 - arm_swing * 0.7)
    return left_leg, right_leg, left_arm, right_arm


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

    FRAME_MS = 33  # ~30fps

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
        self._button_pos: tuple[float, float] = (0.0, 0.0)
        self._after_id: Optional[str] = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def play(self, anim_id: str, button_x: float, button_y: float) -> None:
        if anim_id not in ANIMATIONS:
            return
        self.stop()  # cancel any in-flight animation
        self._anim_id = anim_id
        self._button_pos = (button_x, button_y)
        self._start_time = time.monotonic()
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
        try:
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            bx, by = self._button_pos
            anim["draw_fn"](canvas, t, w, h, bx, by)
        except tk.TclError:
            self.stop()
            return

        if t >= 1.0:
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

        bx_screen = (
            button_widget.winfo_rootx()       # type: ignore[attr-defined]
            + button_widget.winfo_width() // 2  # type: ignore[attr-defined]
        )
        by_screen = (
            button_widget.winfo_rooty()       # type: ignore[attr-defined]
            + button_widget.winfo_height() // 2  # type: ignore[attr-defined]
        )
    except tk.TclError:
        return None

    overlay = StickFigureOverlay(parent_window)
    overlay.show(px, py, pw, ph)

    # Translate button center from screen → overlay-canvas coordinates.
    button_x = bx_screen - px
    button_y = by_screen - py

    def _done() -> None:
        overlay.hide()
        overlay.destroy()
        if on_complete:
            try:
                on_complete()
            except Exception:
                pass

    player = AnimationPlayer(parent_window, overlay, on_complete=_done)
    player.play(anim_id, button_x, button_y)
    return player


# ---------------------------------------------------------------------------
# The 10 animations. Each `draw_anim_*` is a function:
#   def draw_anim_xxx(canvas, t, w, h, bx, by):
# with `t` in [0, 1], canvas dimensions w/h, and button center (bx, by) in
# overlay-canvas coordinates. Each draws one frame for the current `t`.
# Animations clear the canvas via the player (it calls delete('all') each
# frame before invoking draw_fn).
# ---------------------------------------------------------------------------


def _hidden(t: float, threshold: float = 0.04) -> bool:
    """Phase helper: the figure is 'behind the button' for the first and last
    `threshold` of the animation. We don't draw during these windows."""
    return t < threshold or t > (1.0 - threshold)


# Animation 1: Surprise — figure pops out, hands shoot up, "!" above head.
def draw_anim_surprise(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    # Pick a side to stand on (deterministic via animation phase so we don't
    # bounce around mid-frame — random.choice would re-roll each frame).
    side = 1 if (bx < w * 0.5) else -1
    target_x = bx + side * 130
    target_y = by - 10

    if t < 0.20:
        # Emerge + walk out
        local = _ease_in_out((t - 0.04) / 0.16)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 18
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.85:
        # Surprised pose — both arms straight up, slight head jiggle
        hx, hy = target_x, target_y
        # Small jiggle for the held pose
        jiggle = math.sin((t - 0.20) * 14) * 1.5
        draw_figure_pose(
            canvas, hx, hy + jiggle,
            left_arm=(280, 290),    # both arms reaching up
            right_arm=(260, 250),
        )
        # "!" above head
        head_x = hx
        head_y = hy - LEN_BODY - HEAD_R * 2 - 8
        canvas.create_line(
            head_x, head_y - 26,
            head_x, head_y - 8,
            fill=COLOR, width=STROKE, capstyle="round",
        )
        canvas.create_oval(
            head_x - 2, head_y - 2, head_x + 2, head_y + 2,
            outline=COLOR, fill=COLOR, width=STROKE,
        )
    else:
        # Walk back
        local = _ease_in_out((t - 0.85) / 0.11)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 18 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 2: Reads Newspaper — sits cross-legged, holds rectangle, head
# oscillates as if reading.
def draw_anim_newspaper(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    side = 1 if (bx < w * 0.5) else -1
    target_x = bx + side * 150
    target_y = by + 10

    if t < 0.20:
        local = _ease_in_out((t - 0.04) / 0.16)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 16
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.85:
        # Sit cross-legged: legs angled outward, body upright
        hx, hy = target_x, target_y + 20
        head_offset = math.sin((t - 0.20) * 6) * 7  # head pans left/right "reading"
        draw_figure_pose(
            canvas, hx, hy,
            body_angle=270,
            left_leg=(170, 110),     # tucked left
            right_leg=(10, 70),      # tucked right
            left_arm=(20, 350),      # holding newspaper top-left
            right_arm=(160, 190),    # holding newspaper top-right
        )
        # Newspaper rectangle in front of figure
        nx1 = hx - 28
        ny1 = hy - LEN_BODY - 5
        nx2 = hx + 28
        ny2 = hy - LEN_BODY + 22
        # Draw the newspaper, with the head_offset translating it slightly
        canvas.create_rectangle(
            nx1 + head_offset * 0.2, ny1, nx2 + head_offset * 0.2, ny2,
            outline=COLOR, width=STROKE, fill="",
        )
        # A few horizontal lines to suggest text
        for i in range(3):
            ly = ny1 + 6 + i * 6
            canvas.create_line(
                nx1 + 4 + head_offset * 0.2, ly,
                nx2 - 4 + head_offset * 0.2, ly,
                fill=COLOR, width=1,
            )
    else:
        local = _ease_in_out((t - 0.85) / 0.11)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y + 20, by + 30, local)
        phase = t * 16 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 3: Stretches — touches toes, reaches up, twists side-to-side.
def draw_anim_stretches(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    target_x = w * 0.5
    target_y = h * 0.55

    if t < 0.18:
        local = _ease_in_out((t - 0.04) / 0.14)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 18
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.40:
        # Touch toes — body bends forward
        local = _ease_in_out((t - 0.18) / 0.22)
        body = _lerp(270, 200, local)
        arm = _lerp(90, 130, local)
        draw_figure_pose(canvas, target_x, target_y,
                         body_angle=body,
                         left_arm=(arm, arm), right_arm=(arm, arm))
    elif t < 0.65:
        # Stand up and reach up high
        local = _ease_in_out((t - 0.40) / 0.25)
        body = _lerp(200, 270, local)
        arm = _lerp(130, 270, local)
        draw_figure_pose(canvas, target_x, target_y,
                         body_angle=body,
                         left_arm=(arm, arm), right_arm=(arm, arm))
    elif t < 0.86:
        # Twist side to side, arms out
        twist_phase = (t - 0.65) * 4 * math.pi / 0.21
        body_lean = math.sin(twist_phase) * 18
        body_angle = 270 + body_lean
        draw_figure_pose(canvas, target_x, target_y,
                         body_angle=body_angle,
                         left_arm=(180, 180), right_arm=(0, 0))
    else:
        local = _ease_in_out((t - 0.86) / 0.10)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 18 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 4: Horse Ride — figure rides a horse across the screen with a
# bouncing gait, exits behind the button.
def draw_anim_horse(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    # Direction: gallop across the window in a big arc and return
    # Path: start near button, go to far side, swing back via top of screen.
    # Simplified: figure stays at constant height, horse bounces.
    side = -1 if (bx > w * 0.5) else 1
    far_x = bx + side * (w * 0.5)
    base_y = max(h * 0.55, by)

    if t < 0.45:
        # Gallop outward
        local = _ease_in_out((t - 0.04) / 0.41)
        x = _lerp(bx, far_x, local)
    elif t < 0.55:
        # Pause at far side
        x = far_x
    else:
        # Gallop back
        local = _ease_in_out((t - 0.55) / 0.41)
        x = _lerp(far_x, bx, local)

    bounce = abs(math.sin(t * 30)) * 8
    y = base_y - bounce
    # Horse body
    canvas.create_oval(
        x - 32, y + 10, x + 32, y + 28,
        outline=COLOR, width=STROKE, fill="",
    )
    # Horse head (front based on travel direction)
    head_dir = 1 if (t < 0.5) else -1
    if t > 0.45 and t < 0.55:
        head_dir = side
    head_x = x + head_dir * 30
    canvas.create_oval(
        head_x - 7, y, head_x + 7, y + 12,
        outline=COLOR, width=STROKE, fill="",
    )
    canvas.create_line(
        x + head_dir * 20, y + 12, head_x, y + 6,
        fill=COLOR, width=STROKE, capstyle="round",
    )
    # Tail (opposite end)
    tail_x = x - head_dir * 30
    canvas.create_line(
        tail_x, y + 14, tail_x - head_dir * 12, y + 6,
        fill=COLOR, width=STROKE, capstyle="round",
    )
    # 4 horse legs with galloping motion
    leg_phase = t * 30
    for i, offset in enumerate((-20, -8, 8, 20)):
        leg_swing = math.sin(leg_phase + i * 1.4) * 6
        canvas.create_line(
            x + offset, y + 26,
            x + offset + leg_swing, y + 44,
            fill=COLOR, width=STROKE, capstyle="round",
        )
    # Rider — figure on top of horse, leaning slightly forward
    rider_x = x
    rider_y = y + 4
    draw_figure_pose(
        canvas, rider_x, rider_y,
        body_angle=270 + head_dir * 10,
        left_arm=(60 if head_dir > 0 else 120, 70 if head_dir > 0 else 110),
        right_arm=(60 if head_dir > 0 else 120, 70 if head_dir > 0 else 110),
        left_leg=(40, 130),
        right_leg=(140, 50),
        scale=0.7,
    )


# Animation 5: Jumping Jacks — center of screen, several reps.
def draw_anim_jacks(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    target_x = w * 0.5
    target_y = h * 0.55

    if t < 0.15:
        local = _ease_in_out((t - 0.04) / 0.11)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 18
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.88:
        # 6 jacks across this section
        jack_phase = ((t - 0.15) / 0.73) * 6 * math.pi
        open_amt = (math.sin(jack_phase) + 1.0) * 0.5  # 0..1
        bounce = open_amt * -10
        leg_angle = _lerp(90, 60, open_amt)            # legs slightly apart when open
        leg_angle_r = _lerp(90, 120, open_amt)
        arm_angle = _lerp(90, 290, open_amt)           # arms swing up to vertical
        arm_angle_r = _lerp(90, 250, open_amt)
        draw_figure_pose(
            canvas, target_x, target_y + bounce,
            left_arm=(arm_angle, arm_angle),
            right_arm=(arm_angle_r, arm_angle_r),
            left_leg=(leg_angle, leg_angle),
            right_leg=(leg_angle_r, leg_angle_r),
        )
    else:
        local = _ease_in_out((t - 0.88) / 0.08)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 18 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 6: Sleeps — lays down, "Z"s float up, wakes back up.
def draw_anim_sleep(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    side = 1 if (bx < w * 0.5) else -1
    target_x = bx + side * 160
    target_y = by + 20

    if t < 0.20:
        local = _ease_in_out((t - 0.04) / 0.16)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 16
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.78:
        # Lying down: body angle horizontal, legs along same line
        hx, hy = target_x, target_y + 30
        # body horizontal — head to the side closer to the centre of screen
        body_angle = 180 if side > 0 else 0
        draw_figure_pose(
            canvas, hx, hy,
            body_angle=body_angle,
            left_arm=(body_angle, body_angle),
            right_arm=(body_angle, body_angle),
            left_leg=(body_angle, body_angle),
            right_leg=(body_angle, body_angle),
        )
        # "Z"s rising — three at staggered phases
        head_x = hx + (LEN_BODY + HEAD_R + 2) * (1 if side > 0 else -1)
        head_y = hy
        for i in range(3):
            z_phase = ((t - 0.20) * 1.6 - i * 0.35) % 1.0
            if z_phase < 0.05:
                continue
            zx = head_x + (1 if side > 0 else -1) * (10 + z_phase * 18)
            zy = head_y - 10 - z_phase * 50
            size = 9 + i * 2
            opacity_dummy = 1 - z_phase
            if opacity_dummy < 0.05:
                continue
            stroke = max(1, int(STROKE * opacity_dummy + 0.5))
            # Draw a Z
            canvas.create_line(
                zx, zy, zx + size, zy,
                fill=COLOR, width=stroke,
            )
            canvas.create_line(
                zx + size, zy, zx, zy + size,
                fill=COLOR, width=stroke,
            )
            canvas.create_line(
                zx, zy + size, zx + size, zy + size,
                fill=COLOR, width=stroke,
            )
    else:
        # Wake + walk back
        local = _ease_in_out((t - 0.78) / 0.18)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y + 30, by + 30, local)
        phase = t * 16 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 7: Lifts Weights — squat + press, several reps.
def draw_anim_weights(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    target_x = w * 0.5
    target_y = h * 0.55

    if t < 0.15:
        local = _ease_in_out((t - 0.04) / 0.11)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 16
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.88:
        # 4 reps
        rep_phase = ((t - 0.15) / 0.73) * 4 * math.pi
        s = (math.sin(rep_phase) + 1.0) * 0.5  # 0..1
        body_lean = _lerp(20, 0, s)            # squat to standing
        squat_y = _lerp(15, 0, s)
        arm = _lerp(120, 290, s)               # press up
        leg = _lerp(70, 90, s)
        leg_r = _lerp(110, 90, s)
        hy = target_y + squat_y
        draw_figure_pose(
            canvas, target_x, hy,
            body_angle=270 - body_lean,
            left_arm=(arm, arm),
            right_arm=(arm, arm),
            left_leg=(leg, leg),
            right_leg=(leg_r, leg_r),
        )
        # Barbell: a line above head with plates on the ends
        # Approximate the hand position using the same projection
        head_x, head_y = _project(target_x, hy, LEN_BODY, 270)
        bar_x = head_x
        bar_y, _ = _project(0, 0, LEN_UPPER_ARM + LEN_FOREARM, arm)
        bar_y = head_y + bar_y  # rough; the bar follows the hands
        canvas.create_line(
            bar_x - 28, bar_y, bar_x + 28, bar_y,
            fill=COLOR, width=STROKE + 1, capstyle="round",
        )
        canvas.create_oval(
            bar_x - 34, bar_y - 6, bar_x - 24, bar_y + 6,
            outline=COLOR, width=STROKE, fill="",
        )
        canvas.create_oval(
            bar_x + 24, bar_y - 6, bar_x + 34, bar_y + 6,
            outline=COLOR, width=STROKE, fill="",
        )
    else:
        local = _ease_in_out((t - 0.88) / 0.08)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 16 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 8: Dance — side-steps and arm swings, with a spin in the middle.
def draw_anim_dance(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    target_x = w * 0.5
    target_y = h * 0.6

    if t < 0.15:
        local = _ease_in_out((t - 0.04) / 0.11)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 18
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.88:
        local = (t - 0.15) / 0.73
        # Side-step with arm waves; horizontal scale "squish" mid-section to
        # fake a spin (the figure narrows when its body is perpendicular to
        # the viewer).
        beat = math.sin(local * 8 * math.pi)
        side_off = beat * 25
        arm_swing = math.sin(local * 8 * math.pi + 1.0) * 70
        # Brief spin around the midpoint of the section
        spin = 1.0
        if 0.45 < local < 0.55:
            sp = (local - 0.45) / 0.10
            spin = abs(math.cos(sp * math.pi))   # 1..0..1
        bounce = -abs(beat) * 6
        draw_figure_pose(
            canvas, target_x + side_off, target_y + bounce,
            left_arm=(90 + arm_swing, 90 + arm_swing * 0.6),
            right_arm=(90 - arm_swing, 90 - arm_swing * 0.6),
            left_leg=(90, 90 + abs(beat) * 10),
            right_leg=(90, 90 + abs(beat) * 10),
            scale=spin,    # squish for spin
        )
    else:
        local = _ease_in_out((t - 0.88) / 0.08)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 18 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 9: Cartwheels — rotate while translating, then return.
def draw_anim_cartwheel(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    side = 1 if (bx < w * 0.5) else -1
    far_x = bx + side * (w * 0.4)
    base_y = h * 0.55

    if t < 0.10:
        # Emerge
        local = _ease_in_out((t - 0.04) / 0.06)
        hx = _lerp(bx, bx + side * 30, local)
        hy = _lerp(by + 30, base_y, local)
        phase = t * 22
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.45:
        # Cartwheel outward — 2 full rotations
        local = (t - 0.10) / 0.35
        hx = _lerp(bx + side * 30, far_x, local)
        rot = local * 4 * math.pi * side
        # Pose with body rotated; the body angle is rotated about hip
        # All limbs are rotated by the same offset.
        body_angle = (270 + math.degrees(rot)) % 360
        # Limbs straight along body line
        arm = body_angle
        leg = (body_angle + 180) % 360
        draw_figure_pose(
            canvas, hx, base_y,
            body_angle=body_angle,
            left_arm=(arm, arm), right_arm=(arm, arm),
            left_leg=(leg, leg), right_leg=(leg, leg),
        )
    elif t < 0.55:
        # Hold at far side, recover
        draw_figure_pose(
            canvas, far_x, base_y,
            body_angle=270,
            left_arm=(120, 120), right_arm=(60, 60),
        )
    elif t < 0.90:
        # Cartwheel back
        local = (t - 0.55) / 0.35
        hx = _lerp(far_x, bx + side * 30, local)
        rot = local * 4 * math.pi * (-side)
        body_angle = (270 + math.degrees(rot)) % 360
        arm = body_angle
        leg = (body_angle + 180) % 360
        draw_figure_pose(
            canvas, hx, base_y,
            body_angle=body_angle,
            left_arm=(arm, arm), right_arm=(arm, arm),
            left_leg=(leg, leg), right_leg=(leg, leg),
        )
    else:
        local = _ease_in_out((t - 0.90) / 0.06)
        hx = _lerp(bx + side * 30, bx, local)
        hy = _lerp(base_y, by + 30, local)
        phase = t * 22 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# Animation 10: Yoga Tree Pose — one leg up against opposite knee, arms reach
# overhead. Holds for several seconds with gentle "breathing" motion.
def draw_anim_yoga(canvas, t, w, h, bx, by):
    if _hidden(t):
        return
    target_x = w * 0.5
    target_y = h * 0.55

    if t < 0.20:
        local = _ease_in_out((t - 0.04) / 0.16)
        hx = _lerp(bx, target_x, local)
        hy = _lerp(by + 30, target_y, local)
        phase = t * 16
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)
    elif t < 0.88:
        # Tree pose: right leg bent up against left knee, arms straight up
        breath = math.sin((t - 0.20) * 3) * 2
        draw_figure_pose(
            canvas, target_x, target_y + breath,
            left_leg=(90, 90),               # standing leg straight down
            right_leg=(150, 60),             # bent up, foot rests on standing knee
            left_arm=(270, 270),             # both arms straight up
            right_arm=(270, 270),
        )
    else:
        local = _ease_in_out((t - 0.88) / 0.08)
        hx = _lerp(target_x, bx, local)
        hy = _lerp(target_y, by + 30, local)
        phase = t * 16 + math.pi
        ll, rl, la, ra = _walk_pose(phase)
        draw_figure_pose(canvas, hx, hy,
                         left_leg=ll, right_leg=rl,
                         left_arm=la, right_arm=ra)


# ---------------------------------------------------------------------------
# Registry — keep in iteration order so the picker lists them stably.
# Each entry's key is the stable id used in the config file's preferences
# dict. Renaming a key is a breaking change for user preferences.
# ---------------------------------------------------------------------------

ANIMATIONS: dict = {
    "surprise":  {"name": "Surprised!",        "duration_ms": 8000,  "draw_fn": draw_anim_surprise},
    "newspaper": {"name": "Reads the paper",   "duration_ms": 10000, "draw_fn": draw_anim_newspaper},
    "stretches": {"name": "Stretches",         "duration_ms": 9000,  "draw_fn": draw_anim_stretches},
    "horse":     {"name": "Horse ride",        "duration_ms": 12000, "draw_fn": draw_anim_horse},
    "jacks":     {"name": "Jumping jacks",     "duration_ms": 10000, "draw_fn": draw_anim_jacks},
    "sleep":     {"name": "Power nap",         "duration_ms": 12000, "draw_fn": draw_anim_sleep},
    "weights":   {"name": "Lifts weights",     "duration_ms": 10000, "draw_fn": draw_anim_weights},
    "dance":     {"name": "Little dance",      "duration_ms": 10000, "draw_fn": draw_anim_dance},
    "cartwheel": {"name": "Cartwheels",        "duration_ms": 8000,  "draw_fn": draw_anim_cartwheel},
    "yoga":      {"name": "Yoga tree pose",    "duration_ms": 12000, "draw_fn": draw_anim_yoga},
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

    State is in-memory only. App restart starts the counter fresh — which is
    fine for a cosmetic feature and prevents persistence-based gaming."""

    MIN_SPACING_SEC = 10 * 60
    MIN_COUNT = 4
    MAX_COUNT = 8

    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._last_qualifying_monotonic: float = -float("inf")
        self._qualifying_count: int = 0
        self._target: int = self._pick_target()

    def _pick_target(self) -> int:
        return self._rng.randint(self.MIN_COUNT, self.MAX_COUNT)

    def on_ready_sent(self) -> bool:
        """Call once per outgoing Ready broadcast. Returns True if the caller
        should fire an animation now."""
        now = time.monotonic()
        if now - self._last_qualifying_monotonic < self.MIN_SPACING_SEC:
            return False  # too soon since last qualifying send
        self._last_qualifying_monotonic = now
        self._qualifying_count += 1
        if self._qualifying_count >= self._target:
            self._qualifying_count = 0
            self._target = self._pick_target()
            return True
        return False


# ---------------------------------------------------------------------------
# Convenience for the main app's "fire a random enabled animation" path.
# ---------------------------------------------------------------------------


def pick_random_enabled_animation(
    prefs: Optional[dict] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Return a random animation id from the set that the user has enabled,
    or None if all are disabled (or no prefs file exists and the default
    'all enabled' set is somehow empty)."""
    if prefs is None:
        prefs = load_animation_prefs()
    if rng is None:
        rng = random
    enabled = [
        aid for aid in ANIMATIONS
        if prefs.get(aid, True)   # default to enabled when key missing
    ]
    if not enabled:
        return None
    return rng.choice(enabled)
