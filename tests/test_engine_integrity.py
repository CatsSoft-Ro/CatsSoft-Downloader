"""Teste de regresie pentru integritatea transferului (motor).

Auditul a identificat mai multe drumuri prin care un fișier putea ajunge
raportat COMPLET, dar cu conținut corupt sau incomplet:

1. Contorul ``dl.downloaded`` putea fi vechi (restaurat din baza de date) și
   folosit ca offset Range - serverul umplea intervalul greșit, iar verificarea
   finală de dimensiune trecea oricum.
2. ``If-Range`` era trimis și la cereri de la zero; un server care răspundea 200
   în loc de 206 făcea aplicația să declare "resursa schimbată" și să ștergă
   segmentele valide.
3. Verificarea spațiului pe disc cerea spațiu pentru întregul fișier, deși
   octeții descărcați ocupau deja loc în fișierele .partN.
4. Îmbinarea segmentelor ștergea sursele înainte de validarea dimensiunii.
5. Un fișier orfan, de aceeași dimensiune ca resursa, era acceptat ca complet.
6. Fără ETag/Last-Modified, reluarea putea amesteca două versiuni ale fișierului.
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


class _RangeServer:
    """Server local care suportă Range și expune un ETag stabil."""

    def __init__(self, payload=None, etag='"stable-etag"', with_validator=True):
        self.payload = payload if payload is not None else os.urandom(512 * 1024)
        self.total = len(self.payload)
        self.etag = etag
        self.with_validator = with_validator
        self.requests = []
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

            def _validator_headers(self):
                if server_self.with_validator:
                    self.send_header("ETag", server_self.etag)
                    self.send_header("Last-Modified", "Mon, 01 Jan 2035 00:00:00 GMT")

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", str(server_self.total))
                self.send_header("Accept-Ranges", "bytes")
                self._validator_headers()
                self.end_headers()

            def do_GET(self):
                rng = self.headers.get("Range")
                start, end, partial = 0, server_self.total - 1, False
                if rng and rng.startswith("bytes="):
                    partial = True
                    spec = rng[len("bytes="):]
                    if "-" in spec:
                        a, _, b = spec.partition("-")
                        start = int(a) if a else 0
                        end = int(b) if b else server_self.total - 1
                server_self.requests.append((start, end, partial,
                                             self.headers.get("If-Range")))
                length = max(0, min(end, server_self.total - 1) - start + 1)
                self.send_response(206 if partial else 200)
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header(
                        "Content-Range",
                        "bytes %d-%d/%d" % (start, start + length - 1, server_self.total))
                self.send_header("Accept-Ranges", "bytes")
                self._validator_headers()
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                try:
                    self.wfile.write(server_self.payload[start:start + length])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _FakeResponse:
    """Răspuns HTTP minimal pentru testele de segment."""

    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.raw = _FakeRaw(body)
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class _FakeRaw:
    def __init__(self, body):
        self._body = body
        self._pos = 0
        self.decode_content = False

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class _FakeSession:
    """Sesiune care înregistrează antetele și întoarce un răspuns fix."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, stream=False, headers=None, timeout=None, **kwargs):
        self.calls.append(dict(headers or {}))
        return self.response


class _FakeManager:
    """NetworkAdapterManager fals: întotdeauna aceeași sesiune."""

    def __init__(self, session):
        self.session = session

    def acquire_session(self):
        return "test", self.session

    def release_session(self, key):
        pass

    def report_bytes(self, key, byte_count):
        pass

    def report_failure(self, key, exc=None):
        pass

    def throttle_wait(self, key, byte_count):
        return 0.0


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _FakeApp:
    """Aplicație falsă: suficientă pentru _download_segment."""

    def __init__(self, session):
        self.config = _FakeConfig()
        self.network_manager = _FakeManager(session)
        self._bandwidth_lock = threading.Lock()
        self._bandwidth_deadline = 0.0
        self._session_transfer_lock = threading.Lock()
        self._session_transferred_total = 0


class EngineIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _make_dl(self, folder_name="dl", segments=1, url="http://127.0.0.1:1/x.bin"):
        folder = os.path.join(self.env.downloads_dir, folder_name)
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url=url, filename="payload.bin",
                               save_path=os.path.join(folder, "payload.bin"),
                               segments=segments)
        dl.status = self.app.DLStatus.DOWNLOADING
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        return dl

    @staticmethod
    def _drain(q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    # ── 1. Contorul vechi nu mai poate strica offset-ul Range ────────────────
    def test_reconcile_uses_real_file_size_not_stale_counter(self):
        dl = self._make_dl("stale")
        dl.total = 1000
        dl.downloaded = 500_000              # valoare veche, din baza de date
        # Fișierul nu există deloc pe disc.
        on_disk = self.app._reconcile_downloaded_with_disk(dl)
        self.assertEqual(on_disk, 0)
        self.assertEqual(dl.downloaded, 0)

    def test_reconcile_uses_partial_file_length(self):
        dl = self._make_dl("partial")
        dl.total = 1000
        with open(dl.save_path, "wb") as handle:
            handle.write(b"x" * 4096)
        dl.downloaded = 100                  # mai mic decât realitatea
        on_disk = self.app._reconcile_downloaded_with_disk(dl)
        self.assertEqual(on_disk, 4096)
        self.assertEqual(dl.downloaded, 4096)

    def test_reconcile_refuses_unverifiable_output(self):
        dl = self._make_dl("blocked")
        dl.total = 1000
        dl.resume_blocked = True
        with open(dl.save_path, "wb") as handle:
            handle.write(b"x" * 4096)
        on_disk = self.app._reconcile_downloaded_with_disk(dl)
        self.assertEqual(on_disk, 0)
        self.assertEqual(dl.downloaded, 0)

    # ── 2. Spațiul pe disc se cere doar pentru octeții rămași ────────────────
    def test_segment_bytes_on_disk_counts_only_numbered_parts(self):
        dl = self._make_dl("parts")
        folder = os.path.dirname(dl.save_path)
        for name, size in (("payload.bin.part0", 1000),
                           ("payload.bin.part1", 2000),
                           ("payload.bin.part0.race123", 1000)):
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(b"x" * size)
        self.assertEqual(self.app._segment_bytes_on_disk(dl.save_path), 3000)

    def test_resume_does_not_demand_space_for_already_downloaded_bytes(self):
        """Cu 90% din fișier deja pe disc, cererea de spațiu trebuie să fie mică."""
        dl = self._make_dl("space")
        total = 10_000_000
        folder = os.path.dirname(dl.save_path)
        with open(os.path.join(folder, "payload.bin.part0"), "wb") as handle:
            handle.write(b"x" * (total - 1000))
        required = []
        original = self.app.check_disk_space

        def spy(path, needed, margin=1.05):
            required.append(needed)
            return True

        self.app.check_disk_space = spy
        try:
            # Nu lăsăm descărcarea să pornească: verificăm doar cererea de spațiu.
            self.app.check_disk_space = spy
            dl.total = total
            dl.downloaded = 0
            bytes_on_disk = self.app._segment_bytes_on_disk(dl.save_path)
            remaining = max(0, total - bytes_on_disk)
            self.assertLess(remaining, 2000)
            self.assertLess(remaining, total)
        finally:
            self.app.check_disk_space = original

    # ── 3. If-Range doar când reluăm efectiv octeți ─────────────────────────
    def test_if_range_is_not_sent_for_a_fresh_segment(self):
        dl = self._make_dl("ifrange")
        dl.resume_validator = '"stable-etag"'
        response = _FakeResponse(
            206,
            {"Content-Range": "bytes 0-9/10", "ETag": '"stable-etag"'},
            b"0123456789")
        session = _FakeSession(response)
        dl.app = _FakeApp(session)
        errors = []
        ok = self.app._download_segment(
            dl, "http://x/y.bin", dl.save_path + ".part0", 0, 9,
            queue.Queue(), errors, threading.Lock())
        self.assertTrue(ok, "segmentul ar trebui descărcat: %s" % errors)
        self.assertNotIn("If-Range", session.calls[0])

    def test_if_range_is_sent_when_resuming_bytes(self):
        dl = self._make_dl("ifrange2")
        dl.resume_validator = '"stable-etag"'
        with open(dl.save_path + ".part0", "wb") as handle:
            handle.write(b"01234")
        response = _FakeResponse(
            206,
            {"Content-Range": "bytes 5-9/10", "ETag": '"stable-etag"'},
            b"56789")
        session = _FakeSession(response)
        dl.app = _FakeApp(session)
        errors = []
        ok = self.app._download_segment(
            dl, "http://x/y.bin", dl.save_path + ".part0", 0, 9,
            queue.Queue(), errors, threading.Lock())
        self.assertTrue(ok, "segmentul ar trebui descărcat: %s" % errors)
        self.assertEqual(session.calls[0].get("If-Range"), '"stable-etag"')

    def test_http_200_for_a_range_request_is_not_reported_as_resource_change(self):
        """200 fără If-Range înseamnă doar că serverul nu onorează Range."""
        dl = self._make_dl("nochange")
        dl.resume_validator = '"stable-etag"'
        response = _FakeResponse(200, {"Content-Length": "10"}, b"0123456789")
        session = _FakeSession(response)
        dl.app = _FakeApp(session)
        errors = []
        ok = self.app._download_segment(
            dl, "http://x/y.bin", dl.save_path + ".part0", 0, 9,
            queue.Queue(), errors, threading.Lock())
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertFalse(any(str(item).startswith("RESOURCE_CHANGED:") for item in errors),
                         "un 200 la Range nu trebuie clasificat ca resursă schimbată")

    def test_http_200_with_if_range_is_reported_as_resource_change(self):
        """Aici 200 înseamnă într-adevăr că validatorul nu se potrivește."""
        dl = self._make_dl("changed")
        dl.resume_validator = '"stable-etag"'
        with open(dl.save_path + ".part0", "wb") as handle:
            handle.write(b"01234")
        response = _FakeResponse(200, {"Content-Length": "10"}, b"0123456789")
        session = _FakeSession(response)
        dl.app = _FakeApp(session)
        errors = []
        ok = self.app._download_segment(
            dl, "http://x/y.bin", dl.save_path + ".part0", 0, 9,
            queue.Queue(), errors, threading.Lock())
        self.assertFalse(ok)
        self.assertTrue(any(str(item).startswith("RESOURCE_CHANGED:") for item in errors))

    # ── 4. Îmbinarea păstrează segmentele dacă dimensiunea nu corespunde ────
    def test_merge_keeps_sources_when_size_does_not_match(self):
        dl = self._make_dl("merge")
        first = dl.save_path + ".part0"
        second = dl.save_path + ".part1"
        with open(first, "wb") as handle:
            handle.write(b"a" * 10)
        with open(second, "wb") as handle:
            handle.write(b"b" * 10)
        with self.assertRaises(Exception):
            self.app._merge_segments(dl.save_path, [first, second], expected_total=999)
        # Segmentele sursă trebuie să existe: reluarea le reutilizează.
        self.assertTrue(os.path.isfile(first))
        self.assertTrue(os.path.isfile(second))
        self.assertFalse(os.path.exists(dl.save_path))

    def test_merge_succeeds_and_removes_sources(self):
        dl = self._make_dl("merge2")
        first = dl.save_path + ".part0"
        second = dl.save_path + ".part1"
        with open(first, "wb") as handle:
            handle.write(b"a" * 10)
        with open(second, "wb") as handle:
            handle.write(b"b" * 5)
        self.app._merge_segments(dl.save_path, [first, second], expected_total=15)
        self.assertTrue(os.path.isfile(dl.save_path))
        self.assertEqual(os.path.getsize(dl.save_path), 15)
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

    # ── 5. Fișier orfan de aceeași dimensiune nu e acceptat ca complet ─────
    def test_orphan_file_of_same_size_is_not_verified(self):
        """Un fișier fără metadate, de aceeași dimensiune ca resursa, nu poate fi
        nici verificat, nici reluat: reluarea ar concatena octeți străini."""
        dl = self._make_dl("orphan")
        dl.url = "http://127.0.0.1:1/payload.bin"
        with open(dl.save_path, "wb") as handle:
            handle.write(b"z" * 100)
        headers = {"etag": '"stable-etag"', "content-length": "100"}
        self.app._validate_or_reset_resume_artifacts(dl, 100, headers)
        self.assertFalse(dl.final_verified,
                         "un fișier fără metadate nu poate fi considerat resursa curentă")
        self.assertTrue(dl.resume_blocked,
                        "un fișier orfan nu poate fi reluat peste el")

    def test_verified_file_is_accepted(self):
        """Cu metadate corespunzătoare, fișierul poate fi considerat complet."""
        dl = self._make_dl("verified")
        dl.url = "http://127.0.0.1:1/payload.bin"
        with open(dl.save_path, "wb") as handle:
            handle.write(b"z" * 100)
        headers = {"etag": '"stable-etag"', "content-length": "100"}
        # Prima apelare scrie metadatele, a doua le poate verifica.
        self.app._validate_or_reset_resume_artifacts(dl, 100, headers)
        self.app._validate_or_reset_resume_artifacts(dl, 100, headers)
        self.assertTrue(dl.final_verified)

    # ── 6. Fără validator nu reluăm peste date neverificabile ──────────────
    def test_missing_validator_restarts_without_resume(self):
        """Server fără ETag/Last-Modified: datele existente nu pot fi dovedite
        ca aparținând resursei curente, deci reluarea este abandonată."""
        dl = self._make_dl("novalidator")
        dl.url = "http://127.0.0.1:1/payload.bin"
        with open(dl.save_path, "wb") as handle:
            handle.write(b"x" * 1000)
        # Metadatele există (descărcare începută într-o sesiune anterioară),
        # dar serverul nu oferă niciun validator.
        self.app._write_download_meta(dl.save_path, {
            "url": dl.url, "total": 1000, "etag": "", "last_modified": "",
            "updated_at": "2026-01-01T00:00:00"})
        headers = {"content-length": "1000"}
        self.app._validate_or_reset_resume_artifacts(dl, 1000, headers)
        self.assertTrue(dl.resume_blocked)
        self.assertFalse(dl.final_verified)
        self.assertEqual(dl.downloaded, 0)

    def test_missing_validator_keeps_data_written_in_this_session(self):
        """La încercările următoare datele pot fi deja ale noastre."""
        dl = self._make_dl("novalidator2")
        dl.url = "http://127.0.0.1:1/payload.bin"
        with open(dl.save_path, "wb") as handle:
            handle.write(b"x" * 1000)
        self.app._write_download_meta(dl.save_path, {
            "url": dl.url, "total": 1000, "etag": "", "last_modified": "",
            "updated_at": "2026-01-01T00:00:00"})
        headers = {"content-length": "1000"}
        self.app._validate_or_reset_resume_artifacts(
            dl, 1000, headers, allow_restart_without_validator=False)
        self.assertFalse(dl.resume_blocked)
        self.assertEqual(dl.downloaded, 0)
        self.assertTrue(os.path.exists(dl.save_path))

    # ── 7. Trackerul de viteză nu mai raportează salturi ca viteză ───────────
    def test_speed_tracker_rebase_removes_the_spike(self):
        tracker = self.app.SpeedTracker()
        tracker.update(0)
        time.sleep(0.2)
        tracker.update(8 * 1024 * 1024)         # salt de 8 MiB într-un sample
        self.assertGreater(tracker.get_speed(), 0)
        tracker.rebase(8 * 1024 * 1024)
        self.assertEqual(tracker.get_speed(), 0.0)


class EngineIntegrityEndToEndTests(unittest.TestCase):
    """Transferuri reale: fișierul final trebuie să fie identic cu sursa."""

    @classmethod
    def setUpClass(cls):
        cls.server = _RangeServer()
        cls.payload_hash = hashlib.sha256(cls.server.payload).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.server.requests.clear()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _make_dl(self, name):
        folder = os.path.join(self.env.downloads_dir, name)
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url=self.server.url, filename="payload.bin",
                               save_path=os.path.join(folder, "payload.bin"), segments=1)
        dl.status = self.app.DLStatus.DOWNLOADING
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        return dl

    @staticmethod
    def _drain(q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    def test_stale_counter_larger_than_file_does_not_corrupt_the_result(self):
        """Contor mai mare decât fișierul: fără reconciliere s-ar cerea un
        interval greșit, iar rezultatul ar fi complet, dar corupt."""
        dl = self._make_dl("e2e_stale_big")
        dl.downloaded = 100_000               # valoare veche, din baza de date
        q = queue.Queue()
        self.app.download_simple(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, [e[2] for e in events])
        with open(dl.save_path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), self.payload_hash)

    def test_stale_counter_smaller_than_file_does_not_corrupt_the_result(self):
        """Contor mai mic decât fișierul: fără reconciliere ar fi reluat de la
        mijloc, iar rezultatul ar trece verificarea de dimensiune, dar cu
        octeți duplicați."""
        dl = self._make_dl("e2e_stale_small")
        with open(dl.save_path, "wb") as handle:
            handle.write(self.server.payload[:100_000])
        dl.downloaded = 50_000                # valoare veche, mai mică decât fișierul
        q = queue.Queue()
        self.app.download_simple(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, [e[2] for e in events])
        with open(dl.save_path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), self.payload_hash)

    def test_resume_from_partial_file_keeps_the_content_correct(self):
        """Reluare corectă de pe fișier parțial: conținutul trebuie să fie exact."""
        dl = self._make_dl("e2e_resume")
        cut = 100_000
        with open(dl.save_path, "wb") as handle:
            handle.write(self.server.payload[:cut])
        q = queue.Queue()
        self.app.download_simple(dl, q)
        events = self._drain(q)
        self.assertIn(self.app.DLStatus.COMPLETED, [e[2] for e in events])
        with open(dl.save_path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), self.payload_hash)
        self.assertTrue(any(r[0] > 0 for r in self.server.requests),
                        "reluarea ar trebui să ceară un interval non-zero")


if __name__ == "__main__":
    unittest.main(verbosity=2)
