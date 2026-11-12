"""core/security — Scanare Windows Defender."""

import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from typing import Optional, Callable, List
# ── orig:3751 ──


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
# ── orig:3766 ──


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
# ── orig:3778 ──


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
