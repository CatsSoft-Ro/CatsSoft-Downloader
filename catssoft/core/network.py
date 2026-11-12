"""core/network — Sesiuni HTTP, proxy și managementul adaptoarelor."""

import threading
import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from urllib.parse import urlparse, parse_qs, unquote
import json

from catssoft.logging_setup import logger
from catssoft.utils.formatting import safe_int
# ── orig:325 ──

# ─── HTTP Session partajat (OPTIMIZARE: reutilizare conexiuni keep-alive) ──────
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# ── orig:328 ──

SESSION = requests.Session()
# ── orig:329 ──
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
# ── orig:330 ──
SESSION.mount("http://", _adapter)
# ── orig:331 ──
SESSION.mount("https://", _adapter)
# ── orig:332 ──
SESSION.headers.update({"User-Agent": USER_AGENT})
# ── orig:334 ──
# Keep Content-Length and streamed bytes comparable (avoid transparent gzip decoding).
SESSION.headers.update({"Accept-Encoding": "identity"})
# ── orig:338 ──


# ─── Proxy (HTTP / HTTPS / SOCKS5) ───────────────────────────────────────────
_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
# ── orig:341 ──


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
# ── orig:369 ──


def _socks_proxy_available() -> bool:
    """PySocks (``requests[socks]``) e necesar pentru proxy-uri SOCKS5."""
    try:
        import socks  # noqa: F401
        return True
    except Exception:
        return False
# ── orig:378 ──


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
# ── orig:409 ──


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
# ── orig:425 ──


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
