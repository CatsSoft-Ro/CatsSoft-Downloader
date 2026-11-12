"""core/models — Modelele de date (descărcare, programare, istoric)."""

import threading
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List
from datetime import datetime, timedelta
# ── orig:1483 ──

# ─── Data Models ──────────────────────────────────────────────────────────────
class DLStatus(Enum):
    QUEUED      = "Queued"
    DOWNLOADING = "Downloading"
    PAUSED      = "Paused"
    COMPLETED   = "Completed"
    ERROR       = "Error"
    CANCELLED   = "Cancelled"
# ── orig:1491 ──

class SpeedTracker:
    def __init__(self):
        self.samples = []
        self.lock = threading.Lock()

    def update(self, bytes_downloaded: int):
        now = time.time()
        with self.lock:
            self.samples.append((now, bytes_downloaded))
            # Păstrează doar ultimele 5 secunde
            cutoff = now - 5
            self.samples = [(t, b) for t, b in self.samples if t > cutoff]

    def get_speed(self) -> float:
        with self.lock:
            if len(self.samples) < 2:
                return 0.0
            t0, b0 = self.samples[0]
            t1, b1 = self.samples[-1]
            if t1 - t0 < 0.1:
                return 0.0
            return max(0.0, (b1 - b0) / (t1 - t0))

    def rebase(self, bytes_downloaded: int):
        """Restart the sampling window on a new counter baseline.

        Bytes recovered from disk (an already complete segment, a resumed file)
        are added to the counter in one step. Without a new baseline the tracker
        would report that jump as transfer speed - a burst of tens of MB/s for
        a single tick.
        """
        now = time.time()
        with self.lock:
            self.samples = [(now, bytes_downloaded)]
# ── orig:1526 ──

@dataclass
class Download:
    url: str
    filename: str
    save_path: str
    status: DLStatus = DLStatus.QUEUED
    progress: float = 0.0
    speed: float = 0.0
    total: int = 0
    downloaded: int = 0
    error_msg: str = ""
    supports_resume: bool = True
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    stop_event: Optional[threading.Event] = field(default=None, repr=False)
    active_response: Optional[object] = field(default=None, repr=False)
    pause_event: Optional[threading.Event] = field(default=None, repr=False)
    speed_limit: int = 0
    segments: int = 1
    checksum: str = ""
    checksum_algo: str = "sha256"
    added_date: datetime = field(default_factory=datetime.now)
    completed_date: Optional[datetime] = None
    app: Optional = None
    lock: threading.Lock = field(default_factory=threading.RLock, repr=False)
    speed_tracker: SpeedTracker = field(default_factory=SpeedTracker, repr=False)
    started_at: Optional[datetime] = None
    peak_speed: float = 0.0
    speed_history: List[tuple] = field(default_factory=list, repr=False)
    retry_count: int = 0
    chunk_size_bytes: int = 0
    chunk_progress: List[float] = field(default_factory=list, repr=False)
    throttle_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    throttle_deadline: float = 0.0
    resume_requested: bool = False
    filename_locked: bool = False  # preserves a collision-safe destination path
    priority: int = 1             # 0=Low, 1=Normal, 2=High
    queue_order: int = 0          # stable ordering within the same priority
    speed_limit_override: Optional[int] = None  # bytes/s; None=global, 0=unlimited per-file
    session_transferred: int = 0  # actual network bytes read in this app session
    resume_validator: str = ""   # ETag or Last-Modified used with HTTP If-Range
    resume_blocked: bool = False  # existing output unverifiable: never resume from it
    final_verified: bool = False  # existing output proven to match the current resource
    history_recorded: bool = field(default=False, repr=False)
    automatic_retry_count: int = 0  # extra retries scheduled by Smart Automation
    auto_retry_at: float = 0.0       # UNIX timestamp; persisted across application restarts
    network_assignments: dict = field(default_factory=dict, repr=False)
    # Toate răspunsurile HTTP deschise ale acestei descărcări. Calea segmentată
    # ține simultan mai multe răspunsuri; ele trebuie închise la Stop, altfel
    # anularea așteaptă timeout-ul de citire (15 s) în loc să fie imediată.
    active_responses: List = field(default_factory=list, repr=False)
# ── orig:1578 ──


def _track_active_response(dl: "Download", response) -> None:
    """Înregistrează un răspuns HTTP deschis, ca să poată fi închis la Stop."""
    if response is None:
        return
    try:
        with dl.lock:
            dl.active_responses.append(response)
    except Exception:
        pass
# ── orig:1589 ──


def _untrack_active_response(dl: "Download", response) -> None:
    """Scoate un răspuns din registru (a fost închis sau a eșuat)."""
    if response is None:
        return
    try:
        with dl.lock:
            try:
                dl.active_responses.remove(response)
            except ValueError:
                pass
    except Exception:
        pass
# ── orig:1603 ──


def _close_response_async(response) -> None:
    """Închide un răspuns HTTP fără să blocheze firul care închide.

    Pe Windows, ``close()`` pe un socket cu un ``recv`` în așteptare blochează
    până la expirarea timeout-ului de citire (15-25 s): firul UI s-ar congela
    la apăsarea butonului Stop. Închiderea într-un fir secundar păstrează
    efectul (citirea încheiată, socketul eliberat) fără să blocheze interfața.
    """
    if response is None:
        return

    def _close_worker():
        try:
            response.close()
        except Exception:
            pass

    try:
        threading.Thread(target=_close_worker, daemon=True,
                         name="catssoft-close-response").start()
    except Exception:
        try:
            response.close()
        except Exception:
            pass
# ── orig:1630 ──


def _close_active_responses(dl: "Download") -> None:
    """Închide toate răspunsurile HTTP deschise ale descărcării.

    O citire blocată într-un răspuns nu se întrerupe la ``stop_event``; ea
    continuă până la timeout sau până la sosirea următorului bloc. Închiderea
    explicită face ca descărcarea să se oprească la citirea următoare, exact
    ca în calea simplă.
    """
    try:
        with dl.lock:
            responses = list(dl.active_responses)
    except Exception:
        responses = []
    for response in responses:
        _close_response_async(response)
# ── orig:1646 ──

@dataclass
class ScheduledDownload:
    url: str
    filename: str
    scheduled_time: datetime
    save_path: str = ""
    repeat: str = "once"
    enabled: bool = True
    segments: int = 1
    checksum: str = ""
# ── orig:1657 ──

@dataclass
class DownloadHistory:
    id: int
    url: str
    filename: str
    save_path: str
    status: str
    total_size: int
    downloaded_size: int
    added_date: str
    completed_date: str
    speed_avg: float
