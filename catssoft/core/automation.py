"""core/automation — Clipboard și bara de sistem (tray)."""

import customtkinter as ctk
import threading
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from PIL import Image, ImageDraw

from catssoft.core.capabilities import TRAY_AVAILABLE
from catssoft.logging_setup import logger
from catssoft.ui.theme import ACCENT, BG_CARD, BG_INPUT, BG_SIDEBAR, TEXT_PRI, TEXT_SEC
from catssoft.utils.files import extract_file_name_from_url, extract_filename_from_url
# ── orig:2910 ──

# ─── Clipboard Monitor ────────────────────────────────────────────────────────
class ClipboardMonitor:
    def __init__(self, app, check_interval=2.0):
        self.app = app
        self.interval = check_interval
        self.last_clip = ""
        self.running = False
        self.notification_shown = False
        self._toast_window = None
        self._toast_close_id = None
        self._after_id = None

    def start(self):
        if self.running or not self.app.config.get("clipboard_monitor", True):
            return
        self.running = True
        # FIX: citirea clipboard-ului rulează acum pe thread-ul principal, nu pe un
        # thread de fundal separat - Tkinter/Tcl nu garantează thread-safety pentru
        # clipboard_get() apelat din afara thread-ului principal (blocaje intermitente
        # greu de reprodus, mai ales pe Windows).
        self._after_id = self.app.after(int(self.interval * 1000), self._check_clipboard)

    def stop(self):
        self.running = False
        if self._after_id:
            try: self.app.after_cancel(self._after_id)
            except Exception: pass
            self._after_id = None
        self._close_toast()

    def _close_toast(self):
        """Închide toast-ul anulând și timerul lui.

        Un `after` rămas activ pe un widget distrus provoacă TclError
        („invalid command name”) în callbackul Tkinter, iar `_reset_notification`
        nu se mai execută - monitorul de clipboard rămâne mut pentru tot restul
        sesiunii.
        """
        toast = self._toast_window
        if toast is not None and self._toast_close_id:
            try:
                toast.after_cancel(self._toast_close_id)
            except Exception:
                pass
        self._toast_close_id = None
        if toast is not None:
            try:
                if toast.winfo_exists():
                    toast.destroy()
            except Exception:
                pass
        self._toast_window = None
        self.notification_shown = False

    def _check_clipboard(self):
        if not self.running:
            return
        try:
            current = self.app.clipboard_get().strip()
            if current != self.last_clip and current.startswith(("http://", "https://")):
                self.last_clip = current
                # FIX: extragerea numelui de fișier face un HEAD request de rețea -
                # rulată direct aici ar îngheța tot UI-ul până răspunde serverul
                # (până la 10s). Se face pe un thread separat, toast-ul apare abia
                # după ce revine rezultatul.
                threading.Thread(target=self._resolve_and_notify, args=(current,), daemon=True).start()
        except Exception:
            pass
        if self.running:
            self._after_id = self.app.after(int(self.interval * 1000), self._check_clipboard)

    def _resolve_and_notify(self, url):
        filename = extract_file_name_from_url(url) or extract_filename_from_url(url)
        self.app.post_ui(lambda: self._show_notification(url, filename))

    def _show_notification(self, url, filename=None):
        if self.notification_shown: return
        self.notification_shown = True
        if self._toast_window and self._toast_window.winfo_exists():
            self._toast_window.destroy()

        try:
            toast = ctk.CTkToplevel(self.app)
        except Exception:
            # Fereastra nu a putut fi creată: nu lăsăm monitorul blocat.
            self.notification_shown = False
            raise
        self._toast_window = toast
        toast.title("Link Detectat")
        toast.geometry("400x140+100+100")
        toast.configure(fg_color=BG_CARD())
        toast.overrideredirect(True)
        toast.attributes('-topmost', True)

        ctk.CTkLabel(toast, text="📋 Link detectat în clipboard",
                    font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT_PRI()).pack(pady=8)

        url_display = url[:60] + "..." if len(url) > 60 else url
        ctk.CTkLabel(toast, text=url_display, font=ctk.CTkFont(size=10),
                    text_color=TEXT_SEC()).pack(pady=(0, 8))

        if filename:
            ctk.CTkLabel(toast, text=f"📄 {filename}", font=ctk.CTkFont(size=10),
                        text_color=ACCENT()).pack(pady=(0, 4))

        btn_frame = ctk.CTkFrame(toast, fg_color="transparent")
        btn_frame.pack(pady=5)

        def close_toast():
            """Închide toast-ul anulând și timerul lui.

            Fără anulare, callbackul Tkinter ar arde peste un widget distrus
            („invalid command name”), iar `_reset_notification()` nu s-ar mai
            executa - monitorul de clipboard ar rămâne mut permanent.
            """
            if self._toast_close_id:
                try:
                    toast.after_cancel(self._toast_close_id)
                except Exception:
                    pass
                self._toast_close_id = None
            try:
                if toast.winfo_exists():
                    toast.destroy()
            except Exception:
                pass
            self._reset_notification()

        def on_download():
            folder = self.app.config.get("default_folder", os.path.expanduser("~/Downloads"))
            self.app._add_download(url, folder, filename, segments=1, checksum="")
            close_toast()  # FIX: lipsea - toast rămânea "blocat" până la 10s

        ctk.CTkButton(btn_frame, text="Descarcă", width=100, fg_color=ACCENT(),
                     command=on_download).pack(side="left", padx=5)
        ctk.CTkButton(btn_frame, text="Ignoră", width=100, fg_color=BG_INPUT(),
                     command=close_toast).pack(side="left", padx=5)

        self._toast_close_id = toast.after(10000, close_toast)

    def _reset_notification(self):
        self.notification_shown = False
        self._toast_window = None
        self._toast_close_id = None
# ── orig:3055 ──

# ─── System Tray Manager ──────────────────────────────────────────────────────
class TrayManager:
    def __init__(self, app):
        self.app = app
        self.icon = None
        self.tray_thread = None

    def setup(self):
        if not TRAY_AVAILABLE or not self.app.config.get("minimize_to_tray", True):
            return False
        if self.icon is not None:
            return True
        try:
            image = Image.new('RGB', (64, 64), ACCENT())
            draw = ImageDraw.Draw(image)
            draw.ellipse([8, 8, 56, 56], fill=BG_SIDEBAR())
            draw.polygon([(32, 16), (48, 40), (16, 40)], fill=ACCENT())
            self.icon = pystray.Icon("CatsSoft", image, "CatsSoft Downloader",
                menu=pystray.Menu(
                    pystray.MenuItem("Deschide", lambda: self.app.post_ui(self.app.deiconify)),
                    pystray.MenuItem("Pauza Toate", lambda: self.app.post_ui(self.app._pause_all)),
                    pystray.MenuItem("Reluare Toate", lambda: self.app.post_ui(self.app._resume_all)),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Ieșire", lambda: self.app.post_ui(self.app._exit_app))))
            return True
        except Exception: return False

    def _run_tray(self):
        """Rulează iconul; excepțiile sunt înregistrate, nu ignorate."""
        try:
            self.icon.run()
        except Exception as exc:
            logger.warning("Tray icon could not start: %s", exc)

    def minimize_to_tray(self) -> bool:
        """Ascunde fereastra în bara de sistem.

        Întoarce False dacă iconul nu a putut porni - în acest caz fereastra
        rămâne vizibilă, pentru că altfel aplicația ar părea să fi dispărut
        (fereastră ascunsă, niciun icon, nicio cale de întoarcere).
        """
        if not self.icon:
            return False
        if self.tray_thread is None or not self.tray_thread.is_alive():
            self.tray_thread = threading.Thread(target=self._run_tray, daemon=True)
            self.tray_thread.start()
            # Dăm iconului o fracțiune de secundă să se înregistreze în zona de
            # notificări. Dacă firul moare imediat, e un eșec - și atunci nu
            # ascundem fereastra.
            time.sleep(0.25)
            if not self.tray_thread.is_alive():
                logger.warning("Tray icon failed to start; keeping the window visible")
                try:
                    self.app.deiconify()
                    self.app.lift()
                except Exception:
                    pass
                return False
        self.app.withdraw()
        return True

    def stop(self):
        if self.icon: self.icon.stop()
