"""core/smart — Automatizare: retry, coadă, priorități."""

import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta
import re
import math

from catssoft.core.models import DLStatus, Download
from catssoft.logging_setup import logger
from catssoft.utils.formatting import safe_int
# ── orig:3222 ──


class SmartAutomationEngine:
    """Conservative, UI-thread-driven policies for retries, queue fairness and disk safety.

    It does not own download threads: the existing worker remains responsible for HTTP
    transfer/recovery, while this controller decides whether a failed item should be
    queued for another attempt and when an already-scheduled retry may run.
    """
    PERMANENT_MARKERS = (
        "checksum", "spațiu insuficient", "spatiu insuficient", "insufficient disk",
        "permission denied", "access denied", "forbidden", "http 401",
        "http 403", "http 404", "status 401", "status 403", "status 404",
        "status 410", "invalid url", "url invalid", "unsupported scheme",
    )
    TRANSIENT_MARKERS = (
        "timed out", "timeout", "read timed out", "connection reset", "connection aborted",
        "connection error", "server disconnected", "remote end closed", "temporarily unavailable",
        "temporary failure", "network is unreachable", "network unreachable", "proxy error",
        "incomplete download", "segment download failed", "incomplete read", "chunked encoding",
        "http 408", "http 425", "http 429", "http 500", "http 502", "http 503", "http 504",
        "status 408", "status 425", "status 429", "status 500", "status 502", "status 503", "status 504",
        "connection refused", "remote host closed", "broken pipe",
    )

    def __init__(self, app):
        self.app = app
        self.block_reason = ""
        self._last_disk_check = 0.0
        self._last_retry_countdown_bucket = None

    def retryable_error(self, error: str) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        if any(marker in text for marker in self.PERMANENT_MARKERS):
            return False
        # requests commonly formats status errors as "503 Server Error", without
        # the words "HTTP" or "status". Keep permanent client errors separate.
        if re.search(r"\b(?:400|401|403|404|405|410|422)\b", text):
            return False
        if any(marker in text for marker in self.TRANSIENT_MARKERS):
            return True
        if re.search(r"\b(?:408|425|429|500|502|503|504)\b", text):
            return True
        # Unknown errors are deliberately not retried automatically. The download
        # worker already performs its own bounded local retries first.
        return False

    def schedule_retry_if_transient(self, dl: Download) -> bool:
        cfg = self.app.config
        if not bool(cfg.get("smart_retry_enabled", True)):
            return False
        if dl.status in (DLStatus.CANCELLED, DLStatus.COMPLETED):
            return False
        if not self.retryable_error(dl.error_msg):
            return False
        max_attempts = safe_int(cfg.get("smart_retry_max_attempts", 2), 2, 0, 8)
        current = safe_int(getattr(dl, "automatic_retry_count", 0), 0, 0, 100)
        if current >= max_attempts:
            return False
        base_delay = safe_int(cfg.get("smart_retry_base_delay", 5), 5, 1, 120)
        delay = min(300, base_delay * (2 ** current))
        original_error = str(dl.error_msg or "Eroare de rețea").strip()
        with dl.lock:
            dl.automatic_retry_count = current + 1
            dl.auto_retry_at = time.time() + delay
            dl.status = DLStatus.QUEUED
            dl.speed = 0.0
            dl.resume_requested = False
            dl.error_msg = (f"{original_error} · reîncercare automată "
                            f"{dl.automatic_retry_count}/{max_attempts} în {delay}s")
        logger.warning("[Smart Automation] %s: retry %s/%s in %ss (%s)",
                       dl.filename, dl.automatic_retry_count, max_attempts, delay, original_error[:180])
        try:
            self.app.db.save_active_queue(self.app.downloads)
        except Exception as exc:
            logger.warning("Could not persist scheduled automatic retry: %s", exc)
        return True

    def retry_is_ready(self, dl: Download, now: Optional[float] = None) -> bool:
        due = max(0.0, float(getattr(dl, "auto_retry_at", 0.0) or 0.0))
        return due <= (time.time() if now is None else now)

    def queue_score(self, dl: Download):
        """Apply aging so a long-waiting Low/Normal item is not starved indefinitely."""
        priority = safe_int(getattr(dl, "priority", 1), 1, 0, 2)
        if bool(self.app.config.get("smart_queue_aging_enabled", True)):
            aging_minutes = safe_int(self.app.config.get("smart_queue_aging_minutes", 20), 20, 1, 1440)
            added = getattr(dl, "added_date", None)
            if isinstance(added, datetime):
                waited_minutes = max(0.0, (datetime.now() - added).total_seconds() / 60.0)
                priority = min(2, priority + int(waited_minutes // aging_minutes))
        order = safe_int(getattr(dl, "queue_order", 0), 0, 0)
        added_ts = dl.added_date.timestamp() if isinstance(getattr(dl, "added_date", None), datetime) else 0
        return (-priority, order if order else 10**12, added_ts)

    def _free_space_for(self, dl: Download) -> Optional[int]:
        path = os.path.abspath(os.path.expanduser(getattr(dl, "save_path", "") or
                                                  self.app.config.get("default_folder", "~")))
        while not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        try:
            return int(shutil.disk_usage(path).free)
        except (OSError, ValueError):
            return None

    def can_start(self, dl: Download) -> bool:
        if not bool(self.app.config.get("smart_disk_guard_enabled", False)):
            self.block_reason = ""
            return True
        free = self._free_space_for(dl)
        minimum_mb = safe_int(self.app.config.get("smart_disk_min_free_mb", 512), 512, 0, 1024 * 1024)
        minimum = minimum_mb * 1024 * 1024
        if free is not None and free >= minimum:
            self.block_reason = ""
            return True
        self.block_reason = (f"Pornire suspendată: spațiu liber sub {minimum_mb} MB"
                             if free is not None else "Pornire suspendată: spațiul liber nu poate fi verificat")
        return False

    def tick(self) -> bool:
        """Release due retry timers. Returns True when queue/UI state needs refreshing."""
        now = time.time()
        changed = False
        for dl in tuple(getattr(self.app, "downloads", ())):
            if dl.status != DLStatus.QUEUED or not getattr(dl, "auto_retry_at", 0.0):
                continue
            if now < float(dl.auto_retry_at):
                # Refresh the countdown only when the visible second changes, not on
                # every 100 ms UI poll. This keeps the card animation lightweight.
                bucket = max(1, int(math.ceil(float(dl.auto_retry_at) - now)))
                if getattr(dl, "_auto_retry_countdown_bucket", None) != bucket:
                    dl._auto_retry_countdown_bucket = bucket
                    changed = True
                    try:
                        self.app._refresh_card(dl)
                    except Exception:
                        pass
                continue
            if dl.thread is not None and dl.thread.is_alive():
                continue
            with dl.lock:
                dl.auto_retry_at = 0.0
                dl.error_msg = ""
                dl.speed = 0.0
                # Counters are rebuilt from the actual file/segment artifacts by the
                # worker. Retain the partial files, but do not carry stale byte counts
                # into a retry (which would double-count completed segments).
                dl.downloaded = 0
                dl.progress = 0.0
                dl.chunk_progress = []
                dl.speed_tracker.samples.clear()
            logger.info("[Smart Automation] Retry ready: %s", dl.filename)
            changed = True
            try:
                self.app._refresh_card(dl)
            except Exception:
                pass
        # Re-check a paused queue if disk space may have become available.
        if self.block_reason and now - self._last_disk_check >= 3.0:
            self._last_disk_check = now
            pending = next((d for d in self.app.downloads if d.status == DLStatus.QUEUED), None)
            if pending is not None and self.can_start(pending):
                changed = True
        return changed
