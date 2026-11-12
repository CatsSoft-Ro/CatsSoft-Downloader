"""core/patches — Patch-uri tkinter / CustomTkinter aplicate la import."""

import customtkinter as ctk
# ── orig:47 ──

# NOTĂ: Pillow (PIL) e o dependență obligatorie (iconițe, tray, sparkline) — importul de mai sus
# nu e opțional, deci nu mai există un fallback real "fără PIL". PIL_OK a fost eliminat.


def _cancel_pending_after(widget):
    """Anulează timerele ``after`` încă pendente ale acestui widget.

    Un timer care arde după distrugerea widgetului face tkinter să apeleze
    ``deletecommand`` pe un widget deja distrus: ``Misc.destroy`` a setat
    ``_tclCommands = None``, iar ``deletecommand`` face ``self._tclCommands.remove(name)``
    → ``AttributeError: 'NoneType' object has no attribute 'remove'``.
    Tkinter 3.10 prinde acolo doar ``TclError``, deci excepția iese ca
    „Exception in Tkinter callback" și lasă urme în consolă.

    Selectăm doar timerele al căror nume de comandă aparține widgetului
    (tkinter înregistrează comanda în ``widget._tclCommands``), deci nu atingem
    timerele altor widgeturi.
    """
    try:
        commands = set(getattr(widget, "_tclCommands", None) or [])
    except Exception:
        return
    if not commands:
        return
    try:
        event_ids = list(widget.tk.call("after", "info"))
    except Exception:
        return
    for event_id in event_ids:
        try:
            # `after info <id>` întoarce perechea (comandă, tip). Comanda este
            # numele Tcl înregistrat de tkinter în widget._tclCommands.
            info = widget.tk.call("after", "info", event_id)
            script = info[0] if isinstance(info, (tuple, list)) else info
        except Exception:
            continue
        if str(script) in commands:
            try:
                widget.tk.call("after", "cancel", event_id)
            except Exception:
                pass
# ── orig:86 ──


def _cancel_pending_after_tree(widget):
    """Anulează timerele ``after`` ale întregului subarbore al widgetului.

    Tk distruge descendenții unui widget la nivel Tcl, **fără** să apeleze
    metoda Python ``destroy()`` a lor. Așadar, patch-ul de destroy nu ajunge
    la canvas-urile CustomTkinter: fiecare ``CTkButton`` își desenează starea
    printr-un ``after`` înregistrat pe canvas, iar acel timer arde după
    distrugere → „Exception in Tkinter callback: invalid command name
    ...!ctkcanvas". La închiderea ferestrei principale se înregistrau sute de
    astfel de urme.

    Parcurgem subarborele o singură dată și reîinem comenzile Tcl ale tuturor
    widgeturilor, apoi anulăm timerele care le corespund — costă O(widgeturi +
    timeri), nu O(widgeturi × timeri).
    """
    commands = set()
    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            commands.update(getattr(current, "_tclCommands", None) or [])
        except Exception:
            pass
        try:
            stack.extend(current.winfo_children())
        except Exception:
            pass
    if not commands:
        return
    try:
        event_ids = list(widget.tk.call("after", "info"))
    except Exception:
        return
    for event_id in event_ids:
        try:
            info = widget.tk.call("after", "info", event_id)
            script = info[0] if isinstance(info, (tuple, list)) else info
        except Exception:
            continue
        if str(script) in commands:
            try:
                widget.tk.call("after", "cancel", event_id)
            except Exception:
                pass
# ── orig:132 ──


def _prune_dead_commands(widget):
    """Elimină din ``widget._tclCommands`` comenzile Tcl care nu mai există.

    Tk șterge comanda unui callback atunci când timerul lui ``after`` este
    anulat, dar tkinter păstrează numele în ``_tclCommands``. La distrugerea
    widgetului, ``Misc.destroy`` încearcă să ștergă comanda deja dispărută și
    Tk aruncă ``can't delete Tcl command``. Curățăm lista înainte de
    ``super().destroy()``.

    Anulăm și timerele ``after`` încă active: altfel ele ard după distrugerea
    widgetului și lasă „Exception in Tkinter callback" în consolă.
    """
    _cancel_pending_after(widget)
    try:
        commands = getattr(widget, "_tclCommands", None)
        if not commands:
            return
        alive = []
        for name in list(commands):
            try:
                # `info commands` tratează argumentul ca glob, deci verificăm
                # potrivirea exactă a numelui.
                found = str(widget.tk.call("info", "commands", name)).split()
                if name in found:
                    alive.append(name)
            except Exception:
                alive.append(name)
        widget._tclCommands = alive
    except Exception:
        pass
# ── orig:164 ──


def _patch_customtkinter_destroy():
    """Anulează timerele interne ale CustomTkinter înainte de distrugere.

    CustomTkinter programează un ``after`` propriu (setarea iconiței pe
    Windows). Timerul supraviețuiește distrugerii widgetului, iar când arde:
    - Tk afișează „invalid command name ..._windows_set_titlebar_icon";
    - tkinterul apelează ``deletecommand`` pe un widget distrus, unde
      ``Misc.destroy`` a setat ``_tclCommands = None`` → ``AttributeError:
      'NoneType' object has no attribute 'remove'`` (3.10 prinde doar TclError).

    Ambele sunt doar zgomot în consolă, dar apar la fiecare închidere de
    dialog, deci merită eliminate.

    IMPORTANT: ``CTkToplevel`` NU moștenește de la ``CTkBaseClass``, ci de la
    ``tkinter.Toplevel`` - altfel dialogurile (AddURLDialog, ResumeQueueDialog,
    toast-ul de clipboard) ar rămâne neacoperite. Un timer ``after`` rămas pe
    un Toplevel distrus nu provoacă doar o excepție Python: la următorul
    ``update()`` (CustomTkinter îl apelează chiar în constructorul fiecărei
    ferestre) Tcl încearcă să execute comanda ștearsă și procesul moare cu
    „access violation" - un crash dur, nu o excepție prinsă.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return

    def _make_patched(original):
        def destroy(self, *args, **kwargs):
            # Subarborele întreg: Tk distruge descendenții la nivel Tcl, fără să
            # apeleze destroy() Python al lor, deci timerele lor ar arde oricum.
            _cancel_pending_after_tree(self)
            _prune_dead_commands(self)
            return original(self, *args, **kwargs)
        destroy._catssoft_patched = True
        return destroy

    holders = [getattr(ctk, "CTk", None), getattr(ctk, "CTkBaseClass", None),
               getattr(ctk, "CTkToplevel", None)]
    try:
        import tkinter as _tk
        holders.append(_tk.Toplevel)
    except Exception:
        pass
    for holder in holders:
        if holder is None:
            continue
        original = holder.__dict__.get("destroy")
        if original is None or getattr(original, "_catssoft_patched", False):
            continue
        try:
            holder.destroy = _make_patched(original)
        except Exception:
            continue
# ── orig:219 ──


def _patch_tkinter_deletecommand():
    """Tolerează widgeturi deja distruse în ``Misc.deletecommand``.

    ``Misc.after`` înconjoară apelul în ``try/finally``, iar ramura ``finally``
    face ``self.deletecommand(name)``. ``deletecommand`` presupune că
    ``_tclCommands`` mai există - dar ``Misc.destroy`` îl pune ``None``.

    Rezultat: la fiecare închidere a aplicației dintr-un timer (de exemplu
    ``after(8000, app.destroy)``) tkinter tipărește „Exception in Tkinter
    callback" cu „AttributeError: 'NoneType' object has no attribute 'remove'".
    Tkinter prinde acolo doar ``TclError``, deci excepția scapă.

    Comportamentul este identic cu cel original, doar că nu mai aruncă dacă
    widgetul a fost distrus între timp.
    """
    try:
        import tkinter
    except Exception:
        return
    original = getattr(tkinter.Misc, "deletecommand", None)
    if original is None or getattr(original, "_catssoft_patched", False):
        return

    def deletecommand(self, name):
        try:
            self.tk.deletecommand(name)
        except Exception:
            pass
        commands = getattr(self, "_tclCommands", None)
        if not commands:
            return
        try:
            commands.remove(name)
        except ValueError:
            pass

    deletecommand._catssoft_patched = True
    try:
        tkinter.Misc.deletecommand = deletecommand
    except Exception:
        pass
# ── orig:262 ──


def _patch_customtkinter_configure_handler():
    """Evită zgomotul „invalid command name ...!ctkcanvas" la distrugere.

    CustomTkinter își desenează starea în handler-ul evenimentului ``<Configure>``
    (``CTkBaseClass._update_dimensions_event``). Evenimentul e pus în coadă și
    ajunge la widget **după** ce canvas-ul lui a fost distrus - iar atunci
    ``self._canvas.delete(...)`` aruncă ``TclError: invalid command name
    ...!ctkcanvas``, prinsă de tkinter ca „Exception in Tkinter callback".

    La închiderea ferestrei principale se înregistrau sute de asemenea urme în
    consolă (măsurat: 373 pe v6.13, 173 pe v6.12 - deci e o problemă veche,
    nu o regresie). Patch-ul verifică existența canvas-ului înainte de desenare.

    Nu schimbă comportamentul normal: cât timp canvas-ul există, desenarea se
    face exact ca înainte.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return
    base = getattr(ctk, "CTkBaseClass", None)
    if base is None:
        return
    original = getattr(base, "_update_dimensions_event", None)
    if original is None or getattr(original, "_catssoft_patched", False):
        return

    def _update_dimensions_event(self, event=None):
        canvas = getattr(self, "_canvas", None)
        if canvas is not None:
            try:
                if not canvas.winfo_exists():
                    return None
            except Exception:
                return None
        return original(self, event)

    _update_dimensions_event._catssoft_patched = True
    try:
        base._update_dimensions_event = _update_dimensions_event
    except Exception:
        pass
# ── orig:308 ──


# Se aplică la import, nu doar la pornire: aplicația poate fi folosită și ca
# modul (teste, integrare).
_patch_customtkinter_destroy()
# ── orig:309 ──
_patch_tkinter_deletecommand()
# ── orig:310 ──
_patch_customtkinter_configure_handler()
