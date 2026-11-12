"""Construiește executabilul Windows din pachet (PyInstaller).

    python tools\\build_exe.py            # onefile (un singur .exe)
    python tools\\build_exe.py --onedir   # director (start mai rapid)

Sursa e **pachetul** (`main.py` → `catssoft/`), adică sursa de adevăr - nu
artefactul generat. Principiul rămâne: pachetul e adevărul, orice altă formă
se generează din el.

Ce e nevoie pentru un build corect:
* pictograma merge ca *date* (`--add-data`), nu doar ca pictogramă de .exe -
  aplicația o caută în `icon\\icon.ico` la runtime, prin `app_dir()`;
* `pystray` și `plyer` au backend-uri încărcate dinamic, deci sunt declarate
  explicit ca importuri ascunse;
* baza de date nu stă lângă .exe, ci în `%LOCALAPPDATA%\\CatsSoft\\Downloader`
  (vezi `default_database_path()`), deci .exe-ul e portabil.
"""
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "CatsSoft Downloader v6.13"
ICON = os.path.join(ROOT, "icon", "icon.ico")
BUILD_DIR = os.path.join(ROOT, "build")

HIDDEN_IMPORTS = [
    "pystray._win32",
    "pystray._win32_win32",
    "plyer.notification",
    "plyer.notification.windows",
]


def main():
    if not os.path.isfile(ICON):
        print("!! nu există pictograma:", ICON)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("!! PyInstaller lipsește. Instalează-l cu:")
        print("   python -m pip install pyinstaller")
        return 1

    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR, ignore_errors=True)

    args = [
        os.path.join(ROOT, "main.py"),
        "--name", APP_NAME,
        "--paths", ROOT,
        "--icon", ICON,
        "--add-data", "%s;icon" % ICON,
        "--windowed",
        "--noupx",
        "--clean",
        "--noconfirm",
        "--distpath", os.path.join(BUILD_DIR, "dist"),
        "--workpath", os.path.join(BUILD_DIR, "work"),
        "--specpath", os.path.join(BUILD_DIR),
    ]
    if "--onedir" not in sys.argv:
        args.append("--onefile")
    for name in HIDDEN_IMPORTS:
        args.extend(["--hidden-import", name])

    print("rulare:", " ".join(args))
    from PyInstaller.__main__ import run
    run(args)

    target = os.path.join(BUILD_DIR, "dist", APP_NAME + ".exe")
    if os.path.isfile(target):
        print("\nOK: %s (%d octeți)" % (target, os.path.getsize(target)))
        return 0
    print("\n!! executabilul nu a fost produs")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
