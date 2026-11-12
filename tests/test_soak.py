"""Soak test: rulează aplicația reală timp de câteva secunde și verifică că nu
apare nicio excepție în log (bucla UI de 10 Hz, schedulerul, monitorul de rețea,
bara de stare)."""
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402

SOAK_SECONDS = 8


class SoakTests(unittest.TestCase):

    def test_app_runs_without_exceptions(self):
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        previous_level = root.level
        root.setLevel(logging.INFO)

        env = IsolatedEnvironment()
        env.__enter__()
        app = None
        try:
            app = load_app().DownloadManager()
            app.after(SOAK_SECONDS * 1000, app.destroy)
            started = time.time()
            app.mainloop()
            elapsed = time.time() - started
            self.assertGreaterEqual(elapsed, SOAK_SECONDS * 0.8)
        finally:
            if app is not None:
                try:
                    app._closing = True
                    app.destroy()
                except Exception:
                    pass
            root.removeHandler(handler)
            root.setLevel(previous_level)
            env.__exit__(None, None, None)

        errors = [r for r in records if r.levelno >= logging.ERROR and r.exc_info]
        details = ["\n".join(logging.Formatter().formatException(r.exc_info)) for r in errors]
        self.assertEqual(details, [], "excepții în timpul rulării:\n%s" % "\n---\n".join(details))

    def test_exit_app_closes_cleanly(self):
        """Calea reală de închidere (butonul X) trebuie să se încheie fără excepții."""
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        previous_level = root.level
        root.setLevel(logging.INFO)

        env = IsolatedEnvironment()
        env.__enter__()
        app = None
        try:
            app = load_app().DownloadManager()
            app.update()
            app._exit_app()
            self.assertTrue(app._closing)
        finally:
            if app is not None:
                try:
                    app._closing = True
                    app.destroy()
                except Exception:
                    pass
            root.removeHandler(handler)
            root.setLevel(previous_level)
            env.__exit__(None, None, None)

        errors = [r for r in records if r.levelno >= logging.ERROR and r.exc_info]
        details = ["\n".join(logging.Formatter().formatException(r.exc_info)) for r in errors]
        self.assertEqual(details, [], "excepții la închidere:\n%s" % "\n---\n".join(details))


if __name__ == "__main__":
    unittest.main(verbosity=2)
