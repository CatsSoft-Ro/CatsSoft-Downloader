# Plan: împărțirea în module — CatsSoft Downloader v6.13

Document de lucru, **finalizat**: decizia utilizatorului a fost **„pachet + fișier
unic generat"**, iar împărțirea e implementată și verificată. Rezultatul e în
`REPORT_v6.13.md`, secțiunea 7.

Forma livrabilului:
- `catssoft/` — **sursa de adevăr** (21 module + 4 `__init__.py`);
- `CatsSoft_Downloader_v6.13_Plexo_Engine.py` — **artefact de build**, generat din
  pachet de `build_single_file.py`;
- `tools/verify_build.py` — verifică „build la zi" + echivalența AST.

## Starea de la început (înainte de împărțire)

Fișier: 9.896 linii, un singur modul. Repartizarea pe zone (măsurată cu `ast`, nu
estimată):

| Linii | Bloc | Linii | Bloc |
|---|---|---|---|
| 1–29 | imports | 3222–3388 | `SmartAutomationEngine` (167) |
| 35–303 | patch-uri tkinter/CTk la import | 3406–3458 | `_apply_speed_limit` |
| 305–424 | utilitare (filename, proxy) | 3502–3597 | validare reluare |
| 425–786 | `NetworkAdapterManager` (362) | 3778–3906 | Defender + `AdaptiveStreamController` |
| 864–1440 | sistem de teme (4 clase) | 3932–4470 | `download_segmented` (539) |
| 1527–1575 | `Download` | 4472–4527 | `_complete_existing_file` |
| 1869–2092 | helperi nume fișier | 4530–4827 | `download_simple` (298) |
| 2270–2837 | `DatabaseManager` (568) | 4848–5086 | `_download_segment` (239) |
| 2840–2907 | `ConfigManager` | 5089–5139 | `_merge_segments` |
| 2910–3052 | `ClipboardMonitor` | 5142–5369 | swipe (`SwipeGestureController`, `SwipeCardHost`) |
| 3055–3116 | `TrayManager` | 5372–5699 | `DownloadCard` (328) |
| 3119–3219 | `DownloadScheduler` | 5702–6139 | `DownloadDetailsView` (438) |
| | | 6142–6536 | `AddURLDialog` (395) |
| | | 6539–6631 | `ResumeQueueDialog` |
| | | **6634–9891** | **`DownloadManager` (3.258 — o treime din fișier)** |

Observația care contează: **hotspot-ul nu e fișierul, e clasa `DownloadManager`**
(3.258 linii, 33%). Împărțirea în module fără sub-împărțirea acestei clase mută
problema, nu o rezolvă.

## Harta modulelor propusă

```
CatsSoft Downloader PRO/
  main.py                    # punct de intrare (era __main__ din fișierul unic)
  catssoft/
    __init__.py
    paths.py                 # rezolvarea căii iconiței, valabilă în ambele forme
    core/
      __init__.py
      patches.py             # patch-uri tkinter/CTk la import        (35-303)
      models.py              # DLStatus, Download, ScheduledDownload   (1500-1600)
      database.py            # DatabaseManager                       (2270-2837)
      config.py              # ConfigManager                         (2840-2907)
      network.py             # NetworkAdapterManager + proxy          (425-786)
      automation.py          # ClipboardMonitor + TrayManager         (2910-3116)
      scheduler.py           # DownloadScheduler                     (3119-3219)
      smart.py               # SmartAutomationEngine                 (3222-3388)
      engine.py              # segmentat, simplu, merge, reluare  (3406-5139)
      security.py            # scan_with_defender                    (3778-3844)
    ui/
      __init__.py
      theme.py               # ThemePresets, ThemeManager, ...       (864-1440)
      cards.py               # swipe + DownloadCard                (5142-5699)
      details.py             # DownloadDetailsView                 (5702-6139)
      dialogs.py             # AddURLDialog + ResumeQueueDialog   (6142-6631)
      main_window.py         # DownloadManager                    (6634-9891)
    utils/
      __init__.py
      files.py               # helperi nume fișier, artefacte      (1660-2092)
      formatting.py          # formatare, ETA, dimensiuni
  build_single_file.py       # (doar la varianta B) pachet -> fișier .py
```

## De ce se poate face fără pierderi de comportament

1. **Blocurile sunt contigue.** Tăierea pe granițele de mai sus **păstrează ordinea
   definițiilor**, deci întregul fișier devine o succesiune de module importate în
   aceeași ordine. Semantica rămâne identică prin construcție, nu prin noroc.
2. **Nu există importuri circulare reale.** Graful de dependențe e un DAG:
   `models → database/config → scheduler/smart → engine → ui`. Singurele excepții
   (UI ↔ engine) se rezolvă prin import la nivel de modul, nu la nivel de funcție.
3. **Importurile devin explicite.** Fiecare modul își declară ce folosește. E un
   câștig net: dependințele ascunde (unde `DLStatus` sau `Download` sunt folosite
   peste granițe) devin vizibile și verificabile.
4. **Patch-urile rămân la import.** `_patch_customtkinter_destroy()` etc. trebuie
   să se execute înainte de crearea ferestrei, dar **după** importul lui
   `customtkinter`. În pachet: `catssoft/core/patches.py` aplică patch-urile la
   importul lui, iar `main.py` importă `patches` primul. E deja așa în fișierul
   unic (liniile 308–310).

## Punctul delicat: pictograma

Singurul loc unde `__file__` contează:

```python
icon_path = Path(__file__).parent / "icon" / "icon.ico"   # linia ~6700
```

În pachet, `__file__` ar fi `.../catssoft/ui/main_window.py` → ar căuta iconița în
`catssoft/ui/icon/`. Soluție: `catssoft/paths.py` cu o funcție care **caută sus**
până la folderul care conține `icon/icon.ico` și se oprește la primul găsit:

```python
def app_dir() -> Path:
    """Folderul aplicației - funcționează identic în pachet și în fișierul unic."""
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "icon" / "icon.ico").is_file():
            return candidate
    return here.parent if here.name == "ui" else here
```

## Adaptarea harness-ului de teste

| Fișier | Ce trebuie schimbat |
|---|---|
| `tests/_helpers.py` | `load_app()` importă pachetul în loc să execute un `.py`; `find_app_path()` devine `find_package_root()` |
| `tests/negative_control.py` | `_build_copy()` copiază **folderul** pachetului + aplică reverția pe fișierul potrivit din pachet (nu pe un singur fișier) |
| `run_tests.bat` | rămâne identic, dacă `tests/` rămâne în același folder |
| `tests/test_*.py` | nu se ating: referințele sunt `app.DatabaseManager`, `app.DLStatus` etc. — deci pachetul trebuie să **reexporteze** numele la nivel de modul (`catssoft/__init__.py` face `from .core.models import *` etc.) |

Ultimul punct e important: pentru ca **153 de teste existente să treacă nemodificate**,
`catssoft/__init__.py` trebuie să expună aceleași nume la nivel de pachet. Altfel
refactorul ar rescrie și testele, și am pierde singurul lucru care contează: același
harnais care a prins 29 de regresii.

## Varianta B: fișierul unic generat

`build_single_file.py`:
1. citește modulele în ordinea dependențelor,
2. **elimină** liniile `from catssoft... import ...` (importurile interne devin
   directe, fiindcă totul ajunge în același namespace),
3. păstrează importurile externe o singură dată (prima dată),
4. concatenează în ordinea **identică** cu cea din fișierul actual,
5. scrie `CatsSoft_Downloader_v6.13_Plexo_Engine.py`.

Verificarea că nu s-a pierdut nimic: **fișierul generat trebuie să aibă aceeași
semnătură AST** ca fișierul curent (comparare `ast.dump` a definițiilor de nivel
superior, module de import și ordine). E o verificare automată, nu o inspecție
vizuală — și e exact genul de control care a prins celelalte 29 de regresii.

## Ordinea de execuție (fiecare pas e verificabil)

1. **Fără schimbări de comportament**: creez pachetul mutând blocurile **verbatim**,
   în ordinea curentă. `main.py` = vechiul `__main__`.
2. `catssoft/__init__.py` reexportă numele → rulez suita **nemodificată**.
3. Semnătura AST a fișierului nou = cea a vechiului → dovadă că e aceeași aplicație.
4. `paths.py` pentru iconiță → rulez suita din nou.
5. (doar la varianta B) `build_single_file.py` → fișierul generat → rulez suita pe
   **ambele** forme (pachet și fișier) → comparație AST.
6. Actualizez `_helpers.py` și `negative_control.py` → rulez suita + controlul negativ.
7. Actualizez raportul și `tests/README.md`.

## Riscuri și cum sunt acoperite

| Risc | Acoperire |
|---|---|
| Un import circular | `python -c "import catssoft"` la fiecare pas; plus comparația AST |
| Testele nu mai găsesc simboluri | reexport la nivel de pachet + suita nemodificată |
| Comportament schimbat | comparația AST + 153 de teste + controale negative |
| Iconița nu se mai găsește | `paths.py` + test explicit |
| Livrabilul nu mai e „un singur fișier" | depinde de forma aleasă (A/B/C) |
