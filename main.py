"""
main.py
-------
Nexus Tracker - entry point.

Start-up sequence:
  1. Configure logging (file + console).
  2. Load settings from config/settings.json.
  3. Initialise the SQLite database.
  4. Create the ActivityTracker (not started yet).
  5. Create the MainWindow (builds the Tk root widget).
  6. Create the SystemTray (runs in its own daemon thread).
  7. Wire up a thread-safe command queue so tray callbacks safely mutate
     the UI on the Tk thread using root.after().
  8. Apply autostart registration if enabled.
  9. Start the tracker thread and the tray thread.
 10. Enter the Tk main loop - blocks until quit.

Thread map:
  Main thread       - Tk event loop (all widget reads/writes happen here)
  NexusTracker thread - polls active window, writes to SQLite
  SystemTray thread   - runs pystray blocking loop
"""

import json
import logging
import queue
import sys
from pathlib import Path

# -- Logging setup (before any imports that may log) --------------------------
_LOG_DIR = Path.home() / "AppData" / "Roaming" / "NexusTracker"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-20s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# -- Application imports (after logging is configured) ------------------------
from core.tracker import ActivityTracker
from storage import db
from ui.main_window import MainWindow
from ui.tray import SystemTray
import autostart as autostart_mod


# -- Icon path -----------------------------------------------------------------
# Place your icon file at assets/icon.ico (or icon.png) next to main.py.
# If the file does not exist the app runs with no custom icon (no error).
ICON_PATH = Path(__file__).parent / "assets" / "icon.ico"


# -- Settings loader -----------------------------------------------------------

_SETTINGS_PATH = Path(__file__).parent / "config" / "settings.json"

def load_settings() -> dict:
    """Read settings.json; return safe defaults if the file is missing/corrupt."""
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load settings (%s); using defaults.", exc)
        return {
            "idle_threshold_seconds": 300,
            "poll_interval_seconds":  5,
            "autostart":              True,
        }


# -- Main ----------------------------------------------------------------------

def main() -> None:
    logger.info("Nexus Tracker starting up.")

    # -- 1. Settings ----------------------------------------------------------
    settings = load_settings()

    # -- 2. Database ----------------------------------------------------------
    db.init_db()

    # -- 3. Tracker -----------------------------------------------------------
    tracker = ActivityTracker(settings=settings)

    # -- 4. Thread-safe command queue -----------------------------------------
    # Tray callbacks run on the pystray thread; they push commands here.
    # The Tk thread drains this queue via root.after() polling.
    ui_queue: queue.Queue = queue.Queue()

    # -- 5. Main window -------------------------------------------------------
    window = MainWindow(
        tracker   = tracker,
        settings  = settings,
        on_quit   = lambda: ui_queue.put(("quit",)),
        icon_path = ICON_PATH,
    )

    # -- 6. System tray -------------------------------------------------------
    tray = SystemTray(
        on_open   = lambda: ui_queue.put(("show",)),
        on_quit   = lambda: ui_queue.put(("quit",)),
        on_pause  = lambda paused: ui_queue.put(("pause", paused)),
        icon_path = ICON_PATH,
    )

    # -- 7. Queue drain loop (runs on Tk main thread via root.after) ----------
    def drain_ui_queue() -> None:
        """Process all pending tray commands on the Tk thread."""
        try:
            while True:
                cmd = ui_queue.get_nowait()

                if cmd[0] == "show":
                    window.show()

                elif cmd[0] == "quit":
                    logger.info("Quit requested - shutting down.")
                    tracker.stop()
                    tray.stop()
                    window.quit()
                    return  # Do NOT reschedule after quitting

                elif cmd[0] == "pause":
                    paused = cmd[1]
                    tracker.state.set_paused(paused)
                    window.set_paused(paused)
                    tray.set_tooltip(
                        "Nexus Tracker (Paused)" if paused else "Nexus Tracker"
                    )

        except queue.Empty:
            pass

        # Reschedule - 100 ms keeps the tray responsive without wasting CPU
        window.root.after(100, drain_ui_queue)

    window.root.after(100, drain_ui_queue)

    # -- 8. Autostart ---------------------------------------------------------
    if settings.get("autostart", True) and not autostart_mod.is_autostart_enabled():
        autostart_mod.enable_autostart()

    # -- 9. Start threads -----------------------------------------------------
    tracker.start()
    tray.start()

    # -- 10. Tk main loop -----------------------------------------------------
    logger.info("Entering Tk main loop.")
    window.run()

    logger.info("Nexus Tracker shut down cleanly.")


if __name__ == "__main__":
    main()