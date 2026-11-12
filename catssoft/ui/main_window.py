"""ui/main_window — Fereastra principală."""

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
from datetime import datetime, timedelta
from pathlib import Path
import math

from catssoft.core.automation import ClipboardMonitor, TrayManager
from catssoft.core.capabilities import NOTIFY_AVAILABLE, TRAY_AVAILABLE
from catssoft.core.config import ConfigManager
from catssoft.core.database import DatabaseManager
from catssoft.core.engine import _download_worker_entry, _remove_download_meta, _remove_segment_parts, scan_orphan_temp_files
from catssoft.core.models import DLStatus, Download, ScheduledDownload, _close_active_responses, _close_response_async
from catssoft.core.network import NetworkAdapterManager, SESSION, _normalize_proxy_url, _socks_proxy_available, apply_proxy_to_session
from catssoft.core.patches import _prune_dead_commands
from catssoft.core.scheduler import DownloadScheduler
from catssoft.core.smart import SmartAutomationEngine
from catssoft.logging_setup import logger
from catssoft.paths import app_dir
from catssoft.ui.cards import DownloadCard, SwipeCardHost
from catssoft.ui.details import DownloadDetailsView
from catssoft.ui.dialogs import AddURLDialog, ResumeQueueDialog
from catssoft.ui.theme import ACCENT, BG_CARD, BG_CARD_HOV, BG_INPUT, BG_MAIN, BG_NAV, BG_SIDEBAR, BORDER, ERROR, SUCCESS, TEXT_DIM, TEXT_PRI, TEXT_SEC, THEME, WARNING, shade_color
from catssoft.ui.widgets import AccentColorSelector, ThemePreviewCard, ThemedFrame, ThemedScrollableFrame, _Tooltip, center_window
from catssoft.utils.files import extract_file_name_from_url, extract_filename_from_url, guess_filetype, open_folder, sanitize_filename, unique_target_path
from catssoft.utils.formatting import fmt_datetime, fmt_duration, fmt_size, fmt_speed, safe_int
# ── orig:6634 ──
# ─── Main Application ─────────────────────────────────────────────────────────
class DownloadManager(ctk.CTk):
    def __init__(self):
        super().__init__()
        self._closing = False
        self._session_started_at = datetime.now()
        self._session_transfer_lock = threading.Lock()
        self._session_transferred_total = 0
        self._active_queue_persist_id = None
        self._poll_job_id = None
        self._appearance_poll_id = None
        self._queue_sequence = 0
        self.title("CatsSoft Downloader v6.13 — Adaptive Network Engine")
        self.geometry("1024x720")
        self.minsize(850, 600)
        THEME.register_window(self)
        self.configure(fg_color=BG_MAIN())
        THEME.add_listener(self)
        self.after(100, lambda: center_window(self, 850, 600))
        try:
            icon_path = app_dir() / "icon" / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception as exc:
            logger.debug("Window icon unavailable: %s", exc)

        self.db = DatabaseManager()
        self.config = ConfigManager(self.db)
        self.network_manager = NetworkAdapterManager(self.config)
        # Proxy-ul se aplică pe sesiunea globală înainte de primul transfer.
        apply_proxy_to_session(SESSION, self.config.get("proxy_url", ""))
        self.scheduler = DownloadScheduler(self, self.db)
        self.clipboard_monitor = ClipboardMonitor(self)
        self.tray_manager = TrayManager(self)
        self._apply_saved_theme()
        self.configure(fg_color=BG_MAIN())

        self.downloads: List[Download] = []
        self.cards: dict[int, DownloadCard] = {}
        self.swipe_hosts: dict[int, SwipeCardHost] = {}
        self.selected_download_ids = set()
        self.update_queue: queue.Queue = queue.Queue()
        self.smart_automation = SmartAutomationEngine(self)
        try:
            self.max_concurrent: int = safe_int(self.config.get("max_concurrent", 3), 3, 1, 32)
        except (TypeError, ValueError):
            self.max_concurrent = 3
        self._bandwidth_lock = threading.Lock()
        self._bandwidth_deadline = 0.0
        self.default_folder: str = self.config.get("default_folder", os.path.expanduser("~/Downloads"))
        self._search_var: tk.StringVar = tk.StringVar()
        self._search_var.trace_add("write", self._filter_cards)
        self._status_filter = tk.StringVar(value="Toate")
        self._sort_order = tk.StringVar(value="Cele mai noi")

        settings = {
            "max_title_length": 50,
            "max_desc_length": 50
        }

        # În get_response():
        self.max_title_length = settings["max_title_length"]
        self.max_desc_length = settings["max_desc_length"]

        self._build_ui()
        self._update_status_bar(update_disk=True)
        self._status_tick_id = self.after(500, self._tick_status_bar)
        self._appearance_poll_id = self.after(1800, self._poll_system_appearance)
        self._setup_keyboard_shortcuts()
        self._poll_updates()
        self.network_manager.refresh_async(lambda: self.post_ui(self._render_network_cards))
        self._network_monitor_id = self.after(20000, self._network_monitor_tick)
        self._global_speed_history = []

        self.clipboard_monitor.start()
        self.scheduler.start()
        self.tray_manager.setup()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._check_resumable_queue)  # după ce fereastra e vizibilă

    def _queue_snapshot_signature(self):
        """Semnătura stării cozii active.

        Se schimbă la orice modificare de stare (status, prioritate, limită
        per-fișier, poziție în coadă, programarea unei reîncercări) și la un
        progres avansat cu cel puțin 1%. Poziția în listă e folosită în loc de
        ``id()``, pentru că CPython poate refolosi un id pentru un obiect nou.
        """
        entries = []
        for index, dl in enumerate(self.downloads):
            try:
                with dl.lock:
                    total = max(0, int(dl.total or 0))
                    downloaded = max(0, int(dl.downloaded or 0))
                    status = dl.status
                    priority = dl.priority
                    queue_order = dl.queue_order
                    speed_override = getattr(dl, "speed_limit_override", None)
                    retry_at = float(getattr(dl, "auto_retry_at", 0.0) or 0.0)
                    retry_count = int(getattr(dl, "automatic_retry_count", 0) or 0)
            except Exception:
                continue
            bucket = (downloaded * 100 // total) if total > 0 else 0
            entries.append((index, dl.filename, dl.url,
                            getattr(status, "value", str(status)),
                            priority, queue_order, speed_override, total, bucket,
                            retry_at, retry_count))
        return tuple(entries)

    def _persist_active_queue(self):
        """Snapshot periodic pentru recuperare după închidere neașteptată.

        Scrierea în DB se face doar dacă starea cozii s-a schimbat: altfel, o
        rescriere completă (DELETE + INSERT pentru fiecare element) la fiecare
        5 secunde ar fi inutilă. La închidere snapshot-ul se salvează oricum,
        fără condiții, deci o închidere neașteptată nu pierde nimic.
        """
        self._active_queue_persist_id = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        try:
            signature = self._queue_snapshot_signature()
            if signature != getattr(self, "_queue_snapshot_state", None):
                self.db.save_active_queue(self.downloads)
                self._queue_snapshot_state = signature
        except Exception as exc:
            logger.error("Nu am putut persista coada activă: %s", exc)
        if not getattr(self, "_closing", False):
            self._active_queue_persist_id = self.after(5000, self._persist_active_queue)

    def _check_resumable_queue(self):
        """FEATURE NOU: la pornire, verifică dacă există descărcări neterminate din
        sesiunea anterioară și oferă utilizatorului opțiunea de a le relua.
        FIX: ciclul periodic de _persist_active_queue pornește DOAR după ce decizia
        utilizatorului a fost luată (wait_window blochează până se închide dialogul) -
        altfel primul apel de persistare, cu self.downloads încă gol, ar șterge coada
        salvată înainte ca dialogul să apuce s-o arate."""
        try:
            entries = self.db.load_active_queue()
        except Exception as e:
            logger.error(f"Nu am putut încărca coada persistată: {e}")
            entries = []
        try:
            if entries:
                dlg = ResumeQueueDialog(self, entries)
                self.wait_window(dlg)
        except Exception:
            logger.exception("Dialogul de reluare a eșuat")
        finally:
            # Ciclul de persistență trebuie pornit ORIUNE, chiar dacă dialogul a
            # aruncat o excepție: altfel coada activă nu mai este salvată niciodată
            # în sesiunea curentă, iar reluarea la pornire se pierde definitiv.
            self._persist_active_queue()

    def _resume_from_queue_entry(self, entry: dict):
        """Reconstituie transferul și lasă motorul să recalculeze progresul din artefactele reale."""
        dl = Download(url=entry["url"], filename=entry["filename"], save_path=entry["save_path"],
                      segments=max(1, safe_int(entry.get("segments", 1), 1, 1, 32)),
                      checksum=entry.get("checksum", ""),
                      checksum_algo=entry.get("checksum_algo", "sha256"))
        try:
            if entry.get("added_date"):
                dl.added_date = datetime.fromisoformat(entry["added_date"])
        except (TypeError, ValueError):
            pass
        dl.app = self
        dl.speed_limit = max(0, safe_int(entry.get("speed_limit", 0), 0))
        override = entry.get("speed_limit_override")
        dl.speed_limit_override = None if override is None else max(0, safe_int(override, 0))
        dl.priority = safe_int(entry.get("priority", 1), 1, 0, 2)
        dl.automatic_retry_count = safe_int(entry.get("automatic_retry_count", 0), 0, 0, 100)
        dl.auto_retry_at = max(0.0, float(entry.get("auto_retry_at", 0.0) or 0.0))
        persisted_order = safe_int(entry.get("queue_order", 0), 0, 0)
        if persisted_order <= 0:
            self._queue_sequence += 1
            dl.queue_order = self._queue_sequence
        else:
            dl.queue_order = persisted_order
            self._queue_sequence = max(self._queue_sequence, dl.queue_order)
        dl.filename_locked = not str(entry.get("filename", "")).lower().endswith(".tmp")
        dl.status = DLStatus.QUEUED
        dl.started_at = None
        self.downloads.append(dl)
        self._render_card(dl)
        self._next_queued()
        self._update_stats()
        logger.info("Reluare din coada persistată: %s", dl.filename)

    def _discard_queue_entry_files(self, entry: dict):
        """Șterge doar artefactele incomplete pentru intrările la care s-a renunțat."""
        save_path = entry.get("save_path")
        if save_path:
            try:
                if os.path.isfile(save_path):
                    actual_size = os.path.getsize(save_path)
                    recorded_total = safe_int(entry.get("total", 0), 0, 0)
                    # Ștergem doar când știm sigur că fișierul este incomplet.
                    # Cu total necunoscut (0), fișierul poate fi perfect complet —
                    # ștergerea lui ar distruge o descărcare reușită.
                    if recorded_total > 0 and actual_size != recorded_total:
                        os.remove(save_path)
                        _remove_download_meta(save_path)
            except OSError as exc:
                logger.warning("Could not remove incomplete output file: %s", exc)
            _remove_segment_parts(save_path)
            _remove_download_meta(save_path)

    def _notify_scan_flag(self, dl: Download, scan: dict):
        """FEATURE NOU: notificare când scanarea Windows Defender semnalează ceva
        neobișnuit (cod de ieșire diferit de 0). Nu afirmăm ferm 'virus găsit' - doar
        îndrumăm utilizatorul să verifice manual, Defender gestionează el carantina."""
        logger.warning(f"Rezultat scanare pentru {dl.filename}: {scan.get('output', '')[:300]}")
        if NOTIFY_AVAILABLE and self.config.get("notifications", True):
            try:
                notification.notify(
                    title="Scanarea a semnalat ceva neobișnuit",
                    message=f"{dl.filename} - verifică manual în Windows Security.",
                    app_name="CatsSoft Downloader", timeout=8)
            except Exception:
                pass

    def _apply_saved_theme(self):
        theme_name = self.config.get("theme_name", "Dark Modern")
        saved_mode = self.config.get("theme_mode", None)
        mode = str(saved_mode or self._theme_family(theme_name)).lower()
        if mode not in {"auto", "light", "dark"}:
            mode = "dark"
        ctk.set_appearance_mode("system" if mode == "auto" else mode)
        is_dark = ctk.get_appearance_mode().lower() == "dark" if mode == "auto" else mode == "dark"
        family = "dark" if is_dark else "light"
        self._last_system_appearance = family
        if self._theme_family(theme_name) != family:
            theme_name = "Dark Modern" if is_dark else "Light Modern"
        accent_color = self.config.get("accent_color", "#3b82f6")
        theme = THEME.get_theme_by_name(theme_name)
        theme = theme.with_accent(accent_color)
        THEME.set_theme(theme, apply_immediately=False)

    @staticmethod
    def _theme_family(theme_name):
        light_themes = {"Light Modern", "Light Warm", "Pastel Bloom", "Rose Gold",
                        "Lavender Mist", "Ivory Elegance"}
        return "light" if theme_name in light_themes else "dark"

    def _set_effective_theme(self, preferred_name=None):
        mode = self.theme_mode_var.get().lower()
        if mode == "auto":
            ctk.set_appearance_mode("system")
            is_dark = ctk.get_appearance_mode().lower() == "dark"
        else:
            is_dark = mode == "dark"
            ctk.set_appearance_mode(mode)
        family = "dark" if is_dark else "light"
        name = preferred_name or self.theme_var.get()
        if self._theme_family(name) != family:
            name = "Dark Modern" if is_dark else "Light Modern"
        accent = (self.accent_selector.current_color if hasattr(self, "accent_selector")
                  else self.config.get("accent_color", "#3b82f6"))
        THEME.set_theme(THEME.get_theme_by_name(name).with_accent(accent), apply_immediately=True)
        self._last_system_appearance = family
        self._refresh_theme_card_selection()
        self.after(100, self._on_theme_change)

    def _refresh_theme_card_selection(self):
        for name, card in getattr(self, "theme_cards", {}).items():
            if card.winfo_exists():
                card.set_selected(name == self.theme_var.get())

    def _poll_system_appearance(self):
        self._appearance_poll_id = None
        if not self.winfo_exists() or getattr(self, "_closing", False):
            return
        if getattr(self, "theme_mode_var", None) and self.theme_mode_var.get() == "Auto":
            current = ctk.get_appearance_mode().lower()
            if current in {"dark", "light"} and current != self._last_system_appearance:
                self._set_effective_theme(self.theme_var.get())
        if not getattr(self, "_closing", False):
            self._appearance_poll_id = self.after(1800, self._poll_system_appearance)

    def _on_theme_change(self):
        if not self.winfo_exists(): return
        self.configure(fg_color=BG_MAIN())
        for tab in self._tab_frames.values():
            self._refresh_tab_theme(tab)
        # Aceste liste sunt reconstruite dinamic; refacerea lor aplică valorile
        # curente chiar și rândurilor care nu sunt listeners individuali.
        if hasattr(self, "queue_table"):
            self._refresh_queue()
        if hasattr(self, "scheduled_table"):
            self._refresh_scheduled()
        if hasattr(self, "history_table"):
            self._filter_history()
        self._refresh_theme_controls()
        self._update_status_bar_theme()

    def _refresh_tab_theme(self, widget):
        previous = getattr(THEME, "_previous_theme", THEME.theme)
        color_map = {
            previous.bg_main: BG_MAIN(), previous.bg_sidebar: BG_SIDEBAR(),
            previous.bg_card: BG_CARD(), previous.bg_card_hover: BG_CARD_HOV(),
            previous.bg_nav: BG_NAV(), previous.bg_input: BG_INPUT(),
            previous.border: BORDER(), previous.text_primary: TEXT_PRI(),
            previous.text_secondary: TEXT_SEC(), previous.text_dim: TEXT_DIM(),
            previous.accent: ACCENT(),
        }
        def recolor(value):
            if isinstance(value, str):
                return color_map.get(value, value)
            if isinstance(value, tuple):
                return tuple(color_map.get(part, part) if isinstance(part, str) else part for part in value)
            return value
        try:
            if isinstance(widget, ctk.CTkFrame):
                fg = widget.cget("fg_color")
                if fg not in ["transparent", None]:
                    widget.configure(fg_color=recolor(fg))
            elif isinstance(widget, ctk.CTkLabel):
                widget.configure(text_color=recolor(widget.cget("text_color")))
            elif isinstance(widget, ctk.CTkButton):
                old_fg = widget.cget("fg_color")
                new_fg = recolor(old_fg)
                old_accent = previous.accent
                accent_button = old_fg == old_accent or (isinstance(old_fg, tuple) and old_accent in old_fg)
                widget.configure(fg_color=new_fg,
                                 hover_color=shade_color(ACCENT(), 0.82) if accent_button else recolor(widget.cget("hover_color")),
                                 text_color=recolor(widget.cget("text_color")))
            elif isinstance(widget, ctk.CTkOptionMenu):
                widget.configure(fg_color=recolor(widget.cget("fg_color")),
                                 button_color=recolor(widget.cget("button_color")),
                                 button_hover_color=recolor(widget.cget("button_hover_color")),
                                 text_color=recolor(widget.cget("text_color")))
            elif isinstance(widget, ctk.CTkEntry):
                widget.configure(fg_color=recolor(widget.cget("fg_color")),
                                 border_color=recolor(widget.cget("border_color")),
                                 text_color=recolor(widget.cget("text_color")))
            elif isinstance(widget, ctk.CTkProgressBar):
                widget.configure(fg_color=recolor(widget.cget("fg_color")),
                                 progress_color=recolor(widget.cget("progress_color")))
            elif isinstance(widget, ctk.CTkSwitch):
                widget.configure(progress_color=ACCENT())
            elif isinstance(widget, ctk.CTkSegmentedButton):
                widget.configure(selected_color=ACCENT(),
                                 selected_hover_color=shade_color(ACCENT(), 0.82))
        except Exception:
            pass
        for child in widget.winfo_children():
            self._refresh_tab_theme(child)

    def _build_ui(self):
        sidebar = ThemedFrame(self, fg_color_key='bg_sidebar', width=70, corner_radius=0)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)
        right = ThemedFrame(self, fg_color_key='bg_main', corner_radius=0)
        right.pack(side="left", fill="both", expand=True)
        nav = ThemedFrame(right, fg_color_key='bg_nav', height=54, corner_radius=0)
        nav.pack(fill="x")
        nav.pack_propagate(False)
        self._build_nav(nav)
        self._build_status_bar(right)
        self.content = ThemedFrame(right, fg_color_key='transparent')
        self.content.pack(fill="both", expand=True)
        self._tab_frames = {
            "Downloads": self._build_downloads_tab(), "Queue": self._build_queue_tab(),
            "History": self._build_history_tab(), "Networks": self._build_networks_tab(),
            "Scheduled": self._build_scheduled_tab(), "Settings": self._build_settings_tab(),
        }
        self._switch_tab("Downloads")

    def _build_sidebar(self, sidebar):
        ctk.CTkFrame(sidebar, height=14, fg_color="transparent").pack()
        def sb(icon, tip, cmd):
            btn = ctk.CTkButton(sidebar, text=icon, width=48, height=48, fg_color="transparent",
                               hover_color=BG_CARD_HOV(), corner_radius=10, text_color=TEXT_SEC(),
                               font=ctk.CTkFont(size=20), command=cmd)
            btn.pack(pady=5, padx=11)
            return btn
        sb("＋", "Adaugă URL nou", self._open_add_dialog)
        sb("📋", "Lipește din clipboard", self._paste_and_add)
        ctk.CTkFrame(sidebar, height=1, fg_color=BORDER()).pack(fill="x", padx=11, pady=6)
        sb("⬇", "Descărcări", lambda: self._switch_tab("Downloads"))
        sb("☰", "Coada", lambda: self._switch_tab("Queue"))
        sb("📜", "Istoric", lambda: self._switch_tab("History"))
        sb("⇄", "Rețele", lambda: self._switch_tab("Networks"))
        sb("📅", "Programate", lambda: self._switch_tab("Scheduled"))
        sb("⚙", "Setări", lambda: self._switch_tab("Settings"))
        ctk.CTkFrame(sidebar, height=1, fg_color=BORDER()).pack(fill="x", padx=11, pady=6)
        sb("📂", "Deschide folder", self._open_downloads_folder)

    def _build_nav(self, nav):
        self._nav_frame = nav
        self._nav_btns = {}
        self._nav_animation_id = None
        for i, t in enumerate(["Downloads", "Queue", "History", "Networks", "Scheduled", "Settings"]):
            btn = ctk.CTkButton(nav, text=t, fg_color="transparent", hover_color=BG_CARD(),
                               text_color=TEXT_SEC(), corner_radius=6, font=ctk.CTkFont(size=12),
                               height=36, width=88, command=lambda x=t: self._switch_tab(x))
            btn.pack(side="left", padx=(12 if i == 0 else 2, 2), pady=9)
            self._nav_btns[t] = btn
        ctk.CTkEntry(nav, textvariable=self._search_var, placeholder_text="🔍  Caută…",
                     fg_color=BG_INPUT(), border_color=BORDER(), text_color=TEXT_PRI(),
                     width=145, height=34).pack(side="right", padx=10, pady=10)
        # Accent underline glides beneath the active tab; its geometry is independent
        # of the pack-managed tab buttons.
        self._nav_indicator = ctk.CTkFrame(nav, width=38, height=3, fg_color=ACCENT(), corner_radius=2)
        # CustomTkinter primește width/height în constructor/configure(), nu în place().
        self._nav_indicator.place(x=12, y=50)

    def _animate_nav_indicator(self, name: str):
        if not hasattr(self, "_nav_indicator") or not self._nav_indicator.winfo_exists():
            return
        button = self._nav_btns.get(name)
        if button is None or not button.winfo_exists():
            return
        self.update_idletasks()
        target_x = button.winfo_x() + 8
        target_w = max(24, button.winfo_width() - 16)
        target_y = max(0, self._nav_frame.winfo_height() - 3)
        try:
            animations_enabled = bool(self.config.get("animations", True))
        except Exception:
            animations_enabled = True
        if self._nav_animation_id is not None:
            try: self.after_cancel(self._nav_animation_id)
            except Exception: pass
            self._nav_animation_id = None
        if not animations_enabled:
            self._nav_indicator.configure(width=target_w, height=3)
            self._nav_indicator.place(x=target_x, y=target_y)
            return
        start_x = self._nav_indicator.winfo_x()
        start_w = max(1, self._nav_indicator.winfo_width())
        started = time.monotonic()
        duration = 0.20

        def frame():
            if not self.winfo_exists() or not self._nav_indicator.winfo_exists():
                self._nav_animation_id = None
                return
            t = min(1.0, (time.monotonic() - started) / duration)
            eased = 1.0 - (1.0 - t) ** 3
            x = round(start_x + (target_x - start_x) * eased)
            width = max(1, round(start_w + (target_w - start_w) * eased))
            # Lățimea se animă prin configure(); place() gestionează doar poziția.
            self._nav_indicator.configure(width=width, height=3)
            self._nav_indicator.place(x=x, y=target_y)
            if t >= 1.0:
                self._nav_indicator.configure(width=target_w, height=3)
                self._nav_indicator.place(x=target_x, y=target_y)
                self._nav_animation_id = None
            else:
                self._nav_animation_id = self.after(16, frame)
        frame()

    def _build_status_bar(self, parent):
        """Bară compactă cu trafic real de sesiune, coadă, ETA și spațiu liber."""
        self.statusbar = ThemedFrame(parent, fg_color_key="bg_nav", corner_radius=0, height=44)
        self.statusbar.pack(side="bottom", fill="x")
        self.statusbar.pack_propagate(False)
        self.statusbar_separator = ctk.CTkFrame(parent, height=1, fg_color=BORDER(), corner_radius=0)
        self.statusbar_separator.pack(side="bottom", fill="x")
        row = ctk.CTkFrame(self.statusbar, fg_color="transparent", corner_radius=0)
        row.pack(fill="both", expand=True, padx=12, pady=4)

        self.status_disk_lbl = ctk.CTkLabel(row, text="Spațiu liber —", width=105, anchor="w",
            font=ctk.CTkFont(size=10), text_color=TEXT_SEC())
        self.status_disk_lbl.pack(side="left", padx=(0, 6))
        self._status_dividers = []
        divider = ctk.CTkFrame(row, width=1, height=18, fg_color=BORDER(), corner_radius=0)
        divider.pack(side="left", padx=(2, 8)); self._status_dividers.append(divider)
        self.status_dot_canvas = tk.Canvas(row, width=10, height=10, bg=BG_NAV(), highlightthickness=0, bd=0)
        self.status_dot_canvas.pack(side="left", padx=(0, 4))
        self.status_dot_id = self.status_dot_canvas.create_oval(1, 1, 9, 9, fill=TEXT_DIM(), outline="")
        self.status_state_lbl = ctk.CTkLabel(row, text="Pregătit", width=70, anchor="w",
            font=ctk.CTkFont(size=10), text_color=TEXT_SEC())
        self.status_state_lbl.pack(side="left", padx=(0, 6))
        self.status_speed_lbl = ctk.CTkLabel(row, text="↓ 0 KB/s", width=100, anchor="w",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=ACCENT())
        self.status_speed_lbl.pack(side="left", padx=(0, 6))
        divider = ctk.CTkFrame(row, width=1, height=18, fg_color=BORDER(), corner_radius=0)
        divider.pack(side="left", padx=(1, 8)); self._status_dividers.append(divider)
        self.status_counts_lbl = ctk.CTkLabel(row, text="0 active · 0 în coadă", anchor="w",
            font=ctk.CTkFont(size=10), text_color=TEXT_SEC())
        self.status_counts_lbl.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.status_eta_lbl = ctk.CTkLabel(row, text="ETA —", width=66, anchor="e",
            font=ctk.CTkFont(size=10), text_color=TEXT_SEC())
        self.status_eta_lbl.pack(side="left", padx=(0, 8))
        self.status_transferred_lbl = ctk.CTkLabel(row, text="Sesiune 0 B", width=105, anchor="e",
            font=ctk.CTkFont(size=10), text_color=TEXT_DIM())
        self.status_transferred_lbl.pack(side="left", padx=(0, 7))
        self.status_unit_btn = ctk.CTkButton(row, text="KB/s", width=58, height=26, corner_radius=7,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=ACCENT(),
            font=ctk.CTkFont(size=10, weight="bold"), command=self._toggle_status_speed_unit)
        self.status_unit_btn.pack(side="right")
        self.slow_mode_btn = ctk.CTkButton(row, text="Slow mode OFF", width=112, height=26, corner_radius=7,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_SEC(),
            font=ctk.CTkFont(size=10, weight="bold"), command=self._toggle_slow_mode)
        self.slow_mode_btn.pack(side="right", padx=(0, 6))
        self._status_speed_unit = "KB/s"
        self._update_slow_mode_button()
        self._status_tick_id = None
        self._status_animation_phase = 0
        self._status_last_animation_at = 0.0
        self._status_last_disk_update = 0.0
        self._status_last_free_bytes = None

    def _toggle_status_speed_unit(self):
        units = ["KB/s", "MB/s", "Mbps"]
        current = getattr(self, "_status_speed_unit", "KB/s")
        self._status_speed_unit = units[(units.index(current) + 1) % len(units)] if current in units else units[0]
        if hasattr(self, "status_unit_btn") and self.status_unit_btn.winfo_exists():
            self.status_unit_btn.configure(text=self._status_speed_unit)
        self._update_status_bar()

    @staticmethod
    def _format_status_speed(speed_bps: float, unit: str) -> str:
        speed_bps = max(0.0, float(speed_bps or 0.0))
        if unit == "MB/s":
            return f"{speed_bps / (1024 ** 2):.2f} MB/s"
        if unit == "Mbps":
            return f"{speed_bps * 8 / 1_000_000:.2f} Mbps"
        return f"{speed_bps / 1024:.0f} KB/s"

    def _read_free_disk_space(self):
        path = os.path.abspath(os.path.expanduser(self.default_folder or "~"))
        while not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                path = os.path.expanduser("~")
                break
            path = parent
        try:
            return shutil.disk_usage(path).free
        except (OSError, ValueError):
            return None

    def _update_status_bar(self, update_disk=False):
        if not hasattr(self, "statusbar") or not self.statusbar.winfo_exists():
            return
        active = queued = paused = completed = errors = 0
        speed = remaining_known = 0
        session_transferred = 0
        unknown_remaining = False
        for dl in tuple(self.downloads):
            with dl.lock:
                status = dl.status
                current_speed = max(0.0, float(dl.speed or 0.0))
                current_downloaded = max(0, int(dl.downloaded or 0))
                current_total = max(0, int(dl.total or 0))
            if status == DLStatus.DOWNLOADING:
                active += 1; speed += current_speed
            elif status == DLStatus.QUEUED:
                queued += 1
            elif status == DLStatus.PAUSED:
                paused += 1
            elif status == DLStatus.COMPLETED:
                completed += 1
            elif status == DLStatus.ERROR:
                errors += 1
            if status in (DLStatus.DOWNLOADING, DLStatus.QUEUED, DLStatus.PAUSED):
                if current_total > 0:
                    remaining_known += max(0, current_total - current_downloaded)
                else:
                    unknown_remaining = True

        session_lock = getattr(self, "_session_transfer_lock", None)
        if session_lock is not None:
            with session_lock:
                session_transferred = max(0, int(getattr(self, "_session_transferred_total", 0) or 0))
        else:
            # Compatibility for isolated tests or instances created without session counters.
            session_transferred = sum(max(0, int(getattr(d, "session_transferred", 0) or 0))
                                      for d in tuple(self.downloads))

        if active:
            state, state_color = "Descarcă", ACCENT()
        elif paused:
            state, state_color = "În pauză", WARNING()
        elif queued:
            state, state_color = "În coadă", TEXT_SEC()
        elif errors and not completed:
            state, state_color = "Erori", ERROR()
        elif completed and completed == len(self.downloads):
            state, state_color = "Finalizat", SUCCESS()
        else:
            state, state_color = "Pregătit", TEXT_SEC()
        self.status_state_lbl.configure(text=state, text_color=state_color)
        self.status_speed_lbl.configure(text=f"↓ {self._format_status_speed(speed, self._status_speed_unit)}",
                                        text_color=ACCENT() if active else TEXT_SEC())
        self.status_counts_lbl.configure(text=f"{active} active · {queued} coadă · {paused} pauză")
        self.status_transferred_lbl.configure(text=f"Sesiune {fmt_size(session_transferred) if session_transferred else '0 B'}")
        eta_seconds = (remaining_known / speed) if speed > 0 and not unknown_remaining else None
        self.status_eta_lbl.configure(text=f"ETA {fmt_duration(eta_seconds)}")

        animations_enabled = bool(self.config.get("animations", True))
        now = time.monotonic()
        if active and animations_enabled:
            if now - self._status_last_animation_at >= 0.45:
                self._status_animation_phase = (self._status_animation_phase + 1) % 2
                self._status_last_animation_at = now
            dot_color = ACCENT() if self._status_animation_phase else shade_color(ACCENT(), 1.18)
        elif active:
            dot_color = ACCENT()
        elif paused:
            dot_color = WARNING()
        elif errors:
            dot_color = ERROR()
        else:
            dot_color = TEXT_DIM()
        self.status_dot_canvas.itemconfigure(self.status_dot_id, fill=dot_color)
        if hasattr(self, "_global_speed_history"):
            # Mostenirea la 10 Hz (la fiecare ciclu de UI) umplea lista cu 600 de
            # eșantioane pe minut și refărea lista completă de fiecare dată.
            # Un eșantion pe secundă este suficient pentru grafic și e de 10x mai ieftin.
            now_sample = time.time()
            if (not self._global_speed_history
                    or now_sample - self._global_speed_history[-1][0] >= 1.0):
                self._global_speed_history.append((now_sample, speed))
                self._global_speed_history = [(t, v) for t, v in self._global_speed_history
                                              if t >= now_sample - 60]
        if getattr(self, "_current_tab", "") == "Networks":
            try: self._refresh_network_metrics()
            except Exception: pass

        if update_disk or now - self._status_last_disk_update >= 8.0:
            self._status_last_disk_update = now
            self._status_last_free_bytes = self._read_free_disk_space()
        disk_text = ("Spațiu liber —" if self._status_last_free_bytes is None
                     else f"{fmt_size(self._status_last_free_bytes)} liberi")
        self.status_disk_lbl.configure(text=disk_text)

    def _tick_status_bar(self):
        self._status_tick_id = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        try:
            self._update_status_bar()
        except Exception:
            logger.debug("Status bar refresh failed", exc_info=True)
        try:
            self._maybe_refresh_dynamic_sort()
        except Exception:
            logger.debug("Dynamic sort refresh failed", exc_info=True)
        if not getattr(self, "_closing", False) and self.winfo_exists():
            self._status_tick_id = self.after(650, self._tick_status_bar)

    def _update_status_bar_theme(self):
        if not hasattr(self, "statusbar") or not self.statusbar.winfo_exists():
            return
        try:
            self.statusbar_separator.configure(fg_color=BORDER())
            self.status_dot_canvas.configure(bg=BG_NAV())
            for divider in self._status_dividers:
                divider.configure(fg_color=BORDER())
            self.status_disk_lbl.configure(text_color=TEXT_SEC())
            self.status_state_lbl.configure(text_color=TEXT_SEC())
            self.status_speed_lbl.configure(text_color=ACCENT())
            self.status_counts_lbl.configure(text_color=TEXT_SEC())
            self.status_transferred_lbl.configure(text_color=TEXT_DIM())
            self.status_unit_btn.configure(fg_color=BG_CARD(), hover_color=BG_CARD_HOV(),
                                           text_color=ACCENT())
            self._update_slow_mode_button()
            if hasattr(self, "_nav_indicator") and self._nav_indicator.winfo_exists():
                self._nav_indicator.configure(fg_color=ACCENT())
            self._update_status_bar(update_disk=False)
        except Exception:
            logger.debug("Could not refresh status bar theme", exc_info=True)

    def _build_downloads_tab(self):
        frame = ThemedFrame(self.content, fg_color_key='transparent')
        self.downloads_list_view = ThemedFrame(frame, fg_color_key='transparent')
        self.downloads_list_view.pack(fill="both", expand=True)
        tb = ThemedFrame(self.downloads_list_view, fg_color_key='transparent')
        tb.pack(fill="x", padx=20, pady=(16, 6))
        def btn(text, cmd, prim=False, w=130):
            return ctk.CTkButton(tb, text=text, width=w, height=38,
                                fg_color=ACCENT() if prim else BG_CARD(),
                                hover_color=shade_color(ACCENT(), 0.82) if prim else BG_CARD_HOV(),
                                text_color=TEXT_PRI() if prim else TEXT_SEC(), corner_radius=8,
                                font=ctk.CTkFont(size=13), command=cmd)
        btn("＋  Adaugă", self._open_add_dialog, prim=True).pack(side="left")
        btn("📋  Lipește", self._paste_and_add, w=110).pack(side="left", padx=6)
        ctk.CTkFrame(tb, width=1, height=32, fg_color=BORDER()).pack(side="left", padx=6)
        btn("⏸  Pauză", self._pause_all, w=100).pack(side="left", padx=3)
        btn("▶  Reluare", self._resume_all, w=100).pack(side="left", padx=3)
        btn("⏹  Stop", self._stop_all, w=90).pack(side="left", padx=3)
        ctk.CTkFrame(tb, width=1, height=32, fg_color=BORDER()).pack(side="left", padx=6)
        btn("🗑  Curăță", self._clear_done, w=90).pack(side="left", padx=3)
        ctk.CTkOptionMenu(tb, variable=self._status_filter,
                          values=["Toate", "Active", "În coadă", "Completate", "Erori"],
                          width=116, height=34, fg_color=BG_CARD(), button_color=BG_CARD_HOV(),
                          button_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                          command=lambda _value: self._filter_cards()).pack(side="right", padx=(4, 2))
        ctk.CTkOptionMenu(tb, variable=self._sort_order,
                          values=["Cele mai noi", "Cele mai vechi", "Nume A–Z",
                                  "Viteză ↓", "Dimensiune ↓", "ETA ↑"],
                          width=128, height=34, fg_color=BG_CARD(), button_color=BG_CARD_HOV(),
                          button_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                          command=lambda _value: self._filter_cards()).pack(side="right", padx=(2, 4))
        self.stats_row = ThemedFrame(self.downloads_list_view, fg_color_key='transparent')
        self.stats_row.pack(fill="x", padx=22, pady=(0, 6))
        self.stats_lbl = ctk.CTkLabel(self.stats_row, text="", font=ctk.CTkFont(size=11), text_color=TEXT_DIM())
        self.stats_lbl.pack(side="left", anchor="w")
        self.delete_selected_btn = ctk.CTkButton(
            self.stats_row, text="🗑 Selectate (0)", width=130, height=28,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_SEC(),
            state="disabled", command=self._delete_selected_downloads)
        self.delete_selected_btn.pack(side="right", padx=(4, 0))
        self.bulk_action_buttons = {}
        self.bulk_action_frame = ctk.CTkFrame(self.stats_row, fg_color="transparent")
        self.bulk_action_frame.pack(side="right", padx=(0, 4))
        for action, label, callback in (
                ("pause", "⏸", lambda: self._bulk_selected("pause")),
                ("resume", "▶", lambda: self._bulk_selected("resume")),
                ("retry", "↻", lambda: self._bulk_selected("retry")),
                ("cancel", "⏹", lambda: self._bulk_selected("cancel"))):
            button = ctk.CTkButton(self.bulk_action_frame, text=label, width=30, height=27,
                fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_SEC(),
                state="disabled", command=callback)
            button.pack(side="left", padx=2)
            self.bulk_action_buttons[action] = button
        self.scroll = ThemedScrollableFrame(self.downloads_list_view, fg_color_key='transparent',
                                            scrollbar_button_color=BORDER(), scrollbar_button_hover_color=TEXT_DIM())
        self.scroll.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.empty_lbl = ctk.CTkLabel(self.scroll, text="Nicio descărcare.\nApasă ＋ Adaugă sau 📋 Lipește pentru a începe.",
                                     font=ctk.CTkFont(size=14), text_color=TEXT_DIM(), justify="center")
        self.empty_lbl.pack(expand=True, pady=80)
        self.no_match_lbl = ctk.CTkLabel(self.scroll, text="Nicio descărcare nu corespunde filtrelor.",
                                         font=ctk.CTkFont(size=13), text_color=TEXT_DIM())
        return frame

    def _build_queue_tab(self):
        frame = ThemedScrollableFrame(self.content, fg_color_key="transparent",
                                      scrollbar_button_color=BORDER(),
                                      scrollbar_button_hover_color=TEXT_DIM())
        ctk.CTkLabel(frame, text="Coada descărcări", font=ctk.CTkFont(size=18, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="w", padx=20, pady=(20, 4))
        self.queue_info = ctk.CTkLabel(frame, text="", font=ctk.CTkFont(size=13), text_color=TEXT_SEC())
        self.queue_info.pack(anchor="w", padx=20, pady=(0, 10))
        self.queue_table = ctk.CTkFrame(frame, fg_color="transparent")
        self.queue_table.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._refresh_queue()
        return frame

    def _build_history_tab(self):
        # Header and controls stay fixed; only the results list scrolls.
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        header_frame = ctk.CTkFrame(frame, fg_color="transparent")
        header_frame.pack(fill="x", padx=28, pady=(18, 12))
        ctk.CTkLabel(header_frame, text="🕐 Istoric Descărcări",
                    font=ctk.CTkFont(size=24, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="w")

        stats_frame = ctk.CTkFrame(frame, fg_color="transparent")
        stats_frame.pack(fill="x", padx=28, pady=(0, 14))
        stats_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.stat_total_card = self._create_stat_card(stats_frame, "Total Fișiere", "0", 0, "#3b82f6")
        self.stat_total_card.grid(row=0, column=0, padx=(0, 12), sticky="ew")
        self.stat_size_card = self._create_stat_card(stats_frame, "Dimensiune Totală", "0 GB", 0, "#8b5cf6")
        self.stat_size_card.grid(row=0, column=1, padx=(0, 12), sticky="ew")
        self.stat_month_card = self._create_stat_card(stats_frame, "Luna Aceasta", "0", 0, "#22c55e")
        self.stat_month_card.grid(row=0, column=2, padx=(0, 12), sticky="ew")
        self.stat_errors_card = self._create_stat_card(stats_frame, "Erori Recente", "0", 0, "#ef4444")
        self.stat_errors_card.grid(row=0, column=3, sticky="ew")

        filter_frame = ctk.CTkFrame(frame, fg_color="transparent")
        filter_frame.pack(fill="x", padx=28, pady=(0, 10))
        self.history_search = ctk.CTkEntry(
            filter_frame, placeholder_text="🔍 Caută în istoric...",
            width=280, height=36, fg_color=BG_INPUT(), border_color=BORDER(), text_color=TEXT_PRI())
        self.history_search.pack(side="left")
        self.history_search.bind("<KeyRelease>", lambda _event: self._schedule_history_filter())

        self.history_filter_var = ctk.StringVar(value="toate")
        filter_menu = ctk.CTkOptionMenu(
            filter_frame, values=["toate", "completate", "eșuate", "anulate"],
            variable=self.history_filter_var, command=lambda _value: self._filter_history(),
            fg_color=BG_INPUT(), text_color=TEXT_PRI(), width=150, height=36)
        filter_menu.pack(side="left", padx=(8, 12))

        self.history_clear_btn = ctk.CTkButton(
            filter_frame, text="🗑 Șterge", width=100, height=36,
            fg_color=ERROR(), hover_color="#dc2626", command=self._clear_history)
        self.history_clear_btn.pack(side="left", padx=(0, 8))
        self.history_export_btn = ctk.CTkButton(
            filter_frame, text="📄 Export CSV", width=112, height=36,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
            command=self._export_history)
        self.history_export_btn.pack(side="left")

        # Advanced filters occupy their own row so the controls do not get cramped.
        advanced_filter_frame = ctk.CTkFrame(frame, fg_color="transparent")
        advanced_filter_frame.pack(fill="x", padx=28, pady=(0, 10))
        ctk.CTkLabel(advanced_filter_frame, text="Perioadă", text_color=TEXT_DIM(),
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 5))
        self.history_period_var = ctk.StringVar(value=self.db.get_setting("history_period_filter", "Toate"))
        self.history_period_menu = ctk.CTkOptionMenu(
            advanced_filter_frame, values=["Toate", "Astăzi", "Ultimele 7 zile", "Ultimele 30 zile"],
            variable=self.history_period_var, command=lambda _value: self._filter_history(),
            width=142, height=30, fg_color=BG_INPUT(), button_color=BG_INPUT(),
            button_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
            dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI())
        self.history_period_menu.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(advanced_filter_frame, text="Tip", text_color=TEXT_DIM(),
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 5))
        self.history_type_var = ctk.StringVar(value=self.db.get_setting("history_type_filter", "Toate"))
        self.history_type_menu = ctk.CTkOptionMenu(
            advanced_filter_frame, values=["Toate", "Video", "Audio", "Imagini", "Arhive", "Documente", "Altele"],
            variable=self.history_type_var, command=lambda _value: self._filter_history(),
            width=120, height=30, fg_color=BG_INPUT(), button_color=BG_INPUT(),
            button_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
            dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI())
        self.history_type_menu.pack(side="left", padx=(0, 14))
        ctk.CTkLabel(advanced_filter_frame, text="Sortare", text_color=TEXT_DIM(),
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 5))
        self.history_sort_var = ctk.StringVar(value=self.db.get_setting("history_sort_order", "Cele mai noi"))
        self.history_sort_menu = ctk.CTkOptionMenu(
            advanced_filter_frame, values=["Cele mai noi", "Cele mai vechi", "Nume A–Z", "Dimensiune ↓", "Status"],
            variable=self.history_sort_var, command=lambda _value: self._filter_history(),
            width=145, height=30, fg_color=BG_INPUT(), button_color=BG_INPUT(),
            button_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
            dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI())
        self.history_sort_menu.pack(side="left")

        self.selected_history_ids = set()
        self.history_cards = {}
        self._history_page_size = safe_int(self.db.get_setting("history_page_size", 25), 25, 25, 100)
        if self._history_page_size not in (25, 50, 100):
            self._history_page_size = 25
        self._history_page_index = 0
        self._history_total_count = 0
        self._history_filter_after_id = None
        self._history_render_after_id = None
        self._history_render_generation = 0
        self._history_render_entries = []
        self._history_render_index = 0
        self._history_query_generation = 0
        self._history_query_queue = queue.Queue()
        self._history_query_inflight = 0
        self._history_query_poll_id = None
        self._history_export_queue = queue.Queue()
        self._history_export_active = False
        self._history_export_token = 0
        self._history_export_poll_id = None
        self._history_export_cancel_event = None
        self._history_export_dialog = None
        self._history_mutation_active = False

        self.delete_selected_history_btn = ctk.CTkButton(
            filter_frame, text="🗑 Selectate (0)", width=130, height=36,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_SEC(),
            state="disabled", command=self._delete_selected_history)
        self.delete_selected_history_btn.pack(side="left", padx=(8, 0))

        # Pagination remains visible even while the user scrolls through result cards.
        pagination = ctk.CTkFrame(frame, fg_color="transparent")
        pagination.pack(side="bottom", fill="x", padx=28, pady=(8, 14))
        self.history_page_summary = ctk.CTkLabel(
            pagination, text="0 rezultate", text_color=TEXT_SEC(), font=ctk.CTkFont(size=11))
        self.history_page_summary.pack(side="left", padx=(0, 12))
        ctk.CTkLabel(pagination, text="Pe pagină", text_color=TEXT_DIM(),
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 5))
        self.history_page_size_var = ctk.StringVar(value=str(self._history_page_size))
        self.history_page_size_menu = ctk.CTkOptionMenu(
            pagination, values=["25", "50", "100"], variable=self.history_page_size_var,
            command=self._on_history_page_size_change, width=72, height=28,
            fg_color=BG_INPUT(), button_color=BG_INPUT(), button_hover_color=BG_CARD_HOV(),
            text_color=TEXT_PRI(), dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI())
        self.history_page_size_menu.pack(side="left", padx=(0, 12))

        nav_style = dict(width=64, height=28, corner_radius=7, fg_color=BG_INPUT(),
                         hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(), font=ctk.CTkFont(size=11))
        # Pack right-to-left so the visual order is: Prima, Anterioară, Următoarea, Ultima.
        self.history_last_btn = ctk.CTkButton(pagination, text="Ultima »", command=self._history_go_to_last_page, **nav_style)
        self.history_last_btn.pack(side="right", padx=(4, 0))
        self.history_next_btn = ctk.CTkButton(pagination, text="Următoarea ›", width=100,
                                              command=lambda: self._history_go_to_page(self._history_page_index + 1), **{k:v for k,v in nav_style.items() if k != "width"})
        self.history_next_btn.pack(side="right", padx=(4, 0))
        self.history_prev_btn = ctk.CTkButton(pagination, text="‹ Anterioară", width=100,
                                              command=lambda: self._history_go_to_page(self._history_page_index - 1), **{k:v for k,v in nav_style.items() if k != "width"})
        self.history_prev_btn.pack(side="right", padx=(4, 0))
        self.history_first_btn = ctk.CTkButton(pagination, text="« Prima", command=lambda: self._history_go_to_page(0), **nav_style)
        self.history_first_btn.pack(side="right", padx=(4, 0))

        self.history_scroll = ThemedScrollableFrame(
            frame, fg_color_key="transparent", scrollbar_button_color=BORDER(),
            scrollbar_button_hover_color=TEXT_DIM())
        self.history_scroll.pack(fill="both", expand=True, padx=28, pady=(0, 0))
        self.history_table = ThemedFrame(self.history_scroll, fg_color_key="transparent")
        self.history_table.pack(fill="x", expand=False)

        self._refresh_history()
        return frame

    def _create_stat_card(self, parent, title, value, count, color):
        card = ctk.CTkFrame(parent, fg_color=BG_CARD(), corner_radius=14,
                           border_width=1, border_color=BORDER())

        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=12),
                    text_color=TEXT_SEC()).pack(anchor="w", padx=20, pady=(18, 6))

        value_lbl = ctk.CTkLabel(card, text=value,
                                font=ctk.CTkFont(size=28, weight="bold"),
                                text_color=color)
        value_lbl.pack(anchor="w", padx=20, pady=(0, 16))

        return card

    def _build_networks_tab(self):
        frame = ThemedScrollableFrame(self.content, fg_color_key="transparent",
                                      scrollbar_button_color=BORDER(),
                                      scrollbar_button_hover_color=TEXT_DIM())
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 8))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(title_box, text="Network Dashboard", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=TEXT_PRI()).pack(anchor="w")
        ctk.CTkLabel(title_box,
                     text="Fluxuri HTTP distribuite pe adrese IPv4 locale. Agregarea reală cere conexiuni independente.",
                     font=ctk.CTkFont(size=10), text_color=TEXT_DIM(), wraplength=620,
                     justify="left").pack(anchor="w", pady=(2, 0))
        ctk.CTkButton(header, text="↻ Detectează", width=118, height=34,
                      fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                      command=self._refresh_network_adapters).pack(side="right", padx=(8, 0))

        controls = ThemedFrame(frame, fg_color_key="bg_card", corner_radius=10,
                               border_width=1, border_color=BORDER())
        controls.pack(fill="x", padx=22, pady=(0, 10))
        self.multi_network_var = ctk.BooleanVar(value=bool(self.config.get("multi_network_enabled", False)))
        ctk.CTkLabel(controls, text="Multi-Network Engine", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=14, pady=12)
        self.multi_network_switch = ctk.CTkSwitch(
            controls, text="Folosește adaptoarele selectate", variable=self.multi_network_var,
            progress_color=ACCENT(), command=self._toggle_multi_network)
        self.multi_network_switch.pack(side="left", padx=(8, 14), pady=12)
        self.slow_mode_var = ctk.BooleanVar(value=bool(self.config.get("slow_mode_enabled", False)))
        self.slow_mode_switch = ctk.CTkSwitch(
            controls, text="Slow mode", variable=self.slow_mode_var,
            progress_color=ACCENT(), command=self._toggle_slow_mode)
        self.slow_mode_switch.pack(side="right", padx=14, pady=12)

        graph_card = ThemedFrame(frame, fg_color_key="bg_card", corner_radius=10,
                                 border_width=1, border_color=BORDER())
        graph_card.pack(fill="x", padx=22, pady=(0, 10))
        head = ctk.CTkFrame(graph_card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(head, text="THROUGHPUT · ULTIMELE 60 SECUNDE", text_color=TEXT_DIM(),
                     font=ctk.CTkFont(size=10, weight="bold")).pack(side="left")
        self.network_total_speed_label = ctk.CTkLabel(head, text="↓ 0 KB/s", text_color=ACCENT(),
                     font=ctk.CTkFont(size=12, weight="bold"))
        self.network_total_speed_label.pack(side="right")
        self.network_graph_canvas = tk.Canvas(graph_card, height=116, bg=BG_CARD(), highlightthickness=0)
        self.network_graph_canvas.pack(fill="x", padx=12, pady=(4, 12))
        self.network_graph_canvas.bind("<Configure>", lambda _event: self._draw_network_dashboard())

        hint = ctk.CTkLabel(frame,
            text="Sfat: Wi-Fi și Ethernet pe același router împart de regulă același abonament. Selectează adaptoare conectate la surse de internet independente pentru a putea obține un debit agregat.",
            text_color=TEXT_SEC(), font=ctk.CTkFont(size=11), wraplength=800, justify="left")
        hint.pack(anchor="w", padx=24, pady=(0, 8))
        self.network_profile_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self.network_profile_frame.pack(fill="x", padx=22, pady=(0, 18))
        self.network_profile_hint = ctk.CTkLabel(self.network_profile_frame,
            text="Se detectează adaptoarele de rețea…", text_color=TEXT_DIM(),
            font=ctk.CTkFont(size=12))
        self.network_profile_hint.pack(anchor="w", pady=18)
        self._network_widgets = {}
        self._network_speed_history = []
        return frame

    def _refresh_network_adapters(self):
        if hasattr(self, "network_profile_hint") and self.network_profile_hint.winfo_exists():
            self.network_profile_hint.configure(text="Se detectează adaptoarele de rețea…")
        self.network_manager.refresh_async(lambda: self.post_ui(self._render_network_cards))

    def _toggle_multi_network(self):
        enabled = bool(self.multi_network_var.get())
        self.network_manager.set_multi_enabled(enabled)
        if enabled and not any(item["selected"] and item["available"] for item in self.network_manager.snapshot()):
            self.network_profile_hint.configure(
                text="Activează cel puțin un adaptor din listă. Pentru agregare reală sunt necesare conexiuni independente.")
        self._render_network_cards()
        self._update_status_bar()

    def _toggle_slow_mode(self):
        enabled = bool(self.slow_mode_var.get())
        self.config.set("slow_mode_enabled", enabled)
        self._update_slow_mode_button()
        self._update_status_bar()

    def _update_slow_mode_button(self):
        button = getattr(self, "slow_mode_btn", None)
        if button is None or not button.winfo_exists():
            return
        enabled = bool(self.config.get("slow_mode_enabled", False))
        button.configure(text="Slow mode ON" if enabled else "Slow mode OFF",
                         fg_color=WARNING() if enabled else BG_CARD(),
                         hover_color=shade_color(WARNING(), .88) if enabled else BG_CARD_HOV(),
                         text_color="#111111" if enabled else TEXT_SEC())
        if hasattr(self, "slow_mode_var"):
            try: self.slow_mode_var.set(enabled)
            except Exception: pass

    def _render_network_cards(self):
        if getattr(self, "_closing", False) or not hasattr(self, "network_profile_frame"):
            return
        try:
            for widget in self.network_profile_frame.winfo_children():
                widget.destroy()
            self._network_widgets = {}
            profiles = self.network_manager.snapshot()
            if not profiles:
                ctk.CTkLabel(self.network_profile_frame,
                    text="Nu au fost detectate adrese IPv4 locale. Transferurile standard rămân disponibile.",
                    text_color=TEXT_DIM(), font=ctk.CTkFont(size=12)).pack(anchor="w", pady=18)
                return
            selected_keys = {p["key"] for p in profiles if p["selected"]}
            for profile in profiles:
                row = ThemedFrame(self.network_profile_frame, fg_color_key="bg_card", corner_radius=9,
                                  border_width=1, border_color=BORDER())
                row.pack(fill="x", pady=4)
                row.grid_columnconfigure(1, weight=1)
                var = ctk.BooleanVar(value=profile["selected"])
                check = ctk.CTkCheckBox(row, text="", variable=var, width=24,
                    state="normal" if profile["available"] else "disabled",
                    command=lambda key=profile["key"], value=var: self._on_network_selection_changed(key, value.get()))
                check.grid(row=0, column=0, padx=(12, 4), pady=10)
                details = ctk.CTkFrame(row, fg_color="transparent")
                details.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
                state = "ONLINE" if profile["available"] else "OFFLINE"
                ctk.CTkLabel(details, text=f"{profile['name']}  ·  {state}",
                    text_color=TEXT_PRI() if profile["available"] else TEXT_DIM(),
                    font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(anchor="w")
                ctk.CTkLabel(details, text=profile["ip"], text_color=TEXT_DIM(),
                    font=ctk.CTkFont(size=10), anchor="w").pack(anchor="w", pady=(1, 0))
                metrics = ctk.CTkFrame(row, fg_color="transparent")
                metrics.grid(row=0, column=2, padx=8, pady=6)
                speed_lbl = ctk.CTkLabel(metrics, text=fmt_speed(profile["speed_bps"]),
                    text_color=ACCENT(), font=ctk.CTkFont(size=11, weight="bold"), width=95, anchor="e")
                speed_lbl.pack(anchor="e")
                bytes_lbl = ctk.CTkLabel(metrics, text=f"{fmt_size(profile['total_bytes'])} total",
                    text_color=TEXT_SEC(), font=ctk.CTkFont(size=9), width=95, anchor="e")
                bytes_lbl.pack(anchor="e")
                limit_box = ctk.CTkFrame(row, fg_color="transparent")
                limit_box.grid(row=0, column=3, padx=(2, 12), pady=6)
                limit_entry = ctk.CTkEntry(limit_box, width=72, height=28, fg_color=BG_INPUT(),
                    border_color=BORDER(), text_color=TEXT_PRI(), justify="right",
                    placeholder_text="0 = ∞")
                limit_value = profile["limit_bps"]
                limit_entry.insert(0, str((limit_value + 1023) // 1024 if limit_value else 0))
                limit_entry.pack(side="left", padx=(0, 4))
                ctk.CTkButton(limit_box, text="KB/s", width=48, height=28,
                    fg_color=BG_INPUT(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                    command=lambda key=profile["key"], entry=limit_entry: self._save_network_limit(key, entry)).pack(side="left")
                self._network_widgets[profile["key"]] = {"speed": speed_lbl, "bytes": bytes_lbl,
                                                            "var": var, "limit": limit_entry}
            self.network_profile_hint = ctk.CTkLabel(self.network_profile_frame,
                text=(f"{sum(1 for p in profiles if p['available'])} adaptoare cu IPv4 · "
                      f"{len(selected_keys)} selectate · {('Multi-Network activ' if self.network_manager.multi_enabled() else 'mod sistem implicit')}."),
                text_color=TEXT_DIM(), font=ctk.CTkFont(size=10))
            self.network_profile_hint.pack(anchor="w", pady=(8, 2))
            self._update_slow_mode_button()
            self._draw_network_dashboard()
        except Exception:
            logger.exception("Nu s-a putut actualiza Network Dashboard")

    def _on_network_selection_changed(self, key, enabled):
        selected = {p["key"] for p in self.network_manager.snapshot() if p["selected"]}
        if enabled:
            selected.add(key)
        else:
            selected.discard(key)
        self.network_manager.set_selected(selected)
        # Reconstruirea panelului se amână: distrugerea checkbox-ului care rulează
        # chiar acest callback provoacă "invalid command name" în Tk.
        self.after_idle(self._render_network_cards)

    def _save_network_limit(self, key, entry):
        try:
            kbps = max(0, int(entry.get() or "0"))
            self.network_manager.set_limit(key, kbps * 1024)
            messagebox.showinfo("Network Dashboard", "Limita adaptorului a fost salvată.", parent=self)
        except ValueError:
            messagebox.showerror("Valoare invalidă", "Introdu o limită în KB/s (0 = nelimitat).", parent=self)

    def _network_monitor_tick(self):
        self._network_monitor_id = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        if self.network_manager.multi_enabled():
            self.network_manager.refresh_async(lambda: self.post_ui(self._render_network_cards))
        if getattr(self, "_current_tab", "") == "Networks":
            self._refresh_network_metrics()
        self._network_monitor_id = self.after(20000, self._network_monitor_tick)

    def _refresh_network_metrics(self):
        if not hasattr(self, "_network_widgets"):
            return
        profiles = {p["key"]: p for p in self.network_manager.snapshot()}
        for key, widgets in list(self._network_widgets.items()):
            profile = profiles.get(key)
            if not profile:
                continue
            try:
                widgets["speed"].configure(text=fmt_speed(profile["speed_bps"]))
                widgets["bytes"].configure(text=f"{fmt_size(profile['total_bytes'])} total")
            except tk.TclError:
                pass
        total_speed = sum(p["speed_bps"] for p in profiles.values() if p["available"])
        self._network_speed_history.append((time.time(), total_speed))
        self._network_speed_history = [(t, v) for t, v in self._network_speed_history if t >= time.time() - 60]
        if hasattr(self, "network_total_speed_label") and self.network_total_speed_label.winfo_exists():
            self.network_total_speed_label.configure(text=f"↓ {fmt_speed(total_speed)}")
        self._draw_network_dashboard()

    def _draw_network_dashboard(self):
        canvas = getattr(self, "network_graph_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        try:
            canvas.delete("all")
            width, height = max(120, canvas.winfo_width()), max(90, canvas.winfo_height())
            left, right, top, bottom = 54, width - 12, 10, height - 24
            history = list(getattr(self, "_network_speed_history", []))
            if not history:
                speed = sum(p["speed_bps"] for p in self.network_manager.snapshot() if p["available"])
                history = [(time.time() - 1, 0), (time.time(), speed)]
            values = [sample[1] for sample in history]
            maximum = max(values or [0]) or 1
            for frac, label in ((0.0, "0"), (0.5, fmt_speed(maximum / 2)), (1.0, fmt_speed(maximum))):
                y = bottom - frac * (bottom - top)
                canvas.create_line(left, y, right, y, fill=BORDER())
                canvas.create_text(left - 6, y, text=label, fill=TEXT_DIM(), anchor="e", font=("Consolas", 8))
            if len(history) >= 2:
                coords = []
                for index, (_t, value) in enumerate(history):
                    x = left + index * (right - left) / (len(history) - 1)
                    y = bottom - value / maximum * (bottom - top)
                    coords.extend((x, y))
                canvas.create_polygon([left, bottom, *coords, right, bottom],
                    fill=shade_color(ACCENT(), .6), outline="", stipple="gray50")
                canvas.create_line(*coords, fill=ACCENT(), width=2, smooth=True)
            canvas.create_text(left, height - 7, text="60s", fill=TEXT_DIM(), anchor="w", font=("Consolas", 8))
            canvas.create_text(right, height - 7, text="acum", fill=TEXT_DIM(), anchor="e", font=("Consolas", 8))
        except Exception:
            logger.debug("Network graph redraw failed", exc_info=True)

    def _build_scheduled_tab(self):
        frame = ThemedScrollableFrame(self.content, fg_color_key="transparent",
                                      scrollbar_button_color=BORDER(),
                                      scrollbar_button_hover_color=TEXT_DIM())
        ctk.CTkLabel(frame, text="Descărcări programate", font=ctk.CTkFont(size=18, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="w", padx=20, pady=(20, 4))
        self.scheduled_table = ctk.CTkFrame(frame, fg_color="transparent")
        self.scheduled_table.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._refresh_scheduled()
        return frame

    def _build_settings_tab(self):
        frame = ThemedScrollableFrame(self.content, fg_color_key='transparent')
        ctk.CTkLabel(frame, text="Setări", font=ctk.CTkFont(size=18, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="w", padx=20, pady=(20, 4))
        card = ThemedFrame(frame, fg_color_key='bg_card', corner_radius=10, border_width=1, border_color=BORDER())
        card.pack(fill="x", padx=20, pady=16)
        def row(label, default, unit=""):
            f = ThemedFrame(card, fg_color_key='transparent')
            f.pack(fill="x", padx=16, pady=10)
            ctk.CTkLabel(f, text=label, text_color=TEXT_PRI(), font=ctk.CTkFont(size=13)).pack(side="left")
            if unit:
                ctk.CTkLabel(f, text=unit, text_color=TEXT_DIM(), font=ctk.CTkFont(size=11)).pack(side="right", padx=(4, 0))
            e = ctk.CTkEntry(f, width=100, fg_color=BG_INPUT(), border_color=BORDER(),
                            text_color=TEXT_PRI(), justify="right")
            e.insert(0, str(default))
            e.pack(side="right")
            return e
        self.max_dl_e = row("Descărcări simultane max.", self.config.get("max_concurrent", 3))
        profile_frame = ThemedFrame(card, fg_color_key="transparent")
        profile_frame.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(profile_frame, text="Profil coadă", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.queue_profile_var = ctk.StringVar(value=self.config.get("queue_profile", "Balanced"))
        self.queue_profile_control = ctk.CTkSegmentedButton(
            profile_frame, values=["Eco", "Balanced", "Performance", "Custom"],
            variable=self.queue_profile_var, command=self._apply_queue_profile,
            selected_color=ACCENT(), selected_hover_color=shade_color(ACCENT(), .82),
            unselected_color=BG_INPUT(), unselected_hover_color=BG_CARD_HOV(),
            text_color=TEXT_PRI(), width=310, height=30)
        self.queue_profile_control.pack(side="right")
        stored_limit = max(0, safe_int(self.config.get("speed_limit", 0), 0))
        displayed_limit = (stored_limit + 1023) // 1024 if stored_limit > 0 else 0
        self.speed_e = row("Limită viteză", str(displayed_limit), "KB/s (0 = nelimitat)")
        self.segments_e = row("Segmente default", self.config.get("segments_default", 1))
        ctk.CTkFrame(card, height=1, fg_color=BORDER()).pack(fill="x", padx=16)
        theme_frame = ThemedFrame(card, fg_color_key='transparent')
        theme_frame.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(theme_frame, text="Aspect", text_color=TEXT_PRI(), font=ctk.CTkFont(size=13)).pack(side="left")
        self.theme_var = ctk.StringVar(value=self.config.get("theme_name", "Dark Modern"))
        initial_mode = self.config.get("theme_mode") or self._theme_family(self.theme_var.get())
        self.theme_mode_var = ctk.StringVar(value=str(initial_mode).title())
        self.theme_mode_control = ctk.CTkSegmentedButton(
            theme_frame, values=["Auto", "Light", "Dark"], variable=self.theme_mode_var,
            command=self._on_theme_mode_changed, selected_color=ACCENT(),
            selected_hover_color=shade_color(ACCENT(), 0.82),
            unselected_color=BG_INPUT(), unselected_hover_color=BG_CARD_HOV(),
            text_color=TEXT_PRI(), height=32, width=250)
        self.theme_mode_control.pack(side="right")
        ctk.CTkLabel(card, text="Teme", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(5, 2))
        self.theme_gallery = ThemedFrame(card, fg_color_key='transparent')
        self.theme_gallery.pack(fill="x", padx=12, pady=(0, 12))
        self.theme_cards = {}
        for index, name in enumerate(THEME.get_theme_names()):
            preview = ThemePreviewCard(self.theme_gallery, name, self._on_theme_changed,
                                       selected=name == self.theme_var.get())
            preview.grid(row=index // 3, column=index % 3, padx=5, pady=5, sticky="nsew")
            self.theme_cards[name] = preview
        for column in range(3):
            self.theme_gallery.grid_columnconfigure(column, weight=1)
        ctk.CTkFrame(card, height=1, fg_color=BORDER()).pack(fill="x", padx=16)
        accent_label_frame = ThemedFrame(card, fg_color_key='transparent')
        accent_label_frame.pack(fill="x", padx=16, pady=(10, 5))
        ctk.CTkLabel(accent_label_frame, text="Culoare accent:", text_color=TEXT_PRI(),
                    font=ctk.CTkFont(size=13)).pack(side="left")
        self.accent_selector = AccentColorSelector(card, on_color_change=self._on_accent_changed_live,
                                                   current_color=self.config.get("accent_color", "#3b82f6"))
        self.accent_selector.pack(fill="x", padx=16, pady=(0, 10))
        self.anim_var = ctk.BooleanVar(value=self.config.get("animations", True))
        anim_frame = ThemedFrame(card, fg_color_key='transparent')
        anim_frame.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(anim_frame, text="Animații UI", text_color=TEXT_PRI(), font=ctk.CTkFont(size=13)).pack(side="left")
        anim_switch = ctk.CTkSwitch(anim_frame, text="", variable=self.anim_var, progress_color=ACCENT())
        anim_switch.pack(side="right")
        ctk.CTkFrame(card, height=1, fg_color=BORDER()).pack(fill="x", padx=16)
        def switch_row(label, default):
            f = ThemedFrame(card, fg_color_key='transparent')
            f.pack(fill="x", padx=16, pady=10)
            ctk.CTkLabel(f, text=label, text_color=TEXT_PRI(), font=ctk.CTkFont(size=13)).pack(side="left")
            var = ctk.BooleanVar(value=default)
            sw = ctk.CTkSwitch(f, text="", variable=var, progress_color=ACCENT())
            sw.pack(side="right")
            return var
        ctk.CTkFrame(card, height=1, fg_color=BORDER()).pack(fill="x", padx=16, pady=(4, 4))
        ctk.CTkLabel(card, text="Smart Automation", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(7, 0))
        self.smart_retry_var = switch_row(
            "Reîncercare automată pentru erori temporare",
            bool(self.config.get("smart_retry_enabled", True)))
        self.smart_queue_aging_var = switch_row(
            "Prioritate adaptivă pentru elementele care așteaptă",
            bool(self.config.get("smart_queue_aging_enabled", True)))
        self.smart_disk_guard_var = switch_row(
            "Protecție la spațiu liber redus (oprește pornirea de noi transferuri)",
            bool(self.config.get("smart_disk_guard_enabled", False)))
        self.smart_retry_attempts_e = row(
            "Reîncercări automate suplimentare", self.config.get("smart_retry_max_attempts", 2))
        self.smart_retry_delay_e = row(
            "Întârziere inițială retry", self.config.get("smart_retry_base_delay", 5), "sec")
        self.smart_queue_aging_e = row(
            "Promovare prioritate după", self.config.get("smart_queue_aging_minutes", 20), "minute")
        self.smart_disk_min_free_e = row(
            "Rezervă minimă de spațiu liber", self.config.get("smart_disk_min_free_mb", 512), "MB")
        ctk.CTkFrame(card, height=1, fg_color=BORDER()).pack(fill="x", padx=16, pady=(4, 4))
        ctk.CTkLabel(card, text="Plexo-Inspired Download Engine", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(7, 0))
        self.adaptive_streams_var = switch_row(
            "Streams adaptivi (concurența crește/scade automat)",
            bool(self.config.get("adaptive_streams_enabled", True)))
        self.endgame_var = switch_row(
            "Endgame Mode (duplică prudent blocurile care stagnează)",
            bool(self.config.get("endgame_enabled", True)))
        self.adaptive_stream_max_e = row(
            "Număr maxim de streams", self.config.get("adaptive_stream_max", 16))
        self.endgame_trigger_e = row(
            "Endgame după lipsă de progres", self.config.get("endgame_trigger_seconds", 4), "sec")
        self.slow_mode_limit_e = row(
            "Limita Slow Mode", self.config.get("slow_mode_limit_kbps", 512), "KB/s")
        proxy_frame = ThemedFrame(card, fg_color_key='transparent')
        proxy_frame.pack(fill="x", padx=16, pady=10)
        ctk.CTkLabel(proxy_frame, text="Proxy (HTTP/HTTPS/SOCKS5)", text_color=TEXT_PRI(),
                     font=ctk.CTkFont(size=13)).pack(side="left")
        self.proxy_e = ctk.CTkEntry(proxy_frame, width=300, fg_color=BG_INPUT(),
                                    border_color=BORDER(), text_color=TEXT_PRI(),
                                    placeholder_text="http://user:pass@host:port")
        self.proxy_e.insert(0, str(self.config.get("proxy_url", "") or ""))
        self.proxy_e.pack(side="right")
        ctk.CTkLabel(card, text="Gol = fără proxy. Pentru SOCKS5 e necesar PySocks (requests[socks]).",
                     font=ctk.CTkFont(size=10), text_color=TEXT_DIM()).pack(anchor="w", padx=16, pady=(0, 6))
        self.auto_sort_var = switch_row("Auto-sortare fișiere", self.config.get("auto_sort", True))
        self.clipboard_var = switch_row("Monitor clipboard", self.config.get("clipboard_monitor", True))
        self.av_scan_var = switch_row("Scanează fișierele cu Windows Defender la finalizare",
                                      self.config.get("av_scan", False))
        ctk.CTkLabel(card, text="Doar Windows • adaugă câteva secunde la fiecare descărcare completă",
                    font=ctk.CTkFont(size=10), text_color=TEXT_DIM()).pack(anchor="w", padx=16, pady=(0, 6))
        self.notify_var = switch_row("Notificări", self.config.get("notifications", True))
        self.tray_var = switch_row("Minimize în tray", self.config.get("minimize_to_tray", True))
        self.clear_exit_var = switch_row("Șterge completate la ieșire", self.config.get("clear_on_exit", False))
        settings_actions = ctk.CTkFrame(card, fg_color="transparent")
        settings_actions.pack(fill="x", padx=16, pady=(14, 14))
        ctk.CTkButton(settings_actions, text="Curăță fișierele temporare", fg_color=BG_CARD(), width=195,
                      hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                      command=self._cleanup_temp_files).pack(side="right", padx=(8, 0))
        ctk.CTkButton(settings_actions, text="Salvează setările", fg_color=ACCENT(), width=140,
                      command=self._save_settings).pack(side="right")
        about = ThemedFrame(frame, fg_color_key='bg_card', corner_radius=10, border_width=1, border_color=BORDER())
        about.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkLabel(about, text="Despre aplicație", font=ctk.CTkFont(size=14, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(about, text="CatsSoft Downloader v6.13", font=ctk.CTkFont(size=13),
                    text_color=TEXT_SEC()).pack(anchor="w", padx=16)
        ctk.CTkLabel(about, text="Python + CustomTkinter • Adaptive Streams • Network Dashboard • Recovery",
                    font=ctk.CTkFont(size=11), text_color=TEXT_DIM()).pack(anchor="w", padx=16, pady=(2, 14))
        return frame

    def _on_theme_changed(self, theme_name):
        self.theme_var.set(theme_name)
        # Alegerea unui preset este explicită și îi activează familia; Auto revine
        # la urmărirea sistemului când este selectat în controlul de aspect.
        self.theme_mode_var.set("Light" if self._theme_family(theme_name) == "light" else "Dark")
        self._set_effective_theme(theme_name)

    def _on_theme_mode_changed(self, mode):
        self.theme_mode_var.set(mode)
        preferred = self.theme_var.get()
        if mode != "Auto" and self._theme_family(preferred) != mode.lower():
            preferred = "Light Modern" if mode == "Light" else "Dark Modern"
            self.theme_var.set(preferred)
        self._set_effective_theme(preferred)

    def _on_accent_changed_live(self, hex_color: str):
        THEME.set_accent(hex_color, apply_immediately=True)
        self.after(100, lambda: self._on_theme_change())

    def _refresh_theme_controls(self):
        if getattr(self, "queue_profile_control", None) and self.queue_profile_control.winfo_exists():
            self.queue_profile_control.configure(selected_color=ACCENT(),
                selected_hover_color=shade_color(ACCENT(), 0.82), unselected_color=BG_INPUT(),
                unselected_hover_color=BG_CARD_HOV(), text_color=TEXT_PRI())
        if getattr(self, "theme_mode_control", None) and self.theme_mode_control.winfo_exists():
            self.theme_mode_control.configure(selected_color=ACCENT(),
                selected_hover_color=shade_color(ACCENT(), 0.82),
                unselected_color=BG_INPUT(), unselected_hover_color=BG_CARD_HOV(),
                text_color=TEXT_PRI())

    def _apply_preview_live(self):
        original_bg = self.cget("fg_color")
        self.configure(fg_color=BG_CARD())
        self.after(150, lambda: self.configure(fg_color=original_bg))
        for tab in self._tab_frames.values():
            self._refresh_tab_theme(tab)
        if hasattr(self, 'accent_selector'):
            self.accent_selector._update_selection()

    def _switch_tab(self, name: str):
        if name != "History":
            if hasattr(self, "_cancel_history_render"):
                self._cancel_history_render()
            pending = getattr(self, "_history_filter_after_id", None)
            if pending:
                try:
                    self.after_cancel(pending)
                except (tk.TclError, RuntimeError):
                    pass
                self._history_filter_after_id = None
        for n, f in self._tab_frames.items():
            f.pack_forget()
            if n in self._nav_btns:
                self._nav_btns[n].configure(text_color=TEXT_SEC(), fg_color="transparent")
        self._tab_frames[name].pack(fill="both", expand=True)
        if name in self._nav_btns:
            self._nav_btns[name].configure(text_color=TEXT_PRI(), fg_color=BG_CARD())
            self.after_idle(lambda selected=name: self._animate_nav_indicator(selected))
        self._current_tab = name
        if name == "Queue": self._refresh_queue()
        elif name == "History": self._refresh_history()
        elif name == "Networks":
            self._refresh_network_metrics()
            self._render_network_cards()
        elif name == "Scheduled": self._refresh_scheduled()

    def _setup_keyboard_shortcuts(self):
        self.bind('<Control-v>', lambda e: self._paste_and_add())
        self.bind('<Control-n>', lambda e: self._open_add_dialog())
        self.bind('<Control-q>', lambda e: self._exit_app())
        self.bind('<Control-p>', lambda e: self._pause_all())
        self.bind('<Control-r>', lambda e: self._resume_all())
        self.bind('<Control-w>', lambda e: self._clear_done())
        self.bind('<Escape>', lambda e: self.focus())

    def _open_downloads_folder(self):
        open_folder(self.default_folder)

    def _paste_and_add(self):
        try:
            clip = self.clipboard_get().strip()
        except tk.TclError:
            clip = ""
        AddURLDialog(self, self._add_download, prefill_url=clip if clip.startswith(("http://", "https://")) else "")

    def _open_add_dialog(self):
        AddURLDialog(self, self._add_download)

    def _add_download(self, url: str, folder: str, filename: str = None,
                      segments: int = 1, checksum: str = ""):
        """Adaugă o descărcare nouă și o așază într-o coadă persistentă, fără suprascriere."""
        details_were_open = False
        current_details = getattr(self, "details_view", None)
        if current_details is not None:
            try:
                details_were_open = current_details.winfo_exists()
            except Exception:
                details_were_open = False

        if not filename:
            filename = extract_file_name_from_url(url) or extract_filename_from_url(url)
            if not filename or filename.lower().endswith((".php", ".asp", ".aspx", ".cgi", ".dll", ".js")):
                filename = f"download_{int(time.time())}.tmp"
                logger.info("Using temporary filename until response headers are available: %s", filename)

        fname = sanitize_filename(filename)
        os.makedirs(folder, exist_ok=True)
        save_path = unique_target_path(os.path.join(folder, fname))
        self._queue_sequence += 1
        dl = Download(url=url, filename=os.path.basename(save_path), save_path=save_path,
                      segments=max(1, safe_int(segments, 1, 1, 32)), checksum=checksum,
                      priority=1, queue_order=self._queue_sequence)
        dl.app = self
        dl.speed_limit = max(0, safe_int(self.config.get("speed_limit", 0), 0))
        dl.speed_limit_override = None
        self.downloads.append(dl)
        self._render_card(dl)
        self._next_queued()
        self._update_stats()
        self._update_status_bar()
        self._persist_queue_snapshot_now()
        if details_were_open:
            self._open_download_details(dl)
        self._switch_tab("Downloads")
        logger.info("Download added: %s (priority=%s)", dl.filename, dl.priority)

    def _persist_queue_snapshot_now(self):
        try:
            self.db.save_active_queue(self.downloads)
            self._queue_snapshot_state = self._queue_snapshot_signature()
        except Exception as exc:
            logger.warning("Queue snapshot failed: %s", exc)

    @staticmethod
    def _priority_number(value) -> int:
        labels = {"Low": 0, "Normal": 1, "High": 2, "Scăzută": 0, "Normală": 1, "Ridicată": 2}
        return labels.get(str(value), safe_int(value, 1, 0, 2))

    @staticmethod
    def _priority_label(value: int) -> str:
        return {0: "Low", 1: "Normal", 2: "High"}.get(safe_int(value, 1, 0, 2), "Normal")

    def _queue_sort_key(self, dl: Download):
        return self.smart_automation.queue_score(dl)

    def _set_download_priority(self, dl: Download, value: str):
        if dl not in self.downloads:
            return
        with dl.lock:
            dl.priority = self._priority_number(value)
        self._persist_queue_snapshot_now()
        self._refresh_queue()
        self._next_queued()
        self._update_status_bar()

    def _queue_limit_label(self, dl: Download) -> str:
        override = getattr(dl, "speed_limit_override", None)
        if override is None:
            return "Global"
        if override <= 0:
            return "Unlimited"
        if override >= 1024 * 1024 and override % (1024 * 1024) == 0:
            return f"{override // (1024 * 1024)} MB/s"
        return f"{max(1, round(override / 1024))} KB/s"

    def _set_download_speed_limit(self, dl: Download, value: str):
        if dl not in self.downloads:
            return
        presets = {
            "Global": None, "128 KB/s": 128 * 1024, "256 KB/s": 256 * 1024,
            "512 KB/s": 512 * 1024, "1 MB/s": 1024 * 1024,
            "2 MB/s": 2 * 1024 * 1024, "5 MB/s": 5 * 1024 * 1024,
            "Unlimited": 0,
        }
        with dl.lock:
            dl.speed_limit_override = presets.get(value, None)
            dl.throttle_deadline = time.monotonic()
        self._persist_queue_snapshot_now()
        self._refresh_queue()

    def _move_queued_download(self, dl: Download, direction: int):
        if dl not in self.downloads or dl.status != DLStatus.QUEUED:
            return
        peers = sorted((item for item in self.downloads
                        if item.status == DLStatus.QUEUED and item.priority == dl.priority),
                       key=lambda item: (item.queue_order, item.added_date))
        try:
            index = peers.index(dl)
        except ValueError:
            return
        other_index = index + (-1 if direction < 0 else 1)
        if other_index < 0 or other_index >= len(peers):
            return
        other = peers[other_index]
        dl.queue_order, other.queue_order = other.queue_order, dl.queue_order
        # Normalize to distinct positive positions, retaining visual order.
        peers = sorted(peers, key=lambda item: (item.queue_order, item.added_date))
        for pos, item in enumerate(peers, 1):
            item.queue_order = pos
        self._queue_sequence = max(self._queue_sequence, len(self.downloads))
        self._persist_queue_snapshot_now()
        self._refresh_queue()
        self._next_queued()


    def _render_card(self, dl: Download):
        if self.empty_lbl.winfo_ismapped():
            self.empty_lbl.pack_forget()
        host = SwipeCardHost(
            self.scroll, "⏸ Pauză · ⏹ Oprește", "✓ Selectează · ✕ Elimină",
            on_left=lambda d=dl: self._download_swipe_primary(d),
            on_right=lambda d=dl: self._toggle_download_selection(d),
            on_left_long=lambda d=dl: self._stop_download(d),
            on_right_long=lambda d=dl: self._remove_download_from_swipe(d))
        host.pack(fill="x", padx=4, pady=5)
        card = DownloadCard(host, dl, on_remove=self._remove_download, on_pause=self._pause_download,
                           on_resume=self._resume_download, on_stop=self._stop_download,
                           on_open_folder=lambda d: open_folder(d.save_path))
        host.attach(card)
        self.cards[id(dl)] = card
        self.swipe_hosts[id(dl)] = host
        card.set_selected(id(dl) in self.selected_download_ids)
        self._update_swipe_card(dl)
        # Card nou => setul vizibil s-a schimbut: invalidăm semnătura, ca o
        # eventuală reutilizare de id() să nu lase o stare veche.
        self._visible_signature = None
        self._filter_cards()

    def _active_slot_count(self) -> int:
        active = 0
        for item in self.downloads:
            status = item.status
            thread_alive = bool(item.thread and item.thread.is_alive())
            if status == DLStatus.DOWNLOADING or (thread_alive and status != DLStatus.PAUSED):
                active += 1
        return active

    def _start_if_slot(self, dl: Download):
        if dl.status != DLStatus.QUEUED or self._active_slot_count() >= self.max_concurrent:
            return
        dl.status = DLStatus.DOWNLOADING
        if dl.started_at is None:
            dl.started_at = datetime.now()
        dl.stop_event = threading.Event()
        dl.pause_event = threading.Event()
        dl.pause_event.set()
        dl.resume_requested = False
        thread = threading.Thread(target=_download_worker_entry,
                                  args=(dl, self.update_queue),
                                  name=f"CatsSoftDownload-{id(dl):x}", daemon=True)
        dl.thread = thread
        thread.start()
        self._refresh_card(dl)
        self._filter_cards()
        self._update_status_bar()

    def _retry_download(self, dl: Download):
        if dl not in self.downloads or dl.status not in (DLStatus.ERROR, DLStatus.CANCELLED):
            return
        if dl.thread and dl.thread.is_alive():
            messagebox.showinfo("Descărcare încă în oprire",
                                "Transferul anterior încă se închide. Reîncearcă peste câteva momente.", parent=self)
            return
        if dl.status == DLStatus.ERROR and "checksum" in dl.error_msg.lower():
            try:
                if os.path.isfile(dl.save_path):
                    os.remove(dl.save_path)
                _remove_segment_parts(dl.save_path)
                _remove_download_meta(dl.save_path)
            except OSError as exc:
                messagebox.showerror("Eroare", f"Nu pot elimina fișierul cu checksum invalid:\n{exc}", parent=self)
                return
        with dl.lock:
            dl.status = DLStatus.QUEUED
            dl.error_msg = ""
            dl.downloaded = 0
            dl.progress = 0.0
            dl.speed = 0.0
            dl.resume_requested = False
            dl.history_recorded = False
            dl.automatic_retry_count = 0
            dl.auto_retry_at = 0.0
            dl.speed_tracker.samples.clear()
            dl.throttle_deadline = time.monotonic()
        self._refresh_card(dl)
        self._next_queued()
        self._update_stats()
        self._filter_cards()
        self._persist_queue_snapshot_now()

    def _next_queued(self):
        """Umple sloturile libere după prioritate și ordine, fără a depăși limita concurentă."""
        if getattr(self, "_closing", False):
            return
        guard = 0
        disk_blocked_ids = set()
        while self._active_slot_count() < self.max_concurrent and guard < len(self.downloads) + 2:
            guard += 1
            now = time.time()
            candidates = [
                d for d in self.downloads
                if (d.status == DLStatus.QUEUED
                    and id(d) not in disk_blocked_ids
                    and not (d.thread and d.thread.is_alive())
                    and self.smart_automation.retry_is_ready(d, now))
                or (d.status == DLStatus.PAUSED and d.resume_requested and id(d) not in disk_blocked_ids)
            ]
            if not candidates:
                break
            item = min(candidates, key=self._queue_sort_key)
            if not self.smart_automation.can_start(item):
                disk_blocked_ids.add(id(item))
                continue
            if item.status == DLStatus.PAUSED:
                item.resume_requested = False
                item.status = DLStatus.DOWNLOADING
                if item.pause_event:
                    item.pause_event.set()
                self._refresh_card(item)
                continue
            before = self._active_slot_count()
            self._start_if_slot(item)
            if self._active_slot_count() <= before and item.status == DLStatus.QUEUED:
                break
        if hasattr(self, "_statusbar") or hasattr(self, "statusbar"):
            self._update_status_bar()
        if getattr(self, "_current_tab", None) == "Queue" and hasattr(self, "queue_table"):
            self._refresh_queue()

    def _reset_bandwidth_if_idle(self):
        if not any(d.status == DLStatus.DOWNLOADING for d in self.downloads):
            with self._bandwidth_lock:
                self._bandwidth_deadline = time.monotonic()

    def _pause_download(self, dl: Download):
        if dl.status == DLStatus.DOWNLOADING and dl.pause_event:
            dl.status = DLStatus.PAUSED
            dl.resume_requested = False
            dl.pause_event.clear()
            self._refresh_card(dl)
            self._update_stats()
            self._update_status_bar()
            self._next_queued()
            self._filter_cards()

    def _resume_download(self, dl: Download):
        if dl.status != DLStatus.PAUSED or not dl.pause_event:
            return
        if self._active_slot_count() < self.max_concurrent:
            dl.status = DLStatus.DOWNLOADING
            dl.resume_requested = False
            dl.pause_event.set()
        else:
            dl.resume_requested = True
        self._refresh_card(dl)
        self._update_stats()
        self._update_status_bar()
        self._next_queued()
        self._filter_cards()

    def _stop_download(self, dl: Download):
        previous_status = dl.status
        if dl.stop_event:
            dl.stop_event.set()
        # Wake a paused worker so it can observe stop_event and leave promptly.
        if dl.pause_event:
            dl.pause_event.set()
        # Închidem răspunsul HTTP activ: fără asta, descărcarea ar continua
        # până la următorul bloc citit (ori până la timeout-ul de citire, în
        # cazul unui server care nu mai trimite nimic). Închiderea se face în
        #tr-un fir secundar: pe Windows, close() pe un socket cu un recv în
        # așteptare blochează până la timeout și ar congela interfața.
        response = getattr(dl, "active_response", None)
        if response is not None:
            _close_response_async(response)
        # Calea segmentată ține simultan mai multe răspunsuri deschise: le
        # închidem pe toate, ca oprirea să funcționeze și în ea.
        _close_active_responses(dl)
        # Reținem dacă era o reîncercare automată programată. Informația se
        # pierde imediat la resetarea contorului de mai jos, iar ea e exact ce
        # permite înregistrarea anulării în istoric.
        had_scheduled_retry = bool(getattr(dl, "auto_retry_at", 0.0))
        if previous_status in (DLStatus.DOWNLOADING, DLStatus.PAUSED, DLStatus.QUEUED):
            dl.auto_retry_at = 0.0
            dl.status = DLStatus.CANCELLED
            dl.speed = 0.0
            dl.resume_requested = False
            self._refresh_card(dl)
            self._update_stats()
            self._update_status_bar()
            # A queued automatic retry can briefly still have its previous worker
            # unwinding. Always record cancellation of that scheduled retry; otherwise
            # worker_finished may see CANCELLED and skip the terminal history event.
            if (previous_status == DLStatus.QUEUED and had_scheduled_retry) or not dl.thread or not dl.thread.is_alive():
                self.update_queue.put(("status", dl, DLStatus.CANCELLED))
            self._next_queued()
            self._filter_cards()
            self._persist_queue_snapshot_now()

    def _remove_download(self, dl: Download):
        self._stop_download(dl)
        if dl in self.downloads: self.downloads.remove(dl)
        self.selected_download_ids.discard(id(dl))
        card = self.cards.pop(id(dl), None)
        host = self.swipe_hosts.pop(id(dl), None)
        # Semnătura vizibilității e construită din id()-uri, iar CPython poate
        # refolosi un id pentru un obiect nou. Invalidarea ei la orice schimbare
        # structurală garantează că reîmpachetarea se face corect.
        self._visible_signature = None
        if host: host.destroy()
        elif card: card.destroy()
        visible = [d for d in self.downloads]
        if not visible: self.empty_lbl.pack(expand=True, pady=80)
        self._update_stats()
        self._filter_cards()
        self._update_download_selection_button()

    def _download_swipe_primary(self, dl):
        if dl.status == DLStatus.DOWNLOADING:
            self._pause_download(dl)
        elif dl.status == DLStatus.PAUSED:
            self._resume_download(dl)
        elif dl.status == DLStatus.QUEUED:
            self._stop_download(dl)

    def _remove_download_from_swipe(self, dl):
        if messagebox.askyesno("Confirmă eliminarea", f"Elimini {dl.filename} din listă?", parent=self):
            self._remove_download(dl)

    def _toggle_download_selection(self, dl):
        key = id(dl)
        if key in self.selected_download_ids:
            self.selected_download_ids.remove(key)
        else:
            self.selected_download_ids.add(key)
        card = self.cards.get(key)
        if card:
            card.set_selected(key in self.selected_download_ids)
        self._update_download_selection_button()

    def _update_download_selection_button(self):
        button = getattr(self, "delete_selected_btn", None)
        selected = [dl for dl in getattr(self, "downloads", []) if id(dl) in getattr(self, "selected_download_ids", set())]
        if button and button.winfo_exists():
            count = len(selected)
            button.configure(text=f"🗑 ({count})",
                             state="normal" if count else "disabled",
                             fg_color=ACCENT() if count else BG_CARD(),
                             hover_color=shade_color(ACCENT(), 0.82) if count else BG_CARD_HOV(),
                             text_color="#ffffff" if count else TEXT_SEC())
        buttons = getattr(self, "bulk_action_buttons", {})
        states = {
            "pause": any(dl.status == DLStatus.DOWNLOADING for dl in selected),
            "resume": any(dl.status == DLStatus.PAUSED for dl in selected),
            "retry": any(dl.status in (DLStatus.ERROR, DLStatus.CANCELLED) for dl in selected),
            "cancel": any(dl.status in (DLStatus.DOWNLOADING, DLStatus.PAUSED, DLStatus.QUEUED) for dl in selected),
        }
        for name, item in buttons.items():
            try: item.configure(state="normal" if states.get(name, False) else "disabled")
            except tk.TclError: pass

    def _bulk_selected(self, action: str):
        selected = [dl for dl in self.downloads if id(dl) in self.selected_download_ids]
        for dl in selected:
            try:
                if action == "pause" and dl.status == DLStatus.DOWNLOADING:
                    self._pause_download(dl)
                elif action == "resume" and dl.status == DLStatus.PAUSED:
                    self._resume_download(dl)
                elif action == "retry" and dl.status in (DLStatus.ERROR, DLStatus.CANCELLED):
                    self._retry_download(dl)
                elif action == "cancel" and dl.status in (DLStatus.DOWNLOADING, DLStatus.PAUSED, DLStatus.QUEUED):
                    self._stop_download(dl)
            except Exception:
                logger.exception("Bulk action %s failed for %s", action, dl.filename)
        self._next_queued()
        self._update_download_selection_button()
        self._update_status_bar()

    def _delete_selected_downloads(self):
        selected = [dl for dl in self.downloads if id(dl) in self.selected_download_ids]
        if not selected:
            return
        if messagebox.askyesno("Confirmă eliminarea", f"Elimini {len(selected)} descărcări selectate din listă?", parent=self):
            for dl in selected:
                self._remove_download(dl)

    def _update_swipe_card(self, dl):
        host = self.swipe_hosts.get(id(dl))
        if not host or not host.winfo_exists():
            return
        host.sync_content_height()
        if dl.status == DLStatus.DOWNLOADING:
            left = "⏸ Pauză · ⏹ Oprește"
        elif dl.status == DLStatus.PAUSED:
            left = "▶ Reia · ⏹ Oprește"
        elif dl.status == DLStatus.QUEUED:
            left = "⏹ Anulează"
        else:
            left = "Acțiune indisponibilă"
        right = "✓ Selectează · ✕ Elimină" if id(dl) not in self.selected_download_ids else "✓ Selectat · ✕ Elimină"
        host.set_action_text(left=left, right=right)

    def _pause_all(self):
        for dl in list(self.downloads):
            if dl.status == DLStatus.DOWNLOADING: self._pause_download(dl)

    def _resume_all(self):
        for dl in list(self.downloads):
            if dl.status == DLStatus.PAUSED: self._resume_download(dl)
        self._next_queued()

    def _stop_all(self):
        for dl in list(self.downloads):
            if dl.status in (DLStatus.DOWNLOADING, DLStatus.PAUSED, DLStatus.QUEUED):
                self._stop_download(dl)

    def _clear_done(self):
        for dl in list(self.downloads):
            if dl.status in (DLStatus.COMPLETED, DLStatus.CANCELLED, DLStatus.ERROR):
                self._remove_download(dl)

    def _refresh_card(self, dl: Download):
        card = self.cards.get(id(dl))
        if card:
            try: card.refresh_ui()
            except Exception: pass
        details = getattr(self, "details_view", None)
        if details is not None:
            try:
                details.refresh_from_model(dl)
            except Exception:
                logger.debug("Nu am putut actualiza detaliile descărcării", exc_info=True)

    def _open_download_details(self, dl: Download):
        if getattr(self, "details_view", None) is not None:
            try:
                self.details_view.destroy()
            except Exception:
                pass
        self.downloads_list_view.pack_forget()
        self.details_view = DownloadDetailsView(self._tab_frames["Downloads"], self, dl)
        self.details_view.pack(fill="both", expand=True)

    def _close_download_details(self):
        self.details_view = None
        self.downloads_list_view.pack(fill="both", expand=True)

    @staticmethod
    def _download_speed_value(dl: Download) -> float:
        """Viteza curentă (B/s), citită sub lock ca să nu fie o valoare veche."""
        try:
            with dl.lock:
                return max(0.0, float(dl.speed or 0.0))
        except Exception:
            return 0.0

    @staticmethod
    def _download_total_value(dl: Download) -> int:
        """Dimensiunea totală a resursei (octeți), citită sub lock."""
        try:
            with dl.lock:
                return max(0, int(dl.total or 0))
        except Exception:
            return 0

    @staticmethod
    def _download_eta_key(dl: Download):
        """Cheie de sortare după ETA.

        Descărcările fără ETA estimabilă (nu sunt în transfer, viteza e 0 sau
        dimensiunea e necunoscută) merg la sfârșit, nu la început.
        """
        try:
            with dl.lock:
                status = dl.status
                speed = max(0.0, float(dl.speed or 0.0))
                downloaded = max(0, int(dl.downloaded or 0))
                total = max(0, int(dl.total or 0))
        except Exception:
            return (1, 0.0)
        if status != DLStatus.DOWNLOADING or speed <= 0 or total <= downloaded:
            return (1, 0.0)
        return (0, (total - downloaded) / speed)

    def _maybe_refresh_dynamic_sort(self):
        """Re-ordonează cardurile când sortarea e dinamică (viteză / ETA).

        Sortarea dinamică schimbă ordinea odată cu datele, deci trebuie
        reevaluată periodic. E limitată la o dată la 2 secunde și doar pe
        tab-ul Descărcări, ca să nu reîmpachetăm cardurile la fiecare ciclu.
        """
        if getattr(self, "_current_tab", "") != "Downloads":
            return
        try:
            if self._sort_order.get() not in ("Viteză ↓", "ETA ↑"):
                return
        except Exception:
            return
        now = time.monotonic()
        if now - getattr(self, "_dynamic_sort_last", 0.0) < 2.0:
            return
        self._dynamic_sort_last = now
        self._filter_cards()

    def _filter_cards(self, *_):
        term = self._search_var.get().lower()
        filter_name = self._status_filter.get()
        statuses = {
            "Active": {DLStatus.DOWNLOADING, DLStatus.PAUSED},
            "În coadă": {DLStatus.QUEUED},
            "Completate": {DLStatus.COMPLETED},
            "Erori": {DLStatus.ERROR},
        }
        visible = []
        for dl in self.downloads:
            matches_term = not term or term in dl.filename.lower() or term in dl.url.lower()
            matches_status = filter_name == "Toate" or dl.status in statuses.get(filter_name, set())
            if matches_term and matches_status:
                visible.append(dl)
        sort_order = self._sort_order.get()
        if sort_order == "Cele mai noi":
            visible.sort(key=lambda item: item.added_date, reverse=True)
        elif sort_order == "Cele mai vechi":
            visible.sort(key=lambda item: item.added_date)
        elif sort_order == "Viteză ↓":
            visible.sort(key=lambda item: -self._download_speed_value(item))
        elif sort_order == "Dimensiune ↓":
            visible.sort(key=lambda item: -self._download_total_value(item))
        elif sort_order == "ETA ↑":
            visible.sort(key=self._download_eta_key)
        else:
            visible.sort(key=lambda item: item.filename.casefold())
        # Reîmpachetarea tuturor cardurilor la fiecare ciclu de UI (10/s) era
        # costisitoare și invalida geometria fiecărui card. O facem doar când
        # setul vizibil (sau ordinea lui) chiar se schimbă.
        visible_signature = tuple(id(dl) for dl in visible)
        if visible_signature == getattr(self, "_visible_signature", None):
            return
        self._visible_signature = visible_signature
        for dl in self.downloads:
            host = self.swipe_hosts.get(id(dl))
            if not host: continue
            host.pack_forget()
        if not self.downloads:
            self.no_match_lbl.pack_forget()
            if not self.empty_lbl.winfo_ismapped():
                self.empty_lbl.pack(expand=True, pady=80)
            return
        self.empty_lbl.pack_forget()
        for dl in visible:
            host = self.swipe_hosts.get(id(dl))
            if host:
                host.pack(fill="x", padx=4, pady=5)
        if visible:
            self.no_match_lbl.pack_forget()
        elif not self.no_match_lbl.winfo_ismapped():
            self.no_match_lbl.pack(expand=True, pady=50)

    def _update_stats(self):
        total = len(self.downloads)
        done = sum(1 for d in self.downloads if d.status == DLStatus.COMPLETED)
        active = sum(1 for d in self.downloads if d.status == DLStatus.DOWNLOADING)
        queued = sum(1 for d in self.downloads if d.status == DLStatus.QUEUED)
        speed = sum(d.speed for d in self.downloads if d.status == DLStatus.DOWNLOADING)
        parts = []
        if total: parts.append(f"{total} fișier{'e' if total > 1 else ''}")
        if active: parts.append(f"{active} activ{'e' if active > 1 else ''}")
        if queued: parts.append(f"{queued} în așteptare")
        if done: parts.append(f"{done} completat{'e' if done > 1 else ''}")
        if speed: parts.append(f"↓ {fmt_speed(speed)}")
        self.stats_lbl.configure(text="  •  ".join(parts))

    def _refresh_queue(self):
        if not hasattr(self, "queue_table") or not self.queue_table.winfo_exists():
            return
        for widget in self.queue_table.winfo_children():
            widget.destroy()
        counts = {status: 0 for status in DLStatus}
        speed = 0.0
        for dl in self.downloads:
            counts[dl.status] += 1
            if dl.status == DLStatus.DOWNLOADING:
                with dl.lock:
                    speed += max(0.0, float(dl.speed or 0.0))
        retry_waiting = sum(1 for item in self.downloads
                            if item.status == DLStatus.QUEUED and getattr(item, "auto_retry_at", 0.0) > time.time())
        extra_info = []
        if retry_waiting:
            extra_info.append(f"Retry programate: {retry_waiting}")
        if self.smart_automation.block_reason:
            extra_info.append(self.smart_automation.block_reason)
        extra_text = ("   ·   " + "   ·   ".join(extra_info)) if extra_info else ""
        self.queue_info.configure(
            text=(f"Active: {counts[DLStatus.DOWNLOADING]}   În așteptare: {counts[DLStatus.QUEUED]}   "
                  f"Pauză: {counts[DLStatus.PAUSED]}   Completate: {counts[DLStatus.COMPLETED]}   "
                  f"Erori: {counts[DLStatus.ERROR]}" + (f"   ·   {fmt_speed(speed)}" if speed else "") + extra_text))
        if not self.downloads:
            ctk.CTkLabel(self.queue_table, text="Nicio descărcare.", text_color=TEXT_DIM(),
                         font=ctk.CTkFont(size=13)).pack(pady=40)
            return

        head = ctk.CTkFrame(self.queue_table, fg_color="transparent")
        head.pack(fill="x", padx=4, pady=(0, 4))
        headings = [(0, "FIȘIER", "w"), (1, "STARE", "e"), (2, "PRIORITATE", "center"),
                    (3, "LIMITĂ INDIVIDUALĂ", "center"), (4, "ORDINE", "center")]
        for column, title, anchor in headings:
            ctk.CTkLabel(head, text=title, text_color=TEXT_DIM(),
                         font=ctk.CTkFont(size=9, weight="bold"), anchor=anchor).grid(
                         row=0, column=column, sticky="ew", padx=5)
        head.grid_columnconfigure(0, weight=1)
        row_status_color = {
            DLStatus.DOWNLOADING: ACCENT(), DLStatus.PAUSED: WARNING(),
            DLStatus.COMPLETED: SUCCESS(), DLStatus.ERROR: ERROR(),
            DLStatus.QUEUED: ACCENT(), DLStatus.CANCELLED: TEXT_DIM(),
        }
        for dl in sorted(self.downloads, key=lambda item: (
                0 if item.status == DLStatus.QUEUED else 1,
                self._queue_sort_key(item) if item.status == DLStatus.QUEUED else (0, 0, item.added_date))):
            row = ctk.CTkFrame(self.queue_table, fg_color=BG_CARD(), corner_radius=8)
            row.pack(fill="x", pady=3, padx=4)
            row.grid_columnconfigure(0, weight=1)
            color = row_status_color.get(dl.status, TEXT_DIM())
            file_cell = ctk.CTkFrame(row, fg_color="transparent")
            file_cell.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=7)
            file_text = dl.filename
            ctk.CTkLabel(file_cell, text="●", text_color=color,
                         font=ctk.CTkFont(size=10), width=14).pack(side="left", padx=(0, 5))
            ctk.CTkLabel(file_cell, text=file_text, text_color=TEXT_PRI(),
                         font=ctk.CTkFont(size=11), anchor="w").pack(side="left", fill="x", expand=True)

            if dl.status == DLStatus.QUEUED and getattr(dl, "auto_retry_at", 0.0) > time.time():
                remaining_retry = max(1, int(math.ceil(dl.auto_retry_at - time.time())))
                status_text = f"Retry în {remaining_retry}s"
            else:
                status_text = f"{dl.status.value}  {min(100, max(0, dl.progress)):.0f}%"
            ctk.CTkLabel(row, text=status_text, text_color=TEXT_SEC(),
                         font=ctk.CTkFont(size=10), width=122, anchor="e").grid(
                         row=0, column=1, padx=(2, 10), pady=7)

            priority_menu = ctk.CTkOptionMenu(
                row, values=["Low", "Normal", "High"], width=96, height=28,
                fg_color=BG_INPUT(), button_color=BG_INPUT(), button_hover_color=BG_CARD_HOV(),
                text_color=TEXT_PRI(), dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI(),
                command=lambda value, item=dl: self._set_download_priority(item, value))
            priority_menu.set(self._priority_label(dl.priority))
            priority_menu.grid(row=0, column=2, padx=4, pady=5)

            limit_menu = ctk.CTkOptionMenu(
                row, values=["Global", "128 KB/s", "256 KB/s", "512 KB/s", "1 MB/s",
                             "2 MB/s", "5 MB/s", "Unlimited"], width=122, height=28,
                fg_color=BG_INPUT(), button_color=BG_INPUT(), button_hover_color=BG_CARD_HOV(),
                text_color=TEXT_PRI(), dropdown_fg_color=BG_CARD(), dropdown_text_color=TEXT_PRI(),
                command=lambda value, item=dl: self._set_download_speed_limit(item, value))
            limit_menu.set(self._queue_limit_label(dl))
            limit_menu.grid(row=0, column=3, padx=4, pady=5)

            order = ctk.CTkFrame(row, fg_color="transparent")
            order.grid(row=0, column=4, padx=(2, 7), pady=4)
            queued = dl.status == DLStatus.QUEUED
            ctk.CTkButton(order, text="↑", width=27, height=27, corner_radius=6,
                          fg_color=BG_INPUT(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                          state="normal" if queued else "disabled",
                          command=lambda item=dl: self._move_queued_download(item, -1)).pack(side="left", padx=2)
            ctk.CTkButton(order, text="↓", width=27, height=27, corner_radius=6,
                          fg_color=BG_INPUT(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                          state="normal" if queued else "disabled",
                          command=lambda item=dl: self._move_queued_download(item, 1)).pack(side="left", padx=2)

    def _truncate_filename(self, filename: str, max_length: int = 55) -> str:
        """Trunchiază numele fișierului dacă depășește lungimea maximă"""
        if len(filename) <= max_length:
            return filename

        # Arată începutul și sfârșitul cu "..." la mijloc
        # Ex: "nume_foarte_lung_video.mp4" -> "nume_foa...video.mp4"
        start_len = max_length // 2
        end_len = max_length - start_len - 3  # -3 pentru "..."

        return f"{filename[:start_len]}...{filename[-end_len:]}"

    def _cancel_history_render(self):
        """Cancel pending history-page rendering and invalidate stale callbacks."""
        self._history_render_generation = getattr(self, "_history_render_generation", 0) + 1
        after_id = getattr(self, "_history_render_after_id", None)
        if after_id:
            try:
                self.after_cancel(after_id)
            except (tk.TclError, RuntimeError):
                pass
        self._history_render_after_id = None

    def _schedule_history_filter(self):
        """Debounce keyboard search to avoid rebuilding the UI on every keystroke."""
        old_id = getattr(self, "_history_filter_after_id", None)
        if old_id:
            try:
                self.after_cancel(old_id)
            except (tk.TclError, RuntimeError):
                pass
        self._history_filter_after_id = self.after(250, self._run_debounced_history_filter)

    def _run_debounced_history_filter(self):
        self._history_filter_after_id = None
        self._filter_history()

    def _on_history_page_size_change(self, value):
        size = safe_int(value, 25, 25, 100)
        if size not in (25, 50, 100):
            size = 25
        self._history_page_size = size
        self._history_page_index = 0
        try:
            self.db.save_setting("history_page_size", size)
        except Exception:
            logger.debug("Nu s-a putut salva dimensiunea paginii de istoric", exc_info=True)
        self._refresh_history()

    def _history_go_to_page(self, page_index):
        try:
            page_index = max(0, int(page_index))
        except (TypeError, ValueError):
            page_index = 0
        page_size = max(1, getattr(self, "_history_page_size", 25))
        total_pages = max(1, (self._history_total_count + page_size - 1) // page_size)
        page_index = min(page_index, total_pages - 1)
        if page_index != self._history_page_index:
            self._history_page_index = page_index
            self._refresh_history()

    def _history_go_to_last_page(self):
        page_size = max(1, getattr(self, "_history_page_size", 25))
        last_page = max(0, (self._history_total_count - 1) // page_size)
        self._history_go_to_page(last_page)

    def _update_history_pagination(self, total_count):
        page_size = max(1, getattr(self, "_history_page_size", 25))
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        self._history_page_index = min(max(0, self._history_page_index), total_pages - 1)
        if total_count:
            first = self._history_page_index * page_size + 1
            last = min(total_count, first + page_size - 1)
            label = f"{first:,}–{last:,} din {total_count:,} · pagina {self._history_page_index + 1}/{total_pages}".replace(",", " ")
        else:
            label = "0 rezultate · pagina 1/1"
        self.history_page_summary.configure(text=label)
        self.history_first_btn.configure(state="normal" if self._history_page_index > 0 else "disabled")
        self.history_prev_btn.configure(state="normal" if self._history_page_index > 0 else "disabled")
        has_next = self._history_page_index < total_pages - 1
        self.history_next_btn.configure(state="normal" if has_next else "disabled")
        self.history_last_btn.configure(state="normal" if has_next else "disabled")

    def _refresh_history(self):
        """Query SQLite on a worker thread; create/update widgets only on the Tk thread."""
        if not hasattr(self, "history_table") or getattr(self, "_closing", False):
            return
        self._cancel_history_render()
        self._update_history_selection_button()
        # Nu golim tabelul dacă există deja rânduri: cele vechi rămân vizibile
        # până sosesc rezultatele noi. Altfel apărea un flash cu „Se încarcă
        # istoricul…" la fiecare schimbare de filtru.
        if not self.history_cards:
            for widget in self.history_table.winfo_children():
                try:
                    widget.destroy()
                except tk.TclError:
                    pass
            self.history_cards = {}
            ctk.CTkLabel(self.history_table, text="Se încarcă istoricul…",
                         font=ctk.CTkFont(size=13), text_color=TEXT_DIM()).pack(anchor="w", padx=12, pady=20)

        search_term = self.history_search.get().strip() if hasattr(self, "history_search") else ""
        status_filter = self.history_filter_var.get() if hasattr(self, "history_filter_var") else "toate"
        period_filter = self.history_period_var.get() if hasattr(self, "history_period_var") else "Toate"
        type_filter = self.history_type_var.get() if hasattr(self, "history_type_var") else "Toate"
        sort_order = self.history_sort_var.get() if hasattr(self, "history_sort_var") else "Cele mai noi"
        page_size = max(1, getattr(self, "_history_page_size", 25))
        offset = self._history_page_index * page_size
        self._history_query_generation += 1
        token = self._history_query_generation
        self._history_query_inflight += 1

        # Persist only when advanced filters change; do the writes off the Tk thread.
        filter_state = (period_filter, type_filter, sort_order)
        save_filter_state = filter_state != getattr(self, "_history_saved_filter_state", None)
        if save_filter_state:
            self._history_saved_filter_state = filter_state

        def query_worker():
            try:
                if save_filter_state and token == self._history_query_generation:
                    try:
                        self.db.save_setting("history_period_filter", period_filter)
                        self.db.save_setting("history_type_filter", type_filter)
                        self.db.save_setting("history_sort_order", sort_order)
                    except Exception:
                        logger.debug("Nu s-au putut salva preferințele History Pro", exc_info=True)
                history, total_count = self.db.get_history_page(
                    limit=page_size, offset=offset, search_term=search_term,
                    status_filter=status_filter, period_filter=period_filter,
                    type_filter=type_filter, sort_order=sort_order)
                stats = self.db.get_history_stats()
                result = (token, "ok", history, total_count, stats, None,
                          search_term, status_filter, period_filter, type_filter)
            except Exception as exc:
                logger.exception("Nu s-a putut interoga istoricul")
                result = (token, "error", [], 0, None, str(exc),
                          search_term, status_filter, period_filter, type_filter)
            self._history_query_queue.put(result)

        threading.Thread(target=query_worker, name=f"HistoryQuery-{token}", daemon=True).start()
        if self._history_query_poll_id is None:
            self._history_query_poll_id = self.after(50, self._poll_history_query_results)

    def _poll_history_query_results(self):
        self._history_query_poll_id = None
        while True:
            try:
                result = self._history_query_queue.get_nowait()
            except queue.Empty:
                break
            self._history_query_inflight = max(0, self._history_query_inflight - 1)
            token, status, history, total_count, stats, error, search_term, status_filter, period_filter, type_filter = result
            if token != self._history_query_generation or getattr(self, "_closing", False):
                continue  # Ignore stale results from a superseded search/page request.
            self._apply_history_query_result(token, status, history, total_count, stats, error,
                                             search_term, status_filter, period_filter, type_filter)
        if self._history_query_inflight > 0 and not getattr(self, "_closing", False):
            self._history_query_poll_id = self.after(50, self._poll_history_query_results)

    def _apply_history_query_result(self, token, status, history, total_count, stats, error,
                                    search_term, status_filter, period_filter, type_filter):
        if token != self._history_query_generation or not hasattr(self, "history_table"):
            return
        for widget in self.history_table.winfo_children():
            try:
                widget.destroy()
            except tk.TclError:
                pass
        # Referințele trebuie resetate aici: widget-urile vechi tocmai au fost
        # distruse, iar selecția ar încerca să opereze pe ele.
        self.history_cards = {}

        if stats is not None:
            self._update_history_stats(stats=stats)
        if status == "error":
            self._history_total_count = 0
            ctk.CTkLabel(self.history_table,
                         text=f"Nu s-a putut încărca istoricul.\n{error or 'Verifică baza de date.'}",
                         font=ctk.CTkFont(size=13), text_color=ERROR(), justify="left").pack(
                             anchor="w", padx=12, pady=20)
            self._update_history_pagination(0)
            return

        page_size = max(1, getattr(self, "_history_page_size", 25))
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        if self._history_page_index >= total_pages:
            self._history_page_index = total_pages - 1
            # A concurrent deletion may have invalidated the page. Re-query asynchronously.
            self._refresh_history()
            return

        self._history_total_count = total_count
        self._update_history_pagination(total_count)
        try:
            self.history_scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

        if not history:
            is_unfiltered = (not search_term and status_filter == "toate" and
                             period_filter == "Toate" and type_filter == "Toate")
            message = "📭 Istoric gol\nDescărcările vor apărea aici" if total_count == 0 and is_unfiltered else "🔍 Nu s-au găsit rezultate"
            ctk.CTkLabel(self.history_table, text=message, font=ctk.CTkFont(size=14),
                         text_color=TEXT_DIM(), justify="center").pack(fill="x", pady=48)
            return

        # Group only the unfiltered recent view; advanced sorting/filtering stays compact.
        group_by_date = (not search_term and status_filter == "toate" and
                         period_filter == "Toate" and type_filter == "Toate" and
                         self.history_sort_var.get() == "Cele mai noi")
        render_entries = []
        previous_date = None
        for item in history:
            date_key = (item.completed_date or item.added_date or "-")[:10]
            if group_by_date and date_key != previous_date:
                render_entries.append(("date", date_key))
                previous_date = date_key
            render_entries.append(("item", item))

        self._history_render_entries = render_entries
        self._history_render_index = 0
        self._history_render_generation += 1
        generation = self._history_render_generation
        self._render_history_batch(generation)

    def _render_history_batch(self, generation):
        """Create a few cards per event-loop turn so the window remains responsive."""
        if generation != self._history_render_generation:
            return
        self._history_render_after_id = None
        end = min(len(self._history_render_entries), self._history_render_index + 7)
        while self._history_render_index < end:
            kind, value = self._history_render_entries[self._history_render_index]
            self._history_render_index += 1
            if kind == "date":
                ctk.CTkLabel(self.history_table, text=value,
                            font=ctk.CTkFont(size=12, weight="bold"),
                            text_color=TEXT_SEC()).pack(anchor="w", pady=(12, 6), padx=4)
            else:
                self._create_history_item(value)
        if self._history_render_index < len(self._history_render_entries):
            self._history_render_after_id = self.after(
                5, lambda token=generation: self._render_history_batch(token))

    def _update_history_stats(self, history=None, stats=None):
        try:
            if stats is None:
                stats = self.db.get_history_stats()
            total_files = stats["total_files"]
            total_size = stats["total_size"]
            this_month = stats["this_month"]
            errors = stats["recent_errors"]
        except Exception:
            logger.debug("Fallback la calculul statisticilor din pagina curentă", exc_info=True)
            history = history or []
            total_files = len(history)
            total_size = sum((h.downloaded_size or 0) for h in history if h.status == "Completed")
            current_month = datetime.now().strftime("%Y-%m")
            this_month = sum(1 for h in history if h.completed_date and h.completed_date.startswith(current_month))
            errors = sum(1 for h in history if h.status == "Error")

        try:
            self.stat_total_card.winfo_children()[1].configure(text=f"{total_files:,}".replace(",", " "))
            self.stat_size_card.winfo_children()[1].configure(text=fmt_size(total_size))
            self.stat_month_card.winfo_children()[1].configure(text=str(this_month))
            self.stat_errors_card.winfo_children()[1].configure(text=str(errors))
        except Exception:
            logger.debug("Nu s-au putut actualiza cardurile de statistici", exc_info=True)

    def _create_history_item(self, h):
        # Configurare status
        is_light = self._theme_family(THEME.theme.name) == "light"
        status_map = {
            "Completed": ("✓ Finalizat", SUCCESS(), "#d1fae5" if is_light else "#064e3b"),
            "Error": ("✗ Eroare", ERROR(), "#fee2e2" if is_light else "#7f1d1d"),
            "Cancelled": ("⊘ Anulat", TEXT_SEC(), "#f3f4f6" if is_light else "#374151"),
        }
        status_text, status_color, status_bg = status_map.get(h.status, ("?", TEXT_SEC(), BG_INPUT()))

        # Iconiță tip fișier
        filetype = guess_filetype(h.url)
        icons = {"video": "▶", "image": "🖼", "audio": "♫", "document": "📄",
                 "archive": "", "disk": "💿", "file": "📦"}
        icon = icons.get(filetype, "📦")
        icon_color = {"video": "#3b82f6", "image": "#8b5cf6", "audio": "#ec4899",
                      "document": "#f59e0b", "archive": "#14b8a6"}.get(filetype, "#6b7280")

        # Card principal
        selected = h.id in self.selected_history_ids
        host = SwipeCardHost(
            self.history_table, "✕ Șterge din istoric", "✓ Marchează selectat",
            on_left=lambda hid=h.id: self._delete_history_item(hid),
            on_right=lambda hid=h.id: self._toggle_history_selection(hid))
        host.pack(fill="x", pady=5, padx=4)
        card = ctk.CTkFrame(host, fg_color=BG_CARD(), corner_radius=12,
                            border_width=2 if selected else 1,
                            border_color=ACCENT() if selected else BORDER())

        # Partea stângă: Icon + Info
        left = ctk.CTkFrame(card, fg_color="transparent")
        left.pack(side="left", padx=16, pady=14, fill="x", expand=True)

        # Icon background
        icon_bg = ctk.CTkFrame(left, fg_color=BG_INPUT(), corner_radius=8, width=40, height=40)
        icon_bg.pack(side="left", padx=(0, 12))
        icon_bg.pack_propagate(False)
        ctk.CTkLabel(icon_bg, text=icon, font=ctk.CTkFont(size=18), text_color=icon_color).place(relx=0.5, rely=0.5, anchor="center")

        # === FIX TOOLTIP: Etichetă trunchiată vizual, dar cu text complet în tooltip ===
        full_name = h.filename
        display_name = full_name if len(full_name) <= 25 else full_name[:30] + "..."

        # Nume fișier + URL
        info = ctk.CTkFrame(left, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)

        name_lbl = ctk.CTkLabel(info, text=display_name, font=ctk.CTkFont(size=13, weight="bold"),
                    text_color=TEXT_PRI(), anchor="w")
        name_lbl.pack(anchor="w")

        # Creează tooltip DOAR dacă numele e trunchiat
        if len(full_name) > 45:
            _Tooltip(name_lbl, full_name)  # Instanță separată per rând!

        url_short = h.url[:55] + "..." if len(h.url) > 58 else h.url
        ctk.CTkLabel(info, text=url_short, font=ctk.CTkFont(size=11),
                    text_color=TEXT_DIM(), anchor="w").pack(anchor="w", pady=(2, 0))

        # Partea dreaptă: Dimensiune, Dată, Status, Acțiuni
        right = ctk.CTkFrame(card, fg_color="transparent")
        right.pack(side="right", padx=(0, 16), pady=14)

        # Dimensiune
        ctk.CTkLabel(right, text=fmt_size(h.downloaded_size), font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=TEXT_PRI()).pack(anchor="e")

        # Dată
        date_val = (h.completed_date or h.added_date or "-")[:16].replace("T", " ")
        ctk.CTkLabel(right, text=date_val, font=ctk.CTkFont(size=10),
                    text_color=TEXT_DIM()).pack(anchor="e", pady=(2, 0))

        # Badge status
        badge = ctk.CTkFrame(right, fg_color=status_bg, corner_radius=6, height=24)
        badge.pack(anchor="e", pady=(8, 0))
        badge.pack_propagate(False)

        status_lbl = ctk.CTkLabel(badge, text=status_text, font=ctk.CTkFont(size=9, weight="bold"),
                    text_color=status_color, corner_radius=8)
        status_lbl.place(relx=0.5, rely=0.5, anchor="center")

        _Tooltip(status_lbl, status_text)  # Instanță separată per rând!
        selection_lbl = ctk.CTkLabel(right, text="✓ Selectat" if selected else "",
                                     text_color=ACCENT(),
                                     font=ctk.CTkFont(size=10, weight="bold"))
        if selected:
            selection_lbl.pack(anchor="e", pady=(4, 0))

        # Butoane acțiune
        actions = ctk.CTkFrame(card, fg_color="transparent", width=80)
        actions.pack(side="right", padx=(0, 8), pady=12)

        def open_folder_action(path=h.save_path):
            open_folder(path)

        def redownload_action(url=h.url, folder=os.path.dirname(h.save_path), name=h.filename):
            self._add_download(url, folder, name)

        def delete_action(hid=h.id):
            self._delete_history_item(hid)

        # FIX: Configurare de bază fără hover_color pentru a evita conflictul
        btn_kwargs = {"width": 30, "height": 30, "fg_color": BG_INPUT(),
                      "text_color": TEXT_SEC(), "font": ctk.CTkFont(size=14)}

        # Deschide folder
        open_btn = ctk.CTkButton(actions, text="📂", command=open_folder_action, hover_color=BG_CARD_HOV(), **btn_kwargs)
        open_btn.pack(side="left", padx=2)

        _Tooltip(open_btn, "Deschide locația de salvare")  # Instanță separată per rând!

        # Redescarcă (doar dacă nu e completat)
        if h.status != "Completed":
            redownload_btn = ctk.CTkButton(actions, text="🔄", command=redownload_action, hover_color="#3b82f6", **btn_kwargs)
            redownload_btn.pack(side="left", padx=2)

            _Tooltip(redownload_btn, "Descarcă din nou")  # Instanță separată per rând!

        # Șterge
        remove_btn = ctk.CTkButton(actions, text="✕", command=delete_action, hover_color="#ef4444", **btn_kwargs)
        remove_btn.pack(side="left", padx=2)

        _Tooltip(remove_btn, "Șterge fișierul din listă")  # Instanță separată per rând!
        host.attach(card)
        host.set_action_text(right="✓ Selectat" if selected else "✓ Marchează selectat")
        self.history_cards[h.id] = (host, card, selection_lbl)

    def _filter_history(self):
        """Apply all History Pro filters and return to the first matching page."""
        pending = getattr(self, "_history_filter_after_id", None)
        if pending:
            try:
                self.after_cancel(pending)
            except (tk.TclError, RuntimeError):
                pass
            self._history_filter_after_id = None
        self._history_page_index = 0
        self._refresh_history()

    def _refresh_scheduled(self):
        self.scheduler._load_scheduled()
        for w in self.scheduled_table.winfo_children(): w.destroy()
        scheduled = self.scheduler.scheduled
        if not scheduled:
            ctk.CTkLabel(self.scheduled_table, text="Nicio descărcare programată.", text_color=TEXT_DIM(),
                        font=ctk.CTkFont(size=13)).pack(pady=40)
            return
        for sd in scheduled:
            row = ctk.CTkFrame(self.scheduled_table, fg_color=BG_CARD(), corner_radius=8)
            row.pack(fill="x", pady=3, padx=4)
            ctk.CTkLabel(row, text="📅", text_color=ACCENT(), font=ctk.CTkFont(size=14), width=30).pack(side="left", padx=(10, 4))
            ctk.CTkLabel(row, text=sd.filename, text_color=TEXT_PRI(), font=ctk.CTkFont(size=12), anchor="w").pack(side="left", padx=4, fill="x", expand=True)
            ctk.CTkLabel(row, text=fmt_datetime(sd.scheduled_time), text_color=TEXT_SEC(),
                        font=ctk.CTkFont(size=11)).pack(side="right", padx=10)
            ctk.CTkLabel(row, text=sd.repeat, text_color=TEXT_DIM(), font=ctk.CTkFont(size=11)).pack(side="right", padx=10)
            ctk.CTkButton(row, text="✕", width=30, height=30, fg_color="transparent", hover_color="#3a1a1a",
                         text_color=TEXT_SEC(), command=lambda x=sd: [self._remove_scheduled(x), self._refresh_scheduled()]).pack(side="right", padx=5)

    def _remove_scheduled(self, sd: ScheduledDownload):
        self.scheduler.remove(sd)
        self._refresh_scheduled()

    def _clear_history(self):
        if (getattr(self, "_history_mutation_active", False)
                or getattr(self, "_history_export_active", False)):
            return
        if not messagebox.askyesno("Confirmare", "Ștergi tot istoricul?", parent=self):
            return
        self._history_mutation_active = True
        self._set_history_mutation_controls_busy(True)
        self.history_page_summary.configure(text="Se șterge istoricul în fundal…")

        def worker():
            try:
                self.db.clear_history()
                error = None
            except Exception as exc:
                logger.exception("Nu s-a putut șterge istoricul")
                error = str(exc)
            self.post_ui(lambda err=error: self._finish_history_clear(err))

        threading.Thread(target=worker, name="HistoryClear", daemon=True).start()

    def _finish_history_clear(self, error=None):
        self._history_mutation_active = False
        self._set_history_mutation_controls_busy(False)
        if getattr(self, "_closing", False):
            return
        if error:
            messagebox.showerror("Istoric", f"Istoricul nu a putut fi șters:\n{error}", parent=self)
            self._refresh_history()
            return
        self.selected_history_ids.clear()
        self._history_page_index = 0
        self._refresh_history()

    def _set_history_mutation_controls_busy(self, busy):
        state = "disabled" if busy else "normal"
        for attr in ("history_clear_btn", "history_export_btn"):
            button = getattr(self, attr, None)
            if button is not None:
                try:
                    button_state = state
                    if attr == "history_export_btn" and getattr(self, "_history_export_active", False):
                        button_state = "disabled"
                    button.configure(state=button_state)
                except tk.TclError:
                    pass
        if busy:
            try:
                self.delete_selected_history_btn.configure(state="disabled")
            except (tk.TclError, AttributeError):
                pass
        else:
            self._update_history_selection_button()

    def _toggle_history_selection(self, history_id):
        if history_id in self.selected_history_ids:
            self.selected_history_ids.remove(history_id)
        else:
            self.selected_history_ids.add(history_id)
        selected = history_id in self.selected_history_ids
        entry = self.history_cards.get(history_id)
        if entry:
            host, card, selection_lbl = entry
            card.configure(border_width=2 if selected else 1,
                           border_color=ACCENT() if selected else BORDER())
            selection_lbl.configure(text="✓ Selectat" if selected else "")
            if selected and not selection_lbl.winfo_ismapped():
                selection_lbl.pack(anchor="e", pady=(4, 0))
            elif not selected and selection_lbl.winfo_ismapped():
                selection_lbl.pack_forget()
            host.set_action_text(right="✓ Selectat" if selected else "✓ Marchează selectat")
            host.sync_content_height()
        self._update_history_selection_button()

    def _delete_history_item(self, history_id):
        if getattr(self, "_history_mutation_active", False):
            return
        if not messagebox.askyesno("Confirmă", "Ștergi acest element din istoric?", parent=self):
            return
        with self.db._connect(10) as conn:
            conn.execute("DELETE FROM history WHERE id = ?", (history_id,))
        self.selected_history_ids.discard(history_id)
        self._refresh_history()

    def _delete_selected_history(self):
        selected = tuple(self.selected_history_ids)
        if (not selected or getattr(self, "_history_mutation_active", False)
                or getattr(self, "_history_export_active", False)):
            return
        if not messagebox.askyesno(
                "Confirmă ștergerea",
                f"Ștergi {len(selected)} elemente selectate din istoric?", parent=self):
            return
        self._history_mutation_active = True
        self._set_history_mutation_controls_busy(True)
        self.history_page_summary.configure(text=f"Se șterg {len(selected):,} înregistrări…".replace(",", " "))

        def worker():
            try:
                with self.db._connect(20) as conn:
                    conn.execute("PRAGMA busy_timeout=20000")
                    for start in range(0, len(selected), 500):
                        batch = selected[start:start + 500]
                        placeholders = ",".join("?" for _ in batch)
                        conn.execute(f"DELETE FROM history WHERE id IN ({placeholders})", batch)
                    conn.commit()
                error = None
            except Exception as exc:
                logger.exception("Nu s-au putut șterge înregistrările selectate")
                error = str(exc)
            self.post_ui(lambda err=error, ids=selected: self._finish_selected_history_delete(ids, err))

        threading.Thread(target=worker, name="HistoryBulkDelete", daemon=True).start()

    def _finish_selected_history_delete(self, selected_ids, error=None):
        self._history_mutation_active = False
        self._set_history_mutation_controls_busy(False)
        if getattr(self, "_closing", False):
            return
        if error:
            messagebox.showerror("Istoric", f"Nu s-au putut șterge înregistrările:\n{error}", parent=self)
        else:
            self.selected_history_ids.difference_update(selected_ids)
        self._refresh_history()

    def _update_history_selection_button(self):
        button = getattr(self, "delete_selected_history_btn", None)
        if button and button.winfo_exists():
            count = len(self.selected_history_ids)
            button.configure(text=f"🗑 Selectate ({count})",
                             state="normal" if count else "disabled",
                             fg_color=ACCENT() if count else BG_CARD(),
                             hover_color=shade_color(ACCENT(), 0.82) if count else BG_CARD_HOV(),
                             text_color="#ffffff" if count else TEXT_SEC())

    def _export_history(self):
        """Ask which history subset should be exported, then stream CSV off the UI thread."""
        if getattr(self, "_history_export_active", False):
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Export History Pro")
        dialog.geometry("460x245")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.configure(fg_color=BG_MAIN())
        ctk.CTkLabel(dialog, text="Exportă istoricul", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT_PRI()).pack(anchor="w", padx=22, pady=(20, 5))
        ctk.CTkLabel(dialog, text="Alege ce înregistrări să incluzi în fișierul CSV.",
                     font=ctk.CTkFont(size=12), text_color=TEXT_SEC()).pack(anchor="w", padx=22, pady=(0, 16))
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 10))
        def choose(mode):
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
            self._start_history_export(mode)
        ctk.CTkButton(buttons, text="Tot istoricul", command=lambda: choose("all"),
                      fg_color=ACCENT(), hover_color=shade_color(ACCENT(), 0.86), width=125).pack(side="left", padx=4, expand=True)
        ctk.CTkButton(buttons, text="Filtrate curente", command=lambda: choose("filtered"),
                      fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(), width=125).pack(side="left", padx=4, expand=True)
        selected_btn = ctk.CTkButton(buttons, text=f"Selectate ({len(self.selected_history_ids)})",
                      command=lambda: choose("selected"),
                      fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(), width=135,
                      state="normal" if self.selected_history_ids else "disabled")
        selected_btn.pack(side="left", padx=4, expand=True)
        ctk.CTkButton(dialog, text="Anulează", command=dialog.destroy, width=90,
                      fg_color="transparent", hover_color=BG_CARD_HOV(), text_color=TEXT_SEC()).pack(anchor="e", padx=22, pady=(0, 14))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.after(50, lambda: center_window(dialog, 460, 245))
        dialog.grab_set()

    def _start_history_export(self, mode):
        if getattr(self, "_history_export_active", False):
            return
        selected_ids = tuple(self.selected_history_ids) if mode == "selected" else ()
        if mode == "selected" and not selected_ids:
            messagebox.showinfo("Export CSV", "Selectează cel puțin o înregistrare din istoric.", parent=self)
            return
        file_path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("Fișiere CSV", "*.csv")],
            title="Salvează exportul de istoric")
        if not file_path:
            return

        self._history_export_active = True
        self._history_export_token += 1
        token = self._history_export_token
        self._history_export_cancel_event = threading.Event()
        self.history_export_btn.configure(state="disabled")

        progress_dialog = ctk.CTkToplevel(self)
        self._history_export_dialog = progress_dialog
        progress_dialog.title("Export în curs")
        progress_dialog.geometry("430x155")
        progress_dialog.resizable(False, False)
        progress_dialog.transient(self)
        progress_dialog.configure(fg_color=BG_MAIN())
        ctk.CTkLabel(progress_dialog, text="Se exportă istoricul…", font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=TEXT_PRI()).pack(anchor="w", padx=20, pady=(18, 6))
        self._history_export_message = ctk.CTkLabel(progress_dialog, text="Se pregătesc înregistrările…",
                     font=ctk.CTkFont(size=11), text_color=TEXT_SEC())
        self._history_export_message.pack(anchor="w", padx=20, pady=(0, 8))
        self._history_export_progress = ctk.CTkProgressBar(progress_dialog, mode="determinate", progress_color=ACCENT())
        self._history_export_progress.pack(fill="x", padx=20, pady=(0, 10))
        self._history_export_progress.set(0)
        self._history_export_cancel_btn = ctk.CTkButton(
            progress_dialog, text="Anulează exportul", width=125, height=30,
            fg_color=BG_CARD(), hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
            command=self._cancel_history_export)
        self._history_export_cancel_btn.pack(anchor="e", padx=20, pady=(0, 12))
        progress_dialog.protocol("WM_DELETE_WINDOW", self._cancel_history_export)
        progress_dialog.after(50, lambda: center_window(progress_dialog, 430, 155))
        progress_dialog.grab_set()

        search_term = self.history_search.get().strip()
        status_filter = self.history_filter_var.get()
        period_filter = self.history_period_var.get()
        type_filter = self.history_type_var.get()
        sort_order = self.history_sort_var.get()

        def report_progress(done, total):
            self._history_export_queue.put((token, "progress", int(done), int(total), None))

        def export_worker():
            try:
                result = self.db.export_history_csv(
                    file_path, mode=mode, search_term=search_term, status_filter=status_filter,
                    period_filter=period_filter, type_filter=type_filter, sort_order=sort_order,
                    selected_ids=selected_ids, cancel_event=self._history_export_cancel_event,
                    progress_callback=report_progress)
                if result.get("status") == "cancelled":
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                self._history_export_queue.put((token, "finished", result.get("count", 0), result.get("total", 0), result))
            except Exception as exc:
                logger.exception("Exportul CSV a eșuat")
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                self._history_export_queue.put((token, "error", 0, 0, str(exc)))

        threading.Thread(target=export_worker, name=f"HistoryExport-{token}", daemon=True).start()
        if self._history_export_poll_id is None:
            self._history_export_poll_id = self.after(100, self._poll_history_export)

    def _cancel_history_export(self):
        event = getattr(self, "_history_export_cancel_event", None)
        if event is not None:
            event.set()
        dialog = getattr(self, "_history_export_dialog", None)
        if dialog is not None:
            try:
                self._history_export_message.configure(text="Se anulează exportul…")
                self._history_export_cancel_btn.configure(state="disabled", text="Se anulează…")
            except (tk.TclError, AttributeError):
                pass

    def _poll_history_export(self):
        self._history_export_poll_id = None
        while True:
            try:
                token, kind, done, total, payload = self._history_export_queue.get_nowait()
            except queue.Empty:
                break
            if token != self._history_export_token:
                continue
            if kind == "progress":
                try:
                    self._history_export_progress.set(min(1.0, done / max(1, total)))
                    self._history_export_message.configure(
                        text=f"Exportate {done:,} din {total:,} înregistrări".replace(",", " "))
                except (tk.TclError, AttributeError):
                    pass
            elif kind in ("finished", "error"):
                self._finish_history_export(kind, done, total, payload)
        if self._history_export_active and not getattr(self, "_closing", False):
            self._history_export_poll_id = self.after(100, self._poll_history_export)

    def _finish_history_export(self, kind, done, total, payload):
        self._history_export_active = False
        self._history_export_cancel_event = None
        try:
            self.history_export_btn.configure(state="normal")
        except tk.TclError:
            pass
        dialog = getattr(self, "_history_export_dialog", None)
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        self._history_export_dialog = None
        if getattr(self, "_closing", False):
            return
        if kind == "error":
            messagebox.showerror("Export CSV", f"Exportul nu a reușit:\n{payload}", parent=self)
            return
        status = payload.get("status") if isinstance(payload, dict) else ""
        if status == "cancelled":
            messagebox.showinfo("Export CSV", "Exportul a fost anulat. Fișierul parțial a fost eliminat.", parent=self)
        elif status == "empty":
            messagebox.showinfo("Export CSV", "Nu există înregistrări pentru exportul ales.", parent=self)
        else:
            messagebox.showinfo("Export CSV", f"Export complet: {done:,} înregistrări.".replace(",", " "), parent=self)

    def _apply_queue_profile(self, profile: str):
        presets = {"Eco": 1, "Balanced": 3, "Performance": 5}
        if profile in presets:
            self.max_dl_e.delete(0, "end")
            self.max_dl_e.insert(0, str(presets[profile]))
            # Profilul trebuie să aibă efect imediat: altfel switching-ul între
            # Eco/Balanced/Performance nu schimbă nimic până la apăsarea pe
            # „Salvează", iar utilizatorul crede că setarea nu funcționează.
            self.max_concurrent = min(32, max(1, int(presets[profile])))
        self.queue_profile_var.set(profile)

    def _save_settings(self):
        try:
            self.max_concurrent = min(32, max(1, int(self.max_dl_e.get())))
            speed_limit = max(0, int(self.speed_e.get() or 0)) * 1024
            with self._bandwidth_lock:
                self._bandwidth_deadline = time.monotonic()
            for dl in self.downloads:
                dl.speed_limit = speed_limit  # legacy display/persistence field; per-download override stays intact
            try:
                segments_default = min(32, max(1, int(self.segments_e.get())))
            except ValueError:
                segments_default = 1
            # Toate setările se scriu într-o singură tranzacție: o salvare atomică
            # evită un config pe jumătate scris dacă aplicația se închide mid-scriere.
            settings = {
                "max_concurrent": self.max_concurrent,
                "speed_limit": speed_limit,
                "queue_profile": self.queue_profile_var.get(),
                "segments_default": segments_default,
                "theme_name": self.theme_var.get(),
                "theme_mode": self.theme_mode_var.get().lower(),
                "accent_color": self.accent_selector.current_color,
                "auto_sort": bool(self.auto_sort_var.get()),
                "clipboard_monitor": bool(self.clipboard_var.get()),
                "av_scan": bool(self.av_scan_var.get()),
                "notifications": bool(self.notify_var.get()),
                "minimize_to_tray": bool(self.tray_var.get()),
                "clear_on_exit": bool(self.clear_exit_var.get()),
                "animations": bool(self.anim_var.get()),
                "smart_retry_enabled": bool(self.smart_retry_var.get()),
                "smart_retry_max_attempts": min(8, max(0, int(self.smart_retry_attempts_e.get()))),
                "smart_retry_base_delay": min(120, max(1, int(self.smart_retry_delay_e.get()))),
                "smart_queue_aging_enabled": bool(self.smart_queue_aging_var.get()),
                "smart_queue_aging_minutes": min(1440, max(1, int(self.smart_queue_aging_e.get()))),
                "smart_disk_guard_enabled": bool(self.smart_disk_guard_var.get()),
                "smart_disk_min_free_mb": min(1024 * 1024, max(0, int(self.smart_disk_min_free_e.get()))),
                "adaptive_streams_enabled": bool(self.adaptive_streams_var.get()),
                "adaptive_stream_max": min(32, max(1, int(self.adaptive_stream_max_e.get()))),
                "endgame_enabled": bool(self.endgame_var.get()),
                "endgame_trigger_seconds": min(30, max(2, int(self.endgame_trigger_e.get()))),
                "slow_mode_limit_kbps": min(100000, max(1, int(self.slow_mode_limit_e.get()))),
            }
            # Proxy: o adresă invalidă NU e aplicată - altfel toate descărcările
            # ar eșua. Valoarea veche rămâne, iar utilizatorul primește avertisment.
            raw_proxy = ""
            try:
                raw_proxy = str(self.proxy_e.get() or "").strip()
            except Exception:
                raw_proxy = ""
            normalized_proxy = _normalize_proxy_url(raw_proxy)
            proxy_warning = ""
            if raw_proxy and not normalized_proxy:
                proxy_warning = ("Adresa de proxy nu e validă și a fost ignorată. "
                                 "Formate acceptate: http://host:port, socks5://host:port.")
                settings["proxy_url"] = str(self.config.get("proxy_url", "") or "")
            elif normalized_proxy.startswith(("socks5", "socks4")) and not _socks_proxy_available():
                proxy_warning = ("PySocks nu este instalat (pip install requests[socks]); "
                                 "proxy-ul SOCKS a fost ignorat.")
                settings["proxy_url"] = ""
            else:
                settings["proxy_url"] = normalized_proxy
            self.config.set_many(settings)
            # Proxy-ul se aplică imediat: sesiunea globală și sesiunile
            # adaptorului folosesc valoarea nouă fără repornirea aplicației.
            apply_proxy_to_session(SESSION, settings["proxy_url"])
            self.network_manager.apply_proxy(settings["proxy_url"])
            if self.clipboard_var.get():
                self.clipboard_monitor.start()
            else:
                self.clipboard_monitor.stop()
            if self.tray_var.get():
                self.tray_manager.setup()
            self._next_queued()
            self._update_status_bar(update_disk=True)
            if proxy_warning:
                messagebox.showwarning("Proxy", proxy_warning, parent=self._tab_frames["Settings"])
            else:
                messagebox.showinfo("Succes", "Setările au fost salvate.", parent=self._tab_frames["Settings"])
        except ValueError:
            messagebox.showerror("Eroare", "Introdu valori numerice valide.", parent=self._tab_frames["Settings"])

    def _cleanup_temp_files(self):
        """FEATURE NOU: curăță fișierele temporare orfane.

        O descărcare întreruptă sau anulată lasă în urmă segmente .part/.race pe
        care nimeni nu le mai folosește; la volum mare ocupă spațiu fără rost.
        Fișierele aparținând descărcărilor active sunt păstrate.
        """
        try:
            folders = set()
            default_folder = self.config.get("default_folder") or os.path.expanduser("~/Downloads")
            if default_folder:
                folders.add(os.path.abspath(default_folder))
            for dl in self.downloads:
                folder = os.path.dirname(dl.save_path or "")
                if folder:
                    folders.add(os.path.abspath(folder))
            protected = {os.path.basename(dl.save_path) for dl in self.downloads if dl.save_path}

            candidates = []
            for folder in sorted(folders):
                if not os.path.isdir(folder):
                    continue
                candidates.extend(scan_orphan_temp_files(folder, protected_bases=protected))

            if not candidates:
                messagebox.showinfo("Curățare", "Nu s-au găsit fișiere temporare orfane.",
                                    parent=self)
                return

            total_size = sum(item[1] for item in candidates)
            preview = "\n".join(f"· {os.path.basename(path)} ({fmt_size(size)})"
                                for path, size, _age in candidates[:12])
            if len(candidates) > 12:
                preview += f"\n… și încă {len(candidates) - 12} fișiere"
            if not messagebox.askyesno(
                    "Curățare fișiere temporare",
                    f"{len(candidates)} fișiere temporare orfane ({fmt_size(total_size)}).\n\n"
                    f"{preview}\n\nȘtergi aceste fișiere?", parent=self):
                return

            removed = 0
            freed = 0
            for path, size, _age in candidates:
                try:
                    os.remove(path)
                    removed += 1
                    freed += size
                except OSError as exc:
                    logger.warning("Nu am putut șterge fișierul temporar %s: %s", path, exc)
            messagebox.showinfo("Curățare",
                                f"{removed} fișiere șterse, {fmt_size(freed)} eliberați pe disc.",
                                parent=self)
        except Exception as exc:
            logger.exception("Curățarea fișierelor temporare a eșuat")
            messagebox.showerror("Curățare", f"Curățarea a eșuat:\n{exc}", parent=self)

    def _apply_update_event(self, kind, dl, extra, dirty):
        """Procesează un singur eveniment din coada worker.

        Întoarce (needs_stats, needs_filter, needs_queue). Excepțiile sunt
        tratate de apelant, astfel încât un eveniment defect nu mai aruncă
        restul lotului de evenimente din coadă.
        """
        needs_stats = needs_filter = needs_queue = False
        if kind == "ui":
            try:
                if callable(extra) and not getattr(self, "_closing", False):
                    extra()
            except Exception as exc:
                logger.debug("UI callback failed: %s", exc)
        elif kind == "refresh":
            if dl is not None:
                dirty[id(dl)] = dl
            needs_filter = True
        elif kind == "scan_warning":
            try:
                self._notify_scan_flag(dl, extra)
            except Exception as exc:
                logger.warning("Could not display Defender scan result: %s", exc)
        elif kind == "progress":
            if dl is not None:
                dirty[id(dl)] = dl
            needs_stats = True
        elif kind == "worker_finished":
            if dl is not None:
                # Status messages from the same worker precede worker_finished in the queue.
                if dl.status == DLStatus.DOWNLOADING:
                    if dl.stop_event and dl.stop_event.is_set():
                        dl.status = DLStatus.CANCELLED
                        dl.speed = 0.0
                        self._record_history_once(dl)
                    elif dl.pause_event and not dl.pause_event.is_set():
                        dl.status = DLStatus.PAUSED
                    else:
                        dl.error_msg = dl.error_msg or "Worker încheiat fără status final."
                        dl.status = DLStatus.ERROR
                        self._record_history_once(dl)
                dirty[id(dl)] = dl
                needs_stats = needs_filter = needs_queue = True
        elif kind == "status":
            if dl is None:
                return needs_stats, needs_filter, needs_queue
            # Give Smart Automation a chance to defer transient failures without
            # recording a terminal history row for an attempt that will be retried.
            if extra == DLStatus.ERROR and self.smart_automation.schedule_retry_if_transient(dl):
                needs_filter = needs_stats = needs_queue = True
                dirty[id(dl)] = dl
                return needs_stats, needs_filter, needs_queue
            # Terminal state updates are idempotent; repeated worker notices cannot duplicate history.
            dl.status = extra
            if extra in (DLStatus.COMPLETED, DLStatus.ERROR, DLStatus.CANCELLED):
                dl.speed = 0.0
                dl.auto_retry_at = 0.0
                if extra == DLStatus.COMPLETED:
                    dl.completed_date = dl.completed_date or datetime.now()
                    dl.automatic_retry_count = 0
                self._record_history_once(dl)
                self._reset_bandwidth_if_idle()
                needs_queue = True
            needs_filter = needs_stats = True
            dirty[id(dl)] = dl
            if extra == DLStatus.COMPLETED and NOTIFY_AVAILABLE and self.config.get("notifications", True):
                try:
                    notification.notify(title="Descărcare completă",
                        message=f"{dl.filename} a fost descărcat cu succes!",
                        app_name="CatsSoft Downloader", timeout=5)
                except Exception:
                    pass
        elif kind == "queue_changed":
            needs_queue = True
        return needs_stats, needs_filter, needs_queue

    def _poll_updates(self):
        """Consumă evenimente worker în batch-uri și actualizează fiecare componentă o singură dată per ciclu."""
        # Timerul care a declanșat acest apel s-a consumat; orice timer în
        # așteptare (de exemplu după un apel manual) trebuie anulat înainte de a
        # programa altul, altfel se acumulează bucle de polling paralele.
        pending_job = getattr(self, "_poll_job_id", None)
        self._poll_job_id = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        processed = 0
        max_per_cycle = 150
        dirty = {}
        needs_stats = False
        needs_filter = False
        needs_queue = False
        try:
            while processed < max_per_cycle:
                try:
                    kind, dl, extra = self.update_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                # Fiecare eveniment este izolat: o excepție într-un handler nu mai
                # aruncă restul lotului (cardurile rămase nu se mai actualizau).
                try:
                    flags = self._apply_update_event(kind, dl, extra, dirty)
                except Exception:
                    logger.exception("Eveniment UI ignorat: %s", kind)
                    continue
                needs_stats = needs_stats or flags[0]
                needs_filter = needs_filter or flags[1]
                needs_queue = needs_queue or flags[2]
        except Exception:
            logger.exception("Update queue processing failed")

        if getattr(self, "_closing", False):
            return
        try:
            if self.smart_automation.tick():
                needs_queue = needs_stats = needs_filter = True
                for item in self.downloads:
                    if item.status == DLStatus.QUEUED and getattr(item, "auto_retry_at", 0.0) == 0.0:
                        dirty[id(item)] = item
        except Exception:
            logger.exception("Smart Automation tick failed")
        for dirty_dl in dirty.values():
            self._refresh_card(dirty_dl)
            details = getattr(self, "details_view", None)
            if details is not None:
                try:
                    details.refresh_from_model(dirty_dl)
                except Exception:
                    pass
        if needs_stats:
            self._update_stats()
        if needs_filter:
            self._filter_cards()
        if needs_queue and getattr(self, "_current_tab", None) == "Queue":
            self._refresh_queue()
        # A worker that just finished may not have released its slot when the status event was processed.
        if any(d.status == DLStatus.QUEUED or (d.status == DLStatus.PAUSED and d.resume_requested)
               for d in self.downloads):
            self._next_queued()
        self._update_status_bar()
        if not getattr(self, "_closing", False) and self.winfo_exists():
            try:
                # Anulăm timerul anterior înainte de a programa unul nou: dacă
                # metoda este apelată și manual, nu se acumulează timer-uri multiple.
                if pending_job is not None:
                    try:
                        self.after_cancel(pending_job)
                    except tk.TclError:
                        pass
                self._poll_job_id = self.after(100, self._poll_updates)
            except tk.TclError:
                pass

    def _record_history_once(self, dl: Download):
        if getattr(dl, "history_recorded", False):
            return
        try:
            self.db.add_history(dl)
            dl.history_recorded = True
        except Exception:
            logger.exception("Could not save download history for %s", dl.filename)

    def post_ui(self, callback: Callable) -> None:
        """Queue callbacks for the Tk main thread; safe for workers/tray/scheduler threads."""
        if getattr(self, "_closing", False):
            return
        try:
            self.update_queue.put(("ui", None, callback))
        except Exception as exc:
            logger.debug("Ignoring UI callback during shutdown: %s", exc)

    def _on_close(self):
        # Closing the window hides it in the tray; background jobs must keep running.
        if (not getattr(self, "_closing", False)
                and self.config.get("minimize_to_tray", True)
                and TRAY_AVAILABLE and self.tray_manager.icon is not None):
            if self.tray_manager.minimize_to_tray():
                return
        self._exit_app()

    def _exit_app(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if getattr(self, "_history_export_cancel_event", None) is not None:
            self._history_export_cancel_event.set()
        for attr in ("_status_tick_id", "_nav_animation_id", "_active_queue_persist_id",
                     "_poll_job_id", "_appearance_poll_id", "_network_monitor_id"):
            job_id = getattr(self, attr, None)
            if job_id is not None:
                try:
                    self.after_cancel(job_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.db.save_active_queue(self.downloads)
        except Exception as exc:
            logger.error("Nu am putut persista coada la închidere: %s", exc)
        try:
            self.network_manager.persist_usage()
        except Exception:
            logger.debug("Network usage persistence failed", exc_info=True)
        # Cooperative stop: preserve partial files, wake paused threads and wait briefly.
        workers = []
        for dl in self.downloads:
            if dl.thread and dl.thread.is_alive():
                if dl.stop_event:
                    dl.stop_event.set()
                if dl.pause_event:
                    dl.pause_event.set()
                workers.append(dl.thread)
        deadline = time.monotonic() + 1.5
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                worker.join(timeout=min(0.25, remaining))
            except RuntimeError:
                pass
        self.clipboard_monitor.stop()
        self.scheduler.stop()
        try:
            self.network_manager.close_sessions()
        except Exception:
            logger.debug("Network session cleanup failed", exc_info=True)
        try:
            # Persist only active/queued/paused states; worker status events may still be waiting in the queue.
            self.db.save_active_queue(self.downloads)
        except Exception as exc:
            logger.error("Final queue snapshot failed: %s", exc)
        try:
            if self.config.get("clear_on_exit", False):
                self._clear_done()
            self.config.save()
        except Exception as exc:
            logger.error("Nu am putut salva setările la închidere: %s", exc)
        self.tray_manager.stop()
        self.destroy()

    def destroy(self):
        self._closing = True
        # Anulăm TOATE timerele proprii, nu doar pe cele de istoric: altfel un
        # callback `after` în așteptare arde după distrugerea ferestrei și Tk
        # afișează "invalid command name ..._poll_updates".
        for attr in ("_history_query_poll_id", "_history_export_poll_id", "_history_filter_after_id",
                     "_history_render_after_id", "_status_tick_id", "_nav_animation_id",
                     "_active_queue_persist_id", "_poll_job_id", "_appearance_poll_id",
                     "_network_monitor_id"):
            callback_id = getattr(self, attr, None)
            if callback_id:
                try:
                    self.after_cancel(callback_id)
                except (tk.TclError, RuntimeError):
                    pass
                setattr(self, attr, None)
        # NU anulăm timerele globale prin `after info`: lista returnată conține
        # TOKEN-urile comenzilor (nu id-urile), iar `after cancel <token>` șterge
        # comanda Tcl a callbackului. Tkinter o păstrează totuși în
        # widget._tclCommands, iar destroy() ulterior ar eșua cu
        # "can't delete Tcl command". Timerele proprii sunt anulate mai sus,
        # după id, prin atributele urmărite.
        # Monitorul de clipboard are propriul timer `after`; fără oprirea lui,
        # callbackul arde după distrugerea ferestrei (TclError "invalid command").
        try:
            self.clipboard_monitor.stop()
        except Exception:
            pass
        # Închidem conexiunea SQLite persistentă (altfel rămâne deschisă până
        # la ieșirea procesului).
        try:
            self.db.close()
        except Exception:
            pass
        _prune_dead_commands(self)
        if getattr(self, "_history_export_cancel_event", None) is not None:
            self._history_export_cancel_event.set()
        try:
            THEME.unregister_window(self)
            THEME.remove_listener(self)
        except Exception:
            pass
        super().destroy()
