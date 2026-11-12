"""core/database — Baza de date SQLite (istorik, coadă, setări, export)."""

import threading
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta
import sqlite3
import json

from catssoft.core.models import DLStatus, Download, DownloadHistory, ScheduledDownload
from catssoft.logging_setup import logger
from catssoft.utils.files import csv_safe_cell, default_database_path
from catssoft.utils.formatting import safe_int
# ── orig:2265 ──

class _ExportCancelled(Exception):
    """Raised internally when a history export is cancelled by the user."""
# ── orig:2270 ──


# ─── Database Manager (SQLite) ────────────────────────────────────────────────
class DatabaseManager:
    """SQLite wrapper that reuses one connection per thread.

    Opening a connection for every single query is expensive: each call re-reads the
    schema and re-locks the database file. A thread-local, long-lived connection keeps
    the same semantics (each ``with conn`` block still commits or rolls back) while
    removing that overhead from every settings read and every history page.
    """

    def __init__(self, db_path=None):
        self.db_path = os.path.abspath(db_path or default_database_path())
        self._local = threading.local()
        self.degraded_mode = False
        # Liste de perechi (fir_proprietar, conexiune). Perechea e necesară ca să
        # putem închide conexiunile firurilor care s-au terminat: aplicația
        # creează fire scurte (fiecare căutare în istoric, fiecare export) și,
        # fără această curățare, fiecare ar lăsa un handle SQLite deschis până
        # la ieșirea aplicației.
        self._connections = []
        self._connections_lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._init_db()
        except (sqlite3.Error, OSError) as exc:
            # Baza de date nu poate fi folosită (fișier blocat, corupt, lipsă de
            # permisiuni). Nu lăsăm aplicația să nu pornească: continuăm cu o
            # bază temporară, iar istoricul se pierde doar pentru această sesiune.
            logger.exception("Baza de date nu poate fi inițializată la %s: %s", self.db_path, exc)
            broken = getattr(self._local, "conn", None)
            if broken is not None:
                try:
                    broken.close()
                except sqlite3.Error:
                    pass
                self._local.conn = None
            fallback = os.path.join(tempfile.gettempdir(),
                                    f"catssoft_downloader_{os.getpid()}_{int(time.time())}.db")
            self.db_path = fallback
            self.degraded_mode = True
            self._init_db()

    @staticmethod
    def _connection_is_dead(conn) -> bool:
        """True dacă SQLite a marcat conexiunea ca fiind închisă."""
        if conn is None:
            return True
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            # "Cannot operate on a closed database."
            return True
        except sqlite3.Error:
            # Eroare tranzitorie (blocare, I/O): conexiunea e încă vie.
            return False
        return False

    def _connect(self, timeout: int = 10):
        """Return the calling thread's connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and self._connection_is_dead(conn):
            # close() poate fi apelat (de ex. la ieșirea aplicației) cât timp alt
            # fir mai folosește managerul. Conexiunea cache-uită devine atunci
            # inutilizabilă, așa că o recreăm transparent în loc să aruncăm
            # "Cannot operate on a closed database" în mijlocul operației.
            self._local.conn = None
            conn = None
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=timeout, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                # Fișierul bazei de date este corupt: conexiunea e inutilizabilă.
                # Nu o memorăm, ca fallback-ul să poată crea una nouă pe altă cale.
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                raise
            self._local.conn = conn
            # Fiecare fir creează propria conexiune; le ținem într-un registru ca
            # să putem închide toate la ieșire (altfel rămân deschise până la GC).
            with self._connections_lock:
                self._connections.append((threading.current_thread(), conn))
        try:
            # busy_timeout is the knob the `timeout` argument maps to; keep it in sync
            # so long-running exports keep their original tolerance for a locked file.
            conn.execute(f"PRAGMA busy_timeout={int(timeout) * 1000}")
        except sqlite3.Error:
            pass
        # Firele scurte (căutări/exporturi în istoric) lasă în urmă handle-uri
        # deschise; le închidem pe cele ale firurilor deja terminate.
        self._sweep_dead_connections()
        return conn

    def _sweep_dead_connections(self):
        """Close connections whose owner thread has already finished."""
        current = threading.current_thread()
        with self._connections_lock:
            stale = []
            alive = []
            for owner, connection in self._connections:
                if owner is not current and not owner.is_alive():
                    stale.append(connection)
                else:
                    alive.append((owner, connection))
            self._connections = alive
        for connection in stale:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def close(self):
        """Close every connection opened by this manager (one per thread)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        with self._connections_lock:
            connections = [connection for _, connection in self._connections]
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _init_db(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("""CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, status TEXT,
                total_size INTEGER, downloaded_size INTEGER, added_date TEXT,
                completed_date TEXT, speed_avg REAL)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, scheduled_time TEXT,
                repeat_type TEXT, enabled INTEGER, segments INTEGER, checksum TEXT)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS active_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, status TEXT,
                total_size INTEGER, downloaded_size INTEGER, segments INTEGER,
                checksum TEXT, checksum_algo TEXT, speed_limit INTEGER, added_date TEXT,
                priority INTEGER NOT NULL DEFAULT 1, queue_order INTEGER NOT NULL DEFAULT 0,
                speed_limit_override INTEGER, automatic_retry_count INTEGER NOT NULL DEFAULT 0,
                auto_retry_at REAL NOT NULL DEFAULT 0)""")

            # Existing user databases are migrated in-place. ALTER TABLE adds only missing columns.
            migrations = {
                "history": {
                    "save_path": "TEXT", "status": "TEXT", "total_size": "INTEGER",
                    "downloaded_size": "INTEGER", "added_date": "TEXT",
                    "completed_date": "TEXT", "speed_avg": "REAL",
                },
                "scheduled": {
                    "save_path": "TEXT", "scheduled_time": "TEXT", "repeat_type": "TEXT",
                    "enabled": "INTEGER DEFAULT 1", "segments": "INTEGER DEFAULT 1", "checksum": "TEXT DEFAULT ''",
                },
                "active_queue": {
                    "total_size": "INTEGER", "downloaded_size": "INTEGER",
                    "segments": "INTEGER DEFAULT 1", "checksum": "TEXT DEFAULT ''",
                    "checksum_algo": "TEXT DEFAULT 'sha256'", "speed_limit": "INTEGER DEFAULT 0",
                    "added_date": "TEXT", "priority": "INTEGER NOT NULL DEFAULT 1",
                    "queue_order": "INTEGER NOT NULL DEFAULT 0", "speed_limit_override": "INTEGER",
                    "automatic_retry_count": "INTEGER NOT NULL DEFAULT 0", "auto_retry_at": "REAL NOT NULL DEFAULT 0",
                },
            }
            for table, columns in migrations.items():
                existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, declaration in columns.items():
                    if column not in existing:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

            # History pagination indexes: avoid repeatedly sorting the complete history.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_order ON history(COALESCE(added_date, '') DESC, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_status_order ON history(status, COALESCE(added_date, '') DESC, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_filename_nocase ON history(filename COLLATE NOCASE)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_size ON history(downloaded_size DESC)")
            conn.commit()

    def add_history(self, dl: Download):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO history (url, filename, save_path, status, total_size,
                downloaded_size, added_date, completed_date, speed_avg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (dl.url, dl.filename, dl.save_path, dl.status.value, dl.total, dl.downloaded,
                 dl.added_date.isoformat(), dl.completed_date.isoformat() if dl.completed_date else None,
                 self._average_speed(dl)))
            conn.commit()

    @staticmethod
    def _average_speed(dl: Download) -> float:
        end = dl.completed_date or datetime.now()
        start = dl.started_at or dl.added_date
        elapsed = max(0.0, (end - start).total_seconds())
        if elapsed > 0 and dl.downloaded > 0:
            return float(dl.downloaded) / elapsed
        return float(getattr(dl, "speed", 0.0) or 0.0)

    def get_history(self, limit=100) -> List[DownloadHistory]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT id, url, filename, save_path, status, total_size,
                downloaded_size, added_date, completed_date, speed_avg
                FROM history ORDER BY COALESCE(added_date, '') DESC, id DESC LIMIT ?""",
                (safe_int(limit, 100, 1, 10000),))
            rows = cursor.fetchall()
        return [DownloadHistory(*row) for row in rows]

    @staticmethod
    def _history_where_params(search_term="", status_filter="toate",
                              period_filter="Toate", type_filter="Toate"):
        """Build parameterized filters shared by pagination and streaming export."""
        clauses = []
        params = []
        term = str(search_term or "").strip()
        if term:
            # Escape user-supplied SQL wildcard characters; preserve literal search semantics.
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append("(filename LIKE ? ESCAPE '\\' COLLATE NOCASE OR url LIKE ? ESCAPE '\\' COLLATE NOCASE)")
            params.extend((pattern, pattern))

        statuses = {
            "completate": "Completed", "completed": "Completed",
            "eșuate": "Error", "esuate": "Error", "error": "Error",
            "anulate": "Cancelled", "cancelled": "Cancelled",
        }
        status_value = statuses.get(str(status_filter or "toate").strip().lower())
        if status_value:
            clauses.append("status = ?")
            params.append(status_value)

        period = str(period_filter or "Toate").strip().lower()
        now = datetime.now()
        if period in ("astăzi", "astazi", "today"):
            clauses.append("substr(COALESCE(completed_date, added_date, ''), 1, 10) = ?")
            params.append(now.strftime("%Y-%m-%d"))
        elif period in ("ultimele 7 zile", "7 zile", "7 days"):
            clauses.append("COALESCE(completed_date, added_date, '') >= ?")
            params.append((now - timedelta(days=7)).isoformat(timespec="seconds"))
        elif period in ("ultimele 30 zile", "30 zile", "30 days"):
            clauses.append("COALESCE(completed_date, added_date, '') >= ?")
            params.append((now - timedelta(days=30)).isoformat(timespec="seconds"))

        type_value = str(type_filter or "Toate").strip().lower()
        extensions = {
            "video": (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".mpeg", ".mpg", ".m4v", ".ts", ".wmv"),
            "audio": (".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a", ".opus", ".wma", ".aiff"),
            "imagini": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".ico"),
            "arhive": (".zip", ".tar", ".gz", ".bz2", ".rar", ".7z", ".xz", ".tgz"),
            "documente": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".csv", ".md"),
        }
        aliases = {"images": "imagini", "image": "imagini", "archives": "arhive", "archive": "arhive",
                   "documents": "documente", "document": "documente"}
        type_value = aliases.get(type_value, type_value)
        suffix_expr = "lower(COALESCE(NULLIF(filename, ''), save_path, ''))"
        if type_value in extensions:
            checks = [f"{suffix_expr} LIKE ?" for _ in extensions[type_value]]
            clauses.append("(" + " OR ".join(checks) + ")")
            params.extend("%" + ext for ext in extensions[type_value])
        elif type_value in ("altele", "other"):
            all_exts = tuple(ext for items in extensions.values() for ext in items)
            checks = [f"{suffix_expr} LIKE ?" for _ in all_exts]
            clauses.append("NOT (" + " OR ".join(checks) + ")")
            params.extend("%" + ext for ext in all_exts)

        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _history_order_by(sort_order="Cele mai noi", prefix=""):
        """Whitelist SQL sort expressions; never interpolate arbitrary UI strings."""
        key = str(sort_order or "Cele mai noi").strip().lower()
        sort_map = {
            "cele mai noi": f"COALESCE({prefix}added_date, '') DESC, {prefix}id DESC",
            "cele mai vechi": f"COALESCE({prefix}added_date, '') ASC, {prefix}id ASC",
            "nume a–z": f"{prefix}filename COLLATE NOCASE ASC, {prefix}id DESC",
            "nume a-z": f"{prefix}filename COLLATE NOCASE ASC, {prefix}id DESC",
            "dimensiune ↓": f"COALESCE({prefix}downloaded_size, {prefix}total_size, 0) DESC, {prefix}id DESC",
            "dimensiune": f"COALESCE({prefix}downloaded_size, {prefix}total_size, 0) DESC, {prefix}id DESC",
            "status": f"COALESCE({prefix}status, '') COLLATE NOCASE ASC, COALESCE({prefix}added_date, '') DESC, {prefix}id DESC",
        }
        return sort_map.get(key, sort_map["cele mai noi"])

    def get_history_page(self, limit=25, offset=0, search_term="", status_filter="toate",
                         period_filter="Toate", type_filter="Toate", sort_order="Cele mai noi"):
        """Fetch one filtered/sorted page in SQLite; only the visible page reaches Tkinter."""
        limit = safe_int(limit, 25, 1, 100)
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
        where, params = self._history_where_params(search_term, status_filter, period_filter, type_filter)
        order_by = self._history_order_by(sort_order)
        select_sql = """SELECT id, url, filename, save_path, status, total_size,
            downloaded_size, added_date, completed_date, speed_avg FROM history"""
        with self._connect(15) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM history" + where, params).fetchone()[0]
            rows = conn.execute(
                select_sql + where + " ORDER BY " + order_by + " LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            conn.commit()
        return [DownloadHistory(*row) for row in rows], int(total or 0)

    def export_history_csv(self, file_path, mode="all", search_term="", status_filter="toate",
                           period_filter="Toate", type_filter="Toate", sort_order="Cele mai noi",
                           selected_ids=(), cancel_event=None, progress_callback=None):
        """Stream a complete CSV export in bounded batches; never materialize history in RAM."""
        cancel_event = cancel_event or threading.Event()
        mode = str(mode or "all").lower()
        selected_ids = tuple(dict.fromkeys(int(value) for value in (selected_ids or ()) if str(value).isdigit()))

        with self._connect(20) as conn:
            conn.execute("PRAGMA busy_timeout=20000")
            if mode == "selected":
                if not selected_ids:
                    return {"status": "empty", "count": 0}
                conn.execute("CREATE TEMP TABLE selected_history_export (id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT OR IGNORE INTO selected_history_export(id) VALUES (?)",
                                 ((item_id,) for item_id in selected_ids))
                from_sql = "history h JOIN selected_history_export sx ON sx.id = h.id"
                where, params = "", []
            elif mode == "filtered":
                where, params = self._history_where_params(search_term, status_filter, period_filter, type_filter)
                from_sql = "history h"
            else:
                where, params = "", []
                from_sql = "history h"

            total = int(conn.execute(f"SELECT COUNT(*) FROM {from_sql}{where}", params).fetchone()[0] or 0)
            if total <= 0:
                return {"status": "empty", "count": 0}

            order_by = self._history_order_by(sort_order, prefix="h.")
            query = f"""SELECT h.id, h.url, h.filename, h.save_path, h.status,
                h.total_size, h.downloaded_size, h.added_date, h.completed_date, h.speed_avg
                FROM {from_sql}{where} ORDER BY {order_by}"""
            written = 0
            cancelled = False
            # Scriem într-un fișier temporar și mutăm la locul final doar la
            # finalizare: altfel, un export anulat sau o eroare lăsa fișierul ales
            # de utilizator trunchiat (sau complet gol).
            temp_path = f"{file_path}.part"
            try:
                with open(temp_path, "w", encoding="utf-8-sig", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(["ID", "URL", "Filename", "Path", "Status", "Total bytes",
                                     "Downloaded bytes", "Added", "Completed", "Average speed (bytes/s)"])
                    cursor = conn.execute(query, params)
                    while True:
                        if cancel_event.is_set():
                            cancelled = True
                            break
                        rows = cursor.fetchmany(500)
                        if not rows:
                            break
                        writer.writerows([[csv_safe_cell(cell) for cell in row] for row in rows])
                        written += len(rows)
                        if progress_callback:
                            progress_callback(written, total)
                if cancelled:
                    raise _ExportCancelled()
                os.replace(temp_path, file_path)
            except _ExportCancelled:
                return {"status": "cancelled", "count": written, "total": total}
            finally:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
            return {"status": "completed", "count": written, "total": total}

    def get_history_stats(self):
        """Return all-history summary statistics without materializing every row in Python."""
        current_month = datetime.now().strftime("%Y-%m")
        recent_cutoff = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        with self._connect(10) as conn:
            row = conn.execute("""SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status = 'Completed' THEN COALESCE(downloaded_size, 0) ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN substr(COALESCE(completed_date, ''), 1, 7) = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'Error' AND COALESCE(added_date, '') >= ? THEN 1 ELSE 0 END), 0)
                FROM history""", (current_month, recent_cutoff)).fetchone()
        return {
            "total_files": int(row[0] or 0),
            "total_size": int(row[1] or 0),
            "this_month": int(row[2] or 0),
            "recent_errors": int(row[3] or 0),
        }

    def clear_history(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM history')
            conn.commit()

    def save_setting(self, key: str, value):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, json.dumps(value)))
            conn.commit()

    def save_settings_batch(self, items):
        """Persist many settings in a single transaction.

        Saving the whole configuration used to open one connection and commit once per
        key; a single executemany keeps the same result but makes the write atomic
        and roughly an order of magnitude cheaper.
        """
        payload = [(key, json.dumps(value)) for key, value in items]
        if not payload:
            return
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.executemany('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', payload)
            conn.commit()

    def get_setting(self, key: str, default=None):
        try:
            with self._connect(10) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
                row = cursor.fetchone()
            return json.loads(row[0]) if row else default
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Could not read setting %s; using default: %s", key, exc)
            return default

    def get_all_settings(self) -> dict:
        """Read every persisted setting with a single query.

        The settings screen has 26+ keys; reading them one by one meant 26
        SELECT statements (plus 26 busy_timeout PRAGMAs) on every start-up.
        Values that cannot be decoded are skipped, so the caller falls back to
        its own default - same behaviour as get_setting().
        """
        try:
            with self._connect(10) as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error as exc:
            logger.warning("Could not read settings; using defaults: %s", exc)
            return {}
        result = {}
        for key, raw in rows:
            try:
                result[key] = json.loads(raw)
            except (ValueError, TypeError):
                continue
        return result

    def add_scheduled(self, sd: ScheduledDownload):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO scheduled (url, filename, save_path, scheduled_time,
                repeat_type, enabled, segments, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(),
                 sd.repeat, 1 if sd.enabled else 0, sd.segments, sd.checksum))
            conn.commit()

    def update_scheduled_time(self, sd: ScheduledDownload, previous_time: datetime) -> None:
        """Persist the next recurrence on the existing row instead of duplicating it."""
        with self._connect(10) as conn:
            cursor = conn.execute(
                "UPDATE scheduled SET scheduled_time = ? "
                "WHERE url = ? AND filename = ? AND COALESCE(save_path, '') = ? AND scheduled_time = ? AND repeat_type = ?",
                (sd.scheduled_time.isoformat(), sd.url, sd.filename, sd.save_path,
                 previous_time.isoformat(), sd.repeat))
            if cursor.rowcount == 0:
                # Rândul nu a fost găsit (migrare sau editare manuală): păstrăm
                # recurența inserând un rând nou, în loc să o pierdem.
                logger.warning("[DB] Recurența nu a fost actualizată; se inserează un rând nou.")
                cursor.execute('''INSERT INTO scheduled (url, filename, save_path, scheduled_time,
                    repeat_type, enabled, segments, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(),
                     sd.repeat, 1 if sd.enabled else 0, sd.segments, sd.checksum))

    def get_scheduled(self) -> List[ScheduledDownload]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            # Coloane explicite: `SELECT *` depinde de ordinea fizică a coloanelor,
            # iar ALTER TABLE pe baze vechi mută datele la sfârșitul rândului —
            # programările salvate dispăreau după actualizarea aplicației.
            cursor.execute('SELECT url, filename, save_path, scheduled_time, repeat_type, '
                           'enabled, segments, checksum FROM scheduled WHERE enabled = 1')
            rows = cursor.fetchall()
        result = []
        for row in rows:
            try:
                result.append(ScheduledDownload(url=row[0], filename=row[1], save_path=row[2] or "",
                    scheduled_time=datetime.fromisoformat(row[3]), repeat=row[4] or "once",
                    enabled=bool(row[5]), segments=row[6] or 1, checksum=row[7] or ""))
            except Exception as e:
                logger.error(f"[DB] Eroare la parsare scheduled: {e}")
        return result

    def remove_scheduled(self, sd: ScheduledDownload):
        with self._connect(10) as conn:
            conn.execute(
                "DELETE FROM scheduled WHERE url = ? AND filename = ? AND save_path = ? "
                "AND scheduled_time = ? AND repeat_type = ?",
                (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(), sd.repeat))

    def save_active_queue(self, downloads: List["Download"]):
        """Persistă coada activă și setările Smart Queue într-o singură tranzacție."""
        rows = []
        for dl in downloads:
            with dl.lock:
                status = dl.status
                if status not in (DLStatus.QUEUED, DLStatus.DOWNLOADING, DLStatus.PAUSED):
                    continue
                rows.append((dl.url, dl.filename, dl.save_path, status.value,
                    max(0, int(dl.total or 0)), max(0, int(dl.downloaded or 0)),
                    max(1, safe_int(dl.segments, 1, 1, 32)), dl.checksum, dl.checksum_algo,
                    max(0, safe_int(dl.speed_limit, 0)), dl.added_date.isoformat(),
                    safe_int(dl.priority, 1, 0, 2), safe_int(dl.queue_order, 0, 0),
                    None if dl.speed_limit_override is None else max(0, safe_int(dl.speed_limit_override, 0)),
                    safe_int(getattr(dl, "automatic_retry_count", 0), 0, 0, 100),
                    max(0.0, float(getattr(dl, "auto_retry_at", 0.0) or 0.0))))
        with self._connect(10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute('DELETE FROM active_queue')
            conn.executemany("""INSERT INTO active_queue (
                url, filename, save_path, status, total_size, downloaded_size, segments,
                checksum, checksum_algo, speed_limit, added_date, priority, queue_order,
                speed_limit_override, automatic_retry_count, auto_retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            conn.commit()

    def load_active_queue(self) -> List[dict]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT url, filename, save_path, status, total_size, downloaded_size,
                segments, checksum, checksum_algo, speed_limit, added_date, priority, queue_order,
                speed_limit_override, automatic_retry_count, auto_retry_at FROM active_queue ORDER BY queue_order, id""")
            rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "url": row[0], "filename": row[1], "save_path": row[2], "status": row[3],
                "total": row[4] or 0, "downloaded": row[5] or 0, "segments": row[6] or 1,
                "checksum": row[7] or "", "checksum_algo": row[8] or "sha256",
                "speed_limit": row[9] or 0, "added_date": row[10],
                "priority": safe_int(row[11], 1, 0, 2), "queue_order": safe_int(row[12], 0, 0),
                "speed_limit_override": None if row[13] is None else max(0, safe_int(row[13], 0)),
                "automatic_retry_count": safe_int(row[14], 0, 0, 100),
                "auto_retry_at": max(0.0, float(row[15] or 0.0)),
            })
        return result

    def clear_active_queue(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM active_queue')
            conn.commit()
