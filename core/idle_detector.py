"""
core/idle_detector.py
---------------------
Windows idle detection via GetLastInputInfo (kernel32/user32).

No extra libraries needed - ctypes is part of the standard library.
GetLastInputInfo returns the tick count of the last input event (keyboard,
mouse, touch). We compare it against the current tick count to get idle time.
"""

import ctypes
import ctypes.wintypes


class _LASTINPUTINFO(ctypes.Structure):
    """Maps to the Win32 LASTINPUTINFO struct."""
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),   # tick count of last input event
    ]


def get_idle_seconds() -> float:
    """
    Return the number of seconds since the user last moved the mouse,
    pressed a key, or otherwise produced input.
    """
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))

    # GetTickCount wraps around every ~49.7 days; subtraction handles the wrap
    # correctly because both values are unsigned 32-bit integers.
    elapsed_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return elapsed_ms / 1000.0


def is_idle(threshold_seconds: float = 300.0) -> bool:
    """
    Return True if the user has been idle for at least *threshold_seconds*.

    Args:
        threshold_seconds: Inactivity threshold. Default 300 s (5 min).
    """
    return get_idle_seconds() >= threshold_seconds
