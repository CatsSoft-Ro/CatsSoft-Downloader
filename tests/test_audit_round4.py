"""Teste pentru runda 4 de audit (coadă/UI + calea segmentată).

Constatările verificate aici:

1. **Calea segmentată nu răspundea la Stop.** Calea segmentată este calea
   implicită (Adaptive Streams e activat implicit) și ține mai multe răspunsuri
   HTTP deschise simultan. ``_stop_download`` închidea doar
   ``dl.active_response``, setat DOAR în calea simplă.
2. **Stop congela interfața.** Pe Windows, ``close()`` pe un socket cu un
   ``recv`` în așteptare blochează până la expirarea timeout-ului de citire
   (15-25 s). Corecția din runda anterioară (inchiderea răspunsului activ) se
   executa pe firul UI, deci un server care nu mai trimite nimic congela
   interfața timp de 15-25 s la apăsarea butonului Stop.
3. **Guard mort în ``_stop_download``.** Condiția care cerea înregistrarea
   anulării pentru o reîncercare automată programată citea ``dl.auto_retry_at``
   DUPĂ ce acesta fusese resetat cu 10 linii mai devreme - deci era mereu
   falsă, iar anularea nu ajungea în istoric.
4. **Guard mort în ``_poll_updates``.** ``self._poll_job_id`` era resetat la
   ``None`` la intrarea în metodă, deci ``after_cancel`` nu anula niciodată
   timerul în așteptare: un apel manual al metodei ar fi acumulat bucle de
   polling paralele (invariantul era doar declarat în comentariu).
"""
import os
import queue
import socket
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _helpers import IsolatedEnvironment, load_app  # noqa: E402


class _StopStub:
    """Aplicație minimă pentru _stop_download (doar actualizările de UI)."""

    def __init__(self):
        self.refreshed = []
        self.update_queue = queue.Queue()

    def _refresh_card(self, dl):
        self.refreshed.append(dl)

    def _update_stats(self):
        pass

    def _update_status_bar(self):
        pass

    def _next_queued(self):
        pass

    def _filter_cards(self):
        pass

    def _persist_queue_snapshot_now(self):
        pass


class _PollStub:
    """Aplicație minimă pentru _poll_updates: cronometre simulate, fără Tk."""

    def __init__(self):
        self.pending = set()
        self.next_id = 1
        self.update_queue = queue.Queue()
        self.downloads = []
        self._closing = False
        self._current_tab = "Downloads"
        self._poll_job_id = None
        self._poll_updates = lambda: None
        self.smart_automation = types.SimpleNamespace(tick=lambda: False)

    def winfo_exists(self):
        return True

    def after(self, _ms, _func):
        job = self.next_id
        self.next_id += 1
        self.pending.add(job)
        return job

    def after_cancel(self, job):
        self.pending.discard(job)

    def _refresh_card(self, _dl):
        pass

    def _update_stats(self):
        pass

    def _filter_cards(self):
        pass

    def _refresh_queue(self):
        pass

    def _update_status_bar(self):
        pass


class _SegmentServer:
    """Server local cu suport Range şi două moduri de livrare.

    ``mode="stall"``   - trimite anteturile şi apoi nu mai trimite nimic.
    ``mode="trickle"`` - trimite anteturile şi apoi câte 64 KiB la 0,3 s,
                        deci fiecare citire se încheie în câteva sute de ms.
    """

    def __init__(self, mode="stall", total=1024 * 1024):
        self.mode = mode
        self.total = total
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.url = "http://127.0.0.1:%d/segment.bin" % self.port
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(20)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            last = self.total - 1
            conn.sendall(
                b"HTTP/1.1 206 Partial Content\r\n"
                b"Content-Length: " + str(self.total).encode() + b"\r\n"
                b"Content-Range: bytes 0-" + str(last).encode() + b"/"
                + str(self.total).encode() + b"\r\n"
                b"Accept-Ranges: bytes\r\n"
                b"ETag: \"segment-etag\"\r\n"
                b"Connection: close\r\n\r\n")
            if self.mode == "trickle":
                block = b"\xa5" * (64 * 1024)
                sent = 0
                while sent < self.total and not self._stop.is_set():
                    conn.sendall(block)
                    sent += len(block)
                    time.sleep(0.3)
            else:
                # Server mut: anteturile au sosit, corpul nu mai vine.
                time.sleep(20)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class SegmentedStopTests(unittest.TestCase):
    """Stop trebuie să funcţioneze în calea segmentată, fără să congele UI-ul."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.close()
        self.env.__exit__(None, None, None)

    def _make_download(self, name="x.bin"):
        folder = os.path.join(self.env.downloads_dir, name)
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url="http://127.0.0.1:1/x.bin", filename=name,
                               save_path=os.path.join(folder, name))
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        dl.status = self.app.DLStatus.DOWNLOADING
        return dl, folder

    def test_stop_download_closes_the_segment_responses(self):
        dl, _folder = self._make_download()

        class _Resp:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        first, second = _Resp(), _Resp()
        dl.active_responses.extend([first, second])

        self.app.DownloadManager._stop_download(_StopStub(), dl)

        self.assertTrue(first.closed, "primul răspuns segmentat trebuie închis la Stop")
        self.assertTrue(second.closed, "al doilea răspuns segmentat trebuie închis la Stop")
        self.assertTrue(dl.stop_event.is_set())

    def test_stop_download_does_not_block_the_ui_on_a_stalled_server(self):
        """Un server mort nu trebuie să congeleze interfața la Stop."""
        self.server = _SegmentServer(mode="stall")
        dl, folder = self._make_download("stall.bin")
        dl.url = self.server.url
        segment_path = os.path.join(folder, "stall.bin.part0")
        errors = []

        thread = threading.Thread(
            target=lambda: self.app._download_segment(
                dl, self.server.url, segment_path, 0, self.server.total - 1,
                queue.Queue(), errors, threading.Lock(), max_segment_retries=0,
                chunk_index=None, chunk_cancel_event=None, count_progress=True),
            daemon=True)
        thread.start()

        deadline = time.time() + 8
        while time.time() < deadline and not dl.active_responses:
            time.sleep(0.05)
        self.assertTrue(dl.active_responses,
                        "răspunsul HTTP al segmentului trebuie urmărit pentru a-l putea închide")

        started = time.time()
        self.app.DownloadManager._stop_download(_StopStub(), dl)
        elapsed = time.time() - started
        print("\n    [Stop cu server mort] Stop a durat %.2f s (limita: 1,5 s)" % elapsed)
        self.assertLess(elapsed, 1.5,
                        "Stop a blocat firul interfeței cu %.2f s" % elapsed)
        self.assertEqual(dl.status, self.app.DLStatus.CANCELLED,
                         "descărcarea trebuie marcată anulată imediat")

    def test_stop_download_interrupts_a_live_segment(self):
        """Pe o conexiune vie, segmentul trebuie să se oprească imediat."""
        self.server = _SegmentServer(mode="trickle")
        dl, folder = self._make_download("trickle.bin")
        dl.url = self.server.url
        segment_path = os.path.join(folder, "trickle.bin.part0")
        errors = []

        thread = threading.Thread(
            target=lambda: self.app._download_segment(
                dl, self.server.url, segment_path, 0, self.server.total - 1,
                queue.Queue(), errors, threading.Lock(), max_segment_retries=0,
                chunk_index=None, chunk_cancel_event=None, count_progress=True),
            daemon=True)
        thread.start()

        deadline = time.time() + 8
        while time.time() < deadline and not dl.active_responses:
            time.sleep(0.05)
        self.assertTrue(dl.active_responses,
                        "răspunsul HTTP al segmentului trebuie urmărit pentru a-l putea închide")

        started = time.time()
        self.app.DownloadManager._stop_download(_StopStub(), dl)
        stop_elapsed = time.time() - started
        thread.join(timeout=6)
        elapsed = time.time() - started

        print("\n    [Stop cu server lent] Stop: %.2f s, segment oprit după %.2f s"
              % (stop_elapsed, elapsed))
        self.assertLess(stop_elapsed, 1.5,
                        "Stop a blocat firul interfeței cu %.2f s" % stop_elapsed)
        self.assertFalse(thread.is_alive(),
                         "segmentul nu s-a oprit la Stop (a durat %.2f s)" % elapsed)
        self.assertLess(elapsed, 5.0,
                        "oprirea segmentului a durat prea mult: %.2f s" % elapsed)


class ScheduledRetryCancelTests(unittest.TestCase):
    """Anularea unei reîncercări automate programate trebuie să ajungă în istoric."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_cancel_of_a_scheduled_retry_is_queued_as_an_event(self):
        folder = os.path.join(self.env.downloads_dir, "retry")
        os.makedirs(folder, exist_ok=True)
        dl = self.app.Download(url="http://127.0.0.1:1/x.bin", filename="x.bin",
                               save_path=os.path.join(folder, "x.bin"))
        dl.stop_event = threading.Event()
        dl.status = self.app.DLStatus.QUEUED
        # Reîncercare automată programată: contorul e nenul.
        dl.auto_retry_at = time.time() + 60

        release = threading.Event()

        def linger():
            release.wait(10)

        worker = threading.Thread(target=linger, daemon=True)
        worker.start()
        dl.thread = worker

        stub = _StopStub()
        self.app.DownloadManager._stop_download(stub, dl)

        events = []
        while not stub.update_queue.empty():
            kind, _obj, extra = stub.update_queue.get_nowait()
            events.append((kind, extra))

        self.assertTrue(
            any(kind == "status" and extra == self.app.DLStatus.CANCELLED
                for kind, extra in events),
            "anularea unei reîncercări programate trebuie înregistrată în istoric; "
            "evenimentele primite: %r" % (events,))
        release.set()


class PollTimerTests(unittest.TestCase):
    """Apelurile repetate la _poll_updates nu trebuie să acumuleze cronometre."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_repeated_poll_calls_keep_a_single_timer(self):
        stub = _PollStub()
        self.app.DownloadManager._poll_updates(stub)
        self.assertEqual(len(stub.pending), 1,
                         "bucla de polling trebuie să aibă un singur cronometru")
        first_job = stub._poll_job_id

        # Un apel manual suplimentar nu trebuie să creeze o a doua buclă.
        self.app.DownloadManager._poll_updates(stub)
        self.assertEqual(len(stub.pending), 1,
                         "apelul manual a acumulat cronometre: %r" % (stub.pending,))
        self.assertNotEqual(stub._poll_job_id, first_job,
                            "cronometrul vechi trebuie anulat și înlocuit")


class ConfigureGuardTests(unittest.TestCase):
    """Handler-ul ``<Configure>`` nu trebuie să deseneze după distrugerea canvas-ului."""

    def setUp(self):
        self.env = IsolatedEnvironment()
        self.env.__enter__()
        self.app = load_app()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_update_dimensions_event_is_guarded(self):
        import customtkinter as ctk
        handler = getattr(ctk.CTkBaseClass, "_update_dimensions_event", None)
        self.assertIsNotNone(handler)
        self.assertTrue(getattr(handler, "_catssoft_patched", False),
                        "handler-ul Configure trebuie păzuit: altfel fiecare "
                        "distrugere de widget lasă urme de tip Exception in "
                        "Tkinter callback cu invalid command name ...!ctkcanvas")

    def test_update_dimensions_event_with_a_dead_canvas_does_nothing(self):
        import customtkinter as ctk
        handler = ctk.CTkBaseClass._update_dimensions_event

        class _DeadCanvas:
            def winfo_exists(self):
                return False

        class _FakeWidget:
            _canvas = _DeadCanvas()

        # Widgetul fals nu are nici winfo_width, nici _draw: dacă handlerul
        # ar ajunge la desenare, ar arunca. Cu gardul, întoarce nimic.
        self.assertIsNone(handler(_FakeWidget(), None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
