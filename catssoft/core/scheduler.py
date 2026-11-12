"""core/scheduler — Descărcări programate."""

from tkinter import filedialog, messagebox
import threading
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta

from catssoft.core.database import DatabaseManager
from catssoft.core.models import ScheduledDownload
from catssoft.logging_setup import logger
# ── orig:3119 ──

# ─── Download Scheduler ───────────────────────────────────────────────────────
class DownloadScheduler:
    def __init__(self, app, db: DatabaseManager):
        self.app = app
        self.db = db
        self.scheduled: List[ScheduledDownload] = []
        self.running = False
        self.thread = None
        # Numărul de încercări eșuate per programare, ca o eroare permanentă
        # să nu reîncerce la infinit la fiecare ciclu de 30s.
        self._failed_attempts = {}
        self._load_scheduled()

    def _load_scheduled(self):
        try:
            self.scheduled = self.db.get_scheduled()
            logger.info(f"[Scheduler] Încărcate {len(self.scheduled)} descărcări programate")
        except Exception as e:
            logger.error(f"[Scheduler] Eroare la încărcare: {e}")
            self.scheduled = []

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._check_scheduler, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def add(self, url: str, filename: str, schedule_time: datetime, save_path: str = "",
            repeat: str = "once", segments: int = 1, checksum: str = ""):
        sd = ScheduledDownload(url=url, filename=filename, scheduled_time=schedule_time,
            save_path=save_path, repeat=repeat, enabled=True, segments=segments, checksum=checksum)
        self.scheduled.append(sd)
        self.db.add_scheduled(sd)
        logger.info(f"[Scheduler] Adăugat: {filename} la {schedule_time}")

    def remove(self, sd: ScheduledDownload):
        if sd in self.scheduled: self.scheduled.remove(sd)
        self.db.remove_scheduled(sd)

    def _check_scheduler(self):
        while self.running:
            try:
                now = datetime.now()
                for sd in self.scheduled[:]:
                    if sd.enabled and now >= sd.scheduled_time:
                        logger.info(f"[Scheduler] Executare: {sd.filename}")
                        if sd.repeat == "once":
                            # Marcăm în memorie, dar NU ștergem rândul din baza de
                            # date: rândul dispare doar după ce transferul a fost
                            # acceptat. Altfel o eroare (disc plin, cale invalidă)
                            # pierdea programarea definitiv.
                            sd.enabled = False
                        self.app.post_ui(lambda x=sd: self._execute_download(x))
                        if sd.repeat in ("daily", "weekly"):
                            previous_time = sd.scheduled_time
                            interval = timedelta(days=1) if sd.repeat == "daily" else timedelta(weeks=1)
                            # If the app was closed for several intervals, advance to the next
                            # future occurrence rather than replaying an overdue schedule repeatedly.
                            next_time = previous_time + interval
                            while next_time <= now:
                                next_time += interval
                            sd.scheduled_time = next_time
                            self.db.update_scheduled_time(sd, previous_time)
                time.sleep(30)
            except Exception as e:
                logger.error(f"[Scheduler] Eroare: {e}")
                time.sleep(30)

    def _execute_download(self, sd: ScheduledDownload):
        folder = sd.save_path or self.app.config.get("default_folder")
        key = (sd.url, sd.filename, sd.save_path)
        try:
            self.app._add_download(sd.url, folder, sd.filename, sd.segments, sd.checksum)
        except Exception as exc:
            logger.error("[Scheduler] Nu s-a putut porni descărcarea programată %s: %s", sd.filename, exc)
            attempts = self._failed_attempts.get(key, 0) + 1
            self._failed_attempts[key] = attempts
            if attempts >= 3:
                logger.error("[Scheduler] Programarea %s a eșuat de %d ori; se dezactivează.",
                             sd.filename, attempts)
                sd.enabled = False
                self.remove(sd)
            else:
                # Reactivăm pentru următorul ciclu: rândul a rămas în baza de date.
                sd.enabled = True
            self.app.post_ui(lambda x=sd, e=exc: self._notify_schedule_failure(x, e))
            return
        self._failed_attempts.pop(key, None)
        # Abia acum ștergem programarea unică: transferul a fost acceptat.
        if sd.repeat == "once":
            self.remove(sd)

    def _notify_schedule_failure(self, sd: ScheduledDownload, exc: Exception):
        try:
            messagebox.showwarning(
                "Descărcare programată",
                f"Descărcarea programată „{sd.filename}” nu a putut porni:\n\n{exc}",
                parent=self.app)
        except Exception:
            logger.exception("Nu s-a putut afișa avertismentul pentru programare")
