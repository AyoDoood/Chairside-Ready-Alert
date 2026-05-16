"""
Stand-alone picker for Chairside Ready Alert's whimsical stick-figure
animations.

Run:
    python3 ready_animation_picker.py

What it does:
  - Lists the 10 animations with an enable/disable checkbox on each.
  - Lets you choose which stick-figure character (plain, wavy hair, top hat,
    mohawk, glasses, beard, bow tie) the main app will use.
  - Plays a preview using the EXACT same code path the main app uses for
    live animations (StickFigureOverlay + AnimationPlayer + draw functions
    imported from ready_animations). No HTML mockup, no separate render —
    what you see in the preview here is what the main app will play after
    each qualifying Ready signal.
  - Reads and writes the same config file the main app uses, so changes
    take effect the next time the main app instantiates its trigger
    (typically the next launch, or immediately if you save while the
    main app is closed).

The preview area is a mock of the main window with a centered Ready button.
The animation overlay floats above that mock window, so emergence/return
positions are anchored to the visible button.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

import ready_animations as ra


# Palette pulled from the main app's "Modern Blue" theme so the picker
# feels like part of the same product line.
THEME = {
    "bg":          "#f0f4ff",
    "card_bg":     "#ffffff",
    "accent":      "#2563eb",
    "accent_text": "#ffffff",
    "title":       "#1e40af",
    "text":        "#1e293b",
    "sub":         "#64748b",
    "border":      "#dde6f5",
}


# -----------------------------------------------------------------------------
# A simple "fake Ready button" that mimics the layout role of the real
# RoundedButton in the main app — gives the overlay a defined center point
# to emerge from and return to. We don't actually need clicks on it; it's a
# positioning anchor.
# -----------------------------------------------------------------------------


class MockReadyButton(tk.Canvas):
    def __init__(self, parent, width=180, height=44):
        super().__init__(
            parent, width=width, height=height,
            bg=THEME["card_bg"], highlightthickness=0, bd=0,
        )
        # Don't use self._w / self._h here — tk.Canvas (via tk.Misc) uses
        # self._w internally as the Tcl widget path. Overwriting it makes
        # every subsequent create_* call fail with
        # "_tkinter.TclError: invalid command name '<width>'".
        self._btn_w, self._btn_h = width, height
        self._draw()

    def _draw(self):
        r = 8
        w, h = self._btn_w, self._btn_h
        pts = [
            r, 0,   w - r, 0,   w, 0,   w, r,
            w, h - r, w, h,   w - r, h, r, h,
            0, h,   0, h - r, 0, r,   0, 0,
        ]
        self.create_polygon(pts, smooth=True, fill=THEME["accent"], outline="")
        self.create_text(
            w // 2, h // 2,
            text="Ready",
            fill=THEME["accent_text"],
            font=("Segoe UI" if sys.platform == "win32" else "Helvetica", 12, "bold"),
        )


# -----------------------------------------------------------------------------
# The picker window itself.
# -----------------------------------------------------------------------------


class PickerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Chairside Ready Alert — Animation Picker")
        root.geometry("980x640")
        root.configure(bg=THEME["bg"])

        # Load saved preferences.
        self._prefs: dict = dict(ra.load_animation_prefs())
        self._character: str = ra.load_animation_character()

        # Track tk variables so we can edit and save.
        self._enabled_vars: dict[str, tk.BooleanVar] = {}
        self._character_var = tk.StringVar(value=self._character)
        self._character_var.trace_add("write", lambda *_: self._on_character_change())

        # Track the currently-running player so we can stop it cleanly.
        self._player: ra.AnimationPlayer | None = None

        self._build_ui()
        # Apply the loaded character to the renderer immediately so previews
        # use it.
        ra.set_character(self._character)

    # ---------- UI construction ----------

    def _build_ui(self) -> None:
        # ttk styling
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel",
                        background=THEME["bg"], foreground=THEME["title"],
                        font=("Helvetica", 16, "bold"))
        style.configure("Sub.TLabel",
                        background=THEME["bg"], foreground=THEME["sub"],
                        font=("Helvetica", 11))
        style.configure("Card.TLabelframe",
                        background=THEME["card_bg"], foreground=THEME["title"])
        style.configure("Card.TLabelframe.Label",
                        background=THEME["card_bg"], foreground=THEME["title"],
                        font=("Helvetica", 11, "bold"))
        style.configure("Card.TFrame", background=THEME["card_bg"])
        style.configure("Card.TLabel",
                        background=THEME["card_bg"], foreground=THEME["text"])
        style.configure("Card.TCheckbutton",
                        background=THEME["card_bg"], foreground=THEME["text"])
        style.configure("Accent.TButton",
                        background=THEME["accent"], foreground=THEME["accent_text"])

        # Header
        header = tk.Frame(self.root, bg=THEME["bg"], padx=20, pady=16)
        header.pack(fill="x")
        ttk.Label(header, text="Animation picker", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Choose your stick figure, enable the animations you want to "
                "see, then click any animation to preview it. The preview "
                "uses the same code the main app does — what you see here is "
                "what the app will play."
            ),
            style="Sub.TLabel",
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # Body: 3-column layout — animations list | character chooser | preview
        body = tk.Frame(self.root, bg=THEME["bg"], padx=20, pady=8)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=0)
        body.grid_columnconfigure(2, weight=1)

        # ----- Column 1: animations list -----
        anims_frame = ttk.LabelFrame(
            body, text="Animations", style="Card.TLabelframe", padding=12,
        )
        anims_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        for anim_id, info in ra.ANIMATIONS.items():
            row = ttk.Frame(anims_frame, style="Card.TFrame")
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=ra.is_animation_enabled(anim_id, self._prefs))
            self._enabled_vars[anim_id] = var
            cb = ttk.Checkbutton(
                row, variable=var, style="Card.TCheckbutton",
                command=lambda aid=anim_id: self._on_enable_toggle(aid),
            )
            cb.pack(side="left")
            btn = tk.Button(
                row,
                text=f"▶  {info['name']}  ·  {info['duration_ms'] / 1000:.0f}s",
                anchor="w",
                relief="flat",
                bg=THEME["card_bg"],
                fg=THEME["text"],
                activebackground=THEME["border"],
                activeforeground=THEME["title"],
                font=("Helvetica", 10),
                command=lambda aid=anim_id: self._play_preview(aid),
                cursor="hand2",
                padx=8,
            )
            btn.pack(side="left", fill="x", expand=True)

        # ----- Column 2: character chooser -----
        char_frame = ttk.LabelFrame(
            body, text="Stick figure", style="Card.TLabelframe", padding=12,
        )
        char_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 12))

        for char_id, info in ra.CHARACTERS.items():
            rb = ttk.Radiobutton(
                char_frame, text=info["name"],
                variable=self._character_var, value=char_id,
                style="Card.TCheckbutton",
            )
            rb.pack(anchor="w", pady=3)

        # Add a "Preview character (uses Surprised animation)" hint
        ttk.Label(
            char_frame,
            text=(
                "Tip: pick a character, then click any animation on the "
                "left to see it with that look."
            ),
            wraplength=200,
            style="Card.TLabel",
            font=("Helvetica", 9, "italic"),
        ).pack(anchor="w", pady=(12, 0))

        # ----- Column 3: preview pane -----
        preview_outer = tk.Frame(body, bg=THEME["bg"])
        preview_outer.grid(row=0, column=2, sticky="nsew")
        ttk.Label(
            preview_outer,
            text="Preview",
            style="Sub.TLabel",
            font=("Helvetica", 11, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        # The preview window is itself a Toplevel that hosts the mock app
        # area + the floating animation overlay. Inline preview in the
        # picker's main window won't work because the overlay is a separate
        # Toplevel and would conflict with the picker chrome on top of it.
        # Instead we use a nested Frame styled like the main app and let the
        # overlay float over the picker window directly.
        self._preview_frame = tk.Frame(
            preview_outer,
            bg=THEME["bg"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )
        self._preview_frame.pack(fill="both", expand=True)

        # A faux "Ready Messages" header at the top of the preview
        inner = tk.Frame(self._preview_frame, bg=THEME["bg"], padx=18, pady=18)
        inner.pack(fill="both", expand=True)

        ttk.Label(
            inner,
            text="Chairside Ready Alert (mock window)",
            background=THEME["bg"],
            foreground=THEME["title"],
            font=("Helvetica", 13, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        # Fake "log" area so the preview pane isn't empty
        log_card = tk.Frame(
            inner,
            bg=THEME["card_bg"],
            highlightbackground=THEME["border"],
            highlightthickness=1,
        )
        log_card.pack(fill="both", expand=True, pady=(0, 12))
        tk.Label(
            log_card,
            text="Ready Messages\n\n[11:42 AM] Room 1: Ready\n[11:48 AM] Room 2: Ready",
            justify="left",
            anchor="nw",
            padx=10, pady=10,
            bg=THEME["card_bg"], fg=THEME["text"],
            font=("Helvetica", 10),
        ).pack(fill="both", expand=True, anchor="nw")

        # Centered mock Ready button — the visual anchor for animations.
        btn_row = tk.Frame(inner, bg=THEME["bg"])
        btn_row.pack(fill="x")
        self._mock_button = MockReadyButton(btn_row)
        self._mock_button.pack(pady=(0, 4))

        # ----- Footer: Save + Close + status -----
        footer = tk.Frame(self.root, bg=THEME["bg"], padx=20, pady=12)
        footer.pack(fill="x")
        self._status_var = tk.StringVar(value="Changes save immediately to the shared config file.")
        ttk.Label(
            footer, textvariable=self._status_var, style="Sub.TLabel",
        ).pack(side="left")
        tk.Button(
            footer, text="Close", command=self.root.destroy,
            relief="flat", bg=THEME["card_bg"], fg=THEME["text"],
            activebackground=THEME["border"], padx=14, pady=4,
            font=("Helvetica", 10, "bold"), cursor="hand2",
        ).pack(side="right")
        tk.Button(
            footer, text="Stop preview", command=self._stop_preview,
            relief="flat", bg=THEME["card_bg"], fg=THEME["text"],
            activebackground=THEME["border"], padx=14, pady=4,
            font=("Helvetica", 10), cursor="hand2",
        ).pack(side="right", padx=(0, 8))

    # ---------- Event handlers ----------

    def _on_enable_toggle(self, anim_id: str) -> None:
        """User clicked the checkbox on an animation row."""
        self._prefs[anim_id] = bool(self._enabled_vars[anim_id].get())
        try:
            ra.save_animation_prefs(self._prefs)
            state = "enabled" if self._prefs[anim_id] else "disabled"
            self._status_var.set(f"{ra.ANIMATIONS[anim_id]['name']} {state}.")
        except OSError as exc:
            self._status_var.set(f"Could not save: {exc}")

    def _on_character_change(self) -> None:
        """User picked a different stick figure."""
        char_id = self._character_var.get()
        if char_id not in ra.CHARACTERS:
            return
        self._character = char_id
        ra.set_character(char_id)
        try:
            ra.save_animation_character(char_id)
            self._status_var.set(f"Character set to {ra.CHARACTERS[char_id]['name']}.")
        except OSError as exc:
            self._status_var.set(f"Could not save: {exc}")

    def _play_preview(self, anim_id: str) -> None:
        """Play the chosen animation as an overlay over the picker window."""
        self._stop_preview()
        # Ensure the renderer is using the currently selected character —
        # save_animation_character may not have flushed yet on Windows.
        ra.set_character(self._character)
        try:
            self._player = ra.play_animation(
                anim_id, self.root, self._mock_button,
                on_complete=self._on_preview_complete,
            )
            if self._player is None:
                self._status_var.set(
                    f"Could not play '{ra.ANIMATIONS[anim_id]['name']}'."
                )
            else:
                self._status_var.set(
                    f"Playing '{ra.ANIMATIONS[anim_id]['name']}'…"
                )
        except Exception as exc:
            self._status_var.set(f"Playback error: {exc}")

    def _on_preview_complete(self) -> None:
        self._player = None
        self._status_var.set("Preview finished.")

    def _stop_preview(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player = None


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


def main() -> None:
    root = tk.Tk()
    PickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
