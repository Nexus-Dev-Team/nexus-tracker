"""
core/productivity.py
--------------------
Time formatting utility.
"""

from typing import List
from storage.models import AppSession


def format_seconds(total: int) -> str:
    """
    Convert a raw second count to a concise human-readable string.

    Examples:
        format_seconds(0)      -> "0s"
        format_seconds(75)     -> "1m 15s"
        format_seconds(3661)   -> "1h 01m"
        format_seconds(90000)  -> "25h 00m"
    """
    if total <= 0:
        return "0s"

    hours, remainder = divmod(int(total), 3600)
    minutes, seconds  = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    elif minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    else:
        return f"{seconds}s"
