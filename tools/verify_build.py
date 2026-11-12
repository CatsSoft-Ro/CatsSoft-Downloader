"""Verificarea livrabilului: pachetul -> fișierul unic.

Două verificări independente, ambele automate:

1. **Build la zi** - fișierul unic livrat trebuie să fie exact ceea ce produce
   `build_single_file.py` din pachetul curent. Dacă diferă, pachetul a fost
   modificat fără regenerarea artefactului (sau invers).
2. **Echivalență de cod** - definițiile de nivel superior ale pachetului și
   ale fișierului unic trebuie să fie identice (nume + AST), în aceeași ordine.
   Comparația e pe AST, nu pe text: o diferență de formatare nu contează, o
   diferență de cod contează.

    python tools\\verify_build.py

Ieșire: OK (totul e consistent) sau lista problemelor.
"""
import ast
import io
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACKAGE = os.path.join(ROOT, "catssoft")
ARTIFACT = os.path.join(ROOT, "CatsSoft_Downloader_v6.13_Plexo_Engine.py")
MARKER = re.compile(r"^# ── orig:(\d+) ──$")


def definitions(source, skip_entry=False):
    """[(nume, ast_dump)] pentru definițiile de nivel superior, în ordine."""
    tree = ast.parse(source)
    result = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        if skip_entry and isinstance(node, ast.If):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = ",".join(names) or "<assign>"
        elif isinstance(node, ast.AnnAssign):
            name = node.target.id if isinstance(node.target, ast.Name) else "<ann>"
        else:
            name = "<anonim>"
        result.append((name, ast.dump(node, annotate_fields=False)))
    return result


def read_blocks(path):
    """[(ancoră, text_bloc)] dintr-un modul marcat."""
    blocks = []
    anchor = None
    current = []
    for line in io.open(path, encoding="utf-8").read().splitlines():
        match = MARKER.match(line.strip())
        if match:
            if anchor is not None:
                blocks.append((anchor, "\n".join(current)))
            anchor = int(match.group(1))
            current = []
            continue
        if anchor is not None:
            current.append(line)
    if anchor is not None:
        blocks.append((anchor, "\n".join(current)))
    return blocks


def package_definitions():
    """Definițiile pachetului, asamblate în ordinea originală (după ancoră)."""
    blocks = []
    for directory, _dirs, files in os.walk(PACKAGE):
        for name in sorted(files):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            blocks.extend(read_blocks(os.path.join(directory, name)))
    blocks.extend(read_blocks(os.path.join(ROOT, "main.py")))
    blocks.sort(key=lambda item: item[0])
    result = []
    for _anchor, text in blocks:
        result.extend(definitions(text, skip_entry=True))
    return result


def build_content():
    """Exact ce produce `build_single_file.py`."""
    sys.path.insert(0, ROOT)
    import build_single_file
    return build_single_file.build()


def main():
    problems = 0

    print("=" * 70)
    print("1. FIȘIERUL UNIC E LA ZI?")
    print("=" * 70)
    if not os.path.isfile(ARTIFACT):
        print("!! lipsește artefactul:", ARTIFACT)
        return 1
    current = io.open(ARTIFACT, encoding="utf-8").read()
    rebuilt = build_content()
    if current == rebuilt:
        print("OK: fișierul unic e identic cu ce produce build_single_file.py.")
    else:
        print("!! fișierul unic diferă de cel generat (%d vs %d caractere)"
              % (len(current), len(rebuilt)))
        print("   rulează:  python build_single_file.py")
        problems += 1

    print()
    print("=" * 70)
    print("2. PACHETUL ȘI FIȘIERUL UNIC SUNT ECHIVALENTE?")
    print("=" * 70)
    package = package_definitions()
    artifact = definitions(current, skip_entry=True)
    print("pachet  : %d definiții" % len(package))
    print("artefact: %d definiții" % len(artifact))

    package_names = {name for name, _ in package}
    extras = [name for name, _ in artifact if name not in package_names]
    print("definiții doar în artefact:", extras or "niciuna")

    filtered = [(n, d) for n, d in artifact if n not in extras]
    if len(package) != len(filtered):
        print("!! număr diferit de definiții (%d vs %d)" % (len(package), len(filtered)))
        problems += 1
    else:
        differences = []
        for index, ((name_a, dump_a), (name_b, dump_b)) in enumerate(zip(package, filtered)):
            if name_a != name_b:
                differences.append((index, name_a, name_b, "nume diferit"))
            elif dump_a != dump_b:
                differences.append((index, name_a, name_b, "cod diferit"))
        if differences:
            print("!! %d diferențe:" % len(differences))
            for index, name_a, name_b, kind in differences[:20]:
                print("   #%d  %s -> %s  (%s)" % (index, name_a, name_b, kind))
            problems += 1
        else:
            print("OK: toate cele %d definiții sunt identice și în aceeași ordine." % len(package))

    print()
    if problems:
        print("VERIFICARE FAILED - %d problemă(e)." % problems)
        return 1
    print("VERIFICARE OK: pachetul, artefactul și sursa sunt consistente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
