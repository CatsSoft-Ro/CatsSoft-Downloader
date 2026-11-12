"""ui/details — Panoul de detalii al descărcării."""

import customtkinter as ctk
import tkinter as tk
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from datetime import datetime, timedelta

from catssoft.core.models import DLStatus, Download
from catssoft.core.patches import _prune_dead_commands
from catssoft.logging_setup import logger
from catssoft.ui.theme import ACCENT, BG_CARD, BG_CARD_HOV, BG_INPUT, BG_MAIN, BG_SIDEBAR, BORDER, ERROR, TEXT_DIM, TEXT_PRI, TEXT_SEC, THEME, shade_color
from catssoft.utils.files import open_folder
from catssoft.utils.formatting import fmt_duration, fmt_size, fmt_speed
# ── orig:5702 ──


class DownloadDetailsView(ctk.CTkFrame):
    """Ecran de detalii integrat în tabul Downloads, nu într-o fereastră separată."""
    def __init__(self, parent, app, dl: Download):
        super().__init__(parent, fg_color=BG_MAIN(), corner_radius=0)
        self.app = app
        self.dl = dl
        self._flow_phase = 0.0
        self._flow_job = None
        self._refresh_job = None
        self._last_live_refresh = 0.0
        THEME.add_listener(self)
        self._build()
        self._refresh(schedule_next=False)
        self._refresh_job = self.after(250, self._refresh_tick)

    def _build(self):
        self.top = ctk.CTkFrame(self, fg_color=BG_SIDEBAR(), corner_radius=0)
        self.top.pack(fill="x")
        self.back_btn = ctk.CTkButton(self.top, text="‹  Descărcări", width=120,
                                      fg_color="transparent", hover_color=BG_CARD_HOV(),
                                      text_color=TEXT_SEC(), command=self._close)
        self.back_btn.pack(side="left", padx=12, pady=10)
        self.actions = ctk.CTkFrame(self.top, fg_color="transparent")
        self.actions.pack(side="right", padx=12, pady=8)
        self.pause_btn = ctk.CTkButton(self.actions, text="Pauză", width=82,
                                       fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                                       text_color=TEXT_PRI(), command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        self.retry_btn = ctk.CTkButton(self.actions, text="Reîncearcă", width=104,
                                       fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                                       text_color=TEXT_PRI(), command=self._retry)
        self.retry_btn.pack(side="left", padx=4)
        self.cancel_btn = ctk.CTkButton(self.actions, text="Anulează", width=95,
                                        fg_color=shade_color(ERROR(), .82), hover_color=shade_color(ERROR(), .72),
                                        text_color="#ffaaa5", command=self._cancel)
        self.cancel_btn.pack(side="left", padx=4)
        self.show_btn = ctk.CTkButton(self.actions, text="Deschide folderul", width=140,
                                      fg_color=ACCENT(), hover_color=shade_color(ACCENT(), .84),
                                      text_color="#111111", command=self._show_folder)
        self.show_btn.pack(side="left", padx=4)
        self.dashboard = ctk.CTkFrame(self, fg_color="transparent")
        self.dashboard.pack(fill="both", expand=True)

        # Plexo-inspired throughput area: connection summary left, live graph right.
        self.throughput = ctk.CTkFrame(self.dashboard, fg_color="transparent", corner_radius=0,
                                       border_width=0)
        self.throughput.pack(fill="x", padx=22, pady=(10, 8))
        self.throughput.grid_columnconfigure(0, weight=4, uniform="top")
        self.throughput.grid_columnconfigure(1, weight=6, uniform="top")
        self.flow_canvas = tk.Canvas(self.throughput, height=150, bg=BG_MAIN(), highlightthickness=0)
        self.flow_canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        graph_box = ctk.CTkFrame(self.throughput, fg_color="transparent")
        graph_box.grid(row=0, column=1, sticky="nsew")
        self.graph_title = ctk.CTkLabel(graph_box, text="VITEZĂ · ULTIMELE 60 SECUNDE",
                                        font=ctk.CTkFont(size=10, weight="bold"),
                                        text_color=TEXT_DIM(), anchor="w")
        self.graph_title.pack(fill="x", pady=(0, 3))
        self.graph = tk.Canvas(graph_box, height=128, bg=BG_MAIN(), highlightthickness=0)
        self.graph.pack(fill="both", expand=True)
        self.graph.bind("<Configure>", lambda _e: self._draw_graph())
        self.flow_canvas.bind("<Configure>", lambda _e: self._draw_flow())

        divider = ctk.CTkFrame(self.dashboard, height=1, fg_color=BORDER(), corner_radius=0)
        divider.pack(fill="x")
        self.file_row = ctk.CTkFrame(self.dashboard, fg_color="transparent")
        self.file_row.pack(fill="x", padx=22, pady=(12, 8))
        self.file_badge = ctk.CTkLabel(self.file_row, text="FILE", width=48, height=48,
                                       corner_radius=10, fg_color=BG_CARD(), text_color=TEXT_SEC(),
                                       font=ctk.CTkFont(size=11, weight="bold"))
        self.file_badge.pack(side="left", padx=(0, 14))
        file_info = ctk.CTkFrame(self.file_row, fg_color="transparent")
        file_info.pack(side="left", fill="x", expand=True)
        self.name_label = ctk.CTkLabel(file_info, text=self.dl.filename,
                                       font=ctk.CTkFont(size=16, weight="bold"),
                                       text_color=TEXT_PRI(), anchor="w")
        self.name_label.pack(anchor="w")
        self.file_progress_label = ctk.CTkLabel(file_info, text="",
                                                font=ctk.CTkFont(size=12),
                                                text_color=TEXT_SEC(), anchor="w")
        self.file_progress_label.pack(anchor="w", pady=(3, 0))
        self.path_label = ctk.CTkLabel(self.file_row, text="", font=ctk.CTkFont(size=11),
                                       text_color=TEXT_SEC(), anchor="e")
        self.path_label.pack(side="right", padx=(8, 0))

        telemetry = ctk.CTkFrame(self.dashboard, fg_color="transparent")
        telemetry.pack(fill="x", padx=22, pady=(0, 10))
        telemetry.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="telemetry")
        self.telemetry_values = {}
        for index, (key, title, initial) in enumerate((
                ("elapsed", "TIMP SCURS", "0s"), ("eta", "TIMP RĂMAS", "—"),
                ("average", "VITEZĂ MEDIE", "—"), ("retries", "REÎNCERCĂRI · RANGE", "0 · —"))):
            tile = ctk.CTkFrame(telemetry, fg_color=BG_CARD(), corner_radius=8,
                                border_width=1, border_color=BORDER())
            tile.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0 if index == 3 else 5))
            ctk.CTkLabel(tile, text=title, text_color=TEXT_DIM(),
                         font=ctk.CTkFont(size=9, weight="bold")).pack(anchor="w", padx=10, pady=(7, 1))
            value = ctk.CTkLabel(tile, text=initial, text_color=TEXT_PRI(),
                                 font=ctk.CTkFont(size=12, weight="bold"), anchor="w")
            value.pack(anchor="w", padx=10, pady=(0, 7))
            self.telemetry_values[key] = value

        self.chunks_card = ctk.CTkFrame(self.dashboard, fg_color=BG_CARD(), corner_radius=9,
                                        border_width=1, border_color=BORDER())
        self.chunks_card.pack(fill="x", padx=22, pady=(0, 12))
        chunks_head = ctk.CTkFrame(self.chunks_card, fg_color="transparent")
        chunks_head.pack(fill="x", padx=12, pady=(7, 1))
        self.chunks_title = ctk.CTkLabel(chunks_head, text="SEGMENTE",
                                         font=ctk.CTkFont(size=9, weight="bold"),
                                         text_color=TEXT_DIM(), anchor="w")
        self.chunks_title.pack(side="left")
        self.chunks_meta = ctk.CTkLabel(chunks_head, text="",
                                        font=ctk.CTkFont(size=10), text_color=TEXT_SEC())
        self.chunks_meta.pack(side="right")
        self.chunks_canvas = tk.Canvas(self.chunks_card, height=40, bg=BG_CARD(), highlightthickness=0)
        self.chunks_canvas.pack(fill="x", padx=12, pady=(1, 7))
        self.chunks_canvas.bind("<Configure>", lambda _e: self._draw_chunks())

        self.table = ctk.CTkFrame(self.dashboard, fg_color="transparent")
        self.table.pack(fill="x", padx=22)
        self.table.grid_columnconfigure(0, weight=4)
        self.table.grid_columnconfigure(1, weight=2)
        self.table.grid_columnconfigure(2, weight=1)
        self.table.grid_columnconfigure(3, weight=1)
        self.table.grid_columnconfigure(4, weight=1)
        for col, title in enumerate(("CONEXIUNE", "PROGRES", "PARTE", "VITEZĂ", "DESCĂRCAT")):
            ctk.CTkLabel(self.table, text=title, font=ctk.CTkFont(size=9, weight="bold"),
                         text_color=TEXT_DIM(), anchor="w" if col == 0 else "e").grid(
                         row=0, column=col, sticky="ew", padx=(4, 8), pady=(0, 5))
        self.system_label = ctk.CTkLabel(self.table, text="●  Conexiune de sistem",
                                         font=ctk.CTkFont(size=11, weight="bold"),
                                         text_color=TEXT_PRI(), anchor="w")
        self.system_label.grid(row=1, column=0, sticky="ew", padx=4, pady=9)
        self.network_progress = ctk.CTkProgressBar(self.table, height=8, corner_radius=4,
                                                   fg_color=BG_CARD(), progress_color=ACCENT())
        self.network_progress.grid(row=1, column=1, sticky="ew", padx=8)
        self.share_label = ctk.CTkLabel(self.table, text="0%", font=ctk.CTkFont(size=11, weight="bold"),
                                        text_color=TEXT_PRI(), anchor="e")
        self.share_label.grid(row=1, column=2, sticky="ew", padx=8)
        self.network_speed_label = ctk.CTkLabel(self.table, text="—", font=ctk.CTkFont(size=11, weight="bold"),
                                                text_color=ACCENT(), anchor="e")
        self.network_speed_label.grid(row=1, column=3, sticky="ew", padx=8)
        self.network_downloaded_label = ctk.CTkLabel(self.table, text="0 B", font=ctk.CTkFont(size=11),
                                                     text_color=TEXT_SEC(), anchor="e")
        self.network_downloaded_label.grid(row=1, column=4, sticky="ew", padx=(8, 2))
        ctk.CTkFrame(self.dashboard, height=1, fg_color=BORDER(), corner_radius=0).pack(fill="x", side="bottom")
        self.url_label = ctk.CTkLabel(self.dashboard, text=self.dl.url, font=ctk.CTkFont(size=10),
                                      text_color=TEXT_DIM(), anchor="w", wraplength=940)
        self.url_label.pack(fill="x", side="bottom", padx=22, pady=(8, 10))

    def _on_theme_change(self):
        if not self.winfo_exists():
            return
        try:
            self.configure(fg_color=BG_MAIN())
            self.graph.configure(bg=BG_MAIN())
            self.chunks_card.configure(fg_color=BG_CARD(), border_color=BORDER())
            self.chunks_canvas.configure(bg=BG_CARD())
            self.flow_canvas.configure(bg=BG_MAIN())
            self.graph_title.configure(text_color=TEXT_DIM())
            for value in getattr(self, "telemetry_values", {}).values():
                value.configure(text_color=TEXT_PRI())
            self._draw_graph()
            self._draw_flow()
        except Exception:
            pass

    def _refresh(self, schedule_next=True):
        if not self.winfo_exists():
            return
        dl = self.dl
        with dl.lock:
            status, total, downloaded, speed = dl.status, dl.total, dl.downloaded, dl.speed
            peak, history = dl.peak_speed, list(dl.speed_history)
            chunk_progress = list(dl.chunk_progress)
            chunk_size = dl.chunk_size_bytes
            retries = dl.retry_count
        elapsed = 0
        if dl.started_at:
            end_time = dl.completed_date or datetime.now()
            elapsed = max(0, int((end_time - dl.started_at).total_seconds()))
        elif dl.completed_date:
            elapsed = max(0, int((dl.completed_date - dl.added_date).total_seconds()))
        percent = min(1.0, downloaded / total) if total else 0
        if status == DLStatus.COMPLETED:
            eta = "Completat"
        elif status == DLStatus.DOWNLOADING and speed > 0 and total > downloaded:
            eta = f"{int((total - downloaded) / speed)} s rămase"
        elif status == DLStatus.PAUSED:
            eta = "Reluare în așteptare" if dl.resume_requested else "În pauză"
        elif status == DLStatus.QUEUED:
            eta = "În coadă"
        elif status == DLStatus.ERROR:
            eta = f"Eroare · {dl.error_msg[:60]}"
        elif status == DLStatus.CANCELLED:
            eta = "Anulat"
        else:
            eta = "Se conectează…"
        avg_speed = downloaded / elapsed if elapsed else 0
        eta_seconds = ((total - downloaded) / speed) if status == DLStatus.DOWNLOADING and speed > 0 and total > downloaded else None
        self.telemetry_values["elapsed"].configure(text=fmt_duration(elapsed))
        self.telemetry_values["eta"].configure(text=("Completat" if status == DLStatus.COMPLETED else
            ("În pauză" if status == DLStatus.PAUSED else fmt_duration(eta_seconds))))
        self.telemetry_values["average"].configure(text=fmt_speed(avg_speed) if avg_speed > 0 else "—")
        range_text = "ON" if dl.supports_resume else "OFF / secvențial"
        self.telemetry_values["retries"].configure(text=f"{retries} · {range_text}")
        self.name_label.configure(text=dl.filename)
        self.path_label.configure(text=f"📁  {os.path.basename(os.path.dirname(dl.save_path)) or os.path.dirname(dl.save_path)}")
        size_text = f"{fmt_size(downloaded)} din {fmt_size(total)}" if total else fmt_size(downloaded)
        self.file_progress_label.configure(text=f"{size_text}   ·   {percent * 100:.0f}%   ·   {eta}")
        extension = os.path.splitext(dl.filename)[1].replace(".", "").upper()[:5] or "FILE"
        self.file_badge.configure(text=extension)
        if chunk_progress:
            self.chunks_title.configure(text=f"{len(chunk_progress)} segmente")
            chunk_desc = f"{fmt_size(chunk_size)} fiecare" if chunk_size else "transfer secvențial"
            retry_desc = f"   ·   {retries} retry-uri" if retries else ""
            self.chunks_meta.configure(text=f"{chunk_desc}{retry_desc}")
        else:
            self.chunks_title.configure(text="SEGMENTE DE TRANSFER")
            self.chunks_meta.configure(text="Așteptare răspuns server" if not total else "Pregătire segmente")
        self.network_progress.set(percent)
        self.share_label.configure(text=f"{percent * 100:.0f}%")
        self.network_speed_label.configure(text=fmt_speed(speed) if speed else "—")
        self.network_downloaded_label.configure(text=fmt_size(downloaded))
        self.graph_title.configure(text=f"VITEZĂ · ULTIMELE 60 SECUNDE     AVG {fmt_speed(avg_speed)}   ·   VÂRF {fmt_speed(peak)}")
        self.pause_btn.configure(text="Reia" if status == DLStatus.PAUSED else "Pauză",
                                  state="normal" if status in (DLStatus.DOWNLOADING, DLStatus.PAUSED) else "disabled")
        self.retry_btn.configure(state="normal" if status in (DLStatus.ERROR, DLStatus.CANCELLED) else "disabled")
        self.cancel_btn.configure(text="Elimină din listă" if status in
                                  (DLStatus.COMPLETED, DLStatus.ERROR, DLStatus.CANCELLED) else "Anulează",
                                  state="normal")
        self.show_btn.configure(state="normal" if os.path.exists(dl.save_path) else "disabled")
        self._draw_graph(history)
        self._draw_flow(speed)
        self._draw_chunks(chunk_progress)
        try:
            animations_enabled = bool(self.app.config.get("animations", True))
        except Exception:
            animations_enabled = True
        if status == DLStatus.DOWNLOADING and animations_enabled and self._flow_job is None:
            self._schedule_flow_animation()
        elif not animations_enabled and self._flow_job is not None:
            try: self.after_cancel(self._flow_job)
            except Exception: pass
            self._flow_job = None
        if schedule_next:
            self._refresh_job = self.after(250, self._refresh_tick)

    def _refresh_tick(self):
        """Polling independent de coada UI; erorile temporare nu opresc refresh-ul."""
        self._refresh_job = None
        if not self.winfo_exists():
            return
        # Dacă descărcarea a fost eliminată din aplicație, oprim polling-ul:
        # altfel interogăm un model inexistent la fiecare 250 ms, pentru totdeauna.
        try:
            downloads = getattr(self.app, "downloads", None)
            if downloads is not None and self.dl not in downloads:
                self._removed = True
                return
        except Exception:
            pass
        try:
            self._refresh(schedule_next=False)
        except Exception:
            logger.exception("Refresh-ul paginii de detalii a eșuat")
        if self.winfo_exists() and not getattr(self, "_removed", False):
            self._refresh_job = self.after(250, self._refresh_tick)

    def refresh_from_model(self, dl):
        """Actualizează imediat detaliile când motorul publică progres nou."""
        if dl is not self.dl or not self.winfo_exists():
            return
        now = time.monotonic()
        if now - self._last_live_refresh < 0.25:
            return
        self._last_live_refresh = now
        self._refresh(schedule_next=False)

    def _schedule_flow_animation(self):
        try:
            animations_enabled = bool(self.app.config.get("animations", True))
        except Exception:
            animations_enabled = True
        if (not self.winfo_exists() or self.dl.status != DLStatus.DOWNLOADING
                or not animations_enabled):
            self._flow_job = None
            return
        self._flow_phase = (self._flow_phase + 0.045) % 1.0
        self._draw_flow()
        self._flow_job = self.after(55, self._schedule_flow_animation)

    def _draw_flow(self, speed=None):
        try:
            if speed is None:
                with self.dl.lock:
                    speed = self.dl.speed
            canvas = self.flow_canvas
            canvas.delete("all")
            w, h = max(100, canvas.winfo_width()), max(80, canvas.winfo_height())
            mid = h * .52
            muted, accent = TEXT_DIM(), ACCENT()
            canvas.create_text(6, 15, text="TRANSFER", fill=TEXT_DIM(), anchor="w",
                               font=("Consolas", 9, "bold"))
            canvas.create_text(w * .12, mid - 24, text="CONEXIUNE", fill=muted, anchor="center",
                               font=("Consolas", 8))
            canvas.create_oval(w * .12 - 4, mid - 3, w * .12 + 4, mid + 5, fill=accent, outline="")
            canvas.create_line(w * .12 + 8, mid + 1, w * .48, mid + 1, fill=accent, width=2,
                               arrow="last", dash=(5, 4))
            if self.dl.status == DLStatus.DOWNLOADING:
                dot_x = w * .18 + (w * .25 * self._flow_phase)
                canvas.create_oval(dot_x - 3, mid - 2, dot_x + 3, mid + 4,
                                   fill=TEXT_PRI(), outline=accent, width=1)
            canvas.create_oval(w * .50 - 8, mid - 7, w * .50 + 8, mid + 9,
                               outline=accent, width=2)
            canvas.create_text(w * .50, mid + 20, text="SERVER", fill=muted, anchor="center",
                               font=("Consolas", 8))
            canvas.create_text(w * .77, mid - 27, text="VITEZĂ TOTALĂ", fill=muted,
                               anchor="center", font=("Consolas", 8, "bold"))
            canvas.create_text(w * .77, mid, text=fmt_speed(speed), fill=TEXT_PRI(),
                               anchor="center", font=("Consolas", 21, "bold"))
            with self.dl.lock:
                elapsed = max(1.0, (datetime.now() - self.dl.started_at).total_seconds()) if self.dl.started_at else 1.0
                avg = self.dl.downloaded / elapsed
                peak = self.dl.peak_speed
            canvas.create_text(w * .77, mid + 24,
                               text=f"AVG {fmt_speed(avg)}   VÂRF {fmt_speed(peak)}", fill=muted,
                               anchor="center", font=("Consolas", 8))
        except Exception:
            pass

    def _draw_chunks(self, progress=None):
        try:
            if progress is None:
                with self.dl.lock:
                    progress = list(self.dl.chunk_progress)
            canvas = self.chunks_canvas
            canvas.delete("all")
            width = max(100, canvas.winfo_width())
            if not progress:
                canvas.create_text(4, 18, text="Progresul pe segmente va apărea după detectarea suportului Range.",
                                   fill=TEXT_DIM(), anchor="w", font=("Segoe UI", 9))
                return
            count = len(progress)
            visual_count = min(count, 120)
            columns = min(60, visual_count)
            rows = (visual_count + columns - 1) // columns
            gap = 4
            cell_w = max(4, (width - (columns - 1) * gap) / columns)
            cell_h = 13 if rows > 1 else 18
            for visual_i in range(visual_count):
                first = visual_i * count // visual_count
                last = max(first + 1, (visual_i + 1) * count // visual_count)
                fraction = sum(progress[first:last]) / (last - first)
                row, col = divmod(visual_i, columns)
                x1, y1 = col * (cell_w + gap), row * (cell_h + gap)
                x2, y2 = x1 + cell_w, y1 + cell_h
                canvas.create_rectangle(x1, y1, x2, y2, fill=BG_INPUT(), outline=BORDER(), width=1)
                if fraction > 0:
                    canvas.create_rectangle(x1 + 1, y1 + 1,
                                            x1 + 1 + max(1, (x2 - x1 - 2) * fraction), y2 - 1,
                                            fill=ACCENT(), outline="")
        except Exception:
            pass

    def _draw_graph(self, history=None):
        try:
            self.graph.delete("all")
            if history is None:
                with self.dl.lock:
                    history = list(self.dl.speed_history)
            width, height = max(100, self.graph.winfo_width()), max(100, self.graph.winfo_height())
            left, right, top, bottom = 58, width - 12, 18, height - 28
            for fraction, label in ((0, "0"), (.5, "med"), (1, "max")):
                y = bottom - fraction * (bottom - top)
                self.graph.create_line(left, y, right, y, fill=BORDER())
                self.graph.create_text(left - 8, y, text=label, fill=TEXT_DIM(), anchor="e",
                                       font=("Consolas", 9))
            cutoff = time.time() - 60
            recent = [sample for sample in history if sample[0] >= cutoff]
            values = [sample[1] for sample in (recent or history)]
            if len(values) == 1:
                values *= 2
            if len(values) >= 2:
                maximum = max(values) or 1
                coords = []
                for i, value in enumerate(values):
                    x = left + i * (right - left) / (len(values) - 1)
                    y = bottom - value / maximum * (bottom - top)
                    coords.extend((x, y))
                polygon = [left, bottom, *coords, right, bottom]
                self.graph.create_polygon(*polygon, fill=shade_color(ACCENT(), .55), outline="", stipple="gray50")
                # Linia se desenează o singură dată, peste umplere (z-order corect).
                self.graph.create_line(*coords, fill=ACCENT(), width=2, smooth=True)
            self.graph.create_text(left, height - 8, text="60s în urmă", fill=TEXT_DIM(), anchor="w",
                                   font=("Consolas", 9))
            self.graph.create_text(right, height - 8, text="acum", fill=TEXT_DIM(), anchor="e",
                                   font=("Consolas", 9))
        except Exception:
            pass

    def _toggle_pause(self):
        if self.dl.status == DLStatus.DOWNLOADING:
            self.app._pause_download(self.dl)
        elif self.dl.status == DLStatus.PAUSED:
            self.app._resume_download(self.dl)

    def _cancel(self):
        if self.dl.status in (DLStatus.COMPLETED, DLStatus.ERROR, DLStatus.CANCELLED):
            self.app._remove_download(self.dl)
            self._close()
        else:
            self.app._stop_download(self.dl)

    def _retry(self):
        self.app._retry_download(self.dl)

    def _show_folder(self):
        if os.path.exists(self.dl.save_path):
            open_folder(self.dl.save_path)

    def _close(self):
        self.destroy()
        self.app._close_download_details()

    def destroy(self):
        for job in (self._flow_job, self._refresh_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._flow_job = self._refresh_job = None
        try:
            THEME.remove_listener(self)
        except Exception:
            pass
        _prune_dead_commands(self)
        super().destroy()
