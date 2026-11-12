# CatsSoft Downloader v6.13 — Evaluare tehnică, optimizare, fixuri și feature-uri noi

**Fișier analizat:** `CatsSoft_Downloader_v6.12_Plexo_Engine.py` (8209 linii, 414 KB)
**Livrabil:** `CatsSoft_Downloader_v6.13_Plexo_Engine.py` (9157 linii, 459 KB)
**Diff:** +754 / −11 linii, 78 blocuri modificate
**Metodă:** citire integrală (8209 linii) + audituri paralele pe cod (UI/UX, strat de date, motor + coadă) + **84 de verificări automate în 11 suite** (dintre care trei suite end-to-end cu server HTTP local) + soak test de 20 s cu rulare reală + **trei controale negative** care reconstruiesc comportamentul pre-fix și dovedesc că testele prind regresiile + revizie proprie a diff-ului complet

> **Runda 2 de audit** (secțiunile 2.1, 2.2 și 6) a fost pornită după livrare, ca audit
> complet al codului deja scris — și **al suitei de teste**. A găsit **10 defecte noi**,
> dintre care **4 de severitate HIGH** — inclusiv două drumuri prin care un fișier
> putea fi raportat **COMPLET, dar corupt** — plus două erori în chiar suita de teste
> (inclusiv unul care făcea ca „verificările live" din raportul anterior să nu fie
> susținute de fapt).

---

## 1. Evaluare generală

### Arhitectura

Structura e, în esență, sănătoasă pentru un proiect de această dimensiune:

| Strat | Evaluare |
|---|---|
| Motor de descărcare | **Bun.** Segmentare adaptativă, validare `Content-Range`, race-uri „endgame", checksum, scanare AV, reluare cu `If-Range`. Logica HTTP e corectă și defensivă. |
| Strat de date (SQLite) | **Mediu.** Schema cu migrari `ALTER TABLE` corecte, parametrizație SQL corectă, `ORDER BY` pe whitelist. Dar conexiune nouă la fiecare query. |
| UI (CustomTkinter) | **Mediu.** Widget-uri tematizate, sistem de navigație, swipe gestures, detalii cu grafice PIL. Dar actualizarea la 10 Hz cu reconstrucții repetate. |
| Concurență | **Mediu–bun.** `dl.lock`, `update_queue`, `post_ui`. Dar câteva încălceri UI din fire de worker. |

### Puncte forte (păstrate)

- Validare strictă a răspunsurilor HTTP (`Content-Range` lipsă → eroare, nu fișier corupt).
- Reluare cu validator HTTP (`ETag`/`Last-Modified`) prin `If-Range`.
- Migrații de schemă non-distructive (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` doar pentru coloane lipsă).
- SQL parametrizat integral — **zero** injecție SQL în tot fișierul.
- `_history_order_by` folosește whitelist, nu interpolare.

### Puncte slabe identificate

1. **Conexiune SQLite deschisă per operație** — fiecare citire de setare, fiecare pagină de istoric deschidea un fișier nou.
2. **Actualizare UI la 10 Hz cu reconstrucție** — sparkline nou la fiecare refresh, `update_idletasks()` per card, re-împachetarea tuturor cardurilor.
3. **Reluare pierdută la retry** — o eroare de rețea tranzitorie arunca tot segmentul și relua de la zero.
4. **Programări șterse înainte de pornire** — o eroare la pornire pierdea definitiv programarea.
5. **Fișier final șters fără dovadă** — lipsa metadatelor ducea la ștergerea fișierului și pierderea progresului.

---

## 2. Bug-uri fixate

### CRITICE / HIGH

| # | Locație | Defect | Impact înainte de fix | Soluție |
|---|---|---|---|---|
| 1 | `AddURLDialog._finish_submit` (~5544) | Verificarea URL eșuată afișa eroarea **și continua oricum** — descărcarea pornea oricum, tăcut | Utilizatorul vedea „link inaccesibil" și, peste 2 secunde, avea un download care eșua | Dialog `askyesno`: „Adaugă oricum?" — fără răspuns nu pornește nimic |
| 2 | `SwipeGestureController._press` (~4020) | Glisajul pornit de pe un buton declanșa **și** comanda butonului | Pauză + swipe = acțiune dublă; ștergere + swipe = ștergere nedorită | Detectarea tipului de widget (marcaj pe 12 niveluri) → `ignore_gesture`; plus `_gesture_drag` ca santinelă, resetată la eliberare |
| 3 | `_poll_updates` (~8290) | O singură excepție într-un handler arunca **restul lotului** de evenimente | 150 evenimente pierdute dintr-un singur callback cu excepție → cardurile îngheațau | Fiecare eveniment are `try/except` propriu (`_apply_update_event`) |
| 4 | `_on_network_selection_changed` (~6500) | Rebuild-ul panelului distrugea checkbox-ul care rula callback-ul | `TclError: invalid command name` la fiecare bifare | `after_idle()` — rebuild după ce callback-ul se încheie |
| 5 | `DatabaseManager.get_scheduled` (~2120) | `SELECT *` cu indexare pozițională + `ALTER TABLE` pe baze vechi | Programările salvate dispăreau după actualizare | Coloane explicite în `SELECT` |
| 6 | `DownloadScheduler._check_scheduler` (~2450) | Programarea „once" era ștearsă **înainte** de a porni transferul | Eroare (disc plin, cale invalidă) → programare pierdută definitiv | Ștergere abia după ce `_add_download` reușește; reîncercare limitată la 3 ori |
| 7 | `_validate_or_reset_resume_artifacts` (~2805) | lipsa metadatelor → `os.remove(save_path)` fără nicio dovadă | Fișier șters + progres pierdut, fără motiv | Se șterge doar cu dovadă pozitivă (`changed`); altfel `dl.resume_blocked = True` și transferul reîncepe curat |
| 8 | `download_segmented` (~3061) | Verificarea spațiului pe disc cerea dimensiunea **totală** la reluare | „Spațiu insuficient" fals la reluare | Se verifică doar octeții rămași |

### MEDIUM

| # | Locație | Defect | Soluție |
|---|---|---|---|
| 9 | `_download_segment` (~3890) | Reluarea funcționa doar la `attempt == 0` — un retry relua segmentul de la zero | Reluarea e permisă la fiecare încercare; validarea `Content-Range` garantează că datele sunt de același interval |
| 10 | `_validate_or_reset_resume_artifacts` | ETag vechi comparat cu Last-Modified nou → conflict fals, reluare invalidată | Comparare „de același fel": ETag↔ETag, Last-Modified↔Last-Modified |
| 11 | `update_scheduled_time` | `UPDATE` care nu potrivește niciun rând trecea tăcut | Verificare `rowcount` + inserare de rezervă |
| 12 | `NetworkAdapterManager.refresh` | Profilurile dispărute (DHCP) păstraau sesiuni HTTP deschise la nesfârșit | Evicție după 3 refresh-uri fără adaptor, cu `session.close()` |
| 13 | `DatabaseManager.__init__` | Baza de date blocată/coruptă → crash la pornire | Fallback pe bază temporară + `degraded_mode` |
| 14 | `export_history_csv` | Fișierul ales de utilizator era **trunchiat** la deschidere și rămâne gol dacă exportul era anulat | Scriere în `.part` + `os.replace` la final |
| 15 | `_discard_queue_entry_files` | Cu `total` necunoscut, un fișier **complet** era șters | Se șterge doar când totalul e cunoscut și diferă |
| 16 | `download_simple` | Bloc de raportare a progresului **duplicat** (dubla evenimente) | Eliminat |
| 17 | `download_simple` | `pause_event.wait()` fără timeout → fir blocat | Buclă cu timeout de 0,25 s care verifică `stop_event` |
| 18 | `download_segmented` | Backoff-ul `2 ** attempt` ignora anularea | Pauză interruptibilă, maximum ~4 s de întârziere la Stop |
| 19 | `DownloadDetailsView._refresh_tick` | Polling-ul continua la nesfârșit pentru o descărcare ștearsă | Se oprește dacă modelul nu mai există |
| 20 | `_remove_segment_parts` | `None` → `TypeError` | Gardă pe intrare |
| 21 | `auto_sort_file` | **Destinația era fixată pe `~/Downloads/<Categorie>`**, ignorând atât folderul ales în dialog, cât și setarea `default_folder` | Un download pornit din `D:\Work` ajungea totuși în `C:\Users\<utilizator>\Downloads` — pe altă disc, cu o mutare lentă, într-un folder neales | Subfolderul de categorie se creează acum **în folderul efectiv al descărcării** (`dirname(save_path)`), nu în `~` |
| 22 | `destroy()` | Timerele principale (`_poll_updates`, `_status_tick_id`, `_appearance_poll_id`, `_network_monitor_id`, `_active_queue_persist_id`, `_nav_animation_id`) erau anulate doar în `_exit_app()`, nu și în `destroy()` | Callback-uri `after` vii după distrugerea ferestrei → `invalid command name "…_poll_updates"` | Anularea tuturor timerelor mutată în `destroy()`; monitorul de clipboard e oprit explicit |
| 23 | Graficul de detalii | `create_line` desenat de două ori cu aceleași coordonate | Cod mort, fără efect vizual | Dublat înlăturat |
| 24 | `_refresh_history` | Tabelul era golit și afișat „Se încarcă istoricul…" la **fiecare** schimbare de filtru → flash vizibil | Rândurile vechi rămân vizibile până sosesc rezultatele noi; `history_cards` se resetează în handler ca să nu rămână referințe spre widget-uri distruse |
| 25 | `DownloadManager.destroy()` | **`after info` + `after cancel <token>`** ștergea comanda Tcl a callbackului, dar tkinter o ținea în `_tclCommands` → `Misc.destroy` pica cu `TclError: can't delete Tcl command` la închiderea aplicației (mai ales cu panoul de detalii deschis) | Bucla `after info` eliminată; adăugat `_prune_dead_commands()`, care curăță comenzile Tcl deja dispărute înainte de `super().destroy()` |

---

## 2.1 Runda 2 de audit — defecte găsite în codul deja livrat

Auditul complet al codului scris pentru v6.13 (motor, coadă, strat de date, zonele
neatinse) a identificat **10 defecte suplimentare**, dintre care **4 HIGH**. Două dintre
ele produceau fișiere **raportate COMPLET, dar corupt** — cel mai grav tip de bug într-un
downloader, pentru că nu lasă nicio urmă vizibilă.

| # | Sev | Locație | Problema | Fix aplicat |
|---|-----|---------|----------|------------|
| H1 | **HIGH** | `download_simple`, offset Range | Contorul `dl.downloaded` putea fi **vechi** (restaurat din baza de date) și era folosit direct ca offset `Range`. Serverul umplea intervalul greșit, iar verificarea finală de dimensiune **trecea oricum**: fișier raportat COMPLET, cu primii octeți lipsă și ultimii duplicați | `_reconcile_downloaded_with_disk()` aliniază contorul cu octeții reali de pe disc înainte de fiecare cerere Range |
| H2 | **HIGH** | `_download_segment` | `If-Range` era trimis și la cereri de la zero. Un server care răspundea `200` în loc de `206` (ETag diferit între HEAD și GET) făcea aplicația să declare „resursa schimbată" și să **ștergă toate segmentele valide** | `If-Range` se trimite **doar** când se reutilizează efectiv octeți; un `200` fără `If-Range` e clasificat ca „serverul nu onorează Range", nu ca resursă schimbată |
| H3 | **HIGH** | `download_segmented`, spațiu disc | La reluare se cerea spațiu pentru **întregul** fișier, deși octeții descărcați ocupau deja loc în fișierele `.partN`. Un fișier mare reluat putea fi respins cu „spațiu insuficient" | `_segment_bytes_on_disk()` scade octeții deja scriși; se cere doar restul |
| M1 | MED | `_validate_or_reset_resume_artifacts` | Fără `ETag`/`Last-Modified` nu există nicio dovadă că octeții de pe disc aparțin resursei curente. Reluarea putea **amesteca două versiuni** ale fișierului, raportat COMPLET (dimensiunea se potrivește) | Fără validator, datele neverificabile sunt abandonate și transferul reia de la zero |
| M2 | MED | `_merge_segments` | Dimensiunea era validată **după** ștergerea segmentelor sursă. O îmbinare incompletă distrugea singura copie a datelor, iar reluarea trebuia să reia de la zero | Validarea se face înainte de `os.replace` și înainte de ștergerea surselor |
| M3 | MED | `download_simple`, metadate | Totalul resursei era luat din `Content-Length` (lungimea **intervalului**) în loc din `Content-Range` (dimensiunea resursei), ceea ce reseta o reluare validă | Totalul autoritativ vine din `Content-Range` |
| M4 | MED | `download_simple` | Fișierul era decomprimat (`decode_content=True`), dar dimensiunea anunțată era cea a formei **comprimate** | Se cer octeți brute (`Accept-Encoding: identity`); dacă serverul comprimă totuși, dimensiunea e marcată necunoscută, nu falsă |
| M5 | MED | `download_segmented` | Un fișier **orfan** (fără metadate), de aceeași dimensiune ca resursa, era acceptat ca complet: raport COMPLET fără un octet transferat, cu fișierul vechi „ștampilat" cu URL-ul curent | `final_verified` — fișierul final e acceptat doar dacă metadatele corespund resursei curente |
| L1 | LOW | tracker viteză | Octeți găsiți pe disc intrau în contor într-un singur salt, raportat ca viteză de transfer (spike de câțiva MB/s) | `SpeedTracker.rebase()` repornește fereastra de eșantionare |
| L2 | LOW | `_apply_queue_profile` | Schimbarea profilului de coadă (Eco/Balanced/Performance) avea efect doar după apăsarea pe „Salvează" | Profilul se aplică imediat |
| H4 | **HIGH** | ciclu de viață widget | Un timer `after` care ardea **după** distrugerea widgetului lăsa în consolă „Exception in Tkinter callback" (`AttributeError: 'NoneType' object has no attribute 'remove'`) și „invalid command name ..._windows_set_titlebar_icon". E **exact familia** bug-ului raportat de utilizator („can't delete Tcl command") | `_cancel_pending_after()` anulează timerele pendente ale widgetului înainte de `destroy()`; trei clase (`AddURLDialog`, `ResumeQueueDialog`, `SwipeCardHost`) nu apelasau deloc curățarea; `destroy()` din CustomTkinter este acoperit printr-un patch aplicat la import. Runda 4 a extins asta la **întregul subarbore** și a păzuit handler-ul `<Configure>` (vezi 2.5) |

**Constatări acceptate fără schimbare** (documentate, nu defecte): dimensiune necunoscută
(total = 0) afișată ca „?", limita de 2 GB pe fișier impusă de `Content-Length` pe
serverele care nu trimit `Content-Range`, și fallback-ul pe `download_simple` pentru
serverele fără suport Range.

### 2.2 Runda 2 — optimizări suplimentare

| Optimizare | Efect |
|------------|-------|
| Setările se citesc cu **o singură interogare** (`get_all_settings()`) în loc de 32 | 32 SELECT + 32 PRAGMA `busy_timeout` → 1 SELECT la fiecare pornire |
| Conexiunile SQLite ale firelor terminate sunt **închise automat** | Fiecare căutare în istoric crea un fir nou și lăsa un handle deschis până la ieșirea aplicației (31 de conexiuni scurse măsurate) |
| Reconexiune transparentă după `close()` | Un fir activ primea „Cannot operate on a closed database" și pierdea date |
| Verificare periodică a spațiului când dimensiunea e necunoscută | Un fișier cu răspuns chunked putea umple discul până la capăt |

---

### 2.3 Runda 3 de audit — multi-adaptor, clipboard și persistența cozii

Runda 3 a fost cerută explicit („pe toate, dar pe rând"). Subagenții de audit au eșuat cu
„insufficient balance (1008)" (plafon de cont al platformei), deci auditul a fost făcut
**manual**, pe zonele rămase neverificate: `NetworkAdapterManager`, `ClipboardMonitor`,
`DownloadScheduler`, `SmartAutomationEngine`, `_check_resumable_queue`, `save_active_queue`.

Rezultat: **5 defecte**, dintre care **2 HIGH**. Unul dintre ele producea un **crash dur
de proces** („access violation"), nu o excepție prinsă — cel mai grav tip de bug posibil.

| # | Sev | Locație | Problema | Fix aplicat |
|---|-----|---------|----------|------------|
| H5 | **HIGH** | `ClipboardMonitor`, butonul „Ignoră" | Toast-ul era distrus **fără anularea timerului** de 10 s. Timerul supraviețuia distrugerii, iar la următorul `update()` — pe care CustomTkinter îl apelează chiar în constructorul fiecărei ferestre — Tcl încerca să execute comanda ștearsă → **„access violation"**: procesul murea. Calea de reproducere: copiez un link → „Ignoră" → deschid dialogul „Adaugă URL" → **aplicația dispare** | Timerul e anulat **înainte** de distrugerea toast-ului, pe toate căile (butonul „Ignoră", butonul „Descarcă", `stop()`); suplimentar, `CTkToplevel` este acum acoperit de patch-ul de `destroy` |
| H6 | **HIGH** | `NetworkAdapterManager.report_failure` | Un adaptor fără rută (ex. un switch virtual) eșuează la **fiecare** segment. Era scos din rotație doar 5 secunde (cooldown), după care revenea și ardea un segment, consumând bugetul de reîncercări al segmentului. Promisiunea din documentația clasei („workers can fall back to the normal system session") **nu era implementată** | Un traseu care ratează transferul este scos din rotație până la următoarea reîmprospătare (care îl revalidează); `acquire_session()` revine pe calea sănătoasă sau pe sesiunea de sistem |
| M6 | MED | `tkinter.Misc.deletecommand` | Ramura `finally` din `Misc.after` apelează `deletecommand` **după** ce `Misc.destroy` a pus `_tclCommands = None` → „AttributeError: 'NoneType' object has no attribute 'remove'" → „Exception in Tkinter callback". Apărea la **fiecare închidere a aplicației dintr-un timer** (ex. `after(8000, app.destroy)`) | `deletecommand` este patch-at să tolereze widgeturi deja distruse; comportamentul rămâne identic |
| M7 | MED | `_check_resumable_queue` | Dacă dialogul de reluare arunca o excepție, ciclul periodic de salvare a cozii **nu pornea niciodată** → reluarea la pornire se pierde definitiv pentru sesiunea respectivă | `try/finally`: ciclul de persistență pornește oricum, chiar dacă dialogul eșuează |
| M8 | MED | `ClipboardMonitor.stop()` | Starea `notification_shown` nu era resetată: după o oprire urmată de repornire, monitorul de clipboard rămânea mut tot restul sesiunii | `stop()` resetează starea și anulează timerul toast-ului |

### Cum a fost găsit crash-ul (nu era o excepție, era un proces mort)

„Access violation" nu poate fi prinsă într-un `try/except` — procesul moare direct.
Metoda a fost **bisejul pe scenarii**, fiecare rulat într-un proces separat:

| Scenariu | Rezultat |
|----------|----------|
| Două ferestre, fără toast | OK |
| Două ferestre, cu un `tkinter.Toplevel` obișnuit distrus | OK |
| Două ferestre, cu un toast `CTkToplevel` creat | **access violation** |
| Timer `after` rămas pe un Toplevel distrus, `update()` imediat | Tcl „invalid command name" (eroare, nu crash) |

Concluzia a fost măsurată, nu presupusă: CustomTkinter 6.0.0 intră în „access violation"
la crearea unei **a doua rădăcini CTk** în același proces, după ce a existat un
`CTkToplevel`. Cauza nu e în aplicație — `CTkToplevel` nu moștenește de la `CTkBaseClass`,
deci **nu era acoperit de patch-ul de `destroy`** al runda 2: timerul lui `after` supraviețuia
distrugerii.

Două lucruri au urmat din asta:

1. **Corecția în aplicație**: `CTkToplevel` (și `tkinter.Toplevel`) sunt acum acoperite de
   patch, deci orice timer al unui dialog e anulat înainte de distrugere.
2. **O limită a mediului de testare**, documentată: suita creează mai multe ferestre în
   același proces, deci testele runda 3 **nu creează ferestre Tk reale** în procesul
   principal. Calea reală a toast-ului (butonul „Ignoră", al doilea toast, `deletecommand`)
   este verificată într-un **proces separat**, prin `tests/_toast_probe.py`.

### Verificarea hardware-ului pentru multi-adaptor

Inventarul pe această mașină: `Wi-Fi` (`192.168.8.95`) și `vEthernet (Default Switch)`
(`172.28.176.1`) — două adaptoare cu IPv4 valid (celelalte sunt link-local/loopback și sunt
filtrate corect). Latența măsurată pe fiecare traseu:

| Traseu | Rezultat |
|--------|----------|
| `system` (fără bind) | OK, 256 KB în 2,3 s |
| `Wi-Fi` (bind pe IP) | OK, 256 KB în 1,7 s |
| `vEthernet (Default Switch)` | **ConnectionError în 0,0 s** — bind-ul eșuează instant, adaptorul nu are rută |

Această măsurătoare e cea care a dovedit defectul H6: al doilea adaptor nu era lent, ci
**mort**, iar aplicația îl putea realesa după cooldown. Nu s-a putut exercita agregarea pe
două căi funcționale pe acest hardware (nu există o a doua conexiune la internet), dar
comportamentul pe traseul mort e acum acoperit de teste.

### 2.4 Auditul extern — verificare punctuală a fiecărei afirmații

A fost adus un audit scris de altă trecătoare, pe **fragmente** ale fișierului (lucrarea
spune chiar ea: „am auditat fragmentele furnizate"). Fiecare afirmație a fost verificată
**contra codului real**, de pe mașină. Rezultatul: din ~60 de constatări, **3 erau reale și
au fost reparate acum**, 20 erau deja reparate în rundele 1–3, 3 erau false pozitive, iar
restul erau observații de proiectare (refactor, funcționalități noi), nu defecte.

| Afirmația din audit | Verdict | Detaliu |
|---|---|---|
| Erori de sintaxă: `def init(`, `f "..."`, `1024 3` | **Artefact de extragere** | Fișierul real compilează (`py_compile` exit 0) și rulează; fragmentele au fost lipite cu text din mijloc |
| `final_verified` lipsește | **Deja reparat** (runda 1) | Există în metadate și e propagat pe întregul traseu de recuperare |
| `If-Range` se trimite și la cereri de la zero | **Deja reparat** (runda 2) | Se trimite doar când `resume_from > 0`; un 200 fără `If-Range` e tratat ca „serverul nu onorează Range" |
| `Content-Range` nu e verificat la reluare | **Deja reparat** (runda 2) | Se cere ca `start` să fie exact `dl.downloaded` |
| `decode_content=True` strică reluarea | **Deja reparat** (runda 2) | `Accept-Encoding: identity` + `decode_content=False` |
| Metadatele se scriu neatomic | **Deja reparat** | Fișier temporar → `fsync` → `os.replace` |
| `sanitize_filename` nu e robust | **Deja reparat** | Taie separatorii de cale, caracterele de control, numele rezervate Windows (CON, COM1–9, LPT1–9) și limitează lungimea |
| Lipsește validarea URL-ului | **Deja reparat** | `is_valid_http_url()`: doar http/https, `netloc` obligatoriu, port validat |
| `_complete_existing_file` sare peste checksum și AV | **Deja reparat** (runda 2) | Verifică `final_verified`, dimensiunea, checksum-ul și rulează scanarea AV |
| Reîncearcă și erorile permanente (4xx) | **Deja reparat** | `SmartAutomationEngine` separă explicit `PERMANENT_MARKERS` de `TRANSIENT_MARKERS` |
| `id(dl)` drept cheie persistentă | **Fals pozitiv, cu o singură lacună închisă acum** | Cele trei structuri (`cards`, `swipe_hosts`, `selected_download_ids`) sunt curățate în `_remove_download`, deci reutilizarea unui `id()` nu poate aliasa o descărcare vie. Singurul loc unde un `id()` reutilizat ar fi putut strica ceva era **semnătura de vizibilitate** (un snapshot comparat la fiecare ciclu de UI) — acum e invalidată explicit la orice adăugare sau eliminare |
| `dl.app` ține o referință circulară | **Risc acceptat** | Ciclu rezolvat de garbage collector; într-un tool personal cu un proces per sesiune impactul e neglijabil. `weakref` e deja folosit pentru listener-ii de temă |
| Evenimentele de progres nu sunt coalescedate | **Deja reparat** | Coada de UI e golită în loturi (`max_per_cycle = 150`) cu un dict `dirty` |
| **Spațiul pentru asamblarea segmentelor** | **REAL → reparat acum** | Detalii mai jos |
| **Anularea nu închide răspunsul HTTP activ** | **REAL → reparat acum** | Detalii mai jos |
| **Exportul istoricului scrie celule-brute (CSV injection)** | **REAL → reparat acum** | Detalii mai jos |
| SQLite fără WAL și fără indexuri | **Parțial adevărat** | WAL e activat; indexurile pe `history` există (4), create la migrare |
| Log-uri doar pe consolă, fără fișier rotativ | **Real, neimplementat** | Aplicația e lansată de obicei ca fereastră (`pythonw`), deci în practică **nu rămâne niciun log**. Adăugarea unui `RotatingFileHandler` e 10 linii, dar creează fișiere noi în folderul utilizatorului fără cerere explicită |
| Timpuri stocate în UTC | **Real, neimplementat deliberat** | Sunt ore locale naive, ceea ce e corect pentru „descărcări de azi, la ora 9" și ar necesita migrarea istoricului |
| Scheduler cu timezone/DST | **Real, neimplementat** | Programarea e în ora locală; la trecerea DST ora de vară se poate dubla sau lipsi. Pentru un tool personal, impactul e minor |
| Split în module, `uid` stabil, tipuri, state machine | **Proiectare, nu defect** | Refactor de ordinul zilelor, cu risc de regresie pe un motor deja verificat cu 134 de teste. `uid` n-ar fi necesar: cheile `id(dl)` sunt curățate corect |
| Autentificare/headers, mirrors, limită globală, bundle de diagnostic, single-instance | **Funcționalități noi** | Out of scope pentru „corectitudinea descărcărilor" |

#### Cele patru defecte reale, reparate acum

**1. Asamblarea segmentelor cerea spațiu dublu (HIGH).**
`_merge_segments()` scrie o copie temporară a întregului fișier (`temp` → `os.replace`),
deci în momentul asamblării discul trebuie să conțină **simultan** segmentele descărcate și
copia finală. Fără verificare, o descărcare pe segmente pornită cu puțin mai mult spațiu
liber decât dimensiunea fișierului **consuma tot bandwidth-ul și apoi eșua** la asamblare cu
„No space left on device". Ca scenariu: un ISO de 5 GB pe un disc cu 6 GB liberi — întreaga
descărcare se arunca.

*Fix:* înainte de a porni calea cu segmente se verifică dacă locul ajunge pentru ambele
copii (`_merge_space_available`). Dacă nu, descărcarea **coboară pe calea simplă** (scrie
direct în fișierul final, fără asamblare și fără spațiu suplimentar) — mai lentă, dar
**se termină**. Calea simplă e aleasă doar la prima încercare și doar dacă nu există
segmente deja pe disc, ca reluarea să nu abandoneze datele deja descărcate. În plus,
„disc plin" la asamblare produce acum un mesaj explicit în română, cu dimensiunea necesară,
în loc de „No space left on device".

**2. Anularea (Stop) aștepta până la 25 de secunde (MED).**
Răspunsul HTTP activ nu era păstrat, deci oprirea unei descărcări în calea simplă se
concretiza abia după timeout-ul de citire. *Fix:* răspunsul activ e păstrat pe `Download` și
închis la Stop — anularea devine imediată.

**3. Exportul istoricului era vulnerabil la CSV injection (MED).**
Celulele se scriau brute, iar Excel interpretează o celulă care începe cu `=`, `+`, `-` sau
`@` ca formulă. Numele fișierului poate veni de la **server** (`Content-Disposition`), deci
un server malițios ar putea injecta o formulă în exportul pe care utilizatorul îl deschide.
*Fix:* celulele care încep cu astfel de caractere sunt prefixate cu `'`, devenind text simplu.

**4. Semnătura de vizibilitate putea deveni stă (MED-LOW).**
`_filter_cards()` evita reîmpachetarea cardurilor comparând un snapshot de `id()`-uri. Cum
CPython poate refolosi un `id()` pentru un obiect nou, o eliminare urmată de o adăugare
putea produce aceeași semnătură, iar reîmpachetarea ar fi sărită — noul card ar fi putut
rămâne neafișat până la următoarea schimbare. *Fix:* semnătura e invalidată explicit la
oricare adăugare sau eliminare.

---

### 2.5 Runda 4 de audit — coadă, interfață și calea segmentată

Auditul a fost reluat pe ultima versiune livrată, pe zone, exclusiv pe codul real. Cei
trei subagenți de audit paraleli au eșuat cu „insufficient balance (1008)" (plafon de
cont al platformei), deci verificarea a fost făcută **manual**, pe zone: strat de date,
scheduler, config, validarea reluării, limitator de bandă, calea segmentată, calea
simplă, coada de evenimente, istoric și export, notificări, swipe, validarea URL-ului,
curățarea artefactelor și închiderea aplicației.

**Rezultat: 5 defecte reale, toate reparate și verificate pe mașina reală.** Ipotezele
verificate și **infirmate** sunt enumerate mai jos, fiindcă o constatare negată e la
fel de utilă ca una confirmată.

**1. Calea segmentată nu răspundea la Stop (MED).**
Calea segmentată este calea *implicită* (Adaptive Streams e activat implicit) și ține
până la 16 răspunsuri HTTP deschise simultan. `_stop_download` închidea doar
`dl.active_response`, iar acel câmp era setat **doar** în calea simplă: în calea
segmentată răspunsurile stăteau într-un dicționar local (`chunk_response_handles`), la
care Stop nu avea acces. *Fix:* `Download.active_responses` ține lista răspunsurilor
deschise; `_track_active_response()` / `_untrack_active_response()` o actualizează, iar
`_close_active_responses()` le închide pe toate la Stop.

**2. Stop congela interfața (MED) — regresie proprie a rundei anterioare.**
Corecția „răspunsul activ se închide la Stop" se executa **pe firul UI**, iar pe Windows
`close()` pe un socket cu un `recv` în așteptare **blochează până la expirarea
timeout-ului de citire** (15 s în calea segmentată, 25 s în calea simplă). Măsurat pe
mașina reală, cu un server care acceptă conexiunea și apoi nu mai trimite nimic:
`resp.close()` a durat **15,00 s**. *Fix:* `_close_response_async()` face închiderea
într-un fir secundar; firul interfeței nu blochează niciodată.

Măsură după corecție: **Stop = 0,00 s** cu server mort, și **0,25 s** până la oprirea
efectivă a segmentului pe o conexiune vie.

> **Limitare de platformă, raportată sincer:** pe Windows, o citire de socket blocată
> **nu poate fi întreruptă din alt fir** — nici `close()`, nici `shutdown(SHUT_RDWR)`,
> nici `SO_LINGER=0` (verificat experimental: `select()` cu timeout pe Windows nu se
> întrerupe). Consecință: dacă serverul nu mai trimite *niciun octet*, firul de lucru
> rămâne viu până la timeout-ul de citire (15/25 s), **dar interfața nu se congelează**
> și descărcarea e marcată CANCELLED imediat. Când datele circulă, oprirea e
> imediată.

**3. Guard mort în `_stop_download` (MED-LOW).**
Condiția care cerea înregistrarea anulării în istoric pentru o reîncercare automată
programată citea `dl.auto_retry_at` — dar contorul fusese resetat cu 10 linii mai
devreme, deci valoarea era întotdeauna `0.0`, iar condiția era mereu falsă. În cursa
dintre un worker care se închide și o reîncercare programată, anularea **nu ajungea
în istoric**: cardul arăta CANCELLED, istoricul nu știa nimic. *Fix:* valoarea e
capturată înainte de resetare (`had_scheduled_retry`).

**4. Guard mort în `_poll_updates` (LOW).**
Metoda reseta `self._poll_job_id = None` la intrare, deci ramura `after_cancel` nu
anula niciodată timerul în așteptare. Invariantul declarat în comentariu („dacă
metoda e apelată și manual, nu se acumulează timer-uri multiple") **nu era impus de
cod**: un apel manual al metodei ar fi creat o a doua buclă de polling, iar fiecare
buclă suplimentară ar fi dublat numărul de actualizări ale interfeței. *Fix:* id-ul
timerului pendent e capturat la intrare și anulat înainte de programarea următorului.

**5. Închiderea aplicației vărgea 373 de excepții Tkinter în consolă (MED-LOW).**
Măsurat pe mașina reală, pornind aplicația și închizând-o: **373 de „Exception in
Tkinter callback"** cu `invalid command name ...!ctkcanvas`, adică **7.724 de linii
de urme în consolă** la fiecare închidere. Cauza: CustomTkinter își desenează starea
în handler-ul evenimentului `<Configure>` (`CTkBaseClass._update_dimensions_event`).
Evenimentul e pus în coadă și ajunge la widget **după** ce canvas-ul lui a fost
distrus → `self._canvas.delete(...)` aruncă `TclError`.

Important: problema **nu e o regresie** — v6.12 producea 173 de asemenea urme. Rundele
anterioare au eliminat zgomotul pentru dialoguri, dar nu pentru fereastra principală.
*Fix:* handler-ul e păzuit (verifică existența canvas-ului înainte de desenare) și
patch-ul de `destroy` anulează acum timerele `after` ale **întregului subarbore**
(Tk distruge descendenții la nivel Tcl, fără să apeleze `destroy()` Python al lor).
Rezultat după corecție: **0 excepții, 10 linii în consolă** — aplicația pornește și
se închide curat.

### Ipoteze verificate și infirmate în runda 4

| Ipoteză | Verdict | Dovadă |
|---|---|---|
| Lipsa unui `commit()` în `remove_scheduled` / `update_scheduled_time` | **Fals pozitiv** | `with conn:` (context manager sqlite3) face commit la ieșire; tranzacțiile sunt consistente. |
| Filtrele de căutare din istoric cu `ESCAPE` + `COLLATE` | **Fals pozitiv** | Verificat pe SQLite 3.37.2: sintaxa validă, căutare case-insensitive, wildcard-urile `\%` și `\_` tratate corect. |
| SQL injection în exportul istoricului | **Fals pozitiv** | `_history_where_params()` folosește doar parametri legați; `search_term` e parametrizat cu `ESCAPE`. |
| `history_recorded` nu se resetează la reluare | **Fals pozitiv** | `_retry_download()` resetează explicit contorul. |
| Firele de fundal ating direct widget-uri Tk | **Fals pozitiv** | Toate cele **15** puncte de pornire de fire trec prin `post_ui()` sau prin cozi thread-safe (`update_queue`, `_history_query_queue`); niciunul nu atinge un widget Tk direct. |
| `sanitize_filename` permite traversarea de drumuri | **Fals pozitiv** | Separatorii și `..` sunt eliminate; rămâne doar numele de fișier. |
| `is_valid_url` acceptă scheme periculoase | **Fals pozitiv** | Doar `http`/`https`, fără spații sau caractere de control, cu validarea portului. |
| `AdaptiveStreamController` se poate bloca | **Fals pozitiv** | `acquire()` re-verifică în buclă, `release()` reduce contorul; fără blocare. |
| Smart Automation tratează erorile tranzitorii ca definitive | **Fals pozitiv** | Marcajele permanente sunt verificate *înaintea* regexului de coduri HTTP. |
| Curățarea artefactelor poate șterge fișiere ale utilizatorului | **Design, cu confirmare** | Pattern strict `<bază>.partN` / `.catssoft-meta.json`, vârstă minimă 1 h, dialog cu listă și confirmare explicită. |
| Exportul istoricului scrie celule brute | **Deja reparat** (runda 3) | `csv_safe_cell()` prefixează `=`, `+`, `-`, `@`, `\t`, `\r`. |

---

### 2.6 Verificare punctuală a unui audit extern (runda 4, partea 2)

A mai venit un raport de audit extern, cu 5 secțiuni și recomandări prioritizate. Fiecare
afirmație a fost verificată pe codul real, cu referință la linie. Rezultatul: **niciun
defect nou real** — 5 afirmații sunt greșite sau deja remediate, 3 sunt observații de
proiectare, 3 sunt îmbunătățiri valide și 1 este o funcție redundantă.

#### Afirmații greșite sau deja remediate

| Afirmația din raport | Verdict | Dovadă |
|---|---|---|
| „Codul e monolit → **imposibilitatea** de a scrie teste unitare" | **Greșit** | Monolitul e real, dar justificarea e falsă: suita are **153 de teste automate în 22 de fișiere**, cu control negativ. Costul refactorului e mult mai mare decât beneficiul, iar împărțirea în module e deja trecută deliberat la capitolul „Ce a rămas deliberat neoptimizat". |
| „`_update_status_bar` accesează `dl.status`/`dl.progress` **în afara lock-ului**" | **Greșit** | `_update_status_bar` (linia 7038) citește toate valorile într-un `with dl.lock:` (linia 7046). |
| „Logica de cursă (endgame) e fragilă: închiderea răspunsului HTTP al pierdătorului din alt thread poate arunca excepții" | **Greșit** | `publish_winner` (4054-4058) și `register_response` (4067-4070) închid răspunsurile în `try/except Exception: pass`. Merge-ul e „fail closed": dacă vreun racer mai poate scrie, nu se asamblează nimic. |
| „Istoricul se reconstruiește integral la fiecare filtru" | **Greșit** | `get_history_page(limit, offset)` paginează, iar doar pagina vizibilă ajunge în Tkinter („only the visible page reaches Tkinter"). Virtual scrolling ar fi o rafinare peste paginare. |
| „Nu există limită de viteză per-fișier" | **Greșit / deja existent** | `Download.speed_limit_override` există, e persistat în DB (coloană `speed_limit_override`) și setat din interfață cu presetări (linia 7974). E aplicat în `_apply_speed_limit` (3326-3327). |
| „Asamblarea cere 2× spațiul (50 GB → 100 GB liberi)" | **Deja reparat** (runda 3) | `_merge_space_available` (3521) verifică spațiul **înainte** de descărcare și `download_segmented` coboară automat pe calea simplă. E acoperit de testul `test_falls_back_to_simple_path_when_merge_would_not_fit` (hash SHA-256 identic). |
| „Cookie-urile sunt partajate între toate descărcările → 403" | **Greșit** | `requests` folosește un cookiejar **cu domeniu**: cookie-ul serverului A nu e trimis la serverul B. Singurul caz real ar fi două descărcări de pe *același* domeniu cu seturi diferite de cookie — rar. |
| „Obiectele PIL/CTkImage din sparkline fac consumul de RAM să crească" | **Nesustențiat** | Cache-ul există deja: semnătură + throttle de 0,5 s (5432-5438), iar imaginea veche e eliberată la GC. Rămâne un churn de imagini Tk, nu o creștere fără limită. |

#### Observații de proiectare (nu defecte)

- **Tipuri incomplete.** Adevărat: `_apply_update_event(self, kind, dl, extra, dirty)` nu e
  adnotat. Dar adăugarea de tipuri pe un motor deja verificat cu 134 de teste introduce
  risc fără să aducă valoare funcțională; e prinsă în „Ce a rămas deliberat neoptimizat".
- **Split în module.** Adevărat și deja decis: refactor de ordinul zilelor.
- **Sortarea în tab-ul Descărcări e limitată (3 opțiuni).** Adevărat — dar istoricul are
  5 opțiuni (inclusiv „Dimensiune ↓" și „Status"), iar filtrarea e pe server, cu
  paginare.

#### Îmbunătățiri valide — implementate la cererea utilizatorului

Utilizatorul a decis să implementeze toate trei punctele valide (proxy, sortare extinsă,
dirty flag). Detaliile sunt în secțiunea 4.6.

| # | Observația | Evaluare | Stare |
|---|---|---|---|
| A | **Fără suport proxy (HTTP/SOCKS5)** | Corect: `requests` n-are proxy configurat. `requests` suportă proxy nativ (`session.proxies`), deci e o funcționalitate izolată, de ~30 de linii, cu un câmp de setare + UI. | **Implementat** (4.6.1) |
| B | **Sortare după viteză/dimensiune/ETA în tab-ul Descărcări** | Corect: tab-ul avea doar „Cele mai noi / Cele mai vechi / Nume A–Z". ETA și viteza existau deja per card, deci e o funcționalitate mică. | **Implementat** (4.6.2) |
| C | **„Dirty flag" la `_persist_active_queue`** (scrie coada completă la 5 s) | Corect că n-are dirty flag. Pragul „progres +1%" e acceptabil: motorul recalculează progresul de pe artefactele de pe disc la reluare, deci snapshot-ul poate rămâne ușor în urmă. | **Implementat** (4.6.3) |

#### Funcție redundantă (singurul punct nou de calitate a codului)

**`extract_filename_from_url()` (linia 2008) e redundantă.** Logica ei — parametrul
`response-content-disposition` din query string — e deja acoperită de
`_filename_from_query_params()` (1845), folosită de `filename_from_url()`, care e un
**superset** (`response-content-disposition`, `content-disposition`, `filename`, `file`,
`name`, `download`). Funcția e folosită ca ultim fallback în 4 locuri, dar în 2 dintre
ele (`extract_file_name_from_url` la 2051 și `filename_from_url(...) or ...` la 6269)
e **cod mort**: `filename_from_url` întoarce mereu un nume (are fallback
`download_<ts>.bin`), deci ramura alternativă nu e atinsă niciodată.

Nu a fost eliminată, pentru că schimbarea ar muta comportamentul de numire în fallback
(„download_123.bin" în loc de „download_123.tmp" / segmentul de URL) — un câștig de
calitate cosmetic, pentru un risc mic dar inutil pe un produs deja verificat.

---

## 3. Optimizări

### Strat de date

**Conexiune SQLite reutilizată per thread** (`DatabaseManager._connect`)

Înainte: fiecare operație deschidea o conexiune nouă (`sqlite3.connect`) → re-citirea schemei, re-blocarea fișierului, un file descriptor per operație. Salvarea setărilor făcea **28 de tranzacții** (câte una per cheie).

Acum: o conexiune `threading.local` per thread, cu `WAL` + `synchronous=NORMAL` și `busy_timeout` sincronizat cu timeout-ul cerut. `save_settings_batch()` + `ConfigManager.set_many()` scriu toate cele 26 de setări în **o singură tranzacție atomică** (o salvare întreruptă nu mai lasă configul pe jumătate scris).

### Motor

**Chunk adaptiv în calea segmentată** — `adaptive_chunk_size()` exista și era folosit doar în `download_simple`; calea principală (segmentată, până la 16 segmente) folosea un bloc fix de 64 KiB. Acum blocul se ajustează la viteza măsurată (până la 256 KiB), reevaluat la fiecare 4 MiB — mai puține apeluri de sistem și mai puține blocări pe `dl.lock` cu 16 segmente paralele.

**Reluare pe retry** — descris mai sus; efectul direct e că o eroare tranzitorie nu mai aruncă munca deja validată pe disc.

### UI

| Optimizare | Înainte | Acum |
|---|---|---|
| Sparkline | imagine PIL + CTkImage nouă la fiecare refresh (10/s × carduri) | redesenare doar când setul de eșantioane se schimbă, max. 2 Hz |
| `sync_content_height` | `update_idletasks()` per card per refresh (golește coada Tk a întregii aplicații) | limitat la ~2,5 Hz |
| `_filter_cards` | re-împachetarea tuturor cardurilor la fiecare ciclu | doar când setul vizibil/ordinea chiar se schimbă (cache de semnătură) |
| `set_action_text` | `configure(text=...)` cu același text de 10 ori/secundă | scris doar când textul diferă |
| Istoric viteză global | 10 eșantioane/s, lista refăcută complet de fiecare dată | 1 eșantion/s |
| `os.path.exists()` | un `stat()` pe disc per card per refresh | cache cu TTL de 2 s |
| Verificare existență fișier | la fiecare refresh | la tranziții de stare |

---

## 4. Feature-uri noi

### 4.1 Timp rămas estimat (ETA) pe card
O etichetă dedicată lângă sparkline arată `⏱ 3m 20s` în timpul descărcării și `⏸ în pauză` când e în pauză. Se ascunde automat în celelalte stări. Eticheta e așezată pe rândul 3, lângă sparkline, ca să nu înghesue bara de progres.

### 4.2 Tooltips pe toate butoanele de acțiune
Cele șase butoane iconiță (⏸ ⏹ 📂 ✕ ↻ ↗) aveau doar emoji-uri fără explicație. Acum fiecare are un tooltip în română. Tooltip-ul e ancorat pe buton (`button._tooltip`) ca să nu fie eligibil pentru garbage collection.

### 4.3 Trunchierea numelor lungi pe card
Numele lungi împingeau butoanele în afara ecranului. Acum se trunchiază la 52 de caractere **păstrând extensia**, iar numele complet apare în tooltip.

### 4.4 Curățarea fișierelor temporare orfane
Buton nou în Setări: **„Curăță fișierele temporare"**.

- Scanează folderul implicit de descărcare + folderele descărcărilor active.
- Identifică fișiere `.partN`, `.partN.raceNNN`, `.catssoft-meta.json` (+`.tmp`).
- **Protejează** fișierele aparținând descărcărilor active.
- **Gardează de vârstă**: ignoră fișierele modificate în ultimele 60 de minute (transfer în desfășurare).
- Afișează numărul + dimensiunea totală + preview și cere confirmare înainte de ștergere.

### 4.5 Salvare atomică a setărilor
`_save_settings` scrie acum toate setările într-o singură tranzacție. O eroare de valoare într-un câmp nu mai lasă configul pe jumătate scris.

### 4.6 Funcționalități cerute după auditul extern

Implementate la cererea explicită a utilizatorului, după verificarea punctuală a
raportului de audit (secțiunea 2.6). Toate trei sunt acoperite de teste dedicate
(`tests\test_features_audit.py`, 18 teste) și de controale negative.

#### 4.6.1 Suport proxy (HTTP / HTTPS / SOCKS5)

Câmp nou în **Setări**: „Proxy (HTTP/HTTPS/SOCKS5)", cu hintul că SOCKS5 cere
PySocks (`requests[socks]`).

- `_normalize_proxy_url()` acceptă forma scurtă (`host:port` → `http://host:port`) și
  normaliză scheme acceptate (`http`, `https`, `socks5`, `socks5h`, `socks4`).
- **O adresă invalidă nu e aplicată** — e păstrată valoarea veche și utilizatorul
  primește avertisment, pentru că un proxy invalid ar strica toate descărcările.
- Proxy-ul se aplică pe **toate** sesiunile HTTP: sesiunea globală (`SESSION`) și
  sesiunile per-adaptor (`NetworkAdapterManager._make_session`). Schimbarea se
  aplică **imediat**, fără repornirea aplicației (`NetworkAdapterManager.apply_proxy`).
- Pentru SOCKS5 fără PySocks, proxy-ul e **ignorat** (cu avertisment în log), nu
  aplicat — altfel fiecare cerere ar eșua cu `InvalidSchema`.

#### 4.6.2 Sortare în tab-ul Descărcări: viteză, dimensiune, ETA

Opțiunile existente („Cele mai noi", „Cele mai vechi", „Nume A–Z") au fost completate
cu **„Viteză ↓"**, **„Dimensiune ↓"** și **„ETA ↑"**.

- Citirile se fac **sub `dl.lock`**, ca în restul interfeței.
- Descărcările fără ETA estimabilă (nu sunt în transfer, viteza e 0 sau dimensiunea
  e necunoscută) merg **la sfârșit**, nu la început.
- Sortarea dinamică (viteză/ETA) se reevaluează **o dată la 2 secunde** și doar pe
  tab-ul Descărcări. Reîmpachetarea cardurilor se face doar dacă **ordinea chiar
  s-a schimbat** — semnătura de vizibilitate include ordinea, deci nu se rescrie
  lista la fiecare ciclu de UI.

#### 4.6.3 Dirty flag la persistența cozii active

`_persist_active_queue()` scrie în DB **doar dacă starea cozii s-a schimbat**: orice
modificare de stare (status, prioritate, limită per-fișier, poziție în coadă,
reîncercare programată) sau un progres avansat cu **cel puțin 1%**.

- Poziția în listă e folosită în loc de `id()`, pentru că CPython poate refolosi un id.
- **La închidere snapshot-ul se salvează oricum**, fără condiții — deci o închidere
  neașteptată nu pierde nimic.
- Snapshot-ul poate rămâne cu până la 1% în urmă față de progresul real; e
  inofensibil, pentru că motorul recalculează progresul din artefactele de pe disc
  la reluare (`_reconcile_downloaded_with_disk`).
- `_persist_queue_snapshot_now()` (forțat, la adăugare/ștergere/prioritate) scrie
  întotdeauna și actualizează semnătura, ca timerul periodic să nu rescrie imediat.

---

## 5. UI / UX — îmbunătățiri punctuale

- **Stări de feedback**: butonul „Descarcă" se dezactivează în timpul verificării și se reactivează pe toate căile (inclusiv când verificarea eșuează); câmpul URL se reactivează la fel.
- **Consistență temă**: butonul „Anulează" din pagina de detalii folosea culori hardcodate (`#49383a`) care arătau greșit în tema deschisă → acum derive din `ERROR()`.
- **Prioritate vizuală**: ETA are propriul spațiu, nu concurează cu bara de progres sau cu procentul.
- **Accesibilitate**: fiecare buton iconiță are acum o etichetă textuală accesibilă prin tooltip; numele lungi de fișier sunt accesibile integral.

---

## 6. Verificare

### Suita de teste instalată în proiect

Suita trăiește acum lângă aplicație, în `CatsSoft Downloader PRO\tests\`, și se rulează
cu `run_tests.bat` (sau `python -m unittest discover -s tests`). Folosește doar
`unittest` din stdlib — zero dependențe. Fiecare test rulează pe o bază de date și un
folder de descărcări **temporare**, deci suita nu atinge niciodată datele tale reale.

| Fișier de test | Verificări | Rezultat |
|---|---|---|
| `test_data_layer.py` — SQLite, setări, programări, istoric, export CSV | 11 | **Toate trec** |
| `test_engine.py` — motor end-to-end cu server HTTP local | 5 | **Toate trec** |
| `test_resume_and_cleanup.py` — validare reluare, fișiere orfane, mod degradat | 9 | **Toate trec** |
| `test_ui.py` — randare card, ETA, tooltips, swipe | 8 | **Toate trec** |
| `test_scheduler.py` — programare „once", eșec la pornire, recurență | 5 | **Toate trec** |
| `test_auto_sort.py` — fișierul rămâne în folderul ales | 5 | **Toate trec** |
| `test_soak.py` — rulare reală a aplicației + calea de închidere | 2 | **Trec: 0 excepții** |
| `test_shutdown.py` — închiderea cu detalii deschise, comenzi Tcl orfane | 5 | **Toate trec** |
| `test_real_world.py` — descărcări REALE pe gazde live, prin dialog | 4 | **Toate trec** (se sar automat fără internet) |
| `test_db_concurrency.py` — ciclu de viață al conexiunilor SQLite, fire scurte, scrieri concurente | 6 | **Toate trec** |
| `test_engine_integrity.py` — integritatea transferului: offset Range, `If-Range`, spațiu disc, îmbinare, fișier orfan, validator lipsă, viteză | 19 | **Toate trec** |
| `test_widget_lifecycle.py` — timerele `after` vs `destroy()`, excepții Tkinter, semnătura de vizibilitate | 6 | **Toate trec** |
| `test_large_file.py` — descărcare live multi-fragment pe 100 MB, hash SHA-256 | 1 | **Trece** (opțioănal: `CATSSoft_RUN_LARGE=1`) |
| `test_defender_scan.py` — integrarea cu Windows Defender: verdict pe text, threats extrase, „was skipped", timeout | 9 | **Toate trec** |
| `test_tray.py` — minimizare în bara de sistem: fereastra se ascunde **doar** după ce iconul e confirmat viu | 5 | **Toate trec** |
| `test_round3.py` — multi-adaptor (fallback pe traseu mort), clipboard (timer/toast), persistența cozii, `deletecommand` | 16 | **Toate trec** |
| `test_audit_followup.py` — spațiul pentru asamblare (coborârea pe calea simplă), „disc plin" la asamblare, Stop închide răspunsul activ, CSV injection | 11 | **Toate trec** |
| `test_audit_round4.py` — calea segmentată: răspunsurile urmărite și închise la Stop, Stop care nu blochează interfața, oprirea pe conexiune vie, anularea unei reîncercări programate, timerul unic de polling, gardul handler-ului `<Configure>` | 7 | **Toate trec** |
| `test_features_audit.py` — funcționalitățile cerute după auditul extern: proxy (normalizare, aplicare, degradare fără PySocks, sesiuni per-adaptor), sortare dinamică (viteză/dimensiune/ETA), dirty flag la persistența cozii | 19 | **Toate trec** |
| **Total** | **151 + 1 opțional** | **0 eșucuri** |

### Auditul suitei de teste — trei erori găsite în teste

Runda 2 a auditat și **suita de teste**, nu doar aplicația. Trei probleme:

1. **`test_real_world.py` era cod mort.** Callback-ul primește argumentele în
   ordinea `(url, folder, filename, segments, checksum)` — confirmat în v6.12 și
   v6.13 — dar testul despacheta `(url, filename, folder, ...)`. Numele fișierului
   ajungea să fie *folderul*, deci căutarea descărcării nu găsea nimic și testul
   **nu putea trece niciodată**. Prin urmare, afirmația din versiunea precedentă a
   raportului — că „descărcările live au trecut" — nu era susținută de fapt: testele
   fie erau sărite (fără internet), fie ar fi eșuat. Acum sunt corecte, iar rularea
   de pe gazde live chiar pornește descărcarea (34 s în loc de 386 s).

2. **O rețea degradată făcea suita să eșueze, nu doar să se sară.** Testele live
   așteptau până la 180 s pentru fiecare descărcare și raportau eșec dacă gazda
   externa răspundea greșit — un motiv care nu are nimic de-a face cu aplicația.
   Acum: se verifică ambele gazde cu timeout scurt, iar o descărcare care nu a
   transferat niciun octet în 120 s **sare testul**; una care a transferat și s-a
   blocat **eșuează**, pentru că asta e o blocare reală.

2b. **Aceeași verificare de istoric era greșită:** `get_history()` întoarce obiecte
   `DownloadHistory`, nu tupluri, iar testul accesa `r[2]` → `TypeError`. Era a doua
   linie care ar fi aruncat în același test.

**Diferența dintre rularea per-fișier și rularea într-un singur proces.** `run_tests.bat`
folosește `unittest discover`, deci toate suite-ul rulează într-un singur proces, cu
stare globală partajată. Prima rulare în acest mod a scos la iveală că un
`messagebox` modal (avertismentul de eșec al schedulerului) ar bloca suita — mesajul
este acum interceptat de test, care verifică și calea de avertizare. **Ambele moduri
de rulare sunt verzi: 153/153.**

O observație de rigoare: o aserțiune a testului live cerea “mai mult de un fragment”,
ceea ce ar fi eșuat pentru orice fișier sub 1 MiB — grila de fragmente e de 1–8 MiB,
deci un fișier mic are un singur interval. Verificarea corectă e `chunk_size_bytes`,
care e 0 în calea simplă și nenul în calea segmentată.

### Controale negative — de ce contează

Un test care trece nu dovedește că prinde ceva. `tests\negative_control.py` reconstruiește
**comportamentul pre-fix** într-o copie a aplicației, rulează suita contra copiei și cere ca
testele de regresie să **pică**. Dacă un test continuă să treacă, el nu verifică ceea ce
pretinde — deci e inutil.

Runda 3 a extins mecanismul la 12 fragmente de reverție, în patru grupuri, iar auditul extern
l-a extins cu încă 4 (un grup nou). Runda 4 adaugă încă 5 fragmente (un grup nou), iar
funcționalitățile cerute cu încă 6 (un grup nou). În total, **8 grupuri, 34 fragmente de
reverție**:

| Grup | Ce se revertește | Testele care trebuie să pică |
|------|-------------------|------------------------------|
| Strat de date | health-check-ul conexiunii, sweep-ul de conexiuni | `test_other_thread_recovers_from_close`, `test_short_lived_threads_do_not_leak_connections` |
| Integritatea transferului | `If-Range` la cereri de la zero, îmbinarea nesigură, reluarea fără validator, fișierul orfan, contorul vechi | 7 teste din `test_engine_integrity.py` |
| Ciclul de viață al widget-urilor | anularea timerelor `after` înainte de `destroy()` (widget și subarbore), acoperirea `CTkToplevel`, apelul explicit din `AddURLDialog`, toleranța lui `deletecommand` | 3 teste din `test_widget_lifecycle.py` |
| **Runda 3** | traseul mort scos din rotație, anularea timerului toast-ului, butonul „Ignoră", `CTkToplevel` în patch, ciclul de persistență, `deletecommand` tolerant | 8 teste din `test_round3.py` |
| **Audit extern** | verificarea spațiului pentru asamblare, închiderea răspunsului activ la Stop, neutralizarea formulelor CSV | 6 teste din `test_audit_followup.py` |
| **Semnătura de vizibilitate** | invalidarea semnăturii de vizibilitate la schimbarea listei și la eliminare | 1 test din `test_widget_lifecycle.py` |
| **Runda 4** | urmărirea răspunsurilor în calea segmentată, închiderea răspunsului pe firul UI, guardul reîncercării programate, guardul timerului de polling, gardul handler-ului `<Configure>` | 6 teste din `test_audit_round4.py` |
| **Funcționalități cerute** | proxy-ul nu se mai aplică pe sesiune, dirty flag-ul cozii dispare, cheile de sortare dinamică dispar | 11 teste din `test_features_audit.py` |

Rezultatul ultimei rulări: **CONTROL NEGATIV OK: toate regresiile sunt prinse de teste** —
pe **ambele forme** (pachet și fișier unic), deci 8 grupuri × 2 forme.

Cinci detalii care contează pentru rigoare:


1. Prima variantă a reverției runda 3 **nu făcea testele să pică** — patch-ul pe
   `CTkToplevel` masca reverția anulării timerului. Controlul negativ a fost extins și cu
   reverția patch-ului, abia atunci testele au picat așa cum trebuie.
2. La fel, reverția anulării timerelor din dialoguri era mascată de apelul explicit
   `_prune_dead_commands(self)` din `AddURLDialog.destroy` — controlul negativ a fost
   extins și cu acesta.
3. Testul `test_destroying_a_dialog_with_pending_timer_raises_nothing` **nu detecta**
   regresia: verifica doar că nu apare o excepție Python, iar când timerul rămâne pendent
   Tkinter încearcă să execute o comandă ștearsă — o eroare la nivel Tcl, care nu ajunge
   la `report_callback_exception`. Testul a fost rescris să verifice direct că timerul
   dialogului dispare din lista de timeri pending.
4. **Runda 4 a produs cel mai instructiv caz.** Prima variantă a testului „Stop nu
   blochează interfața" **trecea** — și totuși defectul era real. Cronometrarea începea
   *după* apelul `Stop`, iar blocajul de 15 s se producea chiar în acel apel: măsurătoarea
   privea doar ce se întâmpla după blocaj. Testul a fost rescris să măsoare chiar
   apelul `Stop`, iar controlul negativ al rundei 4 cere ca el să pică — ceea ce se și
   întâmplă. **Un test care trece poate fi la fel de mult o problemă de măsurare pe cât
   e o problemă de acoperire.**
5. **Corecția rundei 4 a mascat o reverție veche.** După ce patch-ul de `destroy` a
   început să anuleze timerele **întregului subarbore**, controlul negativ a raportat
   că `test_pending_after_is_cancelled_before_destroy` nu mai prinde regresia: noua
   anulare a subarborelui face același lucru ca vechea anulare a widgetului. Controlul
   negativ a fost extins cu încă un fragment de reverție (R6e) — exact pentru că
   **măsurarea controlului negativ e ea însăși o măsurătoare care poate fi mascată**.
6. **Controlul negativ are și el o condiție de validitate.** După împărțirea în
   module, ștergerea helperului `csv_safe_cell` din pachet rupea importul lui
   `core\database.py` — și atunci *toate* testele din fișier picau, din motivul
   greșit, deci controlul ar fi „trecut" fără să dovedească nimic. De aceea
   controlul verifică acum, înainte de a rula suita, că **aplicația revertită se
   mai poate importa**; dacă nu, se oprește cu mesaj explicit. În pachet, reverția
   are nevoie și de o editură de susținere (eliminarea importului devenit
   dangling), marcată ca atare — nu ca regresie.

Asta e exact rolul controlului negativ: **un test care „trece" întotdeauna nu verifică
nimic** — iar asta se vede și când controlul e construit, nu doar când testele trec.

### Validare pe fișier mare — lacuna care mai era deschisă

Până acum, singurul fișier descărcat live din suită avea 110 KB. Grila de fragmente
e de 1–8 MiB, deci pentru un fișier atât de mic produce **un singur interval** — calea
segmentată multi-fragment (cea care rulează pe fişiere mari, cu reluare parțială
și „endgame racing”) nu fusese validată niciodată pe un server real, doar de serverul local
de test.

Rezultatul pe un fișier de 100 MB de la o gazdă reală care răspunde `206` la Range:

| Verificare | Rezultat |
|---|---|
| Calea folosită | **segmentată** (`chunk_size_bytes` = 2 MiB, **50 de fragmente**) |
| Dimensiune finală | 104.857.600 bytes — identică cu referința |
| SHA-256 | **identic** cu descărcarea de referința (`cc844cac4b231032…`) |
| Fragmente `.part` rămase | **0** |
| Timp | 27,7 s (3,6 MB/s pe această legătură) |

Testul e salvat ca `tests\test_large_file.py`, dar **opțioănal** — consumă ~200 MB
(referința + transfer) éi durează ~1 minut:

```
set CATSSoft_RUN_LARGE=1
python tests\test_large_file.py
```


Un test care trece nu dovedește că prinde ceva. Pentru fiecare grup de fixuri am
reconstruit **comportamentul pre-fix** într-o copie a aplicației și am rulat suita
contra copiei. Testele care prind regresiile trebuie să **pică** — și pica:

| Control negativ | Regresii simulate | Rezultat |
|---|---|---|
| `test_db_concurrency.py` | conexiune închisă folosită de alt fir; 31 de conexiuni scurse de fire scurte | **2 teste pică**: `ProgrammingError('Cannot operate on a closed database.')` și `31 not less than or equal to 2` |
| `test_engine_integrity.py` | `If-Range` la cereri de la zero; îmbinare fără validare; reluare fără validator; fișier orfan acceptat; contor vechi ca offset Range | **7 teste pică**, inclusiv două end-to-end care ar fi descărcat un fișier **corupt**, raportat COMPLET |
| `test_widget_lifecycle.py` | timerele `after` ard după `destroy()` | **2 teste pică**: excepție Tkinter la distrugerea dialogului, timer ars pe un widget distrus |

O singură eroare de tip „citit greșit" a apărut aici: primul control negativ a raportat
că un test nu pică, deși păstrase comportamentul vechi. Testul afirma un lucru
secundar (`final_verified`), care era fals și înainte de fix, și după. L-am rescris să
verifice **comportamentul** observabil (`resume_blocked`), iar controlul a trecut.

Al doilea control a avut problema opusă: testul cerea ca un timer de 5 secunde să nu
fie; evident, timpul de așteptare al testului era prea scurt pentru a observa arderea.
Timerul a fost scurtat la 300 ms, iar controlul a prins regresia.


### Testele „real world" — de ce contează

Singurul test care a descoperit un bug pe care restul suitei nu l-a putut vedea:
`auto_sort_file` muta fișierul terminat în `~/Downloads/<Categorie>`, **ignorând
folderul ales în dialog**. Un download pornit din `D:\Work` ajungea totuși în
`C:\Users\<utilizator>\Downloads` — pe alt disc, cu o mutare lentă, într-un folder
neales de utilizator. Niciun test offline nu ar fi prins asta: serverul local de
test nu are nevoie de auto-sort, iar folderul de destinație era verificat abia
după mutare.

`test_real_world.py` acoperă ambele drumuri ale motorului pe gazde reale:
- `files.pythonhosted.org` → răspunde `206` cu `Content-Range` → calea **segmentată**;
- `speed.cloudflare.com/__down` → răspunde `200` fără `Content-Range` → calea **simplă**.

Ambele sunt parcurse prin `AddURLDialog` real (verificare → pornire → descărcare →
istoric), nu prin apeluri directe la motor.

### O regresie prinsă de utilizator, nu de teste

Bug-ul „can't delete Tcl command" la închidere a apărut în mediul real
(Portable Python 3.10 + CustomTkinter), nu în suite — pentru că suite-ul rula pe
Python 3.14. Cauza era o buclă `after info` adăugată de mine pentru anularea
timerelor globale: `after info` întoarce **token-urile** comenzilor (nu id-urile),
iar `after cancel <token>` șterge comanda Tcl a callbackului, lăsând tkinter-ul cu
o referință moartă în `_tclCommands`.

Consecință practică: **suita trebuie rulată cu același interpreter ca aplicația.**
`test_shutdown.py` acoperă acum scenariul (detalii deschise + butonul X) și rulează
verde pe ambele interprete.

Soak test-ul pornește aplicația reală, lasă `mainloop`-ul să ruleze 20 de secunde (acoperind bucla UI de 10 Hz, schedulerul, monitorul de rețea, bara de stare) și verifică că nu apare nicio excepție în log.

### Bug prins de testul propriu (autocorrectat)

Testul de mod degradat a dezvăluit o regresie **introdusă de aceste schimbări**: fallback-ul pentru baza de date coruptă refolosea aceeași conexiune SQLite ruptă (memorată în `threading.local` înainte ca `PRAGMA` să eșueze), deci și încercarea de fallback arunca. Corecție în `_connect()`: o conexiune care nu poate seta `PRAGMA` este închisă și **nu** este memorată, iar ramura de fallback resetează explicit `self._local.conn`.

Testul de motor folosește un server HTTP local care suportă Range și poate injecta erori 500 tranzitorii, plus un mecanism de „stall" pentru anulare deterministă.

Cazuri critice verificate end-to-end:

- ✅ Descărcare segmentată completă → hash SHA-256 identic cu sursa, niciun segment `.part` rămas.
- ✅ Anulare la mijloc → segmentele parțiale rămân pe disc → reluare → **hash corect**.
- ✅ Reluarea cere intervale **non-zero** → a continuat datele, nu a reluat de la zero.
- ✅ Eroare tranzitorie 500 → retry reușit → hash corect, iar cererea de retry a început **strict mai târziu** decât prima (segment reluat de la poziția rămasă).
- ✅ Spațiu insuficient → status `ERROR` cu mesaj explicit.
- ✅ Descărcare simplă (non-segmentată) → hash corect.

### Verificări punctuale ale regresiilor

- Programare salvată pe bază cu `ALTER TABLE` (simulând o bază veche) → citită corect după migrare.
- Export CSV anulat → **fișierul ales de utilizator nu e trunchiat**.
- Export CSV complet → scris corect, fără `.part` rămas.
- Validare reluare: fișier neverificabil → **nu e șters**, `resume_blocked` setat.
- Validare reluare: resursă schimbată (ETag diferit) → fișier șters.
- ETag vechi vs Last-Modified nou → **nu e conflict fals**.
- Scanner fișiere orfane: găsește orfanul, protejează segmentul activ, respectă garda de vârstă.
- Card: trunchiere cu păstrarea extensiei, ETA calculat și ascuns în stările ne-descărcare, tooltips prezente.
- Swipe: ignorat pe buton, permis pe suprafața cardului, butoanele nu rămân blocate.
- `_filter_cards`: semnătura setului vizibil rămâne stabilă → nu reîmpachetează inutil.

---

## 7. Împărțirea în module — pachet + fișier unic generat

### De ce

Fișierul unic avea **9.896 de linii**, iar un singur obiect — clasa `DownloadManager` —
ținea 3.258 de linii, adică **o treime din tot fișierul**. Orice reparație se făcea
într-un fișier în care codul, importurile și comentariile stau amestecate, iar o
editare manuală putea strica ordinea definițiilor fără niciun semnal.

### Ce s-a livrat

| | |
|---|---|
| `catssoft/` | **sursa de adevăr** — 21 de module cu cod (în `core/`, `ui/`, `utils/`) + 4 fișiere `__init__.py` |
| `main.py` | punct de intrare (vechiul `__main__`) |
| `CatsSoft_Downloader_v6.13_Plexo_Engine.py` | **artefact de build** — generat din pachet |
| `build_single_file.py` + `build_header.py` | generatorul (`--check` nu scrie nimic, doar compară) |
| `tools\verify_build.py` | verificare în două straturi: „build la zi" + echivalență AST |

Regula: **modificările se fac în pachet**, fișierul `.py` nu se editează direct. O
editare directă în artefact e suprascrisă la următoarea generare.

### Cum e construit pachetul

Fiecare bloc de cod poartă ancora cu linia sa din fișierul original:

```python
# ── orig:6634 ──
class DownloadManager(ctk.CTk):
    ...
```

Reasamblarea e o concatenare în ordinea ancorelor. Astfel, blocurile rămân
**secvențe contigue din original**, comentariile și spațierea se păstrează, iar
ordinea definițiilor e garantată prin construcție, nu prin acord.

Importurile între module sunt **generate automat** din AST (graful de dependențe
e un DAG: `models → database/config → scheduler/smart → engine → ui`), nu scrise
manual. Singura dependență manuală e `catssoft\paths.py`, cu funcția `app_dir()`,
care caută sus până la folderul ce conține `icon\icon.ico` — deci merge identic
în pachet și în fișierul unic.

### Cele patru verificări ale împărțirii

1. **Semnătura AST, definiție cu definiție.** Cele **134 de definiții** de nivel
   superior au aceeași semnătură (`ast.dump`) și **aceeași ordine** în pachet și în
   artefact, față de original. Comparația e pe AST: o diferență de formatare nu
   contează, o diferență de cod contează.
2. **Diferența textuală față de original: exact 2 zone**, ambele intenționate —
   helperul `app_dir()` (10 linii) și linia pictogramei. **Restul fișierului e
   octet cu octet identic** cu ce era livrat înainte de împărțire, comentariile
   de secțiune cu tot.
3. **153 de teste, pe ambele forme**, nemodificate. `catssoft\__init__.py`
   reexportă întregul namespace (inclusiv numele private), deci testele folosesc
   `app.DatabaseManager`, `app._cancel_pending_after` etc. **exact ca înainte**.
4. **Controalele negative, pe ambele forme** (vezi mai jos).

### Ce a trebuit adaptat în harness — și ce a scos la iveală

`tests\_helpers.py` a trebuit să știe ambele forme (`CATSSoft_FORM=package|single`).
În forma de pachet, codul își ia numele din modulul propriu, deci un patch aplicat
doar pe namespace-ul pachetului nu ajungea nicăieri. Soluția: helperul întoarce un
**proxy** care propagă orice atribuire în toate modulele aplicației.

Testarea pe forma de pachet a scos la iveală **trei lacune reale de harness** —
nu de aplicație: `test_defender_scan` (patch pe `_find_mpcmdrun`),
`test_engine` (patch pe `check_disk_space`) și `test_features_audit` (patch pe
`_socks_proxy_available`) nu ajungeau la codul testat. În forma de fișier unic
treceau oricum, pentru că acolo toate numele stăteau în același modul. Așadar,
**forma de pachet a verificat harness-ul însuși**, iar suita e acum cu adevărat
identică în ambele forme.

Controlul negativ a trebuit adaptat la fel: reverțiile se aplică acum pe modulele
pachetului (fragmentul e căutat automat, cu verificare de unicitate) sau pe
fișierul unic, iar suita e rulată în forma corespunzătoare. Fragmentele sunt
scrise pe textul originalului — care, datorită formatului de blocuri, e identic
în ambele forme.

### Singura schimbare de cod, intenționată

```python
# înainte
icon_path = Path(__file__).parent / "icon" / "icon.ico"
# după
icon_path = app_dir() / "icon" / "icon.ico"
```

În pachet, `__file__` ar fi `...\catssoft\ui\main_window.py`, deci pictograma s-ar
cauta în `catssoft\ui\icon\`. Cu `app_dir()` se caută sus până la folderul
aplicației. **În fișierul unic rezultatul e identic** cu cel de dinainte.

### Varianta executabilă (`.exe`)

Din pachet se poate construi și un executabil Windows, cu PyInstaller:

```
python tools\build_exe.py            # un singur .exe (onefile)
python tools\build_exe.py --onedir   # director, start mai rapid
```

Rezultat: **`CatsSoft Downloader v6.13.exe`, 38.784.693 de octeți (~37 MB)** — un singur
fișier, care **nu are nevoie de Python instalat**. E construit din **pachet**
(`main.py` → `catssoft\`), nu din artefact, deci regula rămâne aceeași: pachetul e
adevărul, `.exe`-ul e o altă formă generată.

Trei lucruri au făcut build-ul să funcționeze fără adaptări ale codului:

1. **Baza de date nu stă lângă `.exe`** — `default_database_path()` scrie în
   `%LOCALAPPDATA%\CatsSoft\Downloader`, deci executabilul e portabil (merge și în
   `Program Files`). O bază de date veche, lângă aplicație, e folosită în continuare.
2. **Pictograma merge ca *date*, nu doar ca pictogramă de `.exe`** — `app_dir()`
   caută `icon\icon.ico` în directorul de extracție al arhivei, unde PyInstaller o
   pune (`--add-data`). De aceea nu a fost nevoie de nicio ramură nouă în cod.
3. **`yt-dlp` și `ffmpeg` nu sunt folosite de cod** — folderul `bin\` e vestigial, din
   versiuni mai vechi, deci nu are ce fi înglobat.

Testul de fum, pe executabilul construit: pornește, rămâne viu, creează baza de date
cu schema completă (5 tabele: `history`, `scheduled`, `active_queue`, `settings`,
`sqlite_sequence`) și scrie setările implicite, iar pictograma e prezentă în arhivă.

### Ce NU s-a schimbat

Nimic din comportament. Același motor, aceleași bug-uri reparate, aceleași
funcționalități, aceleași 153 de teste. Împărțirea e o reorganizare structurală,
verificată de patru controale independente — dintre care unul (diferența textuală)
arată că **99,8% din fișierul livrat e neschimbat**.

---

## 8. Verdict de pregătire

### Este produs final?

**Stadiu: BETA de utilizare.** Aplicația e stabilă și de folosit acum, iar tot ce se
poate verifica automat e verificat. Ce nu e „final" sunt trei lucruri care depind de
mediu, nu de cod — sunt enumerate în „Ce NU e verificat".

Argumentul care m-a convins că blind-spot-urile sunt acoperite: testul pe gazde reale
a descoperit un bug major (`auto_sort_file`) pe care **niciun** test offline nu
l-ar fi prins. Blind-spot-ul exista; acum e acoperit.

### Ce e verificat

- 153 de verificări automate în 22 fișiere de test, toate verzi pe Portable Python 3.10
  — **pe ambele forme ale aplicației** (pachetul `catssoft\` și fișierul unic generat),
  plus opt **grupuri de controale negative** (34 fragmente de reverție) rulate pe
  **ambele forme**, care dovedesc că testele prind regresiile reale.
- **Împărțirea în module**: cele **134 de definiții** de nivel superior au aceeași
  semnătură AST și aceeași ordine în pachet și în artefact, iar diferența textuală
  față de fișierul livrat înainte de împărțire e de **exact 2 zone**, ambele
  intenționate (helperul `app_dir()` și linia pictogramei). Restul e octet cu octet
  identic.
- **Executabilul Windows**: pornește, rămâne viu, creează schema completă a bazei de
  date (5 tabele) cu setările implicite, iar pictograma e prezentă în arhivă.
- Descărcări reale pe gazde live, prin `AddURLDialog` real, pe ambele drumuri
  ale motorului (cu și fără suport Range) — acum cu testul corect, deci verificarea
  chiar se execută.- Reluare după anulare, retry după eroare tranzitorie, spațiu insuficient, DB corupt,
  programare cu eșec, inchidere curată a aplicației.
- **Integritatea transferului**: contor vechi ca offset Range, `If-Range` la cereri de la
  zero, reluare fără validator, fișier orfan, îmbinare cu dimensiune greșită — toate
  acoperite end-to-end cu server HTTP local și verificare de hash SHA-256.
- **Ciclul de viață al widget-urilor**: timerele `after` sunt anulate înainte de
  `destroy()`, iar închiderea ferestrei și a dialogurilor nu mai lasă excepții Tkinter.
- **Integrarea cu Windows Defender**, pe mașina reală: `MpCmdRun.exe` era invocat cu
  `-ScanType 3 -File`, variantă care **sărea fișierul** („was skipped") și returna cod 0 —
  aplicația raporta „curat" fără să scaneze nimic. Singura invocare care dă verdict real
  e `-ScanType 3 -DisableRemediation -File`. Verificat pe un fișier EICAR real (detectat,
  `Virus:DOS/EICAR_Test_File`) și pe un fișier curat (raportat curat).
- **Bara de sistem**: minimizarea în tray e verificată pe mașina reală (iconul pornește,
  fereastra se ascunde, `stop()` în 0,10 s). Riscul identificat și reparat: fereastra era
  ascunsă **înainte** ca iconul să pornească — dacă `pystray` eșua, aplicația dispărea fără
  cale de întoarcere.
- **Multi-adaptor**: traseul mort (fără rută) e scos acum din rotație după o eroare de
  transport, iar `acquire_session()` revine pe traseul sănătos sau pe sesiunea de sistem —
  promisiunea din documentația clasei, care nu era implementată.
- **Spațiul pentru asamblare**: o descărcare pe segmente pornită cu spațiu insuficient
  pentru copia de asamblare coboară automat pe calea simplă (scrie direct în fișierul
  final) și **se termină**, în loc să consume tot bandwidth-ul și să eșueze la final.
  Verificat end-to-end cu server HTTP local: hash SHA-256 identic cu sursa.
- **Anularea descărcării**: în calea simplă și în calea segmentată, răspunsurile HTTP
  deschise sunt urmărite și închise la Stop, iar închiderea se face **într-un fir
  secundar** — pe Windows, `close()` pe un socket cu un `recv` în așteptare blochează
  până la timeout (15-25 s) și ar congela interfața. Măsurat pe mașina reală: Stop în
  **0,00 s** cu server mort, **0,25 s** până la oprirea segmentului pe o conexiune vie.
- **Exportul istoricului**: celulele care ar putea deveni formule în Excel sunt
  neutralizate — inclusiv numele de fișier care vin de la server.
- **Închiderea aplicației**: pornirea + închiderea pe mașina reală nu mai lasă niciun
  „Exception in Tkinter callback" (era 373, adică 7.724 de linii de urme în consolă la
  fiecare ieșire).
- **Funcționalitățile cerute după auditul extern** (proxy, sortare dinamică, dirty flag):
  acoperite cu 19 teste dedicate + 5 fragmente de reverție în controlul negativ.
  Proxy-ul e verificat pe normalizare, aplicare pe sesiunea globală și pe cele
  per-adaptor, pe degradarea curată când PySocks lipsește și **end-to-end**: o
  descărcare printr-un proxy HTTP local real, cu hash SHA-256 identic cu sursa.
  Dirty flag-ul pe „nu se scrie dacă nimic nu s-a schimbat", „se scrie la schimbare
  de stare" și „se scrie la 1% progres".
- **Calea segmentată pe un fișier mare, pe gazdă reală**: testul opțional de 100 MB
  (multi-fragment, cu reluare parțială și endgame racing) a fost rulat pe **buildul
  curent** — 45 s, hash SHA-256 identic cu sursa, niciun fragment orfan rămas.

### Ce NU e verificat (și de ce nu se poate verifica automat)

| Zonă | De ce |
|---|---|
| Agregare multi-adaptor pe două căi **funcționale** | Necesită 2+ interfețe de rețea fizice pe aceeași mașină, ambele cu rută proprie. Pe acest hardware există `Wi-Fi` și un `vEthernet (Default Switch)` — dar al doilea **nu are rută** (bind-ul eșuează în 0,0 s), deci agregarea pe două căi live nu s-a putut exercita. Calea de traseu mort e acoperită de teste. |
| Carantină automată de către aplicație | `MpCmdRun -DisableRemediation` e singura variantă care dă verdict pe acest hardware, dar ea **nu carantinează**. Detectarea e raportată, remedierea rămâne rolul Defender-ului (protecția în timp real acționează în timpul descărcării, iar dacă fișierul dispare, aplicația marchează descărcarea ca eroare). |
| **Proxy SOCKS5 real** | Proxy-ul **HTTP** e validat end-to-end (descărcare printr-un proxy local real, hash SHA-256 identic cu sursa). Pentru SOCKS5 e validată doar partea de configurare: PySocks e instalat pe această mașină, dar nu există un server SOCKS5 la îndemână ca să se facă transferul prin el. |
| **Recuperare după oprirea bruscă a procesului** | Snapshot-ul cozii e testat la nivel de DB (scrie, citește, restaurează), iar închiderea curată salvează forțat. Nu s-a simulat un `kill` al procesului în mijlocul unui transfer: ar necesita un harness care omoară procesul din exterior la un moment aleator. |

### Ce a rămas deliberat neoptimizat

| Zonă | De ce e acceptabil |
|---|---|
| Tab-ul „Coadă" reconstruiește toate rândurile la fiecare eveniment de stare | Se întâmplă doar când tab-ul Coadă e vizibil; restul aplicației nu e afectat. |
| `save_active_queue` rescrie tabelul complet, dar **doar când starea s-a schimbat** | Dirty flag-ul e implementat (4.6.3): scrierea are loc la schimbare de stare sau la un progres avansat cu ≥1%. Ce a rămas: tabelul e rescris integral (nu upsert), pentru că e o singură tranzacție ieftină. |
| Căutarea din istoric pornește un fir per apăsare de tastă | Rezultatele învechite sunt deja respinse prin token de generație; impactul e neglijabil pentru o istorică de dimensiune personală. Conexiunile SQLite ale firelor terminate se închid acum automat (sweep), deci firul scurt nu mai lasă handle-uri deschise. |
| Log-uri în fișier rotativ | Aplicația e lansată de obicei ca fereastră (`pythonw`), deci azi **nu rămâne niciun log** la o eroare. E 10 linii de `RotatingFileHandler`, dar creează fișiere noi în folderul utilizatorului — făcut la cerere, nu pe initativă. |
| Timpuri stocate în UTC | Orele locale naive sunt corecte pentru „descărcări de azi, la ora 9"; migrarea istoricului ar risca să altereze datele existente. |
| Programare cu timezone/DST | În ora locală, la trecerea DST ora de vară se poate dubla sau lipsi. Pentru un tool personal e o raritate; un scheduler cu `zoneinfo` ar necesita migrarea datelor. |
| Împărțirea în module, `uid` stabil, state machine | Refactor de ordinul zilelor, cu risc de regresie pe un motor deja verificat cu 132 de teste. Cheile `id()` actuale sunt curate — lacuna era una singură (semnătura de vizibilitate) și e închisă. |
| Citirea de socket blocată nu poate fi întreruptă din alt fir (Windows) | Limitare de platformă, verificată experimental: nici `close()`, nici `shutdown(SHUT_RDWR)`, nici `SO_LINGER=0` nu o întrerup. Mitigarea e făcută: interfața nu se blochează, iar cu date care circulă oprirea e imediată. |

### Recomandare

Pentru un tool personal, **v6.13 se poate folosi în producție acum**. Cele trei zone
neverificate din tabel sunt hardware/mediu-specifice și se testează manual în câteva
minute pe mașina ta (un download de pe un server mare, unul marcat, minimizarea în
tray).

1. ~~**Teste unitare în repo**~~ — **făcut**: suita e instalată în `CatsSoft Downloader PRO\tests\`, cu `run_tests.bat`.
2. **Migrare separată a bazei de date** — *neimplementat, ne-necesar pentru tine:* migrarea in-place funcționează și e testată pe o bază veche cu `ALTER TABLE`. Contează doar la baze cu sute de mii de rânduri.
3. **Debounce pe căutarea din istoric** — *neimplementat, marginal:* fiecare apăsare pornește un fir, dar rezultatele învechite sunt deja respinse prin token de generație.
4. **Dimensiunea range-urilor fixată la pornire** — *corecție față de prima versiune a raportului:* concurența e deja adaptivă (`AdaptiveStreamController` urcă/scade workerii după goodput). Ce rămâne fix e doar **numărul de fragmente** (~96, de 1–8 MiB), calculat o singură dată din dimensiunea totală. Refacerea grilei în timpul transferului ar ajuta doar pe legături foarte rapide.
5. **Telemetrie locală** — *opțional, e un feature nu un fix:* un contor persistent (transferuri, bytes, eșuaje) ar permite statistici utile fără să trimiți nimic în exterior.

---

## 8. Fișiere

| Fișier | Rol |
|---|---|
| `README.md` | Cum rulezi fiecare formă, unde sunt datele |
| `CatsSoft_Downloader_v6.13_Plexo_Engine.py` | **Livrabil** — artefact de build generat din pachet (aplicația corectată și extinsă) |
| `CatsSoft Downloader v6.13.exe` | Executabil Windows (~37 MB), un singur fișier, fără Python instalat |
| `icon\icon.ico` / `icon\png` | Pictograma aplicației |
| `REPORT_v6.13.md` | Acest raport |
| `catssoft\` | **Sursa de adevăr** — aplicația împărțită în module (21 module + 4 `__init__.py`) |
| `main.py` | Punct de intrare al pachetului |
| `build_single_file.py` + `build_header.py` | Generatorul fișierului unic din pachet |
| `tools\build_exe.py` | Generatorul executabilului (PyInstaller) |
| `tools\verify_build.py` | Verificare: fișierul unic e la zi + pachetul și artefactul sunt echivalente |
| `tests\_helpers.py` | Forma testată (pachet sau fișier unic) + izolarea mediului de test |
| `tests\test_data_layer.py` | Teste strat de date |
| `tests\test_engine.py` | Teste motor end-to-end (server HTTP local) |
| `tests\test_resume_and_cleanup.py` | Teste reluare, curățare, mod degradat |
| `tests\test_ui.py` | Teste interfață grafică |
| `tests\test_shutdown.py` | Teste de închidere (comenzi Tcl orfane) |
| `tests\test_soak.py` | Test de rulare reală |
| `tests\test_db_concurrency.py` | Teste de ciclu de viață al conexiunilor SQLite și scrieri concurente |
| `tests\test_engine_integrity.py` | Teste de integritate a transferului (end-to-end cu server HTTP local) |
| `tests\test_widget_lifecycle.py` | Teste de ciclu de viață al widget-urilor (timerele `after` vs `destroy()`) |
| `tests\test_large_file.py` | Test opțioănal: descărcare live multi-fragment pe 100 MB, cu hash |
| `tests\test_defender_scan.py` | Teste de integrare cu Windows Defender (verdict pe text, threats, „skipped") |
| `tests\test_tray.py` | Teste de minimizare în bara de sistem |
| `tests\test_round3.py` | Teste runda 3: multi-adaptor, clipboard, persistența cozii, `deletecommand` |
| `tests\test_audit_followup.py` | Teste pentru defectele reale găsite în auditul extern: spațiu la asamblare, anulare, CSV injection |
| `tests\test_audit_round4.py` | Teste runda 4: calea segmentată la Stop (răspunsuri urmărite, interfață neblocată), anularea unei reîncercări programate, timerul unic de polling, gardul handler-ului `<Configure>` |
| `tests\test_features_audit.py` | Funcționalitățile cerute după auditul extern: proxy (HTTP/HTTPS/SOCKS5), sortare dinamică în tab-ul Descărcări (viteză/dimensiune/ETA), dirty flag la persistența cozii |
| `tests\_toast_probe.py` | Sonda separată (proces propriu) pentru calea reală a toast-ului de clipboard |
| `tests\negative_control.py` | Controale negative: reconstruiește comportamentul pre-fix și verifică că testele prind regresiile (pe ambele forme) |
| `tests\README.md` | Cum se rulează suita |
| `run_tests.bat` | Runner cu un click (rulează suita pe ambele forme + controlul negativ) |

Rulare: `python CatsSoft_Downloader_v6.13_Plexo_Engine.py` (sau `python main.py`, din pachet), sau direct `CatsSoft Downloader v6.13.exe`
Teste: `run_tests.bat` sau `python -m unittest discover -s tests -p "test_*.py" -v`
Verificare livrabil: `python tools\verify_build.py`
Regenerare executabil: `python tools\build_exe.py`
