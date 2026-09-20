"""Lightweight, non-invasive phase timers for performance measurement.

This module only records elapsed wall times under labelled checkpoints. It
never changes scraper behaviour; it exists so Phase 2 (Performance +
Reliability) can quantify where runtime is spent before and after
optimization and rely on measured numbers instead of guesses.
"""

import threading
import time
from collections import defaultdict
from typing import Dict, List, Tuple

_LOCK = threading.Lock()
_EVENTS: List[Tuple[str, float]] = []


def reset() -> None:
    with _LOCK:
        _EVENTS.clear()


def add(label: str, seconds: float) -> None:
    with _LOCK:
        _EVENTS.append((label, float(seconds)))


class _Timed:
    __slots__ = ("_label", "_t0")

    def __init__(self, label: str):
        self._label = label
        self._t0 = None

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc) -> bool:
        add(self._label, time.monotonic() - self._t0)
        return False


def timed(label: str) -> _Timed:
    """Context manager recording wall time under `label` (in seconds)."""
    return _Timed(label)


def summary(floor: float = 0.01) -> List[str]:
    """Return a sorted, human-readable summary of accumulated timings.

    Groups identical labels: total, call count, and average per call."""
    with _LOCK:
        events = list(_EVENTS)
    totals: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    for label, sec in events:
        totals[label] += sec
        counts[label] += 1

    lines: List[str] = ["TIMING SUMMARY (seconds)"]
    if not totals:
        lines.append("  (no timings recorded)")
        return lines
    for label in sorted(totals, key=lambda k: -totals[k]):
        if totals[label] < floor:
            continue
        n = counts[label]
        lines.append(
            f"  {label:32s} total={totals[label]:7.2f}  n={n:3d}  avg={totals[label] / (n or 1):6.2f}"
        )
    return lines


def labels() -> List[str]:
    """Return the distinct timing labels recorded so far."""
    with _LOCK:
        return sorted({label for label, _ in _EVENTS})


def totals(label: str) -> Tuple[float, int]:
    """Return (total_seconds, call_count) accumulated under `label`."""
    total = 0.0
    count = 0
    with _LOCK:
        for lbl, sec in _EVENTS:
            if lbl == label:
                total += sec
                count += 1
    return total, count