"""Shared helpers for the CatsSoft Downloader test suite.

Every test runs against an ISOLATED temporary database and download folder, so the
suite never touches the real history, the real queue or the user's files.

Aplicația există în două forme, și suita trebuie să le verifice pe **ambele**:

  * ``package`` - pachetul ``catssoft/`` (sursa de adevăr)
  * ``single``  - fişierul unic generat din pachet (artefact de build)

Forma se alege cu variabila de mediu ``CATSSoft_FORM`` (``package`` / ``single``).
Dacă nu e setată, se preferă pachetul, iar dacă acesta lipseşte se foloseşte
fişierul unic.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)

PREFERRED_APP = "CatsSoft_Downloader_v6.13_Plexo_Engine.py"
PACKAGE_DIR = os.path.join(PROJECT_DIR, "catssoft")

_APP_MODULE = None


def app_form() -> str:
    """'package' sau 'single' - forma aplicaţiei testate."""
    forced = (os.environ.get("CATSSoft_FORM") or "").strip().lower()
    if forced in ("package", "single"):
        return forced
    if os.path.isfile(os.path.join(PACKAGE_DIR, "__init__.py")):
        return "package"
    return "single"


def find_app_path() -> str:
    """Locate the single-file application to test."""
    candidate = os.path.join(PROJECT_DIR, PREFERRED_APP)
    if os.path.isfile(candidate):
        return candidate
    try:
        import glob
        matches = sorted(
            glob.glob(os.path.join(PROJECT_DIR, "CatsSoft_Downloader_v6*.py")),
            key=os.path.getmtime, reverse=True)
        matches = [m for m in matches if "Copie" not in os.path.basename(m)]
        if matches:
            return matches[0]
    except Exception:
        pass
    raise FileNotFoundError(
        "Nu am găsit fișierul aplicației în folderul: %s" % PROJECT_DIR)


def load_app():
    """Import the application once and reuse the module across tests."""
    global _APP_MODULE
    if _APP_MODULE is None:
        if app_form() == "package":
            _APP_MODULE = _load_package()
        else:
            _APP_MODULE = _load_single()
    return _APP_MODULE


def _load_package():
    """Importă pachetul ``catssoft`` ca aplicație."""
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)
    import catssoft  # noqa: F401  (importul face toată munca)
    modules = [module for name, module in list(sys.modules.items())
               if module is not None and (name == "catssoft"
                                          or name.startswith("catssoft."))]
    return _AppProxy(catssoft, modules)


def _load_single():
    """Încarcă fişierul unic ca modul."""
    path = find_app_path()
    spec = importlib.util.spec_from_file_location("catssoft_downloader_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _AppProxy:
    """Proxy peste aplicație, cu propagarea atribuirilor în toate modulele.

    În forma de pachet, codul folosește numele din modulul propriu
    (``catssoft.core.engine`` etc.), deci un patch aplicat doar pe namespace-ul
    pachetului nu ar ajunge nicăieri. Proxy-ul face ca ``app.check_disk_space =
    ...`` să ajungă în toate modulele aplicației - deci testele se comportă
    identic în ambele forme.
    """

    def __init__(self, module, modules):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_modules", list(modules))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_module"), name)

    def __setattr__(self, name, value):
        module = object.__getattribute__(self, "_module")
        setattr(module, name, value)
        for other in object.__getattribute__(self, "_modules"):
            if other is not module and hasattr(other, name):
                setattr(other, name, value)

    def __dir__(self):
        return dir(object.__getattribute__(self, "_module"))


def _app_modules(app):
    """Modulele aplicației în care trebuie aplicate patch-urile de izolare."""
    if app_form() == "package":
        return [module for name, module in list(sys.modules.items())
                if module is not None and (name == "catssoft"
                                           or name.startswith("catssoft."))]
    return [app]


class IsolatedEnvironment:
    """Context manager that redirects the app to a throwaway DB + download folder.

    Usage::

        with IsolatedEnvironment() as env:
            app = load_app()
            db = app.DatabaseManager(env.db_path)
    """

    def __init__(self):
        self.app = load_app()
        self.tmp = tempfile.mkdtemp(prefix="catssoft_test_")
        self.db_path = os.path.join(self.tmp, "downloads_history.db")
        self.downloads_dir = os.path.join(self.tmp, "downloads")
        os.makedirs(self.downloads_dir, exist_ok=True)

        # Urmărim fiecare DatabaseManager creat (inclusiv cele din firele de
        # lucru) ca să le închidem la final - altfel rămân conexiuni SQLite
        # deschise și testele emit ResourceWarning.
        self._databases = []
        registry = self._databases
        self._original_db_class = self.app.DatabaseManager
        base = self._original_db_class

        class _TrackedDatabase(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                registry.append(self)

        self._modules = _app_modules(self.app)
        self._original_default = {}
        for module in self._modules:
            if hasattr(module, "default_database_path"):
                self._original_default[module] = module.default_database_path
                module.default_database_path = lambda: self.db_path
            if hasattr(module, "DatabaseManager"):
                module.DatabaseManager = _TrackedDatabase

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        for module, original in self._original_default.items():
            module.default_database_path = original
        for module in self._modules:
            if hasattr(module, "DatabaseManager"):
                module.DatabaseManager = self._original_db_class
        self.app.DatabaseManager = self._original_db_class
        for database in self._databases:
            try:
                database.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False
