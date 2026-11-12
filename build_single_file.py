"""Generează fișierul unic din pachet.

Pachetul e sursa de adevăr; fişierul `.py` e un artefact de build.

Formatul pachetului: fiecare modul e o secvenţă de blocuri, iar fiecare bloc
poartă ancora cu linia sa din fişierul original:

    # ── orig:1234 ──
    <liniile originale ale blocului>

Reasamblarea e o simplă concatenare în ordinea ancorelor, deci fişierul
generat reproduce originalul **octet cu octet**, cu excepţia modificărilor
intenţionate (calea pictogramei şi helperul ``app_dir``).

    python build_single_file.py [--check]

`--check` nu scrie nimic: doar compară rezultatul cu fişierul țintă și
raportează diferențele.
"""
import io
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(ROOT, "catssoft")
HEADER = os.path.join(ROOT, "build_header.py")
TARGET = os.path.join(ROOT, "CatsSoft_Downloader_v6.13_Plexo_Engine.py")

MARKER = re.compile(r"^# ── orig:(\d+) ──$")


def read_blocks(path):
    """[(linie_originală, text_bloc)] dintr-un modul marcat."""
    blocks = []
    current_line = None
    current = []
    for line in io.open(path, encoding="utf-8").read().splitlines():
        match = MARKER.match(line.strip())
        if match:
            if current_line is not None:
                blocks.append((current_line, "\n".join(current)))
            current_line = int(match.group(1))
            current = []
            continue
        if current_line is not None:
            current.append(line)
    if current_line is not None:
        blocks.append((current_line, "\n".join(current)))
    return blocks


def build():
    blocks = []
    for directory, _dirs, files in os.walk(PACKAGE):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            if name == "__init__.py":
                continue
            blocks.extend(read_blocks(os.path.join(directory, name)))
    blocks.extend(read_blocks(os.path.join(ROOT, "main.py")))
    blocks.sort(key=lambda item: item[0])

    header = io.open(HEADER, encoding="utf-8").read().rstrip("\n")
    parts = [header]
    parts.extend(text for _line, text in blocks)
    return "\n".join(parts) + "\n"


def main():
    content = build()
    check_only = "--check" in sys.argv
    if check_only:
        if os.path.isfile(TARGET):
            current = io.open(TARGET, encoding="utf-8").read()
            if current == content:
                print("OK: fișierul generat e identic cu ținta.")
                return 0
            print("!! fișierul generat diferă de țintă (%d vs %d caractere)"
                  % (len(content), len(current)))
            return 1
        print("!! nu există ținta: %s" % TARGET)
        return 1
    with io.open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    print("scris: %s (%d linii)" % (TARGET, len(content.splitlines())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
