"""Teste de regresie pentru ciclul de viață al widget-urilor.

Un timer ``after`` care arde după distrugerea widgetului său lasă în consolă
„Exception in Tkinter callback": tkinter apelează ``deletecommand`` pe un widget
distrus, unde ``Misc.destroy`` a setat deja ``_tclCommands = None`` →
``AttributeError: 'NoneType' object has no attribute 'remove'`` (tkinter 3.10
prinde acolo doar ``TclError``).

Este exact familia bug-ului raportat de utilizator („can't delete Tcl command").
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _TclErrorTrap:
    """Capturază erorile raportate de Tk (invalid command name ...)."""

    def __init__(self, root):
        self.root = root
        self.messages = []

    def __enter__(self):
        def report(message, *args):
            self.messages.append(str(message))
        self.root.report_callback_exception = report
        return self

    def __exit__(self, *exc_info):
        return False


class WidgetLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app_module = load_app()
        self.app = self.app_module.DownloadManager()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self.env.__exit__(None, None, None)

    def test_pending_after_is_cancelled_before_destroy(self):
        """Timerul pendent al widgetului trebuie anulat înainte de distrugere."""
        fired = []
        label = self.app_module.ctk.CTkLabel(self.app, text="x")
        # Timer scurt: dacă nu e anulat, callbackul arde în timpul testului.
        label.after(300, lambda: fired.append(1))

        pending = str(self.app.tk.call("after", "info"))
        self.assertIn("after#", pending, "sanity: ar trebui să existe un timer pendent")

        label.destroy()

        deadline = time.time() + 1.2
        while time.time() < deadline:
            self.app.update()
            time.sleep(0.05)
        self.assertEqual(fired, [], "timerul a ars după distrugerea widgetului")

    def test_destroying_a_dialog_with_pending_timer_raises_nothing(self):
        """Dialogul de adăugare are un `after` de centrare; distrugerea lui
        imediată trebuie să anuleze timerul.

        Verificarea anterioară („nu apare nicio excepție Python") nu era suficientă:
        când timerul rămâne pendent, Tkinter încearcă să execute comanda ștearsă
        de `Misc.destroy` - o eroare la nivel Tcl, care NU ajunge la
        `report_callback_exception`, deci trecea oricum. Aici verificăm direct
        că timerul dialogului dispare din lista de timeri pending.
        """
        with _TclErrorTrap(self.app) as trap:
            dialog = self.app_module.AddURLDialog(self.app, lambda *a, **k: None)
            dialog.withdraw()
            dialog.update()

            # Timer propriu, cu durată lungă: rămâne sigur pending în momentul
            # distrugerii, deci putem urmări exact ce se întâmplă cu el.
            dialog.after(3000, lambda: None)
            # Comențele se citesc DUPĂ programarea timerului, ca să-l includem.
            commands = set(getattr(dialog, "_tclCommands", None) or [])
            pending_ids = list(self.app.tk.call("after", "info"))
            dialog_timers = []
            for event_id in pending_ids:
                info = self.app.tk.call("after", "info", event_id)
                script = str(info[0] if isinstance(info, (tuple, list)) else info)
                if any(command in script for command in commands):
                    dialog_timers.append(event_id)
            self.assertTrue(dialog_timers,
                            "sanity: dialogul ar trebui să aibă un timer pendent")

            dialog.destroy()

            pending_after = list(self.app.tk.call("after", "info"))
            for event_id in dialog_timers:
                self.assertNotIn(
                    event_id, pending_after,
                    "timerul dialogului trebuie anulat la distrugere; altfel "
                    "Tkinter va încerca să execute o comandă ștearsă")

            # Lasă timpul de simulare să treacă: un timer rămas ar arde aici.
            for _ in range(20):
                self.app.update()
                time.sleep(0.05)
        self.assertEqual(trap.messages, [],
                         "excepții Tkinter la distrugerea dialogului: %s" % trap.messages)

    def test_prune_helper_is_safe_on_plain_objects(self):
        """Helperul trebuie să reziste la obiecte fără Tk."""
        class Dummy:
            pass

        self.app_module._prune_dead_commands(Dummy())
        self.app_module._cancel_pending_after(Dummy())

    def test_app_window_destroy_is_clean(self):
        """Fereastra principală se închide fără excepții Tkinter."""
        with _TclErrorTrap(self.app) as trap:
            self.app.destroy()
            for _ in range(20):
                try:
                    self.app.update()
                except Exception:
                    break
                time.sleep(0.05)
        self.assertEqual(trap.messages, [],
                         "excepții Tkinter la închiderea ferestrei: %s" % trap.messages)

    def test_many_widgets_destroyed_leave_no_pending_timers(self):
        """Distrugerea în masă a widget-urilor nu trebuie să lase timere active."""
        widgets = []
        for _ in range(12):
            frame = self.app_module.ctk.CTkFrame(self.app)
            frame.after(3000, lambda: None)
            widgets.append(frame)
        with _TclErrorTrap(self.app) as trap:
            for frame in widgets:
                frame.destroy()
            for _ in range(20):
                self.app.update()
                time.sleep(0.05)
        self.assertEqual(trap.messages, [],
                         "excepții Tkinter: %s" % trap.messages)


    def test_visible_signature_is_invalidated_when_the_list_changes(self):
        """Semnatura vizibilitatii trebuie invalidata la adaugare si eliminare.

        Semnatura e construita din ``id()``-uri, iar CPython poate refolosi un
        id pentru un obiect nou. Fara invalidare, o comparatie egala ar sari
        reimpachetarea, iar cardul nou ar putea ramane neafisat.

        Verificam valoarea semnaturii exact in momentul in care filtrarea este
        apelata: acolo trebuie sa fie ``None``, nu valoarea veche.
        """
        dl = self.app_module.Download(url="http://127.0.0.1:1/x.bin", filename="x.bin",
                                     save_path=os.path.join(self.env.downloads_dir, "x.bin"))
        seen = []
        original_filter = self.app._filter_cards

        def spy():
            seen.append(getattr(self.app, "_visible_signature", None))
            return original_filter()

        self.app._filter_cards = spy
        try:
            self.app.downloads.append(dl)
            self.app._visible_signature = ("sentinel",)
            self.app._render_card(dl)
            self.assertTrue(seen, "sanity: filtrarea trebuie apelata")
            self.assertIsNone(seen[-1],
                              "semnatura trebuie invalidata inainte de filtrare, "
                              "la adaugarea unui card")

            seen.clear()
            self.app._visible_signature = ("sentinel",)
            self.app._remove_download(dl)
            self.assertTrue(seen, "sanity: filtrarea trebuie apelata")
            self.assertIsNone(seen[-1],
                              "semnatura trebuie invalidata inainte de filtrare, "
                              "la eliminarea unui card")
        finally:
            self.app._filter_cards = original_filter


if __name__ == "__main__":
    unittest.main(verbosity=2)
