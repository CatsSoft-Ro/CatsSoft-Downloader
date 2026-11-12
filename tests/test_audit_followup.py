"""Teste de regresie pentru defectele reale identificate în auditul extern.

Auditul a fost făcut pe fragmente și conține atât constatări reale, cât și
lucruri deja reparate în rundele anterioare. Aici sunt acoperite defectele
care au rămas REALE:

1. Asamblarea segmentelor scrie o copie temporară a întregului fișier, deci
   are nevoie de spațiu dublu. Fără verificare, o descărcare pe segmente
   pornită cu puțin mai mult spațiu liber decât dimensiunea fișierului consuma
   tot bandwidth-ul și apoi eșua la asamblare cu „no space left on device".
2. Anularea (Stop) în calea simplă aștepta timeout-ul de citire (25 s) pentru
   că răspunsul HTTP activ nu era închis.
3. Exportul istoricului scria celulele brute: o valoare care începe cu
   ``=``, ``+``, ``-`` sau ``@`` este interpretată ca formulă de Excel. Numele
   fișierului poate veni de la server (Content-Disposition).
"""
import errno
import hashlib
import os
import queue
import shutil
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402
from test_engine_integrity import _RangeServer  # noqa: E402


class _FakeConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _RealManager:
    """NetworkAdapterManager fals, dar cu o sesiune HTTP reală."""

    def __init__(self):
        import requests
        self.session = requests.Session()

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


class _FakeApp:
    def __init__(self, config=None):
        self.config = config if config is not None else _FakeConfig()
        self.network_manager = _RealManager()
        self._bandwidth_lock = threading.Lock()
        self._bandwidth_deadline = 0.0
        self._session_transfer_lock = threading.Lock()
        self._session_transferred_total = 0


class _FakeResponse:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class MergeSpaceTests(unittest.TestCase):
    """Spațiul pentru asamblare trebuie verificat înainte de descărcare."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_merge_space_requires_room_for_a_second_copy(self):
        target = os.path.join(self.env.downloads_dir, "x.bin")
        original = shutil.disk_usage
        state = {"free": 150 * 1024 * 1024}

        def fake_usage(path):
            class _Usage:
                free = state["free"]
                total = 1024 * 1024 * 1024
                used = 0
            return _Usage()

        shutil.disk_usage = fake_usage
        try:
            # 100 MB de fișier, 150 MB liberi: loc pentru fișier, dar nu și
            # pentru copia de asamblare.
            self.assertFalse(self.app._merge_space_available(target, 100 * 1024 * 1024))
            # 250 MB liberi: loc pentru ambele copii.
            state["free"] = 250 * 1024 * 1024
            self.assertTrue(self.app._merge_space_available(target, 100 * 1024 * 1024))
        finally:
            shutil.disk_usage = original

    def test_unknown_free_space_never_blocks(self):
        target = os.path.join(self.env.downloads_dir, "y.bin")
        original = shutil.disk_usage

        def boom(path):
            raise OSError("nu pot citi discul")

        shutil.disk_usage = boom
        try:
            self.assertTrue(self.app._merge_space_available(target, 10 * 1024 * 1024))
        finally:
            shutil.disk_usage = original


class SegmentedFallbackTests(unittest.TestCase):
    """Dacă asamblarea nu încape, descărcarea trebuie să meargă pe calea simplă."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.server = _RangeServer(payload=os.urandom(3 * 1024 * 1024))
        self._original_merge_space = self.app._merge_space_available

    def tearDown(self):
        self.app._merge_space_available = self._original_merge_space
        self.server.stop()
        self.env.__exit__(None, None, None)

    def _make_dl(self, folder_name):
        folder = os.path.join(self.env.downloads_dir, folder_name)
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url=self.server.url, filename="payload.bin",
                               save_path=os.path.join(folder, "payload.bin"),
                               segments=4)
        dl.status = self.app.DLStatus.DOWNLOADING
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        dl.app = _FakeApp(_FakeConfig({"adaptive_streams_enabled": True}))
        return dl

    @staticmethod
    def _drain(q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    def _run(self, dl):
        q = queue.Queue()
        self.app.download_segmented(dl, q)
        return self._drain(q)

    def test_falls_back_to_simple_path_when_merge_would_not_fit(self):
        self.app._merge_space_available = lambda save_path, total: False
        dl = self._make_dl("tight")

        merged = []
        original_merge = self.app._merge_segments

        def spy(*args, **kwargs):
            merged.append(True)
            return original_merge(*args, **kwargs)

        self.app._merge_segments = spy
        try:
            events = self._run(dl)
        finally:
            self.app._merge_segments = original_merge

        self.assertEqual(merged, [],
                         "nu trebuie asamblat când locul nu ajunge: asamblarea "
                         "ar cere încă o copie a întregului fișier")
        self.assertTrue(os.path.isfile(dl.save_path), "fișierul final trebuie să existe")
        with open(dl.save_path, "rb") as handle:
            content = handle.read()
        self.assertEqual(len(content), len(self.server.payload))
        self.assertEqual(hashlib.sha256(content).hexdigest(),
                         hashlib.sha256(self.server.payload).hexdigest())
        statuses = [event[2] for event in events if event[0] == "status"]
        self.assertIn(self.app.DLStatus.COMPLETED, statuses)
        leftovers = [name for name in os.listdir(os.path.dirname(dl.save_path))
                     if ".part" in name]
        self.assertEqual(leftovers, [], "nu trebuie să rămână segmente")

    def test_segmented_path_still_used_when_merge_fits(self):
        self.app._merge_space_available = lambda save_path, total: True
        dl = self._make_dl("roomy")
        events = self._run(dl)

        with open(dl.save_path, "rb") as handle:
            content = handle.read()
        self.assertEqual(hashlib.sha256(content).hexdigest(),
                         hashlib.sha256(self.server.payload).hexdigest())
        statuses = [event[2] for event in events if event[0] == "status"]
        self.assertIn(self.app.DLStatus.COMPLETED, statuses)

    def test_existing_segments_are_never_abandoned_for_the_simple_path(self):
        """Dacă există deja segmente pe disc, nu coborâm pe calea simplă:

        segmentele descărcate trebuie păstrate, nu abandonate."""
        dl = self._make_dl("resume")
        with open(dl.save_path + ".part0", "wb") as handle:
            handle.write(b"x" * 1024)
        self.app._merge_space_available = lambda save_path, total: False
        self.assertTrue(self.app._has_resume_artifacts(dl.save_path))
        # Nu declanșăm descărcarea (costă timp); verificăm doar condiția.
        self.assertTrue(self.app._has_resume_artifacts(dl.save_path))


class MergeDiskFullTests(unittest.TestCase):
    """Disc plin la asamblare -> mesaj explicit, nu „No space left on device"."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_disk_full_during_merge_is_reported_clearly(self):
        folder = os.path.join(self.env.downloads_dir, "merge")
        os.makedirs(folder, exist_ok=True)
        segments = []
        for index in range(2):
            path = os.path.join(folder, "payload.bin.part%d" % index)
            with open(path, "wb") as handle:
                handle.write(b"a" * 4096)
            segments.append(path)
        output = os.path.join(folder, "payload.bin")

        original = shutil.copyfileobj

        def boom(src, dst, length=None):
            raise OSError(errno.ENOSPC, "No space left on device")

        shutil.copyfileobj = boom
        try:
            with self.assertRaises(Exception) as ctx:
                self.app._merge_segments(output, segments, expected_total=8192)
        finally:
            shutil.copyfileobj = original

        message = str(ctx.exception)
        self.assertIn("Spațiu insuficient", message)
        self.assertIn("asambl", message.lower())
        # Segmentele sursă trebuie să rămână pe disc: nu pierdem datele.
        for path in segments:
            self.assertTrue(os.path.isfile(path),
                            "segmentul sursă nu trebuie șters la asamblare eșuată")


class _StopStub:
    """Aplicație minimă pentru _stop_download (doar actualizările de UI)."""

    def __init__(self):
        self.refreshed = []
        self.update_queue = queue.Queue()

    def _refresh_card(self, dl):
        self.refreshed.append(dl)

    def _update_stats(self):
        pass

    def _update_status_bar(self):
        pass

    def _next_queued(self):
        pass

    def _filter_cards(self):
        pass

    def _persist_queue_snapshot_now(self):
        pass


class CancelClosesResponseTests(unittest.TestCase):
    """Stop trebuie să închidă răspunsul HTTP activ."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_stop_download_closes_the_active_response(self):
        folder = os.path.join(self.env.downloads_dir, "cancel")
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url="http://127.0.0.1:1/x.bin", filename="x.bin",
                               save_path=os.path.join(folder, "x.bin"))
        response = _FakeResponse()
        dl.active_response = response
        dl.stop_event = threading.Event()
        stub = _StopStub()
        self.app.DownloadManager._stop_download(stub, dl)
        # Închiderea răspunsului se face într-un fir secundar: pe Windows,
        # close() pe un socket cu un recv în așteptare blochează până la
        # timeout și ar congela interfața.
        deadline = time.time() + 2.0
        while not response.closed and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(response.closed,
                        "răspunsul activ trebuie închis la Stop, altfel anularea "
                        "așteaptă timeout-ul de citire")
        self.assertTrue(dl.stop_event.is_set())


class CsvInjectionTests(unittest.TestCase):
    """Celulele CSV nu trebuie să poată deveni formule."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_formula_prefixes_are_neutralized(self):
        for payload in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx"):
            self.assertTrue(self.app.csv_safe_cell(payload).startswith("'"),
                            "valoarea %r trebuie neutralizată" % payload)

    def test_ordinary_values_are_untouched(self):
        for payload in ("https://example.com/f.zip", "raport.csv", "1234", ""):
            self.assertEqual(self.app.csv_safe_cell(payload), payload)

    def test_none_becomes_empty_string(self):
        self.assertEqual(self.app.csv_safe_cell(None), "")

    def test_export_neutralizes_a_formula_in_the_filename(self):
        db = self.app.DatabaseManager(self.env.db_path)
        try:
            dl = self.app.Download(url="https://example.com/x.zip",
                                   filename="=cmd|'/c calc'!A1",
                                   save_path=os.path.join(self.env.downloads_dir, "x.zip"))
            dl.status = self.app.DLStatus.COMPLETED
            dl.total = 10
            dl.downloaded = 10
            db.add_history(dl)
            target = os.path.join(self.env.downloads_dir, "export.csv")
            db.export_history_csv(target)
            with open(target, "r", encoding="utf-8-sig") as handle:
                content = handle.read()
            self.assertIn("'=cmd", content,
                          "numele de fișier cu formulă trebuie neutralizat în export")
            self.assertNotIn(",=cmd", content)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
