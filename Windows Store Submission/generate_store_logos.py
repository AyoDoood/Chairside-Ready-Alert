#!/usr/bin/env python3
"""
Generate Microsoft Store logo PNGs from Logo.svg in the sizes Partner Center
accepts. The square logo is rendered directly via cairosvg; non-square
promotional sizes (e.g., 1240x600) are produced by centering the rendered
square logo on a white canvas at the target dimensions.

Usage:
    python3 "Windows Store Submission/generate_store_logos.py"

Output:
    Windows Store Submission/logos/*.png
"""
from __future__ import annotations

import os
import sys
from io import BytesIO

try:
    import cairosvg
except ImportError:
    sys.stderr.write(
        "cairosvg not installed. Run:\n"
        "    python3 -m pip install --user cairosvg pillow\n"
    )
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    sys.stderr.write(
        "Pillow not installed. Run:\n"
        "    python3 -m pip install --user pillow\n"
    )
    sys.exit(1)


# (width, height, filename, partner-center-purpose)
SIZES = [
    (50, 50, "store-logo-50.png", "small icon"),
    (71, 71, "store-logo-71.png", "Store small tile"),
    (150, 150, "store-logo-150.png", "Square 150 / Start Tile"),
    (256, 256, "store-logo-256.png", "general-purpose square"),
    (300, 300, "store-logo-300.png", "Store logo (REQUIRED for Partner Center)"),
    (310, 150, "store-logo-310x150.png", "wide tile"),
    (310, 310, "store-logo-310x310.png", "large tile"),
    (620, 300, "store-promo-620x300.png", "promotional banner"),
    (1080, 1080, "store-promo-1080x1080.png", "high-res square promotional"),
    (1240, 600, "store-hero-1240x600.png", "hero image (Partner Center optional)"),
]

BACKGROUND = (255, 255, 255, 255)  # opaque white — Store-friendly across themes


def render_square_png(svg_path: str, size: int) -> Image.Image:
    """Render the SVG at size×size and return a PIL Image."""
    png_bytes = cairosvg.svg2png(
        url=svg_path, output_width=size, output_height=size
    )
    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def make_canvas(width: int, height: int, logo: Image.Image) -> Image.Image:
    """Center `logo` on a white canvas of (width, height) and return it."""
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    x = (width - logo.width) // 2
    y = (height - logo.height) // 2
    canvas.paste(logo, (x, y), logo)
    return canvas


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, os.pardir))
    src = os.path.join(repo_root, "Logo.svg")
    if not os.path.isfile(src):
        sys.stderr.write(f"Source SVG not found: {src}\n")
        return 1

    out_dir = os.path.join(here, "logos")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Source: {src}")
    print(f"Output: {out_dir}\n")

    for width, height, name, purpose in SIZES:
        target = os.path.join(out_dir, name)
        if width == height:
            img = render_square_png(src, width)
        else:
            # Render the logo at the canvas height so it sits centered
            # on a white background of (width × height).
            logo = render_square_png(src, height)
            img = make_canvas(width, height, logo)
        img.save(target, "PNG", optimize=True)
        print(f"  {width:>4}×{height:<4}  {name:<32}  ({purpose})")

    print(f"\nDone. {len(SIZES)} files in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
