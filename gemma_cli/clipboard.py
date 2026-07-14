#!/usr/bin/env python3
"""
Clipboard image capture for the /paste command.

Uses Pillow's ImageGrab.grabclipboard(), which on Windows returns either an
Image (e.g. after Win+Shift+S / PrtScn) or a list of file paths (after copying
files in Explorer). Both are handled; the image lands in a temp PNG that the
normal vision path consumes.
"""

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def grab_clipboard_image() -> Tuple[Optional[str], Optional[str]]:
    """Return (image_path, error). Exactly one of the two is set."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return None, "Pillow isn't installed. Run: pip install pillow  (then restart gemma)"

    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return None, f"Could not read the clipboard: {e}"

    if data is None:
        return None