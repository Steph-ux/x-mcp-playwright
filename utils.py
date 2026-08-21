"""Shared utilities for x-mcp-playwright — Chrome discovery, paths, logging."""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config constants (read once at import time)
# ---------------------------------------------------------------------------

USER_DATA_DIR = os.path.expanduser("~/.x-mcp-playwright/profile")
HEADLESS = os.environ.get("X_MCP_HEADLESS", "1") not in ("0", "false", "False")
DEFAULT_TIMEOUT_MS = int(os.environ.get("X_MCP_TIMEOUT_MS", "20000"))
SCREENSHOT_DIR = Path(os.environ.get(
    "X_MCP_SCREENSHOT_DIR",
    os.path.expanduser("~/.x-mcp-playwright/screenshots"),
))
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Logging (to stderr, never interferes with MCP stdio)
# ---------------------------------------------------------------------------

_builtin_print = builtins.print


def _log(*args: object, **kwargs: object) -> None:
    """Log to stderr so it doesn't interfere with MCP stdio protocol."""
    kwargs.setdefault("file", sys.stderr)
    kwargs.setdefault("flush", True)
    _builtin_print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Chrome binary discovery
# ---------------------------------------------------------------------------

_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
]


def find_chrome() -> Optional[str]:
    """Locate a Chrome binary; falls back to bundled Chromium if None.

    Set X_MCP_USE_SYSTEM_CHROME=1 to opt into the system Chrome binary.
    Default is to use patchright's bundled Chromium, which avoids profile
    lock conflicts with a user Chrome already running.
    """
    if os.environ.get("X_MCP_USE_SYSTEM_CHROME", "0") != "1":
        return None
    for path in _CHROME_PATHS:
        if os.path.exists(path):
            return path
    return None