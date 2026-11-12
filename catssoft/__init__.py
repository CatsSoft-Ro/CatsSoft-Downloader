"""CatsSoft Downloader — aplicația împărțită în module.

Namespace-ul pachetului reproduce 1:1 namespace-ul vechiului fișier
unic, inclusiv numele private, ca suita de teste existente să poată
continua să folosească `app.Nume` fără nicio modificare.
"""

from . import logging_setup  # noqa: F401  (aplică patch-urile la import)
from .core import (automation, capabilities, config, database, engine, models,
                  network, patches, scheduler, security, smart)
from .ui import cards, details, dialogs, main_window, theme, widgets
from .utils import files, formatting
from .paths import app_dir

_MODULES = (logging_setup, patches, capabilities, network, models, database,
            config, automation, scheduler, smart, engine, security,
            theme, widgets, cards, details, dialogs, main_window,
            formatting, files)


def _reexport_namespace():
    """Copiază toate numele publice şi private în namespace-ul pachetului."""
    namespace = globals()
    for module in _MODULES:
        for name in dir(module):
            if not name.startswith('__'):
                namespace[name] = getattr(module, name)


_reexport_namespace()

__all__ = sorted(name for name in globals() if not name.startswith('_'))
