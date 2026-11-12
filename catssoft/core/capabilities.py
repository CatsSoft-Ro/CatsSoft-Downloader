"""core/capabilities — Capacități opționale (tray, notificări)."""
# ── orig:312 ──

try:
    import pystray
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
# ── orig:318 ──

try:
    from plyer import notification
    NOTIFY_AVAILABLE = True
except ImportError:
    NOTIFY_AVAILABLE = False
