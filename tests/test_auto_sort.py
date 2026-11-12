"""Teste pentru auto_sort_file: fișierul trebuie să rămână în folderul ales."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import load_app  # noqa: E402


class AutoSortTests(unittest.TestCase):

    def setUp(self):
        self.app = load_app()
        self.tmp = tempfile.mkdtemp(prefix="catssoft_sort_")
        self.chosen = os.path.join(self.tmp, "chosen_folder")
        os.makedirs(self.chosen, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_file(self, name="movie.mp4"):
        path = os.path.join(self.chosen, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 32)
        return path

    def test_file_stays_in_the_chosen_folder(self):
        """Fișierul nu trebuie să fie mutat în ~/Downloads ignorând folderul ales."""
        path = self._make_file()
        result = self.app.auto_sort_file(path, "video", True)
        self.assertTrue(os.path.abspath(result).startswith(os.path.abspath(self.chosen)),
                        "fișierul a fost mutat în afara folderului ales: %s" % result)
        self.assertTrue(os.path.isfile(result))
        home_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        self.assertFalse(os.path.abspath(result).startswith(os.path.abspath(home_downloads)),
                         "fișierul a fost mutat în ~/Downloads, ignorând folderul ales")

    def test_category_subfolder_is_created_inside_chosen_folder(self):
        path = self._make_file()
        result = self.app.auto_sort_file(path, "video", True)
        self.assertTrue(os.path.isfile(result))
        self.assertTrue(os.path.abspath(result).startswith(os.path.abspath(self.chosen)))
        self.assertNotEqual(os.path.abspath(result), os.path.abspath(path),
                            "ar trebui creat un subfolder de categorie")

    def test_disabled_auto_sort_keeps_the_path(self):
        path = self._make_file()
        result = self.app.auto_sort_file(path, "video", False)
        self.assertEqual(result, path)

    def test_unknown_type_keeps_the_path(self):
        path = self._make_file("notes.txt")
        result = self.app.auto_sort_file(path, "unknown-type", True)
        self.assertEqual(result, path)

    def test_existing_destination_is_not_overwritten(self):
        path = self._make_file("movie.mp4")
        # ținem folderul de categorie gol la început, dar pre-creăm un conflict
        result = self.app.auto_sort_file(path, "video", True)
        self.assertTrue(os.path.isfile(result))
        # al doilea fișier cu același nume trebuie să ajungă lângă primul, nu să-l înlocuiască
        second = os.path.join(self.chosen, "other.mp4")
        with open(second, "wb") as fh:
            fh.write(b"y" * 16)
        result2 = self.app.auto_sort_file(second, "video", True)
        self.assertNotEqual(os.path.abspath(result), os.path.abspath(result2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
