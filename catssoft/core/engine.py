"""core/engine — Motorul de transfer (segmente, reluare, asamblare)."""

import threading
import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from catssoft.core.models import DLStatus, Download, _track_active_response, _untrack_active_response
from catssoft.core.network import SESSION
from catssoft.core.security import scan_with_defender
from catssoft.logging_setup import logger
from catssoft.utils.files import _get_ci_header, auto_sort_file, filename_from_url, guess_filetype, sanitize_filename, unique_target_path, verify_checksum
from catssoft.utils.formatting import fmt_size, safe_int
# ── orig:1469 ──

# 1. Dynamic Segmentation (în AddURLDialog._submit())
def optimal_segments(file_size: int) -> int:
    if file_size <= 0: return 1
    elif file_size < 5 * 1024 * 1024: return 1
    elif file_size < 50 * 1024 * 1024: return 4
    elif file_size < 500 * 1024 * 1024: return 8
    else: return min(16, max(8, file_size // (50 * 1024 * 1024)))
# ── orig:1477 ──

# 2. Adaptive Chunk Size (în _download_segment())
def adaptive_chunk_size(speed_bps: float) -> int:
    if speed_bps < 2 * 1024 * 1024: return 32 * 1024
    elif speed_bps < 10 * 1024 * 1024: return 64 * 1024
    else: return 256 * 1024
# ── orig:3392 ──

# ─── Download Worker (ALL FIXES) ──────────────────────────────────────────────

def _record_session_transfer(dl: "Download", bytes_count: int) -> None:
    """Count bytes actually read from the network for this session, even if a card is removed."""
    count = max(0, safe_int(bytes_count, 0))
    if count <= 0:
        return
    with dl.lock:
        dl.session_transferred = max(0, int(getattr(dl, "session_transferred", 0) or 0)) + count
    app = getattr(dl, "app", None)
    lock = getattr(app, "_session_transfer_lock", None) if app is not None else None
    if lock is not None:
        with lock:
            app._session_transferred_total = max(0, int(getattr(app, "_session_transferred_total", 0) or 0)) + count
# ── orig:3406 ──


def _apply_speed_limit(dl: "Download", bytes_count: int, network_key: str = "system"):
    """Enforce global, per-download, optional slow-mode and per-network speed caps."""
    app = dl.app
    global_limit = 0
    try:
        global_limit = max(0, safe_int(app.config.get("speed_limit", 0), 0)) if app else 0
        if app and app.config.get("slow_mode_enabled", False):
            slow_limit = max(1, safe_int(app.config.get("slow_mode_limit_kbps", 512), 512)) * 1024
            global_limit = min(global_limit, slow_limit) if global_limit > 0 else slow_limit
    except Exception:
        global_limit = max(0, safe_int(getattr(dl, "speed_limit", 0), 0))
    per_download_limit = getattr(dl, "speed_limit_override", None)
    per_download_limit = None if per_download_limit is None else max(0, safe_int(per_download_limit, 0))
    if bytes_count <= 0 and not network_key:
        return

    waits = []
    now = time.monotonic()
    if global_limit > 0 and bytes_count > 0:
        owner = app if app is not None else dl
        lock = getattr(owner, "_bandwidth_lock", dl.throttle_lock)
        with lock:
            deadline = max(now, getattr(owner, "_bandwidth_deadline", 0.0))
            wait_for = max(0.0, deadline - now)
            new_deadline = deadline + bytes_count / global_limit
            if hasattr(owner, "_bandwidth_deadline"):
                owner._bandwidth_deadline = new_deadline
            else:
                dl.throttle_deadline = new_deadline
        waits.append(wait_for)

    if per_download_limit is not None and per_download_limit > 0 and bytes_count > 0:
        with dl.throttle_lock:
            deadline = max(now, dl.throttle_deadline)
            wait_for = max(0.0, deadline - now)
            dl.throttle_deadline = deadline + bytes_count / per_download_limit
        waits.append(wait_for)

    manager = getattr(app, "network_manager", None) if app is not None else None
    if manager is not None and bytes_count > 0:
        waits.append(manager.throttle_wait(network_key, bytes_count))

    wait_until = time.monotonic() + max(waits or [0.0])
    while True:
        if dl.stop_event and dl.stop_event.is_set():
            return
        if dl.pause_event and not dl.pause_event.is_set():
            dl.pause_event.wait(timeout=0.25)
            continue
        remaining = wait_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))
# ── orig:3461 ──


def _download_meta_path(save_path: str) -> str:
    return f"{save_path}.catssoft-meta.json"
# ── orig:3465 ──


def _read_download_meta(save_path: str) -> dict:
    try:
        with open(_download_meta_path(save_path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
# ── orig:3474 ──


def _write_download_meta(save_path: str, data: dict) -> None:
    meta_path = _download_meta_path(save_path)
    temp_path = meta_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, meta_path)
    except OSError as exc:
        logger.warning("Could not persist resume metadata for %s: %s", os.path.basename(save_path), exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
# ── orig:3493 ──


def _remove_download_meta(save_path: str) -> None:
    for candidate in (_download_meta_path(save_path), _download_meta_path(save_path) + ".tmp"):
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError:
            pass
# ── orig:3502 ──


def _validate_or_reset_resume_artifacts(dl: Download, total: int, headers: dict,
                                        allow_restart_without_validator: bool = True) -> str:
    """Check HTTP validators before reusing persisted partial data; returns current validator."""
    get_header = lambda name: _get_ci_header(headers, name, "") if headers else ""
    etag = str(get_header("etag") or "").strip()
    last_modified = str(get_header("last-modified") or "").strip()
    current_validator = etag or last_modified
    old = _read_download_meta(dl.save_path)
    old_etag = str(old.get("etag") or "").strip()
    old_last_modified = str(old.get("last_modified") or "").strip()
    old_validator = old_etag or old_last_modified
    old_total = safe_int(old.get("total", 0), 0, 0)
    old_url = str(old.get("url") or "")
    # Validatoarele se compară „de același fel”: un ETag vechi nu trebuie comparat
    # cu un Last-Modified nou — tipuri diferite, deci o comparație care produce
    # întotdeauna o diferență falsă și invalida reluarea fără motiv.
    validator_conflict = (
        (bool(old_etag) and bool(etag) and old_etag != etag)
        or (bool(old_last_modified) and bool(last_modified) and old_last_modified != last_modified)
    )
    changed = bool(old) and (
        (old_url and old_url != dl.url)
        or (old_total > 0 and total > 0 and old_total != total)
        or validator_conflict
    )
    # Segment files from older versions may exist without a matching metadata file.
    # They are not trusted because their URL/size/validator cannot be confirmed.
    orphaned_segments = False
    try:
        base = os.path.basename(dl.save_path)
        pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
        orphaned_segments = any(pattern.fullmatch(item.name)
                                for item in Path(dl.save_path).parent.iterdir()) and not old
    except OSError:
        pass
    orphaned_final = False
    if os.path.isfile(dl.save_path):
        try:
            existing_final_size = os.path.getsize(dl.save_path)
            if existing_final_size > 0:
                # Un fișier final fără metadate care să-l corespundă nu poate fi
                # dovedit ca fiind resursa curentă. Acceptarea lui ar raporta
                # COMPLET fără un octet transferat și ar „ștampila" fișierul vechi
                # cu URL-ul și validatorul curente.
                orphaned_final = (not old) or total <= 0 or existing_final_size != total
        except OSError:
            pass
    if changed or orphaned_segments or orphaned_final:
        logger.warning("Resource changed for %s; discarding stale partial data", dl.filename)
        _remove_segment_parts(dl.save_path)
        if changed:
            try:
                # Doar o schimbare dovedită justifică ștergerea fișierului final.
                if os.path.isfile(dl.save_path):
                    os.remove(dl.save_path)
            except OSError as exc:
                logger.warning("Could not remove stale output: %s", exc)
        elif orphaned_final:
            # Fișierul existent nu poate fi verificat (lipsesc metadatele): nu îl
            # ștergem, dar nici nu reluăm peste el — transferul pornește de la zero
            # și va suprascrie fișierul, în loc să concateneze octeți străini.
            dl.resume_blocked = True
            logger.warning("Existing output for %s cannot be verified; restarting without resume",
                           dl.filename)
        with dl.lock:
            dl.downloaded = 0
            dl.progress = 0.0
            dl.chunk_progress = []
    validator = current_validator or old_validator
    restarted_without_validator = False
    if not validator and allow_restart_without_validator and _has_resume_artifacts(dl.save_path):
        # Fără ETag și fără Last-Modified nu există NICIO dovadă că octeții aflați
        # pe disc aparțin resursei curente: serverul ar returna 206 cu conținut NOU
        # pentru intervalul cerut, iar rezultatul ar fi un fișier care amestecă două
        # versiuni - raportat COMPLET, pentru că dimensiunea se potrivește.
        # Renunțăm la reluare și pornim de la zero.
        logger.warning("Serverul nu expune validator HTTP pentru %s; reluarea este nesigură, "
                       "se reia de la zero", dl.filename)
        _remove_segment_parts(dl.save_path)
        dl.resume_blocked = True
        restarted_without_validator = True
        with dl.lock:
            dl.downloaded = 0
            dl.progress = 0.0
            dl.chunk_progress = []
    dl.resume_validator = validator
    # Fișierul final poate fi considerat complet doar dacă metadatele sale corespund
    # resursei curente; altfel există riscul de a-l raporta COMPLET fără transfer.
    dl.final_verified = (bool(old) and not changed and not orphaned_final
                         and not restarted_without_validator)
    _write_download_meta(dl.save_path, {
        "url": dl.url, "total": int(total or old_total or 0), "etag": etag or old.get("etag", ""),
        "last_modified": last_modified or old.get("last_modified", ""),
        "updated_at": datetime.now().isoformat(),
    })
    return validator
# ── orig:3600 ──


def check_disk_space(save_path: str, required_bytes: int, margin: float = 1.05) -> bool:
    """FEATURE NOU: verifică dacă mai e loc pe disc (cu marjă 5%) înainte să pornească
    efectiv descărcarea, ca să nu eșueze abia la final după ce a consumat bandă degeaba."""
    try:
        folder = os.path.dirname(save_path) or "."
        os.makedirs(folder, exist_ok=True)
        free = shutil.disk_usage(folder).free
        return free >= required_bytes * margin
    except Exception:
        return True  # dacă nu putem verifica, nu blocăm descărcarea
# ── orig:3612 ──


def _merge_space_available(save_path: str, total: int) -> bool:
    """Există loc pe disc pentru asamblarea segmentelor în calea cu segmente?

    Asamblarea scrie o copie temporară a întregului fișier (temp + os.replace),
    deci în momentul asamblării discul trebuie să conțină simultan segmentele
    descărcate ȘI copia finală: de două ori dimensiunea fișierului.

    Fără această verificare, o descărcare pe segmente pornită cu puțin mai
    mult spațiu liber decât dimensiunea fișierului consumă tot bandwidth-ul și
    apoi eșuează la asamblare cu „no space left on device".
    """
    if total <= 0:
        return True
    try:
        folder = os.path.dirname(save_path) or "."
        free = shutil.disk_usage(folder).free
    except (OSError, ValueError):
        return True  # nu putem verifica -> nu blocăm descărcarea
    return free >= total * 2
# ── orig:3633 ──


def _remove_segment_parts(save_path: str):
    """Șterge doar fișierele .part cu sufix numeric ale descărcării curente."""
    if not save_path:
        return
    base = os.path.basename(save_path)
    parent = Path(save_path).parent
    pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
    try:
        for candidate in parent.iterdir():
            if pattern.fullmatch(candidate.name):
                try:
                    candidate.unlink()
                except OSError:
                    logger.warning("Nu am putut șterge segmentul temporar %s", candidate)
    except OSError as exc:
        logger.warning("Nu am putut inspecta segmentele temporare pentru %s: %s", save_path, exc)
# ── orig:3651 ──


def _has_resume_artifacts(save_path: str) -> bool:
    """Return True when partial data already binds the transfer to this destination path."""
    try:
        if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
            return True
    except OSError:
        pass
    base = os.path.basename(save_path)
    pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
    try:
        return any(pattern.fullmatch(candidate.name) for candidate in Path(save_path).parent.iterdir())
    except OSError:
        return False
# ── orig:3666 ──


def _segment_bytes_on_disk(save_path: str) -> int:
    """Total bytes already stored in the ``.partN`` ranges of this download.

    Used to compute the space still required on disk: the ranges already on disk
    occupy room, so asking for the whole file again would wrongly report
    "insufficient disk space" while resuming a large download.
    """
    if not save_path:
        return 0
    base = os.path.basename(save_path)
    parent = Path(save_path).parent
    pattern = re.compile(re.escape(base) + r"\.part\d+$")
    total = 0
    try:
        for candidate in parent.iterdir():
            if pattern.fullmatch(candidate.name):
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total
# ── orig:3691 ──


def _reconcile_downloaded_with_disk(dl: Download) -> int:
    """Align ``dl.downloaded`` with the bytes that really exist in the output file.

    The counter can come from the database (a restored queue) and may be stale:
    the file may have been truncated, replaced or removed in the meantime. Using
    a stale value as the Range offset makes the server fill the wrong interval -
    the final size check still passes and the file is reported COMPLETED while
    the first bytes are missing and the last ones duplicated.
    """
    on_disk = 0
    if getattr(dl, "supports_resume", False) and not getattr(dl, "resume_blocked", False):
        try:
            if os.path.isfile(dl.save_path):
                on_disk = os.path.getsize(dl.save_path)
        except OSError:
            on_disk = 0
    with dl.lock:
        dl.downloaded = on_disk
        dl.progress = (min(99.9, on_disk / dl.total * 100.0)
                       if getattr(dl, "total", 0) > 0 else 0.0)
    return on_disk
# ── orig:3715 ──


# Fișierele temporare create de motor: segmente, leftovers din „race" și metadate.
_TEMP_FILE_PATTERN = re.compile(r"\.part\d+(?:\.race\d+)?$|\.catssoft-meta\.json(?:\.tmp)?$")
# ── orig:3718 ──


def scan_orphan_temp_files(folder: str, protected_bases=(), min_age_seconds: int = 3600):
    """FEATURE NOU: găsește fișierele temporare fără owner (segmente orfane).

    ``protected_bases`` conține numele de fișier ale descărcărilor active; orice
    fișier temporar care începe cu ele este considerat în uz și nu este oferit
    pentru ștergere. Returnează o listă de tuple (cale, dimensiune, vârstă_s).
    """
    protected = [str(base) for base in (protected_bases or ()) if base]
    found = []
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return found
    now = time.time()
    for entry in entries:
        name = entry.name
        if not _TEMP_FILE_PATTERN.search(name):
            continue
        if any(name.startswith(base + ".") for base in protected):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age < min_age_seconds:
            continue
        found.append((str(entry), stat.st_size, age))
    return found
# ── orig:3846 ──


class AdaptiveStreamController:
    """Adjust active range workers in response to successful/failed range requests."""
    def __init__(self, initial_workers: int, max_workers: int, enabled: bool = True):
        self.max_workers = max(1, int(max_workers))
        self.min_workers = 1
        self.target_workers = max(1, min(self.max_workers, int(initial_workers)))
        self.enabled = bool(enabled)
        if not self.enabled:
            self.target_workers = self.max_workers
        self.active_workers = 0
        self.success_streak = 0
        self._goodput_samples = []
        self._condition = threading.Condition()

    def acquire(self, stop_event=None, pause_event=None):
        with self._condition:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                if pause_event is not None and not pause_event.is_set():
                    self._condition.wait(timeout=0.2)
                    continue
                if self.active_workers < self.target_workers:
                    self.active_workers += 1
                    return True
                self._condition.wait(timeout=0.2)

    def release(self):
        with self._condition:
            self.active_workers = max(0, self.active_workers - 1)
            self._condition.notify_all()

    def observe(self, success: bool, elapsed: float, byte_count: int, cancelled=False):
        with self._condition:
            if not self.enabled or cancelled:
                self._condition.notify_all()
                return
            if success:
                goodput = max(0, int(byte_count)) / max(0.01, float(elapsed))
                self._goodput_samples.append(goodput)
                self._goodput_samples = self._goodput_samples[-8:]
                # Only add concurrency when ranges complete quickly enough to suggest
                # the server/network is benefiting from parallelism. Slow, throttled
                # blocks do not automatically trigger ever-higher request counts.
                if goodput >= 64 * 1024:
                    self.success_streak += 1
                else:
                    self.success_streak = 0
                if self.success_streak >= 2 and self.target_workers < self.max_workers:
                    recent_avg = sum(self._goodput_samples[-4:]) / max(1, len(self._goodput_samples[-4:]))
                    if recent_avg >= 64 * 1024:
                        self.target_workers += 1
                        self.success_streak = 0
                        logger.debug("Adaptive streams increased to %s (recent range goodput %.0f B/s)",
                                     self.target_workers, recent_avg)
            else:
                self.success_streak = 0
                if self.target_workers > self.min_workers:
                    self.target_workers -= 1
                    logger.debug("Adaptive streams reduced to %s after a range failure", self.target_workers)
            self._condition.notify_all()
# ── orig:3909 ──


class DownloadResourceChangedError(Exception):
    """Raised when resume validators indicate the server representation changed."""
# ── orig:3913 ──


def _download_worker_entry(dl: Download, update_q: queue.Queue):
    """Entry point thread-safe: transformă excepțiile neașteptate în erori UI vizibile."""
    try:
        download_segmented(dl, update_q)
    except BaseException as exc:
        logger.exception("Unexpected worker failure for %s", getattr(dl, "filename", "download"))
        dl.error_msg = f"Eroare internă: {exc}"
        update_q.put(("status", dl, DLStatus.CANCELLED if dl.stop_event and dl.stop_event.is_set() else DLStatus.ERROR))
    finally:
        dl.active_response = None
        # Niciun răspuns HTTP nu mai trebuie urmărit după ieșirea din worker.
        try:
            with dl.lock:
                dl.active_responses.clear()
        except Exception:
            pass
        update_q.put(("worker_finished", dl, None))
# ── orig:3932 ──


def download_segmented(dl: Download, update_q: queue.Queue, max_retries: int = 3):
    """FIX: Progress corect bazat pe dl.downloaded / dl.total"""

    for attempt in range(max_retries):
        try:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            if attempt > 0:
                logger.info(f"Retry attempt {attempt + 1}/{max_retries}")
                with dl.lock:
                    dl.downloaded = 0  # FIX: Reset corect
                    dl.progress = 0.0
                    dl.chunk_progress = []
                # Keep already verified range files: the next pass reuses complete
                # blocks and resumes partial ones. Stale blocks are removed only when
                # the server validator/size check proves the resource changed.
                with dl.lock:
                    dl.retry_count += 1

            total = 0
            supports_ranges = False
            final_url = dl.url
            response_headers = {}

            try:
                with SESSION.head(dl.url, timeout=10, allow_redirects=True) as head:
                    total = int(head.headers.get('content-length', 0))
                    supports_ranges = head.headers.get('Accept-Ranges', '').lower() == 'bytes'
                    final_url = head.url
                    has_disposition = bool(head.headers.get('content-disposition'))
                    response_headers = {k.lower(): v for k, v in head.headers.items()}

                if not has_disposition:
                    logger.info("No Content-Disposition in HEAD, trying GET...")
                    with SESSION.get(dl.url, stream=True, timeout=10,
                                      headers={"Range": "bytes=0-0"}) as test_get:
                        response_headers = dict(test_get.headers)
                        final_url = test_get.url

            except Exception as e:
                logger.warning(f"HEAD request failed, using GET: {e}")
                try:
                    with SESSION.get(dl.url, stream=True, timeout=10) as resp:
                        total = int(resp.headers.get('content-length', 0))
                        supports_ranges = resp.headers.get('Accept-Ranges', '').lower() == 'bytes'
                        final_url = resp.url
                        response_headers = {k.lower(): v for k, v in resp.headers.items()}
                except Exception:
                    pass

            # Unele servere nu declară Accept-Ranges, iar altele ignoră HEAD.
            # Proba reală determină atât suportul Range, cât și totalul din Content-Range.
            if not supports_ranges:
                range_ok, probed_total, probe_headers, probe_url = probe_range(dl.url)
                supports_ranges = range_ok
                if total <= 0 and probed_total > 0:
                    total = probed_total
                if probe_headers:
                    response_headers = {str(k).lower(): v for k, v in probe_headers.items()}
                    final_url = probe_url or final_url

            # FIX: Verifică dacă total este valid
            if total <= 0:
                logger.warning("Content-Length is 0 or invalid, progress will be estimated")
                # NU setăm total = 1, lăsăm să fie 0 și gestionăm în UI

            # FEATURE NOU: verificare spațiu disc disponibil înainte de a începe efectiv.
            # La reluare verificăm doar octeții rămași: cei deja descărcați ocupă
            # deja loc pe disc, iar cererea lor ar respinde fals "spațiu insuficient".
            # CONTORUL dl.downloaded nu poate fi folosit aici: pe o reluare din coadă
            # este 0 (sau vechi), în timp ce octeții reali stau în fișierele .partN.
            bytes_on_disk = _segment_bytes_on_disk(dl.save_path)
            remaining_required = max(0, total - bytes_on_disk)
            if remaining_required > 0 and not check_disk_space(dl.save_path, remaining_required):
                dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FIX: numele fișierului folosește acum final_url (după redirect) + headerele
            # deja obținute, în loc să arunce final_url și să reinterogheze serverul.
            if response_headers:
                suggested_name = filename_from_url(dl.url, response_headers, final_url)
                if (suggested_name and suggested_name != dl.filename and not dl.filename_locked
                        and not _has_resume_artifacts(dl.save_path)):
                    logger.info(f"Updating filename: {dl.filename} -> {suggested_name}")
                    proposed_path = os.path.join(os.path.dirname(dl.save_path), sanitize_filename(suggested_name))
                    if os.path.abspath(proposed_path) != os.path.abspath(dl.save_path):
                        if os.path.exists(proposed_path):
                            proposed_path = unique_target_path(proposed_path)
                        dl.save_path = proposed_path
                        dl.filename = os.path.basename(proposed_path)
                        if dl.app:
                            update_q.put(("refresh", dl, None))

            dl.supports_resume = supports_ranges
            dl.total = total  # FIX: Setează total CORECT
            # Renunțarea la reluare fără validator se face doar la prima încercare:
            # ulterior, datele de pe disc pot fi deja ale noastre.
            _validate_or_reset_resume_artifacts(
                dl, total, response_headers, allow_restart_without_validator=(attempt == 0))

            adaptive_enabled = bool(dl.app and dl.app.config.get("adaptive_streams_enabled", True))
            if total == 0 or not supports_ranges or (dl.segments <= 1 and not adaptive_enabled):
                return download_simple(dl, update_q, max_retries)

            # Calea cu segmente asamblează la final segmentele într-o copie
            # temporară a întregului fișier: discul trebuie să țină simultan
            # segmentele ȘI copia, adică de două ori dimensiunea fișierului.
            # Dacă locul nu ajunge, descărcarea ar consuma tot bandwidth-ul și
            # ar eșua abia la asamblare. În acest caz coborâm pe calea simplă:
            # scrie direct în fișierul final și nu are nevoie de spațiu în plus.
            # Nu schimbăm calea dacă există deja segmente pe disc (reluare) sau
            # dacă nu e prima încercare (retry): acolo segmentele trebuie păstrate.
            if attempt == 0 and total > 0 and not _has_resume_artifacts(dl.save_path):
                if not _merge_space_available(dl.save_path, total):
                    logger.info("Spațiu insuficient pentru asamblare (%s) - descărcare simplă",
                                dl.filename)
                    return download_simple(dl, update_q, max_retries)

            # FEATURE NOU: dacă fișierul final există deja complet (ex: merge-ul s-a
            # terminat chiar înainte ca aplicația să se închidă, între sesiuni), nu mai
            # redescărcăm totul de la zero prin segmente.
            # DOAR dacă fișierul poate fi dovedit ca fiind resursa curentă: altfel
            # raportăm COMPLET un fișier vechi, fără să transferăm un octet.
            already_complete = (getattr(dl, "final_verified", False)
                                and os.path.exists(dl.save_path)
                                and os.path.getsize(dl.save_path) == total)

            if not already_complete:
                # Reconstruct the logical byte count from on-disk segments below. A
                # restored Download object may already contain a DB progress value;
                # retaining it here would count every recovered segment twice.
                with dl.lock:
                    dl.downloaded = 0
                    dl.progress = 0.0
                    dl.speed = 0.0
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                # Plexo-style work queue: workers claim the next missing range when
                # they finish, so fast connections naturally take more of the file.
                # Keep a Plexo-like visible chunk map without creating thousands of
                # tiny files for large downloads (target about 96 ranges, 1-8 MiB).
                mib = 1024 * 1024
                range_bytes = max(mib, min(8 * mib,
                                           ((total + 96 * mib - 1) // (96 * mib)) * mib))
                range_count = (total + range_bytes - 1) // range_bytes
                segment_files = [f"{dl.save_path}.part{i}" for i in range(range_count)]
                with dl.lock:
                    dl.chunk_size_bytes = range_bytes
                    dl.chunk_progress = []
                    for i, segment_path in enumerate(segment_files):
                        start = i * range_bytes
                        expected = min(total - start, range_bytes)
                        existing = os.path.getsize(segment_path) if os.path.exists(segment_path) else 0
                        dl.chunk_progress.append(min(1.0, existing / expected) if expected else 0.0)
                work_queue = queue.Queue()
                for i in range(range_count):
                    start = i * range_bytes
                    end = min(total - 1, start + range_bytes - 1)
                    work_queue.put((i, start, end))

                thread_errors = []  # entries: (chunk_index | None, message)
                thread_errors_lock = threading.Lock()
                resource_changed_event = threading.Event()
                adaptive_enabled = bool(dl.app and dl.app.config.get("adaptive_streams_enabled", True))
                max_streams = max(1, safe_int(
                    dl.app.config.get("adaptive_stream_max", 16), 16, 1, 32)) if adaptive_enabled and dl.app else max(1, safe_int(dl.segments, 1, 1, 32))
                worker_cap = min(max_streams, range_count)
                initial_workers = min(worker_cap, max(2, min(safe_int(dl.segments, 1, 1, 32), 4))) if adaptive_enabled else worker_cap
                controller = AdaptiveStreamController(initial_workers, worker_cap, adaptive_enabled)

                active_chunks = {}
                chunk_winners = {}
                primary_cancel_events = {i: threading.Event() for i in range(range_count)}
                race_cancel_events = {i: [] for i in range(range_count)}
                # Store open responses so a selected winner can close the losing HTTP
                # response and prevent a stale worker from continuing to write to disk.
                chunk_response_handles = {i: [] for i in range(range_count)}
                raced_chunks = set()
                race_threads = []
                race_state_lock = threading.RLock()
                race_stop = threading.Event()
                active_races = {"count": 0, "total": 0}
                endgame_enabled = bool(dl.app and dl.app.config.get("endgame_enabled", True))
                endgame_trigger = max(1.5, float(safe_int(
                    dl.app.config.get("endgame_trigger_seconds", 4), 4, 2, 30))) if dl.app else 4.0
                max_races_total = min(4, max(1, worker_cap // 4)) if endgame_enabled else 0

                def range_progress(index, byte_count):
                    now = time.monotonic()
                    with race_state_lock:
                        item = active_chunks.get(index)
                        if item is not None:
                            item["last_progress"] = now
                            item["bytes"] += max(0, int(byte_count))

                def publish_winner(index, path):
                    responses_to_close = []
                    with race_state_lock:
                        current = chunk_winners.get(index)
                        if current is not None:
                            return current == path
                        chunk_winners[index] = path
                        if path != segment_files[index]:
                            primary_cancel_events[index].set()
                        with dl.lock:
                            if index < len(dl.chunk_progress):
                                dl.chunk_progress[index] = 1.0
                        for event in race_cancel_events[index]:
                            event.set()
                        responses_to_close = [resp for owner_path, resp in chunk_response_handles[index]
                                              if owner_path != path and resp is not None]
                    for response in responses_to_close:
                        try:
                            response.close()
                        except Exception:
                            pass
                    return True

                def register_response(index, owner_path, response):
                    with race_state_lock:
                        chunk_response_handles[index].append((owner_path, response))
                        winner = chunk_winners.get(index)
                    # A response can open just after a winner has already been chosen.
                    if winner is not None and winner != owner_path:
                        try:
                            response.close()
                        except Exception:
                            pass

                def race_slow_chunk(index, start, end):
                    race_path = f"{segment_files[index]}.race{threading.get_ident()}"
                    race_cancel = threading.Event()
                    with race_state_lock:
                        race_cancel_events[index].append(race_cancel)
                        active_races["count"] += 1
                        active_races["total"] += 1
                    local_errors = []
                    try:
                        ok = _download_segment(
                            dl, dl.url, race_path, start, end, update_q,
                            local_errors, threading.Lock(), max_segment_retries=0,
                            chunk_index=None, chunk_cancel_event=race_cancel,
                            count_progress=False, activity_callback=None,
                            response_callback=lambda resp, idx=index, path=race_path: register_response(idx, path, resp))
                        changed = [str(err) for err in local_errors if str(err).startswith("RESOURCE_CHANGED:")]
                        if changed:
                            resource_changed_event.set()
                            with thread_errors_lock:
                                thread_errors.extend((index, err) for err in changed)
                            primary_cancel_events[index].set()
                            with race_state_lock:
                                for event in race_cancel_events[index]:
                                    event.set()
                        if ok:
                            publish_winner(index, race_path)
                        elif os.path.exists(race_path):
                            try: os.remove(race_path)
                            except OSError: pass
                    finally:
                        with race_state_lock:
                            active_races["count"] = max(0, active_races["count"] - 1)

                def endgame_monitor():
                    while not race_stop.wait(0.35):
                        if dl.stop_event and dl.stop_event.is_set():
                            return
                        if dl.pause_event is not None and not dl.pause_event.is_set():
                            continue
                        app_config = getattr(getattr(dl, "app", None), "config", {})
                        try:
                            intentionally_limited = (safe_int(app_config.get("speed_limit", 0), 0) > 0
                                or bool(app_config.get("slow_mode_enabled", False))
                                or (getattr(dl, "speed_limit_override", None) is not None
                                    and safe_int(getattr(dl, "speed_limit_override", 0), 0) > 0))
                        except Exception:
                            intentionally_limited = False
                        if intentionally_limited:
                            continue
                        if not work_queue.empty():
                            continue
                        with race_state_lock:
                            if (not active_chunks and controller.active_workers == 0
                                    and active_races["count"] == 0):
                                return
                            if active_races["count"] >= max(1, min(2, worker_cap // 4 or 1)):
                                continue
                            if active_races["total"] >= max_races_total:
                                continue
                            now = time.monotonic()
                            candidates = [(idx, state) for idx, state in active_chunks.items()
                                          if idx not in raced_chunks
                                          and idx not in chunk_winners
                                          and now - state.get("last_progress", now) >= endgame_trigger]
                            if not candidates:
                                continue
                            idx, state = max(candidates, key=lambda pair: now - pair[1].get("last_progress", now))
                            start, end = state["start"], state["end"]
                            raced_chunks.add(idx)
                            thread = threading.Thread(target=race_slow_chunk, args=(idx, start, end),
                                                      name=f"CatsSoft-Endgame-{idx}", daemon=True)
                            race_threads.append(thread)
                        thread.start()

                def range_worker():
                    while not (dl.stop_event and dl.stop_event.is_set()):
                        if not controller.acquire(dl.stop_event, dl.pause_event):
                            return
                        task = None
                        try:
                            try:
                                task = work_queue.get_nowait()
                            except queue.Empty:
                                return
                            i, start, end = task
                            with race_state_lock:
                                active_chunks[i] = {"started": time.monotonic(),
                                                    "last_progress": time.monotonic(),
                                                    "bytes": 0, "start": start, "end": end}
                            local_errors = []
                            started = time.monotonic()
                            ok = _download_segment(
                                dl, dl.url, segment_files[i], start, end, update_q,
                                local_errors, threading.Lock(), chunk_index=i,
                                chunk_cancel_event=primary_cancel_events[i],
                                count_progress=True, activity_callback=range_progress,
                                response_callback=lambda resp, idx=i, path=segment_files[i]: register_response(idx, path, resp))
                            elapsed = max(0.001, time.monotonic() - started)
                            cancelled_by_winner = primary_cancel_events[i].is_set() and not ok
                            controller.observe(ok, elapsed, end - start + 1,
                                               cancelled=cancelled_by_winner or bool(dl.stop_event and dl.stop_event.is_set()))
                            if ok:
                                publish_winner(i, segment_files[i])
                            elif not (dl.stop_event and dl.stop_event.is_set()):
                                changed = [str(err) for err in local_errors if str(err).startswith("RESOURCE_CHANGED:")]
                                if changed:
                                    resource_changed_event.set()
                                    with thread_errors_lock:
                                        thread_errors.extend((i, err) for err in changed)
                                elif not cancelled_by_winner:
                                    with race_state_lock:
                                        race_has_winner = i in chunk_winners
                                    if not race_has_winner:
                                        with thread_errors_lock:
                                            thread_errors.extend((i, str(err)) for err in (local_errors or [f"Segment {start}-{end} failed"]))
                        finally:
                            if task is not None:
                                with race_state_lock:
                                    active_chunks.pop(task[0], None)
                                work_queue.task_done()
                            controller.release()

                monitor_thread = None
                if endgame_enabled and max_races_total > 0 and total >= 16 * mib and worker_cap >= 2:
                    monitor_thread = threading.Thread(target=endgame_monitor,
                                                      name="CatsSoft-Endgame-Monitor", daemon=True)
                    monitor_thread.start()

                threads = [threading.Thread(target=range_worker, name=f"CatsSoft-Range-{i}", daemon=True)
                           for i in range(worker_cap)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                # Give any endgame racers time to finish/cancel before choosing winners.
                if monitor_thread is not None:
                    deadline = time.monotonic() + max(3.0, endgame_trigger + 2.0)
                    while time.monotonic() < deadline:
                        with race_state_lock:
                            races_left = active_races["count"]
                            pending_work = not work_queue.empty()
                            running_primary = controller.active_workers
                        if races_left == 0 and not pending_work and running_primary == 0:
                            break
                        time.sleep(0.1)
                    race_stop.set()
                    monitor_thread.join(timeout=1.0)
                with race_state_lock:
                    local_races = list(race_threads)
                for thread in local_races:
                    thread.join(timeout=max(2.0, endgame_trigger + 1.0))
                alive_races = [thread for thread in local_races if thread.is_alive()]
                if alive_races:
                    # Fail closed: never merge while a duplicate racer could still write.
                    with race_state_lock:
                        for events in race_cancel_events.values():
                            for event in events:
                                event.set()
                        open_responses = [resp for items in chunk_response_handles.values()
                                          for _owner_path, resp in items if resp is not None]
                    for response in open_responses:
                        try: response.close()
                        except Exception: pass
                    for thread in alive_races:
                        thread.join(timeout=3.0)
                    if any(thread.is_alive() for thread in local_races):
                        raise Exception("Endgame worker nu s-a oprit la timp; merge-ul a fost anulat pentru a proteja integritatea fișierului")

                if dl.stop_event and dl.stop_event.is_set():
                    update_q.put(("status", dl, DLStatus.CANCELLED))
                    return

                # A resource validator mismatch invalidates all persisted ranges before retry.
                if resource_changed_event.is_set() or any(
                        str(item[1] if isinstance(item, tuple) and len(item) > 1 else item).startswith("RESOURCE_CHANGED:")
                        for item in thread_errors):
                    _remove_segment_parts(dl.save_path)
                    _remove_download_meta(dl.save_path)
                    dl.resume_validator = ""
                    with dl.lock:
                        dl.downloaded = 0
                        dl.progress = 0.0
                    raise DownloadResourceChangedError("Serverul a schimbat resursa; segmentele vechi au fost eliminate")

                # Ignore failures from a primary stream if its endgame duplicate won.
                with thread_errors_lock:
                    effective_errors = []
                    for item in thread_errors:
                        if isinstance(item, tuple) and len(item) == 2:
                            idx, message = item
                            if isinstance(idx, int) and idx in chunk_winners:
                                continue
                            effective_errors.append(str(message))
                        else:
                            effective_errors.append(str(item))
                if effective_errors:
                    raise Exception(f"Segment download failed: {effective_errors[0]}")

                chosen_segments = []
                for index, original_path in enumerate(segment_files):
                    chosen = chunk_winners.get(index, original_path)
                    if not os.path.isfile(chosen):
                        raise IOError(f"Segmentul {index + 1} lipsește după transfer")
                    chosen_segments.append(chosen)
                    # Remove losing copies only after all primary/race writers are done.
                    if original_path != chosen and os.path.exists(original_path):
                        try: os.remove(original_path)
                        except OSError: pass
                    for race_path in Path(os.path.dirname(original_path) or ".").glob(os.path.basename(original_path) + ".race*"):
                        if str(race_path) != chosen:
                            try: race_path.unlink()
                            except OSError: pass

                # Endgame racers intentionally do not increment logical progress while
                # downloading duplicate bytes. Reconcile from the winning on-disk ranges.
                # This also removes any partial-byte accounting from a cancelled primary.
                actual_segment_bytes = 0
                for chosen in chosen_segments:
                    actual_segment_bytes += os.path.getsize(chosen)
                with dl.lock:
                    dl.downloaded = actual_segment_bytes
                    dl.progress = min(99.9, actual_segment_bytes / total * 100.0) if total > 0 else 0.0
                    if dl.chunk_progress:
                        dl.chunk_progress = [1.0 for _ in range(len(segment_files))]
                update_q.put(("progress", dl, None))

                _merge_segments(dl.save_path, chosen_segments, expected_total=total)

                # Validate the final file before setting progress to 100%.
                if os.path.exists(dl.save_path):
                    actual_size = os.path.getsize(dl.save_path)
                    if total > 0 and actual_size != total:
                        logger.error("Segment mismatch: expected %s, got %s", total, actual_size)
                        # Atomic merge retains source ranges if it fails; do not trust a bad final file.
                        try: os.remove(dl.save_path)
                        except OSError: pass
                        raise Exception(f"Incomplete download: {actual_size}/{total} bytes")
                    with dl.lock:
                        dl.downloaded = actual_size
                else:
                    raise Exception("Merged file not found")
            else:
                logger.info("Fișierul final există deja complet (%s bytes) - sar peste re-descărcare", total)
                with dl.lock:
                    dl.downloaded = total
                    dl.chunk_size_bytes = total
                    dl.chunk_progress = [1.0]

            # Verificare checksum
            if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
                dl.error_msg = "Checksum verification failed"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FEATURE NOU: scanare antivirus opțională (Windows Defender)
            if dl.app and dl.app.config.get("av_scan", False):
                scan = scan_with_defender(dl.save_path)
                if scan["ran"] and not scan["clean"]:
                    logger.warning(f"Scanare Defender a semnalat ceva la {dl.filename}: {scan['output'][:200]}")
                    update_q.put(("scan_warning", dl, scan))
                if not os.path.exists(dl.save_path):
                    logger.warning(f"{dl.filename} nu mai există după scanare (posibil carantinat)")
                    dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

            # Auto-sort
            metadata_source_path = dl.save_path
            if dl.status != DLStatus.CANCELLED and dl.app:
                filetype = guess_filetype(dl.url)
                new_path = auto_sort_file(dl.save_path, filetype, dl.app.config.get("auto_sort", True))
                dl.save_path = new_path
                update_q.put(("refresh", dl, None))

            # FIX: Setează progress = 100% DOAR DUPĂ toate verificările
            with dl.lock:
                dl.progress = 100.0
                dl.speed = 0.0
                dl.completed_date = datetime.now()

            _remove_download_meta(dl.save_path)
            if metadata_source_path != dl.save_path:
                _remove_download_meta(metadata_source_path)
            update_q.put(("status", dl, DLStatus.COMPLETED))  # Status final după verificări
            return

        except Exception as e:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            logger.error(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt == max_retries - 1:
                dl.error_msg = str(e)
                update_q.put(("status", dl, DLStatus.ERROR))
            else:
                with dl.lock:
                    dl.retry_count += 1
                    dl.speed = 0.0
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                # Backoff conștient de anulare: utilizatorul nu trebuie să aștepte
                # până la 4 secunde ca butonul Stop să producă un efect vizibil.
                deadline = time.monotonic() + (2 ** attempt)
                while time.monotonic() < deadline:
                    if dl.stop_event and dl.stop_event.is_set():
                        update_q.put(("status", dl, DLStatus.CANCELLED))
                        return
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
# ── orig:4472 ──

def _complete_existing_file(dl: Download, update_q: queue.Queue) -> bool:
    """Finalize a verified-size file recovered from disk without requesting bytes=total- (HTTP 416)."""
    try:
        # Doar un fișier DOVEDIT a fiind resursa curentă poate fi raportat
        # complet fără transfer. Altfel, un fișier orfan de aceeași dimensiune
        # ar fi declarat descărcat, iar conținutul lui nu are nimic de-a face
        # cu ceea ce s-a cerut.
        if not getattr(dl, "final_verified", False):
            return False
        if not dl.total or not os.path.isfile(dl.save_path):
            return False
        actual_size = os.path.getsize(dl.save_path)
        if actual_size != int(dl.total):
            return False
    except OSError:
        return False

    if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
        dl.error_msg = "Checksum verification failed"
        update_q.put(("status", dl, DLStatus.ERROR))
        return True

    if dl.app and dl.app.config.get("av_scan", False):
        scan = scan_with_defender(dl.save_path)
        if scan["ran"] and not scan["clean"]:
            logger.warning("Scanare Defender a semnalat ceva la %s: %s", dl.filename, scan["output"][:200])
            update_q.put(("scan_warning", dl, scan))
        if not os.path.exists(dl.save_path):
            dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
            update_q.put(("status", dl, DLStatus.ERROR))
            return True

    metadata_source_path = dl.save_path
    if dl.app:
        new_path = auto_sort_file(dl.save_path, guess_filetype(dl.url), dl.app.config.get("auto_sort", True))
        dl.save_path = new_path
        if new_path != metadata_source_path:
            update_q.put(("refresh", dl, None))

    with dl.lock:
        dl.downloaded = actual_size
        dl.progress = 100.0
        dl.speed = 0.0
        dl.completed_date = datetime.now()
        if dl.chunk_progress:
            dl.chunk_progress = [1.0 for _ in dl.chunk_progress]
        else:
            dl.chunk_progress = [1.0]

    _remove_download_meta(dl.save_path)
    if metadata_source_path != dl.save_path:
        _remove_download_meta(metadata_source_path)
    update_q.put(("progress", dl, None))
    update_q.put(("status", dl, DLStatus.COMPLETED))
    logger.info("Fișier complet recuperat de pe disc: %s", dl.filename)
    return True
# ── orig:4530 ──


def download_simple(dl: Download, update_q: queue.Queue, max_retries: int = 3):
    """Download simplu cu reluare sigură și rutare opțională printr-un adaptor selectat."""
    manager = getattr(getattr(dl, "app", None), "network_manager", None)
    # True după ce am scris singuri octeți în fișier: de atunci conținutul de pe
    # disc ne aparține și poate fi reluat în siguranță.
    wrote_own_data = False
    for attempt in range(max_retries):
        network_key, http_session = manager.acquire_session() if manager is not None else ("system", SESSION)
        try:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            if attempt == 0 and _complete_existing_file(dl, update_q):
                return
            if attempt > 0:
                if (dl.supports_resume and os.path.isfile(dl.save_path)
                        and (wrote_own_data or not dl.resume_blocked)):
                    # Continue from the bytes on disk after a transient network failure.
                    try:
                        actual_partial = os.path.getsize(dl.save_path)
                    except OSError:
                        actual_partial = 0
                    with dl.lock:
                        dl.downloaded = actual_partial
                        dl.progress = (min(99.9, actual_partial / dl.total * 100.0)
                                       if dl.total > 0 else 0.0)
                else:
                    with dl.lock:
                        dl.downloaded = 0
                        dl.progress = 0.0
                    if os.path.exists(dl.save_path):
                        try: os.remove(dl.save_path)
                        except OSError: pass
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                with dl.lock:
                    dl.speed = 0.0
            else:
                # CONTORUL POATE FI VECHII: valoarea vine din baza de date (coadă
                # restaurată), iar fișierul poate fi fost trunchiat, înlocuit sau
                # șters între timp. Offset-ul Range trebuie să fie întotdeauna
                # numărul REAL de octeți de pe disc: altfel serverul umple
                # intervalul greșit, iar verificarea finală de dimensiune trece
                # oricum - fișier raportat COMPLET, cu primii octeți lipsă și
                # ultimii duplicați.
                on_disk = _reconcile_downloaded_with_disk(dl)
                if on_disk > 0:
                    logger.info(f"Reluare download simplu de la {on_disk} bytes (fișier existent)")

            requested_resume = dl.downloaded > 0 and dl.supports_resume
            # Accept-Encoding: identity - cerem octeții brute. Altfel, un server
            # care comprimă răspunsul trimite Content-Length pentru forma
            # COMPRIMATĂ, iar fișierul scris (decodat) nu mai corespunde: verificarea
            # finală de dimensiune eșuează sau, mai rău, reluarea concatenează
            # octeți comprimați peste octeți decomprimați.
            headers = {"Accept-Encoding": "identity"}
            if requested_resume:
                headers["Range"] = f"bytes={dl.downloaded}-"
                if getattr(dl, "resume_validator", ""):
                    headers["If-Range"] = dl.resume_validator

            with http_session.get(dl.url, stream=True, timeout=(8, 25), headers=headers) as resp:
                # Răspunsul activ e păstrat ca să poată fi închis imediat la
                # Stop: altfel anularea aștepta până la timeout-ul de citire
                # (25 s) înainte să aibă efect vizibil.
                dl.active_response = resp
                resp.raise_for_status()

                # FIX: dacă am cerut reluare (Range) dar serverul a ignorat-o și a trimis
                # fișierul întreg (200 în loc de 206), NU putem continua în modul append -
                # ar rezulta un fișier corupt (bytes vechi + fișier întreg concatenate).
                if requested_resume and resp.status_code != 206:
                    logger.warning("Serverul nu a onorat cererea de reluare (Range) - repornesc de la 0")
                    with dl.lock:
                        dl.downloaded = 0
                elif requested_resume:
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                    if not match or int(match.group(1)) != dl.downloaded:
                        raise requests.RequestException(
                            f"Serverul a răspuns cu Content-Range invalid la reluare: {content_range or 'lipsește'}")

                final_url = resp.url
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                # A server may ignore If-Range and return a fresh full response; status handling above
                # resets downloaded=0 so the file is overwritten, never concatenated.
                new_validator = str(resp.headers.get("ETag") or resp.headers.get("Last-Modified") or "").strip()
                if (requested_resume and resp.status_code == 206 and dl.resume_validator
                        and new_validator and new_validator != dl.resume_validator):
                    raise DownloadResourceChangedError("Resursa s-a schimbat; reluarea vechiului fișier este nesigură")
                if new_validator:
                    dl.resume_validator = new_validator
                if final_url != dl.url:
                    logger.info(f"Redirect detected: {dl.url} -> {final_url}")

                # FIX: consolidat pe filename_from_url (elimina regex duplicat + funcție redundantă)
                suggested_name = filename_from_url(dl.url, response_headers, final_url)
                if (suggested_name and suggested_name != dl.filename and not dl.filename_locked
                        and not _has_resume_artifacts(dl.save_path)):
                    logger.info(f"Updating filename: {dl.filename} -> {suggested_name}")
                    proposed_path = os.path.join(os.path.dirname(dl.save_path), sanitize_filename(suggested_name))
                    if os.path.abspath(proposed_path) != os.path.abspath(dl.save_path):
                        old_save_path = dl.save_path
                        if os.path.exists(proposed_path):
                            proposed_path = unique_target_path(proposed_path)
                        dl.save_path = proposed_path
                        dl.filename = os.path.basename(proposed_path)
                        _remove_download_meta(old_save_path)
                        if dl.app:
                            update_q.put(("refresh", dl, None))

                response_length = int(resp.headers.get("content-length", 0))
                total = response_length + dl.downloaded
                if resp.status_code == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                    if match and match.group(3) != "*":
                        total = int(match.group(3))
                # Un răspuns comprimat nu poate fi dimensionat prin Content-Length:
                # lungimea anunțată este cea a formei comprimate, nu a octeților
                # scriși efectiv în fișier.
                content_encoding = str(resp.headers.get("Content-Encoding", "") or "").strip().lower()
                if content_encoding and content_encoding != "identity":
                    total = 0
                    logger.warning("Serverul a trimis %s; dimensiunea finală este necunoscută",
                                   content_encoding)

                _write_download_meta(dl.save_path, {
                    "url": dl.url,
                    "total": int(total or 0),
                    "etag": str(resp.headers.get("ETag") or ""),
                    "last_modified": str(resp.headers.get("Last-Modified") or ""),
                    "updated_at": datetime.now().isoformat(),
                })
                dl.total = total
                with dl.lock:
                    dl.chunk_size_bytes = 0
                    dl.chunk_progress = [min(1.0, dl.downloaded / total) if total else 0.0]

                # FIX: Dacă total = 0, nu putem calcula progress corect
                if total == 0:
                    logger.warning("Content-Length is 0, progress will show downloaded bytes only")

                # FEATURE NOU: verificare spațiu disc (doar partea rămasă de descărcat)
                remaining = max(0, total - dl.downloaded)
                if remaining > 0 and not check_disk_space(dl.save_path, remaining):
                    dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

                os.makedirs(os.path.dirname(dl.save_path) or ".", exist_ok=True)
                mode = "ab" if dl.downloaded > 0 else "wb"

                resp.raw.decode_content = True
                chunk_size = 32768
                last_update = time.time()
                update_interval = 0.5

                with open(dl.save_path, mode) as f:
                    wrote_own_data = True
                    last_space_check = time.time()
                    while True:
                        if dl.stop_event and dl.stop_event.is_set():
                            update_q.put(("status", dl, DLStatus.CANCELLED))
                            return
                        if dl.pause_event:
                            # Așteptare în trepte: permite oprirea imediată în loc
                            # să blocheze firul pe un wait() fără timeout.
                            while not dl.pause_event.wait(timeout=0.25):
                                if dl.stop_event and dl.stop_event.is_set():
                                    update_q.put(("status", dl, DLStatus.CANCELLED))
                                    return
                        if dl.stop_event and dl.stop_event.is_set():
                            return
                        chunk = resp.raw.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)

                        with dl.lock:
                            dl.downloaded += len(chunk)
                            downloaded_now = dl.downloaded
                            if dl.chunk_progress:
                                dl.chunk_progress[0] = min(1.0, downloaded_now / total) if total else 0.0
                            now = time.time()
                            should_update = (now - last_update >= update_interval)

                        # Dimensiunea necunoscută (răspuns chunked) nu permite o
                        # verificare de spațiu înainte de transfer: o facem periodic
                        # în timpul descărcării, ca un fișier uriaș să nu umple
                        # discul până la capăt.
                        if total <= 0 and now - last_space_check >= 15:
                            last_space_check = now
                            try:
                                free = shutil.disk_usage(os.path.dirname(dl.save_path) or ".").free
                            except OSError:
                                free = None
                            if free is not None and free < 50 * 1024 * 1024:
                                dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                                update_q.put(("status", dl, DLStatus.ERROR))
                                return

                        _record_session_transfer(dl, len(chunk))
                        if manager is not None:
                            manager.report_bytes(network_key, len(chunk))
                        if should_update:
                            dl.speed_tracker.update(downloaded_now)
                            speed_now = dl.speed_tracker.get_speed()

                            with dl.lock:
                                dl.speed = speed_now
                                if total > 0:
                                    dl.progress = min(99.9, (downloaded_now / total) * 100.0)
                                else:
                                    dl.progress = 0.0
                                chunk_size = adaptive_chunk_size(speed_now)

                            update_q.put(("progress", dl, None))
                            last_update = now

                        _apply_speed_limit(dl, len(chunk), network_key)

                        # Notă: blocul de raportare de mai sus (should_update) este
                        # singurul responsabil de actualizare; un al doilea bloc
                        # identic exista aici și dubla evenimentele "progress".


            # FIX: Verifică dimensiunea reală a fișierului
            actual_size = os.path.getsize(dl.save_path)
            if total > 0 and actual_size != total:
                raise IOError(f"Descărcare incompletă: {actual_size}/{total} bytes")
            with dl.lock:
                dl.downloaded = actual_size
                if total <= 0:
                    dl.total = actual_size  # now known after clean EOF
                dl.progress = 100.0
                if dl.chunk_progress:
                    dl.chunk_progress[0] = 1.0
                dl.speed = 0.0

            update_q.put(("progress", dl, None))  # Final progress update

            if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
                dl.error_msg = "Checksum verification failed"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FEATURE NOU: scanare antivirus opțională (Windows Defender)
            if dl.app and dl.app.config.get("av_scan", False):
                scan = scan_with_defender(dl.save_path)
                if scan["ran"] and not scan["clean"]:
                    logger.warning(f"Scanare Defender a semnalat ceva la {dl.filename}: {scan['output'][:200]}")
                    update_q.put(("scan_warning", dl, scan))
                if not os.path.exists(dl.save_path):
                    logger.warning(f"{dl.filename} nu mai există după scanare (posibil carantinat)")
                    dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

            metadata_source_path = dl.save_path
            if dl.app:
                filetype = guess_filetype(dl.url)
                new_path = auto_sort_file(dl.save_path, filetype, dl.app.config.get("auto_sort", True))
                dl.save_path = new_path
                update_q.put(("refresh", dl, None))

            dl.completed_date = datetime.now()
            _remove_download_meta(dl.save_path)
            if metadata_source_path != dl.save_path:
                _remove_download_meta(metadata_source_path)
            update_q.put(("status", dl, DLStatus.COMPLETED))
            return

        except Exception as e:
            if manager is not None and isinstance(e, (requests.ConnectionError, requests.Timeout, OSError, TimeoutError)):
                manager.report_failure(network_key, e)
            if isinstance(e, DownloadResourceChangedError):
                try:
                    if os.path.isfile(dl.save_path): os.remove(dl.save_path)
                except OSError:
                    pass
                _remove_download_meta(dl.save_path)
                dl.downloaded = 0
                dl.resume_validator = ""
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            logger.error(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt == max_retries - 1:
                dl.error_msg = str(e)
                update_q.put(("status", dl, DLStatus.ERROR))
            else:
                with dl.lock:
                    dl.retry_count += 1
                time.sleep(2 ** attempt)
        finally:
            if manager is not None:
                manager.release_session(network_key)
# ── orig:4829 ──

def probe_range(url):
    """Probează Range și, dacă serverul răspunde 206, extrage dimensiunea totală."""
    try:
        with SESSION.get(url, headers={"Range": "bytes=0-0"}, timeout=8, stream=True) as response:
            if response.status_code != 206:
                return False, 0, dict(response.headers), response.url
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+0-0/(\d+|\*)", content_range, re.IGNORECASE)
            total = int(match.group(1)) if match and match.group(1) != "*" else 0
            return bool(match), total, dict(response.headers), response.url
    except Exception as exc:
        logger.debug("Range probe failed for %s: %s", url, exc)
        return False, 0, {}, url
# ── orig:4844 ──


def supports_range(url):
    supported, _total, _headers, _final_url = probe_range(url)
    return supported
# ── orig:4848 ──

def _download_segment(dl, url, segment_path, start, end, update_q, errors_list, errors_lock,
                       max_segment_retries: int = 2,
                       chunk_index: Optional[int] = None,
                       chunk_cancel_event: Optional[threading.Event] = None,
                       count_progress: bool = True,
                       activity_callback: Optional[Callable] = None,
                       response_callback: Optional[Callable] = None):
    """Download one HTTP byte range, with safe resume, per-network routing and race cancellation.

    ``count_progress=False`` is used only by endgame racers: duplicate bytes still count
    toward network/session usage, but never inflate the file's logical progress.
    """
    manager = getattr(getattr(dl, "app", None), "network_manager", None)
    for attempt in range(max_segment_retries + 1):
        segment_bytes_written = 0
        resume_from = 0
        resume_counted = False
        network_key, http_session = manager.acquire_session() if manager is not None else ("system", SESSION)
        attempt_started = time.monotonic()
        tracked_response = None
        try:
            if dl.stop_event and dl.stop_event.is_set():
                return False
            if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                return False

            # Reluarea funcționează la fiecare încercare, nu doar la prima: altfel o
            # eroare de rețea tranzitorie relua segmentul de la zero și arunca munca
            # deja validată pe disc.
            if os.path.exists(segment_path):
                try:
                    existing_size = os.path.getsize(segment_path)
                except OSError:
                    existing_size = 0
                expected_size = end - start + 1
                if existing_size == expected_size:
                    if count_progress:
                        with dl.lock:
                            dl.downloaded += existing_size
                            if chunk_index is not None:
                                while len(dl.chunk_progress) <= chunk_index:
                                    dl.chunk_progress.append(0.0)
                                dl.chunk_progress[chunk_index] = 1.0
                        # Octeți găsiți pe disc, nu transferați acum: re-basăm
                        # trackerul, altfel saltul ar apărea ca viteză de transfer.
                        dl.speed_tracker.rebase(dl.downloaded)
                    return True
                if 0 < existing_size < expected_size:
                    resume_from = existing_size
                elif existing_size > expected_size:
                    # Segment supradimensionat (ex. împărțire anterioară diferită):
                    # reluăm curat, altfel am concatena octeți din alt interval.
                    try:
                        os.remove(segment_path)
                    except OSError:
                        pass

            if count_progress and chunk_index is not None:
                with dl.lock:
                    while len(dl.chunk_progress) <= chunk_index:
                        dl.chunk_progress.append(0.0)
                    dl.chunk_progress[chunk_index] = resume_from / max(1, end - start + 1)

            range_start = start + resume_from
            headers = {"Range": f"bytes={range_start}-{end}",
                       # Octeți brute: un răspuns comprimat ar decodifica în
                       # segment un conținut care nu corespunde intervalului cerut.
                       "Accept-Encoding": "identity"}
            # If-Range se trimite DOAR când reutilizăm efectiv octeți de pe disc.
            # Trimis și la o cerere de la zero, un server care răspunde 200 în loc de
            # 206 (ETag diferit între HEAD și GET) ar face aplicația să declare
            # "resursa s-a schimbat" și să șteargă toate segmentele valide.
            if resume_from > 0 and getattr(dl, "resume_validator", ""):
                headers["If-Range"] = dl.resume_validator
            file_mode = "ab" if resume_from > 0 else "wb"

            # Keep a bounded read timeout so cancellation/endgame races do not leave
            # worker threads writing to staging files long after a winner was selected.
            read_timeout = 7 if not count_progress else 15
            with http_session.get(url, stream=True, headers=headers, timeout=(8, read_timeout)) as resp:
                # Răspunsul e înregistrat ca să poată fi închis imediat la Stop.
                # Fără asta, o citire blocată așteaptă timeout-ul de citire (15 s)
                # înainte ca anularea să aibă efect în calea segmentată.
                _track_active_response(dl, resp)
                tracked_response = resp
                if response_callback:
                    try:
                        response_callback(resp)
                    except Exception:
                        pass
                if resp.status_code != 206:
                    if resp.status_code == 200 and headers.get("If-Range"):
                        # Reluam octeți existenți, dar serverul a răspuns cu 200:
                        # validatorul nu se potrivește, deci resursa s-a schimbat.
                        raise DownloadResourceChangedError(
                            "Range/If-Range returned HTTP 200; the server resource may have changed")
                    # Fără If-Range un 200 înseamnă doar că serverul nu onorează
                    # Range - NU că resursa s-a schimbat. Nu ștergem segmentele valide.
                    raise requests.RequestException(
                        f"Serverul nu a acceptat cererea Range (HTTP {resp.status_code})")

                content_range = str(resp.headers.get("Content-Range", ""))
                match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                if not match or int(match.group(1)) != range_start or int(match.group(2)) != end:
                    raise requests.RequestException(f"Răspuns Content-Range invalid: {content_range or 'lipsește'}")

                response_validator = str(resp.headers.get("ETag") or resp.headers.get("Last-Modified") or "").strip()
                if (headers.get("If-Range") and response_validator
                        and response_validator != headers["If-Range"]):
                    raise DownloadResourceChangedError("Validatorul HTTP s-a schimbat în timpul reluării")

                resp.raw.decode_content = False
                # Dimensiunea blocului se adaptează la viteza reală: cu până la 16
                # segmente paralele, blocurile mici înseamnă milioane de apeluri de
                # sistem și de blocări pe dl.lock.
                chunk_size = max(64 * 1024, adaptive_chunk_size(dl.speed))
                bytes_since_retune = 0
                last_update = time.monotonic()
                update_interval = 0.35
                if count_progress and resume_from > 0:
                    with dl.lock:
                        dl.downloaded += resume_from
                    resume_counted = True
                    # Octeți reluați de pe disc: nu sunt viteză de transfer.
                    dl.speed_tracker.rebase(dl.downloaded)

                with open(segment_path, file_mode) as handle:
                    while True:
                        if dl.stop_event and dl.stop_event.is_set():
                            if count_progress:
                                with dl.lock:
                                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                            return False
                        if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                            if count_progress:
                                with dl.lock:
                                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                            return False
                        if dl.pause_event:
                            while not dl.pause_event.wait(timeout=0.25):
                                if dl.stop_event and dl.stop_event.is_set():
                                    if count_progress:
                                        with dl.lock:
                                            dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                                    return False
                                if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                                    if count_progress:
                                        with dl.lock:
                                            dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                                    return False

                        chunk = resp.raw.read(chunk_size)
                        if not chunk:
                            break
                        expected_size = end - range_start + 1
                        if segment_bytes_written + len(chunk) > expected_size:
                            raise requests.RequestException("Serverul a trimis mai mulți octeți decât intervalul solicitat")
                        handle.write(chunk)
                        segment_bytes_written += len(chunk)
                        bytes_since_retune += len(chunk)
                        if bytes_since_retune >= 4 * 1024 * 1024:
                            # Re-evaluează mărimea blocului o dată la 4 MiB.
                            bytes_since_retune = 0
                            chunk_size = max(64 * 1024, adaptive_chunk_size(dl.speed))
                        if manager is not None:
                            manager.report_bytes(network_key, len(chunk))
                        _record_session_transfer(dl, len(chunk))
                        if activity_callback and chunk_index is not None:
                            try: activity_callback(chunk_index, len(chunk))
                            except Exception: pass

                        with dl.lock:
                            if count_progress:
                                dl.downloaded += len(chunk)
                                downloaded_now = dl.downloaded
                                if chunk_index is not None:
                                    dl.chunk_progress[chunk_index] = min(
                                        1.0, (resume_from + segment_bytes_written) / max(1, end - start + 1))
                            else:
                                downloaded_now = dl.downloaded
                            now = time.monotonic()
                            should_update = (now - last_update >= update_interval)

                        if count_progress and should_update:
                            dl.speed_tracker.update(downloaded_now)
                            speed_now = dl.speed_tracker.get_speed()
                            with dl.lock:
                                dl.speed = speed_now
                                if dl.total > 0:
                                    dl.progress = min(99.9, downloaded_now / dl.total * 100.0)
                            update_q.put(("progress", dl, None))
                            last_update = now

                        _apply_speed_limit(dl, len(chunk), network_key)

            expected_size = end - start + 1
            if segment_bytes_written + resume_from != expected_size:
                raise requests.RequestException(
                    f"Segment incomplet: {segment_bytes_written + resume_from}/{expected_size} bytes")
            if count_progress and chunk_index is not None:
                with dl.lock:
                    dl.chunk_progress[chunk_index] = 1.0
            return True

        except DownloadResourceChangedError as exc:
            with errors_lock:
                errors_list.append(f"RESOURCE_CHANGED: {exc}")
            return False
        except Exception as exc:
            if count_progress and (segment_bytes_written or resume_counted):
                with dl.lock:
                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
            if manager is not None and isinstance(exc, (requests.ConnectionError, requests.Timeout, OSError, TimeoutError)):
                manager.report_failure(network_key, exc)
            if attempt < max_segment_retries and not (chunk_cancel_event and chunk_cancel_event.is_set()):
                with dl.lock:
                    dl.retry_count += 1
                    if count_progress and chunk_index is not None and chunk_index < len(dl.chunk_progress):
                        dl.chunk_progress[chunk_index] = 0.0
                logger.warning("Segment %s-%s retry %s/%s: %s", start, end, attempt + 1, max_segment_retries, exc)
                # Stop-aware backoff instead of an unconditional sleep.
                delay = 1.0 + attempt * 1.5
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if dl.stop_event and dl.stop_event.is_set():
                        return False
                    if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                        return False
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                continue
            logger.error("Segment download error %s-%s: %s", start, end, exc)
            with errors_lock:
                errors_list.append(str(exc))
            return False
        finally:
            if tracked_response is not None:
                _untrack_active_response(dl, tracked_response)
            if manager is not None:
                manager.release_session(network_key)
# ── orig:5089 ──


def _merge_segments(output_path, segment_files, expected_total: Optional[int] = None):
    """Atomically assemble all ranges; never remove source segments before replace succeeds."""
    temp_path = output_path + ".catssoft-merge.tmp"
    try:
        missing = [path for path in segment_files if not os.path.isfile(path)]
        if missing:
            raise IOError(f"Lipsesc {len(missing)} segmente; fișierul final nu va fi asamblat")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        try:
            with open(temp_path, "wb") as outfile:
                for segment_path in segment_files:
                    with open(segment_path, "rb") as infile:
                        shutil.copyfileobj(infile, outfile, length=1024 * 1024)
                outfile.flush()
                os.fsync(outfile.fileno())
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOSPC:
                # Mesaj explicit: altfel utilizatorul vede „No space left on
                # device" și nu află de ce, în condițiile în care chiar a fost
                # nevoie de încă o copie a întregului fișier.
                raise IOError(
                    "Spațiu insuficient pe disc pentru asamblarea segmentelor "
                    "(era nevoie de încă %s pentru fișierul final). "
                    "Eliberează spațiu și reia descărcarea - segmentele descărcate "
                    "sunt păstrate." % fmt_size(int(expected_total or 0))) from exc
            raise
        # Dimensiunea se verifică ÎNAINTE de înlocuirea fișierului final și de
        # ștergerea segmentelor sursă. Altfel, o îmbinare incompletă distruge
        # singura copie a datelor, iar reluarea trebuie să reia transferul de la
        # zero - exact ce nu trebuie să se întâmple la o eroare tranzitorie.
        if expected_total:
            merged_size = os.path.getsize(temp_path)
            if merged_size != int(expected_total):
                raise IOError(f"Îmbinare incompletă: {merged_size}/{int(expected_total)} bytes")
        os.replace(temp_path, output_path)
        for segment_path in segment_files:
            try:
                os.remove(segment_path)
            except OSError:
                logger.warning("Merged segment could not be removed: %s", segment_path)
    except Exception as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        # Păstrăm tipul erorii (de exemplu „discul plin" cu mesajul clar),
        # ca să poată fi recunoscută mai sus.
        if isinstance(exc, OSError):
            raise
        raise Exception(f"Error merging segments atomically: {exc}")
