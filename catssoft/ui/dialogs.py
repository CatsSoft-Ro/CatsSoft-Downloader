"""ui/dialogs — Dialoguri (adăugare URL, reluare din coadă)."""

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta
import re

from catssoft.core.engine import optimal_segments
from catssoft.core.network import SESSION
from catssoft.core.patches import _prune_dead_commands
from catssoft.logging_setup import logger
from catssoft.ui.theme import ACCENT, BG_CARD, BG_CARD_HOV, BG_INPUT, BG_MAIN, BORDER, ERROR, SUCCESS, TEXT_DIM, TEXT_PRI, TEXT_SEC, THEME, WARNING, shade_color
from catssoft.ui.widgets import ThemedScrollableFrame, _Tooltip, center_window
from catssoft.utils.files import extract_filename_from_url, filename_from_url, is_valid_url
from catssoft.utils.formatting import fmt_size
# ── orig:6142 ──

# ─── Add URL Dialog ──────────────────────────────────────────────────────────
class AddURLDialog(ctk.CTkToplevel):
    def __init__(self, master, on_add: Callable, prefill_url: str = ""):
        super().__init__(master)
        self.title("Descărcare nouă · CatsSoft")
        self.geometry("760x610")
        self.minsize(680, 560)
        THEME.register_window(self)
        THEME.add_listener(self)
        self.configure(fg_color=BG_MAIN())
        self.after(100, lambda: center_window(self, 760, 610))
        self.grab_set()
        self.on_add = on_add
        self._verify_res = None
        self._verified_url = None
        self._submit_in_progress = False
        self._build(prefill_url)
        if prefill_url: self.after(300, self._verify)

    def _on_theme_change(self):
        if not self.winfo_exists(): return
        try:
            self.configure(fg_color=BG_MAIN())
            for widget in self.winfo_children():
                self._update_widget_theme(widget)
        except Exception as e:
            logger.error(f"Dialog theme update error: {e}")

    def _update_widget_theme(self, widget):
        try:
            if isinstance(widget, ctk.CTkFrame) and widget.cget("fg_color") != "transparent":
                widget.configure(fg_color=BG_CARD())
            elif isinstance(widget, ctk.CTkLabel):
                widget.configure(text_color=TEXT_PRI())
            elif isinstance(widget, ctk.CTkEntry):
                widget.configure(fg_color=BG_INPUT(), border_color=BORDER(), text_color=TEXT_PRI())
            elif isinstance(widget, ctk.CTkButton):
                text = widget.cget("text")
                if "Descarcă" in text or "Verifică" in text:
                    # FIX: hover_color era hardcodat albastru (#2563eb) indiferent de accentul
                    # ales din AccentColorSelector - acum derivat live din accentul curent.
                    widget.configure(fg_color=ACCENT(), hover_color=shade_color(ACCENT(), 0.82))
                else:
                    widget.configure(fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_SEC())
        except Exception: pass
        for child in widget.winfo_children():
            self._update_widget_theme(child)

    def _build(self, prefill: str):
        pad = 24
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=pad, pady=(20, 14))
        ctk.CTkLabel(header, text="⬇", width=38, height=38, corner_radius=10,
                     fg_color=BG_CARD(), text_color=ACCENT(),
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left", padx=(0, 12))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title_box, text="Descărcare nouă", font=ctk.CTkFont(size=19, weight="bold"),
                     text_color=TEXT_PRI()).pack(anchor="w")
        ctk.CTkLabel(title_box, text="Adaugă un link și configurează descărcarea",
                     font=ctk.CTkFont(size=12), text_color=TEXT_DIM()).pack(anchor="w", pady=(2, 0))

        url_card = ctk.CTkFrame(self, fg_color=BG_CARD(), corner_radius=12,
                                border_width=1, border_color=BORDER())
        url_card.pack(fill="x", padx=pad, pady=(0, 10))
        ctk.CTkLabel(url_card, text="LINK", font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=TEXT_DIM(), width=54).pack(side="left", padx=(14, 2), pady=13)
        url_row = ctk.CTkFrame(url_card, fg_color="transparent")
        url_row.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)
        self.url_entry = ctk.CTkEntry(url_row, placeholder_text="https://",
                                      fg_color=BG_INPUT(), border_color=BG_INPUT(),
                                      text_color=TEXT_PRI(), height=38)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        if prefill: self.url_entry.insert(0, prefill)
        self.paste_btn = ctk.CTkButton(url_row, text="Lipește", width=76, height=38,
                                       fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                                       text_color=TEXT_SEC(), font=ctk.CTkFont(size=12), command=self._paste)
        self.paste_btn.pack(side="left", padx=(0, 6))
        _Tooltip(self.paste_btn, "Copiați și lipiți aici un link")
        self.verify_btn = ctk.CTkButton(url_row, text="Verifică", width=88, height=38,
                                        fg_color=ACCENT(), hover_color=shade_color(ACCENT(), 0.82),
                                        text_color="#ffffff", command=self._verify)
        self.verify_btn.pack(side="left")


        _Tooltip(self.verify_btn, "Verificați manual link-ul")
        vf = ctk.CTkFrame(self, fg_color=BG_CARD(), corner_radius=10, border_width=1, border_color=BORDER())
        vf.pack(fill="x", padx=pad, pady=(0, 14))
        self.verify_icon = ctk.CTkLabel(vf, text="", font=ctk.CTkFont(size=16), width=30)
        self.verify_icon.pack(side="left", padx=(12, 4), pady=10)
        self.verify_lbl = ctk.CTkLabel(vf, text="Introdu un URL și apasă Verifică",
                                        font=ctk.CTkFont(size=12), text_color=TEXT_DIM(), anchor="w", justify="left")
        self.verify_lbl.pack(side="left", fill="x", expand=True, pady=10, padx=(0, 12))
        sp_row = ctk.CTkFrame(self, fg_color=BG_CARD(), corner_radius=10,
                              border_width=1, border_color=BORDER())
        sp_row.pack(fill="x", padx=pad, pady=(0, 12))
        ctk.CTkLabel(sp_row, text="SALVEAZĂ ÎN", font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=TEXT_DIM(), width=96, anchor="w").pack(side="left", padx=(14, 4), pady=8)
        self.path_entry = ctk.CTkEntry(sp_row, fg_color=BG_INPUT(), border_color=BORDER(),
                                       text_color=TEXT_PRI(), height=36)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.path_entry.insert(0, os.path.expanduser("~/Downloads"))
        ctk.CTkButton(sp_row, text="Răsfoiește…", width=104, height=34, fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                      text_color=ACCENT(), command=self._browse).pack(side="left", padx=(0, 8))

        stream_card = ctk.CTkFrame(self, fg_color=BG_CARD(), corner_radius=10,
                                   border_width=1, border_color=BORDER())
        stream_card.pack(fill="x", padx=pad, pady=(0, 12))
        stream_head = ctk.CTkFrame(stream_card, fg_color="transparent")
        stream_head.pack(fill="x", padx=14, pady=(10, 6))
        ctk.CTkLabel(stream_head, text="FLUXURI PARALELE", font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=TEXT_DIM()).pack(side="left")
        ctk.CTkLabel(stream_head, text="Segmente HTTP pentru descărcare mai rapidă",
                     font=ctk.CTkFont(size=11), text_color=TEXT_SEC()).pack(side="right")
        stream_row = ctk.CTkFrame(stream_card, fg_color="transparent")
        stream_row.pack(fill="x", padx=14, pady=(0, 10))
        self._segment_choice = tk.IntVar(value=max(1, int(self.master.config.get("segments_default", 1))))
        self._segment_buttons = {}
        for count in (1, 2, 4, 8, 16):
            button = ctk.CTkButton(
                stream_row, text=f"{count}×", width=54, height=32,
                fg_color=ACCENT() if self._segment_choice.get() == count else BG_INPUT(),
                hover_color=shade_color(ACCENT(), 0.82),
                text_color="#ffffff" if self._segment_choice.get() == count else TEXT_SEC(),
                command=lambda n=count: self._choose_segments(n))
            button.pack(side="left", padx=(0, 6))
            self._segment_buttons[count] = button
        self.segments_entry = ctk.CTkEntry(stream_row, width=58, fg_color=BG_INPUT(),
                                           border_color=BORDER(), text_color=TEXT_PRI(), justify="center",
                                           placeholder_text="custom")
        self._segments_default_str = str(self.master.config.get("segments_default", 1))
        self.segments_entry.insert(0, self._segments_default_str)
        self.segments_entry.pack(side="left", padx=(2, 0))
        self.segments_entry.bind("<KeyRelease>", self._sync_segment_choice)

        advanced = ctk.CTkFrame(self, fg_color="transparent")
        advanced.pack(fill="x", padx=pad, pady=(0, 4))
        ctk.CTkLabel(advanced, text="Checksum SHA256 (opțional)", font=ctk.CTkFont(size=11),
                     text_color=TEXT_SEC()).pack(anchor="w", pady=(0, 5))
        self.checksum_entry = ctk.CTkEntry(advanced, fg_color=BG_INPUT(), border_color=BORDER(),
                                           text_color=TEXT_PRI(), height=36,
                                           placeholder_text="Lipește valoarea SHA256 pentru verificarea integrității")
        self.checksum_entry.pack(fill="x")
        self.sched_btn = ctk.CTkButton(self, text="📅 Programează descărcarea",
                                  fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                                  text_color=TEXT_SEC(), command=self._open_scheduler)
        self.sched_btn.pack(anchor="w", padx=pad, pady=(6, 0))
        _Tooltip(self.sched_btn, "Programați descărcarea")
        self.scheduled_time = None
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(anchor="e", padx=pad, pady=(12, 16))

        ctk.CTkButton(btns, text="Anulare", width=90, fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                      text_color=TEXT_SEC(), command=self.destroy).pack(side="left", padx=(0, 8))
        self.submit_btn = ctk.CTkButton(btns, text="⬇  Descarcă", width=130, fg_color=ACCENT(), hover_color=shade_color(ACCENT(), 0.82),
                                        text_color="#ffffff", command=self._submit)
        self.submit_btn.pack(side="left")

    def _choose_segments(self, count: int):
        self._segment_choice.set(count)
        self.segments_entry.delete(0, "end")
        self.segments_entry.insert(0, str(count))
        for value, button in self._segment_buttons.items():
            selected = value == count
            button.configure(fg_color=ACCENT() if selected else BG_INPUT(),
                             text_color="#ffffff" if selected else TEXT_SEC())

    def _sync_segment_choice(self, _event=None):
        try:
            value = int(self.segments_entry.get())
        except ValueError:
            value = 0
        if value in self._segment_buttons:
            self._choose_segments(value)
        else:
            for button in self._segment_buttons.values():
                button.configure(fg_color=BG_INPUT(), text_color=TEXT_SEC())

    def _paste(self):
        try:
            clip = self.clipboard_get().strip()
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, clip)
            if clip.startswith(("http://", "https://")): self.after(100, self._verify)
        except tk.TclError: pass

    def _verify_url(self, url: str) -> dict:
        """Probe a URL without touching Tk; this method is intended for worker threads."""
        if not is_valid_url(url):
            return {"ok": False, "error": "URL HTTP(S) invalid", "filename": None}
        try:
            with SESSION.head(url, timeout=10, allow_redirects=True) as response:
                if response.status_code in (405, 501):
                    raise requests.HTTPError(f"HEAD unsupported ({response.status_code})")
                response.raise_for_status()
                headers = dict(response.headers)
                final_url = response.url
                size = int(response.headers.get("content-length", 0) or 0)
                content_type = response.headers.get("content-type", "")
        except requests.HTTPError as head_error:
            # Some legitimate hosts do not support HEAD. Probe one byte without
            # buffering the whole file, then extract the total from Content-Range.
            try:
                with SESSION.get(url, headers={"Range": "bytes=0-0"},
                                 stream=True, timeout=10, allow_redirects=True) as response:
                    response.raise_for_status()
                    headers = dict(response.headers)
                    final_url = response.url
                    content_type = response.headers.get("content-type", "")
                    content_range = response.headers.get("Content-Range", "")
                    match = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", content_range, re.IGNORECASE)
                    size = int(match.group(1)) if match and match.group(1) != "*" else int(
                        response.headers.get("content-length", 0) or 0)
            except Exception:
                return {"ok": False, "error": str(head_error),
                        "filename": filename_from_url(url)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "filename": filename_from_url(url)}

        filename = filename_from_url(url, headers, final_url) or extract_filename_from_url(url)
        return {"ok": True, "filename": filename, "size": size,
                "content_type": content_type, "final_url": final_url}

    def _verify(self):
        """Verify in a worker; all widget changes are dispatched via the app queue."""
        url = self.url_entry.get().strip()
        if not url:
            self._set_v("", "Introdu un URL mai întâi.", TEXT_DIM())
            return
        if not is_valid_url(url):
            self._set_v("⚠", "Introdu un URL HTTP(S) valid.", WARNING())
            return

        self._set_v("⏳", "Se verifică...", TEXT_SEC())
        self.verify_btn.configure(state="disabled")

        def worker():
            try:
                result = self._verify_url(url)
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "filename": None}
            self.master.post_ui(lambda res=result, checked_url=url: self._show_result(res, checked_url))

        threading.Thread(target=worker, daemon=True).start()

    def _show_result(self, res: dict, verified_url: str = None):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        current_url = self.url_entry.get().strip()
        checked_url = verified_url or current_url
        self.verify_btn.configure(state="normal")
        # Ignore a stale result if the user changed the URL while verification ran.
        if current_url != checked_url:
            return
        self._verified_url = checked_url
        self._verify_res = res
        if res.get("ok"):
            size_s = f"  •  {fmt_size(res['size'])}" if res.get("size") else ""
            content_type = res.get("content_type", "")
            ct = content_type.split(";")[0] if content_type else ""
            ct_s = f"  •  {ct}" if ct else ""
            fn = res.get("filename") or filename_from_url(checked_url)
            self._set_v("✅", f"{fn}{size_s}{ct_s}", SUCCESS())
            if res.get("size") and self.segments_entry.get() == self._segments_default_str:
                suggested = str(optimal_segments(res["size"]))
                self.segments_entry.delete(0, "end")
                self.segments_entry.insert(0, suggested)
                self._segments_default_str = suggested
        else:
            self._set_v("❌", f"Verificare eșuată: {res.get('error', 'eroare necunoscută')}", ERROR())

    def _set_v(self, icon: str, msg: str, color: str):
        self.verify_icon.configure(text=icon)
        self.verify_lbl.configure(text=msg, text_color=color)

    def _browse(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, d)

    def _open_scheduler(self):
        sched_win = ctk.CTkToplevel(self)
        sched_win.title("Programează")
        sched_win.geometry("400x300")
        sched_win.configure(fg_color=BG_MAIN())
        THEME.add_listener(sched_win)
        ctk.CTkLabel(sched_win, text="Data și ora", font=ctk.CTkFont(size=14),
                     text_color=TEXT_PRI()).pack(pady=10)
        date_entry = ctk.CTkEntry(sched_win, placeholder_text="YYYY-MM-DD HH:MM",
                                  fg_color=BG_INPUT(), text_color=TEXT_PRI(), width=200)
        date_entry.pack(pady=10)
        date_entry.insert(0, (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))
        repeat_var = ctk.StringVar(value="once")
        ctk.CTkOptionMenu(sched_win, values=["once", "daily", "weekly"],
                          variable=repeat_var, fg_color=BG_INPUT(),
                          text_color=TEXT_PRI()).pack(pady=10)
        def save_schedule():
            try:
                self.scheduled_time = datetime.strptime(date_entry.get(), "%Y-%m-%d %H:%M")
                self.scheduled_repeat = repeat_var.get()
                sched_win.destroy()
            except ValueError:
                messagebox.showerror("Eroare", "Format dată invalid", parent=sched_win)
        ctk.CTkButton(sched_win, text="Salvează", fg_color=ACCENT(), command=save_schedule).pack(pady=10)

    def _submit(self):
        if self._submit_in_progress:
            return
        url = self.url_entry.get().strip()
        path = self.path_entry.get().strip() or os.path.expanduser("~/Downloads")
        try:
            segments = min(32, max(1, int(self.segments_entry.get())))
        except ValueError:
            segments = 1
        checksum = self.checksum_entry.get().strip()

        if not url:
            messagebox.showerror("Eroare", "Introdu un URL.", parent=self)
            return
        if not is_valid_url(url):
            messagebox.showerror("Eroare", "Introdu un URL HTTP(S) valid.", parent=self)
            return

        if self._verify_res and self._verify_res.get("ok") and self._verified_url == url:
            self._finish_submit(url, path, segments, checksum, self._verify_res)
            return

        # Previously the UI thread joined the worker for up to 15 seconds here.
        self._submit_in_progress = True
        self.verify_btn.configure(state="disabled")
        self.url_entry.configure(state="disabled")
        self.submit_btn.configure(state="disabled")
        self._set_v("⏳", "Se verifică link-ul înainte de adăugare...", TEXT_SEC())

        def worker():
            try:
                result = self._verify_url(url)
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "filename": None}
            self.master.post_ui(lambda res=result: self._complete_submit(url, path, segments, checksum, res))

        threading.Thread(target=worker, daemon=True).start()

    def _complete_submit(self, url, path, segments, checksum, result):
        if getattr(self.master, "_closing", False):
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._submit_in_progress = False
        self.verify_btn.configure(state="normal")
        try:
            self.url_entry.configure(state="normal")
            self.submit_btn.configure(state="normal")
        except tk.TclError:
            pass
        self._show_result(result, url)
        self._finish_submit(url, path, segments, checksum, result)

    def _finish_submit(self, url, path, segments, checksum, result):
        # O verificare eșuată NU trebuie să pornească descărcarea în tăc: anterior
        # mesajul de eroare era afișat și apoi ignorat, iar transferul pornea oricum.
        if result is not None and not result.get("ok"):
            error_text = str(result.get("error") or "link inaccesibil")
            proceed = messagebox.askyesno(
                "Verificare eșuată",
                f"Link-ul nu a putut fi verificat:\n\n{error_text}\n\n"
                "Adaugă oricum descărcarea?",
                parent=self)
            if not proceed:
                self._set_v("❌", f"Verificare eșuată: {error_text[:120]}", ERROR())
                return

        filename = result.get("filename") if result and result.get("ok") else None
        if not filename:
            filename = filename_from_url(url) or f"download_{int(time.time())}.bin"

        if self.scheduled_time:
            self.master.scheduler.add(url, filename, self.scheduled_time, path,
                                      self.scheduled_repeat, segments, checksum)
            messagebox.showinfo("Programat", "Descărcarea a fost programată!", parent=self)
        else:
            self.on_add(url, path, filename, segments, checksum)
        self.destroy()

    def destroy(self):
        THEME.unregister_window(self)
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:6539 ──

# ─── Resume Queue Dialog (FEATURE NOU) ─────────────────────────────────────────
class ResumeQueueDialog(ctk.CTkToplevel):
    """Afișat la pornire dacă există descărcări neterminate din sesiunea anterioară
    (aplicația a fost închisă în timp ce ceva era queued/downloading/paused)."""

    def __init__(self, app: "DownloadManager", entries: List[dict]):
        super().__init__(app)
        self.app = app
        self.entries = entries
        self.checks: List[tuple] = []

        self.title("Descărcări neterminate")
        self.geometry("460x440")
        self.configure(fg_color=BG_MAIN())
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._discard_all)
        THEME.register_window(self)
        THEME.add_listener(self)

        self._build()

    def _build(self):
        n = len(self.entries)
        header_txt = f"Ai {n} descărcare{'i' if n != 1 else ''} neterminat{'e' if n != 1 else 'ă'} din sesiunea anterioară"
        ctk.CTkLabel(self, text=header_txt, font=ctk.CTkFont(size=14, weight="bold"),
                    text_color=TEXT_PRI(), wraplength=420, justify="left").pack(
                    padx=20, pady=(20, 4), anchor="w")
        ctk.CTkLabel(self, text="Alege pe care vrei să le reiei - progresul deja descărcat nu se pierde:",
                    font=ctk.CTkFont(size=12), text_color=TEXT_SEC(), wraplength=420,
                    justify="left").pack(padx=20, pady=(0, 10), anchor="w")

        scroll = ThemedScrollableFrame(self, fg_color_key='bg_card', height=240)
        scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        for entry in self.entries:
            var = tk.BooleanVar(value=True)
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkCheckBox(row, text="", variable=var, width=20, checkbox_width=20,
                           checkbox_height=20, fg_color=ACCENT(),
                           hover_color=shade_color(ACCENT(), 0.82)).pack(side="left")

            total = entry.get("total") or 0
            downloaded = entry.get("downloaded") or 0
            pct = (downloaded / total * 100) if total else 0
            size_txt = f"{pct:.0f}%  •  {fmt_size(downloaded)} / {fmt_size(total)}" if total \
                else fmt_size(downloaded)

            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=(6, 0))
            ctk.CTkLabel(info, text=entry["filename"], font=ctk.CTkFont(size=12),
                        text_color=TEXT_PRI(), anchor="w").pack(fill="x")
            ctk.CTkLabel(info, text=size_txt, font=ctk.CTkFont(size=10),
                        text_color=TEXT_DIM(), anchor="w").pack(fill="x")

            self.checks.append((entry, var))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(btns, text="Renunță la toate", width=140, fg_color=BG_CARD(),
                     hover_color=BG_CARD_HOV(), text_color=TEXT_SEC(),
                     command=self._discard_all).pack(side="left")
        ctk.CTkButton(btns, text="Reia selectate", width=140, fg_color=ACCENT(),
                     hover_color=shade_color(ACCENT(), 0.82),
                     command=self._resume_selected).pack(side="right")

    def _resume_selected(self):
        for entry, var in self.checks:
            if var.get():
                self.app._resume_from_queue_entry(entry)
            else:
                self.app._discard_queue_entry_files(entry)
        self.app.db.clear_active_queue()
        self.destroy()

    def _discard_all(self):
        for entry in self.entries:
            self.app._discard_queue_entry_files(entry)
        self.app.db.clear_active_queue()
        self.destroy()

    def _on_theme_change(self):
        if self.winfo_exists():
            try:
                self.configure(fg_color=BG_MAIN())
            except Exception:
                pass

    def destroy(self):
        THEME.unregister_window(self)
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
