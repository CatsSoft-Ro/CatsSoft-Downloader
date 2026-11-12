"""Teste pentru funcționalitățile adăugate după auditul extern (runda 4).

Acestea au fost cerute explicit de utilizator, pe baza constatărilor valide din
raportul de audit extern:

1. **Proxy (HTTP/HTTPS/SOCKS5)** - câmp de setare, aplicat pe toate sesiunile
   HTTP (sesiunea globală și cele per-adaptor), cu degradare curată dacă
   PySocks nu e instalat.
2. **Sortare în tab-ul Descărcări** după viteză, dimensiune și ETA.
3. **Dirty flag la persistența cozii** - rescrierea în DB doar când starea chiar
   s-a schimbat (stare sau progres avansat cu cel puțin 1%).
"""
import hashlib
import os
import queue
import socket
import sys
import threading
import time
import unittest
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402
from test_engine_integrity import _RangeServer  # noqa: E402


class _TinyProxy:
    """Proxy HTTP minimal, doar pentru test: primește cereri absolute și le
    trimite mai departe, reținând ce a trecut prin el."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.requests = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            lines = head.split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            with self._lock:
                self.requests.append((method, target, headers.get("host", "")))
            url = target if target.startswith("http://") else "http://%s%s" % (
                headers.get("host", ""), target)
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            upstream = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=20)
            out = ["%s %s %s" % (method, path, version)]
            for key, value in headers.items():
                if key in ("proxy-connection", "connection", "host"):
                    continue
                out.append("%s: %s" % (key, value))
            out.append("Connection: close")
            upstream.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
            upstream.close()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class _PersistStub:
    """Aplicație minimă pentru _persist_active_queue (fără Tk)."""

    def __init__(self, downloads, db):
        self.downloads = downloads
        self.db = db
        self._closing = False
        self._active_queue_persist_id = None

    def winfo_exists(self):
        return True

    def after(self, _ms, _func):
        return 1


class _CountingDB:
    def __init__(self):
        self.calls = 0
        self.rows = None

    def save_active_queue(self, downloads):
        self.calls += 1
        self.rows = list(downloads)


class ProxyTests(unittest.TestCase):
    """Suportul pentru proxy: normalizare, aplicare, degradare."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_normalize_accepts_the_common_forms(self):
        normalize = self.app._normalize_proxy_url
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")
        self.assertEqual(normalize("host:8080"), "http://host:8080")
        self.assertEqual(normalize("http://user:pass@host:3128"),
                         "http://user:pass@host:3128")
        self.assertEqual(normalize("https://proxy.example:443"),
                         "https://proxy.example:443")
        self.assertEqual(normalize("socks5://127.0.0.1:1080"),
                         "socks5://127.0.0.1:1080")
        self.assertEqual(normalize("SOCKS5H://127.0.0.1:1080"),
                         "socks5h://127.0.0.1:1080")

    def test_normalize_rejects_invalid_input(self):
        normalize = self.app._normalize_proxy_url
        self.assertEqual(normalize("ftp://host:21"), "")
        self.assertEqual(normalize("file:///tmp/x"), "")
        self.assertEqual(normalize("http://"), "")
        self.assertEqual(normalize("http://   "), "")

    def test_apply_proxy_sets_and_clears_the_session(self):
        import requests
        session = requests.Session()
        self.assertTrue(self.app.apply_proxy_to_session(session, "http://proxy.local:3128"))
        self.assertEqual(session.proxies.get("http"), "http://proxy.local:3128")
        self.assertEqual(session.proxies.get("https"), "http://proxy.local:3128")
        # Gol = fără proxy: sesiunea trebuie curățată, nu lăsată cu vechea valoare.
        self.assertFalse(self.app.apply_proxy_to_session(session, ""))
        self.assertEqual(dict(session.proxies), {})

    def test_socks_proxy_is_ignored_when_pysocks_is_missing(self):
        import requests
        original = self.app._socks_proxy_available
        self.app._socks_proxy_available = lambda: False
        try:
            session = requests.Session()
            self.assertFalse(self.app.apply_proxy_to_session(session, "socks5://127.0.0.1:1080"))
            self.assertEqual(dict(session.proxies), {},
                             "fără PySocks proxy-ul SOCKS trebuie ignorat, nu aplicat")
        finally:
            self.app._socks_proxy_available = original

    def test_adapter_sessions_receive_the_proxy(self):
        session = self.app.NetworkAdapterManager._make_session(
            "127.0.0.1", "http://proxy.local:3128")
        self.assertEqual(session.proxies.get("http"), "http://proxy.local:3128")
        self.assertEqual(session.proxies.get("https"), "http://proxy.local:3128")

    def test_manager_apply_proxy_updates_existing_sessions(self):
        import requests
        config = self.app.ConfigManager(self.app.DatabaseManager())
        manager = self.app.NetworkAdapterManager(config)
        first, second = requests.Session(), requests.Session()
        manager._profiles["a|127.0.0.1"] = {"key": "a|127.0.0.1", "session": first}
        manager._profiles["b|127.0.0.2"] = {"key": "b|127.0.0.2", "session": second}

        updated = manager.apply_proxy("http://proxy.local:3128")
        self.assertEqual(updated, 2)
        self.assertEqual(first.proxies.get("https"), "http://proxy.local:3128")
        self.assertEqual(second.proxies.get("https"), "http://proxy.local:3128")

        manager.apply_proxy("")
        self.assertEqual(dict(first.proxies), {})

    def test_proxy_setting_round_trips_through_the_config(self):
        db = self.app.DatabaseManager()
        config = self.app.ConfigManager(db)
        self.assertEqual(config.get("proxy_url", ""), "")
        config.set("proxy_url", "http://proxy.local:3128")
        self.assertEqual(config.get("proxy_url"), "http://proxy.local:3128")
        reloaded = self.app.ConfigManager(db)
        self.assertEqual(reloaded.get("proxy_url"), "http://proxy.local:3128")


class ProxyEndToEndTests(unittest.TestCase):
    """Proxy-ul trebuie să funcționeze pe traseu real, nu doar pe hârtie."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.server = _RangeServer()
        self.proxy = _TinyProxy()

    def tearDown(self):
        try:
            self.app.SESSION.proxies.clear()
        except Exception:
            pass
        self.proxy.close()
        try:
            self.server.httpd.shutdown()
        except Exception:
            pass
        self.env.__exit__(None, None, None)

    def test_download_really_goes_through_the_proxy(self):
        target = os.path.join(self.env.downloads_dir, "payload.bin")
        dl = self.app.Download(url=self.server.url, filename="payload.bin",
                               save_path=target, segments=1)
        dl.app = None  # fără manager: se folosește sesiunea globală

        self.assertTrue(self.app.apply_proxy_to_session(self.app.SESSION, self.proxy.url))
        update_q = queue.Queue()
        self.app.download_simple(dl, update_q, max_retries=1)

        # download_simple semnalează finalizarea prin coadă, nu prin valoarea
        # de returnare.
        statuses = []
        while not update_q.empty():
            kind, _dl, extra = update_q.get_nowait()
            if kind == "status":
                statuses.append(extra)
        self.assertIn(self.app.DLStatus.COMPLETED, statuses,
                      "descărcarea prin proxy nu a ajuns la final; evenimente: %r" % (statuses,))
        self.assertTrue(self.proxy.requests,
                        "nicio cerere nu a trecut prin proxy - setarea nu a fost aplicată")
        with open(target, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(digest, hashlib.sha256(self.server.payload).hexdigest(),
                         "fișierul descărcat prin proxy nu e identic cu sursa")


class SortKeyTests(unittest.TestCase):
    """Sortarea dinamică: viteză, dimensiune, ETA."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _download(self, filename, status=None, total=0, downloaded=0, speed=0.0):
        dl = self.app.Download(url="http://example.test/" + filename, filename=filename,
                               save_path=os.path.join(self.env.downloads_dir, filename),
                               status=status or self.app.DLStatus.DOWNLOADING,
                               total=total, downloaded=downloaded, speed=speed)
        return dl

    def test_speed_sort_puts_the_fastest_first(self):
        slow = self._download("slow.bin", speed=1024.0)
        fast = self._download("fast.bin", speed=1024.0 * 512)
        idle = self._download("idle.bin", status=self.app.DLStatus.QUEUED, speed=0.0)
        visible = [idle, slow, fast]
        visible.sort(key=lambda item: -self.app.DownloadManager._download_speed_value(item))
        self.assertEqual([dl.filename for dl in visible], ["fast.bin", "slow.bin", "idle.bin"])

    def test_size_sort_puts_the_largest_first(self):
        small = self._download("small.bin", total=10)
        big = self._download("big.bin", total=10 ** 9)
        unknown = self._download("unknown.bin", total=0)
        visible = [unknown, small, big]
        visible.sort(key=lambda item: -self.app.DownloadManager._download_total_value(item))
        self.assertEqual([dl.filename for dl in visible], ["big.bin", "small.bin", "unknown.bin"])

    def test_eta_sort_puts_the_soonest_first_and_unknown_last(self):
        # 1 MB la 1 MB/s = ~1 s ; 10 MB la 1 MB/s = ~10 s
        soon = self._download("soon.bin", total=1024 * 1024, speed=1024.0 * 1024)
        later = self._download("later.bin", total=10 * 1024 * 1024, speed=1024.0 * 1024)
        unknown = self._download("unknown.bin", status=self.app.DLStatus.QUEUED)
        visible = [unknown, later, soon]
        visible.sort(key=self.app.DownloadManager._download_eta_key)
        self.assertEqual([dl.filename for dl in visible],
                         ["soon.bin", "later.bin", "unknown.bin"])

    def test_eta_key_marks_downloads_without_eta(self):
        queued = self._download("q.bin", status=self.app.DLStatus.QUEUED)
        self.assertEqual(self.app.DownloadManager._download_eta_key(queued)[0], 1)
        running = self._download("r.bin", total=100, speed=0.0)
        self.assertEqual(self.app.DownloadManager._download_eta_key(running)[0], 1)


class QueueSnapshotDirtyFlagTests(unittest.TestCase):
    """Coada activă se scrie în DB doar când starea chiar s-a schimbat."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.db = _CountingDB()
        self.downloads = []
        self.stub = _PersistStub(self.downloads, self.db)
        self.stub._queue_snapshot_signature = (
            lambda: self.app.DownloadManager._queue_snapshot_signature(self.stub))
        self.stub._persist_active_queue = (
            lambda: self.app.DownloadManager._persist_active_queue(self.stub))

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _download(self, filename, status=None, total=0, downloaded=0):
        dl = self.app.Download(url="http://example.test/" + filename, filename=filename,
                               save_path=os.path.join(self.env.downloads_dir, filename),
                               status=status or self.app.DLStatus.QUEUED,
                               total=total, downloaded=downloaded)
        self.downloads.append(dl)
        return dl

    def _persist(self):
        self.app.DownloadManager._persist_active_queue(self.stub)

    def test_unchanged_queue_is_not_rewritten(self):
        self._download("a.bin")
        self._persist()
        self.assertEqual(self.db.calls, 1, "prima salvare trebuie făcută")
        self._persist()
        self._persist()
        self.assertEqual(self.db.calls, 1,
                         "o coadă neschimbată nu trebuie rescrisă în DB")

    def test_state_change_triggers_a_write(self):
        dl = self._download("a.bin")
        self._persist()
        self.assertEqual(self.db.calls, 1)
        dl.status = self.app.DLStatus.DOWNLOADING
        self._persist()
        self.assertEqual(self.db.calls, 2, "schimbarea de stare trebuie salvată")

    def test_tiny_progress_change_does_not_trigger_a_write(self):
        dl = self._download("a.bin", total=1000, downloaded=0)
        self._persist()
        self.assertEqual(self.db.calls, 1)
        # sub 1% din total: nu e o schimbare relevantă pentru recuperare
        dl.downloaded = 5
        self._persist()
        self.assertEqual(self.db.calls, 1, "progres sub 1% nu trebuie să scrie în DB")

    def test_one_percent_progress_triggers_a_write(self):
        dl = self._download("a.bin", total=1000, downloaded=0)
        self._persist()
        self.assertEqual(self.db.calls, 1)
        dl.downloaded = 10  # 1% din total
        self._persist()
        self.assertEqual(self.db.calls, 2, "un progres de 1% trebuie salvat")

    def test_new_download_triggers_a_write(self):
        self._download("a.bin")
        self._persist()
        self.assertEqual(self.db.calls, 1)
        self._download("b.bin")
        self._persist()
        self.assertEqual(self.db.calls, 2, "o descărcare nouă trebuie salvată")

    def test_removed_download_triggers_a_write(self):
        self._download("a.bin")
        self._download("b.bin")
        self._persist()
        self.assertEqual(self.db.calls, 1)
        self.downloads.pop()
        self._persist()
        self.assertEqual(self.db.calls, 2, "eliminarea unei descărcări trebuie salvată")

    def test_forced_snapshot_always_writes(self):
        dl = self._download("a.bin")
        self._persist()
        self.assertEqual(self.db.calls, 1)
        self.app.DownloadManager._persist_queue_snapshot_now(self.stub)
        self.assertEqual(self.db.calls, 2,
                         "snapshot-ul forțat scrie chiar dacă starea nu s-a schimbat")
        # și semnătura se actualizează, ca timerul periodic să nu scrie din nou
        self._persist()
        self.assertEqual(self.db.calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
