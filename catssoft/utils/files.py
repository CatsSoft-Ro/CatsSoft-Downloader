"""utils/files — Numele de fișier, CSV, sortare automată, spațiu pe disc."""

import requests
import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional, Callable, List
from pathlib import Path

from catssoft.core.network import SESSION
from catssoft.logging_setup import logger
# ── orig:1462 ──


# ─── Auto-Sort Folders ────────────────────────────────────────────────────────
AUTO_SORT_FOLDERS = {
    "video": "Downloads/Videos", "image": "Downloads/Images",
    "audio": "Downloads/Music", "archive": "Downloads/Archives",
    "document": "Downloads/Documents", "disk": "Downloads/ISOs", "file": "Downloads",
}
# ── orig:1718 ──

def guess_filetype(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path.lower())[1]
    if ext in {".mp4",".mkv",".avi",".mov",".webm",".flv"}: return "video"
    if ext in {".jpg",".jpeg",".png",".gif",".webp",".bmp",".svg"}: return "image"
    if ext in {".mp3",".flac",".aac",".wav",".ogg",".m4a"}: return "audio"
    if ext in {".zip",".tar",".gz",".bz2",".rar",".7z"}: return "archive"
    if ext in {".pdf",".docx",".xlsx",".pptx",".txt",".rtf",".odt"}: return "document"
    if ext in {".iso",".img",".dmg"}: return "disk"
    return "file"
# ── orig:1728 ──

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
# ── orig:1754 ──


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# ── orig:1757 ──


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
# ── orig:1776 ──


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
# ── orig:1790 ──


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
# ── orig:1830 ──

_BAD_EXTS = {".php", ".asp", ".aspx", ".cgi", ".js"}
# ── orig:1833 ──


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
# ── orig:1869 ──


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
# ── orig:1935 ──


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
# ── orig:1971 ──


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
# ── orig:2010 ──

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
# ── orig:2098 ──

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
# ── orig:2122 ──


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
# ── orig:2157 ──


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
# ── orig:2179 ──


def open_folder(path: str):
    folder = os.path.dirname(path) if os.path.isfile(path) else path
    if not os.path.exists(folder): return
    try:
        if sys.platform == "win32": os.startfile(folder)
        elif sys.platform == "darwin": subprocess.Popen(["open", folder])
        else: subprocess.Popen(["xdg-open", folder])
    except Exception: pass
# ── orig:2188 ──

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
# ── orig:2203 ──

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
# ── orig:2236 ──

ICONS = {"video": "▶", "image": "🖼", "audio": "♫", "archive": "🗜", "document": "📄", "disk": "💿", "file": "↓"}
# ── orig:2239 ──


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
