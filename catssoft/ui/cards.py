"""ui/cards — Cardul de descărcare și gesturile de glisare."""

import customtkinter as ctk
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from PIL import Image, ImageDraw
import math

from catssoft.core.models import DLStatus, Download
from catssoft.core.patches import _prune_dead_commands
from catssoft.logging_setup import logger
from catssoft.ui.theme import ACCENT, BG_CARD, BG_CARD_HOV, BG_SIDEBAR, BORDER, ERROR, SUCCESS, TEXT_DIM, TEXT_PRI, TEXT_SEC, THEME, WARNING
from catssoft.ui.widgets import _Tooltip
from catssoft.utils.files import ICONS, guess_filetype
from catssoft.utils.formatting import fmt_duration, fmt_size, fmt_speed
# ── orig:5142 ──

# ─── UI Components ────────────────────────────────────────────────────────────
class SwipeGestureController:
    """Glisare orizontală cu feedback la tragere și revenire elastică."""
    def __init__(self, host, content, on_left=None, on_right=None,
                 on_left_long=None, on_right_long=None):
        self.host = host
        self.content = content
        self.on_left = on_left
        self.on_right = on_right
        self.on_left_long = on_left_long
        self.on_right_long = on_right_long
        self.start_x = self.start_y = 0
        self.offset = 0
        self.dragging = False
        self.ignore_gesture = False
        self._animation_id = None
        self._bind_tree(content)

    def _bind_tree(self, widget):
        for sequence, handler in (("<ButtonPress-1>", self._press),
                                  ("<B1-Motion>", self._move),
                                  ("<ButtonRelease-1>", self._release)):
            widget.bind(sequence, handler, add="+")
        for child in widget.winfo_children():
            self._bind_tree(child)

    # Widgete cu semantică proprie de clic. Un glisaj pornit de pe ele nu trebuie
    # să declanșeze simultan acțiunea butonului și acțiunea de swipe.
    _INTERACTIVE_TYPES = (ctk.CTkButton, ctk.CTkEntry, ctk.CTkOptionMenu,
                          ctk.CTkSwitch, ctk.CTkCheckBox, ctk.CTkRadioButton,
                          ctk.CTkTextbox, ctk.CTkSlider)

    def _is_interactive_target(self, widget):
        try:
            current = widget
            for _ in range(12):
                if current is None:
                    return False
                if isinstance(current, self._INTERACTIVE_TYPES):
                    return True
                current = getattr(current, "master", None)
        except Exception:
            return False
        return False

    def _press(self, event):
        if self._animation_id is not None:
            try: self.host.after_cancel(self._animation_id)
            except Exception: pass
            self._animation_id = None
        self.start_x, self.start_y = event.x_root, event.y_root
        self.dragging = False
        # Glisajul pornit de pe un buton este ignorat: altfel, la eliberare se
        # executau atât comanda butonului, cât și acțiunea de swipe.
        self.ignore_gesture = self._is_interactive_target(getattr(event, "widget", None))
        try:
            self.content._gesture_drag = False
        except Exception:
            pass
        return None

    def _move(self, event):
        if self.ignore_gesture:
            return None
        dx, dy = event.x_root - self.start_x, event.y_root - self.start_y
        if not self.dragging and abs(dy) > 10 and abs(dy) > abs(dx) * 1.15:
            self.ignore_gesture = True
            return None
        if abs(dx) > 8 and abs(dx) > abs(dy) * 1.15:
            self.dragging = True
            try:
                # Semnalează cardului că a început un drag, ca butoanele de
                # acțiune să nu se declanșeze la eliberarea deasupra lor.
                self.content._gesture_drag = True
            except Exception:
                pass
        if self.dragging:
            limit = max(130, int(self.host.winfo_width() * 0.42))
            self._set_offset(max(-limit, min(limit, dx)))
        return None

    def _set_offset(self, offset):
        self.offset = int(offset)
        self.host._layout_swipe_content()

    def _release(self, _event):
        # Resetăm indicatorul de drag: butoanele de acțiune vor redeveni normale.
        try:
            self.content._gesture_drag = False
        except Exception:
            pass
        if not self.dragging:
            return None
        width = max(1, self.host.winfo_width())
        long_threshold = max(170, int(width * 0.34))
        action = None
        if self.offset <= -long_threshold:
            action = self.on_left_long or self.on_left
        elif self.offset <= -76:
            action = self.on_left
        elif self.offset >= long_threshold:
            action = self.on_right_long or self.on_right
        elif self.offset >= 76:
            action = self.on_right
        self._animate_back(action)
        self.dragging = False
        return "break"

    def _animate_back(self, action):
        # Use the application's animation preference for all swipe micro-interactions.
        try:
            app = self.host.winfo_toplevel()
            animations_enabled = bool(app.config.get("animations", True))
        except Exception:
            animations_enabled = True
        if not animations_enabled:
            self._set_offset(0)
            if action:
                try: action()
                except Exception as exc: logger.warning("Swipe action failed: %s", exc)
            return

        start = float(self.offset)
        started = time.monotonic()
        duration = 0.24

        def frame():
            if not self.host.winfo_exists():
                self._animation_id = None
                return
            t = min(1.0, (time.monotonic() - started) / duration)
            if t >= 1:
                self._set_offset(0)
                self._animation_id = None
                if action:
                    try: action()
                    except Exception as exc: logger.warning("Swipe action failed: %s", exc)
                return
            # Cubic ease-out: quick response at first, then a controlled soft landing.
            eased = 1.0 - (1.0 - t) ** 3
            self._set_offset(round(start * (1.0 - eased)))
            self._animation_id = self.host.after(16, frame)

        frame()

    def cancel(self):
        if self._animation_id is not None:
            try: self.host.after_cancel(self._animation_id)
            except Exception: pass
            self._animation_id = None
# ── orig:5293 ──


class SwipeCardHost(ctk.CTkFrame):
    """Păstrează acțiunile sub card și le dezvăluie în timpul glisării."""
    def __init__(self, master, left_text, right_text, on_left, on_right,
                 on_left_long=None, on_right_long=None, **kwargs):
        super().__init__(master, fg_color="transparent", corner_radius=12, **kwargs)
        self._left_text = left_text
        self._right_text = right_text
        self.left_panel = ctk.CTkFrame(self, fg_color=ERROR(), corner_radius=10)
        self.left_panel.place(relx=0.5, rely=0, relwidth=0.5, relheight=1)
        self.right_panel = ctk.CTkFrame(self, fg_color=ACCENT(), corner_radius=10)
        self.right_panel.place(relx=0, rely=0, relwidth=0.5, relheight=1)
        self.left_label = ctk.CTkLabel(self.left_panel, text=left_text,
                                       text_color="#ffffff", font=ctk.CTkFont(size=12, weight="bold"))
        self.left_label.place(relx=0.72, rely=0.5, anchor="center")
        self.right_label = ctk.CTkLabel(self.right_panel, text=right_text,
                                        text_color="#ffffff", font=ctk.CTkFont(size=12, weight="bold"))
        self.right_label.place(relx=0.28, rely=0.5, anchor="center")
        self._controller = None
        self._content = None
        self._offset = 0
        self._actions = (on_left, on_right, on_left_long, on_right_long)
        THEME.add_listener(self)

    def attach(self, content):
        self._content = content
        # Keep the wrapper geometry controlled by pack. Forcing its requested
        # height from the child can stretch cards when CTk has not calculated
        # the child's final requested size yet.
        self.pack_propagate(True)
        self.bind("<Configure>", lambda _event: self._layout_swipe_content(), add="+")
        self.bind("<Map>", lambda _event: self.after_idle(self.sync_content_height), add="+")
        self._layout_swipe_content()
        content.lift()
        self._controller = SwipeGestureController(self, content, *self._actions)

    def _layout_swipe_content(self):
        if not self._content or not self._content.winfo_exists():
            return
        offset = self._controller.offset if self._controller else 0
        self._content.place(x=offset, y=0, relwidth=1, relheight=1)
        self._content.lift()

    def sync_content_height(self):
        if self._content and self._content.winfo_exists():
            # update_idletasks() golește coada de evenimente Tk a întregii
            # aplicații. Fiind apelat la fiecare refresh de card (de ~10 ori
            # pe secundă), limităm sincronizarea la ~2,5 Hz: înălțimea cardului
            # se schimbă foarte rar.
            now = time.monotonic()
            if now - getattr(self, "_last_sync_time", 0.0) < 0.4:
                return
            self._last_sync_time = now
            self._content.update_idletasks()
            desired = max(1, self._content.winfo_reqheight())
            if abs(self.winfo_height() - desired) > 2:
                self.configure(height=desired)

    def set_action_text(self, left=None, right=None):
        # Textul era configurat identic la fiecare refresh; evităm redesenarea.
        if left is not None and left != getattr(self, "_left_text", None):
            self._left_text = left
            self.left_label.configure(text=left)
        if right is not None and right != getattr(self, "_right_text", None):
            self._right_text = right
            self.right_label.configure(text=right)

    def _on_theme_change(self):
        if self.winfo_exists():
            self.left_panel.configure(fg_color=ERROR())
            self.right_panel.configure(fg_color=ACCENT())

    def destroy(self):
        THEME.remove_listener(self)
        if self._controller:
            self._controller.cancel()
        _prune_dead_commands(self)
        super().destroy()
# ── orig:5372 ──


class DownloadCard(ctk.CTkFrame):
    def __init__(self, master, dl: Download, on_remove: Callable, on_pause: Callable,
                 on_resume: Callable, on_stop: Callable, on_open_folder: Callable, **kw):
        super().__init__(master, fg_color=BG_CARD(), corner_radius=10,
                         border_width=1, border_color=BORDER(), **kw)
        THEME.add_listener(self)
        self.dl = dl
        self.selected = False
        self.on_remove = on_remove
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_stop = on_stop
        self.on_open_folder = on_open_folder
        self.app = getattr(dl, "app", None)
        self._display_progress = 0.0
        self._target_progress = 0.0
        self._progress_animation_id = None
        self._progress_animation_last = 0.0
        self._gesture_drag = False
        self._sparkline_signature = None
        self._sparkline_last_draw = 0.0
        self._file_exists_cache = (None, False)
        self._build()

    def _on_theme_change(self):
        if not self.winfo_exists(): return
        try:
            self.configure(fg_color=BG_CARD(), border_color=ACCENT() if self.selected else BORDER(),
                           border_width=2 if self.selected else 1)
            self.icon_lbl.configure(text_color=TEXT_SEC())
            self.name_lbl.configure(text_color=TEXT_PRI())
            self.url_lbl.configure(text_color=TEXT_DIM())
            self.info_lbl.configure(text_color=TEXT_SEC())
            self.progress_bar.configure(fg_color=BG_SIDEBAR(), progress_color=ACCENT())
            for btn in self.btn_frame.winfo_children():
                btn.configure(hover_color=BG_CARD_HOV())
        except Exception: pass

    @staticmethod
    def _display_name(filename: str, max_length: int = 52) -> str:
        """Trunchiază numele păstrând extensia, ca butoanele să nu fie împinse."""
        if not filename or len(filename) <= max_length:
            return filename
        stem, ext = os.path.splitext(filename)
        if len(ext) > 12:
            return filename[:max_length - 1] + "…"
        keep = max(8, max_length - len(ext) - 1)
        return stem[:keep] + "…" + ext

    def _guarded(self, cmd):
        """Execută acțiunea doar dacă nu s-a încheiat un drag de swipe."""
        if getattr(self, "_gesture_drag", False):
            return
        try:
            cmd()
        except Exception as exc:
            logger.warning("Acțiune pe card eșuată: %s", exc)

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.icon_lbl = ctk.CTkLabel(self, text=ICONS.get(guess_filetype(self.dl.url), "↓"),
                                     font=ctk.CTkFont(size=22), text_color=TEXT_SEC(), width=44)
        self.icon_lbl.grid(row=0, column=0, rowspan=3, padx=(14, 4), pady=14, sticky="ns")
        self.name_lbl = ctk.CTkLabel(self, text=self._display_name(self.dl.filename),
                                     font=ctk.CTkFont(size=14, weight="bold"),
                                     text_color=TEXT_PRI(), anchor="w")
        self.name_lbl.grid(row=0, column=1, sticky="w", padx=(0, 6), pady=(14, 1))
        if len(self.dl.filename) > 52:
            # Nume lung: cardul ar împinge butoanele în afara ecranului.
            _Tooltip(self.name_lbl, self.dl.filename)
        short = self.dl.url if len(self.dl.url) <= 74 else self.dl.url[:71] + "…"
        self.url_lbl = ctk.CTkLabel(self, text=short, font=ctk.CTkFont(size=11),
                                    text_color=TEXT_DIM(), anchor="w")
        self.url_lbl.grid(row=1, column=1, sticky="w", padx=(0, 6))
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.grid(row=0, column=2, rowspan=2, padx=(0, 10), pady=10, sticky="ne")

        def sb(text, cmd, hover=BG_CARD_HOV(), tip=""):
            button = ctk.CTkButton(self.btn_frame, text=text, width=30, height=28,
                                   fg_color="transparent", hover_color=hover,
                                   text_color=TEXT_SEC(), font=ctk.CTkFont(size=13),
                                   command=lambda: self._guarded(cmd))
            if tip:
                # Referință explicită: obiectul tooltip rămâne viu cât timp butonul există.
                button._tooltip = _Tooltip(button, tip)
            return button

        self.pause_btn = sb("⏸", self._toggle_pause, tip="Pauză / Continuă")
        self.pause_btn.pack(side="left", padx=2)
        self.stop_btn = sb("⏹", lambda: self.on_stop(self.dl), hover="#3a1a1a", tip="Oprește transferul")
        self.stop_btn.pack(side="left", padx=2)
        self.folder_btn = sb("📂", lambda: self.on_open_folder(self.dl), tip="Deschide folderul")
        self.folder_btn.pack(side="left", padx=2)
        self.del_btn = sb("✕", lambda: self.on_remove(self.dl), hover="#3a1a1a", tip="Elimină din listă")
        self.del_btn.pack(side="left", padx=2)
        self.retry_btn = sb("↻", self._retry_download, tip="Reîncearcă descărcarea")
        self.retry_btn.pack(side="left", padx=2)
        self.details_btn = sb("↗", self._open_details, tip="Detalii")
        self.details_btn.pack(side="left", padx=2)

        self.progress_bar = ctk.CTkProgressBar(self, height=4, corner_radius=2,
                                               fg_color=BG_SIDEBAR(), progress_color=ACCENT())
        self.progress_bar.set(0)
        self.progress_bar.grid(row=2, column=1, sticky="ew", padx=(0, 160), pady=(6, 14))
        self.info_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12),
                                     text_color=TEXT_SEC(), anchor="e", width=150)
        self.info_lbl.grid(row=2, column=1, columnspan=2, sticky="e", padx=(0, 10), pady=(6, 14))

        # FEATURE NOU: sparkline live al vitezei, pe fereastra de 5s din SpeedTracker
        # (aceeași clasă folosită acum și pentru calculul vitezei afișate - vezi fix la
        # _download_segment/download_simple). Vizibil doar cât timp descarcă activ.
        self._sparkline_img = None
        self.sparkline_lbl = ctk.CTkLabel(self, text="", width=130, height=24)
        self.sparkline_lbl.grid(row=3, column=1, sticky="w", padx=(0, 6), pady=(0, 10))
        self.sparkline_lbl.grid_remove()
        # Timp rămas estimat: informația cea mai căutată, plasă lângă sparkline
        # ca să nu înghesue bara de progres.
        self.eta_lbl = ctk.CTkLabel(self, text="", width=110, anchor="e",
                                    font=ctk.CTkFont(size=11), text_color=TEXT_DIM())
        self.eta_lbl.grid(row=3, column=2, sticky="e", padx=(0, 10), pady=(0, 10))
        self.eta_lbl.grid_remove()

        self.refresh_ui()

    def _open_details(self):
        if self.app:
            self.app._open_download_details(self.dl)

    def _retry_download(self):
        if self.app:
            self.app._retry_download(self.dl)

    def set_selected(self, selected: bool):
        self.selected = selected
        if self.winfo_exists():
            self.configure(border_color=ACCENT() if selected else BORDER(),
                           border_width=2 if selected else 1)

    def _draw_sparkline(self, force: bool = False):
        """Desenează mini-graficul de viteză pe baza eșantioanelor din SpeedTracker.

        Refacerea unei imagini PIL + CTkImage la fiecare eveniment de progres
        genera zeci de obiecte Tk pe secundă pentru fiecare card activ; acum
        redesenăm doar când setul de eșantioane chiar s-a schimbat.
        """
        try:
            with self.dl.speed_tracker.lock:
                samples = list(self.dl.speed_tracker.samples)
        except Exception:
            samples = []

        signature = (len(samples), samples[-1][0] if samples else 0.0)
        now = time.monotonic()
        if (not force and signature == self._sparkline_signature
                and now - self._sparkline_last_draw < 0.5):
            return
        self._sparkline_signature = signature
        self._sparkline_last_draw = now

        w, h = 130, 24
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

        speeds = []
        for i in range(1, len(samples)):
            t0, b0 = samples[i - 1]
            t1, b1 = samples[i]
            dt = t1 - t0
            if dt > 0:
                speeds.append(max(0.0, (b1 - b0) / dt))

        if len(speeds) >= 2:
            draw = ImageDraw.Draw(img)
            max_speed = max(speeds) or 1.0
            step = w / (len(speeds) - 1)
            points = [(i * step, h - 2 - (s / max_speed) * (h - 4)) for i, s in enumerate(speeds)]
            draw.line(points, fill=ACCENT(), width=2, joint="curve")

        # FIX: păstrăm o referință la imagine (self._sparkline_img) - altfel CTkImage
        # poate fi eliminată de garbage collector și eticheta rămâne goală.
        self._sparkline_img = ctk.CTkImage(light_image=img, dark_image=img, size=(w, h))
        self.sparkline_lbl.configure(image=self._sparkline_img, text="")

    def _toggle_pause(self):
        if self.dl.status == DLStatus.DOWNLOADING: self.on_pause(self.dl)
        elif self.dl.status == DLStatus.PAUSED: self.on_resume(self.dl)

    def _set_progress_smooth(self, target: float):
        """Animare ușoară a barei de progres, cu anulare sigură la distrugerea cardului."""
        target = max(0.0, min(1.0, float(target)))
        self._target_progress = target
        try:
            animations_enabled = bool(self.app and self.app.config.get("animations", True))
        except Exception:
            animations_enabled = True
        if not animations_enabled:
            if self._progress_animation_id is not None:
                try: self.after_cancel(self._progress_animation_id)
                except Exception: pass
                self._progress_animation_id = None
            self._display_progress = target
            self.progress_bar.set(target)
            return
        if self._progress_animation_id is None:
            self._progress_animation_last = time.monotonic()
            self._animate_progress_step()

    def _animate_progress_step(self):
        self._progress_animation_id = None
        if not self.winfo_exists():
            return
        now = time.monotonic()
        dt = max(0.008, min(0.08, now - (self._progress_animation_last or now)))
        self._progress_animation_last = now
        delta = self._target_progress - self._display_progress
        if abs(delta) <= 0.002:
            self._display_progress = self._target_progress
            self.progress_bar.set(self._display_progress)
            return
        # Exponential interpolation stays smooth even when progress updates arrive unevenly.
        factor = 1.0 - math.exp(-dt / 0.10)
        self._display_progress += delta * factor
        self._display_progress = max(0.0, min(1.0, self._display_progress))
        self.progress_bar.set(self._display_progress)
        self._progress_animation_id = self.after(16, self._animate_progress_step)

    def refresh_ui(self):
        """FIX: Progress nu depășește 100%; actualizarea barei este animată opțional."""
        dl = self.dl

        with dl.lock:
            # FIX: Cap progress la 100%
            progress = min(100.0, dl.progress)
            val = max(0.0, min(1.0, progress / 100))
            speed = dl.speed
            downloaded = dl.downloaded
            total = dl.total
            if speed > dl.peak_speed:
                dl.peak_speed = speed
            now = time.time()
            if speed > 0 and (not dl.speed_history or now - dl.speed_history[-1][0] >= 1.0):
                dl.speed_history.append((now, speed))
                dl.speed_history = dl.speed_history[-600:]

        self._set_progress_smooth(val)

        active_set = {DLStatus.DOWNLOADING, DLStatus.PAUSED}

        if dl.status == DLStatus.DOWNLOADING:
            self.pause_btn.configure(text="⏸", state="normal")
        elif dl.status == DLStatus.PAUSED:
            self.pause_btn.configure(text="▶", state="normal")
        else:
            self.pause_btn.configure(text="⏸", state="disabled")

        stoppable = dl.status in active_set or dl.status == DLStatus.QUEUED
        self.stop_btn.configure(state="normal" if stoppable else "disabled")
        self.retry_btn.configure(state="normal" if dl.status in (DLStatus.ERROR, DLStatus.CANCELLED) else "disabled")
        # Un stat() pe disc pentru fiecare card, la fiecare refresh (10/s), este
        # inutil; memorăm rezultatul pentru scurt timp.
        cached_at, cached_exists = self._file_exists_cache
        if cached_at is None or (time.monotonic() - cached_at) > 2.0:
            try:
                cached_exists = os.path.exists(dl.save_path)
            except OSError:
                cached_exists = False
            self._file_exists_cache = (time.monotonic(), cached_exists)
        self.folder_btn.configure(state="normal" if cached_exists else "disabled")

        if dl.status == DLStatus.COMPLETED:
            color = SUCCESS()
            info = f"Completat  •  {fmt_size(total or downloaded)}"
        elif dl.status == DLStatus.ERROR:
            color = ERROR()
            info = f"Eroare: {dl.error_msg[:38]}"
        elif dl.status == DLStatus.PAUSED:
            color = WARNING()
            info = f"Pauza  •  {progress:.1f}%"
        elif dl.status == DLStatus.CANCELLED:
            color = TEXT_DIM()
            info = "Anulat"
        elif dl.status == DLStatus.QUEUED:
            color = ACCENT()
            retry_due = float(getattr(dl, "auto_retry_at", 0.0) or 0.0)
            if retry_due > time.time():
                info = f"Reîncercare automată în {max(1, int(math.ceil(retry_due - time.time())))} s"
            else:
                info = "În așteptare"
        else:
            color = ACCENT()
            if total > 0:
                sz = f"{fmt_size(downloaded)} / {fmt_size(total)}"
            else:
                sz = f"{fmt_size(downloaded)}"
            spd = f"  |  {fmt_speed(speed)}" if speed > 0 else ""
            info = f"{progress:.1f}%  •  {sz}{spd}"

        self.progress_bar.configure(progress_color=color)
        self.info_lbl.configure(text=info)

        # Timp rămas estimat pe card.
        eta_text = ""
        if dl.status == DLStatus.DOWNLOADING and speed > 0 and total > downloaded > 0:
            eta_text = f"⏱ {fmt_duration((total - downloaded) / speed)}"
        elif dl.status == DLStatus.PAUSED and total > downloaded > 0:
            eta_text = "⏸ în pauză"
        if eta_text:
            self.eta_lbl.configure(text=eta_text)
            self.eta_lbl.grid()
        else:
            self.eta_lbl.grid_remove()

        # FEATURE NOU: sparkline vizibil doar cât timp descarcă activ
        if dl.status == DLStatus.DOWNLOADING:
            self._draw_sparkline()
            self.sparkline_lbl.grid()
        else:
            self.sparkline_lbl.grid_remove()
        if self.app and hasattr(self.app, "_update_swipe_card"):
            self.app._update_swipe_card(dl)

    def destroy(self):
        THEME.remove_listener(self)
        if self._progress_animation_id is not None:
            try: self.after_cancel(self._progress_animation_id)
            except Exception: pass
            self._progress_animation_id = None
        _prune_dead_commands(self)
        super().destroy()
