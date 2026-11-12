"""Teste pe gazde REALE, prin calea completă de UI (AddURLDialog -> download -> istoric).

Aceste teste se sar automat dacă nu există conexiune la internet, deci suita
rămâne rulabilă offline. Sunt mai lente decât restul (transferuri reale) și
verifică exact ce nu poate fi simulat: servere care nu suportă Range, servere
care răspund 200 la cereri Range, redirecturi și anteturi reale.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402

# Gazde reale cu comportamente diferite, alese pentru a acoperi ambele drumuri
# din motor: server cu suport Range (206 + Content-Range) și server fără Range (200).
HOST_WITH_RANGE = ("https://files.pythonhosted.org/packages/source/r/requests/"
                   "requests-2.31.0.tar.gz")
HOST_WITHOUT_RANGE = "https://speed.cloudflare.com/__down?bytes=3145728"

_ONLINE = None

# Semne de rețea întreruptă: o gazdă externă care pică nu trebuie să facă
# suita să eșueze - testul e sărit, nu marcat ca defect al aplicației.
NETWORK_ERRORS = ("connection", "timed out", "timeout", "reset", "aborted",
                  "max retries", "failed to establish", "ssl", "proxy",
                  "temporarily", "name resolution")


def online():
    global _ONLINE
    if _ONLINE is None:
        _ONLINE = False
        try:
            import requests
            # Ambele gazde sunt verificate: una singură poate răspunde HEAD
            # dar să cadă la transfer, și atunci suita ar eșua pentru un motiv
            # care nu are nimic de-a face cu aplicația.
            for host in (HOST_WITHOUT_RANGE, HOST_WITH_RANGE):
                requests.head(host, timeout=6, allow_redirects=True)
            _ONLINE = True
        except Exception:
            _ONLINE = False
    return _ONLINE


def pump(app, predicate, timeout=120.0):
    """Avansează bucla UI până când predicatul devine adevărat.

    Nu folosim app.update(): timerul _poll_updates se re-programează singur la
    fiecare 100 ms, deci update() nu ar reveni niciodată. Procesăm explicit
    coada de evenimente, ceea ce este determinist și nu depinde de Tk.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            app.update_idletasks()
            app._poll_updates()
        except Exception:
            pass
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


@unittest.skipUnless(online(), "fără conexiune la internet - testele reale sunt sărite")
class RealWorldTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app_module = load_app()
        self.app = self.app_module.DownloadManager()
        self.app.update()

    def tearDown(self):
        try:
            self.app._closing = True
            self.app.destroy()
        except Exception:
            pass
        self.env.__exit__(None, None, None)

    def _add_via_dialog(self, url, segments):
        """Parcurge calea reală a utilizatorului: dialog -> verificare -> descărcare."""
        added = []
        dialog = self.app_module.AddURLDialog(
            self.app, lambda *args, **kwargs: added.append(args) or self.app._add_download(*args, **kwargs))
        dialog.update()
        dialog.url_entry.delete(0, "end")
        dialog.url_entry.insert(0, url)
        dialog.path_entry.delete(0, "end")
        dialog.path_entry.insert(0, self.env.downloads_dir)
        dialog.segments_entry.delete(0, "end")
        dialog.segments_entry.insert(0, str(segments))
        dialog.update()

        dialog._submit()

        # verificarea rulează într-un fir; rezultatul revine prin coada UI
        started = pump(self.app, lambda: bool(added), timeout=45)
        if not started:
            # Dialogul nu a pornit nimic în 45 de secunde: gazda externă nu
            # răspunde. E o problemă de mediu, nu de aplicație.
            self.skipTest("rețea externă indisponibilă: descărcarea nu a pornit")
        # butoanele trebuie reactivate după terminarea verificării
        self.assertEqual(dialog.url_entry.cget("state"), "normal")
        self.assertEqual(dialog.submit_btn.cget("state"), "normal")
        self.assertFalse(dialog._submit_in_progress)
        try:
            dialog.destroy()
        except Exception:
            pass
        return added[0]

    def _await_completion(self, filename, timeout=120):
        def done():
            return any(d.filename == filename and d.status in
                       (self.app_module.DLStatus.COMPLETED, self.app_module.DLStatus.ERROR)
                       for d in self.app.downloads)
        finished = pump(self.app, done, timeout=timeout)
        for d in self.app.downloads:
            if d.filename == filename:
                self._skip_if_network_error(d)
                if not finished:
                    # Nu s-a transferat niciun octet în 120 de secunde: rețeaua
                    # nu a livrat nimic, deci e o problemă de mediu, nu de
                    # aplicație. Dacă s-a transferat ceva și tot s-a oprit,
                    # e o blocare reală și testul trebuie să eșueze.
                    if not d.downloaded:
                        self.skipTest("rețea externă indisponibilă: niciun octet transferat "
                                      "în %ds" % timeout)
                    self.fail("descărcarea s-a blocat după %d octeți" % d.downloaded)
                return d
        self.assertTrue(finished, "descărcarea nu a fost pornită")
        return None

    def _skip_if_network_error(self, dl):
        """Sare testul dacă gazda externă a fost indisponibilă (nu e defect al aplicației)."""
        if dl is None:
            return
        message = (dl.error_msg or "").lower()
        if dl.status == self.app_module.DLStatus.ERROR and any(k in message for k in NETWORK_ERRORS):
            self.skipTest("gazdă externă indisponibilă: %s" % (dl.error_msg or "")[:140])

    @staticmethod
    def _server_supports_range(url):
        """True dacă gazda răspunde 206 la o cerere Range (drumul segmentat)."""
        try:
            import requests
            response = requests.get(url, headers={"Range": "bytes=0-0"},
                                    timeout=10, stream=True)
            try:
                return response.status_code == 206
            finally:
                response.close()
        except Exception:
            return False

    # ── Server cu suport Range (drumul segmentat) ───────────────────────────
    def test_real_host_with_range_downloads_completely(self):
        # Proba se face ÎNAINTE: dacă rețeaua e degradată, aplicația cade corect
        # pe calea simplă, iar asta nu e o regresie a motorului.
        ranged = self._server_supports_range(HOST_WITH_RANGE)
        url, folder, filename, segments, checksum = self._add_via_dialog(HOST_WITH_RANGE, 4)
        dl = self._await_completion(filename)
        self.assertIsNotNone(dl)
        self.assertEqual(dl.status, self.app_module.DLStatus.COMPLETED,
                         "eroare: %s" % (dl.error_msg or "necunoscută"))
        self.assertTrue(os.path.isfile(dl.save_path))
        self.assertGreater(os.path.getsize(dl.save_path), 0)
        self.assertEqual(dl.total, os.path.getsize(dl.save_path))
        if not ranged:
            self.skipTest("serverul nu a răspuns cu 206 la proba Range; "
                          "aplicația a folosit corect calea simplă")
        # Calea segmentată e confirmată de dimensiunea fragmentului: în calea
        # simplă chunk_size_bytes rămâne 0. Numărul de fragmente nu e un criteriu
        # (grila e de 1-8 MiB, deci un fișier mic are un singur interval).
        self.assertGreater(dl.chunk_size_bytes, 0, "drumul segmentat nu a fost folosit")

    # ── Server fără Range (drumul simplu) ────────────────────────────────────
    def test_real_host_without_range_downloads_completely(self):
        url, folder, filename, segments, checksum = self._add_via_dialog(HOST_WITHOUT_RANGE, 1)
        dl = self._await_completion(filename)
        self.assertIsNotNone(dl)
        self.assertEqual(dl.status, self.app_module.DLStatus.COMPLETED,
                         "eroare: %s" % (dl.error_msg or "necunoscută"))
        self.assertTrue(os.path.isfile(dl.save_path))
        self.assertEqual(os.path.getsize(dl.save_path), 3145728)

    # ── Verificarea URL-ului eșuat NU pornește descărcarea ──────────────────
    def test_failed_verification_does_not_add_download(self):
        import tkinter.messagebox as mb
        original = mb.askyesno
        mb.askyesno = lambda *a, **k: False      # utilizatorul refuză
        try:
            added = []
            dialog = self.app_module.AddURLDialog(self.app, lambda *a, **k: added.append(a))
            dialog.update()
            dialog.url_entry.delete(0, "end")
            dialog.url_entry.insert(0, "https://127.0.0.1:1/nu-exista.bin")
            dialog.path_entry.delete(0, "end")
            dialog.path_entry.insert(0, self.env.downloads_dir)
            dialog.update()
            dialog._submit()
            pump(self.app, lambda: False, timeout=12)
            self.assertEqual(added, [], "nu trebuie adăugată o descărcare cu link invalid")
            self.assertEqual(len(self.app.downloads), 0)
            try:
                dialog.destroy()
            except Exception:
                pass
        finally:
            mb.askyesno = original

    # ── Istoricul se scrie pentru descărcările reale ────────────────────────
    def test_real_download_is_recorded_in_history(self):
        url, folder, filename, segments, checksum = self._add_via_dialog(HOST_WITHOUT_RANGE, 1)
        dl = self._await_completion(filename)
        self.assertIsNotNone(dl)
        rows = self.app.db.get_history()
        # get_history() întoarce obiecte DownloadHistory, nu tupluri.
        self.assertTrue(any(r.filename == filename for r in rows),
                        "descărcarea nu a ajuns în istoric")


if __name__ == "__main__":
    unittest.main(verbosity=2)
