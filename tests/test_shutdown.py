"""Teste de închidere: distrugerea widget-urilor nu trebuie să arunce TclError.

Regresie prinsă de utilizator: „can't delete Tcl command" la închiderea
aplicației cu panoul de detalii deschis. Tk șterge comanda unui callback când
timerul lui ``after`` este anulat, dar tkinter o păstrează în ``_tclCommands``,
iar ``Misc.destroy`` încearcă apoi să ștergă comanda deja dispărută.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class ShutdownTests(unittest.TestCase):

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

    def _make_download(self):
        dl = self.app_module.Download(
            url="https://example.test/f.zip", filename="f.zip",
            save_path=os.path.join(self.env.downloads_dir, "f.zip"),
            status=self.app_module.DLStatus.DOWNLOADING,
            total=100 * 1024 * 1024, downloaded=50 * 1024 * 1024, speed=1024 * 1024)
        dl.app = self.app
        self.app.downloads.append(dl)
        self.app._render_card(dl)
        self.app.update()
        return dl

    def test_close_with_details_view_open(self):
        """Scenariul reprodus de utilizator: detalii deschise + butonul X."""
        dl = self._make_download()
        self.app._open_download_details(dl)
        self.app.update()
        self.app._exit_app()
        self.assertTrue(self.app._closing)

    def test_open_then_close_details_view_quickly(self):
        """Timerul de refresh nu a apucat să firească: comanda Tcl e orfană."""
        dl = self._make_download()
        self.app._open_download_details(dl)
        self.app.update()
        self.app._close_download_details()
        self.app.update()

    def test_reopen_details_view_repeatedly(self):
        dl = self._make_download()
        for _ in range(3):
            self.app._open_download_details(dl)
            self.app.update()
            self.app._close_download_details()
            self.app.update()

    def test_destroy_is_idempotent_after_details_view(self):
        dl = self._make_download()
        self.app._open_download_details(dl)
        self.app.update()
        self.app._close_download_details()
        self.app.update()
        self.app._exit_app()
        self.assertTrue(self.app._closing)

    def test_prune_removes_only_dead_commands(self):
        """Helperul păstrează comenzile vii și le elimină pe cele dispărute."""
        widget = self.app
        widget._register(lambda: None)
        commands = list(getattr(widget, "_tclCommands", []) or [])
        self.assertTrue(commands)
        # ștergem forțat una dintre comenzi
        victim = commands[0]
        try:
            widget.tk.deletecommand(victim)
        except Exception:
            self.skipTest("nu se poate șterge comanda pentru test")
        self.app_module._prune_dead_commands(widget)
        remaining = list(getattr(widget, "_tclCommands", []) or [])
        self.assertNotIn(victim, remaining)
        self.assertLessEqual(len(remaining), len(commands))


if __name__ == "__main__":
    unittest.main(verbosity=2)
