"""Sonda rula intr-un proces separat: verifica calea reala a toast-ului de clipboard.

De ce proces separat? CustomTkinter 6.0.0 intra in "access violation" la
crearea unei A DOUA radacini CTk in acelasi proces, dupa ce a existat un
CTkToplevel (confirmat prin bisej: doar CTkToplevel declanseaza, un
tkinter.Toplevel obisnuit nu). Aplicația reală creează o singură rădăcină
CTk, deci nu e expusă; suita de teste însă creează mai multe ferestre, deci
sonda trebuie izolată.

Scriptul afișează marcaje pe care testul părinte le verifică:
    TIMER_CANCELLED   - butonul "Ignoră" a anulat timerul pending
    SECOND_TOAST_OK   - un al doilea toast se creează fără crash
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _helpers import load_app  # noqa: E402

mod = load_app()


def find_button(widget, text):
    for child in widget.winfo_children():
        try:
            if child.cget("text") == text and hasattr(child, "invoke"):
                return child
        except Exception:
            pass
        found = find_button(child, text)
        if found is not None:
            return found
    return None


def main():
    app = mod.DownloadManager()
    monitor = app.clipboard_monitor
    try:
        monitor._show_notification("https://example.com/f.zip", "f.zip")
        toast = monitor._toast_window
        timer_id = monitor._toast_close_id
        if toast is None or timer_id is None:
            print("PROBE_FAIL: toast sau timer lipsa")
            return 2
        button = find_button(toast, "Ignoră")
        if button is None:
            print("PROBE_FAIL: butonul Ignora nu a fost gasit")
            return 3
        button.invoke()
        pending_after = [str(item) for item in app.tk.call("after", "info")]
        if any(timer_id in item for item in pending_after):
            print("PROBE_FAIL: timerul a ramas pendent dupa inchiderea toast-ului")
            return 4
        print("TIMER_CANCELLED")
        if monitor.notification_shown:
            print("PROBE_FAIL: monitorul a ramas blocat")
            return 5
        # Al doilea toast, în aceeași sesiune: cu timerul rămas pendent, acesta
        # ar arde peste un widget distrus și ar omorî procesul.
        monitor._toast_window = None
        monitor._toast_close_id = None
        monitor.notification_shown = False
        monitor._show_notification("https://example.com/g.zip", "g.zip")
        if monitor._toast_window is None:
            print("PROBE_FAIL: al doilea toast nu a fost creat")
            return 6
        print("SECOND_TOAST_OK")
        # Verificare suplimentară: un callback `after` care își distruge
        # propriul widget în timpul execuției. Ramura `finally` din
        # `Misc.after` apelează `deletecommand` după ce `Misc.destroy` a pus
        # `_tclCommands = None` → „AttributeError: 'NoneType' object has no
        # attribute 'remove'" → „Exception in Tkinter callback".
        errors = []
        app.report_callback_exception = lambda exc, val, tb: errors.append(exc)
        frame = mod.tk.Frame(app)

        def _destroy_self():
            frame.destroy()

        frame.after(10, _destroy_self)
        app.update()
        if errors:
            print("PROBE_FAIL: deletecommand pe widget distrus: %r" % (errors,))
            return 7
        print("DELETCOMMAND_OK")
        return 0
    finally:
        try:
            monitor.stop()
        except Exception:
            pass
        try:
            app._closing = True
            app.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
