"""ui/theme — Paleta de culori și managerul de temă."""

import os, time, queue, subprocess, sys, shutil, hashlib, colorsys, csv, socket, contextlib, ipaddress, tempfile, errno
from enum import Enum
from typing import Optional, Callable, List
import weakref

from catssoft.logging_setup import logger
# ── orig:794 ──



# ═════════════════════════════════════════════════════════════
# THEME SYSTEM (FIX #5 - WeakRef pentru Memory Leak)
# ═════════════════════════════════════════════════════════════

class AccentColor(Enum):
    RED = ("#ef4444", "Roșu")
    CRIMSON = ("#dc2626", "Crimson")
    ROSE = ("#f43f5e", "Rose")
    PINK = ("#ec4899", "Roz")
    ORANGE = ("#f97316", "Portocaliu")
    CORAL = ("#ff7f50", "Coral")
    AMBER = ("#f59e0b", "Chihlimbar")
    GOLD = ("#eab308", "Auriu")
    LIME = ("#84cc16", "Lime")
    GREEN = ("#10b981", "Smarald")
    MINT = ("#14b8a6", "Mentă")
    TEAL = ("#0d9488", "Teal")
    CYAN = ("#06b6d4", "Cyan")
    SKY = ("#0ea5e9", "Albastru cer")
    BLUE = ("#3b82f6", "Albastru")
    INDIGO = ("#6366f1", "Indigo")
    VIOLET = ("#8b5cf6", "Violet")
    MAGENTA = ("#d946ef", "Magenta")
    PURPLE = ("#a855f7", "Mov")
    LAVENDER = ("#c084fc", "Lavandă")
    NEON_RED = ("#ff003c", "Roșu neon")
    NEON_GREEN = ("#39ff14", "Verde neon")
    NEON_BLUE = ("#00f0ff", "Albastru neon")
    SLATE = ("#64748b", "Slate")

    @property
    def hex(self) -> str:
        return self.value[0]

    @property
    def name_local(self) -> str:
        return self.value[1]
# ── orig:829 ──


class ColorTheme:
    def __init__(
        self, name: str, bg_main: str, bg_sidebar: str, bg_card: str,
        bg_card_hover: str, bg_nav: str, bg_input: str, border: str,
        text_primary: str, text_secondary: str, text_dim: str,
        accent: str = "#3b82f6", success: str = "#22c55e",
        warning: str = "#f59e0b", error: str = "#ef4444",
    ):
        self.name = name
        self.bg_main = bg_main
        self.bg_sidebar = bg_sidebar
        self.bg_card = bg_card
        self.bg_card_hover = bg_card_hover
        self.bg_nav = bg_nav
        self.bg_input = bg_input
        self.border = border
        self.text_primary = text_primary
        self.text_secondary = text_secondary
        self.text_dim = text_dim
        self.accent = accent
        self.success = success
        self.warning = warning
        self.error = error

    def with_accent(self, accent: str) -> "ColorTheme":
        return ColorTheme(
            name=self.name, bg_main=self.bg_main, bg_sidebar=self.bg_sidebar,
            bg_card=self.bg_card, bg_card_hover=self.bg_card_hover,
            bg_nav=self.bg_nav, bg_input=self.bg_input, border=self.border,
            text_primary=self.text_primary, text_secondary=self.text_secondary,
            text_dim=self.text_dim, accent=accent, success=self.success,
            warning=self.warning, error=self.error,
        )
# ── orig:864 ──


class ThemePresets:
    @staticmethod
    def dark_modern() -> ColorTheme:
        return ColorTheme(
            name="Dark Modern", bg_main="#1a1a1a", bg_sidebar="#141414",
            bg_card="#242424", bg_card_hover="#2c2c2c", bg_nav="#1e1e1e",
            bg_input="#2a2a2a", border="#333333", text_primary="#f1f5f9",
            text_secondary="#94a3b8", text_dim="#64748b", accent="#3b82f6",
        )

    @staticmethod
    def dark_macos() -> ColorTheme:
        return ColorTheme(
            name="Dark macOS", bg_main="#1c1c1e", bg_sidebar="#151516",
            bg_card="#2c2c2e", bg_card_hover="#38383a", bg_nav="#1c1c1e",
            bg_input="#252527", border="#3a3a3c", text_primary="#f2f2f7",
            text_secondary="#8e8e93", text_dim="#48484a", accent="#007AFF",
        )

    @staticmethod
    def light_modern() -> ColorTheme:
        return ColorTheme(
            name="Light Modern", bg_main="#f5f5f7", bg_sidebar="#ffffff",
            bg_card="#ffffff", bg_card_hover="#f0f0f2", bg_nav="#ffffff",
            bg_input="#f0f0f2", border="#d1d1d6", text_primary="#1d1d1f",
            text_secondary="#6e6e73", text_dim="#aeaeb2", accent="#007AFF",
        )

    @staticmethod
    def dracula() -> ColorTheme:
        return ColorTheme(
            name="Dracula", bg_main="#282a36", bg_sidebar="#21222c",
            bg_card="#44475a", bg_card_hover="#6272a4", bg_nav="#282a36",
            bg_input="#44475a", border="#44475a", text_primary="#f8f8f2",
            text_secondary="#6272a4", text_dim="#44475a", accent="#bd93f9",
        )

    @staticmethod
    def nord() -> ColorTheme:
        return ColorTheme(
            name="Nord", bg_main="#2e3440", bg_sidebar="#242933",
            bg_card="#434c5e", bg_card_hover="#4c566a", bg_nav="#2e3440",
            bg_input="#3b4252", border="#4c566a", text_primary="#eceff4",
            text_secondary="#d8dee9", text_dim="#4c566a", accent="#88c0d0",
        )

    @staticmethod
    def gruvbox() -> ColorTheme:
        return ColorTheme(
            name="Gruvbox", bg_main="#282828", bg_sidebar="#1d2021",
            bg_card="#504945", bg_card_hover="#665c54", bg_nav="#282828",
            bg_input="#3c3836", border="#504945", text_primary="#ebdbb2",
            text_secondary="#a89984", text_dim="#504945", accent="#fabd2d",
        )

    @staticmethod
    def light_warm() -> ColorTheme:
        return ColorTheme("Light Warm", "#faf7f2", "#f1ebe2", "#ffffff", "#e7ddcc", "#f1ebe2",
                          "#f1ebe2", "#e4dacb", "#292524", "#78716c", "#a8a29e", "#d97706")

    @staticmethod
    def dark_future() -> ColorTheme:
        return ColorTheme("Dark Future", "#05070d", "#080b14", "#0b1120", "#12203c", "#080b14",
                          "#0b1120", "#1e2a44", "#e0f7ff", "#93b4cf", "#5a7184", "#22d3ee")

    @staticmethod
    def dark_cosmos() -> ColorTheme:
        return ColorTheme("Dark Cosmos", "#0b0614", "#100a1e", "#170d2b", "#271848", "#100a1e",
                          "#170d2b", "#2d1b4e", "#ede9fe", "#a79bc8", "#6d5f8f", "#a78bfa")

    @staticmethod
    def dark_oled() -> ColorTheme:
        return ColorTheme("Dark OLED", "#000000", "#0a0a0a", "#111111", "#1f1f1f", "#0a0a0a",
                          "#111111", "#262626", "#f2f2f2", "#a1a1a1", "#6f6f6f", "#30d158")

    @staticmethod
    def pastel_bloom() -> ColorTheme:
        return ColorTheme("Pastel Bloom", "#fff6f3", "#ffede6", "#ffffff", "#ffd8ce", "#fff6f3",
                          "#ffede6", "#f6d9cf", "#4a3b3e", "#8a6f73", "#c4a9ac", "#ef7c8e")

    @staticmethod
    def rose_gold() -> ColorTheme:
        return ColorTheme("Rose Gold", "#fbf3f0", "#f3e3de", "#ffffff", "#e8cfc7", "#fbf3f0",
                          "#f3e3de", "#e3ccc4", "#3e2f2c", "#7d6660", "#bba79f", "#b76e79")

    @staticmethod
    def lavender_mist() -> ColorTheme:
        return ColorTheme("Lavender Mist", "#f7f5fb", "#ece8f6", "#ffffff", "#d6cdea", "#f7f5fb",
                          "#ece8f6", "#dcd5ea", "#363250", "#6e6a8a", "#a9a5c2", "#8b7cd8")

    @staticmethod
    def sunset_coral() -> ColorTheme:
        return ColorTheme("Sunset Coral", "#201025", "#2a1430", "#331a3b", "#4a2854", "#2a1430",
                          "#331a3b", "#4a2a56", "#ffede6", "#cba3ae", "#8a6b78", "#ff7a59")

    @staticmethod
    def ivory_elegance() -> ColorTheme:
        return ColorTheme("Ivory Elegance", "#faf6ee", "#f1eadb", "#fffdf8", "#e4d8be", "#f1eadb",
                          "#f1eadb", "#e2d7bf", "#2e2a20", "#6e6653", "#a89f8a", "#b09048")

    @staticmethod
    def graphite_platinum() -> ColorTheme:
        return ColorTheme("Graphite Platinum", "#101214", "#16191c", "#1c2024", "#2c3238", "#16191c",
                          "#1c2024", "#333a41", "#e8ecf0", "#9aa6b2", "#626e7a", "#a9b8c6")

    @staticmethod
    def champagne_noir() -> ColorTheme:
        return ColorTheme("Champagne Noir", "#0e0c0a", "#14110d", "#1b1712", "#2e261d", "#14110d",
                          "#1b1712", "#3a3128", "#f0e8dc", "#b3a48c", "#7a6c55", "#d6b36a")
# ── orig:975 ──


class ThemeManager:
    """FIX #5: WeakRef pentru a preveni memory leak"""

    _instance: Optional["ThemeManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._current_theme = ThemePresets.dark_modern()
        self._previous_theme = self._current_theme
        # FIX #5: Folosește weakref pentru listeners
        self._listeners: List[weakref.ref] = []
        self._windows: List[weakref.ref] = []

    def register_window(self, window):
        # FIX #5: WeakRef pentru windows
        if not any(ref() is window for ref in self._windows):
            self._windows.append(weakref.ref(window))

    def unregister_window(self, window):
        self._windows = [ref for ref in self._windows if ref() is not window]

    def add_listener(self, widget):
        # FIX #5: WeakRef pentru listeners
        if not any(ref() is widget for ref in self._listeners):
            self._listeners.append(weakref.ref(widget))

    def remove_listener(self, widget):
        self._listeners = [ref for ref in self._listeners if ref() is not widget]

    def _cleanup_dead_refs(self):
        """Elimină referințele moarte fără ca TclError să întrerupă schimbarea temei."""
        def alive(ref):
            obj = ref()
            if obj is None:
                return False
            try:
                return bool(obj.winfo_exists())
            except Exception:
                return False
        self._listeners = [ref for ref in self._listeners if alive(ref)]
        self._windows = [ref for ref in self._windows if alive(ref)]

    def notify_listeners(self):
        """FIX #5: Curățare automată înainte de notify"""
        self._cleanup_dead_refs()

        for ref in self._windows[:]:
            window = ref()
            if window and hasattr(window, '_on_theme_change'):
                try:
                    window._on_theme_change()
                except Exception as e:
                    logger.warning(f"Theme update failed for window: {e}")

        for ref in self._listeners[:]:
            widget = ref()
            if widget and hasattr(widget, '_on_theme_change'):
                try:
                    widget._on_theme_change()
                except Exception as e:
                    logger.warning(f"Theme update failed for widget: {e}")

    @property
    def theme(self) -> ColorTheme:
        return self._current_theme

    def set_theme(self, theme: ColorTheme, apply_immediately: bool = True):
        self._previous_theme = self._current_theme
        self._current_theme = theme
        logger.info(f"Theme changed to: {theme.name}")
        if apply_immediately:
            for ref in self._windows:
                window = ref()
                if window and hasattr(window, 'after'):
                    window.after(0, self.notify_listeners)
            self.notify_listeners()

    def set_accent(self, accent_hex: str, apply_immediately: bool = True):
        self._previous_theme = self._current_theme
        self._current_theme = self._current_theme.with_accent(accent_hex)
        logger.info(f"Accent color changed to: {accent_hex}")
        if apply_immediately:
            for ref in self._windows:
                window = ref()
                if window and hasattr(window, 'after'):
                    window.after(0, self.notify_listeners)
            self.notify_listeners()

    def get_theme_names(self) -> list[str]:
        return ["Dark Modern", "Dark macOS", "Light Modern", "Dracula", "Nord", "Gruvbox",
                "Light Warm", "Dark Future", "Dark Cosmos", "Dark OLED", "Pastel Bloom",
                "Rose Gold", "Lavender Mist", "Sunset Coral", "Ivory Elegance",
                "Graphite Platinum", "Champagne Noir"]

    def get_theme_by_name(self, name: str) -> ColorTheme:
        themes = {
            "Dark Modern": ThemePresets.dark_modern,
            "Dark macOS": ThemePresets.dark_macos,
            "Light Modern": ThemePresets.light_modern,
            "Dracula": ThemePresets.dracula,
            "Nord": ThemePresets.nord,
            "Gruvbox": ThemePresets.gruvbox,
            "Light Warm": ThemePresets.light_warm,
            "Dark Future": ThemePresets.dark_future,
            "Dark Cosmos": ThemePresets.dark_cosmos,
            "Dark OLED": ThemePresets.dark_oled,
            "Pastel Bloom": ThemePresets.pastel_bloom,
            "Rose Gold": ThemePresets.rose_gold,
            "Lavender Mist": ThemePresets.lavender_mist,
            "Sunset Coral": ThemePresets.sunset_coral,
            "Ivory Elegance": ThemePresets.ivory_elegance,
            "Graphite Platinum": ThemePresets.graphite_platinum,
            "Champagne Noir": ThemePresets.champagne_noir,
        }
        preset_fn = themes.get(name, ThemePresets.dark_modern)
        return preset_fn()

    def get_accent_colors(self) -> list[tuple[str, str]]:
        return [(c.hex, c.name_local) for c in AccentColor]
# ── orig:1103 ──

THEME = ThemeManager()
# ── orig:1105 ──

BG_MAIN      = lambda: THEME.theme.bg_main
# ── orig:1106 ──
BG_SIDEBAR   = lambda: THEME.theme.bg_sidebar
# ── orig:1107 ──
BG_CARD      = lambda: THEME.theme.bg_card
# ── orig:1108 ──
BG_CARD_HOV  = lambda: THEME.theme.bg_card_hover
# ── orig:1109 ──
BG_NAV       = lambda: THEME.theme.bg_nav
# ── orig:1110 ──
BG_INPUT     = lambda: THEME.theme.bg_input
# ── orig:1111 ──
BORDER       = lambda: THEME.theme.border
# ── orig:1112 ──
TEXT_PRI     = lambda: THEME.theme.text_primary
# ── orig:1113 ──
TEXT_SEC     = lambda: THEME.theme.text_secondary
# ── orig:1114 ──
TEXT_DIM     = lambda: THEME.theme.text_dim
# ── orig:1115 ──
ACCENT       = lambda: THEME.theme.accent
# ── orig:1116 ──
SUCCESS      = lambda: THEME.theme.success
# ── orig:1117 ──
WARNING      = lambda: THEME.theme.warning
# ── orig:1118 ──
ERROR        = lambda: THEME.theme.error
# ── orig:1120 ──

def shade_color(hex_color: str, factor: float) -> str:
    """FIX: derivă un hover/variantă din accent-ul curent în loc de valori hardcodate.
    factor < 1 = mai închis, factor > 1 = mai deschis."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        light = max(0.0, min(1.0, light * factor))
        r, g, b = colorsys.hls_to_rgb(hue, light, sat)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    except Exception:
        return hex_color
