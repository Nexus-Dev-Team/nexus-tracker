"""
ui/main_window.py
-----------------
CustomTkinter main application window.

Layout (top → bottom):
  ┌------------------------------------------------------┐
  │  Header: title  |  status badge  |  settings button  │  row 0
  ├------------------------------------------------------┤
  │  [Productivity Card]    [Total Time Card]            │  row 1
  ├------------------------------------------------------┤
  │  Filter: [Today][Yesterday][Week][Custom]  [↑ date]  │  row 2
  ├------------------------------------------------------┤
  │  Scrollable app table                                │  row 3 (weight=1)
  └------------------------------------------------------┘

Threading model:
  - The Tk main loop runs on the main thread.
  - The tracker thread writes to SQLite, not to Tk widgets.
  - root.after(10_000, _refresh) pulls fresh data from SQLite and updates
    widgets - all on the Tk thread, so no locking is needed in the UI.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox
from typing import Callable, List, Optional

import customtkinter as ctk

from core.productivity import format_seconds
from core import icons
from storage import db
from storage.models import AppSession, DaySummary
import autostart as autostart_mod

logger = logging.getLogger(__name__)

# -- Design tokens -------------------------------------------------------------
FONT        = "Segoe UI"
BG          = "#12162a"       # deepest background
SURFACE     = "#1c2140"       # card / panel background
SURFACE2    = "#252c52"       # row alternate / hover
ACCENT      = "#5b8dee"       # primary blue
TEXT_PRI    = "#e8eaf6"       # primary text
TEXT_SEC    = "#7e8bb5"       # muted / label text
GREEN       = "#4ade80"
YELLOW      = "#facc15"
RED         = "#f87171"


# Path to persist settings (two levels up from this file → project root/config)
_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.json"


# -- Helpers -------------------------------------------------------------------


def save_settings(settings: dict) -> None:
    """Persist the settings dict to config/settings.json."""
    try:
        _SETTINGS_PATH.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.error("Failed to save settings: %s", exc)


# -- Settings dialog -----------------------------------------------------------

class SettingsDialog:
    """
    Modal dialog for user-adjustable settings:
      - Autostart with Windows toggle
    """

    def __init__(self, parent: ctk.CTk, settings: dict) -> None:
        self.settings = settings

        self.win = ctk.CTkToplevel(parent)
        self.win.title("Settings")
        self.win.geometry("440x240")
        self.win.resizable(False, False)
        self.win.configure(fg_color=BG)
        self.win.transient(parent)
        self.win.grab_set()          # block interaction with main window
        self.win.focus_set()

        self._build()

    def _build(self) -> None:
        pad = {"padx": 28, "pady": 8}

        # -- Title ----------------------------------------------------------
        ctk.CTkLabel(
            self.win, text="Settings",
            font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=TEXT_PRI,
        ).pack(anchor="w", padx=28, pady=(24, 4))

        divider(self.win).pack(fill="x", padx=28, pady=(0, 12))

        frame = ctk.CTkFrame(self.win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=28)
        frame.columnconfigure(1, weight=1)

        row = 0

        # -- Autostart ------------------------------------------------------
        ctk.CTkLabel(frame, text="Start with Windows:",
                     font=ctk.CTkFont(family=FONT, size=13),
                     text_color=TEXT_SEC).grid(
            row=row, column=0, sticky="w", **pad)

        self._autostart = ctk.CTkSwitch(
            frame, text="",
            fg_color=SURFACE2, progress_color=ACCENT,
            button_color=TEXT_PRI, button_hover_color=ACCENT,
        )
        if autostart_mod.is_autostart_enabled():
            self._autostart.select()
        self._autostart.grid(row=row, column=1, sticky="w", padx=8, pady=8)
        row += 1

        # -- Buttons --------------------------------------------------------
        divider(self.win).pack(fill="x", padx=28, pady=(8, 0))

        btn_row = ctk.CTkFrame(self.win, fg_color="transparent")
        btn_row.pack(anchor="e", padx=28, pady=16)

        ctk.CTkButton(
            btn_row, text="Cancel", width=90, height=34,
            fg_color=SURFACE2, hover_color=SURFACE,
            text_color=TEXT_SEC, font=ctk.CTkFont(family=FONT, size=13),
            command=self.win.destroy,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row, text="Save", width=90, height=34,
            fg_color=ACCENT, hover_color="#4a79d4",
            text_color="white", font=ctk.CTkFont(family=FONT, size=13, weight="bold"),
            command=self._save,
        ).pack(side="left")

    def _save(self) -> None:
        """Persist settings and close the dialog."""
        self.settings["autostart"] = bool(self._autostart.get())


        # Apply autostart immediately
        if self._autostart.get():
            autostart_mod.enable_autostart()
        else:
            autostart_mod.disable_autostart()

        save_settings(self.settings)
        logger.info("Settings saved.")
        self.win.destroy()


# -- Small layout helpers ------------------------------------------------------

def divider(parent) -> ctk.CTkFrame:
    """Return a 1-pixel horizontal rule."""
    return ctk.CTkFrame(parent, fg_color=SURFACE2, height=1, corner_radius=0)


def label(parent, text: str, size: int = 13, weight: str = "normal",
          color: str = TEXT_PRI, **kwargs) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent, text=text,
        font=ctk.CTkFont(family=FONT, size=size, weight=weight),
        text_color=color, **kwargs,
    )


# -- Main window ---------------------------------------------------------------

class MainWindow:
    """
    Primary application window. Created on the main thread; all widget
    interactions happen on the main thread via root.after().
    """

    def __init__(
        self,
        tracker,
        settings:  dict,
        on_quit:   Callable[[], None],
        icon_path: Optional["Path"] = None,
    ) -> None:
        """
        Args:
            tracker:   ActivityTracker instance (read its .state for status badges).
            settings:  Shared settings dict (mutated on save).
            on_quit:   Called when the user chooses Quit from the tray menu.
            icon_path: Optional path to a .ico or .png file used as the window icon.
        """
        self.tracker   = tracker
        self.settings  = settings
        self._on_quit  = on_quit
        self._icon_path = icon_path

        # View state
        self._filter:       str           = "today"   # today|yesterday|week|custom
        self._custom_start: Optional[date] = None
        self._custom_end:   Optional[date] = None
        self._paused:       bool           = False
        self._icon_cache:   list           = []        # keep CTkImage refs alive

        # -- Bootstrap CustomTkinter ---------------------------------------
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("Nexus Tracker")
        self.root.geometry("820x640")
        self.root.minsize(700, 520)
        self.root.configure(fg_color=BG)

        # Apply custom window icon if provided
        if icon_path and icon_path.exists():
            try:
                suffix = icon_path.suffix.lower()
                if suffix == ".ico":
                    self.root.iconbitmap(str(icon_path))
                else:
                    from PIL import Image as _Img
                    import tkinter as _tk
                    _photo = _tk.PhotoImage(file=str(icon_path))
                    self.root.iconphoto(True, _photo)
            except Exception as exc:
                logger.warning("Could not set window icon: %s", exc)

        # Closing the window hides it to tray - real quit is from tray menu
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        # Grid: col 0 stretches; rows 0-2 fixed height, row 3 fills remaining space.
        # Row 3 may become the custom-date bar when "Custom" filter is selected,
        # pushing the table to row 4. We handle this by storing _table_container.
        self.root.columnconfigure(0, weight=1)
        for r in range(4):
            self.root.rowconfigure(r, weight=0)
        self.root.rowconfigure(3, weight=1)

        self._table_container: Optional[ctk.CTkFrame] = None  # set by _build_table

        self._build_header()
        self._build_cards()
        self._build_filter_bar()
        self._build_table()

        # Kick off the periodic UI refresh (runs on Tk thread via after())
        self.root.after(500, self._refresh)

    # ═════════════════════════════════════════════════════════════════════════
    # UI construction
    # ═════════════════════════════════════════════════════════════════════════

    def _build_header(self) -> None:
        """Top bar: app title, live status badge, settings button."""
        hdr = ctk.CTkFrame(self.root, fg_color=SURFACE, height=58, corner_radius=0)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(1, weight=1)
        hdr.grid_propagate(False)

        # Title
        label(hdr, "⏱  Nexus Tracker", size=17, weight="bold").grid(
            row=0, column=0, padx=20, pady=16, sticky="w"
        )

        # Status badge - updated every refresh cycle
        self._status = ctk.CTkLabel(
            hdr, text="● Starting...",
            font=ctk.CTkFont(family=FONT, size=11, weight="bold"),
            text_color=YELLOW,
            fg_color=SURFACE2, corner_radius=8,
            padx=10, pady=3,
        )
        self._status.grid(row=0, column=1, padx=8, sticky="e")

        # Settings button
        ctk.CTkButton(
            hdr, text="⚙  Settings", width=110, height=32,
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color="transparent", hover_color=SURFACE2,
            text_color=TEXT_SEC,
            border_width=1, border_color=SURFACE2, corner_radius=8,
            command=self._open_settings,
        ).grid(row=0, column=2, padx=16, pady=12, sticky="e")

    def _build_cards(self) -> None:
        """Single summary card: Total Time Tracked."""
        outer = ctk.CTkFrame(self.root, fg_color=BG)
        outer.grid(row=1, column=0, sticky="ew", padx=16, pady=(14, 0))
        outer.columnconfigure(0, weight=1)

        # -- Total time card ------------------------------------------------
        time_card = ctk.CTkFrame(outer, fg_color=SURFACE, corner_radius=14)
        time_card.grid(row=0, column=0, sticky="nsew")
        time_card.columnconfigure(0, weight=1)

        label(time_card, "TOTAL TIME TRACKED", size=10, weight="bold",
              color=TEXT_SEC).grid(row=0, column=0, padx=18, pady=(16, 2), sticky="w")

        self._total_lbl = label(time_card, "0h 00m", size=34, weight="bold")
        self._total_lbl.grid(row=1, column=0, padx=18, pady=(0, 18), sticky="w")

    def _build_filter_bar(self) -> None:
        """Date-range filter bar with optional custom date fields."""
        bar = ctk.CTkFrame(self.root, fg_color=BG)
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(12, 0))
        bar.columnconfigure(1, weight=1)

        label(bar, "Show:", size=12, color=TEXT_SEC).grid(
            row=0, column=0, padx=(0, 10), pady=6, sticky="w"
        )

        self._seg = ctk.CTkSegmentedButton(
            bar,
            values=["Today", "Yesterday", "This Week", "Custom"],
            command=self._on_filter,
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color=SURFACE,
            selected_color=ACCENT,
            selected_hover_color="#4a79d4",
            unselected_color=SURFACE,
            unselected_hover_color=SURFACE2,
            text_color=TEXT_PRI,
            text_color_disabled=TEXT_SEC,
            corner_radius=8,
        )
        self._seg.set("Today")
        self._seg.grid(row=0, column=1, pady=6, sticky="w")

        # -- Custom date row (hidden until "Custom" is selected) ------------
        self._custom_bar = ctk.CTkFrame(self.root, fg_color=BG)
        # Not .grid()'d initially - shown by _on_filter()

        label(self._custom_bar, "From:", size=12, color=TEXT_SEC).pack(
            side="left", padx=(0, 4))

        self._start_entry = ctk.CTkEntry(
            self._custom_bar, width=115, placeholder_text="YYYY-MM-DD",
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color=SURFACE, border_color=SURFACE2, text_color=TEXT_PRI,
        )
        self._start_entry.pack(side="left", padx=(0, 12))

        label(self._custom_bar, "To:", size=12, color=TEXT_SEC).pack(
            side="left", padx=(0, 4))

        self._end_entry = ctk.CTkEntry(
            self._custom_bar, width=115, placeholder_text="YYYY-MM-DD",
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color=SURFACE, border_color=SURFACE2, text_color=TEXT_PRI,
        )
        self._end_entry.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            self._custom_bar, text="Apply", width=72, height=30,
            font=ctk.CTkFont(family=FONT, size=12),
            fg_color=ACCENT, hover_color="#4a79d4",
            command=self._apply_custom,
        ).pack(side="left")

    def _build_table(self) -> None:
        """App usage table: scrollable rows with icon, name, time."""
        container = ctk.CTkFrame(self.root, fg_color=SURFACE, corner_radius=14)
        container.grid(row=3, column=0, sticky="nsew", padx=16, pady=12)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)
        self._table_container = container  # saved for filter re-grid

        # -- Column headers -------------------------------------------------
        hdr = ctk.CTkFrame(container, fg_color=SURFACE, corner_radius=0)
        hdr.grid(row=0, column=0, sticky="ew", padx=0)
        hdr.columnconfigure(1, weight=1)

        for col, (txt, w, anchor) in enumerate([
            ("",            46,   "center"),  # icon placeholder
            ("Application",  0,   "w"),        # stretches
            ("Time Used",   100,  "center"),
        ]):
            ctk.CTkLabel(
                hdr, text=txt,
                font=ctk.CTkFont(family=FONT, size=11, weight="bold"),
                text_color=TEXT_SEC,
                width=w or 0,
                anchor=anchor,
            ).grid(row=0, column=col,
                   padx=(10 if col == 0 else 4, 14 if col == 2 else 4),
                   pady=10, sticky="ew" if col == 1 else "")
        hdr.columnconfigure(1, weight=1)

        divider(container).grid(row=1, column=0, sticky="ew")

        # -- Scrollable rows ------------------------------------------------
        self._scroll = ctk.CTkScrollableFrame(
            container, fg_color=SURFACE, corner_radius=0,
            scrollbar_button_color=SURFACE2,
            scrollbar_button_hover_color=ACCENT,
        )
        self._scroll.grid(row=2, column=0, sticky="nsew")
        self._scroll.columnconfigure(1, weight=1)
        container.rowconfigure(2, weight=1)

    # ═════════════════════════════════════════════════════════════════════════
    # Data refresh (runs on Tk thread via root.after)
    # ═════════════════════════════════════════════════════════════════════════

    def _refresh(self) -> None:
        """Pull fresh data from SQLite and update all widgets."""
        try:
            summary = self._query_summary()
            self._update_cards(summary)
            self._update_table(summary.sessions)
            self._update_status()
        except Exception as exc:
            logger.error("UI refresh error: %s", exc)

        # Schedule next refresh
        self.root.after(10_000, self._refresh)

    def _query_summary(self) -> DaySummary:
        """Determine which date range to query based on current filter."""
        today = date.today()

        if self._filter == "today":
            return db.get_day_summary(today.isoformat())
        elif self._filter == "yesterday":
            return db.get_day_summary((today - timedelta(days=1)).isoformat())
        elif self._filter == "week":
            monday = today - timedelta(days=today.weekday())
            return db.get_range_summary(monday, today)
        elif self._filter == "custom" and self._custom_start and self._custom_end:
            return db.get_range_summary(self._custom_start, self._custom_end)
        else:
            return db.get_day_summary(today.isoformat())

    def _update_cards(self, summary: DaySummary) -> None:
        """Refresh the total time card."""
        total = summary.total_seconds
        self._total_lbl.configure(text=format_seconds(total) if total else "0s")

    def _update_table(self, sessions: List[AppSession]) -> None:
        """Destroy and rebuild all rows in the scrollable table."""
        # Clear old rows and icon references
        for widget in self._scroll.winfo_children():
            widget.destroy()
        self._icon_cache.clear()

        if not sessions:
            label(
                self._scroll,
                "No activity recorded for this period.\n"
                "Make sure the tracker is running and you are not idle.",
                size=13, color=TEXT_SEC,
            ).grid(row=0, column=0, columnspan=3, pady=50)
            return

        self._scroll.columnconfigure(1, weight=1)

        for i, session in enumerate(sessions):
            self._add_row(i, session)

    def _add_row(self, idx: int, session: AppSession) -> None:
        """Add one app row to the scrollable table."""
        row_bg = BG if idx % 2 == 0 else SURFACE

        row = ctk.CTkFrame(self._scroll, fg_color=row_bg, corner_radius=0, height=46)
        row.grid(row=idx, column=0, columnspan=3, sticky="ew")
        row.columnconfigure(1, weight=1)
        row.grid_propagate(False)

        # -- Icon ----------------------------------------------------------
        try:
            pil_img = icons.get_icon(session.exe_path)
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(26, 26))
            self._icon_cache.append(ctk_img)  # prevent garbage collection
            icon_widget = ctk.CTkLabel(row, image=ctk_img, text="", width=46)
        except Exception:
            icon_widget = ctk.CTkLabel(
                row, text="□", width=46,
                font=ctk.CTkFont(size=18), text_color=TEXT_SEC,
            )
        icon_widget.grid(row=0, column=0, padx=(8, 0), pady=10)

        # -- Process name ---------------------------------------------------
        label(row, session.process_name, size=13, anchor="w").grid(
            row=0, column=1, padx=(6, 4), pady=10, sticky="ew"
        )

        # -- Time ----------------------------------------------------------
        label(row, format_seconds(session.total_seconds), size=13, weight="bold",
              anchor="center").grid(
            row=0, column=2, padx=4, pady=10
        )


    def _update_status(self) -> None:
        """Refresh the header status badge from the tracker's current state."""
        state = self.tracker.state
        if self._paused:
            self._status.configure(text="⏸  Paused", text_color=YELLOW)
        elif state.is_idle:
            self._status.configure(text="○  Idle", text_color=TEXT_SEC)
        else:
            app = state.current_app
            if app:
                display = (app[:22] + "...") if len(app) > 22 else app
                self._status.configure(text=f"●  {display}", text_color=GREEN)
            else:
                self._status.configure(text="●  Tracking", text_color=GREEN)

    # ═════════════════════════════════════════════════════════════════════════
    # Filter callbacks
    # ═════════════════════════════════════════════════════════════════════════

    def _on_filter(self, value: str) -> None:
        """Handle segment button selection."""
        mapping = {
            "Today":      "today",
            "Yesterday":  "yesterday",
            "This Week":  "week",
            "Custom":     "custom",
        }
        self._filter = mapping.get(value, "today")

        if self._filter == "custom":
            # Show the custom date inputs; push the table to row 4
            self._custom_bar.grid(row=3, column=0, sticky="w", padx=16, pady=(4, 0))
            self.root.rowconfigure(3, weight=0)
            self.root.rowconfigure(4, weight=1)
            if self._table_container:
                self._table_container.grid(row=4, column=0,
                                           sticky="nsew", padx=16, pady=12)
        else:
            # Hide custom inputs; restore table to row 3
            self._custom_bar.grid_remove()
            self.root.rowconfigure(3, weight=1)
            self.root.rowconfigure(4, weight=0)
            if self._table_container:
                self._table_container.grid(row=3, column=0,
                                           sticky="nsew", padx=16, pady=12)
            self._refresh()

    def _apply_custom(self) -> None:
        """Parse and validate the custom date range, then refresh."""
        try:
            s = date.fromisoformat(self._start_entry.get().strip())
            e = date.fromisoformat(self._end_entry.get().strip())
            if s > e:
                s, e = e, s
            self._custom_start = s
            self._custom_end   = e
            self._refresh()
        except ValueError:
            messagebox.showerror(
                "Invalid Date", "Please enter dates as YYYY-MM-DD."
            )

    # ═════════════════════════════════════════════════════════════════════════
    # Settings
    # ═════════════════════════════════════════════════════════════════════════

    def _open_settings(self) -> None:
        SettingsDialog(self.root, self.settings)

    # ═════════════════════════════════════════════════════════════════════════
    # Window lifecycle / tray integration
    # ═════════════════════════════════════════════════════════════════════════

    def _hide_to_tray(self) -> None:
        """Minimise to tray instead of closing when the user presses ✕."""
        self.root.withdraw()

    def show(self) -> None:
        """Restore and focus the window (called from tray 'Open' callback)."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def set_paused(self, paused: bool) -> None:
        """Called when the tray 'Pause' item is toggled."""
        self._paused = paused

    def run(self) -> None:
        """Start the Tk main loop. Blocks until quit() is called."""
        self.root.mainloop()

    def quit(self) -> None:
        """Destroy the window and exit the main loop."""
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
