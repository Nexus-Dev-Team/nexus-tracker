"""
core/tracker.py
---------------
The core activity tracking engine.

Runs in a single daemon thread and:
  1. Polls the active window every POLL_INTERVAL seconds (default 5 s).
  2. Checks for user idle via GetLastInputInfo - skips DB write when idle.
  3. Resolves the process name and exe path for the active window.
  4. Writes an incremental time record to SQLite on every non-idle tick.
  5. Pre-warms the icon cache in the same thread (low-priority side effect).

CPU impact:
  - 5-second sleep dominates the loop (>99.9% of time sleeping).
  - Exception paths always sleep before continuing - no busy-loops.
  - DB write is a single upsert (one round-trip, WAL mode).

Thread safety:
  TrackerState exposes a lock-protected interface so the UI thread can read
  the current app name and idle/paused status without data races.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import psutil
import pywinctl

from core import icons
from core.idle_detector import is_idle
from storage import db

logger = logging.getLogger(__name__)

# Default poll interval in seconds. Can be overridden via settings.
POLL_INTERVAL = 5


# -- Shared state object -------------------------------------------------------

class TrackerState:
    """
    Thread-safe container for the tracker's observable state.
    The UI thread reads this; the tracker thread writes it.
    """

    def __init__(self) -> None:
        self._lock        = threading.Lock()
        self._current_app: Optional[str] = None
        self._is_idle     = False
        self._is_paused   = False
        self._is_running  = True

    # -- Properties (read from any thread) ------------------------------------

    @property
    def current_app(self) -> Optional[str]:
        with self._lock:
            return self._current_app

    @property
    def is_idle(self) -> bool:
        with self._lock:
            return self._is_idle

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._is_paused

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    # -- Mutators (called from tracker thread or UI thread) --------------------

    def update(self, app: Optional[str], idle: bool) -> None:
        with self._lock:
            self._current_app = app
            self._is_idle     = idle

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._is_paused = paused

    def stop(self) -> None:
        with self._lock:
            self._is_running = False


# -- Tracker -------------------------------------------------------------------

class ActivityTracker:
    """
    Manages the background tracking thread.

    Usage:
        tracker = ActivityTracker(settings)
        tracker.start()
        # ... app runs ...
        tracker.stop()
    """

    def __init__(self, settings: dict, on_tick: Optional[Callable] = None) -> None:
        """
        Args:
            settings: Application settings dict (idle_threshold_seconds,
                      poll_interval_seconds).
            on_tick:  Optional zero-argument callback invoked after each
                      successful DB write - used to trigger a UI refresh.
        """
        self.settings = settings
        self.on_tick  = on_tick
        self.state    = TrackerState()

        self._thread: Optional[threading.Thread] = None

        # Cached per-day DB id - avoids repeated "SELECT id FROM days" queries
        self._current_date_str: Optional[str] = None
        self._current_day_id:   Optional[int] = None

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon tracking thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="NexusTracker"
        )
        self._thread.start()
        logger.info("Activity tracker started (poll interval: %s s)",
                    self.settings.get("poll_interval_seconds", POLL_INTERVAL))

    def stop(self) -> None:
        """Signal the tracking thread to exit on its next iteration."""
        self.state.stop()
        logger.info("Activity tracker stop requested.")

    # -- Internal helpers ------------------------------------------------------

    def _day_id(self, date_str: str) -> int:
        """Return the DB day_id for *date_str*, creating a new row if needed."""
        if date_str != self._current_date_str:
            self._current_date_str = date_str
            self._current_day_id   = db.get_or_create_day(date_str)
            logger.debug("Switched to day_id=%s (%s)", self._current_day_id, date_str)
        return self._current_day_id  # type: ignore[return-value]

    def _sleep_for_remainder(self, tick_start: float) -> None:
        """Sleep for whatever time remains in the current poll interval."""
        interval  = self.settings.get("poll_interval_seconds", POLL_INTERVAL)
        elapsed   = time.monotonic() - tick_start
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # -- Main loop -------------------------------------------------------------

    def _run(self) -> None:
        """
        Core polling loop. Runs until state.is_running becomes False.

        Key invariant: *every* code path through the loop calls
        _sleep_for_remainder() before the next iteration, so we never
        accidentally spin on CPU.
        """
        logger.info("Tracker thread running.")

        while self.state.is_running:
            tick_start = time.monotonic()

            # -- Paused by user ------------------------------------------------
            if self.state.is_paused:
                self.state.update(app=None, idle=False)
                self._sleep_for_remainder(tick_start)
                continue

            # -- Idle check ----------------------------------------------------
            idle_threshold = self.settings.get("idle_threshold_seconds", 300)
            if is_idle(idle_threshold):
                self.state.update(app=None, idle=True)
                self._sleep_for_remainder(tick_start)
                continue

            # -- Get active window ---------------------------------------------
            process_name: Optional[str] = None
            exe_path:     Optional[str] = None

            try:
                active_window = pywinctl.getActiveWindow()
                if active_window is not None:
                    pid  = active_window.getPID()
                    proc = psutil.Process(pid)
                    process_name = proc.name()
                    try:
                        exe_path = proc.exe()
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        exe_path = None  # Not critical - icon just uses default

            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, AttributeError, OSError) as exc:
                logger.debug("Active window lookup failed: %s", exc)
                self.state.update(app=None, idle=False)
                self._sleep_for_remainder(tick_start)   # always sleep on error
                continue

            # -- Update shared state for UI ------------------------------------
            self.state.update(app=process_name, idle=False)

            # -- Write to DB ---------------------------------------------------
            if process_name:
                # Use current date (handles midnight crossover naturally)
                date_str = datetime.now().strftime("%Y-%m-%d")
                day_id   = self._day_id(date_str)

                interval = self.settings.get("poll_interval_seconds", POLL_INTERVAL)

                try:
                    db.upsert_app_time(
                        day_id       = day_id,
                        process_name = process_name,
                        exe_path     = exe_path,
                        seconds_to_add = interval,
                    )
                except Exception as exc:
                    logger.error("DB write error: %s", exc)

                # Pre-warm icon cache (non-critical - errors are silently ignored)
                if exe_path:
                    try:
                        icons.get_icon(exe_path)
                    except Exception:
                        pass

            # -- Notify UI (optional callback) ---------------------------------
            if self.on_tick:
                try:
                    self.on_tick()
                except Exception as exc:
                    logger.debug("on_tick callback raised: %s", exc)

            self._sleep_for_remainder(tick_start)

        logger.info("Tracker thread exited.")
