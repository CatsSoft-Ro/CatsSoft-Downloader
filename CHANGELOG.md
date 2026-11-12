# Changelog

Toate schimbările importante ale proiectului sunt documentate aici.
Formatul e bazat pe [Keep a Changelog](https://keepachangelog.com/ro/1.1.0/),
versiunea e bazată pe [Semantic Versioning](https://semver.org/lang/ro/).

## [6.13] - 2026-11-08

Aplicația a fost auditată exhaustiv (4 runde), reparată, extinsă cu funcționalități
cerute explicit și apoi **împărțită în module**. Detalii complete în
[`REPORT_v6.13.md`](REPORT_v6.13.md).

### Adaugat

- **Executabil Windows** (`CatsSoft Downloader v6.13.exe`, ~37 MB) - un singur
  fisier, fara Python instalat. Construit cu PyInstaller din pachet.
- **Proxy HTTP / HTTPS / SOCKS5** - se aplica pe sesiunea globala si pe fiecare
  adaptor de retea. Daca proxy-ul e invalid se pastreaza valoarea veche si se
  avertizeaza; SOCKS5 fara PySocks e ignorat, nu crapa.
- **Sortare dinamica** in tab-ul Descarcari: viteza, dimensiune, ETA. Se
  reevalueaza la 2 secunde, fara rescrierea bazei de date.
- **Dirty flag la persistenta cozii active** - coada se scrie doar cand chiar s-a
  schimbat (stare sau progres >= 1%), iar la inchidere se salveaza fortat.
- **ETA** pe card, calculat din viteza masurata.
- **Tooltips** pe toate butoanele de actiune.
- **Trunchierea numelor lungi** pe card.
- **Curatarea fisierelor temporare orfane** (segmente `.part`, `.race`,
  `.catssoft-meta.json` ramase fara owner).
- **Salvare atomica a setarilor** (scrie intr-un fisier temporar, apoi
  `os.replace`) - o inchidere in mijloc nu mai strica setarile.
- **Impartirea in module** - sursa de adevar e acum pachetul `catssoft/`
  (21 module), iar fisierul `.py` de 9.896 de linii devine un artefact de build
  generat automat. Diferenta textuala fata de versiunea anterioara: exact 2
  zone, ambele intentionate.

### Corectat

- **Calea segmentata nu raspundea la Stop** - raspunsurile HTTP deschise erau
  urmarite doar in calea simpla.
- **Stop congela interfata** - inchiderea socket-ului pe firul UI bloca pana la
  timeout (masurat: 15,00 s inainte, 0,00 s dupa fix); inchiderea se face acum
  intr-un fir secundar.
- **Guard mort in `_stop_download`** - programarea unei reincercari era anulata
  inainte de a fi capturata.
- **Guard mort in `_poll_updates`** - timerul de polling pendent nu era anulat.
- **373 de exceptii Tkinter la inchidere** - handler-ul `<Configure>` era pastrat
  fara paza si timerele subarborelui nu erau anulate. Acum: 0 exceptii, 10 linii
  de cod in loc de 7.724.
- **Fişier orfan acceptat ca complet** - o descarcare părăsită în folder putea fi
  considerată terminată.
- **Contor vechi folosit ca offset Range** - reluarea putea corupe rezultatul.
- **`If-Range` trimis si la cereri de la zero** - serverele care ignora
  `If-Range` puteau returna continut complet peste segmentul nou.
- **Imbinare fara validarea dimensiunii** - segmentele imbinate puteau avea o
  dimensiune diferita de cea anuntata.
- **Spaţiu insuficient pentru asamblare** - descarcarea pe segmente consuma
  tot bandwidth-ul si esua la final; acum coboara automat pe calea simpla.
- **Exportul istoricului scria celulele brute** - un server maliţios putea
  injecta o formula in CSV-ul deschis de utilizator (`=`, `+`, `-`, `@`).
- **Adaptorul mort ramanea in rotatie** dupa cooldown.
- **Timerul toast-ului de clipboard** nu era anulat la inchidere.
- **Ciclul de persistenta** nu pornea daca dialogul de reluare arunca o
  exceptie - coada activa nu mai era salvata niciodata.

### Verificat

- **153 de teste automate**, pe **ambele forme** ale aplicatiei (pachet si
  fisier unic), plus **8 grupuri de controale negative** (34 fragmente de
  revertie) rulate pe ambele forme.
- **Semnatura AST** a celor 134 de definitii de nivel superior: identica si in
  aceeasi ordine, in pachet si in artefact.
- **Descarcare reala de 100 MB** prin calea segmentata multi-fragment, cu hash
  SHA-256 identic cu referinta si zero fragmente `.part` ramase.
- **Transfer end-to-end printr-un proxy HTTP local real.**

## [6.12] - Plexo Engine

Motorul segmentat cu grila de fragmente adaptativa (1-8 MiB), validare cu
validator (`ETag` / `Last-Modified`), detectarea fisierelor orfane si salvarea
atomica a setarilor.

## [6.11] - Smart Automation

Programari cu reincercare limitata si recurenta zilnica, monitor de clipboard,
tray si monitor de retea multi-adaptor.

## [6.10] - History Pro Performance

Istoric cu cautare si export, paginare, si optimizari pe baza de date SQLite.

## [6.9] si mai devreme

Carduri cu swipe, temă întunecată, descărcări simple si segmentate, programări
de bază, integrare cu Windows Defender.
