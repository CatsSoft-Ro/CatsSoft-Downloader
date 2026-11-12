"""Teste de regresie pentru runda 3 de audit.

Trei zone, fiecare verificată pe mașina reală înainte de fixare:

1. Multi-adaptor: un adaptor fără rută (ex. un switch virtual) eșuează la
   fiecare segment și consuma bugetul de reîncercări al segmentului. Promisiunea
   din documentația clasei („workers can fall back to the normal system session")
   nu era implementată: adaptorul mort revenea în rotație după cooldown și
   descărcarea putea eșua chiar dacă exista o cale funcțională.

2. Clipboard: butonul „Ignoră" distrugea toast-ul fără să anuleze timerul de
   10 secunde. Timerul supraviețuia distrugerii, iar la următorul `update()`
   (CustomTkinter îl apelează în constructorul fiecărei ferestre) Tcl încerca
   să execute comanda ștearsă → „access violation", un crash dur al procesului.

3. Persistența cozii: dacă dialogul de reluare arunca o excepție, ciclul
   periodic de salvare nu pornea niciodată, iar reluarea la pornire se pierde
   definitiv pentru sesiunea respectivă.

Notă: testele nu creează ferestre Tk reale în procesul principal - CustomTkinter
6.0.0 intră în „access violation" la o a doua rădăcină CTk după ce a existat un
CTkToplevel în același proces. Calea reală a toast-ului este verificată într-un
proces separat, prin `_toast_probe.py`.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import load_app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class _ConfigStub:
    def __init__(self, **data):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def _make_profile(key, ip="192.168.0.2"):
    return {
        "key": key, "name": key.split("|")[0], "ip": ip, "available": True,
        "session": object(), "active_streams": 0, "bytes_session": 0,
        "samples": [], "speed_bps": 0.0, "errors": 0, "cooldown_until": 0.0,
        "consecutive_failures": 0, "missing_refreshes": 0, "limit_bps": 0,
        "total_bytes": 0, "throttle_deadline": 0.0,
    }


class _FakeToast:
    """Toast fals: nu are nevoie de Tk, doar de comportamentul așteptat."""

    def __init__(self):
        self.cancelled = []
        self.destroyed = False

    def after_cancel(self, timer_id):
        self.cancelled.append(timer_id)

    def winfo_exists(self):
        return True

    def destroy(self):
        self.destroyed = True


class _FakeApp:
    def __init__(self):
        self.config = _ConfigStub()
        self.timers = []
        self._next = 0

    def after(self, ms, func=None, *args):
        self._next += 1
        timer_id = "after#%d" % self._next
        self.timers.append(timer_id)
        return timer_id

    def after_cancel(self, timer_id):
        if timer_id in self.timers:
            self.timers.remove(timer_id)


class MultiAdapterFallbackTests(unittest.TestCase):
    """Traseele de rețea moarte trebuie scoase din rotație."""

    def setUp(self):
        self.mod = load_app()

    def _manager(self, keys):
        cfg = _ConfigStub(multi_network_enabled=True, multi_network_selected=list(keys),
                          network_speed_limits={}, network_usage={})
        manager = self.mod.NetworkAdapterManager(cfg)
        for key in keys:
            manager._profiles[key] = _make_profile(key)
        return manager

    def test_dead_adapter_is_removed_from_rotation(self):
        manager = self._manager(["Wi-Fi|192.168.0.2"])
        manager.report_failure("Wi-Fi|192.168.0.2", OSError("bind failed"))
        self.assertFalse(manager._profiles["Wi-Fi|192.168.0.2"]["available"],
                         "traseul care a eșuat nu mai trebuie selectat")
        key, session = manager.acquire_session()
        self.assertEqual(key, "system",
                         "fără trasee sănătoase se revine pe sesiunea de sistem")
        self.assertIs(session, self.mod.SESSION)

    def test_healthy_adapter_is_preferred_over_the_dead_one(self):
        manager = self._manager(["Dead|10.0.0.1", "Wi-Fi|192.168.0.2"])
        manager.report_failure("Dead|10.0.0.1", OSError("bind failed"))
        key, _session = manager.acquire_session()
        self.assertEqual(key, "Wi-Fi|192.168.0.2",
                         "traseul sănătos trebuie preferat celui mort")

    def test_dead_adapter_stays_out_of_rotation_after_the_cooldown(self):
        """Cooldown-ul de 5 secunde nu este suficient.

        După expirarea lui, adaptorul mort trebuie să rămână în afara rotației.
        Altfel el este realesat, arde un segment, consumă bugetul de reîncercări
        și poate face descărcarea să eșueze chiar dacă există o cale funcțională.
        """
        manager = self._manager(["Dead|10.0.0.1"])
        manager.report_failure("Dead|10.0.0.1", OSError("bind failed"))
        manager._profiles["Dead|10.0.0.1"]["cooldown_until"] = 0.0
        key, _session = manager.acquire_session()
        self.assertEqual(key, "system",
                         "traseul mort nu trebuie realesat după expirarea cooldown-ului")

    def test_healthy_adapter_is_preferred_after_the_cooldown_expires(self):
        manager = self._manager(["Dead|10.0.0.1", "Wi-Fi|192.168.0.2"])
        manager.report_failure("Dead|10.0.0.1", OSError("bind failed"))
        manager._profiles["Dead|10.0.0.1"]["cooldown_until"] = 0.0
        key, _session = manager.acquire_session()
        self.assertEqual(key, "Wi-Fi|192.168.0.2",
                         "după expirarea cooldown-ului, adaptorul mort nu mai poate fi ales")

    def test_refresh_revalidates_a_removed_adapter(self):
        manager = self._manager(["Wi-Fi|192.168.0.2"])
        manager._discover = lambda: [("Wi-Fi", "192.168.0.2")]
        manager.report_failure("Wi-Fi|192.168.0.2", OSError("bind failed"))
        self.assertFalse(manager._profiles["Wi-Fi|192.168.0.2"]["available"])
        manager.refresh()
        self.assertTrue(manager._profiles["Wi-Fi|192.168.0.2"]["available"],
                        "reîmprospătarea trebuie să revalideze traseul scos din rotație")
        key, _session = manager.acquire_session()
        self.assertEqual(key, "Wi-Fi|192.168.0.2")

    def test_healthy_adapter_stays_in_rotation(self):
        manager = self._manager(["Wi-Fi|192.168.0.2"])
        manager.report_bytes("Wi-Fi|192.168.0.2", 4096)
        key, _session = manager.acquire_session()
        self.assertEqual(key, "Wi-Fi|192.168.0.2")

    def test_failure_counter_is_tracked(self):
        manager = self._manager(["Wi-Fi|192.168.0.2"])
        manager.report_failure("Wi-Fi|192.168.0.2", OSError("x"))
        profile = manager._profiles["Wi-Fi|192.168.0.2"]
        self.assertGreaterEqual(profile["consecutive_failures"], 1)
        self.assertGreaterEqual(profile["errors"], 1)

    def test_system_session_is_used_when_multi_network_is_off(self):
        manager = self._manager(["Wi-Fi|192.168.0.2"])
        manager.set_multi_enabled(False)
        key, session = manager.acquire_session()
        self.assertEqual(key, "system")
        self.assertIs(session, self.mod.SESSION)


class ClipboardToastLogicTests(unittest.TestCase):
    """Toast-ul nu trebuie să lase timeri pendenți (logică, fără Tk)."""

    def setUp(self):
        self.mod = load_app()
        self.app = _FakeApp()
        self.monitor = self.mod.ClipboardMonitor(self.app)

    def _show(self):
        toast = _FakeToast()
        self.monitor._toast_window = toast
        self.monitor._toast_close_id = "after#7"
        self.monitor.notification_shown = True
        return toast

    def test_close_toast_cancels_the_pending_timer(self):
        toast = self._show()
        self.monitor._close_toast()
        self.assertEqual(toast.cancelled, ["after#7"],
                         "timerul trebuie anulat înainte de distrugerea toast-ului")
        self.assertTrue(toast.destroyed)
        self.assertIsNone(self.monitor._toast_close_id)
        self.assertFalse(self.monitor.notification_shown,
                         "monitorul trebuie să poată notifica din nou")

    def test_stop_cancels_the_toast_timer_and_resets_state(self):
        toast = self._show()
        self.monitor.stop()
        self.assertEqual(toast.cancelled, ["after#7"])
        self.assertTrue(toast.destroyed)
        self.assertFalse(self.monitor.notification_shown)
        self.assertIsNone(self.monitor._toast_close_id)

    def test_stop_cancels_the_clipboard_poll_timer(self):
        self.monitor.start()
        self.assertEqual(len(self.app.timers), 1)
        self.monitor.stop()
        self.assertEqual(self.app.timers, [],
                         "timerul de polling trebuie anulat la stop()")

    def test_monitor_can_notify_again_after_stop_and_start(self):
        self._show()
        self.monitor.stop()
        self.monitor.start()
        self.assertFalse(self.monitor.notification_shown)
        self.monitor.stop()


class ClipboardToastProcessTests(unittest.TestCase):
    """Calea reală (Tk adevărat), verificată într-un proces separat."""

    def test_ignore_button_cancels_timer_and_second_toast_survives(self):
        probe = os.path.join(HERE, "_toast_probe.py")
        self.assertTrue(os.path.isfile(probe), "sonda lipsește: %s" % probe)
        result = subprocess.run(
            [sys.executable, probe], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240)
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        self.assertEqual(result.returncode, 0,
                         "sonda a eșuat (cod %s):\n%s" % (result.returncode, combined[-2500:]))
        self.assertIn("TIMER_CANCELLED", combined,
                      "butonul „Ignoră\" trebuie să anuleze timerul toast-ului")
        self.assertIn("SECOND_TOAST_OK", combined,
                      "un al doilea toast trebuie să se creeze fără crash")
        self.assertIn("DELETCOMMAND_OK", combined,
                      "deletecommand pe un widget deja distrus nu trebuie să "
                      "producă „Exception in Tkinter callback\"")


class _StubDb:
    def __init__(self, entries):
        self.entries = list(entries)

    def load_active_queue(self):
        return list(self.entries)


class _StubApp:
    def __init__(self, entries):
        self.db = _StubDb(entries)
        self.persisted = []
        self.waited = []

    def wait_window(self, dialog):
        self.waited.append(dialog)

    def _persist_active_queue(self):
        self.persisted.append(True)


class ActiveQueuePersistenceTests(unittest.TestCase):
    """Ciclul de salvare a cozii pornește chiar dacă dialogul de reluare eșuează."""

    def setUp(self):
        self.mod = load_app()
        self.original_dialog = self.mod.ResumeQueueDialog

    def tearDown(self):
        self.mod.ResumeQueueDialog = self.original_dialog

    def _check(self, entries, dialog=None):
        stub = _StubApp(entries)
        if dialog is not None:
            self.mod.ResumeQueueDialog = dialog
        self.mod.DownloadManager._check_resumable_queue(stub)
        return stub

    def test_persist_loop_starts_even_if_the_resume_dialog_fails(self):
        class _Boom:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("dialog indisponibil")

        stub = self._check(
            [{"url": "https://example.com/f.zip", "filename": "f.zip", "save_path": "p"}],
            dialog=_Boom)
        self.assertTrue(stub.persisted,
                        "ciclul de persistență trebuie pornit chiar dacă dialogul aruncă "
                        "- altfel coada activă nu mai este salvată niciodată")

    def test_persist_loop_starts_without_entries(self):
        stub = self._check([])
        self.assertTrue(stub.persisted)

    def test_persist_loop_starts_after_a_successful_dialog(self):
        class _Ok:
            def __init__(self, *args, **kwargs):
                pass

            def destroy(self):
                pass

        stub = self._check(
            [{"url": "https://example.com/f.zip", "filename": "f.zip", "save_path": "p"}],
            dialog=_Ok)
        self.assertTrue(stub.persisted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
