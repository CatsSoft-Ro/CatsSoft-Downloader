# Teste — CatsSoft Downloader v6.13

Suita de teste automatizate pentru aplicația CatsSoft Downloader v6.13.

Aplicația există în **două forme**, iar suita le verifică pe **ambele**:

| Formă | Ce e | Rol |
|---|---|---|
| `package` | pachetul `catssoft/` | **sursa de adevăr** — aici se face orice modificare |
| `single` | `CatsSoft_Downloader_v6.13_Plexo_Engine.py` | **artefact de build** — generat din pachet, nu editat direct |

Dacă cele două forme se comportă diferit, una dintre ele a fost modificată fără
să se regenereze. De aceea `run_tests.bat` rulează suita de două ori, o dată
pe fiecare formă.

## Rulare

Tot dintr-un click:

```
run_tests.bat
```

Sau din terminal, în folderul proiectului:

```
python -m unittest discover -s tests -p "test_*.py" -v
```

Pe o formă anume (variabila de mediu `CATSSoft_FORM`):

```
set CATSSoft_FORM=package
python -m unittest discover -s tests -p "test_*.py" -v

set CATSSoft_FORM=single
python -m unittest discover -s tests -p "test_*.py" -v
```

Dacă `CATSSoft_FORM` nu e setată, se preferă pachetul; dacă acesta lipsește
din folder, se folosește fișierul unic.

Un singur fișier:

```
python tests\test_engine.py
```

Nu e nevoie de `pytest` — suita folosește doar `unittest` din biblioteca standard.

## Ce conține

| Fișier | Acoperire |
|---|---|
| `_helpers.py` | Localizarea formei testate (pachet sau fișier unic) + izolarea bazei de date și a folderului de descărcări |
| `test_data_layer.py` | SQLite, setări, programări (inclusiv baze vechi cu `ALTER TABLE`), istoric, export CSV |
| `test_engine.py` | Motor end-to-end cu server HTTP local: descărcare segmentată, reluare după anulare, retry după eroare tranzitorie, spațiu insuficient |
| `test_resume_and_cleanup.py` | Validarea reluării, scannerul de fișiere orfane, modul degradat (bază de date coruptă) |
| `test_ui.py` | Randarea cardului, ETA, tooltips, trunchierea numelui, garda swipe-vs-buton |
| `test_scheduler.py` | Programare „once" cu succes și cu eșec, reîncercare limitată, recurență zilnică |
| `test_auto_sort.py` | Fișierul terminat trebuie să rămână în folderul ales de utilizator |
| `test_shutdown.py` | Închiderea aplicației cu panoul de detalii deschis; comenzi Tcl orfane |
| `test_real_world.py` | Descărcări REALE pe gazde live (cu și fără suport Range), prin `AddURLDialog` |
| `test_soak.py` | Rulare reală a aplicației, plus calea reală de închidere |
| `test_db_concurrency.py` | Ciclu de viață al conexiunilor SQLite (reconectare după `close()`, fire scurte, scrieri concurente) |
| `test_engine_integrity.py` | Integritatea transferului: offset Range cu contor vechi, `If-Range`, spațiu disc la reluare, îmbinare cu dimensiune greșită, fișier orfan, lipsa validatorului, spike de viteză |
| `test_widget_lifecycle.py` | Ciclul de viață al widget-urilor: timerele `after` vs `destroy()`, excepții Tkinter |
| `test_defender_scan.py` | Integrarea cu Windows Defender: verdict pe textul ieșirii, threats extrase, „was skipped", timeout |
| `test_tray.py` | Minimizare în bara de sistem: fereastra se ascunde **doar** după ce iconul e confirmat viu |
| `test_round3.py` | Runda 3: multi-adaptor (traseu mort scos din rotație, fallback pe sesiunea de sistem), clipboard (timer/toast), persistența cozii, `deletecommand` tolerant |
| `test_audit_followup.py` | Defectele reale găsite într-un audit extern: spațiul pentru asamblarea segmentelor (coborârea automată pe calea simplă), „disc plin" la asamblare, Stop închide răspunsul HTTP activ, neutralizarea formulelor CSV în export |
| `test_audit_round4.py` | Runda 4: calea segmentată la Stop (răspunsurile deschise sunt urmărite și închise), Stop care nu blochează interfața, oprirea pe o conexiune vie, anularea unei reîncercări automate programate, timerul unic de polling, gardul handler-ului `<Configure>` |
| `test_features_audit.py` | Funcționalitățile cerute după auditul extern: proxy (normalizare, aplicare pe sesiunea globală și pe cele per-adaptor, degradare fără PySocks, **transfer end-to-end printr-un proxy HTTP local real**), sortare dinamică în tab-ul Descărcări (viteză / dimensiune / ETA), dirty flag la persistența cozii active |
| `test_large_file.py` | **Opțional** — descărcare live multi-fragment pe un fișier de 100 MB, cu comparație de hash SHA-256 |
| `_toast_probe.py` | **Nu e test** — sondă rulată într-un **proces separat**, pentru calea reală a toast-ului de clipboard (vezi „Limitare cunoscută" mai jos) |
| `negative_control.py` | **Nu e test** — reconstruiește comportamentul pre-fix și cere ca testele de mai sus să **pică**. Rulează-l ca să verifici că testele prind regresiile |

## Rulează suita cu interpreterul aplicației

**Important:** rulează testele cu **același Python cu care rulezi aplicația**.
O regresie („can't delete Tcl command" la închidere) a apărut doar pe Portable
Python 3.10 + CustomTkinter și nu se reproducea pe Python 3.14 — diferențele de
versiune de tkinter schimbă comportamentul la distrugerea widget-urilor.

```
"C:\Users\Andruska\Downloads\Portable Python\App\Python\python.exe" -m unittest discover -s tests -p "test_*.py"
```

Pentru rularea cu un singur click, editează `run_tests.bat` și înlocuiește
`python` cu calea de mai sus.

## Izolare

Fiecare test rulează întotdeauna pe o bază de date și un folder de descărcări
temporare (`%TEMP%\catssoft_test_*`), curățate la final. **Suita nu atinge niciodată
istoricul real, coada reală sau fișierele tale.**

Forma testată se alege prin `CATSSoft_FORM` (vezi mai sus). În forma `package`,
`catssoft/__init__.py` reexportă întregul namespace al aplicației — inclusiv
numele private — ca testele să poată folosi `app.DatabaseManager`,
`app._cancel_pending_after` etc. **exact ca înainte de împărțire**, fără să fi
modificat vreun test.

În forma de pachet, codul își ia numele din modulul propriu
(`catssoft.core.engine` etc.), deci un patch aplicat doar pe namespace-ul
pachetului nu ar ajunge nicăieri. De aceea `_helpers.py` întoarce un **proxy**
care propagă orice atribuire (`app.check_disk_space = ...`) în toate modulele
aplicației — astfel testele se comportă identic în ambele forme.

## Serverul de test

`test_engine.py` ridică un server HTTP local pe `127.0.0.1` (port aleator) care:

- suportă cereri `Range` și răspunde corect cu `Content-Range`;
- poate injecta erori `500` tranzitorii, pentru a testa calea de retry;
- poate bloca transferul la jumătate, ca anularea să fie deterministă.

## Testele „real world"

`test_real_world.py` face descărcări **reale** de pe internet, prin `AddURLDialog`:

- `files.pythonhosted.org` → `206` + `Content-Range` → calea **segmentată**;
- `speed.cloudflare.com/__down` → `200` fără `Content-Range` → calea **simplă**.

Testele se **sar automat** dacă nu există conexiune, deci suita rămâne rulabilă
offline. Sunt singurele teste care au prins un bug major: `auto_sort_file` muta
fișierul terminat în `~/Downloads/<Categorie>`, ignorând folderul ales de utilizator.

## Testul de fișier mare (opțional)

```
set CATSSoft_RUN_LARGE=1
python tests\test_large_file.py
```

Descărcă un fișier de 100 MB de la o gazdă reală prin calea **segmentată**
multi-fragment și compară SHA-256 cu o descărcare de referință. Durează ~1 minut
și consumă ~200 MB, de aceea nu rulează implicit.

De ce există: singurul fișier descărcat live din suita obișnuită are 110 KB, iar
grila de fragmente (1–8 MiB) produce pentru el un singur interval. Calea segmentată
multi-fragment — cea care rulează pe fișiere mari, cu reluare parțială și „endgame
racing" — era validată doar de serverul local de test. Pe 100 MB: **50 de fragmente,
SHA-256 identic cu referința, zero fragmente `.part` rămase**.

## Controale negative — de ce contează

```
python tests\negative_control.py
```

Scriptul copiază aplicația într-un folder temporar, **reface comportamentul pre-fix**
(conexiune închisă folosită de alt fir, `If-Range` la cereri de la zero, îmbinare fără
validare, reluare fără validator, fișier orfan acceptat, contor vechi ca offset Range,
traseu mort lăsat în rotația multi-adaptor, timerul toast-ului neanulat, `CTkToplevel`
neacoperit de patch, `deletecommand` intolerant, spațiul pentru asamblare neverificat,
răspunsul HTTP neînchis la Stop, formulele CSV brute, răspunsurile segmentate neurmărite,
Stop care blochează interfața, guardul reîncercării programate, guardul timerului de
polling, handlerul `<Configure>` nepăzuit)
și rulează suita contra copiei. Testele de regresie trebuie să **pică**; dacă unul
continuă să treacă, înseamnă că nu verifică ceea ce pretinde.

Controlul rulează pe **ambele forme**: reverțiile sunt aplicate fie pe modulele
pachetului, fie pe fișierul unic, și suita e rulată în forma corespunzătoare.
Fragmentele sunt scrise pe textul originalului, iar blocurile pachetului sunt
secvențe contigue din el — de aceea aceleași fragmente descriu aceleași regresii
în ambele forme. Dacă un fragment nu e găsit sau apare în mai multe module,
scriptul se oprește cu mesaj explicit, în loc să omită verificarea.

## Limitare cunoscută: CustomTkinter 6.0.0 și rădăcini multiple

CustomTkinter 6.0.0 intră în **„access violation"** (procesul moare direct) la crearea
unei **a doua rădăcini `CTk`** în același proces, după ce a existat un `CTkToplevel`.
Bisekul pe scenarii a arătat că declanșează **doar** `CTkToplevel` — un
`tkinter.Toplevel` obișnuit e nevinovat.

Aplicația reală creează o singură rădăcină `CTk`, deci **nu e expusă**. Dar suita creează
mai multe ferestre, de aceea:

- testele runda 3 **nu creează ferestre Tk reale** în procesul principal — folosesc
  dubluri (fakes) pentru toast;
- calea reală a toast-ului (butonul „Ignoră", al doilea toast, `deletecommand`) e
  verificată într-un **proces separat**, prin `_toast_probe.py`, invocat de
  `test_round3.py`.

Regula pentru orice test nou: **dacă creează un `CTkToplevel`, rulează-l într-un proces
separat.**

## Note

- Testele UI și soak necesită un mediu grafic (Windows desktop).
- Zgomotul `invalid command name "...check_dpi_scaling"` la final vine din
  CustomTkinter (timer intern de scalare DPI) și nu din aplicație.
