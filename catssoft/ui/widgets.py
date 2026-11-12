"""ui/widgets — Widget-uri tematizate și tooltip-uri."""

import customtkinter as ctk
import tkinter as tk
from typing import Optional, Callable, List

from catssoft.core.patches import _prune_dead_commands
from catssoft.ui.theme import ACCENT, BG_CARD, BG_CARD_HOV, BG_INPUT, BG_MAIN, BG_NAV, BG_SIDEBAR, BORDER, TEXT_DIM, TEXT_PRI, TEXT_SEC, THEME
# ── orig:1137 ──

# ─────────────────────────────────────────────────────────────
# WIDGET-uri CU SUPORT LIVE THEME
# ─────────────────────────────────────────────────────────────

class ThemedFrame(ctk.CTkFrame):
    def __init__(self, master, fg_color_key: str = "bg_card", **kw):
        self._fg_color_key = fg_color_key
        self._theme_func = self._get_theme_func(fg_color_key)
        super().__init__(master, fg_color=self._theme_func(), **kw)
        THEME.add_listener(self)

    def _get_theme_func(self, key: str) -> Callable:
        theme_funcs = {
            'bg_main': BG_MAIN, 'bg_sidebar': BG_SIDEBAR, 'bg_card': BG_CARD,
            'bg_nav': BG_NAV, 'bg_input': BG_INPUT, 'transparent': lambda: "transparent",
        }
        return theme_funcs.get(key, BG_CARD)

    def _on_theme_change(self):
        if self.winfo_exists():
            try:
                self.configure(fg_color=self._theme_func())
            except Exception:
                pass

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:1164 ──


class ThemedScrollableFrame(ctk.CTkScrollableFrame):
    def __init__(self, master, fg_color_key: str = "bg_card", **kw):
        self._fg_color_key = fg_color_key
        self._theme_func = self._get_theme_func(fg_color_key)
        super().__init__(master, fg_color=self._theme_func(), **kw)
        THEME.add_listener(self)

    def _get_theme_func(self, key: str) -> Callable:
        theme_funcs = {
            'bg_main': BG_MAIN, 'bg_sidebar': BG_SIDEBAR, 'bg_card': BG_CARD,
            'bg_nav': BG_NAV, 'bg_input': BG_INPUT, 'transparent': lambda: "transparent",
        }
        return theme_funcs.get(key, BG_CARD)

    def _on_theme_change(self):
        if self.winfo_exists():
            try:
                self.configure(fg_color=self._theme_func())
                self.configure(
                    scrollbar_button_color=BORDER(),
                    scrollbar_button_hover_color=TEXT_DIM()
                )
            except Exception:
                pass

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:1195 ──


class ThemedButton(ctk.CTkButton):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        THEME.add_listener(self)

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:1210 ──


# ─────────────────────────────────────────────────────────────
# UTILITARE
# ─────────────────────────────────────────────────────────────

def center_window(window, width: int = None, height: int = None):
    window.update_idletasks()
    if width is None:
        width = window.winfo_width()
    if height is None:
        height = window.winfo_height()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = (screen_width // 2) - (width // 2)
    y = (screen_height // 2) - (height // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
# ── orig:1223 ──


class _Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tooltip_window = None

        # Legare directă de metode (fără lambda -> fără probleme de closure)
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, event=None):
        if self.tooltip_window is not None:
            return

        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() + 20

        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        lbl = tk.Label(tw, text=self.text,
                       background=BG_CARD(), foreground=TEXT_PRI(),
                       relief="solid", borderwidth=1,
                       font=("Segoe UI", 10), padx=6, pady=3)
        lbl.pack()

    def _hide(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None
# ── orig:1259 ──

# ═════════════════════════════════════════════════════════════
# ACCENT COLOR SELECTOR
# ═════════════════════════════════════════════════════════════

class AccentColorSelector(ctk.CTkFrame):
    def __init__(self, master, on_color_change: Callable, current_color: str = "#3b82f6", **kw):
        super().__init__(master, fg_color="transparent", **kw)
        THEME.add_listener(self)
        self.on_color_change = on_color_change
        self.current_color = current_color
        self.buttons = {}
        self.oval_ids = {}
        self._build()

    def _on_theme_change(self):
        if self.winfo_exists():
            for btn in self.buttons.values():
                btn.configure(bg=BG_CARD())
            self._update_selection()

    def _build(self):
        accent_colors = THEME.get_accent_colors()
        colors_frame = ctk.CTkFrame(self, fg_color="transparent")
        colors_frame.pack(fill="x", padx=0, pady=7)

        for i, (hex_color, name) in enumerate(accent_colors):
            btn = tk.Canvas(colors_frame, width=40, height=42, bg=BG_CARD(),
                           highlightthickness=0, cursor="hand2", takefocus=True)
            btn.grid(row=i // 8, column=i % 8, padx=(0, 7), pady=2)
            # Extra outer ring gives the selected swatch a clear, accessible focus.
            ring_id = btn.create_oval(3, 3, 37, 37, fill="", outline=BG_CARD(), width=2)
            oval_id = btn.create_oval(8, 8, 32, 32, fill=hex_color, outline=BORDER(), width=1)
            _Tooltip(btn, name)
            btn.bind("<Button-1>", lambda e, h=hex_color: self._on_click(h))
            btn.bind("<Return>", lambda e, h=hex_color: self._on_click(h))
            btn.bind("<space>", lambda e, h=hex_color: self._on_click(h))
            self.buttons[hex_color] = btn
            self.oval_ids[hex_color] = (oval_id, ring_id, name)

        custom_btn = ctk.CTkButton(colors_frame, text="＋", width=34, height=34,
                                   corner_radius=18, fg_color=BG_INPUT(),
                                   hover_color=BG_CARD_HOV(), text_color=TEXT_PRI(),
                                   command=self._pick_custom_color)
        custom_btn.grid(row=3, column=0, padx=(2, 7), pady=(3, 5), sticky="w")
        _Tooltip(custom_btn, "Alege o culoare personalizată")

        selected_name = next((name for color, name in accent_colors if color == self.current_color), "Personalizat")
        self.selection_label = ctk.CTkLabel(self, text=f"Selectat: {selected_name}  {self.current_color.upper()}",
                                            text_color=TEXT_SEC(), anchor="w",
                                            font=ctk.CTkFont(size=11))
        self.selection_label.pack(anchor="w", pady=(0, 2))
        self._update_selection()

    def _on_click(self, hex_color: str):
        self.current_color = hex_color
        self._update_selection()
        self.on_color_change(hex_color)

    def _pick_custom_color(self):
        from tkinter import colorchooser
        selected = colorchooser.askcolor(color=self.current_color, title="Alege culoarea de accent")
        if selected and selected[1]:
            self.current_color = selected[1].lower()
            self._update_selection()
            self.on_color_change(self.current_color)

    def _update_selection(self):
        selected_name = "Personalizat"
        for hex_color, btn in self.buttons.items():
            oval_id, ring_id, name = self.oval_ids[hex_color]
            if hex_color == self.current_color:
                selected_name = name
                btn.itemconfig(ring_id, outline=hex_color, width=3)
                btn.itemconfig(oval_id, outline=TEXT_PRI(), width=2)
                if not hasattr(btn, 'check_id'):
                    btn.check_id = btn.create_text(20, 20, text="✓", fill="#ffffff",
                                                   font=("Segoe UI", 10, "bold"))
                r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
                check_color = "#111827" if (0.299 * r + 0.587 * g + 0.114 * b) > 155 else "#ffffff"
                btn.itemconfig(btn.check_id, fill=check_color)
                btn.itemconfig(btn.check_id, state="normal")
            else:
                btn.itemconfig(ring_id, outline=BG_CARD(), width=2)
                btn.itemconfig(oval_id, outline=BORDER(), width=1)
                if hasattr(btn, 'check_id'):
                    btn.itemconfig(btn.check_id, state="hidden")
        if hasattr(self, "selection_label") and self.selection_label.winfo_exists():
            self.selection_label.configure(text=f"Selectat: {selected_name}  {self.current_color.upper()}",
                                           text_color=TEXT_SEC())

    def set_color(self, hex_color: str):
        if hex_color in self.buttons:
            self.current_color = hex_color
            self._update_selection()

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:1356 ──


class ThemePreviewCard(ctk.CTkFrame):
    """Card compact cu mini-previzualizare, adaptat la tema și accentul curente."""
    def __init__(self, master, theme_name: str, on_pick: Callable, selected: bool = False, **kwargs):
        self.theme_name = theme_name
        self.on_pick = on_pick
        self.selected = selected
        super().__init__(master, width=196, height=142, corner_radius=10,
                         fg_color=BG_CARD(), border_width=2 if selected else 1,
                         border_color=ACCENT() if selected else BORDER(), **kwargs)
        self.pack_propagate(False)
        self.preview = tk.Canvas(self, width=176, height=78,
                                 bg=THEME.get_theme_by_name(theme_name).bg_main,
                                 highlightthickness=0)
        self.preview.pack(padx=9, pady=(8, 5))
        self.name_label = ctk.CTkLabel(self, text=theme_name,
                                       text_color=ACCENT() if selected else TEXT_PRI(),
                                       font=ctk.CTkFont(size=12, weight="bold"))
        self.name_label.pack(anchor="w", padx=10)
        self.desc_label = ctk.CTkLabel(self, text=self._description(theme_name),
                                       text_color=TEXT_SEC(), anchor="w",
                                       font=ctk.CTkFont(size=10))
        self.desc_label.pack(anchor="w", padx=10)
        self._draw_preview()
        self._bind_clicks(self)
        THEME.add_listener(self)

    @staticmethod
    def _description(name: str) -> str:
        return {
            "Dark Modern": "Slate · echilibrat", "Dark macOS": "Grafit · albastru",
            "Light Modern": "Luminos · curat", "Dracula": "Violet · contrast",
            "Nord": "Albastru · calm", "Gruvbox": "Cald · retro",
            "Light Warm": "Tonuri calde", "Dark Future": "Cyan · futurist",
            "Dark Cosmos": "Violet · profund", "Dark OLED": "Negru · eficient",
            "Pastel Bloom": "Roz · delicat", "Rose Gold": "Trandafir · bej",
            "Lavender Mist": "Lavandă · aerisit", "Sunset Coral": "Amurg · coral",
            "Ivory Elegance": "Fildeș · auriu", "Graphite Platinum": "Grafit · rece",
            "Champagne Noir": "Negru · șampanie",
        }.get(name, "Paletă CatsSoft")

    def _draw_preview(self):
        palette = THEME.get_theme_by_name(self.theme_name)
        accent = ACCENT()
        c = self.preview
        c.delete("all")
        c.configure(bg=palette.bg_main)
        c.create_rectangle(0, 0, 28, 78, fill=palette.bg_sidebar, outline="")
        c.create_rectangle(28, 0, 176, 14, fill=palette.bg_nav, outline="")
        for y in (24, 36, 48):
            c.create_oval(7, y, 12, y + 5, fill=accent if y == 24 else palette.text_dim, outline="")
            c.create_rectangle(16, y + 1, 23, y + 4, fill=palette.text_secondary, outline="")
        c.create_rectangle(38, 23, 93, 55, fill=palette.bg_card, outline=palette.border)
        c.create_rectangle(100, 23, 168, 55, fill=palette.bg_card, outline=palette.border)
        c.create_rectangle(42, 29, 65, 33, fill=palette.text_primary, outline="")
        c.create_rectangle(42, 38, 84, 41, fill=palette.text_dim, outline="")
        c.create_rectangle(105, 29, 137, 34, fill=accent, outline="")
        c.create_rectangle(105, 41, 159, 44, fill=palette.text_secondary, outline="")
        c.create_rectangle(38, 62, 84, 72, fill=accent, outline="")
        c.create_rectangle(90, 64, 164, 69, fill=palette.border, outline="")

    def _bind_clicks(self, widget):
        widget.bind("<Button-1>", lambda _event: self.on_pick(self.theme_name))
        widget.bind("<Return>", lambda _event: self.on_pick(self.theme_name))
        for child in widget.winfo_children():
            self._bind_clicks(child)

    def set_selected(self, selected: bool):
        self.selected = selected
        self.configure(border_width=2 if selected else 1,
                       border_color=ACCENT() if selected else BORDER())
        self.name_label.configure(text_color=ACCENT() if selected else TEXT_PRI(),
                                  text=f"{'✓  ' if selected else ''}{self.theme_name}")

    def _on_theme_change(self):
        if self.winfo_exists():
            self.configure(fg_color=BG_CARD())
            self.preview.configure(bg=THEME.get_theme_by_name(self.theme_name).bg_main)
            self._draw_preview()
            self.set_selected(self.selected)
            self.desc_label.configure(text_color=TEXT_SEC())

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
# ── orig:1443 ──


class PreviewButton(ctk.CTkButton):
    def __init__(self, master, text: str = "👁️ Previzualizează Live", command: Callable = None, **kw):
        super().__init__(master, text=text, fg_color=ACCENT(), hover_color=BG_CARD_HOV(),
                        text_color="#000000" if THEME.theme.name.startswith("Light") else "#ffffff",
                        command=command, **kw)
        THEME.add_listener(self)

    def _on_theme_change(self):
        if self.winfo_exists():
            self.configure(fg_color=ACCENT(),
                          text_color="#000000" if THEME.theme.name.startswith("Light") else "#ffffff")

    def destroy(self):
        THEME.remove_listener(self)
        _prune_dead_commands(self)
        super().destroy()
