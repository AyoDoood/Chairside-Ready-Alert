# Third-Party Notices

This project includes or depends on third-party software. License obligations should be reviewed before commercial distribution.

## Project-Owned Assets

- `Logo.svg` - custom project logo used for app/tray icon generation.
- Program-generated alert sounds are synthesized at runtime from code-defined frequencies; no third-party audio files are bundled.

## Python Dependencies Used by the App/Installers

- Python (PSF License)
- Tk/Tkinter (Tcl/Tk license via Python distribution)
- `pystray` (LGPL-3.0)
- `Pillow` (HPND-style Pillow license)
- `cairosvg` (LGPL-3.0)
- `certifi` (MPL-2.0)

## Packaging/Build Tools

- `PyInstaller` (GPL-2.0-or-later with bootloader exception)

## Full license texts

Full license texts, copyright notices, and source-availability statements
for each component above are reproduced in `licenses/THIRD_PARTY_LICENSES.txt`.
That file is bundled next to the EXE in Windows Store builds so users have
direct access to the notices required by the LGPL-3.0 and MPL-2.0 licenses.

## Maintainer Notes

1. Keep this file updated when adding/removing dependencies.
2. Keep attribution/license texts as required by each dependency.
3. Confirm that all custom assets (logo/icons/branding) are owned by the publisher or properly licensed.
4. If any third-party image/audio/font is added in the future, document origin and license here before release.
