"""Controale negative - dovedesc că testele prind regresiile, nu doar că trec.

Un test care trece nu dovedește că prinde ceva. Scriptul acesta reconstruiește
**comportamentul pre-fix** într-o copie a aplicației, rulează suita contra copiei
și cere ca testele de regresie să **pică**. Dacă un test continuă să treacă, înseamnă
că nu verifică ceea ce pretinde - deci e un test inutil.

Rulare (din folderul proiectului):
    python tests\\negative_control.py

Ieșire așteptată:
    CONTROL NEGATIV OK: regresiile sunt prinse de teste.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Consolele Windows folosesc cp1250/cp1252, care nu conțin litera „ț" (U+021B):
# fără reconfigurare, afișarea etichetelor românești ar opri scriptul.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
APP_NAME = "CatsSoft_Downloader_v6.13_Plexo_Engine.py"

# ── Controalele ──────────────────────────────────────────────────────────────
# Fiecare control: (nume, [(fragment vechi, fragment nou)], teste care trebuie să pică)

DB_REVERTS = [
    ("health-check: conexiune închisă",
     "        conn = getattr(self._local, \"conn\", None)\n"
     "        if conn is not None and self._connection_is_dead(conn):",
     "        conn = getattr(self._local, \"conn\", None)\n"
     "        if False:"),
    ("sweep: conexiuni fire terminate",
     "        # Firele scurte (căutări/exporturi în istoric) lasă în urmă handle-uri\n"
     "        # deschise; le închidem pe cele ale firurilor deja terminate.\n"
     "        self._sweep_dead_connections()\n",
     ""),
]

DB_EXPECTED = {
    "test_other_thread_recovers_from_close",
    "test_short_lived_threads_do_not_leak_connections",
}

ENGINE_REVERTS = [
    ("R1 If-Range trimis și la cereri de la zero",
     '            if resume_from > 0 and getattr(dl, "resume_validator", ""):',
     '            if getattr(dl, "resume_validator", ""):'),
    ("R2 îmbinare fără validarea dimensiunii",
     """        if expected_total:
            merged_size = os.path.getsize(temp_path)
            if merged_size != int(expected_total):
                raise IOError(f"Îmbinare incompletă: {merged_size}/{int(expected_total)} bytes")
""",
     ""),
    ("R3 reluare fără validator",
     """    restarted_without_validator = False
    if not validator and allow_restart_without_validator and _has_resume_artifacts(dl.save_path):""",
     """    restarted_without_validator = False
    if False and not validator and allow_restart_without_validator and _has_resume_artifacts(dl.save_path):"""),
    ("R4 fișier orfan acceptat ca complet",
     "                orphaned_final = (not old) or total <= 0 or existing_final_size != total",
     "                orphaned_final = (not old) and (total <= 0 or existing_final_size != total)"),
    ("R5 contor vechi folosit ca offset Range",
     """                on_disk = _reconcile_downloaded_with_disk(dl)
                if on_disk > 0:
                    logger.info(f"Reluare download simplu de la {on_disk} bytes (fișier existent)")""",
     """                if (dl.downloaded == 0 and dl.supports_resume and not dl.resume_blocked
                        and os.path.exists(dl.save_path)):
                    on_disk = os.path.getsize(dl.save_path)
                    if on_disk > 0:
                        with dl.lock:
                            dl.downloaded = on_disk"""),
]

ENGINE_EXPECTED = {
    "test_if_range_is_not_sent_for_a_fresh_segment",
    "test_http_200_for_a_range_request_is_not_reported_as_resource_change",
    "test_merge_keeps_sources_when_size_does_not_match",
    "test_missing_validator_restarts_without_resume",
    "test_orphan_file_of_same_size_is_not_verified",
    "test_stale_counter_larger_than_file_does_not_corrupt_the_result",
    "test_stale_counter_smaller_than_file_does_not_corrupt_the_result",
}

WIDGET_REVERTS = [
    ("R6 timerele after pendente ard după destroy",
     "    _cancel_pending_after(widget)\n",
     ""),
    ("R6b CTkToplevel nu mai este acoperit de patch-ul de destroy",
     """    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None),
               getattr(ctk, "CTkToplevel", None)]""",
     """    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None)]"""),
    ("R6c dialogul AddURL nu mai anulează timerele proprii",
     """    def destroy(self):
        THEME.unregister_window(self)
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()

# ─── Resume Queue Dialog (FEATURE NOU) ─""",
     """    def destroy(self):
        THEME.unregister_window(self)
        THEME.remove_listener(self)
        super().destroy()

# ─── Resume Queue Dialog (FEATURE NOU) ─"""),
    ("R6d deletecommand nu mai tolerează widgeturi distruse",
     "_patch_customtkinter_destroy()\n_patch_tkinter_deletecommand()\n",
     "_patch_customtkinter_destroy()\n"),
    ("R6e timerele subarborelui nu mai sunt anulate",
     "            _cancel_pending_after_tree(self)\n",
     ""),
]

WIDGET_EXPECTED = {
    "test_destroying_a_dialog_with_pending_timer_raises_nothing",
    "test_pending_after_is_cancelled_before_destroy",
}

ROUND3_REVERTS = [
    ("R7 adaptorul mort rămâne în rotație după cooldown",
     """                if profile["consecutive_failures"] >= self.ERROR_DISABLE_THRESHOLD:
                    if profile.get("available"):
                        logger.warning("Network path %s removed from rotation after %s failed transfer(s): %s",
                                       key, profile["consecutive_failures"], exc)
                    profile["available"] = False
                    profile["cooldown_until"] = 0.0
""",
     "                pass\n"),
    ("R8 timerul toast-ului nu este anulat la închidere",
     """        toast = self._toast_window
        if toast is not None and self._toast_close_id:
            try:
                toast.after_cancel(self._toast_close_id)
            except Exception:
                pass
        self._toast_close_id = None
        if toast is not None:
""",
     """        toast = self._toast_window
        if toast is not None:
"""),
    ("R9 butonul Ignoră lasă timerul pendent",
     """        ctk.CTkButton(btn_frame, text="Ignoră", width=100, fg_color=BG_INPUT(),
                     command=close_toast).pack(side="left", padx=5)""",
     """        ctk.CTkButton(btn_frame, text="Ignoră", width=100, fg_color=BG_INPUT(),
                     command=lambda: [toast.destroy(), self._reset_notification()]).pack(side="left", padx=5)"""),
    ("R10 ciclul de persistență nu pornește la excepția dialogului",
     """        try:
            if entries:
                dlg = ResumeQueueDialog(self, entries)
                self.wait_window(dlg)
        except Exception:
            logger.exception("Dialogul de reluare a eșuat")
        finally:
            # Ciclul de persistență trebuie pornit ORIUNE, chiar dacă dialogul a
            # aruncat o excepție: altfel coada activă nu mai este salvată niciodată
            # în sesiunea curentă, iar reluarea la pornire se pierde definitiv.
            self._persist_active_queue()""",
     """        if entries:
            dlg = ResumeQueueDialog(self, entries)
            self.wait_window(dlg)
        self._persist_active_queue()"""),
    ("R11 deletecommand aruncă pe un widget distrus",
     "_patch_customtkinter_destroy()\n_patch_tkinter_deletecommand()\n",
     "_patch_customtkinter_destroy()\n"),
    ("R12 CTkToplevel nu mai este acoperit de patch-ul de destroy",
     """    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None),
               getattr(ctk, "CTkToplevel", None)]""",
     """    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None)]"""),
]

AUDIT_REVERTS = [
    ("R13 nu se verifică spațiul pentru asamblare",
     """            if attempt == 0 and total > 0 and not _has_resume_artifacts(dl.save_path):
                if not _merge_space_available(dl.save_path, total):
                    logger.info("Spațiu insuficient pentru asamblare (%s) - descărcare simplă",
                                dl.filename)
                    return download_simple(dl, update_q, max_retries)
""",
     ""),
    ("R14 Stop nu închide răspunsul HTTP activ",
     """        response = getattr(dl, "active_response", None)
        if response is not None:
            _close_response_async(response)
""",
     ""),
    ("R15 exportul istoricului scrie celulele brute",
     "writer.writerows([[csv_safe_cell(cell) for cell in row] for row in rows])",
     "writer.writerows(rows)"),
    ("R16 helperul de neutralizare CSV nu mai există",
     '''_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\\t", "\\r")


def csv_safe_cell(value) -> str:
    """Neutralizează formulele CSV (CSV injection).

    Excel și LibreOffice interpretează o celulă care începe cu ``=``, ``+``,
    ``-`` sau ``@`` ca formulă. Numele fișierului poate veni de la server
    (Content-Disposition), deci un server malițios ar putea injecta o formulă
    în exportul istoricului deschis de utilizator.

    Prefixul ``'`` transformă celula în text simplu în Excel și rămâne
    lizibilă în orice alt cititor de CSV.
    """
    if value is None:
        return ""
    text = str(value)
    if text[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text
''',
     ""),
]

AUDIT_EXPECTED = {
    "test_falls_back_to_simple_path_when_merge_would_not_fit",
    "test_stop_download_closes_the_active_response",
    "test_export_neutralizes_a_formula_in_the_filename",
    "test_formula_prefixes_are_neutralized",
    "test_none_becomes_empty_string",
    "test_ordinary_values_are_untouched",
}

# Edituri de susținere, aplicate DOAR în forma de pachet: nu sunt regresii, ci
# adaptări necesare pentru ca pachetul să rămână importabil după reverție.
# În fișierul unic importurile între module nu există, deci nu e nevoie de ele.
AUDIT_SUPPORT_REVERTS = [
    ("R16s importul helperului CSV eliminat (doar pachet)",
     "from catssoft.utils.files import csv_safe_cell, default_database_path",
     "from catssoft.utils.files import default_database_path"),
]

WIDGET2_REVERTS = [
    ("R17 semnătura de vizibilitate nu mai este invalidată",
     """        # Card nou => setul vizibil s-a schimbut: invalidăm semnătura, ca o
        # eventuală reutilizare de id() să nu lase o stare veche.
        self._visible_signature = None
""",
     ""),
    ("R18 semnătura de vizibilitate nu mai este invalidată la eliminare",
     """        # Semnătura vizibilității e construită din id()-uri, iar CPython poate
        # refolosi un id pentru un obiect nou. Invalidarea ei la orice schimbare
        # structurală garantează că reîmpachetarea se face corect.
        self._visible_signature = None
""",
     ""),
]

WIDGET2_EXPECTED = {
    "test_visible_signature_is_invalidated_when_the_list_changes",
}

ROUND4_REVERTS = [
    ("R19 calea segmentată nu mai urmărește răspunsurile deschise",
     """                _track_active_response(dl, resp)
                tracked_response = resp
""",
     ""),
    ("R20 Stop închide răspunsul pe firul UI (congelează interfața)",
     """        response = getattr(dl, "active_response", None)
        if response is not None:
            _close_response_async(response)
""",
     """        response = getattr(dl, "active_response", None)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
"""),
    ("R21 guardul reîncercării programate redevine mort",
     "            if (previous_status == DLStatus.QUEUED and had_scheduled_retry) or not dl.thread or not dl.thread.is_alive():",
     "            if (previous_status == DLStatus.QUEUED and getattr(dl, \"auto_retry_at\", 0.0)) or not dl.thread or not dl.thread.is_alive():"),
    ("R22 _poll_updates nu mai anulează timerul pendent",
     """                if pending_job is not None:
                    try:
                        self.after_cancel(pending_job)
                    except tk.TclError:
                        pass
""",
     """                if getattr(self, "_poll_job_id", None) is not None:
                    self.after_cancel(self._poll_job_id)
"""),
    ("R23 handler-ul Configure nu mai e păzuit",
     "_patch_customtkinter_destroy()\n_patch_tkinter_deletecommand()\n_patch_customtkinter_configure_handler()\n",
     "_patch_customtkinter_destroy()\n_patch_tkinter_deletecommand()\n"),
]

ROUND4_EXPECTED = {
    "test_stop_download_interrupts_a_live_segment",
    "test_stop_download_does_not_block_the_ui_on_a_stalled_server",
    "test_cancel_of_a_scheduled_retry_is_queued_as_an_event",
    "test_repeated_poll_calls_keep_a_single_timer",
    "test_update_dimensions_event_is_guarded",
    "test_update_dimensions_event_with_a_dead_canvas_does_nothing",
}

FEATURE_REVERTS = [
    ("R24 proxy-ul nu se mai aplică pe sesiune",
     """    try:
        session.proxies.clear()
        session.proxies.update({"http": normalized, "https": normalized})
    except Exception as exc:
        logger.warning("Proxy nu a putut fi aplicat: %s", exc)
        return False
""",
     """    try:
        session.proxies.clear()
    except Exception as exc:
        logger.warning("Proxy nu a putut fi aplicat: %s", exc)
        return False
"""),
    ("R25 dirty flag-ul cozii dispare",
     """            signature = self._queue_snapshot_signature()
            if signature != getattr(self, "_queue_snapshot_state", None):
                self.db.save_active_queue(self.downloads)
                self._queue_snapshot_state = signature
""",
     """            self.db.save_active_queue(self.downloads)
"""),
    ("R26 cheile de sortare dinamică dispar",
     """    @staticmethod
    def _download_speed_value(dl: Download) -> float:""",
     """    @staticmethod
    def _download_speed_value_disabled(dl: Download) -> float:"""),
    ("R26b cheile de sortare dinamică dispar (ETA)",
     """    @staticmethod
    def _download_eta_key(dl: Download):""",
     """    @staticmethod
    def _download_eta_key_disabled(dl: Download):"""),
    ("R26c cheile de sortare dinamică dispar (dimensiune)",
     """    @staticmethod
    def _download_total_value(dl: Download) -> int:""",
     """    @staticmethod
    def _download_total_value_disabled(dl: Download) -> int:"""),
]

FEATURE_EXPECTED = {
    "test_apply_proxy_sets_and_clears_the_session",
    "test_adapter_sessions_receive_the_proxy",
    "test_manager_apply_proxy_updates_existing_sessions",
    "test_download_really_goes_through_the_proxy",
    "test_unchanged_queue_is_not_rewritten",
    "test_tiny_progress_change_does_not_trigger_a_write",
    "test_forced_snapshot_always_writes",
    "test_speed_sort_puts_the_fastest_first",
    "test_size_sort_puts_the_largest_first",
    "test_eta_sort_puts_the_soonest_first_and_unknown_last",
    "test_eta_key_marks_downloads_without_eta",
}

ROUND3_EXPECTED = {
    "test_dead_adapter_is_removed_from_rotation",
    "test_dead_adapter_stays_out_of_rotation_after_the_cooldown",
    "test_healthy_adapter_is_preferred_after_the_cooldown_expires",
    "test_refresh_revalidates_a_removed_adapter",
    "test_close_toast_cancels_the_pending_timer",
    "test_stop_cancels_the_toast_timer_and_resets_state",
    "test_ignore_button_cancels_timer_and_second_toast_survives",
    "test_persist_loop_starts_even_if_the_resume_dialog_fails",
}


def _flatten(text):
    """Elimină marcajele de bloc, lăsând textul originalului continuu."""
    return re.sub(r"(?m)^# ── orig:\d+ ──\n", "", text)


def _build_copy(reverts, workdir, form):
    """Create a copy of the app with the pre-fix behaviour restored.

    Funcționează pentru ambele forme ale aplicației:

    * ``package`` - pachetul (sursa de adevăr) e copiat integral, iar reverțiile
      se aplică pe fișierul-modul în care se află fragmentul - fragmentul e
      căutat automat, deci nu trebuie întreținută o mapare manuală între
      fragmente și module.
    * ``single`` - reverțiile se aplică pe fișierul unic generat.

    În ambele forme textul e identic cu originalul (blocurile pachetului sunt
    secvențe contigue din el), deci aceleași fragmente descriu aceleași
    regresii în ambele forme.
    """
    target_dir = os.path.join(workdir, "app")
    os.makedirs(target_dir, exist_ok=True)
    package_dir = os.path.join(PROJECT, "catssoft")
    has_package = os.path.isdir(package_dir)
    single_source = os.path.join(PROJECT, APP_NAME)

    if has_package:
        shutil.copytree(package_dir, os.path.join(target_dir, "catssoft"))
    if os.path.isfile(single_source):
        shutil.copy2(single_source, os.path.join(target_dir, APP_NAME))

    if form == "package":
        if not has_package:
            raise SystemExit("Forma 'package' cere pachetul catssoft/")
        module_files = []
        for directory, _dirs, files in os.walk(os.path.join(target_dir, "catssoft")):
            for name in sorted(files):
                if name.endswith(".py"):
                    module_files.append(os.path.join(directory, name))
        # marcajele de bloc separă textul originalului; fără ele, fragmentele
        # (scrise pe textul originalului) se potrivesc și în pachet.
        for path in module_files:
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
            flattened = _flatten(content)
            if flattened != content:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(flattened)
        for label, old, new in reverts:
            matches = []
            for path in module_files:
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read()
                if old in content:
                    matches.append((path, content))
            if not matches:
                raise SystemExit("Fragment negăsit pentru controlul negativ: %s" % label)
            if len(matches) > 1:
                raise SystemExit("Fragment ambiguu pentru controlul negativ: %s (%d module)"
                                 % (label, len(matches)))
            path, content = matches[0]
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content.replace(old, new, 1))
    else:
        target = os.path.join(target_dir, APP_NAME)
        with open(target, "r", encoding="utf-8") as handle:
            source = handle.read()
        for label, old, new in reverts:
            if old not in source:
                raise SystemExit("Fragment negăsit pentru controlul negativ: %s" % label)
            source = source.replace(old, new, 1)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(source)

    tests_dir = os.path.join(target_dir, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    for name in os.listdir(os.path.join(PROJECT, "tests")):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(PROJECT, "tests", name),
                         os.path.join(tests_dir, name))
    return target_dir


def _check_imports(app_dir, form):
    """Verifică că aplicația revertită se mai poate importa.

    Dacă o reverție rupe importul, testele pică din motivul greșit - și controlul
    ar „trece" fără să dovedească nimic. De aceea e o condiție separată.
    """
    if form == "package":
        code = "import sys; sys.path.insert(0, %r); import catssoft" % app_dir
    else:
        code = ("import importlib.util;"
                "spec = importlib.util.spec_from_file_location('app', %r);"
                "module = importlib.util.module_from_spec(spec);"
                "spec.loader.exec_module(module)"
                % os.path.join(app_dir, APP_NAME))
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", cwd=app_dir)
    return result.returncode == 0, (result.stdout + result.stderr)


def _run_control(label, test_file, reverts, expected, form, support=None):
    print("=" * 70)
    print("CONTROL NEGATIV [%s]: %s" % (form, label))
    print("=" * 70)
    workdir = tempfile.mkdtemp(prefix="catssoft_negctl_")
    try:
        if support and form == "package":
            reverts = list(reverts) + list(support)
        app_dir = _build_copy(reverts, workdir, form)

        ok, output = _check_imports(app_dir, form)
        if not ok:
            print("\n  FAILED: aplicația revertită nu se mai poate importa:")
            print("  " + output.strip().replace("\n", "\n  "))
            print("  -> controlul ar fi invalid: testele pică din motivul greșit.")
            return 1

        env = dict(os.environ)
        env["CATSSoft_FORM"] = form
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, os.path.join(app_dir, "tests", test_file)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=app_dir, env=env)
        combined = result.stdout + "\n" + result.stderr
        failed = set(re.findall(r"^(?:FAIL|ERROR): (\S+)", combined, re.M))
        for name in sorted(failed):
            print("   a picat (corect):", name)
        missing = expected - failed
        if missing:
            print("\n  FAILED: nu au picat:", sorted(missing))
            print("  -> testele NU prind regresia; trebuie rescrise.")
            return 1
        unexpected = failed - expected
        if unexpected:
            print("\n  ATENȚIE - au picat și teste neașteptate:", sorted(unexpected))
        print("\n  OK: regresiile sunt prinse de teste.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


CONTROLS = [
    ("stratul de date", "test_db_concurrency.py", "DB_REVERTS", "DB_EXPECTED", None),
    ("integritatea transferului", "test_engine_integrity.py", "ENGINE_REVERTS", "ENGINE_EXPECTED", None),
    ("ciclul de viață al widget-urilor", "test_widget_lifecycle.py", "WIDGET_REVERTS", "WIDGET_EXPECTED", None),
    ("runda 3 de audit", "test_round3.py", "ROUND3_REVERTS", "ROUND3_EXPECTED", None),
    ("audit extern - următoarele defecte", "test_audit_followup.py", "AUDIT_REVERTS", "AUDIT_EXPECTED", "AUDIT_SUPPORT_REVERTS"),
    ("semnătura de vizibilitate", "test_widget_lifecycle.py", "WIDGET2_REVERTS", "WIDGET2_EXPECTED", None),
    ("runda 4 de audit - coadă/UI și calea segmentată", "test_audit_round4.py", "ROUND4_REVERTS", "ROUND4_EXPECTED", None),
    ("funcționalități cerute - proxy, sortare, dirty flag", "test_features_audit.py", "FEATURE_REVERTS", "FEATURE_EXPECTED", None),
]


def main():
    forms = ["package", "single"]
    if not os.path.isdir(os.path.join(PROJECT, "catssoft")):
        forms = ["single"]
    problems = 0
    for form in forms:
        print()
        print("#" * 70)
        print("#  FORMA: %s" % form)
        print("#" * 70)
        for label, test_file, reverts_name, expected_name, support_name in CONTROLS:
            reverts = globals()[reverts_name]
            expected = globals()[expected_name]
            support = globals().get(support_name) if support_name else None
            problems += _run_control(label, test_file, reverts, expected, form, support)
    print("=" * 70)
    if problems:
        print("CONTROL NEGATIV FAILED - %d grup(uri) de teste nu prind regresiile." % problems)
        return 1
    print("CONTROL NEGATIV OK: toate regresiile sunt prinse de teste.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
