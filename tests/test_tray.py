"""Teste de regresie pentru integrarea cu bara de sistem (tray).

Bug prins in auditul manual: `minimize_to_tray()` ascundea fereastra INAINTE
sa porneasca iconul. Daca pystray esua (zona de notificari indisponibila,
eroare de platforma), aplicatia ramanea ascunsa, fara icon si fara cale de
intoarcere - parea ca a disparut complet.

Noile comportament: iconul porneeste primul, fereastra se ascunde doar dupa ce
firul e confirmat viu. Altfel fereastra ramane vizibila si se intoarce False.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _FakeIcon:
    """Icon fals: ruleaza cat timp e cerut sau se opreste imediat."""

    def __init__(self, fail_immediately=False):
        self.fail_immediately = fail_immediately
        self.stopped = False
        self.title = "CatsSoft Downloader"

    def run(self):
        if self.fail_immediately:
            return
        while not self.stopped:
            time.sleep(0.02)

    def stop(self):
        self.stopped = True


class TrayIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app_module = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _app_stub(self):
        class Stub:
            def __init__(self):
                self.withdrawn = False
                self.deiconified = False
                self.lifted = False

            def withdraw(self):
                self.withdrawn = True

            def deiconify(self):
                self.deiconified = True

            def lift(self):
                self.lifted = True

        return Stub()

    def test_minimize_hides_the_window_when_the_tray_starts(self):
        stub = self._app_stub()
        tray = self.app_module.TrayManager(stub)
        tray.icon = _FakeIcon()
        self.assertTrue(tray.minimize_to_tray())
        self.assertTrue(stub.withdrawn, "fereastra trebuie ascunsa cand tray-ul porneste")
        tray.stop()

    def test_failed_tray_keeps_the_window_visible(self):
        """Daca iconul moare imediat, fereastra NU trebuie ascunsa."""
        stub = self._app_stub()
        tray = self.app_module.TrayManager(stub)
        tray.icon = _FakeIcon(fail_immediately=True)
        self.assertFalse(tray.minimize_to_tray(),
                         "minimize_to_tray trebuie sa semnaleze esecul")
        self.assertFalse(stub.withdrawn,
                         "fereastra nu poate fi ascunsa daca nu exista icon")
        self.assertTrue(stub.deiconified, "fereastra trebuie readusa in fata")

    def test_missing_icon_is_reported(self):
        stub = self._app_stub()
        tray = self.app_module.TrayManager(stub)
        self.assertFalse(tray.minimize_to_tray())
        self.assertFalse(stub.withdrawn)

    def test_stop_ends_the_tray_thread(self):
        stub = self._app_stub()
        tray = self.app_module.TrayManager(stub)
        tray.icon = _FakeIcon()
        tray.minimize_to_tray()
        time.sleep(0.1)
        self.assertTrue(tray.tray_thread.is_alive())
        tray.stop()
        deadline = time.time() + 5
        while time.time() < deadline and tray.tray_thread.is_alive():
            time.sleep(0.05)
        self.assertFalse(tray.tray_thread.is_alive(), "firul tray nu s-a oprit")

    def test_close_without_tray_exits_instead_of_hiding(self):
        """Butonul X, cu tray indisponibil, trebuie sa inchida aplicatia."""
        app = self.app_module.DownloadManager()
        app.withdraw()
        app.update()
        app.tray_manager.icon = None
        exited = {"value": False}
        original_exit = app._exit_app

        def fake_exit():
            exited["value"] = True
            original_exit()

        app._exit_app = fake_exit
        try:
            app._on_close()
            self.assertTrue(exited["value"],
                            "fara tray functional, inchiderea trebuie sa iasa din aplicatie")
        finally:
            try:
                app._closing = True
                app.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
