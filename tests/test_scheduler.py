"""Teste pentru DownloadScheduler: programare 'once', eșec la pornire, reîncercare limitată, recurență."""
import os
import sys
import time
import unittest
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _FakeApp:
    """Aplicație minimală: schedulerul are nevoie doar de config, post_ui și _add_download."""

    def __init__(self, default_folder, raise_on_add=False):
        self.config = {"default_folder": default_folder}
        self.raise_on_add = raise_on_add
        self.added = []
        self.warnings = 0

    def post_ui(self, callback):
        callback()

    def _add_download(self, url, folder, filename, segments=1, checksum=""):
        if self.raise_on_add:
            raise OSError("disc plin (simulat)")
        self.added.append((url, folder, filename, segments, checksum))


class SchedulerTests(unittest.TestCase):

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.db = self.app.DatabaseManager(self.env.db_path)
        # messagebox.showwarning ar deschide o fereastră modală (blochează
        # suita) și cere un master Tk real; aplicația falsă nu are unul.
        # Îl înlocuim cu un recorder, ca să putem verifica și calea de avertizare.
        self.warnings = []
        self._original_showwarning = messagebox.showwarning
        messagebox.showwarning = self._record_warning

    def tearDown(self):
        messagebox.showwarning = self._original_showwarning
        self.env.__exit__(None, None, None)

    def _record_warning(self, *args, **kwargs):
        self.warnings.append(args)

    def _fire(self, scheduler, cycles=1):
        """Execută ciclul de verificare al scheduler-ului (aceeași logică ca în _check_scheduler)."""
        for _ in range(cycles):
            now = self.app.datetime.now()
            for sd in scheduler.scheduled[:]:
                if sd.enabled and now >= sd.scheduled_time:
                    if sd.repeat == "once":
                        sd.enabled = False
                    scheduler.app.post_ui(lambda x=sd: scheduler._execute_download(x))
                    if sd.repeat in ("daily", "weekly"):
                        previous_time = sd.scheduled_time
                        interval = (self.app.timedelta(days=1) if sd.repeat == "daily"
                                    else self.app.timedelta(weeks=1))
                        next_time = previous_time + interval
                        while next_time <= now:
                            next_time += interval
                        sd.scheduled_time = next_time
                        scheduler.db.update_scheduled_time(sd, previous_time)
            time.sleep(0.02)

    def _scheduler(self, raise_on_add=False):
        fake = _FakeApp(self.env.downloads_dir, raise_on_add=raise_on_add)
        return self.app.DownloadScheduler(fake, self.db), fake

    def test_once_schedule_is_removed_only_after_successful_start(self):
        scheduler, fake = self._scheduler()
        past = self.app.datetime.now() - self.app.timedelta(minutes=1)
        scheduler.add("https://x.test/a.zip", "a.zip", past, self.env.downloads_dir, "once", 1, "")
        self.assertEqual(len(self.db.get_scheduled()), 1)

        self._fire(scheduler)
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(len(self.db.get_scheduled()), 0,
                         "programarea 'once' trebuie eliminată după ce transferul a fost acceptat")

    def test_once_schedule_survives_a_failed_start(self):
        """Eroarea la pornire NU trebuie să piardă programarea."""
        scheduler, fake = self._scheduler(raise_on_add=True)
        past = self.app.datetime.now() - self.app.timedelta(minutes=1)
        scheduler.add("https://x.test/b.zip", "b.zip", past, self.env.downloads_dir, "once", 1, "")
        self.assertEqual(len(self.db.get_scheduled()), 1)

        self._fire(scheduler)
        self.assertEqual(len(self.db.get_scheduled()), 1,
                         "programarea s-a pierdut la eșec - trebuie păstrată pentru reîncercare")

    def test_failed_schedule_is_disabled_after_three_attempts(self):
        scheduler, _fake = self._scheduler(raise_on_add=True)
        past = self.app.datetime.now() - self.app.timedelta(minutes=1)
        scheduler.add("https://x.test/c.zip", "c.zip", past, self.env.downloads_dir, "once", 1, "")

        for _ in range(6):
            self._fire(scheduler)
        still_enabled = [s for s in scheduler.scheduled if s.enabled]
        self.assertEqual(still_enabled, [], "programarea ar trebui dezactivată după 3 eșecuri")
        self.assertTrue(self.warnings,
                        "utilizatorul ar trebui avertizat că programarea a eșuat")

    def test_daily_schedule_advances_to_the_next_occurrence(self):
        scheduler, fake = self._scheduler()
        past = self.app.datetime.now() - self.app.timedelta(days=2)
        scheduler.add("https://x.test/d.zip", "d.zip", past, self.env.downloads_dir, "daily", 1, "")

        self._fire(scheduler)
        self.assertEqual(len(fake.added), 1)
        self.assertTrue(any(s.scheduled_time > self.app.datetime.now() for s in scheduler.scheduled),
                        "următoarea apariție trebuie să fie în viitor")

    def test_disabled_schedule_does_not_fire(self):
        scheduler, fake = self._scheduler()
        past = self.app.datetime.now() - self.app.timedelta(minutes=1)
        sd = self.app.ScheduledDownload(url="https://x.test/e.zip", filename="e.zip",
                                        scheduled_time=past, save_path=self.env.downloads_dir,
                                        repeat="once", enabled=False, segments=1, checksum="")
        scheduler.scheduled.append(sd)
        self.db.add_scheduled(sd)
        self._fire(scheduler)
        self.assertEqual(len(fake.added), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
