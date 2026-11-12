"""Teste pentru validarea reluării, fișiere temporare orfane și modul degradat."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class ResumeValidationTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.folder = os.path.join(self.env.downloads_dir, "resume")
        os.makedirs(self.folder, exist_ok=True)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_unverifiable_output_is_not_deleted(self):
        """Fără metadate nu ștergem fișierul: pierdem doar reluarea, nu datele."""
        target = os.path.join(self.folder, "video.mp4")
        with open(target, "wb") as fh:
            fh.write(b"x" * 500)
        dl = self.app.Download(url="https://x.test/video.mp4", filename="video.mp4", save_path=target)
        self.app._validate_or_reset_resume_artifacts(dl, 1000, {})
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(dl.resume_blocked)

    def test_changed_resource_removes_output(self):
        target = os.path.join(self.folder, "movie.mkv")
        with open(target, "wb") as fh:
            fh.write(b"y" * 100)
        self.app._write_download_meta(target, {"url": "https://x.test/other.mkv", "total": 100,
                                               "etag": "old-etag", "last_modified": ""})
        dl = self.app.Download(url="https://x.test/movie.mkv", filename="movie.mkv", save_path=target)
        self.app._validate_or_reset_resume_artifacts(dl, 100, {"ETag": "new-etag"})
        self.assertFalse(os.path.isfile(target))

    def test_etag_is_not_compared_against_last_modified(self):
        """Tipuri diferite de validator NU înseamnă conflict."""
        target = os.path.join(self.folder, "clip.mp4")
        with open(target, "wb") as fh:
            fh.write(b"z" * 100)
        self.app._write_download_meta(target, {"url": "https://x.test/clip.mp4", "total": 100,
                                               "etag": "tag-1", "last_modified": ""})
        dl = self.app.Download(url="https://x.test/clip.mp4", filename="clip.mp4", save_path=target)
        self.app._validate_or_reset_resume_artifacts(
            dl, 100, {"Last-Modified": "Mon, 01 Jan 2035 00:00:00 GMT"})
        self.assertTrue(os.path.isfile(target))

    def test_segment_parts_helper_tolerates_empty_path(self):
        self.app._remove_segment_parts(None)   # nu trebuie să arunce TypeError


class OrphanTempFileTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.folder = os.path.join(self.env.downloads_dir, "orphans")
        os.makedirs(self.folder, exist_ok=True)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _touch(self, name, age_seconds=0):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as fh:
            fh.write(b"data")
        if age_seconds:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))
        return path

    def test_scanner_finds_orphans_and_protects_active(self):
        self._touch("active.mp4")
        self._touch("active.mp4.part0")
        self._touch("active.mp4.part1")
        self._touch("active.mp4.part0.race12345")
        self._touch("orphan.mp4.part0", age_seconds=7200)
        self._touch("recent.mp4.part0")

        found = self.app.scan_orphan_temp_files(self.folder, protected_bases={"active.mp4"},
                                                 min_age_seconds=3600)
        names = {os.path.basename(p) for p, _s, _a in found}
        self.assertIn("orphan.mp4.part0", names)
        self.assertNotIn("active.mp4.part0", names)
        self.assertNotIn("recent.mp4.part0", names)

    def test_scanner_detects_metadata_and_race_leftovers(self):
        self._touch("file.zip.catssoft-meta.json", age_seconds=7200)
        self._touch("file.zip.catssoft-meta.json.tmp", age_seconds=7200)
        found = self.app.scan_orphan_temp_files(self.folder, protected_bases=set(),
                                                 min_age_seconds=0)
        names = {os.path.basename(p) for p, _s, _a in found}
        self.assertIn("file.zip.catssoft-meta.json", names)
        self.assertIn("file.zip.catssoft-meta.json.tmp", names)

    def test_scanner_ignores_regular_files(self):
        self._touch("movie.mp4", age_seconds=7200)
        found = self.app.scan_orphan_temp_files(self.folder, protected_bases=set(),
                                                 min_age_seconds=0)
        self.assertEqual(found, [])


class DegradedModeTests(unittest.TestCase):
    """Baza de date coruptă sau inaccesibilă NU trebuie să oprească aplicația."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_corrupt_database_falls_back_to_temporary_db(self):
        corrupt = os.path.join(self.env.tmp, "corrupt.db")
        with open(corrupt, "wb") as fh:
            fh.write(b"this is definitely not a sqlite database file" * 40)

        db = self.app.DatabaseManager(corrupt)
        self.assertTrue(db.degraded_mode)
        self.assertNotEqual(db.db_path, corrupt)
        self.assertTrue(os.path.isfile(db.db_path))

        cfg = self.app.ConfigManager(db)
        cfg.set_many({"max_concurrent": 7})
        self.assertEqual(self.app.ConfigManager(db).get("max_concurrent"), 7)

    def test_uncreatable_folder_falls_back(self):
        blocked = os.path.join(self.env.tmp, "blocked")
        with open(blocked, "wb") as fh:
            fh.write(b"x")   # un fișier, nu un folder
        db = self.app.DatabaseManager(os.path.join(blocked, "sub", "x.db"))
        self.assertTrue(db.degraded_mode)
        self.assertEqual(db.get_setting("x", 1), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
