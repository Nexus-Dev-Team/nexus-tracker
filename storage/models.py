"""
storage/models.py
-----------------
Pure data containers (no DB logic). These dataclasses are used to pass
structured data between the DB layer and the UI/core layers.
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class AppSession:
    """Represents one app's accumulated usage for a given day."""
    process_name: str
    total_seconds: int
    exe_path: Optional[str] = None
    day_id: Optional[int] = None
    id: Optional[int] = None


@dataclass
class DaySummary:
    """Aggregated view of a single day (or a date range) of activity."""
    date_str: str                        # ISO date "2026-07-16" or range label
    sessions: List[AppSession] = field(default_factory=list)
    day_id: Optional[int] = None

    @property
    def total_seconds(self) -> int:
        return sum(s.total_seconds for s in self.sessions)
