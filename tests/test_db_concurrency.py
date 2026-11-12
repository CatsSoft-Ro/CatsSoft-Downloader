"""Teste de regresie pentru ciclul de viață al conexiunilor SQLite.

v6.13 a înlocuit conexiunile scurte (deschise și închise la fiecare operație) cu
conexiuni persistente, câte una per fir. Au apărut două riscuri noi:

1. ``close()`` închide conexiunile tuturor firilor; un fir activ primea
   "Cannot operate on a closed database" și pierdea date.
2. Firurile scurte (fiecare căutare în istoric, fiecare export) lăsau în urmă
   handle-uri SQLite deschise până la ieșirea aplicației.

Conexiunile moarte trebuie recreate transparent, iar cele ale firurilor
terminate trebuie închise prompt.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class DatabaseConnectionLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.db = self.app.DatabaseManager(self.env.db_path)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        self.env.__exit__(None, None, None)

    # ── Recupere după close() ───────────────────────────────────────────────
    def test_connection_is_recreated_after_close(self):
        """După close(), următoarea operație nu trebuie să arunce."""
        self.db.save_setting("k", "v1")
        self.db.close()
        # get_history nu înghite erorile SQLite: arunca ProgrammingError înainte de fix.
        self.assertIsInstance(self.db.get_history(10), list)
        self.assertEqual(self.db.get_setting("k"), "v1")

    def test_other_thread_recovers_from_close(self):
        """close() închisă conexiunea altui fir; firul trebuie să se auto-repară."""
        self.db.save_setting("k", "v1")
        ready = threading.Event()
        proceed = threading.Event()
        errors = []
        results = []

        def worker():
            try:
                self.db.get_history(1)          # creează conexiunea thread-local
                ready.set()
                proceed.wait(5)
                self.db.get_history(1)          # după close() global
                results.append(self.db.get_setting("k"))
            except Exception as exc:          # noqa: BLE001 - reținem orice regresie
                errors.append(exc)

        thread = threading.Thread(target=worker, name="TestWorker")
        thread.start()
        self.assertTrue(ready.wait(5), "firul nu a pornit")
        self.db.close()                        # închide și conexiunea firului
        proceed.set()
        thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results, ["v1"])

    def test_close_is_idempotent(self):
        self.db.close()
        self.db.close()
        self.assertIsInstance(self.db.get_history(1), list)

    # ── Curățarea handle-urilor ─────────────────────────────────────────────
    def test_short_lived_threads_do_not_leak_connections(self):
        """Fire scurte (cautări în istoric) nu trebuie să lase conexiuni deschise."""
        def worker():
            self.db.get_history(1)

        for _ in range(30):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        # Orice acces ulterior declanșează sweep-ul.
        self.db.get_history(1)
        self.assertLessEqual(
            len(self.db._connections), 2,
            "conexiunile firelor terminate ar trebui închise, nu păstrate")

    def test_connection_is_reused_within_a_thread(self):
        """Același fir trebuie să reutilizeze conexiunea (optimizarea rămâne validă)."""
        first = self.db._connect(10)
        second = self.db._connect(10)
        self.assertIs(first, second)

    def test_concurrent_writes_from_threads(self):
        """Scrieri concurente din mai multe fire: nicio excepție, toate rândurile salvate."""
        errors = []

        def worker(index):
            try:
                for step in range(5):
                    self.db.save_setting("key_%d_%d" % (index, step), step)
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual(errors, [])
        for index in range(6):
            for step in range(5):
                self.assertEqual(self.db.get_setting("key_%d_%d" % (index, step)), step)


if __name__ == "__main__":
    unittest.main(verbosity=2)
