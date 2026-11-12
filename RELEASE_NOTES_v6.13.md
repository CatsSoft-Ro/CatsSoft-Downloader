# CatsSoft Downloader v6.13 — Adaptive Plexo-Inspired Engine

Descărcător pentru Windows cu motor de transfer adaptiv: segmentare, reluare,
multi-adaptor, proxy și interfață în română. Tot codul e auditat, testat și
publicat sub licență MIT.

---

## Descărcare

| Fișier | Pentru cine |
|---|---|
| `CatsSoft Downloader v6.13.exe` (~37 MB) | **Utilizatori** - un singur fișier, nu are nevoie de Python instalat |
| `CatsSoft-Downloader-v6.13-portable.zip` | exe + documentație, dezarhivat și rulat |
| `CatsSoft-Downloader-v6.13-source.zip` | **Dezvoltatori** - sursa completă (inclusiv pachetul `catssoft/`) |

Baza de date se creează automat în `%LOCALAPPDATA%\CatsSoft\Downloader\`. Dacă
există o bază de date veche lângă aplicație, e folosită în continuare — nu se
migrează forțat și nu se pierde nimic.

---

## Ce e nou

**Proxy HTTP / HTTPS / SOCKS5** — se aplică pe sesiunea globală și pe fiecare
adaptor de rețea. Un proxy invalid nu strică transferul: se păstrează valoarea
veche și se avertizează. SOCKS5 fără PySocks e ignorat, nu produce eroare.

**Sortare dinamică în tab-ul Descărcări** — viteză, dimensiune sau ETA, cu
reevaluare la 2 secunde, fără rescrierea bazei de date.

**Dirty flag la persistența cozii active** — coada se scrie în SQLite doar când
 chiar s-a schimbat (stare sau progres ≥ 1%), cu salvare forțată la închidere.

**Executabil Windows** — construit cu PyInstaller direct din pachetul sursă.

**ETA pe card**, tooltips pe toate butoanele, trunchierea numelor lungi,
curățarea fișierelor temporare orfane, salvare atomică a setărilor.

## Corectat

Cele mai importante, dintr-o serie de **4 runde de audit**:

- **Stop nu îngheața interfața** — închiderea socket-ului pe firul UI bloca până
  la timeout. Măsurat: **15,00 s înainte → 0,00 s după**.
- **Calea segmentată nu răspundea la Stop** — răspunsurile HTTP deschise erau
  urmărite doar pe calea simplă.
- **Fișier orfan acceptat ca complet** — o descărcare părăsită în folder putea fi
  raportată ca terminată.
- **Contor vechi folosit ca offset Range** — reluarea putea corupe rezultatul.
- **`If-Range` trimis și la cereri de la zero** — serverele care îl ignoră
  puteau returna conținut complet peste segmentul nou.
- **Îmbinare fără validarea dimensiunii** — segmentele puteau fi îmbinate la o
  dimensiune diferită de cea anunțată.
- **Spațiu insuficient pentru asamblare** — descărcarea pe segmente consuma tot
  bandwidth-ul și eșua la final; acum coboară automat pe calea simplă.
- **CSV injection în exportul istoricului** — celulele care încep cu `=`, `+`,
  `-` sau `@` sunt neutralizate.
- **373 de excepții Tkinter la închidere** — acum **0**.
- Două **guard-uri moarte** (anularea unei reluări programate, anularea timerului
  de polling) care lăsau acțiuni pendente.

## Structura proiectului

Sursa de adevăr e pachetul `catssoft/` (21 module în `core/`, `ui/`, `utils/`);
fisierul `.py` de 9.896 de linii e un **artefact de build** generat automat.

```
catssoft/
  core/    engine, network, database, config, automation, scheduler, smart, security, models, patches
  ui/      main_window, cards, details, dialogs, theme, widgets
  utils/   files, formatting
tools/     build_exe.py, verify_build.py
```

Modificările se fac în pachet:

```bash
python build_single_file.py     # regenerează fișierul unic
python tools/build_exe.py       # regenerează executabilul
python tools/verify_build.py    # verifică consistența
```

## Verificare

- **153 de teste automate** — verzi pe **ambele forme** ale aplicației.
- **8 grupuri de controale negative** (34 fragmente de reverție) — reconstruiesc
  comportamentul pre-fix și cer ca testele de regresie să pică, pe ambele forme.
- **Semnătură AST** a celor 134 de definiții de nivel superior: identică și în
  aceeași ordine, în pachet și în artefact.
- Descărcare reală de **100 MB** prin calea segmentată multi-fragment, cu hash
  SHA-256 identic cu referința.

## Note de actualizare

- Baza de date **nu se schimbă** între versiuni; o bază veche e folosită în
  continuare.
- Executabilul e complet autonom — `customtkinter`, `requests`, `Pillow`,
  `pystray` și `plyer` sunt în interior.
- `yt-dlp` și `ffmpeg` **nu** sunt folosite de cod, deci nu sunt înglobate.

## Licență

MIT — vezi [`LICENSE`](LICENSE).
