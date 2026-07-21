"""
storage/db.py
-------------
SQLite database layer for the Activity Tracker.

Design choices:
- Thread-local connections: each thread gets its own sqlite3.Connection,
  avoiding multi-thread contention while keeping connections persistent.
- WAL journal mode: allows concurrent readers and one writer, ideal for
  background tracking + UI reads happening simultaneously.
- Incremental writes: every poll tick writes directly, so a crash loses at
  most one POLL_INTERVAL worth of data (≤5 s by default).
- ON CONFLICT DO UPDATE: single upsert instead of a read-then-write cycle.
"""

import sqlite3
import threading
import logging
from pathlib import Path
from typing import Optional
from datetime import date

from storage.models import AppSession, DaySummary

logger = logging.getLogger(__name__)

# -- Database location ---------------------------------------------------------
DB_DIR  = Path.home() / "AppData" / "Roaming" / "NexusTracker"
DB_PATH = DB_DIR / "activity.db"

# Thread-local storage for per-thread connections
_local = threading.local()


# -- Connection management -----------------------------------------------------

def _get_connection() -> sqlite3.Connection:
    """
    Return (or create) a thread-local SQLite connection.
    Using one persistent connection per thread avoids the overhead of
    opening/closing a connection on every write.
    """
    if not hasattr(_local, 'conn') or _local.conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        # WAL mode for better concurrent read/write performance
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is safe with WAL and much faster than FULL
        conn.execute("PRAGMA synchronous=NORMAL")
        # Enable foreign-key enforcement
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        logger.debug("Opened new DB connection on thread %s", threading.current_thread().name)
    return _local.conn


def close_connection() -> None:
    """Close the thread-local connection if it exists."""
    if hasattr(_local, 'conn') and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


# -- Schema initialisation -----------------------------------------------------

def init_db() -> None:
    """Create tables and indexes if they do not already exist."""
    conn = _get_connection()
    conn.executescript("""
        -- One record per calendar day
        CREATE TABLE IF NOT EXISTS days (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT    NOT NULL UNIQUE   -- ISO-8601: "2026-07-16"
        );

        -- One record per (day, process) pair - rolled-up totals
        CREATE TABLE IF NOT EXISTS app_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            day_id        INTEGER NOT NULL
                              REFERENCES days(id) ON DELETE CASCADE,
            process_name  TEXT    NOT NULL,
            exe_path      TEXT,
            total_seconds INTEGER NOT NULL DEFAULT 0,
            UNIQUE(day_id, process_name)
        );

        -- Fast lookup by day
        CREATE INDEX IF NOT EXISTS idx_app_sessions_day  ON app_sessions(day_id);
        -- Fast lookup by date string
        CREATE INDEX IF NOT EXISTS idx_days_date         ON days(date);
    """)
    conn.commit()
    logger.info("Database ready at %s", DB_PATH)


# -- Write operations ----------------------------------------------------------

def get_or_create_day(date_str: str) -> int:
    """
    Ensure a row exists for *date_str* in the days table and return its id.
    INSERT OR IGNORE means this is a no-op if the day already exists.
    """
    conn = _get_connection()
    conn.execute("INSERT OR IGNORE INTO days (date) VALUES (?)", (date_str,))
    conn.commit()
    row = conn.execute("SELECT id FROM days WHERE date = ?", (date_str,)).fetchone()
    return row["id"]


def upsert_app_time(
    day_id: int,
    process_name: str,
    exe_path: Optional[str],
    seconds_to_add: int,
) -> None:
    """
    Add *seconds_to_add* to the running total for *process_name* on *day_id*.
    If no row exists yet, it is inserted. This is the hot path - called on
    every poll tick - so it is kept as a single SQL statement.
    """
    conn = _get_connection()
    conn.execute(
        """
        INSERT INTO app_sessions (day_id, process_name, exe_path, total_seconds)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(day_id, process_name) DO UPDATE SET
            total_seconds = total_seconds + excluded.total_seconds,
            -- Only overwrite exe_path if we have a new value
            exe_path      = COALESCE(excluded.exe_path, exe_path)
        """,
        (day_id, process_name, exe_path, seconds_to_add),
    )
    conn.commit()





# -- Read operations -----------------------------------------------------------

def get_day_summary(date_str: str) -> DaySummary:
    """Return all app sessions for a single day, ordered by total time desc."""
    conn = _get_connection()
    row = conn.execute("SELECT id FROM days WHERE date = ?", (date_str,)).fetchone()

    if row is None:
        # No data yet for this date
        return DaySummary(date_str=date_str, sessions=[])

    day_id = row["id"]
    rows = conn.execute(
        """
        SELECT id, process_name, exe_path, total_seconds
        FROM   app_sessions
        WHERE  day_id = ?
        ORDER  BY total_seconds DESC
        """,
        (day_id,),
    ).fetchall()

    sessions = [
        AppSession(
            id=r["id"],
            process_name=r["process_name"],
            exe_path=r["exe_path"],
            total_seconds=r["total_seconds"],
            day_id=day_id,
        )
        for r in rows
    ]
    return DaySummary(date_str=date_str, sessions=sessions, day_id=day_id)


def get_range_summary(start: date, end: date) -> DaySummary:
    """
    Return aggregated app sessions for a date range [start, end] inclusive.
    Rows for the same process across multiple days are summed together.
    """
    conn = _get_connection()
    start_str = start.isoformat()
    end_str   = end.isoformat()

    rows = conn.execute(
        """
        SELECT a.process_name,
               MAX(a.exe_path)          AS exe_path,
               SUM(a.total_seconds)     AS total_seconds
        FROM   app_sessions a
        JOIN   days d ON a.day_id = d.id
        WHERE  d.date BETWEEN ? AND ?
        GROUP  BY a.process_name
        ORDER  BY total_seconds DESC
        """,
        (start_str, end_str),
    ).fetchall()

    sessions = [
        AppSession(
            process_name=r["process_name"],
            exe_path=r["exe_path"],
            total_seconds=r["total_seconds"],
        )
        for r in rows
    ]
    label = f"{start_str} → {end_str}"
    return DaySummary(date_str=label, sessions=sessions)
