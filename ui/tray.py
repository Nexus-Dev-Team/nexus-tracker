"""
ui/tray.py
----------
System tray icon integration using pystray.

The tray icon runs in its own daemon thread so it never blocks the Tk main
loop. Communication back to the UI (show window, pause, quit) is done via a
thread-safe queue that the Tk thread polls with root.after().
"""

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

APP_NAME = "Nexus Tracker"


def _build_tray_image(icon_path: Optional[Path] = None) -> "Image.Image":  # type: ignore[name-defined]
    """
    Return the tray icon image.
    - If *icon_path* exists, load it from disk (PNG, ICO, etc.).
    - Otherwise fall back to the built-in drawn clock-face icon.
    Returns a PIL Image suitable for pystray.
    """
    from PIL import Image

    if icon_path and icon_path.exists():
        try:
            img = Image.open(icon_path).convert("RGBA")
            img = img.resize((64, 64))
            return img
        except Exception as exc:
            logger.warning("Could not load custom tray icon (%s): %s", icon_path, exc)

    # Built-in fallback
    from PIL import ImageDraw
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer circle - brand blue
    draw.ellipse([1, 1, size - 1, size - 1], fill=(74, 120, 220))
    # Inner lighter ring for depth
    draw.ellipse([4, 4, size - 4, size - 4], fill=(90, 140, 240))
    # Inner face
    draw.ellipse([8, 8, size - 8, size - 8], fill=(30, 40, 65))

    # Clock hands
    cx, cy = size // 2, size // 2
    draw.line([cx, cy, cx,      cy - 16], fill="white", width=3)
    draw.line([cx, cy, cx + 12, cy + 6],  fill="white", width=2)
    draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill="white")

    return img


class SystemTray:
    """
    Manages the system tray icon and its context menu.

    Menu items:
      - Open     - show the main window
      - Pause    - toggle tracking on/off (checkmark)
      -----------------
      - Quit     - full application exit
    """

    def __init__(
        self,
        on_open:    Callable[[], None],
        on_quit:    Callable[[], None],
        on_pause:   Callable[[bool], None],
        icon_path:  Optional[Path] = None,
    ) -> None:
        """
        Args:
            on_open:   Called when the user clicks "Open".
            on_quit:   Called when the user clicks "Quit".
            on_pause:  Called with new paused state when "Pause Tracking" is toggled.
            icon_path: Optional path to a custom icon file (PNG or ICO).
                       If the file does not exist the built-in icon is used.
        """
        self._on_open   = on_open
        self._on_quit   = on_quit
        self._on_pause  = on_pause
        self._icon_path = icon_path

        self._icon:   Optional["pystray.Icon"] = None  # type: ignore[name-defined]
        self._paused: bool = False

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start the tray icon in a background daemon thread."""
        thread = threading.Thread(target=self._run, daemon=True, name="SystemTray")
        thread.start()

    def stop(self) -> None:
        """Stop the tray icon (call from any thread)."""
        if self._icon is not None:
            self._icon.stop()

    def set_tooltip(self, text: str) -> None:
        """Update the hover tooltip on the tray icon."""
        if self._icon is not None:
            self._icon.title = text

    # -- Internal --------------------------------------------------------------

    def _run(self) -> None:
        """Entry point for the tray thread - blocks until stop() is called."""
        try:
            import pystray
            from pystray import Icon, Menu, MenuItem

            tray_img = _build_tray_image(self._icon_path)

            self._icon = Icon(
                name  = "NexusTracker",
                icon  = tray_img,
                title = APP_NAME,
                menu  = Menu(
                    MenuItem(f"Open {APP_NAME}", self._handle_open, default=True),
                    MenuItem(
                        "Pause Tracking",
                        self._handle_pause_toggle,
                        checked=lambda _: self._paused,
                    ),
                    Menu.SEPARATOR,
                    MenuItem("Quit", self._handle_quit),
                ),
            )

            logger.info("System tray icon running.")
            self._icon.run()

        except Exception as exc:
            logger.error("Tray thread crashed: %s", exc)

    def _handle_open(self, icon, item) -> None:  # noqa: ARG002
        self._on_open()

    def _handle_pause_toggle(self, icon, item) -> None:  # noqa: ARG002
        self._paused = not self._paused
        self._on_pause(self._paused)

    def _handle_quit(self, icon, item) -> None:  # noqa: ARG002
        icon.stop()
        self._on_quit()
