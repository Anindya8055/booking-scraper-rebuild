"""Logging helper: writes to stdout and a log file."""

import json
import time
from pathlib import Path

from .config import LOG_DIR

LOG_FILE = LOG_DIR / "booking_debug.log"

_PRINT_ENABLED = True


def set_print_enabled(enabled: bool) -> None:
    """Toggle stdout printing. Detail lines still go to the debug log file."""
    global _PRINT_ENABLED
    _PRINT_ENABLED = bool(enabled)


def log(msg: str, **kv):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if kv:
        try:
            extra = " " + json.dumps(kv, ensure_ascii=False, default=str)
        except Exception:
            extra = " " + str(kv)
        line += extra
    if _PRINT_ENABLED:
        print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
