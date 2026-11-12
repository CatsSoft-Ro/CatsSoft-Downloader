"""Teste pentru interfața grafică: randarea cardului, ETA, tooltips, garda swipe."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _FakeEvent:
    def __init__(self, widget, x=200, y=200):
        self.widget = widget
        self.x_root = x
        self.y_root = y
        self.x = x
        self.y = y


class UiTests(unittest.TestCase):

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

    def _make_card(self, filename="file.zip", status=None, total=0, downloaded=0, speed=0.0):
        status = status or self.app_module.DLStatus.DOWNLOADING
        dl = self.app_module.Download(
            url="https://example.test/" + filename, filename=filename,
            save_path=os.path.join(self.env.downloads_dir, filename),
            status=status, total=total, downloaded=downloaded, speed=speed)
        dl.app = self.app
        self.app.downloads.append(dl)
        self.app._render_card(dl)
        self.app.update()
        return dl, self.app.cards.get(id(dl))

    def test_card_is_rendered(self):
        dl, card = self._make_card()
        self.assertIsNotNone(card)

    def test_long_filename_is_truncated_but_keeps_extension(self):
        long_name = "a-really-very-long-file-name-that-should-be-truncated-for-the-card-layout.zip"
        dl, card = self._make_card(filename=long_name)
        shown = card.name_lbl.cget("text")
        self.assertLess(len(shown), len(long_name))
        self.assertTrue(shown.endswith(".zip"))

    def test_eta_is_shown_while_downloading(self):
        dl, card = self._make_card(total=100 * 1024 * 1024, downloaded=50 * 1024 * 1024,
                                   speed=2 * 1024 * 1024)
        card.refresh_ui()
        self.app.update()
        self.assertIn("⏱", card.eta_lbl.cget("text"))
        self.assertTrue(card.eta_lbl.winfo_ismapped())

    def test_eta_is_hidden_when_not_downloading(self):
        dl, card = self._make_card(status=self.app_module.DLStatus.COMPLETED)
        card.refresh_ui()
        self.app.update()
        self.assertFalse(card.eta_lbl.winfo_ismapped())
        self.assertFalse(card.sparkline_lbl.winfo_ismapped())
        self.assertEqual(card.pause_btn.cget("state"), "disabled")

    def test_action_buttons_have_tooltips(self):
        dl, card = self._make_card()
        for button in (card.pause_btn, card.stop_btn, card.folder_btn,
                       card.del_btn, card.retry_btn, card.details_btn):
            self.assertIsNotNone(getattr(button, "_tooltip", None),
                                 "buton fără tooltip: %r" % button)

    def test_swipe_is_ignored_when_started_on_a_button(self):
        """Un glisaj pornit de pe buton nu trebuie să declanșeze și acțiunea butonului."""
        dl, card = self._make_card()
        host = self.app.swipe_hosts.get(id(dl))
        self.assertIsNotNone(host)
        controller = host._controller

        controller._press(_FakeEvent(card.pause_btn))
        self.assertTrue(controller.ignore_gesture)
        controller._move(_FakeEvent(card.pause_btn, x=400))
        self.assertFalse(controller.dragging)

    def test_swipe_is_allowed_on_the_card_body(self):
        dl, card = self._make_card()
        host = self.app.swipe_hosts.get(id(dl))
        controller = host._controller
        controller._press(_FakeEvent(card))
        self.assertFalse(controller.ignore_gesture)

    def test_filter_does_not_repack_unchanged_set(self):
        dl, card = self._make_card()
        before = self.app._visible_signature
        self.app._filter_cards()
        self.assertEqual(before, self.app._visible_signature)


if __name__ == "__main__":
    unittest.main(verbosity=2)
