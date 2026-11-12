"""Test optional: descărcare live multi-fragment pe un fișier mare.

Acest test nu rulează implicit - descarcă ~200 MB (referință + transfer) și
durează ~1 minut. E pornit explicit:

    set CATSSoft_RUN_LARGE=1
    python tests\\test_large_file.py

De ce există: singurul fisier descarcat live din suita obișnuită are 110 KB, deci
grila de fragmente (1-8 MiB) produce un singur interval. Calea segmentată
multi-fragment - cea care rulează pe fișiere mari, cu reluare parțială și
"endgame racing" - nu era validată pe un server real decât prin serverul local de
test. Aici se verifică pe o gazdă reală, cu comparație de hash SHA-256.
"""
import hashlib
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import load_app  # noqa: E402

# Gazde care raspund 206 la o cerere Range pe un fisier de 100 MB.
HOSTS = [
    "https://download.thinkbroadband.com/100MB.zip",
    "https://proof.ovh.net/files/100Mb.dat",
]

RUN_LARGE = os.environ.get("CATSSoft_RUN_LARGE", "") not in ("", "0", "false", "False")


def _first_reachable_host():
    import requests
    for url in HOSTS:
        try:
            response = requests.get(url, headers={"Range": "bytes=0-0"},
                                    timeout=12, stream=True)
            try:
                if response.status_code == 206:
                    return url
            finally:
                response.close()
        except Exception:
            continue
    return None


@unittest.skipUnless(RUN_LARGE,
                     "test optional - porneste cu CATSSoft_RUN_LARGE=1")
class LargeFileTests(unittest.TestCase):

    def setUp(self):
        self.app_module = load_app()
        self.workdir = tempfile.mkdtemp(prefix="catssoft_large_")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _reference_hash(self, url):
        import requests
        digest = hashlib.sha256()
        with requests.get(url, stream=True, timeout=60) as response:
            for chunk in response.iter_content(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def test_segmented_download_of_a_large_file_matches_the_reference(self):
        url = _first_reachable_host()
        if not url:
            self.skipTest("nicio gazda cu suport Range nu e accesibila")

        reference = self._reference_hash(url)

        folder = os.path.join(self.workdir, "dl")
        os.makedirs(folder, exist_ok=True)
        dl = self.app_module.Download(url=url, filename="large.bin",
                                      save_path=os.path.join(folder, "large.bin"),
                                      segments=8)
        dl.status = self.app_module.DLStatus.DOWNLOADING
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()

        events = queue.Queue()
        self.app_module.download_segmented(dl, events)

        statuses = []
        while True:
            try:
                item = events.get_nowait()
            except queue.Empty:
                break
            if item[0] == "status":
                statuses.append(item[2])
        self.assertTrue(statuses, "motorul nu a raportat niciun status")
        self.assertEqual(statuses[-1], self.app_module.DLStatus.COMPLETED,
                         "eroare: %s" % (dl.error_msg or "necunoscuta"))

        # Calea segmentata a fost cea folosita (chunk_size_bytes ramane 0 in calea simpla)
        self.assertGreater(dl.chunk_size_bytes, 0, "drumul segmentat nu a fost folosit")
        self.assertGreater(len(dl.chunk_progress), 1,
                           "grila de fragmente a produs un singur interval")

        # Hash identic cu descarcarea de referinta
        digest = hashlib.sha256()
        with open(dl.save_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self.assertEqual(digest.hexdigest(), reference,
                         "fisierul descarcat difera de referinta")

        # Niciun segment temporar ramas
        leftovers = [name for name in os.listdir(folder) if ".part" in name]
        self.assertEqual(leftovers, [], "au ramas fragmente temporare")


if __name__ == "__main__":
    unittest.main(verbosity=2)
