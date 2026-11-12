"""Teste end-to-end pentru motorul de descărcare, cu un server HTTP local.

Serverul suportă Range și poate (a) injecta erori 500 tranzitorii și (b) bloca
transferul, ca anularea să fie testată determinist.
"""
import hashlib
import os
import queue
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _Server:
    def __init__(self):
        self.payload = os.urandom(3 * 1024 * 1024 + 12345)
        self.total = len(self.payload)
        self.fail_budget = 0
        self.requests = []
        self.stall = False
        self.gate = threading.Event()
        self.gate.set()
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/payload.bin" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(server_self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _resolve(self):
                rng = self.headers.get("Range")
                start, end, partial = 0, server_self.total - 1, False
                if rng and rng.startswith("bytes="):
                    partial = True
                    spec = rng[len("bytes="):]
                    if "-" in spec:
                        a, _, b = spec.partition("-")
                        start = int(a) if a else 0
                        end = int(b) if b else server_self.total - 1
                return start, min(end, server_self.total - 1), partial

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(server_self.total))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"stable-etag"')
                self.send_header("Last-Modified", "Mon, 01 Jan 2035 00:00:00 GMT")
                self.end_headers()

            def do_GET(self):
                start, end, partial = self._resolve()
                server_self.requests.append((start, end, partial))
                if server_self.fail_budget > 0:
                    server_self.fail_budget -= 1
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, server_self.total))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"stable-etag"')
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                body = server_self.payload[start:end + 1]
                try:
                    half = max(1, len(body) // 2)
                    self.wfile.write(body[:half])
                    self.wfile.flush()
                    if server_self.stall:
                        server_self.gate.wait(timeout=30)
                    self.wfile.write(body[half:])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def stop(self):
        self.gate.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class EngineTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _Server()
        cls.payload_hash = hashlib.sha256(cls.server.payload).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)
        self.server.fail_budget = 0
        self.server.stall = False
        self.server.gate.set()
        self.server.requests.clear()

    def _make_dl(self, name="dl", segments=4):
        folder = os.path.join(self.env.downloads_dir, name)
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url=self.server.url, filename="payload.bin",
                               save_path=os.path.join(folder, "payload.bin"), segments=segments)
        dl.status = self.app.DLStatus.DOWNLOADING
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        return dl

    @staticmethod
    def _statuses(events):
        return [e[2] for e in events if e[0] == "status"]

    def _drain(self, q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    # ── Descărcare completă ───────────────────────────────────────────────────
    def test_segmented_download_completes_with_matching_hash(self):
        dl = self._make_dl("full")
        q = queue.Queue()
        self.app.download_segmented(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, self._statuses(events))
        self.assertTrue(os.path.isfile(dl.save_path))
        with open(dl.save_path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.payload_hash)
        leftovers = [f for f in os.listdir(os.path.dirname(dl.save_path)) if ".part" in f]
        self.assertEqual(leftovers, [])

    def test_simple_download_completes_with_matching_hash(self):
        dl = self._make_dl("simple", segments=1)
        q = queue.Queue()
        self.app.download_simple(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, self._statuses(events))
        with open(dl.save_path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.payload_hash)

    # ── Anulare + reluare ─────────────────────────────────────────────────────
    def test_cancel_then_resume_produces_matching_hash(self):
        dl = self._make_dl("resume")
        q = queue.Queue()
        self.server.stall = True
        self.server.gate.clear()

        worker = threading.Thread(target=self.app.download_segmented, args=(dl, q), daemon=True)
        worker.start()

        deadline = time.time() + 20
        partial_seen = False
        while time.time() < deadline:
            folder = os.path.dirname(dl.save_path)
            parts = [f for f in os.listdir(folder) if ".part" in f]
            if any(os.path.getsize(os.path.join(folder, f)) > 0 for f in parts):
                partial_seen = True
                break
            time.sleep(0.05)
        self.assertTrue(partial_seen, "segmentul nu a scris date înainte de anulare")

        dl.stop_event.set()
        worker.join(timeout=25)
        self.assertFalse(worker.is_alive())
        self._drain(q)
        self.server.gate.set()
        self.server.stall = False

        folder = os.path.dirname(dl.save_path)
        parts = [f for f in os.listdir(folder) if ".part" in f]
        self.assertTrue(parts, "segmentele parțiale ar trebui să rămână pe disc pentru reluare")
        self.assertTrue(any(os.path.getsize(os.path.join(folder, f)) > 0 for f in parts))

        self.server.requests.clear()
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        dl.status = self.app.DLStatus.DOWNLOADING
        dl.downloaded = 0
        q2 = queue.Queue()
        self.app.download_segmented(dl, q2)
        events = self._drain(q2)
        self.assertIn(self.app.DLStatus.COMPLETED, self._statuses(events))
        with open(dl.save_path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.payload_hash)
        resumed = [r for r in self.server.requests if r[2] and r[0] > 0]
        self.assertTrue(resumed, "reluarea ar trebui să ceară intervale non-zero")

    # ── Retry după eroare tranzitorie ─────────────────────────────────────────
    def test_retry_resumes_partial_segment_instead_of_restarting(self):
        dl = self._make_dl("retry")
        q = queue.Queue()
        self.server.fail_budget = 1          # primul GET eșuează cu 500
        self.server.requests.clear()
        self.app.download_segmented(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, self._statuses(events))
        with open(dl.save_path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.payload_hash)
        partial = [r for r in self.server.requests if r[2]]
        self.assertGreaterEqual(len(partial), 4)
        self.assertTrue(any(start > 0 for start, _e, _p in partial[1:]),
                        "retry-ul ar trebui să reia segmentul de la poziția rămasă, nu de la 0")

    # ── Spațiu insuficient ────────────────────────────────────────────────────
    def test_insufficient_disk_space_reports_explicit_error(self):
        dl = self._make_dl("space", segments=1)
        dl.save_path = os.path.join("Z:\\nonexistent-drive\\", "x.bin")
        original = self.app.check_disk_space
        self.app.check_disk_space = lambda *a, **k: False
        try:
            q = queue.Queue()
            self.app.download_segmented(dl, q)
            events = self._drain(q)
            self.assertIn(self.app.DLStatus.ERROR, self._statuses(events))
            self.assertIn("spațiu", (dl.error_msg or "").lower())
        finally:
            self.app.check_disk_space = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
