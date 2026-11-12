"""utils/formatting — Formatare pentru interfață."""

from typing import Optional, Callable, List
from datetime import datetime, timedelta
# ── orig:1672 ──


# ─── Utility Functions ────────────────────────────────────────────────────────
def safe_int(value, default=0, minimum=None, maximum=None) -> int:
    """Convert persisted/user values to an integer and optionally clamp the range."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result
# ── orig:1685 ──


def fmt_size(b: int) -> str:
    if b <= 0: return "? B"
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"
# ── orig:1692 ──

def fmt_speed(bps: float) -> str:
    return fmt_size(int(bps)) + "/s"
# ── orig:1695 ──

def fmt_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")
# ── orig:1699 ──


def fmt_duration(seconds: Optional[float]) -> str:
    """Format compact pentru durate/ETA; None indică o estimare indisponibilă."""
    if seconds is None:
        return "—"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}z {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes:02d}:{secs:02d}"
    return f"{secs}s"
