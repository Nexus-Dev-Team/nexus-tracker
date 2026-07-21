"""
core/icons.py
-------------
Extract and cache application icons from .exe files.

Strategy:
1. Check the on-disk PNG cache first (keyed by MD5 of exe path).
2. If not cached, extract the first icon resource from the .exe via Win32 API
   (win32gui.ExtractIconEx + GDI drawing into a memory DC).
3. Cache the result as a PNG for future calls.
4. On any failure, return a generic default icon drawn with Pillow.

The cache lives at %APPDATA%\\NexusTracker\\icon_cache\\ so it persists across
app restarts without re-extraction overhead.
"""

import hashlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ICON_SIZE = 32
CACHE_DIR = Path.home() / "AppData" / "Roaming" / "NexusTracker" / "icon_cache"

# Singleton default icon - created once on first call
_default_icon: Optional["Image.Image"] = None  # type: ignore[name-defined]


# -- Cache helpers -------------------------------------------------------------

def _cache_path(exe_path: str) -> Path:
    """Return the PNG cache file path for a given exe path."""
    key = hashlib.md5(exe_path.encode("utf-8", errors="replace")).hexdigest()
    return CACHE_DIR / f"{key}.png"


# -- Default icon --------------------------------------------------------------

def get_default_icon() -> "Image.Image":  # type: ignore[name-defined]
    """Return (or create) a simple default app icon as a PIL Image."""
    global _default_icon
    if _default_icon is not None:
        return _default_icon

    from PIL import Image, ImageDraw

    img  = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle background
    draw.rounded_rectangle(
        [2, 2, ICON_SIZE - 2, ICON_SIZE - 2],
        radius=5,
        fill=(80, 90, 115),
        outline=(120, 130, 155),
        width=1,
    )
    # Tiny "window" shape inside to suggest an application
    draw.rectangle([7, 9,  ICON_SIZE - 7, 13], fill=(160, 170, 195))   # title bar
    draw.rectangle([7, 15, ICON_SIZE - 7, ICON_SIZE - 7], fill=(110, 120, 145))  # body

    _default_icon = img
    return _default_icon


# -- Extraction ----------------------------------------------------------------

def _extract_from_exe(exe_path: str) -> Optional["Image.Image"]:  # type: ignore[name-defined]
    """
    Extract the first icon from *exe_path* using the Win32 GDI API via pywin32.

    Returns a 32×32 RGBA PIL Image, or None if extraction fails for any reason.
    """
    try:
        import win32gui
        import win32ui
        import win32con
        from PIL import Image

        # Grab icon handle(s); ExtractIconEx returns ([large...], [small...])
        large_handles, small_handles = win32gui.ExtractIconEx(exe_path, 0)
        all_handles = list(large_handles or []) + list(small_handles or [])
        if not all_handles:
            return None

        hicon = all_handles[0]
        size  = ICON_SIZE

        # Obtain a screen DC to create compatible objects
        raw_hdc = win32gui.GetDC(None)
        dc      = win32ui.CreateDCFromHandle(raw_hdc)   # wraps but does NOT own handle
        mem_dc  = dc.CreateCompatibleDC()               # memory DC owned by mem_dc

        # Compatible bitmap to render into
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, size, size)
        mem_dc.SelectObject(bmp)

        # White background so semi-transparent icons look clean
        mem_dc.FillSolidRect((0, 0, size, size), 0x00FFFFFF)

        # Render icon at the requested size
        win32gui.DrawIconEx(
            mem_dc.GetHandleOutput(), 0, 0, hicon,
            size, size, 0, None, win32con.DI_NORMAL,
        )

        # Pull raw pixel bytes out of the bitmap
        bmp_info = bmp.GetInfo()
        bmp_bits = bmp.GetBitmapBits(True)

        img = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits, "raw", "BGRX", 0, 1,
        )

        # Cleanup GDI objects (order matters: mem_dc before releasing screen DC)
        mem_dc.DeleteDC()
        win32gui.ReleaseDC(None, raw_hdc)

        for h in all_handles:
            try:
                win32gui.DestroyIcon(h)
            except Exception:
                pass

        return img.resize((size, size)).convert("RGBA")

    except Exception as e:
        logger.debug("Icon extraction failed for %s: %s", exe_path, e)
        return None


# -- Public API ----------------------------------------------------------------

def get_icon(exe_path: Optional[str]) -> "Image.Image":  # type: ignore[name-defined]
    """
    Return a 32×32 RGBA PIL Image for *exe_path*.

    Lookup order:
        1. On-disk PNG cache  → instant
        2. Win32 extraction   → ~5 ms, then cached
        3. Default icon       → fallback, never fails
    """
    if not exe_path:
        return get_default_icon()

    cache = _cache_path(exe_path)

    # --- 1. Try cache ---
    if cache.exists():
        try:
            from PIL import Image
            return Image.open(cache).convert("RGBA")
        except Exception:
            pass  # Corrupt cache file - fall through to re-extract

    # --- 2. Extract from exe ---
    img = _extract_from_exe(exe_path)

    if img is None:
        return get_default_icon()

    # --- 3. Persist to cache ---
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        img.save(cache, "PNG")
    except Exception as e:
        logger.debug("Failed to cache icon for %s: %s", exe_path, e)

    return img
