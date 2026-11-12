"""Rezolvarea căilor aplicaţiei.

Fişierul unic şi pachetul au structuri de disc diferite, iar pictograma
trăieşte în folderul aplicaţiei. Funcţia caută sus până la primul folder care
conţine ``icon/icon.ico``, deci merge în ambele forme.
"""
# ── orig:6633 ──


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


