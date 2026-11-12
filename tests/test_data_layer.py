"""Teste pentru stratul de date: SQLite, setări, programări, istoric, export CSV."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class DataLayerTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    # ── DatabaseManager ───────────────────────────────────────────────────────
    def test_db_file_created(self):
        db = self.app.DatabaseManager(self.env.db_path)
        self.assertTrue(os.path.isfile(self.env.db_path))
        self.assertFalse(db.degraded_mode)

    def test_batch_save_persists_all_settings(self):
        db = self.app.DatabaseManager(self.env.db_path)
        cfg = self.app.ConfigManager(db)
        cfg.set_many({"max_concurrent": 5, "theme_mode": "light", "accent_color": "#ff0000"})
        reloaded = self.app.ConfigManager(db)
        self.assertEqual(reloaded.get("max_concurrent"), 5)
        self.assertEqual(reloaded.get("accent_color"), "#ff0000")

    def test_theme_mode_is_loaded(self):
        """Regula: alegerea Light/Dark/Auto trebuie să supraviețuiască repornirii."""
        db = self.app.DatabaseManager(self.env.db_path)
        cfg = self.app.ConfigManager(db)
        cfg.set_many({"theme_mode": "light"})
        reloaded = self.app.ConfigManager(db)
        self.assertEqual(reloaded.get("theme_mode"), "light")

    def test_scheduled_survives_legacy_column_order(self):
        """ALTER TABLE mută coloanele la sfârșit: SELECT * cu indexare pozițională pierde rândul."""
        legacy = os.path.join(self.env.tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("""CREATE TABLE scheduled (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                       filename TEXT NOT NULL, save_path TEXT, scheduled_time TEXT)""")
        conn.execute("ALTER TABLE scheduled ADD COLUMN repeat_type TEXT")
        conn.execute("ALTER TABLE scheduled ADD COLUMN enabled INTEGER")
        conn.execute("ALTER TABLE scheduled ADD COLUMN segments INTEGER")
        conn.execute("ALTER TABLE scheduled ADD COLUMN checksum TEXT")
        conn.execute(
            "INSERT INTO scheduled (url, filename, save_path, scheduled_time, repeat_type, enabled, segments, checksum)"
            " VALUES ('https://x.test/a.zip','a.zip','/tmp','2030-01-01T10:00:00','once',1,4,'')")
        conn.commit()
        conn.close()

        legacy_db = self.app.DatabaseManager(legacy)
        rows = legacy_db.get_scheduled()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://x.test/a.zip")
        self.assertEqual(rows[0].repeat, "once")
        self.assertEqual(rows[0].segments, 4)

    def test_scheduled_roundtrip(self):
        db = self.app.DatabaseManager(self.env.db_path)
        sd = self.app.ScheduledDownload(
            url="https://x.test/f.zip", filename="f.zip",
            scheduled_time=self.app.datetime(2030, 1, 1, 10, 0, 0),
            save_path=self.env.downloads_dir, repeat="daily", enabled=True, segments=2, checksum="")
        db.add_scheduled(sd)
        rows = db.get_scheduled()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].filename, "f.zip")
        self.assertEqual(rows[0].repeat, "daily")

    # ── Istoric ───────────────────────────────────────────────────────────────
    def test_history_add_and_read(self):
        db = self.app.DatabaseManager(self.env.db_path)
        sample = self.app.Download(
            url="https://x.test/file.zip", filename="file.zip",
            save_path=os.path.join(self.env.downloads_dir, "file.zip"),
            status=self.app.DLStatus.COMPLETED, total=2048, downloaded=2048,
            added_date=self.app.datetime.now(), completed_date=self.app.datetime.now())
        db.add_history(sample)
        self.assertEqual(len(db.get_history()), 1)

    def test_export_cancelled_keeps_chosen_file_intact(self):
        """Un export anulat nu trebuie să trunchieze fișierul ales de utilizator."""
        db = self.app.DatabaseManager(self.env.db_path)
        sample = self.app.Download(
            url="https://x.test/file.zip", filename="file.zip",
            save_path=os.path.join(self.env.downloads_dir, "file.zip"),
            status=self.app.DLStatus.COMPLETED, total=10, downloaded=10,
            added_date=self.app.datetime.now(), completed_date=self.app.datetime.now())
        db.add_history(sample)

        target = os.path.join(self.env.tmp, "export.csv")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("PRE-EXISTING CONTENT")

        always_cancelled = type("E", (), {"is_set": staticmethod(lambda: True)})()
        result = db.export_history_csv(
            target, mode="all", search_term="", status_filter="Toate", period_filter="Toate",
            type_filter="Toate", sort_order="Cele mai noi", selected_ids=(), cancel_event=always_cancelled)

        self.assertEqual(result.get("status"), "cancelled")
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "PRE-EXISTING CONTENT")
        self.assertFalse(os.path.exists(target + ".part"))

    def test_export_completed_moves_temp_file_into_place(self):
        db = self.app.DatabaseManager(self.env.db_path)
        sample = self.app.Download(
            url="https://x.test/file.zip", filename="file.zip",
            save_path=os.path.join(self.env.downloads_dir, "file.zip"),
            status=self.app.DLStatus.COMPLETED, total=10, downloaded=10,
            added_date=self.app.datetime.now(), completed_date=self.app.datetime.now())
        db.add_history(sample)

        target = os.path.join(self.env.tmp, "export_ok.csv")
        never_cancelled = type("E", (), {"is_set": staticmethod(lambda: False)})()
        db.export_history_csv(
            target, mode="all", search_term="", status_filter="Toate", period_filter="Toate",
            type_filter="Toate", sort_order="Cele mai noi", selected_ids=(), cancel_event=never_cancelled)

        self.assertTrue(os.path.isfile(target))
        self.assertGreater(os.path.getsize(target), 0)
        self.assertFalse(os.path.exists(target + ".part"))

    # ── Helperi ───────────────────────────────────────────────────────────────
    def test_sanitize_filename(self):
        cleaned = self.app.sanitize_filename("a/b\\c?.zip")
        self.assertNotIn("/", cleaned)
        self.assertNotIn("\\", cleaned)

    def test_safe_int_clamps(self):
        self.assertEqual(self.app.safe_int("999", 1, 0, 10), 10)
        self.assertEqual(self.app.safe_int("nope", 7, 0, 10), 7)

    def test_fmt_speed_returns_string(self):
        self.assertIsInstance(self.app.fmt_speed(1024 * 1024), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
