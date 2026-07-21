"""
autostart.py
------------
Manage Windows autostart via the HKCU Registry Run key.

Why HKCU (not HKLM)?
  - No administrator privileges required.
  - Per-user scope - appropriate for a personal utility app.
  - The key is: HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  - Any value under this key is launched automatically when the user logs in.

Compared to placing a shortcut in shell:startup:
  - Registry approach is cleaner (no .lnk file to manage).
  - Works identically for frozen .exe builds and script-mode launches.
  - Easy to inspect / verify in regedit.
"""

import logging
import sys
import winreg

logger = logging.getLogger(__name__)

_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "NexusTracker"


def _launch_command() -> str:
    """
    Build the command string that Windows will run on login.

    - Frozen (PyInstaller) build: just the .exe path.
    - Script mode: `"<python.exe>" "<main.py path>"` (both quoted for spaces).
    """
    if getattr(sys, "frozen", False):
        # Running as a compiled executable
        return f'"{sys.executable}"'
    else:
        return f'"{sys.executable}" "{sys.argv[0]}"'


def enable_autostart() -> bool:
    """
    Register this app to launch on Windows login.
    Returns True on success, False if the Registry write fails.
    """
    try:
        cmd = _launch_command()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
        logger.info("Autostart enabled: %s", cmd)
        return True
    except Exception as exc:
        logger.error("Failed to enable autostart: %s", exc)
        return False


def disable_autostart() -> bool:
    """
    Remove the autostart Registry entry.
    Returns True on success (including if the entry never existed).
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _APP_NAME)
        logger.info("Autostart disabled.")
        return True
    except FileNotFoundError:
        return True  # Already not registered - that's fine
    except Exception as exc:
        logger.error("Failed to disable autostart: %s", exc)
        return False


def is_autostart_enabled() -> bool:
    """
    Return True if the autostart Registry entry currently exists.
    Never raises - returns False on any error.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
            winreg.QueryValueEx(key, _APP_NAME)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
