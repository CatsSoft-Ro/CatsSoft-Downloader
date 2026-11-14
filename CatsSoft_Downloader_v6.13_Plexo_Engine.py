
#-------------------------------------------------------------------------------
# CatsSoft Downloader v6.13 - Adaptive Plexo-Inspired Engine
# Features: Thread-Safe, Proper Error Detection, Segment Validation, No Memory Leaks
# Changelog v6.13: reluare pe retry, chunk adaptiv în segmente, conexiune SQLite
# reutilizată, salvare atomică a setărilor, ETA pe card, curățare fișiere orfane.
# Author: Andruska
# Licence: MIT
#-------------------------------------------------------------------------------

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from urllib.parse import urlparse, parse_qs, unquote
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List
from datetime import datetime, timedelta
from PIL import Image, ImageDraw
import sqlite3
import json
from pathlib import Path
import logging
import weakref
import re
import math

# Configure logging
# Consolele Windows folosesc frecvent cp1250/cp1252, care nu conțin litera „ț"
# (U+021B). Fără reconfigurare, fiecare mesaj în română se pierde cu
# „--- Logging error ---" în loc să fie afișat.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# NOTĂ: Pillow (PIL) e o dependență obligatorie (iconițe, tray, sparkline) — importul de mai sus
# nu e opțional, deci nu mai există un fallback real "fără PIL". PIL_OK a fost eliminat.


def _cancel_pending_after(widget):
    """Anulează timerele ``after`` încă pendente ale acestui widget.

    Un timer care arde după distrugerea widgetului face tkinter să apeleze
    ``deletecommand`` pe un widget deja distrus: ``Misc.destroy`` a setat
    ``_tclCommands = None``, iar ``deletecommand`` face ``self._tclCommands.remove(name)``
    → ``AttributeError: 'NoneType' object has no attribute 'remove'``.
    Tkinter 3.10 prinde acolo doar ``TclError``, deci excepția iese ca
    „Exception in Tkinter callback" și lasă urme în consolă.

    Selectăm doar timerele al căror nume de comandă aparține widgetului
    (tkinter înregistrează comanda în ``widget._tclCommands``), deci nu atingem
    timerele altor widgeturi.
    """
    try:
        commands = set(getattr(widget, "_tclCommands", None) or [])
    except Exception:
        return
    if not commands:
        return
    try:
        event_ids = list(widget.tk.call("after", "info"))
    except Exception:
        return
    for event_id in event_ids:
        try:
            # `after info <id>` întoarce perechea (comandă, tip). Comanda este
            # numele Tcl înregistrat de tkinter în widget._tclCommands.
            info = widget.tk.call("after", "info", event_id)
            script = info[0] if isinstance(info, (tuple, list)) else info
        except Exception:
            continue
        if str(script) in commands:
            try:
                widget.tk.call("after", "cancel", event_id)
            except Exception:
                pass


def _cancel_pending_after_tree(widget):
    """Anulează timerele ``after`` ale întregului subarbore al widgetului.

    Tk distruge descendenții unui widget la nivel Tcl, **fără** să apeleze
    metoda Python ``destroy()`` a lor. Așadar, patch-ul de destroy nu ajunge
    la canvas-urile CustomTkinter: fiecare ``CTkButton`` își desenează starea
    printr-un ``after`` înregistrat pe canvas, iar acel timer arde după
    distrugere → „Exception in Tkinter callback: invalid command name
    ...!ctkcanvas". La închiderea ferestrei principale se înregistrau sute de
    astfel de urme.

    Parcurgem subarborele o singură dată și reîinem comenzile Tcl ale tuturor
    widgeturilor, apoi anulăm timerele care le corespund — costă O(widgeturi +
    timeri), nu O(widgeturi × timeri).
    """
    commands = set()
    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            commands.update(getattr(current, "_tclCommands", None) or [])
        except Exception:
            pass
        try:
            stack.extend(current.winfo_children())
        except Exception:
            pass
    if not commands:
        return
    try:
        event_ids = list(widget.tk.call("after", "info"))
    except Exception:
        return
    for event_id in event_ids:
        try:
            info = widget.tk.call("after", "info", event_id)
            script = info[0] if isinstance(info, (tuple, list)) else info
        except Exception:
            continue
        if str(script) in commands:
            try:
                widget.tk.call("after", "cancel", event_id)
            except Exception:
                pass


def _prune_dead_commands(widget):
    """Elimină din ``widget._tclCommands`` comenzile Tcl care nu mai există.

    Tk șterge comanda unui callback atunci când timerul lui ``after`` este
    anulat, dar tkinter păstrează numele în ``_tclCommands``. La distrugerea
    widgetului, ``Misc.destroy`` încearcă să ștergă comanda deja dispărută și
    Tk aruncă ``can't delete Tcl command``. Curățăm lista înainte de
    ``super().destroy()``.

    Anulăm și timerele ``after`` încă active: altfel ele ard după distrugerea
    widgetului și lasă „Exception in Tkinter callback" în consolă.
    """
    _cancel_pending_after(widget)
    try:
        commands = getattr(widget, "_tclCommands", None)
        if not commands:
            return
        alive = []
        for name in list(commands):
            try:
                # `info commands` tratează argumentul ca glob, deci verificăm
                # potrivirea exactă a numelui.
                found = str(widget.tk.call("info", "commands", name)).split()
                if name in found:
                    alive.append(name)
            except Exception:
                alive.append(name)
        widget._tclCommands = alive
    except Exception:
        pass


def _patch_customtkinter_destroy():
    """Anulează timerele interne ale CustomTkinter înainte de distrugere.

    CustomTkinter programează un ``after`` propriu (setarea iconiței pe
    Windows). Timerul supraviețuiește distrugerii widgetului, iar când arde:
    - Tk afișează „invalid command name ..._windows_set_titlebar_icon";
    - tkinterul apelează ``deletecommand`` pe un widget distrus, unde
      ``Misc.destroy`` a setat ``_tclCommands = None`` → ``AttributeError:
      'NoneType' object has no attribute 'remove'`` (3.10 prinde doar TclError).

    Ambele sunt doar zgomot în consolă, dar apar la fiecare închidere de
    dialog, deci merită eliminate.

    IMPORTANT: ``CTkToplevel`` NU moștenește de la ``CTkBaseClass``, ci de la
    ``tkinter.Toplevel`` - altfel dialogurile (AddURLDialog, ResumeQueueDialog,
    toast-ul de clipboard) ar rămâne neacoperite. Un timer ``after`` rămas pe
    un Toplevel distrus nu provoacă doar o excepție Python: la următorul
    ``update()`` (CustomTkinter îl apelează chiar în constructorul fiecărei
    ferestre) Tcl încearcă să execute comanda ștearsă și procesul moare cu
    „access violation" - un crash dur, nu o excepție prinsă.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return

    def _make_patched(original):
        def destroy(self, *args, **kwargs):
            # Subarborele întreg: Tk distruge descendenții la nivel Tcl, fără să
            # apeleze destroy() Python al lor, deci timerele lor ar arde oricum.
            _cancel_pending_after_tree(self)
            _prune_dead_commands(self)
            return original(self, *args, **kwargs)
        destroy._catssoft_patched = True
        return destroy

    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None),
               getattr(ctk, "CTkToplevel", None)]
    try:
        import tkinter as _tk
        holders.append(_tk.Toplevel)
    except Exception:
        pass
    for holder in holders:
        if holder is None:
            continue
        original = holder.__dict__.get("destroy")
        if original is None or getattr(original, "_catssoft_patched", False):
            continue
        try:
            holder.destroy = _make_patched(original)
        except Exception:
            continue


def _patch_tkinter_deletecommand():
    """Tolerează widgeturi deja distruse în ``Misc.deletecommand``.

    ``Misc.after`` înconjoară apelul în ``try/finally``, iar ramura ``finally``
    face ``self.deletecommand(name)``. ``deletecommand`` presupune că
    ``_tclCommands`` mai există - dar ``Misc.destroy`` îl pune ``None``.

    Rezultat: la fiecare închidere a aplicației dintr-un timer (de exemplu
    ``after(8000, app.destroy)``) tkinter tipărește „Exception in Tkinter
    callback" cu „AttributeError: 'NoneType' object has no attribute 'remove'".
    Tkinter prinde acolo doar ``TclError``, deci excepția scapă.

    Comportamentul este identic cu cel original, doar că nu mai aruncă dacă
    widgetul a fost distrus între timp.
    """
    try:
        import tkinter
    except Exception:
        return
    original = getattr(tkinter.Misc, "deletecommand", None)
    if original is None or getattr(original, "_catssoft_patched", False):
        return

    def deletecommand(self, name):
        try:
            self.tk.deletecommand(name)
        except Exception:
            pass
        commands = getattr(self, "_tclCommands", None)
        if not commands:
            return
        try:
            commands.remove(name)
        except ValueError:
            pass

    deletecommand._catssoft_patched = True
    try:
        tkinter.Misc.deletecommand = deletecommand
    except Exception:
        pass


def _patch_customtkinter_configure_handler():
    """Evită zgomotul „invalid command name ...!ctkcanvas" la distrugere.

    CustomTkinter își desenează starea în handler-ul evenimentului ``<Configure>``
    (``CTkBaseClass._update_dimensions_event``). Evenimentul e pus în coadă și
    ajunge la widget **după** ce canvas-ul lui a fost distrus - iar atunci
    ``self._canvas.delete(...)`` aruncă ``TclError: invalid command name
    ...!ctkcanvas``, prinsă de tkinter ca „Exception in Tkinter callback".

    La închiderea ferestrei principale se înregistrau sute de asemenea urme în
    consolă (măsurat: 373 pe v6.13, 173 pe v6.12 - deci e o problemă veche,
    nu o regresie). Patch-ul verifică existența canvas-ului înainte de desenare.

    Nu schimbă comportamentul normal: cât timp canvas-ul există, desenarea se
    face exact ca înainte.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return
    base = getattr(ctk, "CTkBaseClass", None)
    if base is None:
        return
    original = getattr(base, "_update_dimensions_event", None)
    if original is None or getattr(original, "_catssoft_patched", False):
        return

    def _update_dimensions_event(self, event=None):
        canvas = getattr(self, "_canvas", None)
        if canvas is not None:
            try:
                if not canvas.winfo_exists():
                    return None
            except Exception:
                return None
        return original(self, event)

    _update_dimensions_event._catssoft_patched = True
    try:
        base._update_dimensions_event = _update_dimensions_event
    except Exception:
        pass


# Se aplică la import, nu doar la pornire: aplicația poate fi folosită și ca
# modul (teste, integrare).
_patch_customtkinter_destroy()
_patch_tkinter_deletecommand()
_patch_customtkinter_configure_handler()

try:
    import pystray
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

try:
    from plyer import notification
    NOTIFY_AVAILABLE = True
except ImportError:
    NOTIFY_AVAILABLE = False

# ─── HTTP Session partajat (OPTIMIZARE: reutilizare conexiuni keep-alive) ──────
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
SESSION.headers.update({"User-Agent": USER_AGENT})
# Keep Content-Length and streamed bytes comparable (avoid transparent gzip decoding).
SESSION.headers.update({"Accept-Encoding": "identity"})


# ─── Proxy (HTTP / HTTPS / SOCKS5) ───────────────────────────────────────────
_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def _normalize_proxy_url(url: str) -> str:
    """Normalizează adresa de proxy. Întoarce ``""`` dacă nu e folosibilă.

    Acceptă şi forma scurtă (``host:port``), căreia i se prefixează ``http://``.
    Sunt acceptate numai schemele de proxy cunoscute; orice altceva e ignorat
    (mai bine fără proxy decât cu un proxy invalid care ar strica toate
    descărcările).
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    scheme = (parsed.scheme or "").lower()
    if scheme not in _PROXY_SCHEMES:
        return ""
    try:
        if not parsed.hostname:
            return ""
    except Exception:
        return ""
    return "%s://%s" % (scheme, parsed.netloc)


def _socks_proxy_available() -> bool:
    """PySocks (``requests[socks]``) e necesar pentru proxy-uri SOCKS5."""
    try:
        import socks  # noqa: F401
        return True
    except Exception:
        return False


def apply_proxy_to_session(session, proxy_url: str) -> bool:
    """Aplică proxy-ul configurat pe o sesiune ``requests``.

    Întoarce ``True`` dacă un proxy a fost configurat. Pentru SOCKS5 fără
    PySocks sesiunea rămâne fără proxy - altfel fiecare cerere ar eșua cu
    ``InvalidSchema`` în loc să meargă direct.
    """
    normalized = _normalize_proxy_url(proxy_url)
    if not normalized:
        try:
            session.proxies.clear()
        except Exception:
            pass
        return False
    if normalized.startswith(("socks5", "socks4")) and not _socks_proxy_available():
        logger.warning("Proxy SOCKS ignorat: PySocks (requests[socks]) lipsește")
        try:
            session.proxies.clear()
        except Exception:
            pass
        return False
    try:
        session.proxies.clear()
        session.proxies.update({"http": normalized, "https": normalized})
    except Exception as exc:
        logger.warning("Proxy nu a putut fi aplicat: %s", exc)
        return False
    logger.info("Proxy aplicat: %s", normalized)
    return True


class SourceBoundHTTPAdapter(requests.adapters.HTTPAdapter):
    """HTTP adapter that binds its sockets to a selected local IPv4 address.

    On Windows, source binding is best-effort: the OS routing table still decides
    the route to the remote host. The UI therefore treats profiles as eligible
    source bindings, not as a guarantee that two adapters have independent ISPs.
    """
    def __init__(self, source_ip: str, *args, **kwargs):
        self.source_ip = str(source_ip)
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["source_address"] = (self.source_ip, 0)
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


class NetworkAdapterManager:
    """Discovers local IPv4 adapters and distributes HTTP range jobs between them.

    Multi-network mode is opt-in. If adapter discovery or source binding fails,
    workers can fall back to the normal system session and retry another profile.
    """
    # Numărul de eșecuri de transport consecutive după care traseul este scos
    # din rotație (până la următoarea reîmprospătare).
    ERROR_DISABLE_THRESHOLD = 1
    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._profiles = {}
        self._selected = set(str(x) for x in (config.get("multi_network_selected", []) or []))
        limits = config.get("network_speed_limits", {})
        self._limits = dict(limits) if isinstance(limits, dict) else {}
        self._cursor = 0
        self._last_persisted_at = 0.0
        self._usage = config.get("network_usage", {})
        if not isinstance(self._usage, dict):
            self._usage = {}
        self._deadlines = {}

    @staticmethod
    def _valid_ipv4(ip):
        try:
            parsed = ipaddress.ip_address(str(ip))
            return parsed.version == 4 and not parsed.is_loopback and not parsed.is_link_local and not parsed.is_multicast and not parsed.is_unspecified
        except Exception:
            return False

    def _discover(self):
        found = []
        try:
            import psutil  # optional; the downloader does not require it for normal downloads
            addresses = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for name, items in addresses.items():
                stat = stats.get(name)
                if stat is not None and not stat.isup:
                    continue
                for item in items:
                    if getattr(item, "family", None) == socket.AF_INET and self._valid_ipv4(item.address):
                        found.append((str(name), str(item.address)))
        except Exception as exc:
            logger.debug("psutil adapter discovery unavailable: %s", exc)

        if not found and sys.platform == "win32":
            command = ("Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                       "Where-Object {$_.AddressState -eq 'Preferred'} | "
                       "Select-Object InterfaceAlias,IPAddress | ConvertTo-Json -Compress")
            try:
                result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                        capture_output=True, text=True, timeout=5, check=False)
                payload = json.loads(result.stdout or "[]")
                if isinstance(payload, dict):
                    payload = [payload]
                for item in payload if isinstance(payload, list) else []:
                    ip = str(item.get("IPAddress", ""))
                    name = str(item.get("InterfaceAlias", "Adapter"))
                    if self._valid_ipv4(ip):
                        found.append((name, ip))
            except Exception as exc:
                logger.debug("PowerShell adapter discovery failed: %s", exc)

        if not found:
            try:
                for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
                    ip = item[4][0]
                    if self._valid_ipv4(ip):
                        found.append(("Detected adapter", ip))
            except OSError:
                pass

        # Avoid duplicate alias/IP pairs and nondeterministic UI order.
        unique = {(name, ip) for name, ip in found}
        return sorted(unique, key=lambda pair: (pair[0].casefold(), pair[1]))

    @staticmethod
    def _make_session(source_ip, proxy_url: str = ""):
        session = requests.Session()
        adapter = SourceBoundHTTPAdapter(source_ip, pool_connections=8, pool_maxsize=16, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        apply_proxy_to_session(session, proxy_url)
        return session

    def apply_proxy(self, proxy_url: str = "") -> int:
        """Aplică proxy-ul pe sesiunile deja create.

        Sesiunile de adaptor sunt create la descoperirea adaptoarelor; fără
        această metodă, schimbarea proxy-ului ar necesita repornirea aplicației.
        Întoarce numărul de sesiuni actualizate.
        """
        updated = 0
        with self._lock:
            for profile in self._profiles.values():
                session = profile.get("session")
                if session is None:
                    continue
                apply_proxy_to_session(session, proxy_url)
                updated += 1
        return updated

    def refresh(self):
        discovered = self._discover()
        with self._lock:
            # Keep intent by adapter alias when DHCP changes its IPv4 address.
            original_selected = set(self._selected)
            original_limits = dict(self._limits)
            selected_names = {str(key).rsplit("|", 1)[0] for key in self._selected if "|" in str(key)}
            old_selected_profiles = {p["name"]: p for key, p in self._profiles.items() if key in self._selected}
            discovered_names = {name for name, _ip in discovered}
            remappable_names = selected_names & discovered_names
            discovered_selected_keys = set()
            for profile in self._profiles.values():
                profile["available"] = False
            for name, ip in discovered:
                key = f"{name}|{ip}"
                if name in selected_names:
                    discovered_selected_keys.add(key)
                    old_profile = old_selected_profiles.get(name)
                    if old_profile is not None and key not in self._limits:
                        old_limit = max(0, safe_int(old_profile.get("limit_bps", 0), 0))
                        if old_limit:
                            self._limits[key] = old_limit
                profile = self._profiles.get(key)
                if profile is None:
                    usage = self._usage.get(key, {}) if isinstance(self._usage, dict) else {}
                    if not isinstance(usage, dict):
                        usage = {}
                    profile = {
                        "key": key, "name": name, "ip": ip, "available": True,
                        "session": self._make_session(ip, self.config.get("proxy_url", "")), "active_streams": 0,
                        "bytes_session": 0, "samples": [], "speed_bps": 0.0,
                        "errors": 0, "cooldown_until": 0.0,
                        "consecutive_failures": 0,
                        "missing_refreshes": 0,
                        "limit_bps": max(0, safe_int(self._limits.get(key, 0), 0)),
                        "total_bytes": max(0, safe_int(usage.get("total_bytes", 0), 0)),
                        "throttle_deadline": time.monotonic(),
                    }
                    self._profiles[key] = profile
                profile["name"] = name
                profile["ip"] = ip
                profile["available"] = True
                # Reîmprospătarea revalidatează traseul: un adaptor scos din
                # rotație pentru erori de transport primește din nou o șansă.
                profile["consecutive_failures"] = 0
                profile["limit_bps"] = max(0, safe_int(self._limits.get(key, profile.get("limit_bps", 0)), 0))
                if float(profile.get("cooldown_until", 0.0)) <= time.monotonic():
                    profile["cooldown_until"] = 0.0
            # Evictăm profilurile dispărute de mai multe cicluri: fiecare ține o
            # sesiune HTTP deschisă, iar schimbările de IP (DHCP) lăsau sesiuni
            # orfane pentru totdeauna (scurgere de file descriptors).
            for key in list(self._profiles.keys()):
                profile = self._profiles.get(key)
                if profile is None:
                    continue
                if profile.get("available"):
                    profile["missing_refreshes"] = 0
                    continue
                profile["missing_refreshes"] = int(profile.get("missing_refreshes", 0)) + 1
                if profile["missing_refreshes"] < 3 or int(profile.get("active_streams", 0)) > 0:
                    continue
                self._profiles.pop(key, None)
                session = profile.get("session")
                if session is not None:
                    try:
                        session.close()
                    except Exception as exc:
                        logger.debug("Could not close stale network session: %s", exc)
            if selected_names:
                # Replace stale selected addresses for these adapter aliases with the
                # currently discovered addresses; this supports DHCP/IP changes cleanly.
                # Keep offline aliases selected until they reappear; only replace stale
                # IP keys for aliases that were actually rediscovered in this refresh.
                self._selected = {key for key in self._selected
                                  if key.rsplit("|", 1)[0] not in remappable_names}
                self._selected.update(discovered_selected_keys)
            if self._selected != original_selected:
                self.config.set("multi_network_selected", sorted(self._selected))
            if self._limits != original_limits:
                self.config.set("network_speed_limits", dict(self._limits))
            return self.snapshot()

    def refresh_async(self, callback=None):
        with self._refresh_lock:
            if self._refreshing:
                return False
            self._refreshing = True
        def worker():
            try:
                self.refresh()
            except Exception:
                logger.exception("Network adapter refresh failed")
            finally:
                with self._refresh_lock:
                    self._refreshing = False
                if callback:
                    try:
                        callback()
                    except Exception:
                        logger.debug("Network refresh callback failed", exc_info=True)
        threading.Thread(target=worker, name="CatsSoft-Network-Refresh", daemon=True).start()
        return True

    def snapshot(self):
        with self._lock:
            values = []
            for key, p in self._profiles.items():
                values.append({
                    "key": key, "name": p["name"], "ip": p["ip"],
                    "available": bool(p["available"]), "selected": key in self._selected,
                    "active_streams": int(p["active_streams"]),
                    "bytes_session": int(p["bytes_session"]),
                    "total_bytes": int(p.get("total_bytes", 0)),
                    "speed_bps": (max(0.0, float(p.get("speed_bps", 0.0)))
                                  if p.get("samples") and time.monotonic() - p["samples"][-1][0] <= 1.8 else 0.0),
                    "errors": int(p.get("errors", 0)),
                    "limit_bps": max(0, int(p.get("limit_bps", 0))),
                })
            return sorted(values, key=lambda p: (not p["available"], p["name"].casefold(), p["ip"]))

    def set_multi_enabled(self, enabled):
        self.config.set("multi_network_enabled", bool(enabled))

    def multi_enabled(self):
        return bool(self.config.get("multi_network_enabled", False))

    def set_selected(self, keys):
        clean = sorted(set(str(k) for k in keys))
        with self._lock:
            self._selected = set(clean)
        self.config.set("multi_network_selected", clean)

    def set_limit(self, key, bytes_per_second):
        limit = max(0, safe_int(bytes_per_second, 0))
        with self._lock:
            self._limits[str(key)] = limit
            if str(key) in self._profiles:
                self._profiles[str(key)]["limit_bps"] = limit
        self.config.set("network_speed_limits", dict(self._limits))

    def acquire_session(self):
        if not self.multi_enabled():
            return "system", SESSION
        now = time.monotonic()
        with self._lock:
            candidates = [p for k, p in self._profiles.items()
                          if k in self._selected and p["available"] and p["cooldown_until"] <= now]
            if not candidates:
                # Niciun traseu selectat nu mai este utilizabil (adaptor mort,
                # în cooldown sau scos din rotație): revenim pe sesiunea de
                # sistem, care funcționează întotdeauna.
                return "system", SESSION
            # Prefer less-loaded profiles; throughput gradually weights faster paths.
            def score(profile):
                weight = max(128 * 1024.0, float(profile.get("speed_bps", 0.0))) ** 0.5
                return (profile["active_streams"] / weight, profile["active_streams"], profile["name"].casefold())
            candidates.sort(key=score)
            best_score = score(candidates[0])
            tied = [p for p in candidates if abs(score(p)[0] - best_score[0]) < 1e-12 and score(p)[1] == best_score[1]]
            if len(tied) > 1:
                profile = tied[self._cursor % len(tied)]
                self._cursor += 1
            else:
                profile = candidates[0]
            profile["active_streams"] += 1
            return profile["key"], profile["session"]

    def release_session(self, key):
        if not key or key == "system":
            return
        with self._lock:
            profile = self._profiles.get(key)
            if profile:
                profile["active_streams"] = max(0, profile["active_streams"] - 1)

    def report_bytes(self, key, byte_count):
        if not key or key == "system" or byte_count <= 0:
            return
        now = time.monotonic()
        with self._lock:
            profile = self._profiles.get(key)
            if not profile:
                return
            profile["bytes_session"] += int(byte_count)
            profile["total_bytes"] += int(byte_count)
            # Octeți transferați cu succes dovedesc că traseul e funcțional:
            # resetăm contorul de eșecuri consecutive.
            profile["consecutive_failures"] = 0
            profile["samples"].append((now, int(byte_count)))
            cutoff = now - 5.0
            profile["samples"] = [(t, n) for t, n in profile["samples"] if t >= cutoff]
            if len(profile["samples"]) >= 2:
                elapsed = max(0.1, profile["samples"][-1][0] - profile["samples"][0][0])
                profile["speed_bps"] = sum(n for _, n in profile["samples"]) / elapsed
            elif profile["samples"]:
                profile["speed_bps"] = profile["samples"][-1][1]

    def report_failure(self, key, exc=None):
        if not key or key == "system":
            return
        with self._lock:
            profile = self._profiles.get(key)
            if profile:
                profile["errors"] += 1
                profile["cooldown_until"] = time.monotonic() + 5.0
                profile["consecutive_failures"] = int(profile.get("consecutive_failures", 0)) + 1
                logger.warning("Network path %s cooling down after transfer error: %s", key, exc)
                # Un adaptor care ratează transferul (de exemplu un switch virtual
                # fără rută - bind-ul eșuează instant) nu trebuie realesat la fiecare
                # segment: ar consuma bugetul de reîncercări al segmentului și ar
                # face descărcarea să eșueze chiar dacă există o cale funcțională.
                # Îl scoatem din rotație; acquire_session() revine astfel pe calea
                # sănătoasă sau pe sesiunea de sistem, iar reîmprospătarea îl
                # revalidează.
                if profile["consecutive_failures"] >= self.ERROR_DISABLE_THRESHOLD:
                    if profile.get("available"):
                        logger.warning("Network path %s removed from rotation after %s failed transfer(s): %s",
                                       key, profile["consecutive_failures"], exc)
                    profile["available"] = False
                    profile["cooldown_until"] = 0.0

    def throttle_wait(self, key, byte_count):
        if not key or key == "system" or byte_count <= 0:
            return 0.0
        with self._lock:
            profile = self._profiles.get(key)
            limit = max(0, safe_int(profile.get("limit_bps", 0), 0)) if profile else 0
            if not profile or limit <= 0:
                return 0.0
            now = time.monotonic()
            deadline = max(now, float(profile.get("throttle_deadline", now)))
            wait_for = max(0.0, deadline - now)
            profile["throttle_deadline"] = deadline + byte_count / limit
            return wait_for

    def persist_usage(self):
        with self._lock:
            usage = dict(self._usage)
            for key, profile in self._profiles.items():
                usage[key] = {"total_bytes": int(profile.get("total_bytes", 0))}
            self._usage = usage
        try:
            self.config.set("network_usage", usage)
        except Exception:
            logger.debug("Could not persist per-network usage", exc_info=True)

    def close_sessions(self):
        """Release HTTP pools owned by source-bound adapter profiles during shutdown."""
        with self._lock:
            sessions = [p.get("session") for p in self._profiles.values() if p.get("session") is not None]
        for session in sessions:
            try:
                session.close()
            except Exception:
                logger.debug("Could not close a network HTTP session", exc_info=True)



# ═════════════════════════════════════════════════════════════
# THEME SYSTEM (FIX #5 - WeakRef pentru Memory Leak)
# ═════════════════════════════════════════════════════════════

class AccentColor(Enum):
    RED = ("#ef4444", "Roșu")
    CRIMSON = ("#dc2626", "Crimson")
    ROSE = ("#f43f5e", "Rose")
    PINK = ("#ec4899", "Roz")
    ORANGE = ("#f97316", "Portocaliu")
    CORAL = ("#ff7f50", "Coral")
    AMBER = ("#f59e0b", "Chihlimbar")
    GOLD = ("#eab308", "Auriu")
    LIME = ("#84cc16", "Lime")
    GREEN = ("#10b981", "Smarald")
    MINT = ("#14b8a6", "Mentă")
    TEAL = ("#0d9488", "Teal")
    CYAN = ("#06b6d4", "Cyan")
    SKY = ("#0ea5e9", "Albastru cer")
    BLUE = ("#3b82f6", "Albastru")
    INDIGO = ("#6366f1", "Indigo")
    VIOLET = ("#8b5cf6", "Violet")
    MAGENTA = ("#d946ef", "Magenta")
    PURPLE = ("#a855f7", "Mov")
    LAVENDER = ("#c084fc", "Lavandă")
    NEON_RED = ("#ff003c", "Roșu neon")
    NEON_GREEN = ("#39ff14", "Verde neon")
    NEON_BLUE = ("#00f0ff", "Albastru neon")
    SLATE = ("#64748b", "Slate")

    @property
    def hex(self) -> str:
        return self.value[0]

    @property
    def name_local(self) -> str:
        return self.value[1]


class ColorTheme:
    def __init__(
        self, name: str, bg_main: str, bg_sidebar: str, bg_card: str,
        bg_card_hover: str, bg_nav: str, bg_input: str, border: str,
        text_primary: str, text_secondary: str, text_dim: str,
        accent: str = "#3b82f6", success: str = "#22c55e",
        warning: str = "#f59e0b", error: str = "#ef4444",
    ):
        self.name = name
        self.bg_main = bg_main
        self.bg_sidebar = bg_sidebar
        self.bg_card = bg_card
        self.bg_card_hover = bg_card_hover
        self.bg_nav = bg_nav
        self.bg_input = bg_input
        self.border = border
        self.text_primary = text_primary
        self.text_secondary = text_secondary
        self.text_dim = text_dim
        self.accent = accent
        self.success = success
        self.warning = warning
        self.error = error

    def with_accent(self, accent: str) -> "ColorTheme":
        return ColorTheme(
            name=self.name, bg_main=self.bg_main, bg_sidebar=self.bg_sidebar,
            bg_card=self.bg_card, bg_card_hover=self.bg_card_hover,
            bg_nav=self.bg_nav, bg_input=self.bg_input, border=self.border,
            text_primary=self.text_primary, text_secondary=self.text_secondary,
            text_dim=self.text_dim, accent=accent, success=self.success,
            warning=self.warning, error=self.error,
        )


class ThemePresets:
    @staticmethod
    def dark_modern() -> ColorTheme:
        return ColorTheme(
            name="Dark Modern", bg_main="#1a1a1a", bg_sidebar="#141414",
            bg_card="#242424", bg_card_hover="#2c2c2c", bg_nav="#1e1e1e",
            bg_input="#2a2a2a", border="#333333", text_primary="#f1f5f9",
            text_secondary="#94a3b8", text_dim="#64748b", accent="#3b82f6",
        )

    @staticmethod
    def dark_macos() -> ColorTheme:
        return ColorTheme(
            name="Dark macOS", bg_main="#1c1c1e", bg_sidebar="#151516",
            bg_card="#2c2c2e", bg_card_hover="#38383a", bg_nav="#1c1c1e",
            bg_input="#252527", border="#3a3a3c", text_primary="#f2f2f7",
            text_secondary="#8e8e93", text_dim="#48484a", accent="#007AFF",
        )

    @staticmethod
    def light_modern() -> ColorTheme:
        return ColorTheme(
            name="Light Modern", bg_main="#f5f5f7", bg_sidebar="#ffffff",
            bg_card="#ffffff", bg_card_hover="#f0f0f2", bg_nav="#ffffff",
            bg_input="#f0f0f2", border="#d1d1d6", text_primary="#1d1d1f",
            text_secondary="#6e6e73", text_dim="#aeaeb2", accent="#007AFF",
        )

    @staticmethod
    def dracula() -> ColorTheme:
        return ColorTheme(
            name="Dracula", bg_main="#282a36", bg_sidebar="#21222c",
            bg_card="#44475a", bg_card_hover="#6272a4", bg_nav="#282a36",
            bg_input="#44475a", border="#44475a", text_primary="#f8f8f2",
            text_secondary="#6272a4", text_dim="#44475a", accent="#bd93f9",
        )

    @staticmethod
    def nord() -> ColorTheme:
        return ColorTheme(
            name="Nord", bg_main="#2e3440", bg_sidebar="#242933",
            bg_card="#434c5e", bg_card_hover="#4c566a", bg_nav="#2e3440",
            bg_input="#3b4252", border="#4c566a", text_primary="#eceff4",
            text_secondary="#d8dee9", text_dim="#4c566a", accent="#88c0d0",
        )

    @staticmethod
    def gruvbox() -> ColorTheme:
        return ColorTheme(
            name="Gruvbox", bg_main="#282828", bg_sidebar="#1d2021",
            bg_card="#504945", bg_card_hover="#665c54", bg_nav="#282828",
            bg_input="#3c3836", border="#504945", text_primary="#ebdbb2",
            text_secondary="#a89984", text_dim="#504945", accent="#fabd2d",
        )

    @staticmethod
    def light_warm() -> ColorTheme:
        return ColorTheme("Light Warm", "#faf7f2", "#f1ebe2", "#ffffff", "#e7ddcc", "#f1ebe2",
                          "#f1ebe2", "#e4dacb", "#292524", "#78716c", "#a8a29e", "#d97706")

    @staticmethod
    def dark_future() -> ColorTheme:
        return ColorTheme("Dark Future", "#05070d", "#080b14", "#0b1120", "#12203c", "#080b14",
                          "#0b1120", "#1e2a44", "#e0f7ff", "#93b4cf", "#5a7184", "#22d3ee")

    @staticmethod
    def dark_cosmos() -> ColorTheme:
        return ColorTheme("Dark Cosmos", "#0b0614", "#100a1e", "#170d2b", "#271848", "#100a1e",
                          "#170d2b", "#2d1b4e", "#ede9fe", "#a79bc8", "#6d5f8f", "#a78bfa")

    @staticmethod
    def dark_oled() -> ColorTheme:
        return ColorTheme("Dark OLED", "#000000", "#0a0a0a", "#111111", "#1f1f1f", "#0a0a0a",
                          "#111111", "#262626", "#f2f2f2", "#a1a1a1", "#6f6f6f", "#30d158")

    @staticmethod
    def pastel_bloom() -> ColorTheme:
        return ColorTheme("Pastel Bloom", "#fff6f3", "#ffede6", "#ffffff", "#ffd8ce", "#fff6f3",
                          "#ffede6", "#f6d9cf", "#4a3b3e", "#8a6f73", "#c4a9ac", "#ef7c8e")

    @staticmethod
    def rose_gold() -> ColorTheme:
        return ColorTheme("Rose Gold", "#fbf3f0", "#f3e3de", "#ffffff", "#e8cfc7", "#fbf3f0",
                          "#f3e3de", "#e3ccc4", "#3e2f2c", "#7d6660", "#bba79f", "#b76e79")

    @staticmethod
    def lavender_mist() -> ColorTheme:
        return ColorTheme("Lavender Mist", "#f7f5fb", "#ece8f6", "#ffffff", "#d6cdea", "#f7f5fb",
                          "#ece8f6", "#dcd5ea", "#363250", "#6e6a8a", "#a9a5c2", "#8b7cd8")

    @staticmethod
    def sunset_coral() -> ColorTheme:
        return ColorTheme("Sunset Coral", "#201025", "#2a1430", "#331a3b", "#4a2854", "#2a1430",
                          "#331a3b", "#4a2a56", "#ffede6", "#cba3ae", "#8a6b78", "#ff7a59")

    @staticmethod
    def ivory_elegance() -> ColorTheme:
        return ColorTheme("Ivory Elegance", "#faf6ee", "#f1eadb", "#fffdf8", "#e4d8be", "#f1eadb",
                          "#f1eadb", "#e2d7bf", "#2e2a20", "#6e6653", "#a89f8a", "#b09048")

    @staticmethod
    def graphite_platinum() -> ColorTheme:
        return ColorTheme("Graphite Platinum", "#101214", "#16191c", "#1c2024", "#2c3238", "#16191c",
                          "#1c2024", "#333a41", "#e8ecf0", "#9aa6b2", "#626e7a", "#a9b8c6")

    @staticmethod
    def champagne_noir() -> ColorTheme:
        return ColorTheme("Champagne Noir", "#0e0c0a", "#14110d", "#1b1712", "#2e261d", "#14110d",
                          "#1b1712", "#3a3128", "#f0e8dc", "#b3a48c", "#7a6c55", "#d6b36a")


class ThemeManager:
    """FIX #5: WeakRef pentru a preveni memory leak"""

    _instance: Optional["ThemeManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._current_theme = ThemePresets.dark_modern()
        self._previous_theme = self._current_theme
        # FIX #5: Folosește weakref pentru listeners
        self._listeners: List[weakref.ref] = []
        self._windows: List[weakref.ref] = []

    def register_window(self, window):
        # FIX #5: WeakRef pentru windows
        if not any(ref() is window for ref in self._windows):
            self._windows.append(weakref.ref(window))

    def unregister_window(self, window):
        self._windows = [ref for ref in self._windows if ref() is not window]

    def add_listener(self, widget):
        # FIX #5: WeakRef pentru listeners
        if not any(ref() is widget for ref in self._listeners):
            self._listeners.append(weakref.ref(widget))

    def remove_listener(self, widget):
        self._listeners = [ref for ref in self._listeners if ref() is not widget]

    def _cleanup_dead_refs(self):
        """Elimină referințele moarte fără ca TclError să întrerupă schimbarea temei."""
        def alive(ref):
            obj = ref()
            if obj is None:
                return False
            try:
                return bool(obj.winfo_exists())
            except Exception:
                return False
        self._listeners = [ref for ref in self._listeners if alive(ref)]
        self._windows = [ref for ref in self._windows if alive(ref)]

    def notify_listeners(self):
        """FIX #5: Curățare automată înainte de notify"""
        self._cleanup_dead_refs()

        for ref in self._windows[:]:
            window = ref()
            if window and hasattr(window, '_on_theme_change'):
                try:
                    window._on_theme_change()
                except Exception as e:
                    logger.warning(f"Theme update failed for window: {e}")

        for ref in self._listeners[:]:
            widget = ref()
            if widget and hasattr(widget, '_on_theme_change'):
                try:
                    widget._on_theme_change()
                except Exception as e:
                    logger.warning(f"Theme update failed for widget: {e}")

    @property
    def theme(self) -> ColorTheme:
        return self._current_theme

    def set_theme(self, theme: ColorTheme, apply_immediately: bool = True):
        self._previous_theme = self._current_theme
        self._current_theme = theme
        logger.info(f"Theme changed to: {theme.name}")
        if apply_immediately:
            for ref in self._windows:
                window = ref()
                if window and hasattr(window, 'after'):
                    window.after(0, self.notify_listeners)
            self.notify_listeners()

    def set_accent(self, accent_hex: str, apply_immediately: bool = True):
        self._previous_theme = self._current_theme
        self._current_theme = self._current_theme.with_accent(accent_hex)
        logger.info(f"Accent color changed to: {accent_hex}")
        if apply_immediately:
            for ref in self._windows:
                window = ref()
                if window and hasattr(window, 'after'):
                    window.after(0, self.notify_listeners)
            self.notify_listeners()

    def get_theme_names(self) -> list[str]:
        return ["Dark Modern", "Dark macOS", "Light Modern", "Dracula", "Nord", "Gruvbox",
                "Light Warm", "Dark Future", "Dark Cosmos", "Dark OLED", "Pastel Bloom",
                "Rose Gold", "Lavender Mist", "Sunset Coral", "Ivory Elegance",
                "Graphite Platinum", "Champagne Noir"]

    def get_theme_by_name(self, name: str) -> ColorTheme:
        themes = {
            "Dark Modern": ThemePresets.dark_modern,
            "Dark macOS": ThemePresets.dark_macos,
            "Light Modern": ThemePresets.light_modern,
            "Dracula": ThemePresets.dracula,
            "Nord": ThemePresets.nord,
            "Gruvbox": ThemePresets.gruvbox,
            "Light Warm": ThemePresets.light_warm,
            "Dark Future": ThemePresets.dark_future,
            "Dark Cosmos": ThemePresets.dark_cosmos,
            "Dark OLED": ThemePresets.dark_oled,
            "Pastel Bloom": ThemePresets.pastel_bloom,
            "Rose Gold": ThemePresets.rose_gold,
            "Lavender Mist": ThemePresets.lavender_mist,
            "Sunset Coral": ThemePresets.sunset_coral,
            "Ivory Elegance": ThemePresets.ivory_elegance,
            "Graphite Platinum": ThemePresets.graphite_platinum,
            "Champagne Noir": ThemePresets.champagne_noir,
        }
        preset_fn = themes.get(name, ThemePresets.dark_modern)
        return preset_fn()

    def get_accent_colors(self) -> list[tuple[str, str]]:
        return [(c.hex, c.name_local) for c in AccentColor]

THEME = ThemeManager()

BG_MAIN      = lambda: THEME.theme.bg_main
BG_SIDEBAR   = lambda: THEME.theme.bg_sidebar
BG_CARD      = lambda: THEME.theme.bg_card
BG_CARD_HOV  = lambda: THEME.theme.bg_card_hover
BG_NAV       = lambda: THEME.theme.bg_nav
BG_INPUT     = lambda: THEME.theme.bg_input
BORDER       = lambda: THEME.theme.border
TEXT_PRI     = lambda: THEME.theme.text_primary
TEXT_SEC     = lambda: THEME.theme.text_secondary
TEXT_DIM     = lambda: THEME.theme.text_dim
ACCENT       = lambda: THEME.theme.accent
SUCCESS      = lambda: THEME.theme.success
WARNING      = lambda: THEME.theme.warning
ERROR        = lambda: THEME.theme.error

def shade_color(hex_color: str, factor: float) -> str:
    """FIX: derivă un hover/variantă din accent-ul curent în loc de valori hardcodate.
    factor < 1 = mai închis, factor > 1 = mai deschis."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        light = max(0.0, min(1.0, light * factor))
        r, g, b = colorsys.hls_to_rgb(hue, light, sat)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    except Exception:
        return hex_color

# ─────────────────────────────────────────────────────────────
# WIDGET-uri CU SUPORT LIVE THEME
# ─────────────────────────────────────────────────────────────

class ThemedFrame(ctk.CTkFrame):
    def __init__(self, master, fg_color_key: str = "bg_card", **kw):
        self._fg_color_key = fg_color_key
        self._theme_func = self._get_theme_func(fg_color_key)
        super().__init__(master, fg_color=self._theme_func(), **kw)
        THEME.add_listener(self)

    def _get_theme_func(self, key: str) -> Callable:
        theme_funcs = {
            'bg_main': BG_MAIN, 'bg_sidebar': BG_SIDEBAR, 'bg_card': BG_CARD,
            'bg_nav': BG_NAV, 'bg_input': BG_INPUT, 'transparent': lambda: "transparent",
        }
        return theme_funcs.get(key, BG_CARD)

    def _on_theme_change(self):
        if self.winfo_exists():
            try:
                self.configure(fg_color=self._theme_func())
            except Exception:
                pass

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


class ThemedScrollableFrame(ctk.CTkScrollableFrame):
    def __init__(self, master, fg_color_key: str = "bg_card", **kw):
        self._fg_color_key = fg_color_key
        self._theme_func = self._get_theme_func(fg_color_key)
        super().__init__(master, fg_color=self._theme_func(), **kw)
        THEME.add_listener(self)

    def _get_theme_func(self, key: str) -> Callable:
        theme_funcs = {
            'bg_main': BG_MAIN, 'bg_sidebar': BG_SIDEBAR, 'bg_card': BG_CARD,
            'bg_nav': BG_NAV, 'bg_input': BG_INPUT, 'transparent': lambda: "transparent",
        }
        return theme_funcs.get(key, BG_CARD)

    def _on_theme_change(self):
        if self.winfo_exists():
            try:
                self.configure(fg_color=self._theme_func())
                self.configure(
                    scrollbar_button_color=BORDER(),
                    scrollbar_button_hover_color=TEXT_DIM()
                )
            except Exception:
                pass

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


class ThemedButton(ctk.CTkButton):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        THEME.add_listener(self)

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


# ─────────────────────────────────────────────────────────────
# UTILITARE
# ─────────────────────────────────────────────────────────────

def center_window(window, width: int = None, height: int = None):
    window.update_idletasks()
    if width is None:
        width = window.winfo_width()
    if height is None:
        height = window.winfo_height()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = (screen_width // 2) - (width // 2)
    y = (screen_height // 2) - (height // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


class _Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tooltip_window = None

        # Legare directă de metode (fără lambda -> fără probleme de closure)
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, event=None):
        if self.tooltip_window is not None:
            return

        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() + 20

        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        lbl = tk.Label(tw, text=self.text,
                       background=BG_CARD(), foreground=TEXT_PRI(),
                       relief="solid", borderwidth=1,
                       font=("Segoe UI", 10), padx=6, pady=3)
        lbl.pack()

    def _hide(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

# ═════════════════════════════════════════════════════════════
# ACCENT COLOR SELECTOR
# ═════════════════════════════════════════════════════════════

class AccentColorSelector(ctk.CTkFrame):
    def __init__(self, master, on_color_change: Callable, current_color: str = "#3b82f6", **kw):
        super().__init__(master, fg_color="transparent", **kw)
        THEME.add_listener(self)
        self.on_color_change = on_color_change
        self.current_color = current_color
        self.buttons = {}
        self.oval_ids = {}
        self._build()

    def _on_theme_change(self):
        if self.winfo_exists():
            for btn in self.buttons.values():
                btn.configure(bg=BG_CARD())
            self._update_selection()

    def _build(self):
        accent_colors = THEME.get_accent_colors()
        colors_frame = ctk.CTkFrame(self, fg_color="transparent")
        colors_frame.pack(fill="x", padx=0, pady=7)

        for i, (hex_color, name) in enumerate(accent_colors):
            btn = tk.Canvas(colors_frame, width=40, height=42, bg=BG_CARD(),
                           highlightthickness=0, cursor="hand2", takefocus=True)
            btn.grid(row=i // 8, column=i % 8, padx=(0, 7), pady=2)
            # Extra outer ring gives the selected swatch a clear, accessible focus.
            ring_id = btn.create_oval(3, 3, 37, 37, fill="", outline=BG_CARD(), width=2)
            oval_id = btn.create_oval(8, 8, 32, 32, fill=hex_color, outline=BORDER(), width=1)
            _Tooltip(btn, name)
            btn.bind("<Button-1>", lambda e, h=hex_color: self._on_click(h))
            btn.bind("<Return>", lambda e, h=hex_color: self._on_click(h))
            btn.bind("<space>", lambda e, h=hex_color: self._on_click(h))
            self.buttons[hex_color] = btn
            self.oval_ids[hex_color] = (oval_id, ring_id, name)

        custom_btn = ctk.CTkButton(colors_frame, text="＋", width=34, height=34,
                                   corner_radius=18, fg_color=BG_INPUT(),
                                   hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                                   command=self._pick_custom_color)
        custom_btn.grid(row=3, column=0, padx=(2, 7), pady=(3, 5), sticky="w")
        _Tooltip(custom_btn, "Alege o culoare personalizată")

        selected_name = next((name for color, name in accent_colors if color == self.current_color), "Personalizat")
        self.selection_label = ctk.CTkLabel(self, text=f"Selectat: {selected_name}  {self.current_color.upper()}",
                                            text_color=TEXT_SEC(), anchor="w",
                                            font=ctk.CTkFont(size=11))
        self.selection_label.pack(anchor="w", pady=(0, 2))
        self._update_selection()

    def _on_click(self, hex_color: str):
        self.current_color = hex_color
        self._update_selection()
        self.on_color_change(hex_color)

    def _pick_custom_color(self):
        from tkinter import colorchooser
        selected = colorchooser.askcolor(color=self.current_color, title="Alege culoarea de accent")
        if selected and selected[1]:
            self.current_color = selected[1].lower()
            self._update_selection()
            self.on_color_change(self.current_color)

    def _update_selection(self):
        selected_name = "Personalizat"
        for hex_color, btn in self.buttons.items():
            oval_id, ring_id, name = self.oval_ids[hex_color]
            if hex_color == self.current_color:
                selected_name = name
                btn.itemconfig(ring_id, outline=hex_color, width=3)
                btn.itemconfig(oval_id, outline=TEXT_PRI(), width=2)
                if not hasattr(btn, 'check_id'):
                    btn.check_id = btn.create_text(20, 20, text="✓", fill="#ffffff",
                                                   font=("Segoe UI", 10, "bold"))
                r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
                check_color = "#111827" if (0.299 * r + 0.587 * g + 0.114 * b) > 155 else "#ffffff"
                btn.itemconfig(btn.check_id, fill=check_color)
                btn.itemconfig(btn.check_id, state="normal")
            else:
                btn.itemconfig(ring_id, outline=BG_CARD(), width=2)
                btn.itemconfig(oval_id, outline=BORDER(), width=1)
                if hasattr(btn, 'check_id'):
                    btn.itemconfig(btn.check_id, state="hidden")
        if hasattr(self, "selection_label") and self.selection_label.winfo_exists():
            self.selection_label.configure(text=f"Selectat: {selected_name}  {self.current_color.upper()}",
                                           text_color=TEXT_SEC())

    def set_color(self, hex_color: str):
        if hex_color in self.buttons:
            self.current_color = hex_color
            self._update_selection()

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


class ThemePreviewCard(ctk.CTkFrame):
    """Card compact cu mini-previzualizare, adaptat la tema și accentul curente."""
    def __init__(self, master, theme_name: str, on_pick: Callable, selected: bool = False, **kwargs):
        self.theme_name = theme_name
        self.on_pick = on_pick
        self.selected = selected
        super().__init__(master, width=196, height=142, corner_radius=10,
                         fg_color=BG_CARD(), border_width=2 if selected else 1,
                         border_color=ACCENT() if selected else BORDER(), **kwargs)
        self.pack_propagate(False)
        self.preview = tk.Canvas(self, width=176, height=78,
                                 bg=THEME.get_theme_by_name(theme_name).bg_main,
                                 highlightthickness=0)
        self.preview.pack(padx=9, pady=(8, 5))
        self.name_label = ctk.CTkLabel(self, text=theme_name,
                                       text_color=ACCENT() if selected else TEXT_PRI(),
                                       font=ctk.CTkFont(size=12, weight="bold"))
        self.name_label.pack(anchor="w", padx=10)
        self.desc_label = ctk.CTkLabel(self, text=self._description(theme_name),
                                       text_color=TEXT_SEC(), anchor="w",
                                       font=ctk.CTkFont(size=10))
        self.desc_label.pack(anchor="w", padx=10)
        self._draw_preview()
        self._bind_clicks(self)
        THEME.add_listener(self)

    @staticmethod
    def _description(name: str) -> str:
        return {
            "Dark Modern": "Slate · echilibrat", "Dark macOS": "Grafit · albastru",
            "Light Modern": "Luminos · curat", "Dracula": "Violet · contrast",
            "Nord": "Albastru · calm", "Gruvbox": "Cald · retro",
            "Light Warm": "Tonuri calde", "Dark Future": "Cyan · futurist",
            "Dark Cosmos": "Violet · profund", "Dark OLED": "Negru · eficient",
            "Pastel Bloom": "Roz · delicat", "Rose Gold": "Trandafir · bej",
            "Lavender Mist": "Lavandă · aerisit", "Sunset Coral": "Amurg · coral",
            "Ivory Elegance": "Fildeș · auriu", "Graphite Platinum": "Grafit · rece",
            "Champagne Noir": "Negru · șampanie",
        }.get(name, "Paletă CatsSoft")

    def _draw_preview(self):
        palette = THEME.get_theme_by_name(self.theme_name)
        accent = ACCENT()
        c = self.preview
        c.delete("all")
        c.configure(bg=palette.bg_main)
        c.create_rectangle(0, 0, 28, 78, fill=palette.bg_sidebar, outline="")
        c.create_rectangle(28, 0, 176, 14, fill=palette.bg_nav, outline="")
        for y in (24, 36, 48):
            c.create_oval(7, y, 12, y + 5, fill=accent if y == 24 else palette.text_dim, outline="")
            c.create_rectangle(16, y + 1, 23, y + 4, fill=palette.text_secondary, outline="")
        c.create_rectangle(38, 23, 93, 55, fill=palette.bg_card, outline=palette.border)
        c.create_rectangle(100, 23, 168, 55, fill=palette.bg_card, outline=palette.border)
        c.create_rectangle(42, 29, 65, 33, fill=palette.text_primary, outline="")
        c.create_rectangle(42, 38, 84, 41, fill=palette.text_dim, outline="")
        c.create_rectangle(105, 29, 137, 34, fill=accent, outline="")
        c.create_rectangle(105, 41, 159, 44, fill=palette.text_secondary, outline="")
        c.create_rectangle(38, 62, 84, 72, fill=accent, outline="")
        c.create_rectangle(90, 64, 164, 69, fill=palette.border, outline="")

    def _bind_clicks(self, widget):
        widget.bind("<Button-1>", lambda _event: self.on_pick(self.theme_name))
        widget.bind("<Return>", lambda _event: self.on_pick(self.theme_name))
        for child in widget.winfo_children():
            self._bind_clicks(child)

    def set_selected(self, selected: bool):
        self.selected = selected
        self.configure(border_width=2 if selected else 1,
                       border_color=ACCENT() if selected else BORDER())
        self.name_label.configure(text_color=ACCENT() if selected else TEXT_PRI(),
                                  text=f"{'✓  ' if selected else ''}{self.theme_name}")

    def _on_theme_change(self):
        if self.winfo_exists():
            self.configure(fg_color=BG_CARD())
            self.preview.configure(bg=THEME.get_theme_by_name(self.theme_name).bg_main)
            self._draw_preview()
            self.set_selected(self.selected)
            self.desc_label.configure(text_color=TEXT_SEC())

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


class PreviewButton(ctk.CTkButton):
    def __init__(self, master, text: str = "👁️ Previzualizează Live", command: Callable = None, **kw):
        super().__init__(master, text=text, fg_color=ACCENT(), hover_color=BG_CARD_HOV(),
                        text_color="#000000" if THEME.theme.name.startswith("Light") else "#ffffff",
                        command=command, **kw)
        THEME.add_listener(self)

    def _on_theme_change(self):
        if self.winfo_exists():
            self.configure(fg_color=ACCENT(),
                          text_color="#000000" if THEME.theme.name.startswith("Light") else "#ffffff")

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()


# ─── Auto-Sort Folders ────────────────────────────────────────────────────────
AUTO_SORT_FOLDERS = {
    "video": "Downloads/Videos", "image": "Downloads/Images",
    "audio": "Downloads/Music", "archive": "Downloads/Archives",
    "document": "Downloads/Documents", "disk": "Downloads/ISOs", "file": "Downloads",
}

# 1. Dynamic Segmentation (în AddURLDialog._submit())
def optimal_segments(file_size: int) -> int:
    if file_size <= 0: return 1
    elif file_size < 5 * 1024 * 1024: return 1
    elif file_size < 50 * 1024 * 1024: return 4
    elif file_size < 500 * 1024 * 1024: return 8
    else: return min(16, max(8, file_size // (50 * 1024 * 1024)))

# 2. Adaptive Chunk Size (în _download_segment())
def adaptive_chunk_size(speed_bps: float) -> int:
    if speed_bps < 2 * 1024 * 1024: return 32 * 1024
    elif speed_bps < 10 * 1024 * 1024: return 64 * 1024
    else: return 256 * 1024

# ─── Data Models ──────────────────────────────────────────────────────────────
class DLStatus(Enum):
    QUEUED      = "Queued"
    DOWNLOADING = "Downloading"
    PAUSED      = "Paused"
    COMPLETED   = "Completed"
    ERROR       = "Error"
    CANCELLED   = "Cancelled"

class SpeedTracker:
    def __init__(self):
        self.samples = []
        self.lock = threading.Lock()

    def update(self, bytes_downloaded: int):
        now = time.time()
        with self.lock:
            self.samples.append((now, bytes_downloaded))
            # Păstrează doar ultimele 5 secunde
            cutoff = now - 5
            self.samples = [(t, b) for t, b in self.samples if t > cutoff]

    def get_speed(self) -> float:
        with self.lock:
            if len(self.samples) < 2:
                return 0.0
            t0, b0 = self.samples[0]
            t1, b1 = self.samples[-1]
            if t1 - t0 < 0.1:
                return 0.0
            return max(0.0, (b1 - b0) / (t1 - t0))

    def rebase(self, bytes_downloaded: int):
        """Restart the sampling window on a new counter baseline.

        Bytes recovered from disk (an already complete segment, a resumed file)
        are added to the counter in one step. Without a new baseline the tracker
        would report that jump as transfer speed - a burst of tens of MB/s for
        a single tick.
        """
        now = time.time()
        with self.lock:
            self.samples = [(now, bytes_downloaded)]

@dataclass
class Download:
    url: str
    filename: str
    save_path: str
    status: DLStatus = DLStatus.QUEUED
    progress: float = 0.0
    speed: float = 0.0
    total: int = 0
    downloaded: int = 0
    error_msg: str = ""
    supports_resume: bool = True
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    stop_event: Optional[threading.Event] = field(default=None, repr=False)
    active_response: Optional[object] = field(default=None, repr=False)
    pause_event: Optional[threading.Event] = field(default=None, repr=False)
    speed_limit: int = 0
    segments: int = 1
    checksum: str = ""
    checksum_algo: str = "sha256"
    added_date: datetime = field(default_factory=datetime.now)
    completed_date: Optional[datetime] = None
    app: Optional = None
    lock: threading.Lock = field(default_factory=threading.RLock, repr=False)
    speed_tracker: SpeedTracker = field(default_factory=SpeedTracker, repr=False)
    started_at: Optional[datetime] = None
    peak_speed: float = 0.0
    speed_history: List[tuple] = field(default_factory=list, repr=False)
    retry_count: int = 0
    chunk_size_bytes: int = 0
    chunk_progress: List[float] = field(default_factory=list, repr=False)
    throttle_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    throttle_deadline: float = 0.0
    resume_requested: bool = False
    filename_locked: bool = False  # preserves a collision-safe destination path
    priority: int = 1             # 0=Low, 1=Normal, 2=High
    queue_order: int = 0          # stable ordering within the same priority
    speed_limit_override: Optional[int] = None  # bytes/s; None=global, 0=unlimited per-file
    session_transferred: int = 0  # actual network bytes read in this app session
    resume_validator: str = ""   # ETag or Last-Modified used with HTTP If-Range
    resume_blocked: bool = False  # existing output unverifiable: never resume from it
    final_verified: bool = False  # existing output proven to match the current resource
    history_recorded: bool = field(default=False, repr=False)
    automatic_retry_count: int = 0  # extra retries scheduled by Smart Automation
    auto_retry_at: float = 0.0       # UNIX timestamp; persisted across application restarts
    network_assignments: dict = field(default_factory=dict, repr=False)
    # Toate răspunsurile HTTP deschise ale acestei descărcări. Calea segmentată
    # ține simultan mai multe răspunsuri; ele trebuie închise la Stop, altfel
    # anularea așteaptă timeout-ul de citire (15 s) în loc să fie imediată.
    active_responses: List = field(default_factory=list, repr=False)


def _track_active_response(dl: "Download", response) -> None:
    """Înregistrează un răspuns HTTP deschis, ca să poată fi închis la Stop."""
    if response is None:
        return
    try:
        with dl.lock:
            dl.active_responses.append(response)
    except Exception:
        pass


def _untrack_active_response(dl: "Download", response) -> None:
    """Scoate un răspuns din registru (a fost închis sau a eșuat)."""
    if response is None:
        return
    try:
        with dl.lock:
            try:
                dl.active_responses.remove(response)
            except ValueError:
                pass
    except Exception:
        pass


def _close_response_async(response) -> None:
    """Închide un răspuns HTTP fără să blocheze firul care închide.

    Pe Windows, ``close()`` pe un socket cu un ``recv`` în așteptare blochează
    până la expirarea timeout-ului de citire (15-25 s): firul UI s-ar congela
    la apăsarea butonului Stop. Închiderea într-un fir secundar păstrează
    efectul (citirea încheiată, socketul eliberat) fără să blocheze interfața.
    """
    if response is None:
        return

    def _close_worker():
        try:
            response.close()
        except Exception:
            pass

    try:
        threading.Thread(target=_close_worker, daemon=True,
                         name="catssoft-close-response").start()
    except Exception:
        try:
            response.close()
        except Exception:
            pass


def _close_active_responses(dl: "Download") -> None:
    """Închide toate răspunsurile HTTP deschise ale descărcării.

    O citire blocată într-un răspuns nu se întrerupe la ``stop_event``; ea
    continuă până la timeout sau până la sosirea următorului bloc. Închiderea
    explicită face ca descărcarea să se oprească la citirea următoare, exact
    ca în calea simplă.
    """
    try:
        with dl.lock:
            responses = list(dl.active_responses)
    except Exception:
        responses = []
    for response in responses:
        _close_response_async(response)

@dataclass
class ScheduledDownload:
    url: str
    filename: str
    scheduled_time: datetime
    save_path: str = ""
    repeat: str = "once"
    enabled: bool = True
    segments: int = 1
    checksum: str = ""

@dataclass
class DownloadHistory:
    id: int
    url: str
    filename: str
    save_path: str
    status: str
    total_size: int
    downloaded_size: int
    added_date: str
    completed_date: str
    speed_avg: float


# ─── Utility Functions ────────────────────────────────────────────────────────
def safe_int(value, default=0, minimum=None, maximum=None) -> int:
    """Convert persisted/user values to an integer and optionally clamp the range."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = int(default)
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def fmt_size(b: int) -> str:
    if b <= 0: return "? B"
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def fmt_speed(bps: float) -> str:
    return fmt_size(int(bps)) + "/s"

def fmt_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds: Optional[float]) -> str:
    """Format compact pentru durate/ETA; None indică o estimare indisponibilă."""
    if seconds is None:
        return "—"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}z {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes:02d}:{secs:02d}"
    return f"{secs}s"

def guess_filetype(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path.lower())[1]
    if ext in {".mp4",".mkv",".avi",".mov",".webm",".flv"}: return "video"
    if ext in {".jpg",".jpeg",".png",".gif",".webp",".bmp",".svg"}: return "image"
    if ext in {".mp3",".flac",".aac",".wav",".ogg",".m4a"}: return "audio"
    if ext in {".zip",".tar",".gz",".bz2",".rar",".7z"}: return "archive"
    if ext in {".pdf",".docx",".xlsx",".pptx",".txt",".rtf",".odt"}: return "document"
    if ext in {".iso",".img",".dmg"}: return "disk"
    return "file"

def sanitize_filename(filename: str) -> str:
    """Normalizează un nume de fișier pentru Windows/Linux, inclusiv numele rezervate."""
    if not filename:
        return f"download_{int(time.time())}.bin"

    filename = str(filename).replace("\x00", "")
    # Keep only a file name, never a server-supplied path.
    filename = filename.replace("\\", "/").split("/")[-1]
    invalid_chars = '<>:"/\\|?*'
    filename = "".join("_" if ch in invalid_chars or ord(ch) < 32 else ch for ch in filename)
    filename = filename.strip(". ")

    # Windows reserves these device names even when an extension is present.
    stem = filename.split(".", 1)[0].strip().upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if stem in reserved:
        filename = "_" + filename

    # Keep paths below common Windows path-length trouble spots.
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:max(1, 200 - len(ext))] + ext[:40]
    return filename or f"download_{int(time.time())}.bin"


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value) -> str:
    """Neutralizează formulele CSV (CSV injection).

    Excel și LibreOffice interpretează o celulă care începe cu ``=``, ``+``,
    ``-`` sau ``@`` ca formulă. Numele fișierului poate veni de la server
    (Content-Disposition), deci un server malițios ar putea injecta o formulă
    în exportul istoricului deschis de utilizator.

    Prefixul ``'`` transformă celula în text simplu în Excel și rămâne
    lizibilă în orice alt cititor de CSV.
    """
    if value is None:
        return ""
    text = str(value)
    if text[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def unique_target_path(path: str) -> str:
    """Return a non-existing destination path without overwriting another file."""
    if not os.path.exists(path):
        return path
    parent, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    index = 1
    while True:
        candidate = os.path.join(parent, f"{stem}_{index}{ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def extension_from_mime_type(mime_type: str) -> str:
    """Determină extensia din Content-Type header"""
    if not mime_type:
        return '.bin'

    mime_to_ext = {
        'application/pdf': '.pdf',
        'application/zip': '.zip',
        'application/x-zip-compressed': '.zip',
        'application/x-rar-compressed': '.rar',
        'application/x-7z-compressed': '.7z',
        'application/msword': '.doc',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.ms-excel': '.xls',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
        'application/vnd.ms-powerpoint': '.ppt',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        'application/x-msdownload': '.exe',
        'application/x-executable': '.exe',
        'application/x-msi': '.msi',
        'application/octet-stream': '.bin',
        'video/mp4': '.mp4',
        'video/x-msvideo': '.avi',
        'video/x-matroska': '.mkv',
        'video/webm': '.webm',
        'audio/mpeg': '.mp3',
        'audio/wav': '.wav',
        'audio/ogg': '.ogg',
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'text/plain': '.txt',
        'text/html': '.html',
    }

    # Normalize MIME type (remove charset, etc.)
    mime_type = mime_type.split(';')[0].strip().lower()
    return mime_to_ext.get(mime_type, '.bin')

_BAD_EXTS = {".php", ".asp", ".aspx", ".cgi", ".js"}


def _get_ci_header(headers, name: str, default: str = "") -> str:
    """
    Returnează un header indiferent de majuscule/minuscule.
    Funcționează atât cu requests.headers, cât și cu dict() normal.
    """
    if not headers:
        return default

    # Mai întâi încercarea directă, utilă pentru CaseInsensitiveDict
    try:
        value = headers.get(name)
        if value is not None:
            return value
    except Exception:
        pass

    lower_name = name.lower()

    try:
        value = headers.get(lower_name)
        if value is not None:
            return value
    except Exception:
        pass

    #Fallback pentru dict() normal cu chei de tip "Content-Disposition"
    try:
        for k, v in headers.items():
            if str(k).lower() == lower_name:
                return v
    except Exception:
        pass

    return default


def _parse_content_disposition_filename(cd: str) -> Optional[str]:
    """
    Extrage filename dintr-un header/query Content-Disposition.
    Suportă:
      filename="nume.exe"
      filename=nume.exe
      filename*=UTF-8''nume%20cu%20spa%C8%9Bii.exe
    """
    if not cd:
        return None

    # Împărțim ținând cont de ghilimele, ca să nu spargem un filename care conține ';'
    parts = []
    current = ""
    in_quotes = False

    for ch in cd:
        if ch == '"':
            in_quotes = not in_quotes
            current += ch
        elif ch == ";" and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += ch

    if current:
        parts.append(current)

    fallback = None

    for part in parts:
        part = part.strip()
        if not part or "=" not in part:
            continue

        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "filename*":
            # Format RFC 5987: UTF-8''encoded-filename
            if "''" in value:
                value = value.split("''", 1)[1]

            value = value.strip('"\'')
            if value:
                try:
                    return unquote(value)
                except Exception:
                    return value

        elif key == "filename":
            value = value.strip('"\'')
            if value:
                # Decodează doar dacă pare percent-encoded, ca să nu stricăm nume normale
                if "%" in value:
                    try:
                        value = unquote(value)
                    except Exception:
                        pass
                fallback = value

    return fallback


def _filename_from_query_params(url: str) -> Optional[str]:
    """
    Extrage numele din query parameters, util pentru GitHub/Azure/S3:
      ?response-content-disposition=attachment; filename=file.exe
      ?filename=file.zip
    """
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return None

        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs_ci = {k.lower(): v for k, v in qs.items()}

        # Cazul GitHub: response-content-disposition
        for key in ("response-content-disposition", "content-disposition"):
            values = qs_ci.get(key)
            if values:
                name = _parse_content_disposition_filename(values[0])
                if name:
                    return name

        # Alte forme comune
        for key in ("filename", "file", "name", "download"):
            values = qs_ci.get(key)
            if values and values[0]:
                candidate = os.path.basename(values[0])
                if candidate and not candidate.lower().endswith(tuple(_BAD_EXTS)):
                    return candidate

    except Exception:
        pass

    return None


def _ensure_file_extension(name: str, source_urls, content_type: str = "") -> str:
    """
    Dacă numele extras nu are extensie, încearcă să o adauge din:
      - URL-ul final
      - URL-ul original
      - Content-Type
    """
    if not name:
        return name

    root, ext = os.path.splitext(name)
    ext = ext.lower()

    # Dacă are deja extensie validă, o păstrăm
    if ext and ext not in _BAD_EXTS and len(ext) <= 10:
        return name

    # Dacă are o extensie proastă/genereză, păstrăm doar baza
    base = root if ext else name

    # Încercăm extensia din URL-uri
    for u in source_urls:
        if not u:
            continue
        try:
            path = urlparse(u).path
            cand_ext = os.path.splitext(path)[1].lower()
            if cand_ext and cand_ext not in _BAD_EXTS and len(cand_ext) <= 10:
                return base + cand_ext
        except Exception:
            pass

    # Ultima șansă: Content-Type
    ct_ext = extension_from_mime_type(content_type or "")
    if ct_ext and ct_ext != ".bin" and ct_ext not in _BAD_EXTS:
        return base + ct_ext

    return name

def filename_from_url(url: str, response_headers: dict = None, final_url: str = None) -> str:
    """
    Extrage numele fișierului cu prioritate:
      1. Content-Disposition header
      2. response-content-disposition / filename din query params
      3. Content-Type fallback
      4. final URL path
      5. original URL path
    """
    name = None

    content_type = _get_ci_header(response_headers, "content-type", "") if response_headers else ""

    # 1. Content-Disposition header — prioritate maximă
    cd = _get_ci_header(response_headers, "content-disposition", "") if response_headers else ""
    if cd:
        name = _parse_content_disposition_filename(cd)
        if name:
            name = _ensure_file_extension(name, (final_url, url), content_type)
            logger.info(f"Filename from Content-Disposition: {name}")
            return sanitize_filename(name)

    # 2. Query parameters din final_url și url
    #    Foarte important pentru GitHub:
    #    ?response-content-disposition=attachment; filename=...
    for candidate_url in (final_url, url):
        if candidate_url:
            name = _filename_from_query_params(candidate_url)
            if name:
                name = _ensure_file_extension(name, (final_url, url), content_type)
                logger.info(f"Filename from query parameter: {name}")
                return sanitize_filename(name)

    # 3. Content-Type fallback
    if not name and content_type:
        ext = extension_from_mime_type(content_type)
        if ext and ext != ".bin":
            base_name = os.path.basename(urlparse(url).path).split("?")[0]
            if base_name and "." in base_name:
                base_name = os.path.splitext(base_name)[0]
            else:
                base_name = f"download_{int(time.time())}"

            name = f"{base_name}{ext}"
            logger.info(f"Filename from Content-Type: {name}")
            return sanitize_filename(name)

    # 4. Final URL după redirect
    if final_url and final_url != url:
        name = unquote(os.path.basename(urlparse(final_url).path))
        if "?" in name:
            name = name.split("?")[0]

        if name and not name.lower().endswith(tuple(_BAD_EXTS)):
            name = _ensure_file_extension(name, (final_url, url), content_type)
            logger.info(f"Filename from redirect URL: {name}")
            return sanitize_filename(name)
        else:
            logger.warning(f"Skipping script filename from redirect: {name}")

    # 5. URL original path
    name = unquote(os.path.basename(urlparse(url).path))
    if "?" in name:
        name = name.split("?")[0]

    if name and not name.lower().endswith(tuple(_BAD_EXTS)):
        name = _ensure_file_extension(name, (final_url, url), content_type)
        logger.info(f"Filename from original URL: {name}")
        return sanitize_filename(name)
    else:
        logger.warning(f"Skipping script filename from URL: {name}")

    # 6. Fallback final
    if content_type:
        ext = extension_from_mime_type(content_type)
        if ext != ".bin":
            name = f"download_{int(time.time())}{ext}"
            logger.info(f"Filename from Content-Type fallback: {name}")
            return sanitize_filename(name)

    name = f"download_{int(time.time())}.bin"
    logger.warning(f"Using fallback filename: {name}")
    return name

# ═════════════════════════════════════════════════════════════
# FILENAME EXTRACTION (Simple & Effective)
# ═════════════════════════════════════════════════════════════

def extract_filename_from_url(url):
    """Extrage numele din query parameters (Google Drive style)"""
    try:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)

        if 'response-content-disposition' in query_params:
            disposition_param = query_params['response-content-disposition'][0]
            file_name_param = disposition_param.split('filename=')[-1]
            name = unquote(file_name_param)
            logger.info(f"Filename from query param: {name}")
            return sanitize_filename(name)
        else:
            parts = parsed_url.path.split('/')
            name = parts[-1]
            if '?' in name:
                name = name.split('?')[0]
            logger.info(f"Filename from URL path: {name}")
            return sanitize_filename(name)
    except Exception as e:
        logger.error(f"extract_filename_from_url error: {e}")
        return None


def extract_file_name_from_url(url):
    """Extrage numele din Content-Disposition header (HEAD request).
    FIX: parsarea Content-Disposition e acum delegată la filename_from_url() -
    elimina a patra copie a aceluiași regex din fișier."""
    try:
        # HEAD request cu follow redirects (Session partajat - reutilizare conexiuni)
        response = SESSION.head(url, allow_redirects=True, timeout=10)

        # Verifică Content-Disposition
        if 'Content-Disposition' in response.headers:
            logger.info(f"Content-Disposition: {response.headers['Content-Disposition']}")
            filename = filename_from_url(url, response.headers, response.url)
            if filename:
                return filename
            logger.warning("No filename in Content-Disposition")
            return None
        else:
            logger.info("No Content-Disposition header, trying URL fallback")
            # Fallback la URL
            filename = extract_filename_from_url(url)
            if filename and not filename.endswith(('.php', '.asp', '.aspx', '.cgi', '.dll', '.js')):
                return filename
            return None

    except requests.exceptions.Timeout:
        logger.error("HEAD request timeout")
        return None
    except requests.exceptions.ConnectionError:
        logger.error("HEAD request connection error")
        return None
    except Exception as e:
        logger.error(f"extract_file_name_from_url error: {e}")
        return None


def is_valid_url(url):
    """Validate HTTP(S) URLs accepted by requests (FTP is intentionally rejected)."""
    if not isinstance(url, str):
        return False
    candidate = url.strip()
    if not candidate or any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        return False
    try:
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if not parsed.netloc or not parsed.hostname:
            return False
        # Accessing .port validates that a supplied port is numeric and in range.
        port = parsed.port
        if port is not None and not (1 <= port <= 65535):
            return False
        return True
    except (TypeError, ValueError):
        return False


def open_folder(path: str):
    folder = os.path.dirname(path) if os.path.isfile(path) else path
    if not os.path.exists(folder): return
    try:
        if sys.platform == "win32": os.startfile(folder)
        elif sys.platform == "darwin": subprocess.Popen(["open", folder])
        else: subprocess.Popen(["xdg-open", folder])
    except Exception: pass

def verify_checksum(file_path: str, expected_hash: str, algorithm="sha256") -> bool:
    if not expected_hash:
        return True
    if not os.path.isfile(file_path):
        return False
    try:
        hash_func = hashlib.new(algorithm)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_func.update(chunk)
        return hash_func.hexdigest().lower() == expected_hash.lower()
    except Exception as e:
        logger.error(f"Checksum verification error: {e}")
        return False

def auto_sort_file(save_path: str, filetype: str, auto_sort_enabled: bool = True) -> str:
    """Mută fișierul terminat într-un subfolder de categorie.

    Subfolderul de categorie se creează DENTRU folderul în care merge fișierul
    (cel ales de utilizator sau cel implicit al aplicației). Anterior destinația
    era fixată pe ``~/Downloads/<Categorie>``: o descărcare pornită în alt
    folder sau pe altă disc ajungea totuși în Downloads-ul utilizatorului.
    """
    if not auto_sort_enabled or filetype not in AUTO_SORT_FOLDERS: return save_path
    base_folder = AUTO_SORT_FOLDERS[filetype]
    parent = os.path.dirname(os.path.abspath(save_path))
    # "Downloads/Videos" -> "Videos"; "Downloads" (categoria "file") -> fără subfolder
    parts = [p for p in base_folder.replace("\\", "/").split("/")
             if p and p.lower() != "downloads"]
    category = parts[0] if parts else ""
    full_path = os.path.join(parent, category) if category else parent
    try:
        os.makedirs(full_path, exist_ok=True)
    except OSError as exc:
        logger.error("Auto-sort error: %s", exc)
        return save_path
    filename = os.path.basename(save_path)
    new_path = os.path.join(full_path, filename)
    try:
        if os.path.exists(save_path) and os.path.abspath(save_path) != os.path.abspath(new_path):
            # Do not replace an unrelated existing download in the destination folder.
            new_path = unique_target_path(new_path)
            shutil.move(save_path, new_path)
            return new_path
    except Exception as e:
        logger.error(f"Auto-sort error: {e}")
    return save_path

ICONS = {"video": "▶", "image": "🖼", "audio": "♫", "archive": "🗜", "document": "📄", "disk": "💿", "file": "↓"}


def default_database_path() -> str:
    """Choose a writable per-user database path while honoring an existing legacy DB."""
    override = os.environ.get("CATSSOFT_DB_PATH")
    if override:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(override)))

    legacy_candidates = [Path.cwd() / "downloads_history.db"]
    try:
        legacy_candidates.append(Path(__file__).resolve().parent / "downloads_history.db")
    except Exception:
        pass
    for candidate in legacy_candidates:
        if candidate.is_file():
            return str(candidate)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        data_dir = Path(base) / "CatsSoft" / "Downloader"
    elif sys.platform == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "CatsSoft" / "Downloader"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        data_dir = (Path(base) if base else Path.home() / ".local" / "share") / "catssoft-downloader"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "downloads_history.db")

class _ExportCancelled(Exception):
    """Raised internally when a history export is cancelled by the user."""


# ─── Database Manager (SQLite) ────────────────────────────────────────────────
class DatabaseManager:
    """SQLite wrapper that reuses one connection per thread.

    Opening a connection for every single query is expensive: each call re-reads the
    schema and re-locks the database file. A thread-local, long-lived connection keeps
    the same semantics (each ``with conn`` block still commits or rolls back) while
    removing that overhead from every settings read and every history page.
    """

    def __init__(self, db_path=None):
        self.db_path = os.path.abspath(db_path or default_database_path())
        self._local = threading.local()
        self.degraded_mode = False
        # Liste de perechi (fir_proprietar, conexiune). Perechea e necesară ca să
        # putem închide conexiunile firurilor care s-au terminat: aplicația
        # creează fire scurte (fiecare căutare în istoric, fiecare export) și,
        # fără această curățare, fiecare ar lăsa un handle SQLite deschis până
        # la ieșirea aplicației.
        self._connections = []
        self._connections_lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self._init_db()
        except (sqlite3.Error, OSError) as exc:
            # Baza de date nu poate fi folosită (fișier blocat, corupt, lipsă de
            # permisiuni). Nu lăsăm aplicația să nu pornească: continuăm cu o
            # bază temporară, iar istoricul se pierde doar pentru această sesiune.
            logger.exception("Baza de date nu poate fi inițializată la %s: %s", self.db_path, exc)
            broken = getattr(self._local, "conn", None)
            if broken is not None:
                try:
                    broken.close()
                except sqlite3.Error:
                    pass
                self._local.conn = None
            fallback = os.path.join(tempfile.gettempdir(),
                                    f"catssoft_downloader_{os.getpid()}_{int(time.time())}.db")
            self.db_path = fallback
            self.degraded_mode = True
            self._init_db()

    @staticmethod
    def _connection_is_dead(conn) -> bool:
        """True dacă SQLite a marcat conexiunea ca fiind închisă."""
        if conn is None:
            return True
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            # "Cannot operate on a closed database."
            return True
        except sqlite3.Error:
            # Eroare tranzitorie (blocare, I/O): conexiunea e încă vie.
            return False
        return False

    def _connect(self, timeout: int = 10):
        """Return the calling thread's connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and self._connection_is_dead(conn):
            # close() poate fi apelat (de ex. la ieșirea aplicației) cât timp alt
            # fir mai folosește managerul. Conexiunea cache-uită devine atunci
            # inutilizabilă, așa că o recreăm transparent în loc să aruncăm
            # "Cannot operate on a closed database" în mijlocul operației.
            self._local.conn = None
            conn = None
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=timeout, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                # Fișierul bazei de date este corupt: conexiunea e inutilizabilă.
                # Nu o memorăm, ca fallback-ul să poată crea una nouă pe altă cale.
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                raise
            self._local.conn = conn
            # Fiecare fir creează propria conexiune; le ținem într-un registru ca
            # să putem închide toate la ieșire (altfel rămân deschise până la GC).
            with self._connections_lock:
                self._connections.append((threading.current_thread(), conn))
        try:
            # busy_timeout is the knob the `timeout` argument maps to; keep it in sync
            # so long-running exports keep their original tolerance for a locked file.
            conn.execute(f"PRAGMA busy_timeout={int(timeout) * 1000}")
        except sqlite3.Error:
            pass
        # Firele scurte (căutări/exporturi în istoric) lasă în urmă handle-uri
        # deschise; le închidem pe cele ale firurilor deja terminate.
        self._sweep_dead_connections()
        return conn

    def _sweep_dead_connections(self):
        """Close connections whose owner thread has already finished."""
        current = threading.current_thread()
        with self._connections_lock:
            stale = []
            alive = []
            for owner, connection in self._connections:
                if owner is not current and not owner.is_alive():
                    stale.append(connection)
                else:
                    alive.append((owner, connection))
            self._connections = alive
        for connection in stale:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def close(self):
        """Close every connection opened by this manager (one per thread)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        with self._connections_lock:
            connections = [connection for _, connection in self._connections]
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _init_db(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("""CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, status TEXT,
                total_size INTEGER, downloaded_size INTEGER, added_date TEXT,
                completed_date TEXT, speed_avg REAL)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, scheduled_time TEXT,
                repeat_type TEXT, enabled INTEGER, segments INTEGER, checksum TEXT)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS active_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
                filename TEXT NOT NULL, save_path TEXT, status TEXT,
                total_size INTEGER, downloaded_size INTEGER, segments INTEGER,
                checksum TEXT, checksum_algo TEXT, speed_limit INTEGER, added_date TEXT,
                priority INTEGER NOT NULL DEFAULT 1, queue_order INTEGER NOT NULL DEFAULT 0,
                speed_limit_override INTEGER, automatic_retry_count INTEGER NOT NULL DEFAULT 0,
                auto_retry_at REAL NOT NULL DEFAULT 0)""")

            # Existing user databases are migrated in-place. ALTER TABLE adds only missing columns.
            migrations = {
                "history": {
                    "save_path": "TEXT", "status": "TEXT", "total_size": "INTEGER",
                    "downloaded_size": "INTEGER", "added_date": "TEXT",
                    "completed_date": "TEXT", "speed_avg": "REAL",
                },
                "scheduled": {
                    "save_path": "TEXT", "scheduled_time": "TEXT", "repeat_type": "TEXT",
                    "enabled": "INTEGER DEFAULT 1", "segments": "INTEGER DEFAULT 1", "checksum": "TEXT DEFAULT ''",
                },
                "active_queue": {
                    "total_size": "INTEGER", "downloaded_size": "INTEGER",
                    "segments": "INTEGER DEFAULT 1", "checksum": "TEXT DEFAULT ''",
                    "checksum_algo": "TEXT DEFAULT 'sha256'", "speed_limit": "INTEGER DEFAULT 0",
                    "added_date": "TEXT", "priority": "INTEGER NOT NULL DEFAULT 1",
                    "queue_order": "INTEGER NOT NULL DEFAULT 0", "speed_limit_override": "INTEGER",
                    "automatic_retry_count": "INTEGER NOT NULL DEFAULT 0", "auto_retry_at": "REAL NOT NULL DEFAULT 0",
                },
            }
            for table, columns in migrations.items():
                existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, declaration in columns.items():
                    if column not in existing:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

            # History pagination indexes: avoid repeatedly sorting the complete history.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_order ON history(COALESCE(added_date, '') DESC, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_status_order ON history(status, COALESCE(added_date, '') DESC, id DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_filename_nocase ON history(filename COLLATE NOCASE)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_size ON history(downloaded_size DESC)")
            conn.commit()

    def add_history(self, dl: Download):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO history (url, filename, save_path, status, total_size,
                downloaded_size, added_date, completed_date, speed_avg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (dl.url, dl.filename, dl.save_path, dl.status.value, dl.total, dl.downloaded,
                 dl.added_date.isoformat(), dl.completed_date.isoformat() if dl.completed_date else None,
                 self._average_speed(dl)))
            conn.commit()

    @staticmethod
    def _average_speed(dl: Download) -> float:
        end = dl.completed_date or datetime.now()
        start = dl.started_at or dl.added_date
        elapsed = max(0.0, (end - start).total_seconds())
        if elapsed > 0 and dl.downloaded > 0:
            return float(dl.downloaded) / elapsed
        return float(getattr(dl, "speed", 0.0) or 0.0)

    def get_history(self, limit=100) -> List[DownloadHistory]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT id, url, filename, save_path, status, total_size,
                downloaded_size, added_date, completed_date, speed_avg
                FROM history ORDER BY COALESCE(added_date, '') DESC, id DESC LIMIT ?""",
                (safe_int(limit, 100, 1, 10000),))
            rows = cursor.fetchall()
        return [DownloadHistory(*row) for row in rows]

    @staticmethod
    def _history_where_params(search_term="", status_filter="toate",
                              period_filter="Toate", type_filter="Toate"):
        """Build parameterized filters shared by pagination and streaming export."""
        clauses = []
        params = []
        term = str(search_term or "").strip()
        if term:
            # Escape user-supplied SQL wildcard characters; preserve literal search semantics.
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append("(filename LIKE ? ESCAPE '\\' COLLATE NOCASE OR url LIKE ? ESCAPE '\\' COLLATE NOCASE)")
            params.extend((pattern, pattern))

        statuses = {
            "completate": "Completed", "completed": "Completed",
            "eșuate": "Error", "esuate": "Error", "error": "Error",
            "anulate": "Cancelled", "cancelled": "Cancelled",
        }
        status_value = statuses.get(str(status_filter or "toate").strip().lower())
        if status_value:
            clauses.append("status = ?")
            params.append(status_value)

        period = str(period_filter or "Toate").strip().lower()
        now = datetime.now()
        if period in ("astăzi", "astazi", "today"):
            clauses.append("substr(COALESCE(completed_date, added_date, ''), 1, 10) = ?")
            params.append(now.strftime("%Y-%m-%d"))
        elif period in ("ultimele 7 zile", "7 zile", "7 days"):
            clauses.append("COALESCE(completed_date, added_date, '') >= ?")
            params.append((now - timedelta(days=7)).isoformat(timespec="seconds"))
        elif period in ("ultimele 30 zile", "30 zile", "30 days"):
            clauses.append("COALESCE(completed_date, added_date, '') >= ?")
            params.append((now - timedelta(days=30)).isoformat(timespec="seconds"))

        type_value = str(type_filter or "Toate").strip().lower()
        extensions = {
            "video": (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".mpeg", ".mpg", ".m4v", ".ts", ".wmv"),
            "audio": (".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a", ".opus", ".wma", ".aiff"),
            "imagini": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".ico"),
            "arhive": (".zip", ".tar", ".gz", ".bz2", ".rar", ".7z", ".xz", ".tgz"),
            "documente": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".csv", ".md"),
        }
        aliases = {"images": "imagini", "image": "imagini", "archives": "arhive", "archive": "arhive",
                   "documents": "documente", "document": "documente"}
        type_value = aliases.get(type_value, type_value)
        suffix_expr = "lower(COALESCE(NULLIF(filename, ''), save_path, ''))"
        if type_value in extensions:
            checks = [f"{suffix_expr} LIKE ?" for _ in extensions[type_value]]
            clauses.append("(" + " OR ".join(checks) + ")")
            params.extend("%" + ext for ext in extensions[type_value])
        elif type_value in ("altele", "other"):
            all_exts = tuple(ext for items in extensions.values() for ext in items)
            checks = [f"{suffix_expr} LIKE ?" for _ in all_exts]
            clauses.append("NOT (" + " OR ".join(checks) + ")")
            params.extend("%" + ext for ext in all_exts)

        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _history_order_by(sort_order="Cele mai noi", prefix=""):
        """Whitelist SQL sort expressions; never interpolate arbitrary UI strings."""
        key = str(sort_order or "Cele mai noi").strip().lower()
        sort_map = {
            "cele mai noi": f"COALESCE({prefix}added_date, '') DESC, {prefix}id DESC",
            "cele mai vechi": f"COALESCE({prefix}added_date, '') ASC, {prefix}id ASC",
            "nume a–z": f"{prefix}filename COLLATE NOCASE ASC, {prefix}id DESC",
            "nume a-z": f"{prefix}filename COLLATE NOCASE ASC, {prefix}id DESC",
            "dimensiune ↓": f"COALESCE({prefix}downloaded_size, {prefix}total_size, 0) DESC, {prefix}id DESC",
            "dimensiune": f"COALESCE({prefix}downloaded_size, {prefix}total_size, 0) DESC, {prefix}id DESC",
            "status": f"COALESCE({prefix}status, '') COLLATE NOCASE ASC, COALESCE({prefix}added_date, '') DESC, {prefix}id DESC",
        }
        return sort_map.get(key, sort_map["cele mai noi"])

    def get_history_page(self, limit=25, offset=0, search_term="", status_filter="toate",
                         period_filter="Toate", type_filter="Toate", sort_order="Cele mai noi"):
        """Fetch one filtered/sorted page in SQLite; only the visible page reaches Tkinter."""
        limit = safe_int(limit, 25, 1, 100)
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
        where, params = self._history_where_params(search_term, status_filter, period_filter, type_filter)
        order_by = self._history_order_by(sort_order)
        select_sql = """SELECT id, url, filename, save_path, status, total_size,
            downloaded_size, added_date, completed_date, speed_avg FROM history"""
        with self._connect(15) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM history" + where, params).fetchone()[0]
            rows = conn.execute(
                select_sql + where + " ORDER BY " + order_by + " LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            conn.commit()
        return [DownloadHistory(*row) for row in rows], int(total or 0)

    def export_history_csv(self, file_path, mode="all", search_term="", status_filter="toate",
                           period_filter="Toate", type_filter="Toate", sort_order="Cele mai noi",
                           selected_ids=(), cancel_event=None, progress_callback=None):
        """Stream a complete CSV export in bounded batches; never materialize history in RAM."""
        cancel_event = cancel_event or threading.Event()
        mode = str(mode or "all").lower()
        selected_ids = tuple(dict.fromkeys(int(value) for value in (selected_ids or ()) if str(value).isdigit()))

        with self._connect(20) as conn:
            conn.execute("PRAGMA busy_timeout=20000")
            if mode == "selected":
                if not selected_ids:
                    return {"status": "empty", "count": 0}
                conn.execute("CREATE TEMP TABLE selected_history_export (id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT OR IGNORE INTO selected_history_export(id) VALUES (?)",
                                 ((item_id,) for item_id in selected_ids))
                from_sql = "history h JOIN selected_history_export sx ON sx.id = h.id"
                where, params = "", []
            elif mode == "filtered":
                where, params = self._history_where_params(search_term, status_filter, period_filter, type_filter)
                from_sql = "history h"
            else:
                where, params = "", []
                from_sql = "history h"

            total = int(conn.execute(f"SELECT COUNT(*) FROM {from_sql}{where}", params).fetchone()[0] or 0)
            if total <= 0:
                return {"status": "empty", "count": 0}

            order_by = self._history_order_by(sort_order, prefix="h.")
            query = f"""SELECT h.id, h.url, h.filename, h.save_path, h.status,
                h.total_size, h.downloaded_size, h.added_date, h.completed_date, h.speed_avg
                FROM {from_sql}{where} ORDER BY {order_by}"""
            written = 0
            cancelled = False
            # Scriem într-un fișier temporar și mutăm la locul final doar la
            # finalizare: altfel, un export anulat sau o eroare lăsa fișierul ales
            # de utilizator trunchiat (sau complet gol).
            temp_path = f"{file_path}.part"
            try:
                with open(temp_path, "w", encoding="utf-8-sig", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(["ID", "URL", "Filename", "Path", "Status", "Total bytes",
                                     "Downloaded bytes", "Added", "Completed", "Average speed (bytes/s)"])
                    cursor = conn.execute(query, params)
                    while True:
                        if cancel_event.is_set():
                            cancelled = True
                            break
                        rows = cursor.fetchmany(500)
                        if not rows:
                            break
                        writer.writerows([[csv_safe_cell(cell) for cell in row] for row in rows])
                        written += len(rows)
                        if progress_callback:
                            progress_callback(written, total)
                if cancelled:
                    raise _ExportCancelled()
                os.replace(temp_path, file_path)
            except _ExportCancelled:
                return {"status": "cancelled", "count": written, "total": total}
            finally:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
            return {"status": "completed", "count": written, "total": total}

    def get_history_stats(self):
        """Return all-history summary statistics without materializing every row in Python."""
        current_month = datetime.now().strftime("%Y-%m")
        recent_cutoff = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        with self._connect(10) as conn:
            row = conn.execute("""SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status = 'Completed' THEN COALESCE(downloaded_size, 0) ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN substr(COALESCE(completed_date, ''), 1, 7) = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'Error' AND COALESCE(added_date, '') >= ? THEN 1 ELSE 0 END), 0)
                FROM history""", (current_month, recent_cutoff)).fetchone()
        return {
            "total_files": int(row[0] or 0),
            "total_size": int(row[1] or 0),
            "this_month": int(row[2] or 0),
            "recent_errors": int(row[3] or 0),
        }

    def clear_history(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM history')
            conn.commit()

    def save_setting(self, key: str, value):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, json.dumps(value)))
            conn.commit()

    def save_settings_batch(self, items):
        """Persist many settings in a single transaction.

        Saving the whole configuration used to open one connection and commit once per
        key; a single executemany keeps the same result but makes the write atomic
        and roughly an order of magnitude cheaper.
        """
        payload = [(key, json.dumps(value)) for key, value in items]
        if not payload:
            return
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.executemany('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', payload)
            conn.commit()

    def get_setting(self, key: str, default=None):
        try:
            with self._connect(10) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
                row = cursor.fetchone()
            return json.loads(row[0]) if row else default
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Could not read setting %s; using default: %s", key, exc)
            return default

    def get_all_settings(self) -> dict:
        """Read every persisted setting with a single query.

        The settings screen has 26+ keys; reading them one by one meant 26
        SELECT statements (plus 26 busy_timeout PRAGMAs) on every start-up.
        Values that cannot be decoded are skipped, so the caller falls back to
        its own default - same behaviour as get_setting().
        """
        try:
            with self._connect(10) as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error as exc:
            logger.warning("Could not read settings; using defaults: %s", exc)
            return {}
        result = {}
        for key, raw in rows:
            try:
                result[key] = json.loads(raw)
            except (ValueError, TypeError):
                continue
        return result

    def add_scheduled(self, sd: ScheduledDownload):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO scheduled (url, filename, save_path, scheduled_time,
                repeat_type, enabled, segments, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(),
                 sd.repeat, 1 if sd.enabled else 0, sd.segments, sd.checksum))
            conn.commit()

    def update_scheduled_time(self, sd: ScheduledDownload, previous_time: datetime) -> None:
        """Persist the next recurrence on the existing row instead of duplicating it."""
        with self._connect(10) as conn:
            cursor = conn.execute(
                "UPDATE scheduled SET scheduled_time = ? "
                "WHERE url = ? AND filename = ? AND COALESCE(save_path, '') = ? AND scheduled_time = ? AND repeat_type = ?",
                (sd.scheduled_time.isoformat(), sd.url, sd.filename, sd.save_path,
                 previous_time.isoformat(), sd.repeat))
            if cursor.rowcount == 0:
                # Rândul nu a fost găsit (migrare sau editare manuală): păstrăm
                # recurența inserând un rând nou, în loc să o pierdem.
                logger.warning("[DB] Recurența nu a fost actualizată; se inserează un rând nou.")
                cursor.execute('''INSERT INTO scheduled (url, filename, save_path, scheduled_time,
                    repeat_type, enabled, segments, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(),
                     sd.repeat, 1 if sd.enabled else 0, sd.segments, sd.checksum))

    def get_scheduled(self) -> List[ScheduledDownload]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            # Coloane explicite: `SELECT *` depinde de ordinea fizică a coloanelor,
            # iar ALTER TABLE pe baze vechi mută datele la sfârșitul rândului —
            # programările salvate dispăreau după actualizarea aplicației.
            cursor.execute('SELECT url, filename, save_path, scheduled_time, repeat_type, '
                           'enabled, segments, checksum FROM scheduled WHERE enabled = 1')
            rows = cursor.fetchall()
        result = []
        for row in rows:
            try:
                result.append(ScheduledDownload(url=row[0], filename=row[1], save_path=row[2] or "",
                    scheduled_time=datetime.fromisoformat(row[3]), repeat=row[4] or "once",
                    enabled=bool(row[5]), segments=row[6] or 1, checksum=row[7] or ""))
            except Exception as e:
                logger.error(f"[DB] Eroare la parsare scheduled: {e}")
        return result

    def remove_scheduled(self, sd: ScheduledDownload):
        with self._connect(10) as conn:
            conn.execute(
                "DELETE FROM scheduled WHERE url = ? AND filename = ? AND save_path = ? "
                "AND scheduled_time = ? AND repeat_type = ?",
                (sd.url, sd.filename, sd.save_path, sd.scheduled_time.isoformat(), sd.repeat))

    def save_active_queue(self, downloads: List["Download"]):
        """Persistă coada activă și setările Smart Queue într-o singură tranzacție."""
        rows = []
        for dl in downloads:
            with dl.lock:
                status = dl.status
                if status not in (DLStatus.QUEUED, DLStatus.DOWNLOADING, DLStatus.PAUSED):
                    continue
                rows.append((dl.url, dl.filename, dl.save_path, status.value,
                    max(0, int(dl.total or 0)), max(0, int(dl.downloaded or 0)),
                    max(1, safe_int(dl.segments, 1, 1, 32)), dl.checksum, dl.checksum_algo,
                    max(0, safe_int(dl.speed_limit, 0)), dl.added_date.isoformat(),
                    safe_int(dl.priority, 1, 0, 2), safe_int(dl.queue_order, 0, 0),
                    None if dl.speed_limit_override is None else max(0, safe_int(dl.speed_limit_override, 0)),
                    safe_int(getattr(dl, "automatic_retry_count", 0), 0, 0, 100),
                    max(0.0, float(getattr(dl, "auto_retry_at", 0.0) or 0.0))))
        with self._connect(10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute('DELETE FROM active_queue')
            conn.executemany("""INSERT INTO active_queue (
                url, filename, save_path, status, total_size, downloaded_size, segments,
                checksum, checksum_algo, speed_limit, added_date, priority, queue_order,
                speed_limit_override, automatic_retry_count, auto_retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            conn.commit()

    def load_active_queue(self) -> List[dict]:
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT url, filename, save_path, status, total_size, downloaded_size,
                segments, checksum, checksum_algo, speed_limit, added_date, priority, queue_order,
                speed_limit_override, automatic_retry_count, auto_retry_at FROM active_queue ORDER BY queue_order, id""")
            rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "url": row[0], "filename": row[1], "save_path": row[2], "status": row[3],
                "total": row[4] or 0, "downloaded": row[5] or 0, "segments": row[6] or 1,
                "checksum": row[7] or "", "checksum_algo": row[8] or "sha256",
                "speed_limit": row[9] or 0, "added_date": row[10],
                "priority": safe_int(row[11], 1, 0, 2), "queue_order": safe_int(row[12], 0, 0),
                "speed_limit_override": None if row[13] is None else max(0, safe_int(row[13], 0)),
                "automatic_retry_count": safe_int(row[14], 0, 0, 100),
                "auto_retry_at": max(0.0, float(row[15] or 0.0)),
            })
        return result

    def clear_active_queue(self):
        with self._connect(10) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM active_queue')
            conn.commit()

# ─── Config Manager ───────────────────────────────────────────────────────────
class ConfigManager:
    DEFAULTS = {
        "max_concurrent": 3,
        "queue_profile": "Balanced",
        "default_folder": os.path.expanduser("~/Downloads"),
        "speed_limit": 0,
        "theme_name": "Dark Modern",
        "theme_mode": "",
        "accent_color": "#3b82f6",
        "notifications": True,
        "clear_on_exit": False,
        "auto_sort": True,
        "clipboard_monitor": True,
        "av_scan": False,
        "segments_default": 1,
        "minimize_to_tray": True,
        "animations": True,
        "smart_retry_enabled": True,
        "smart_retry_max_attempts": 2,
        "smart_retry_base_delay": 5,
        "smart_queue_aging_enabled": True,
        "smart_queue_aging_minutes": 20,
        "smart_disk_guard_enabled": False,
        "smart_disk_min_free_mb": 512,
        "adaptive_streams_enabled": True,
        "adaptive_stream_max": 16,
        "endgame_enabled": True,
        "endgame_trigger_seconds": 4,
        "multi_network_enabled": False,
        "multi_network_selected": [],
        "network_speed_limits": {},
        "network_usage": {},
        "slow_mode_enabled": False,
        "slow_mode_limit_kbps": 512,
        "proxy_url": "",
    }

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.config = self.load()

    def load(self):
        """Overlay the persisted values on the defaults with a single query."""
        config = dict(self.DEFAULTS)
        stored = self.db.get_all_settings()
        for key in config:
            if key in stored:
                config[key] = stored[key]
        config["theme_mode"] = str(config.get("theme_mode") or "").lower()
        return config

    def save(self):
        self.db.save_settings_batch(list(self.config.items()))

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.db.save_setting(key, value)

    def set_many(self, items):
        """Update and persist a group of settings in a single transaction."""
        items = dict(items)
        if not items:
            return
        self.config.update(items)
        self.db.save_settings_batch(list(items.items()))

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

# ─── Download Scheduler ───────────────────────────────────────────────────────
class DownloadScheduler:
    def __init__(self, app, db: DatabaseManager):
        self.app = app
        self.db = db
        self.scheduled: List[ScheduledDownload] = []
        self.running = False
        self.thread = None
        # Numărul de încercări eșuate per programare, ca o eroare permanentă
        # să nu reîncerce la infinit la fiecare ciclu de 30s.
        self._failed_attempts = {}
        self._load_scheduled()

    def _load_scheduled(self):
        try:
            self.scheduled = self.db.get_scheduled()
            logger.info(f"[Scheduler] Încărcate {len(self.scheduled)} descărcări programate")
        except Exception as e:
            logger.error(f"[Scheduler] Eroare la încărcare: {e}")
            self.scheduled = []

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._check_scheduler, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def add(self, url: str, filename: str, schedule_time: datetime, save_path: str = "",
            repeat: str = "once", segments: int = 1, checksum: str = ""):
        sd = ScheduledDownload(url=url, filename=filename, scheduled_time=schedule_time,
            save_path=save_path, repeat=repeat, enabled=True, segments=segments, checksum=checksum)
        self.scheduled.append(sd)
        self.db.add_scheduled(sd)
        logger.info(f"[Scheduler] Adăugat: {filename} la {schedule_time}")

    def remove(self, sd: ScheduledDownload):
        if sd in self.scheduled: self.scheduled.remove(sd)
        self.db.remove_scheduled(sd)

    def _check_scheduler(self):
        while self.running:
            try:
                now = datetime.now()
                for sd in self.scheduled[:]:
                    if sd.enabled and now >= sd.scheduled_time:
                        logger.info(f"[Scheduler] Executare: {sd.filename}")
                        if sd.repeat == "once":
                            # Marcăm în memorie, dar NU ștergem rândul din baza de
                            # date: rândul dispare doar după ce transferul a fost
                            # acceptat. Altfel o eroare (disc plin, cale invalidă)
                            # pierdea programarea definitiv.
                            sd.enabled = False
                        self.app.post_ui(lambda x=sd: self._execute_download(x))
                        if sd.repeat in ("daily", "weekly"):
                            previous_time = sd.scheduled_time
                            interval = timedelta(days=1) if sd.repeat == "daily" else timedelta(weeks=1)
                            # If the app was closed for several intervals, advance to the next
                            # future occurrence rather than replaying an overdue schedule repeatedly.
                            next_time = previous_time + interval
                            while next_time <= now:
                                next_time += interval
                            sd.scheduled_time = next_time
                            self.db.update_scheduled_time(sd, previous_time)
                time.sleep(30)
            except Exception as e:
                logger.error(f"[Scheduler] Eroare: {e}")
                time.sleep(30)

    def _execute_download(self, sd: ScheduledDownload):
        folder = sd.save_path or self.app.config.get("default_folder")
        key = (sd.url, sd.filename, sd.save_path)
        try:
            self.app._add_download(sd.url, folder, sd.filename, sd.segments, sd.checksum)
        except Exception as exc:
            logger.error("[Scheduler] Nu s-a putut porni descărcarea programată %s: %s", sd.filename, exc)
            attempts = self._failed_attempts.get(key, 0) + 1
            self._failed_attempts[key] = attempts
            if attempts >= 3:
                logger.error("[Scheduler] Programarea %s a eșuat de %d ori; se dezactivează.",
                             sd.filename, attempts)
                sd.enabled = False
                self.remove(sd)
            else:
                # Reactivăm pentru următorul ciclu: rândul a rămas în baza de date.
                sd.enabled = True
            self.app.post_ui(lambda x=sd, e=exc: self._notify_schedule_failure(x, e))
            return
        self._failed_attempts.pop(key, None)
        # Abia acum ștergem programarea unică: transferul a fost acceptat.
        if sd.repeat == "once":
            self.remove(sd)

    def _notify_schedule_failure(self, sd: ScheduledDownload, exc: Exception):
        try:
            messagebox.showwarning(
                "Descărcare programată",
                f"Descărcarea programată „{sd.filename}” nu a putut porni:\n\n{exc}",
                parent=self.app)
        except Exception:
            logger.exception("Nu s-a putut afișa avertismentul pentru programare")


class SmartAutomationEngine:
    """Conservative, UI-thread-driven policies for retries, queue fairness and disk safety.

    It does not own download threads: the existing worker remains responsible for HTTP
    transfer/recovery, while this controller decides whether a failed item should be
    queued for another attempt and when an already-scheduled retry may run.
    """
    PERMANENT_MARKERS = (
        "checksum", "spațiu insuficient", "spatiu insuficient", "insufficient disk",
        "permission denied", "access denied", "forbidden", "http 401",
        "http 403", "http 404", "status 401", "status 403", "status 404",
        "status 410", "invalid url", "url invalid", "unsupported scheme",
    )
    TRANSIENT_MARKERS = (
        "timed out", "timeout", "read timed out", "connection reset", "connection aborted",
        "connection error", "server disconnected", "remote end closed", "temporarily unavailable",
        "temporary failure", "network is unreachable", "network unreachable", "proxy error",
        "incomplete download", "segment download failed", "incomplete read", "chunked encoding",
        "http 408", "http 425", "http 429", "http 500", "http 502", "http 503", "http 504",
        "status 408", "status 425", "status 429", "status 500", "status 502", "status 503", "status 504",
        "connection refused", "remote host closed", "broken pipe",
    )

    def __init__(self, app):
        self.app = app
        self.block_reason = ""
        self._last_disk_check = 0.0
        self._last_retry_countdown_bucket = None

    def retryable_error(self, error: str) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        if any(marker in text for marker in self.PERMANENT_MARKERS):
            return False
        # requests commonly formats status errors as "503 Server Error", without
        # the words "HTTP" or "status". Keep permanent client errors separate.
        if re.search(r"\b(?:400|401|403|404|405|410|422)\b", text):
            return False
        if any(marker in text for marker in self.TRANSIENT_MARKERS):
            return True
        if re.search(r"\b(?:408|425|429|500|502|503|504)\b", text):
            return True
        # Unknown errors are deliberately not retried automatically. The download
        # worker already performs its own bounded local retries first.
        return False

    def schedule_retry_if_transient(self, dl: Download) -> bool:
        cfg = self.app.config
        if not bool(cfg.get("smart_retry_enabled", True)):
            return False
        if dl.status in (DLStatus.CANCELLED, DLStatus.COMPLETED):
            return False
        if not self.retryable_error(dl.error_msg):
            return False
        max_attempts = safe_int(cfg.get("smart_retry_max_attempts", 2), 2, 0, 8)
        current = safe_int(getattr(dl, "automatic_retry_count", 0), 0, 0, 100)
        if current >= max_attempts:
            return False
        base_delay = safe_int(cfg.get("smart_retry_base_delay", 5), 5, 1, 120)
        delay = min(300, base_delay * (2 ** current))
        original_error = str(dl.error_msg or "Eroare de rețea").strip()
        with dl.lock:
            dl.automatic_retry_count = current + 1
            dl.auto_retry_at = time.time() + delay
            dl.status = DLStatus.QUEUED
            dl.speed = 0.0
            dl.resume_requested = False
            dl.error_msg = (f"{original_error} · reîncercare automată "
                            f"{dl.automatic_retry_count}/{max_attempts} în {delay}s")
        logger.warning("[Smart Automation] %s: retry %s/%s in %ss (%s)",
                       dl.filename, dl.automatic_retry_count, max_attempts, delay, original_error[:180])
        try:
            self.app.db.save_active_queue(self.app.downloads)
        except Exception as exc:
            logger.warning("Could not persist scheduled automatic retry: %s", exc)
        return True

    def retry_is_ready(self, dl: Download, now: Optional[float] = None) -> bool:
        due = max(0.0, float(getattr(dl, "auto_retry_at", 0.0) or 0.0))
        return due <= (time.time() if now is None else now)

    def queue_score(self, dl: Download):
        """Apply aging so a long-waiting Low/Normal item is not starved indefinitely."""
        priority = safe_int(getattr(dl, "priority", 1), 1, 0, 2)
        if bool(self.app.config.get("smart_queue_aging_enabled", True)):
            aging_minutes = safe_int(self.app.config.get("smart_queue_aging_minutes", 20), 20, 1, 1440)
            added = getattr(dl, "added_date", None)
            if isinstance(added, datetime):
                waited_minutes = max(0.0, (datetime.now() - added).total_seconds() / 60.0)
                priority = min(2, priority + int(waited_minutes // aging_minutes))
        order = safe_int(getattr(dl, "queue_order", 0), 0, 0)
        added_ts = dl.added_date.timestamp() if isinstance(getattr(dl, "added_date", None), datetime) else 0
        return (-priority, order if order else 10**12, added_ts)

    def _free_space_for(self, dl: Download) -> Optional[int]:
        path = os.path.abspath(os.path.expanduser(getattr(dl, "save_path", "") or
                                                  self.app.config.get("default_folder", "~")))
        while not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        try:
            return int(shutil.disk_usage(path).free)
        except (OSError, ValueError):
            return None

    def can_start(self, dl: Download) -> bool:
        if not bool(self.app.config.get("smart_disk_guard_enabled", False)):
            self.block_reason = ""
            return True
        free = self._free_space_for(dl)
        minimum_mb = safe_int(self.app.config.get("smart_disk_min_free_mb", 512), 512, 0, 1024 * 1024)
        minimum = minimum_mb * 1024 * 1024
        if free is not None and free >= minimum:
            self.block_reason = ""
            return True
        self.block_reason = (f"Pornire suspendată: spațiu liber sub {minimum_mb} MB"
                             if free is not None else "Pornire suspendată: spațiul liber nu poate fi verificat")
        return False

    def tick(self) -> bool:
        """Release due retry timers. Returns True when queue/UI state needs refreshing."""
        now = time.time()
        changed = False
        for dl in tuple(getattr(self.app, "downloads", ())):
            if dl.status != DLStatus.QUEUED or not getattr(dl, "auto_retry_at", 0.0):
                continue
            if now < float(dl.auto_retry_at):
                # Refresh the countdown only when the visible second changes, not on
                # every 100 ms UI poll. This keeps the card animation lightweight.
                bucket = max(1, int(math.ceil(float(dl.auto_retry_at) - now)))
                if getattr(dl, "_auto_retry_countdown_bucket", None) != bucket:
                    dl._auto_retry_countdown_bucket = bucket
                    changed = True
                    try:
                        self.app._refresh_card(dl)
                    except Exception:
                        pass
                continue
            if dl.thread is not None and dl.thread.is_alive():
                continue
            with dl.lock:
                dl.auto_retry_at = 0.0
                dl.error_msg = ""
                dl.speed = 0.0
                # Counters are rebuilt from the actual file/segment artifacts by the
                # worker. Retain the partial files, but do not carry stale byte counts
                # into a retry (which would double-count completed segments).
                dl.downloaded = 0
                dl.progress = 0.0
                dl.chunk_progress = []
                dl.speed_tracker.samples.clear()
            logger.info("[Smart Automation] Retry ready: %s", dl.filename)
            changed = True
            try:
                self.app._refresh_card(dl)
            except Exception:
                pass
        # Re-check a paused queue if disk space may have become available.
        if self.block_reason and now - self._last_disk_check >= 3.0:
            self._last_disk_check = now
            pending = next((d for d in self.app.downloads if d.status == DLStatus.QUEUED), None)
            if pending is not None and self.can_start(pending):
                changed = True
        return changed

# ─── Download Worker (ALL FIXES) ──────────────────────────────────────────────

def _record_session_transfer(dl: "Download", bytes_count: int) -> None:
    """Count bytes actually read from the network for this session, even if a card is removed."""
    count = max(0, safe_int(bytes_count, 0))
    if count <= 0:
        return
    with dl.lock:
        dl.session_transferred = max(0, int(getattr(dl, "session_transferred", 0) or 0)) + count
    app = getattr(dl, "app", None)
    lock = getattr(app, "_session_transfer_lock", None) if app is not None else None
    if lock is not None:
        with lock:
            app._session_transferred_total = max(0, int(getattr(app, "_session_transferred_total", 0) or 0)) + count


def _apply_speed_limit(dl: "Download", bytes_count: int, network_key: str = "system"):
    """Enforce global, per-download, optional slow-mode and per-network speed caps."""
    app = dl.app
    global_limit = 0
    try:
        global_limit = max(0, safe_int(app.config.get("speed_limit", 0), 0)) if app else 0
        if app and app.config.get("slow_mode_enabled", False):
            slow_limit = max(1, safe_int(app.config.get("slow_mode_limit_kbps", 512), 512)) * 1024
            global_limit = min(global_limit, slow_limit) if global_limit > 0 else slow_limit
    except Exception:
        global_limit = max(0, safe_int(getattr(dl, "speed_limit", 0), 0))
    per_download_limit = getattr(dl, "speed_limit_override", None)
    per_download_limit = None if per_download_limit is None else max(0, safe_int(per_download_limit, 0))
    if bytes_count <= 0 and not network_key:
        return

    waits = []
    now = time.monotonic()
    if global_limit > 0 and bytes_count > 0:
        owner = app if app is not None else dl
        lock = getattr(owner, "_bandwidth_lock", dl.throttle_lock)
        with lock:
            deadline = max(now, getattr(owner, "_bandwidth_deadline", 0.0))
            wait_for = max(0.0, deadline - now)
            new_deadline = deadline + bytes_count / global_limit
            if hasattr(owner, "_bandwidth_deadline"):
                owner._bandwidth_deadline = new_deadline
            else:
                dl.throttle_deadline = new_deadline
        waits.append(wait_for)

    if per_download_limit is not None and per_download_limit > 0 and bytes_count > 0:
        with dl.throttle_lock:
            deadline = max(now, dl.throttle_deadline)
            wait_for = max(0.0, deadline - now)
            dl.throttle_deadline = deadline + bytes_count / per_download_limit
        waits.append(wait_for)

    manager = getattr(app, "network_manager", None) if app is not None else None
    if manager is not None and bytes_count > 0:
        waits.append(manager.throttle_wait(network_key, bytes_count))

    wait_until = time.monotonic() + max(waits or [0.0])
    while True:
        if dl.stop_event and dl.stop_event.is_set():
            return
        if dl.pause_event and not dl.pause_event.is_set():
            dl.pause_event.wait(timeout=0.25)
            continue
        remaining = wait_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def _download_meta_path(save_path: str) -> str:
    return f"{save_path}.catssoft-meta.json"


def _read_download_meta(save_path: str) -> dict:
    try:
        with open(_download_meta_path(save_path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_download_meta(save_path: str, data: dict) -> None:
    meta_path = _download_meta_path(save_path)
    temp_path = meta_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, meta_path)
    except OSError as exc:
        logger.warning("Could not persist resume metadata for %s: %s", os.path.basename(save_path), exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _remove_download_meta(save_path: str) -> None:
    for candidate in (_download_meta_path(save_path), _download_meta_path(save_path) + ".tmp"):
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError:
            pass


def _validate_or_reset_resume_artifacts(dl: Download, total: int, headers: dict,
                                        allow_restart_without_validator: bool = True) -> str:
    """Check HTTP validators before reusing persisted partial data; returns current validator."""
    get_header = lambda name: _get_ci_header(headers, name, "") if headers else ""
    etag = str(get_header("etag") or "").strip()
    last_modified = str(get_header("last-modified") or "").strip()
    current_validator = etag or last_modified
    old = _read_download_meta(dl.save_path)
    old_etag = str(old.get("etag") or "").strip()
    old_last_modified = str(old.get("last_modified") or "").strip()
    old_validator = old_etag or old_last_modified
    old_total = safe_int(old.get("total", 0), 0, 0)
    old_url = str(old.get("url") or "")
    # Validatoarele se compară „de același fel”: un ETag vechi nu trebuie comparat
    # cu un Last-Modified nou — tipuri diferite, deci o comparație care produce
    # întotdeauna o diferență falsă și invalida reluarea fără motiv.
    validator_conflict = (
        (bool(old_etag) and bool(etag) and old_etag != etag)
        or (bool(old_last_modified) and bool(last_modified) and old_last_modified != last_modified)
    )
    changed = bool(old) and (
        (old_url and old_url != dl.url)
        or (old_total > 0 and total > 0 and old_total != total)
        or validator_conflict
    )
    # Segment files from older versions may exist without a matching metadata file.
    # They are not trusted because their URL/size/validator cannot be confirmed.
    orphaned_segments = False
    try:
        base = os.path.basename(dl.save_path)
        pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
        orphaned_segments = any(pattern.fullmatch(item.name)
                                for item in Path(dl.save_path).parent.iterdir()) and not old
    except OSError:
        pass
    orphaned_final = False
    if os.path.isfile(dl.save_path):
        try:
            existing_final_size = os.path.getsize(dl.save_path)
            if existing_final_size > 0:
                # Un fișier final fără metadate care să-l corespundă nu poate fi
                # dovedit ca fiind resursa curentă. Acceptarea lui ar raporta
                # COMPLET fără un octet transferat și ar „ștampila" fișierul vechi
                # cu URL-ul și validatorul curente.
                orphaned_final = (not old) or total <= 0 or existing_final_size != total
        except OSError:
            pass
    if changed or orphaned_segments or orphaned_final:
        logger.warning("Resource changed for %s; discarding stale partial data", dl.filename)
        _remove_segment_parts(dl.save_path)
        if changed:
            try:
                # Doar o schimbare dovedită justifică ștergerea fișierului final.
                if os.path.isfile(dl.save_path):
                    os.remove(dl.save_path)
            except OSError as exc:
                logger.warning("Could not remove stale output: %s", exc)
        elif orphaned_final:
            # Fișierul existent nu poate fi verificat (lipsesc metadatele): nu îl
            # ștergem, dar nici nu reluăm peste el — transferul pornește de la zero
            # și va suprascrie fișierul, în loc să concateneze octeți străini.
            dl.resume_blocked = True
            logger.warning("Existing output for %s cannot be verified; restarting without resume",
                           dl.filename)
        with dl.lock:
            dl.downloaded = 0
            dl.progress = 0.0
            dl.chunk_progress = []
    validator = current_validator or old_validator
    restarted_without_validator = False
    if not validator and allow_restart_without_validator and _has_resume_artifacts(dl.save_path):
        # Fără ETag și fără Last-Modified nu există NICIO dovadă că octeții aflați
        # pe disc aparțin resursei curente: serverul ar returna 206 cu conținut NOU
        # pentru intervalul cerut, iar rezultatul ar fi un fișier care amestecă două
        # versiuni - raportat COMPLET, pentru că dimensiunea se potrivește.
        # Renunțăm la reluare și pornim de la zero.
        logger.warning("Serverul nu expune validator HTTP pentru %s; reluarea este nesigură, "
                       "se reia de la zero", dl.filename)
        _remove_segment_parts(dl.save_path)
        dl.resume_blocked = True
        restarted_without_validator = True
        with dl.lock:
            dl.downloaded = 0
            dl.progress = 0.0
            dl.chunk_progress = []
    dl.resume_validator = validator
    # Fișierul final poate fi considerat complet doar dacă metadatele sale corespund
    # resursei curente; altfel există riscul de a-l raporta COMPLET fără transfer.
    dl.final_verified = (bool(old) and not changed and not orphaned_final
                         and not restarted_without_validator)
    _write_download_meta(dl.save_path, {
        "url": dl.url, "total": int(total or old_total or 0), "etag": etag or old.get("etag", ""),
        "last_modified": last_modified or old.get("last_modified", ""),
        "updated_at": datetime.now().isoformat(),
    })
    return validator


def check_disk_space(save_path: str, required_bytes: int, margin: float = 1.05) -> bool:
    """FEATURE NOU: verifică dacă mai e loc pe disc (cu marjă 5%) înainte să pornească
    efectiv descărcarea, ca să nu eșueze abia la final după ce a consumat bandă degeaba."""
    try:
        folder = os.path.dirname(save_path) or "."
        os.makedirs(folder, exist_ok=True)
        free = shutil.disk_usage(folder).free
        return free >= required_bytes * margin
    except Exception:
        return True  # dacă nu putem verifica, nu blocăm descărcarea


def _merge_space_available(save_path: str, total: int) -> bool:
    """Există loc pe disc pentru asamblarea segmentelor în calea cu segmente?

    Asamblarea scrie o copie temporară a întregului fișier (temp + os.replace),
    deci în momentul asamblării discul trebuie să conțină simultan segmentele
    descărcate ȘI copia finală: de două ori dimensiunea fișierului.

    Fără această verificare, o descărcare pe segmente pornită cu puțin mai
    mult spațiu liber decât dimensiunea fișierului consumă tot bandwidth-ul și
    apoi eșuează la asamblare cu „no space left on device".
    """
    if total <= 0:
        return True
    try:
        folder = os.path.dirname(save_path) or "."
        free = shutil.disk_usage(folder).free
    except (OSError, ValueError):
        return True  # nu putem verifica -> nu blocăm descărcarea
    return free >= total * 2


def _remove_segment_parts(save_path: str):
    """Șterge doar fișierele .part cu sufix numeric ale descărcării curente."""
    if not save_path:
        return
    base = os.path.basename(save_path)
    parent = Path(save_path).parent
    pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
    try:
        for candidate in parent.iterdir():
            if pattern.fullmatch(candidate.name):
                try:
                    candidate.unlink()
                except OSError:
                    logger.warning("Nu am putut șterge segmentul temporar %s", candidate)
    except OSError as exc:
        logger.warning("Nu am putut inspecta segmentele temporare pentru %s: %s", save_path, exc)


def _has_resume_artifacts(save_path: str) -> bool:
    """Return True when partial data already binds the transfer to this destination path."""
    try:
        if os.path.isfile(save_path) and os.path.getsize(save_path) > 0:
            return True
    except OSError:
        pass
    base = os.path.basename(save_path)
    pattern = re.compile(re.escape(base) + r"\.part\d+(?:\.race.*)?$")
    try:
        return any(pattern.fullmatch(candidate.name) for candidate in Path(save_path).parent.iterdir())
    except OSError:
        return False


def _segment_bytes_on_disk(save_path: str) -> int:
    """Total bytes already stored in the ``.partN`` ranges of this download.

    Used to compute the space still required on disk: the ranges already on disk
    occupy room, so asking for the whole file again would wrongly report
    "insufficient disk space" while resuming a large download.
    """
    if not save_path:
        return 0
    base = os.path.basename(save_path)
    parent = Path(save_path).parent
    pattern = re.compile(re.escape(base) + r"\.part\d+$")
    total = 0
    try:
        for candidate in parent.iterdir():
            if pattern.fullmatch(candidate.name):
                try:
                    total += candidate.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _reconcile_downloaded_with_disk(dl: Download) -> int:
    """Align ``dl.downloaded`` with the bytes that really exist in the output file.

    The counter can come from the database (a restored queue) and may be stale:
    the file may have been truncated, replaced or removed in the meantime. Using
    a stale value as the Range offset makes the server fill the wrong interval -
    the final size check still passes and the file is reported COMPLETED while
    the first bytes are missing and the last ones duplicated.
    """
    on_disk = 0
    if getattr(dl, "supports_resume", False) and not getattr(dl, "resume_blocked", False):
        try:
            if os.path.isfile(dl.save_path):
                on_disk = os.path.getsize(dl.save_path)
        except OSError:
            on_disk = 0
    with dl.lock:
        dl.downloaded = on_disk
        dl.progress = (min(99.9, on_disk / dl.total * 100.0)
                       if getattr(dl, "total", 0) > 0 else 0.0)
    return on_disk


# Fișierele temporare create de motor: segmente, leftovers din „race" și metadate.
_TEMP_FILE_PATTERN = re.compile(r"\.part\d+(?:\.race\d+)?$|\.catssoft-meta\.json(?:\.tmp)?$")


def scan_orphan_temp_files(folder: str, protected_bases=(), min_age_seconds: int = 3600):
    """FEATURE NOU: găsește fișierele temporare fără owner (segmente orfane).

    ``protected_bases`` conține numele de fișier ale descărcărilor active; orice
    fișier temporar care începe cu ele este considerat în uz și nu este oferit
    pentru ștergere. Returnează o listă de tuple (cale, dimensiune, vârstă_s).
    """
    protected = [str(base) for base in (protected_bases or ()) if base]
    found = []
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return found
    now = time.time()
    for entry in entries:
        name = entry.name
        if not _TEMP_FILE_PATTERN.search(name):
            continue
        if any(name.startswith(base + ".") for base in protected):
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age < min_age_seconds:
            continue
        found.append((str(entry), stat.st_size, age))
    return found


def _find_mpcmdrun() -> Optional[str]:
    """Localizează MpCmdRun.exe (scanner-ul CLI al Windows Defender). Verifică întâi
    calea clasică, apoi caută în folderul versionat de platformă (calea se schimbă la
    fiecare update al Defender)."""
    candidates = [os.path.expandvars(r"%ProgramFiles%\Windows Defender\MpCmdRun.exe")]
    platform_root = os.path.expandvars(r"%ProgramData%\Microsoft\Windows Defender\Platform")
    try:
        if os.path.isdir(platform_root):
            versions = sorted(os.listdir(platform_root), reverse=True)
            candidates += [os.path.join(platform_root, v, "MpCmdRun.exe") for v in versions]
    except OSError:
        pass
    return next((c for c in candidates if os.path.exists(c)), None)


def _defender_threat_names(output: str) -> list:
    """Extrage numele amenințărilor din raportul text al lui MpCmdRun."""
    names = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("threat") and ":" in stripped:
            value = stripped.split(":", 1)[1].strip()
            if value and value not in names:
                names.append(value)
    return names


def scan_with_defender(path: str, timeout: int = 120) -> dict:
    """FEATURE NOU: scanează un fișier cu Windows Defender după finalizarea descărcării.

    Invocația este ``MpCmdRun.exe -Scan -ScanType 3 -DisableRemediation -File <fisier>``.
    Aceasta e singura variantă care scannează efectiv un fișier individual și dă un
    verdict, verificat empiric pe Windows 11 cu Defender 1.1.26100.4:

    - ``-ScanType 3 -File`` (invocația anterioară) **sărește** fișierul
      („Scanning ... was skipped.") și totuși întoarce cod 0 - deci aplicația
      raporta „curat" fără să fi scanat vreun octet;
    - ``-ScanType 1 -File`` rulează, dar nu produce niciun verdict și nu detectează
      niciun fișier de test (EICAR) - deci nu e util pentru o verificare;
    - ``-ScanType 3 -DisableRemediation -File`` scanează efectiv și raportează
      „found no threats" (cod 0) sau „found N threats" + lista amenințărilor
      (cod 2).

    Doar Windows; pe alte OS-uri sau dacă MpCmdRun nu e găsit, se întoarce
    ran=False fără să blocheze nimic. Remedierea rămâne responsabilitatea
    Defender-ului: protecția în timp real acționează deja în timpul descărcării,
    iar dacă fișierul dispare oricum, aplicația marchează descărcarea ca eroare.
    Aplicația nu ia decizii de ștergere în locul utilizatorului.

    Valoarea 'clean':
      True  - scanare efectuată, niciun incident semnalat
      False - scanare efectuată, incident semnalat (utilizatorul vede output-ul brut)
      None  - scanarea nu a putut fi efectuată (sărită, timeout, eroare)
    """
    result = {"ran": False, "clean": None, "output": "", "returncode": None,
              "skipped": False, "threats": []}
    if sys.platform != "win32":
        return result

    mpcmdrun = _find_mpcmdrun()
    if not mpcmdrun:
        result["output"] = "MpCmdRun.exe nu a fost găsit"
        return result

    try:
        proc = subprocess.run(
            [mpcmdrun, "-Scan", "-ScanType", "3", "-DisableRemediation", "-File", path],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        result["ran"] = True
        result["returncode"] = proc.returncode
        result["output"] = (proc.stdout or "") + (proc.stderr or "")
        output = result["output"]
        lowered = output.lower()
        if "was skipped" in lowered:
            # Defender a refuzat să scaneze fișierul (excluzie de cale sau de
            # extensie). Nu putem afirma că e curat - starea rămâne necunoscută.
            result["skipped"] = True
            result["clean"] = None
        elif "no threats" in lowered:
            result["clean"] = True
        elif "threat" in lowered and ("found" in lowered or "detected" in lowered):
            result["clean"] = False
            result["threats"] = _defender_threat_names(output)
        else:
            # Fără verdict în text: ne bazăm pe codul de ieșire, dar rămânem
            # prudenți - un cod nenul înseamnă „necesită atenție".
            result["clean"] = (proc.returncode == 0)
    except subprocess.TimeoutExpired:
        result["output"] = "Timeout la scanare"
    except Exception as e:
        result["output"] = str(e)

    return result


class AdaptiveStreamController:
    """Adjust active range workers in response to successful/failed range requests."""
    def __init__(self, initial_workers: int, max_workers: int, enabled: bool = True):
        self.max_workers = max(1, int(max_workers))
        self.min_workers = 1
        self.target_workers = max(1, min(self.max_workers, int(initial_workers)))
        self.enabled = bool(enabled)
        if not self.enabled:
            self.target_workers = self.max_workers
        self.active_workers = 0
        self.success_streak = 0
        self._goodput_samples = []
        self._condition = threading.Condition()

    def acquire(self, stop_event=None, pause_event=None):
        with self._condition:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                if pause_event is not None and not pause_event.is_set():
                    self._condition.wait(timeout=0.2)
                    continue
                if self.active_workers < self.target_workers:
                    self.active_workers += 1
                    return True
                self._condition.wait(timeout=0.2)

    def release(self):
        with self._condition:
            self.active_workers = max(0, self.active_workers - 1)
            self._condition.notify_all()

    def observe(self, success: bool, elapsed: float, byte_count: int, cancelled=False):
        with self._condition:
            if not self.enabled or cancelled:
                self._condition.notify_all()
                return
            if success:
                goodput = max(0, int(byte_count)) / max(0.01, float(elapsed))
                self._goodput_samples.append(goodput)
                self._goodput_samples = self._goodput_samples[-8:]
                # Only add concurrency when ranges complete quickly enough to suggest
                # the server/network is benefiting from parallelism. Slow, throttled
                # blocks do not automatically trigger ever-higher request counts.
                if goodput >= 64 * 1024:
                    self.success_streak += 1
                else:
                    self.success_streak = 0
                if self.success_streak >= 2 and self.target_workers < self.max_workers:
                    recent_avg = sum(self._goodput_samples[-4:]) / max(1, len(self._goodput_samples[-4:]))
                    if recent_avg >= 64 * 1024:
                        self.target_workers += 1
                        self.success_streak = 0
                        logger.debug("Adaptive streams increased to %s (recent range goodput %.0f B/s)",
                                     self.target_workers, recent_avg)
            else:
                self.success_streak = 0
                if self.target_workers > self.min_workers:
                    self.target_workers -= 1
                    logger.debug("Adaptive streams reduced to %s after a range failure", self.target_workers)
            self._condition.notify_all()


class DownloadResourceChangedError(Exception):
    """Raised when resume validators indicate the server representation changed."""


def _download_worker_entry(dl: Download, update_q: queue.Queue):
    """Entry point thread-safe: transformă excepțiile neașteptate în erori UI vizibile."""
    try:
        download_segmented(dl, update_q)
    except BaseException as exc:
        logger.exception("Unexpected worker failure for %s", getattr(dl, "filename", "download"))
        dl.error_msg = f"Eroare internă: {exc}"
        update_q.put(("status", dl, DLStatus.CANCELLED if dl.stop_event and dl.stop_event.is_set() else DLStatus.ERROR))
    finally:
        dl.active_response = None
        # Niciun răspuns HTTP nu mai trebuie urmărit după ieșirea din worker.
        try:
            with dl.lock:
                dl.active_responses.clear()
        except Exception:
            pass
        update_q.put(("worker_finished", dl, None))


def download_segmented(dl: Download, update_q: queue.Queue, max_retries: int = 3):
    """FIX: Progress corect bazat pe dl.downloaded / dl.total"""

    for attempt in range(max_retries):
        try:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            if attempt > 0:
                logger.info(f"Retry attempt {attempt + 1}/{max_retries}")
                with dl.lock:
                    dl.downloaded = 0  # FIX: Reset corect
                    dl.progress = 0.0
                    dl.chunk_progress = []
                # Keep already verified range files: the next pass reuses complete
                # blocks and resumes partial ones. Stale blocks are removed only when
                # the server validator/size check proves the resource changed.
                with dl.lock:
                    dl.retry_count += 1

            total = 0
            supports_ranges = False
            final_url = dl.url
            response_headers = {}

            try:
                with SESSION.head(dl.url, timeout=10, allow_redirects=True) as head:
                    total = int(head.headers.get('content-length', 0))
                    supports_ranges = head.headers.get('Accept-Ranges', '').lower() == 'bytes'
                    final_url = head.url
                    has_disposition = bool(head.headers.get('content-disposition'))
                    response_headers = {k.lower(): v for k, v in head.headers.items()}

                if not has_disposition:
                    logger.info("No Content-Disposition in HEAD, trying GET...")
                    with SESSION.get(dl.url, stream=True, timeout=10,
                                      headers={"Range": "bytes=0-0"}) as test_get:
                        response_headers = dict(test_get.headers)
                        final_url = test_get.url

            except Exception as e:
                logger.warning(f"HEAD request failed, using GET: {e}")
                try:
                    with SESSION.get(dl.url, stream=True, timeout=10) as resp:
                        total = int(resp.headers.get('content-length', 0))
                        supports_ranges = resp.headers.get('Accept-Ranges', '').lower() == 'bytes'
                        final_url = resp.url
                        response_headers = {k.lower(): v for k, v in resp.headers.items()}
                except Exception:
                    pass

            # Unele servere nu declară Accept-Ranges, iar altele ignoră HEAD.
            # Proba reală determină atât suportul Range, cât și totalul din Content-Range.
            if not supports_ranges:
                range_ok, probed_total, probe_headers, probe_url = probe_range(dl.url)
                supports_ranges = range_ok
                if total <= 0 and probed_total > 0:
                    total = probed_total
                if probe_headers:
                    response_headers = {str(k).lower(): v for k, v in probe_headers.items()}
                    final_url = probe_url or final_url

            # FIX: Verifică dacă total este valid
            if total <= 0:
                logger.warning("Content-Length is 0 or invalid, progress will be estimated")
                # NU setăm total = 1, lăsăm să fie 0 și gestionăm în UI

            # FEATURE NOU: verificare spațiu disc disponibil înainte de a începe efectiv.
            # La reluare verificăm doar octeții rămași: cei deja descărcați ocupă
            # deja loc pe disc, iar cererea lor ar respinde fals "spațiu insuficient".
            # CONTORUL dl.downloaded nu poate fi folosit aici: pe o reluare din coadă
            # este 0 (sau vechi), în timp ce octeții reali stau în fișierele .partN.
            bytes_on_disk = _segment_bytes_on_disk(dl.save_path)
            remaining_required = max(0, total - bytes_on_disk)
            if remaining_required > 0 and not check_disk_space(dl.save_path, remaining_required):
                dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FIX: numele fișierului folosește acum final_url (după redirect) + headerele
            # deja obținute, în loc să arunce final_url și să reinterogheze serverul.
            if response_headers:
                suggested_name = filename_from_url(dl.url, response_headers, final_url)
                if (suggested_name and suggested_name != dl.filename and not dl.filename_locked
                        and not _has_resume_artifacts(dl.save_path)):
                    logger.info(f"Updating filename: {dl.filename} -> {suggested_name}")
                    proposed_path = os.path.join(os.path.dirname(dl.save_path), sanitize_filename(suggested_name))
                    if os.path.abspath(proposed_path) != os.path.abspath(dl.save_path):
                        if os.path.exists(proposed_path):
                            proposed_path = unique_target_path(proposed_path)
                        dl.save_path = proposed_path
                        dl.filename = os.path.basename(proposed_path)
                        if dl.app:
                            update_q.put(("refresh", dl, None))

            dl.supports_resume = supports_ranges
            dl.total = total  # FIX: Setează total CORECT
            # Renunțarea la reluare fără validator se face doar la prima încercare:
            # ulterior, datele de pe disc pot fi deja ale noastre.
            _validate_or_reset_resume_artifacts(
                dl, total, response_headers, allow_restart_without_validator=(attempt == 0))

            adaptive_enabled = bool(dl.app and dl.app.config.get("adaptive_streams_enabled", True))
            if total == 0 or not supports_ranges or (dl.segments <= 1 and not adaptive_enabled):
                return download_simple(dl, update_q, max_retries)

            # Calea cu segmente asamblează la final segmentele într-o copie
            # temporară a întregului fișier: discul trebuie să țină simultan
            # segmentele ȘI copia, adică de două ori dimensiunea fișierului.
            # Dacă locul nu ajunge, descărcarea ar consuma tot bandwidth-ul și
            # ar eșua abia la asamblare. În acest caz coborâm pe calea simplă:
            # scrie direct în fișierul final și nu are nevoie de spațiu în plus.
            # Nu schimbăm calea dacă există deja segmente pe disc (reluare) sau
            # dacă nu e prima încercare (retry): acolo segmentele trebuie păstrate.
            if attempt == 0 and total > 0 and not _has_resume_artifacts(dl.save_path):
                if not _merge_space_available(dl.save_path, total):
                    logger.info("Spațiu insuficient pentru asamblare (%s) - descărcare simplă",
                                dl.filename)
                    return download_simple(dl, update_q, max_retries)

            # FEATURE NOU: dacă fișierul final există deja complet (ex: merge-ul s-a
            # terminat chiar înainte ca aplicația să se închidă, între sesiuni), nu mai
            # redescărcăm totul de la zero prin segmente.
            # DOAR dacă fișierul poate fi dovedit ca fiind resursa curentă: altfel
            # raportăm COMPLET un fișier vechi, fără să transferăm un octet.
            already_complete = (getattr(dl, "final_verified", False)
                                and os.path.exists(dl.save_path)
                                and os.path.getsize(dl.save_path) == total)

            if not already_complete:
                # Reconstruct the logical byte count from on-disk segments below. A
                # restored Download object may already contain a DB progress value;
                # retaining it here would count every recovered segment twice.
                with dl.lock:
                    dl.downloaded = 0
                    dl.progress = 0.0
                    dl.speed = 0.0
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                # Plexo-style work queue: workers claim the next missing range when
                # they finish, so fast connections naturally take more of the file.
                # Keep a Plexo-like visible chunk map without creating thousands of
                # tiny files for large downloads (target about 96 ranges, 1-8 MiB).
                mib = 1024 * 1024
                range_bytes = max(mib, min(8 * mib,
                                           ((total + 96 * mib - 1) // (96 * mib)) * mib))
                range_count = (total + range_bytes - 1) // range_bytes
                segment_files = [f"{dl.save_path}.part{i}" for i in range(range_count)]
                with dl.lock:
                    dl.chunk_size_bytes = range_bytes
                    dl.chunk_progress = []
                    for i, segment_path in enumerate(segment_files):
                        start = i * range_bytes
                        expected = min(total - start, range_bytes)
                        existing = os.path.getsize(segment_path) if os.path.exists(segment_path) else 0
                        dl.chunk_progress.append(min(1.0, existing / expected) if expected else 0.0)
                work_queue = queue.Queue()
                for i in range(range_count):
                    start = i * range_bytes
                    end = min(total - 1, start + range_bytes - 1)
                    work_queue.put((i, start, end))

                thread_errors = []  # entries: (chunk_index | None, message)
                thread_errors_lock = threading.Lock()
                resource_changed_event = threading.Event()
                adaptive_enabled = bool(dl.app and dl.app.config.get("adaptive_streams_enabled", True))
                max_streams = max(1, safe_int(
                    dl.app.config.get("adaptive_stream_max", 16), 16, 1, 32)) if adaptive_enabled and dl.app else max(1, safe_int(dl.segments, 1, 1, 32))
                worker_cap = min(max_streams, range_count)
                initial_workers = min(worker_cap, max(2, min(safe_int(dl.segments, 1, 1, 32), 4))) if adaptive_enabled else worker_cap
                controller = AdaptiveStreamController(initial_workers, worker_cap, adaptive_enabled)

                active_chunks = {}
                chunk_winners = {}
                primary_cancel_events = {i: threading.Event() for i in range(range_count)}
                race_cancel_events = {i: [] for i in range(range_count)}
                # Store open responses so a selected winner can close the losing HTTP
                # response and prevent a stale worker from continuing to write to disk.
                chunk_response_handles = {i: [] for i in range(range_count)}
                raced_chunks = set()
                race_threads = []
                race_state_lock = threading.RLock()
                race_stop = threading.Event()
                active_races = {"count": 0, "total": 0}
                endgame_enabled = bool(dl.app and dl.app.config.get("endgame_enabled", True))
                endgame_trigger = max(1.5, float(safe_int(
                    dl.app.config.get("endgame_trigger_seconds", 4), 4, 2, 30))) if dl.app else 4.0
                max_races_total = min(4, max(1, worker_cap // 4)) if endgame_enabled else 0

                def range_progress(index, byte_count):
                    now = time.monotonic()
                    with race_state_lock:
                        item = active_chunks.get(index)
                        if item is not None:
                            item["last_progress"] = now
                            item["bytes"] += max(0, int(byte_count))

                def publish_winner(index, path):
                    responses_to_close = []
                    with race_state_lock:
                        current = chunk_winners.get(index)
                        if current is not None:
                            return current == path
                        chunk_winners[index] = path
                        if path != segment_files[index]:
                            primary_cancel_events[index].set()
                        with dl.lock:
                            if index < len(dl.chunk_progress):
                                dl.chunk_progress[index] = 1.0
                        for event in race_cancel_events[index]:
                            event.set()
                        responses_to_close = [resp for owner_path, resp in chunk_response_handles[index]
                                              if owner_path != path and resp is not None]
                    for response in responses_to_close:
                        try:
                            response.close()
                        except Exception:
                            pass
                    return True

                def register_response(index, owner_path, response):
                    with race_state_lock:
                        chunk_response_handles[index].append((owner_path, response))
                        winner = chunk_winners.get(index)
                    # A response can open just after a winner has already been chosen.
                    if winner is not None and winner != owner_path:
                        try:
                            response.close()
                        except Exception:
                            pass

                def race_slow_chunk(index, start, end):
                    race_path = f"{segment_files[index]}.race{threading.get_ident()}"
                    race_cancel = threading.Event()
                    with race_state_lock:
                        race_cancel_events[index].append(race_cancel)
                        active_races["count"] += 1
                        active_races["total"] += 1
                    local_errors = []
                    try:
                        ok = _download_segment(
                            dl, dl.url, race_path, start, end, update_q,
                            local_errors, threading.Lock(), max_segment_retries=0,
                            chunk_index=None, chunk_cancel_event=race_cancel,
                            count_progress=False, activity_callback=None,
                            response_callback=lambda resp, idx=index, path=race_path: register_response(idx, path, resp))
                        changed = [str(err) for err in local_errors if str(err).startswith("RESOURCE_CHANGED:")]
                        if changed:
                            resource_changed_event.set()
                            with thread_errors_lock:
                                thread_errors.extend((index, err) for err in changed)
                            primary_cancel_events[index].set()
                            with race_state_lock:
                                for event in race_cancel_events[index]:
                                    event.set()
                        if ok:
                            publish_winner(index, race_path)
                        elif os.path.exists(race_path):
                            try: os.remove(race_path)
                            except OSError: pass
                    finally:
                        with race_state_lock:
                            active_races["count"] = max(0, active_races["count"] - 1)

                def endgame_monitor():
                    while not race_stop.wait(0.35):
                        if dl.stop_event and dl.stop_event.is_set():
                            return
                        if dl.pause_event is not None and not dl.pause_event.is_set():
                            continue
                        app_config = getattr(getattr(dl, "app", None), "config", {})
                        try:
                            intentionally_limited = (safe_int(app_config.get("speed_limit", 0), 0) > 0
                                or bool(app_config.get("slow_mode_enabled", False))
                                or (getattr(dl, "speed_limit_override", None) is not None
                                    and safe_int(getattr(dl, "speed_limit_override", 0), 0) > 0))
                        except Exception:
                            intentionally_limited = False
                        if intentionally_limited:
                            continue
                        if not work_queue.empty():
                            continue
                        with race_state_lock:
                            if (not active_chunks and controller.active_workers == 0
                                    and active_races["count"] == 0):
                                return
                            if active_races["count"] >= max(1, min(2, worker_cap // 4 or 1)):
                                continue
                            if active_races["total"] >= max_races_total:
                                continue
                            now = time.monotonic()
                            candidates = [(idx, state) for idx, state in active_chunks.items()
                                          if idx not in raced_chunks
                                          and idx not in chunk_winners
                                          and now - state.get("last_progress", now) >= endgame_trigger]
                            if not candidates:
                                continue
                            idx, state = max(candidates, key=lambda pair: now - pair[1].get("last_progress", now))
                            start, end = state["start"], state["end"]
                            raced_chunks.add(idx)
                            thread = threading.Thread(target=race_slow_chunk, args=(idx, start, end),
                                                      name=f"CatsSoft-Endgame-{idx}", daemon=True)
                            race_threads.append(thread)
                        thread.start()

                def range_worker():
                    while not (dl.stop_event and dl.stop_event.is_set()):
                        if not controller.acquire(dl.stop_event, dl.pause_event):
                            return
                        task = None
                        try:
                            try:
                                task = work_queue.get_nowait()
                            except queue.Empty:
                                return
                            i, start, end = task
                            with race_state_lock:
                                active_chunks[i] = {"started": time.monotonic(),
                                                    "last_progress": time.monotonic(),
                                                    "bytes": 0, "start": start, "end": end}
                            local_errors = []
                            started = time.monotonic()
                            ok = _download_segment(
                                dl, dl.url, segment_files[i], start, end, update_q,
                                local_errors, threading.Lock(), chunk_index=i,
                                chunk_cancel_event=primary_cancel_events[i],
                                count_progress=True, activity_callback=range_progress,
                                response_callback=lambda resp, idx=i, path=segment_files[i]: register_response(idx, path, resp))
                            elapsed = max(0.001, time.monotonic() - started)
                            cancelled_by_winner = primary_cancel_events[i].is_set() and not ok
                            controller.observe(ok, elapsed, end - start + 1,
                                               cancelled=cancelled_by_winner or bool(dl.stop_event and dl.stop_event.is_set()))
                            if ok:
                                publish_winner(i, segment_files[i])
                            elif not (dl.stop_event and dl.stop_event.is_set()):
                                changed = [str(err) for err in local_errors if str(err).startswith("RESOURCE_CHANGED:")]
                                if changed:
                                    resource_changed_event.set()
                                    with thread_errors_lock:
                                        thread_errors.extend((i, err) for err in changed)
                                elif not cancelled_by_winner:
                                    with race_state_lock:
                                        race_has_winner = i in chunk_winners
                                    if not race_has_winner:
                                        with thread_errors_lock:
                                            thread_errors.extend((i, str(err)) for err in (local_errors or [f"Segment {start}-{end} failed"]))
                        finally:
                            if task is not None:
                                with race_state_lock:
                                    active_chunks.pop(task[0], None)
                                work_queue.task_done()
                            controller.release()

                monitor_thread = None
                if endgame_enabled and max_races_total > 0 and total >= 16 * mib and worker_cap >= 2:
                    monitor_thread = threading.Thread(target=endgame_monitor,
                                                      name="CatsSoft-Endgame-Monitor", daemon=True)
                    monitor_thread.start()

                threads = [threading.Thread(target=range_worker, name=f"CatsSoft-Range-{i}", daemon=True)
                           for i in range(worker_cap)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                # Give any endgame racers time to finish/cancel before choosing winners.
                if monitor_thread is not None:
                    deadline = time.monotonic() + max(3.0, endgame_trigger + 2.0)
                    while time.monotonic() < deadline:
                        with race_state_lock:
                            races_left = active_races["count"]
                            pending_work = not work_queue.empty()
                            running_primary = controller.active_workers
                        if races_left == 0 and not pending_work and running_primary == 0:
                            break
                        time.sleep(0.1)
                    race_stop.set()
                    monitor_thread.join(timeout=1.0)
                with race_state_lock:
                    local_races = list(race_threads)
                for thread in local_races:
                    thread.join(timeout=max(2.0, endgame_trigger + 1.0))
                alive_races = [thread for thread in local_races if thread.is_alive()]
                if alive_races:
                    # Fail closed: never merge while a duplicate racer could still write.
                    with race_state_lock:
                        for events in race_cancel_events.values():
                            for event in events:
                                event.set()
                        open_responses = [resp for items in chunk_response_handles.values()
                                          for _owner_path, resp in items if resp is not None]
                    for response in open_responses:
                        try: response.close()
                        except Exception: pass
                    for thread in alive_races:
                        thread.join(timeout=3.0)
                    if any(thread.is_alive() for thread in local_races):
                        raise Exception("Endgame worker nu s-a oprit la timp; merge-ul a fost anulat pentru a proteja integritatea fișierului")

                if dl.stop_event and dl.stop_event.is_set():
                    update_q.put(("status", dl, DLStatus.CANCELLED))
                    return

                # A resource validator mismatch invalidates all persisted ranges before retry.
                if resource_changed_event.is_set() or any(
                        str(item[1] if isinstance(item, tuple) and len(item) > 1 else item).startswith("RESOURCE_CHANGED:")
                        for item in thread_errors):
                    _remove_segment_parts(dl.save_path)
                    _remove_download_meta(dl.save_path)
                    dl.resume_validator = ""
                    with dl.lock:
                        dl.downloaded = 0
                        dl.progress = 0.0
                    raise DownloadResourceChangedError("Serverul a schimbat resursa; segmentele vechi au fost eliminate")

                # Ignore failures from a primary stream if its endgame duplicate won.
                with thread_errors_lock:
                    effective_errors = []
                    for item in thread_errors:
                        if isinstance(item, tuple) and len(item) == 2:
                            idx, message = item
                            if isinstance(idx, int) and idx in chunk_winners:
                                continue
                            effective_errors.append(str(message))
                        else:
                            effective_errors.append(str(item))
                if effective_errors:
                    raise Exception(f"Segment download failed: {effective_errors[0]}")

                chosen_segments = []
                for index, original_path in enumerate(segment_files):
                    chosen = chunk_winners.get(index, original_path)
                    if not os.path.isfile(chosen):
                        raise IOError(f"Segmentul {index + 1} lipsește după transfer")
                    chosen_segments.append(chosen)
                    # Remove losing copies only after all primary/race writers are done.
                    if original_path != chosen and os.path.exists(original_path):
                        try: os.remove(original_path)
                        except OSError: pass
                    for race_path in Path(os.path.dirname(original_path) or ".").glob(os.path.basename(original_path) + ".race*"):
                        if str(race_path) != chosen:
                            try: race_path.unlink()
                            except OSError: pass

                # Endgame racers intentionally do not increment logical progress while
                # downloading duplicate bytes. Reconcile from the winning on-disk ranges.
                # This also removes any partial-byte accounting from a cancelled primary.
                actual_segment_bytes = 0
                for chosen in chosen_segments:
                    actual_segment_bytes += os.path.getsize(chosen)
                with dl.lock:
                    dl.downloaded = actual_segment_bytes
                    dl.progress = min(99.9, actual_segment_bytes / total * 100.0) if total > 0 else 0.0
                    if dl.chunk_progress:
                        dl.chunk_progress = [1.0 for _ in range(len(segment_files))]
                update_q.put(("progress", dl, None))

                _merge_segments(dl.save_path, chosen_segments, expected_total=total)

                # Validate the final file before setting progress to 100%.
                if os.path.exists(dl.save_path):
                    actual_size = os.path.getsize(dl.save_path)
                    if total > 0 and actual_size != total:
                        logger.error("Segment mismatch: expected %s, got %s", total, actual_size)
                        # Atomic merge retains source ranges if it fails; do not trust a bad final file.
                        try: os.remove(dl.save_path)
                        except OSError: pass
                        raise Exception(f"Incomplete download: {actual_size}/{total} bytes")
                    with dl.lock:
                        dl.downloaded = actual_size
                else:
                    raise Exception("Merged file not found")
            else:
                logger.info("Fișierul final există deja complet (%s bytes) - sar peste re-descărcare", total)
                with dl.lock:
                    dl.downloaded = total
                    dl.chunk_size_bytes = total
                    dl.chunk_progress = [1.0]

            # Verificare checksum
            if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
                dl.error_msg = "Checksum verification failed"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FEATURE NOU: scanare antivirus opțională (Windows Defender)
            if dl.app and dl.app.config.get("av_scan", False):
                scan = scan_with_defender(dl.save_path)
                if scan["ran"] and not scan["clean"]:
                    logger.warning(f"Scanare Defender a semnalat ceva la {dl.filename}: {scan['output'][:200]}")
                    update_q.put(("scan_warning", dl, scan))
                if not os.path.exists(dl.save_path):
                    logger.warning(f"{dl.filename} nu mai există după scanare (posibil carantinat)")
                    dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

            # Auto-sort
            metadata_source_path = dl.save_path
            if dl.status != DLStatus.CANCELLED and dl.app:
                filetype = guess_filetype(dl.url)
                new_path = auto_sort_file(dl.save_path, filetype, dl.app.config.get("auto_sort", True))
                dl.save_path = new_path
                update_q.put(("refresh", dl, None))

            # FIX: Setează progress = 100% DOAR DUPĂ toate verificările
            with dl.lock:
                dl.progress = 100.0
                dl.speed = 0.0
                dl.completed_date = datetime.now()

            _remove_download_meta(dl.save_path)
            if metadata_source_path != dl.save_path:
                _remove_download_meta(metadata_source_path)
            update_q.put(("status", dl, DLStatus.COMPLETED))  # Status final după verificări
            return

        except Exception as e:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            logger.error(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt == max_retries - 1:
                dl.error_msg = str(e)
                update_q.put(("status", dl, DLStatus.ERROR))
            else:
                with dl.lock:
                    dl.retry_count += 1
                    dl.speed = 0.0
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                # Backoff conștient de anulare: utilizatorul nu trebuie să aștepte
                # până la 4 secunde ca butonul Stop să producă un efect vizibil.
                deadline = time.monotonic() + (2 ** attempt)
                while time.monotonic() < deadline:
                    if dl.stop_event and dl.stop_event.is_set():
                        update_q.put(("status", dl, DLStatus.CANCELLED))
                        return
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

def _complete_existing_file(dl: Download, update_q: queue.Queue) -> bool:
    """Finalize a verified-size file recovered from disk without requesting bytes=total- (HTTP 416)."""
    try:
        # Doar un fișier DOVEDIT a fiind resursa curentă poate fi raportat
        # complet fără transfer. Altfel, un fișier orfan de aceeași dimensiune
        # ar fi declarat descărcat, iar conținutul lui nu are nimic de-a face
        # cu ceea ce s-a cerut.
        if not getattr(dl, "final_verified", False):
            return False
        if not dl.total or not os.path.isfile(dl.save_path):
            return False
        actual_size = os.path.getsize(dl.save_path)
        if actual_size != int(dl.total):
            return False
    except OSError:
        return False

    if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
        dl.error_msg = "Checksum verification failed"
        update_q.put(("status", dl, DLStatus.ERROR))
        return True

    if dl.app and dl.app.config.get("av_scan", False):
        scan = scan_with_defender(dl.save_path)
        if scan["ran"] and not scan["clean"]:
            logger.warning("Scanare Defender a semnalat ceva la %s: %s", dl.filename, scan["output"][:200])
            update_q.put(("scan_warning", dl, scan))
        if not os.path.exists(dl.save_path):
            dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
            update_q.put(("status", dl, DLStatus.ERROR))
            return True

    metadata_source_path = dl.save_path
    if dl.app:
        new_path = auto_sort_file(dl.save_path, guess_filetype(dl.url), dl.app.config.get("auto_sort", True))
        dl.save_path = new_path
        if new_path != metadata_source_path:
            update_q.put(("refresh", dl, None))

    with dl.lock:
        dl.downloaded = actual_size
        dl.progress = 100.0
        dl.speed = 0.0
        dl.completed_date = datetime.now()
        if dl.chunk_progress:
            dl.chunk_progress = [1.0 for _ in dl.chunk_progress]
        else:
            dl.chunk_progress = [1.0]

    _remove_download_meta(dl.save_path)
    if metadata_source_path != dl.save_path:
        _remove_download_meta(metadata_source_path)
    update_q.put(("progress", dl, None))
    update_q.put(("status", dl, DLStatus.COMPLETED))
    logger.info("Fișier complet recuperat de pe disc: %s", dl.filename)
    return True


def download_simple(dl: Download, update_q: queue.Queue, max_retries: int = 3):
    """Download simplu cu reluare sigură și rutare opțională printr-un adaptor selectat."""
    manager = getattr(getattr(dl, "app", None), "network_manager", None)
    # True după ce am scris singuri octeți în fișier: de atunci conținutul de pe
    # disc ne aparține și poate fi reluat în siguranță.
    wrote_own_data = False
    for attempt in range(max_retries):
        network_key, http_session = manager.acquire_session() if manager is not None else ("system", SESSION)
        try:
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            if attempt == 0 and _complete_existing_file(dl, update_q):
                return
            if attempt > 0:
                if (dl.supports_resume and os.path.isfile(dl.save_path)
                        and (wrote_own_data or not dl.resume_blocked)):
                    # Continue from the bytes on disk after a transient network failure.
                    try:
                        actual_partial = os.path.getsize(dl.save_path)
                    except OSError:
                        actual_partial = 0
                    with dl.lock:
                        dl.downloaded = actual_partial
                        dl.progress = (min(99.9, actual_partial / dl.total * 100.0)
                                       if dl.total > 0 else 0.0)
                else:
                    with dl.lock:
                        dl.downloaded = 0
                        dl.progress = 0.0
                    if os.path.exists(dl.save_path):
                        try: os.remove(dl.save_path)
                        except OSError: pass
                with dl.speed_tracker.lock:
                    dl.speed_tracker.samples.clear()
                with dl.lock:
                    dl.speed = 0.0
            else:
                # CONTORUL POATE FI VECHII: valoarea vine din baza de date (coadă
                # restaurată), iar fișierul poate fi fost trunchiat, înlocuit sau
                # șters între timp. Offset-ul Range trebuie să fie întotdeauna
                # numărul REAL de octeți de pe disc: altfel serverul umple
                # intervalul greșit, iar verificarea finală de dimensiune trece
                # oricum - fișier raportat COMPLET, cu primii octeți lipsă și
                # ultimii duplicați.
                on_disk = _reconcile_downloaded_with_disk(dl)
                if on_disk > 0:
                    logger.info(f"Reluare download simplu de la {on_disk} bytes (fișier existent)")

            requested_resume = dl.downloaded > 0 and dl.supports_resume
            # Accept-Encoding: identity - cerem octeții brute. Altfel, un server
            # care comprimă răspunsul trimite Content-Length pentru forma
            # COMPRIMATĂ, iar fișierul scris (decodat) nu mai corespunde: verificarea
            # finală de dimensiune eșuează sau, mai rău, reluarea concatenează
            # octeți comprimați peste octeți decomprimați.
            headers = {"Accept-Encoding": "identity"}
            if requested_resume:
                headers["Range"] = f"bytes={dl.downloaded}-"
                if getattr(dl, "resume_validator", ""):
                    headers["If-Range"] = dl.resume_validator

            with http_session.get(dl.url, stream=True, timeout=(8, 25), headers=headers) as resp:
                # Răspunsul activ e păstrat ca să poată fi închis imediat la
                # Stop: altfel anularea aștepta până la timeout-ul de citire
                # (25 s) înainte să aibă efect vizibil.
                dl.active_response = resp
                resp.raise_for_status()

                # FIX: dacă am cerut reluare (Range) dar serverul a ignorat-o și a trimis
                # fișierul întreg (200 în loc de 206), NU putem continua în modul append -
                # ar rezulta un fișier corupt (bytes vechi + fișier întreg concatenate).
                if requested_resume and resp.status_code != 206:
                    logger.warning("Serverul nu a onorat cererea de reluare (Range) - repornesc de la 0")
                    with dl.lock:
                        dl.downloaded = 0
                elif requested_resume:
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                    if not match or int(match.group(1)) != dl.downloaded:
                        raise requests.RequestException(
                            f"Serverul a răspuns cu Content-Range invalid la reluare: {content_range or 'lipsește'}")

                final_url = resp.url
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                # A server may ignore If-Range and return a fresh full response; status handling above
                # resets downloaded=0 so the file is overwritten, never concatenated.
                new_validator = str(resp.headers.get("ETag") or resp.headers.get("Last-Modified") or "").strip()
                if (requested_resume and resp.status_code == 206 and dl.resume_validator
                        and new_validator and new_validator != dl.resume_validator):
                    raise DownloadResourceChangedError("Resursa s-a schimbat; reluarea vechiului fișier este nesigură")
                if new_validator:
                    dl.resume_validator = new_validator
                if final_url != dl.url:
                    logger.info(f"Redirect detected: {dl.url} -> {final_url}")

                # FIX: consolidat pe filename_from_url (elimina regex duplicat + funcție redundantă)
                suggested_name = filename_from_url(dl.url, response_headers, final_url)
                if (suggested_name and suggested_name != dl.filename and not dl.filename_locked
                        and not _has_resume_artifacts(dl.save_path)):
                    logger.info(f"Updating filename: {dl.filename} -> {suggested_name}")
                    proposed_path = os.path.join(os.path.dirname(dl.save_path), sanitize_filename(suggested_name))
                    if os.path.abspath(proposed_path) != os.path.abspath(dl.save_path):
                        old_save_path = dl.save_path
                        if os.path.exists(proposed_path):
                            proposed_path = unique_target_path(proposed_path)
                        dl.save_path = proposed_path
                        dl.filename = os.path.basename(proposed_path)
                        _remove_download_meta(old_save_path)
                        if dl.app:
                            update_q.put(("refresh", dl, None))

                response_length = int(resp.headers.get("content-length", 0))
                total = response_length + dl.downloaded
                if resp.status_code == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                    if match and match.group(3) != "*":
                        total = int(match.group(3))
                # Un răspuns comprimat nu poate fi dimensionat prin Content-Length:
                # lungimea anunțată este cea a formei comprimate, nu a octeților
                # scriși efectiv în fișier.
                content_encoding = str(resp.headers.get("Content-Encoding", "") or "").strip().lower()
                if content_encoding and content_encoding != "identity":
                    total = 0
                    logger.warning("Serverul a trimis %s; dimensiunea finală este necunoscută",
                                   content_encoding)

                _write_download_meta(dl.save_path, {
                    "url": dl.url,
                    "total": int(total or 0),
                    "etag": str(resp.headers.get("ETag") or ""),
                    "last_modified": str(resp.headers.get("Last-Modified") or ""),
                    "updated_at": datetime.now().isoformat(),
                })
                dl.total = total
                with dl.lock:
                    dl.chunk_size_bytes = 0
                    dl.chunk_progress = [min(1.0, dl.downloaded / total) if total else 0.0]

                # FIX: Dacă total = 0, nu putem calcula progress corect
                if total == 0:
                    logger.warning("Content-Length is 0, progress will show downloaded bytes only")

                # FEATURE NOU: verificare spațiu disc (doar partea rămasă de descărcat)
                remaining = max(0, total - dl.downloaded)
                if remaining > 0 and not check_disk_space(dl.save_path, remaining):
                    dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

                os.makedirs(os.path.dirname(dl.save_path) or ".", exist_ok=True)
                mode = "ab" if dl.downloaded > 0 else "wb"

                resp.raw.decode_content = True
                chunk_size = 32768
                last_update = time.time()
                update_interval = 0.5

                with open(dl.save_path, mode) as f:
                    wrote_own_data = True
                    last_space_check = time.time()
                    while True:
                        if dl.stop_event and dl.stop_event.is_set():
                            update_q.put(("status", dl, DLStatus.CANCELLED))
                            return
                        if dl.pause_event:
                            # Așteptare în trepte: permite oprirea imediată în loc
                            # să blocheze firul pe un wait() fără timeout.
                            while not dl.pause_event.wait(timeout=0.25):
                                if dl.stop_event and dl.stop_event.is_set():
                                    update_q.put(("status", dl, DLStatus.CANCELLED))
                                    return
                        if dl.stop_event and dl.stop_event.is_set():
                            return
                        chunk = resp.raw.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)

                        with dl.lock:
                            dl.downloaded += len(chunk)
                            downloaded_now = dl.downloaded
                            if dl.chunk_progress:
                                dl.chunk_progress[0] = min(1.0, downloaded_now / total) if total else 0.0
                            now = time.time()
                            should_update = (now - last_update >= update_interval)

                        # Dimensiunea necunoscută (răspuns chunked) nu permite o
                        # verificare de spațiu înainte de transfer: o facem periodic
                        # în timpul descărcării, ca un fișier uriaș să nu umple
                        # discul până la capăt.
                        if total <= 0 and now - last_space_check >= 15:
                            last_space_check = now
                            try:
                                free = shutil.disk_usage(os.path.dirname(dl.save_path) or ".").free
                            except OSError:
                                free = None
                            if free is not None and free < 50 * 1024 * 1024:
                                dl.error_msg = "Spațiu insuficient pe disc pentru acest fișier"
                                update_q.put(("status", dl, DLStatus.ERROR))
                                return

                        _record_session_transfer(dl, len(chunk))
                        if manager is not None:
                            manager.report_bytes(network_key, len(chunk))
                        if should_update:
                            dl.speed_tracker.update(downloaded_now)
                            speed_now = dl.speed_tracker.get_speed()

                            with dl.lock:
                                dl.speed = speed_now
                                if total > 0:
                                    dl.progress = min(99.9, (downloaded_now / total) * 100.0)
                                else:
                                    dl.progress = 0.0
                                chunk_size = adaptive_chunk_size(speed_now)

                            update_q.put(("progress", dl, None))
                            last_update = now

                        _apply_speed_limit(dl, len(chunk), network_key)

                        # Notă: blocul de raportare de mai sus (should_update) este
                        # singurul responsabil de actualizare; un al doilea bloc
                        # identic exista aici și dubla evenimentele "progress".


            # FIX: Verifică dimensiunea reală a fișierului
            actual_size = os.path.getsize(dl.save_path)
            if total > 0 and actual_size != total:
                raise IOError(f"Descărcare incompletă: {actual_size}/{total} bytes")
            with dl.lock:
                dl.downloaded = actual_size
                if total <= 0:
                    dl.total = actual_size  # now known after clean EOF
                dl.progress = 100.0
                if dl.chunk_progress:
                    dl.chunk_progress[0] = 1.0
                dl.speed = 0.0

            update_q.put(("progress", dl, None))  # Final progress update

            if dl.checksum and not verify_checksum(dl.save_path, dl.checksum, dl.checksum_algo):
                dl.error_msg = "Checksum verification failed"
                update_q.put(("status", dl, DLStatus.ERROR))
                return

            # FEATURE NOU: scanare antivirus opțională (Windows Defender)
            if dl.app and dl.app.config.get("av_scan", False):
                scan = scan_with_defender(dl.save_path)
                if scan["ran"] and not scan["clean"]:
                    logger.warning(f"Scanare Defender a semnalat ceva la {dl.filename}: {scan['output'][:200]}")
                    update_q.put(("scan_warning", dl, scan))
                if not os.path.exists(dl.save_path):
                    logger.warning(f"{dl.filename} nu mai există după scanare (posibil carantinat)")
                    dl.error_msg = "Fișierul a fost carantinat de Windows Defender după scanare"
                    update_q.put(("status", dl, DLStatus.ERROR))
                    return

            metadata_source_path = dl.save_path
            if dl.app:
                filetype = guess_filetype(dl.url)
                new_path = auto_sort_file(dl.save_path, filetype, dl.app.config.get("auto_sort", True))
                dl.save_path = new_path
                update_q.put(("refresh", dl, None))

            dl.completed_date = datetime.now()
            _remove_download_meta(dl.save_path)
            if metadata_source_path != dl.save_path:
                _remove_download_meta(metadata_source_path)
            update_q.put(("status", dl, DLStatus.COMPLETED))
            return

        except Exception as e:
            if manager is not None and isinstance(e, (requests.ConnectionError, requests.Timeout, OSError, TimeoutError)):
                manager.report_failure(network_key, e)
            if isinstance(e, DownloadResourceChangedError):
                try:
                    if os.path.isfile(dl.save_path): os.remove(dl.save_path)
                except OSError:
                    pass
                _remove_download_meta(dl.save_path)
                dl.downloaded = 0
                dl.resume_validator = ""
            if dl.stop_event and dl.stop_event.is_set():
                update_q.put(("status", dl, DLStatus.CANCELLED))
                return
            logger.error(f"Download attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt == max_retries - 1:
                dl.error_msg = str(e)
                update_q.put(("status", dl, DLStatus.ERROR))
            else:
                with dl.lock:
                    dl.retry_count += 1
                time.sleep(2 ** attempt)
        finally:
            if manager is not None:
                manager.release_session(network_key)

def probe_range(url):
    """Probează Range și, dacă serverul răspunde 206, extrage dimensiunea totală."""
    try:
        with SESSION.get(url, headers={"Range": "bytes=0-0"}, timeout=8, stream=True) as response:
            if response.status_code != 206:
                return False, 0, dict(response.headers), response.url
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+0-0/(\d+|\*)", content_range, re.IGNORECASE)
            total = int(match.group(1)) if match and match.group(1) != "*" else 0
            return bool(match), total, dict(response.headers), response.url
    except Exception as exc:
        logger.debug("Range probe failed for %s: %s", url, exc)
        return False, 0, {}, url


def supports_range(url):
    supported, _total, _headers, _final_url = probe_range(url)
    return supported

def _download_segment(dl, url, segment_path, start, end, update_q, errors_list, errors_lock,
                       max_segment_retries: int = 2,
                       chunk_index: Optional[int] = None,
                       chunk_cancel_event: Optional[threading.Event] = None,
                       count_progress: bool = True,
                       activity_callback: Optional[Callable] = None,
                       response_callback: Optional[Callable] = None):
    """Download one HTTP byte range, with safe resume, per-network routing and race cancellation.

    ``count_progress=False`` is used only by endgame racers: duplicate bytes still count
    toward network/session usage, but never inflate the file's logical progress.
    """
    manager = getattr(getattr(dl, "app", None), "network_manager", None)
    for attempt in range(max_segment_retries + 1):
        segment_bytes_written = 0
        resume_from = 0
        resume_counted = False
        network_key, http_session = manager.acquire_session() if manager is not None else ("system", SESSION)
        attempt_started = time.monotonic()
        tracked_response = None
        try:
            if dl.stop_event and dl.stop_event.is_set():
                return False
            if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                return False

            # Reluarea funcționează la fiecare încercare, nu doar la prima: altfel o
            # eroare de rețea tranzitorie relua segmentul de la zero și arunca munca
            # deja validată pe disc.
            if os.path.exists(segment_path):
                try:
                    existing_size = os.path.getsize(segment_path)
                except OSError:
                    existing_size = 0
                expected_size = end - start + 1
                if existing_size == expected_size:
                    if count_progress:
                        with dl.lock:
                            dl.downloaded += existing_size
                            if chunk_index is not None:
                                while len(dl.chunk_progress) <= chunk_index:
                                    dl.chunk_progress.append(0.0)
                                dl.chunk_progress[chunk_index] = 1.0
                        # Octeți găsiți pe disc, nu transferați acum: re-basăm
                        # trackerul, altfel saltul ar apărea ca viteză de transfer.
                        dl.speed_tracker.rebase(dl.downloaded)
                    return True
                if 0 < existing_size < expected_size:
                    resume_from = existing_size
                elif existing_size > expected_size:
                    # Segment supradimensionat (ex. împărțire anterioară diferită):
                    # reluăm curat, altfel am concatena octeți din alt interval.
                    try:
                        os.remove(segment_path)
                    except OSError:
                        pass

            if count_progress and chunk_index is not None:
                with dl.lock:
                    while len(dl.chunk_progress) <= chunk_index:
                        dl.chunk_progress.append(0.0)
                    dl.chunk_progress[chunk_index] = resume_from / max(1, end - start + 1)

            range_start = start + resume_from
            headers = {"Range": f"bytes={range_start}-{end}",
                       # Octeți brute: un răspuns comprimat ar decodifica în
                       # segment un conținut care nu corespunde intervalului cerut.
                       "Accept-Encoding": "identity"}
            # If-Range se trimite DOAR când reutilizăm efectiv octeți de pe disc.
            # Trimis și la o cerere de la zero, un server care răspunde 200 în loc de
            # 206 (ETag diferit între HEAD și GET) ar face aplicația să declare
            # "resursa s-a schimbat" și să șteargă toate segmentele valide.
            if resume_from > 0 and getattr(dl, "resume_validator", ""):
                headers["If-Range"] = dl.resume_validator
            file_mode = "ab" if resume_from > 0 else "wb"

            # Keep a bounded read timeout so cancellation/endgame races do not leave
            # worker threads writing to staging files long after a winner was selected.
            read_timeout = 7 if not count_progress else 15
            with http_session.get(url, stream=True, headers=headers, timeout=(8, read_timeout)) as resp:
                # Răspunsul e înregistrat ca să poată fi închis imediat la Stop.
                # Fără asta, o citire blocată așteaptă timeout-ul de citire (15 s)
                # înainte ca anularea să aibă efect în calea segmentată.
                _track_active_response(dl, resp)
                tracked_response = resp
                if response_callback:
                    try:
                        response_callback(resp)
                    except Exception:
                        pass
                if resp.status_code != 206:
                    if resp.status_code == 200 and headers.get("If-Range"):
                        # Reluam octeți existenți, dar serverul a răspuns cu 200:
                        # validatorul nu se potrivește, deci resursa s-a schimbat.
                        raise DownloadResourceChangedError(
                            "Range/If-Range returned HTTP 200; the server resource may have changed")
                    # Fără If-Range un 200 înseamnă doar că serverul nu onorează
                    # Range - NU că resursa s-a schimbat. Nu ștergem segmentele valide.
                    raise requests.RequestException(
                        f"Serverul nu a acceptat cererea Range (HTTP {resp.status_code})")

                content_range = str(resp.headers.get("Content-Range", ""))
                match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                if not match or int(match.group(1)) != range_start or int(match.group(2)) != end:
                    raise requests.RequestException(f"Răspuns Content-Range invalid: {content_range or 'lipsește'}")

                response_validator = str(resp.headers.get("ETag") or resp.headers.get("Last-Modified") or "").strip()
                if (headers.get("If-Range") and response_validator
                        and response_validator != headers["If-Range"]):
                    raise DownloadResourceChangedError("Validatorul HTTP s-a schimbat în timpul reluării")

                resp.raw.decode_content = False
                # Dimensiunea blocului se adaptează la viteza reală: cu până la 16
                # segmente paralele, blocurile mici înseamnă milioane de apeluri de
                # sistem și de blocări pe dl.lock.
                chunk_size = max(64 * 1024, adaptive_chunk_size(dl.speed))
                bytes_since_retune = 0
                last_update = time.monotonic()
                update_interval = 0.35
                if count_progress and resume_from > 0:
                    with dl.lock:
                        dl.downloaded += resume_from
                    resume_counted = True
                    # Octeți reluați de pe disc: nu sunt viteză de transfer.
                    dl.speed_tracker.rebase(dl.downloaded)

                with open(segment_path, file_mode) as handle:
                    while True:
                        if dl.stop_event and dl.stop_event.is_set():
                            if count_progress:
                                with dl.lock:
                                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                            return False
                        if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                            if count_progress:
                                with dl.lock:
                                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                            return False
                        if dl.pause_event:
                            while not dl.pause_event.wait(timeout=0.25):
                                if dl.stop_event and dl.stop_event.is_set():
                                    if count_progress:
                                        with dl.lock:
                                            dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                                    return False
                                if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                                    if count_progress:
                                        with dl.lock:
                                            dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
                                    return False

                        chunk = resp.raw.read(chunk_size)
                        if not chunk:
                            break
                        expected_size = end - range_start + 1
                        if segment_bytes_written + len(chunk) > expected_size:
                            raise requests.RequestException("Serverul a trimis mai mulți octeți decât intervalul solicitat")
                        handle.write(chunk)
                        segment_bytes_written += len(chunk)
                        bytes_since_retune += len(chunk)
                        if bytes_since_retune >= 4 * 1024 * 1024:
                            # Re-evaluează mărimea blocului o dată la 4 MiB.
                            bytes_since_retune = 0
                            chunk_size = max(64 * 1024, adaptive_chunk_size(dl.speed))
                        if manager is not None:
                            manager.report_bytes(network_key, len(chunk))
                        _record_session_transfer(dl, len(chunk))
                        if activity_callback and chunk_index is not None:
                            try: activity_callback(chunk_index, len(chunk))
                            except Exception: pass

                        with dl.lock:
                            if count_progress:
                                dl.downloaded += len(chunk)
                                downloaded_now = dl.downloaded
                                if chunk_index is not None:
                                    dl.chunk_progress[chunk_index] = min(
                                        1.0, (resume_from + segment_bytes_written) / max(1, end - start + 1))
                            else:
                                downloaded_now = dl.downloaded
                            now = time.monotonic()
                            should_update = (now - last_update >= update_interval)

                        if count_progress and should_update:
                            dl.speed_tracker.update(downloaded_now)
                            speed_now = dl.speed_tracker.get_speed()
                            with dl.lock:
                                dl.speed = speed_now
                                if dl.total > 0:
                                    dl.progress = min(99.9, downloaded_now / dl.total * 100.0)
                            update_q.put(("progress", dl, None))
                            last_update = now

                        _apply_speed_limit(dl, len(chunk), network_key)

            expected_size = end - start + 1
            if segment_bytes_written + resume_from != expected_size:
                raise requests.RequestException(
                    f"Segment incomplet: {segment_bytes_written + resume_from}/{expected_size} bytes")
            if count_progress and chunk_index is not None:
                with dl.lock:
                    dl.chunk_progress[chunk_index] = 1.0
            return True

        except DownloadResourceChangedError as exc:
            with errors_lock:
                errors_list.append(f"RESOURCE_CHANGED: {exc}")
            return False
        except Exception as exc:
            if count_progress and (segment_bytes_written or resume_counted):
                with dl.lock:
                    dl.downloaded = max(0, dl.downloaded - segment_bytes_written - (resume_from if resume_counted else 0))
            if manager is not None and isinstance(exc, (requests.ConnectionError, requests.Timeout, OSError, TimeoutError)):
                manager.report_failure(network_key, exc)
            if attempt < max_segment_retries and not (chunk_cancel_event and chunk_cancel_event.is_set()):
                with dl.lock:
                    dl.retry_count += 1
                    if count_progress and chunk_index is not None and chunk_index < len(dl.chunk_progress):
                        dl.chunk_progress[chunk_index] = 0.0
                logger.warning("Segment %s-%s retry %s/%s: %s", start, end, attempt + 1, max_segment_retries, exc)
                # Stop-aware backoff instead of an unconditional sleep.
                delay = 1.0 + attempt * 1.5
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if dl.stop_event and dl.stop_event.is_set():
                        return False
                    if chunk_cancel_event is not None and chunk_cancel_event.is_set():
                        return False
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                continue
            logger.error("Segment download error %s-%s: %s", start, end, exc)
            with errors_lock:
                errors_list.append(str(exc))
            return False
        finally:
            if tracked_response is not None:
                _untrack_active_response(dl, tracked_response)
            if manager is not None:
                manager.release_session(network_key)


def _merge_segments(output_path, segment_files, expected_total: Optional[int] = None):
    """Atomically assemble all ranges; never remove source segments before replace succeeds."""
    temp_path = output_path + ".catssoft-merge.tmp"
    try:
        missing = [path for path in segment_files if not os.path.isfile(path)]
        if missing:
            raise IOError(f"Lipsesc {len(missing)} segmente; fișierul final nu va fi asamblat")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        try:
            with open(temp_path, "wb") as outfile:
                for segment_path in segment_files:
                    with open(segment_path, "rb") as infile:
                        shutil.copyfileobj(infile, outfile, length=1024 * 1024)
                outfile.flush()
                os.fsync(outfile.fileno())
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOSPC:
                # Mesaj explicit: altfel utilizatorul vede „No space left on
                # device" și nu află de ce, în condițiile în care chiar a fost
                # nevoie de încă o copie a întregului fișier.
                raise IOError(
                    "Spațiu insuficient pe disc pentru asamblarea segmentelor "
                    "(era nevoie de încă %s pentru fișierul final). "
                    "Eliberează spațiu și reia descărcarea - segmentele descărcate "
                    "sunt păstrate." % fmt_size(int(expected_total or 0))) from exc
            raise
        # Dimensiunea se verifică ÎNAINTE de înlocuirea fișierului final și de
        # ștergerea segmentelor sursă. Altfel, o îmbinare incompletă distruge
        # singura copie a datelor, iar reluarea trebuie să reia transferul de la
        # zero - exact ce nu trebuie să se întâmple la o eroare tranzitorie.
        if expected_total:
            merged_size = os.path.getsize(temp_path)
            if merged_size != int(expected_total):
                raise IOError(f"Îmbinare incompletă: {merged_size}/{int(expected_total)} bytes")
        os.replace(temp_path, output_path)
        for segment_path in segment_files:
            try:
                os.remove(segment_path)
            except OSError:
                logger.warning("Merged segment could not be removed: %s", segment_path)
    except Exception as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        # Păstrăm tipul erorii (de exemplu „discul plin" cu mesajul clar),
        # ca să poată fi recunoscută mai sus.
        if isinstance(exc, OSError):
            raise
        raise Exception(f"Error merging segments atomically: {exc}")

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


from pathlib import Path


def app_dir() -> Path:
    """Folderul aplicaţiei (cel care conţine ``icon/``)."""
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "icon" / "icon.ico").is_file():
            return candidate
    # Fără pictogramă pe disc: în pachet urcăm un nivel (folderul aplicaţiei),
    # în fişierul unic rămânem unde suntem.
    return here.parent if here.name == "catssoft" else here


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

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = DownloadManager()
    app.mainloop()
