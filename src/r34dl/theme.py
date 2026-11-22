"""App themes: System (the desktop's own look), Light, Dark, Black and colour themes. Applied instantly."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEMES = {"system": "System (follow the desktop)", "light": "Light", "dark": "Dark", "black": "Black (OLED)",
          "r34": "R34 (greens)", "sunset": "Sunset", "midnight": "Midnight", "pink": "Pink",
          "gold": "Black & Gold"}

# window, base (lists/fields), alternate, button, text, dim text, link, highlight, text on highlight
_PALETTES = {
    "dark": ("#2b2b2b", "#1f1f1f", "#262626", "#353535", "#e6e6e6", "#8c8c8c", "#6aa6ff", "#3d7fd9", "#ffffff"),
    "black": ("#000000", "#000000", "#0d0d0d", "#141414", "#dddddd", "#7a7a7a", "#6aa6ff", "#3d7fd9", "#ffffff"),
    "r34": ("#b4e4ab", "#e4f6e0", "#cdedc7", "#9fd894", "#132a10", "#4c6b47", "#1b5f8c", "#2e7d32", "#ffffff"),
    "sunset": ("#2b1a2f", "#1f1323", "#301d35", "#40253f", "#ffe9d9", "#b8909c", "#ffae70", "#e8613c", "#ffffff"),
    "midnight": ("#111a2c", "#0a111e", "#152035", "#1c2945", "#dce5f7", "#8090b3", "#86b8ff", "#3d6fd9", "#ffffff"),
    "pink": ("#ffe3ee", "#fff5f9", "#ffd8e7", "#ffc9de", "#3b1a2b", "#a2728a", "#b3125a", "#e0558f", "#ffffff"),
    "gold": ("#0b0b0b", "#000000", "#15130e", "#1d1a12", "#f2e8c9", "#91866a", "#e8c45e", "#c9a227", "#000000"),
}

_system_style: str | None = None  # the desktop's own style, captured before the first change


def _palette(window: str, base: str, alt: str, button: str, text: str, dim: str, link: str, highlight: str,
             on_highlight: str) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: window, QPalette.WindowText: text, QPalette.Base: base, QPalette.AlternateBase: alt,
        QPalette.Button: button, QPalette.ButtonText: text, QPalette.Text: text, QPalette.BrightText: "#ffffff",
        QPalette.ToolTipBase: button, QPalette.ToolTipText: text, QPalette.PlaceholderText: dim,
        QPalette.Highlight: highlight, QPalette.HighlightedText: on_highlight, QPalette.Link: link,
        QPalette.LinkVisited: link, QPalette.Light: button, QPalette.Midlight: alt, QPalette.Mid: dim,
        QPalette.Dark: base, QPalette.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(dim))
    return p


def apply(name: str) -> None:
    global _system_style
    app = QApplication.instance()
    if app is None:
        return
    if name not in THEMES or name == "system":
        if _system_style is not None:  # only undo what we changed; untouched, Qt keeps following the desktop
            app.setStyle(QStyleFactory.create(_system_style) or app.style())
            app.setPalette(QPalette())  # an empty palette = no app override: back to the desktop's colours
        return
    if _system_style is None:
        _system_style = app.style().name()
    app.setStyle(QStyleFactory.create("Fusion"))  # same look on every desktop
    app.setPalette(app.style().standardPalette() if name == "light" else _palette(*_PALETTES[name]))


def _luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def contrast(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio, 1 (same) to 21 (black on white)."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable(color: QColor, background: QColor, minimum: float = 3.0) -> QColor:
    """color, made darker (light background) or lighter (dark background) until it stands out enough.
    Keeps the hue, so tag types stay recognisable on every theme."""
    c = QColor(color)
    darker = _luminance(background) > 0.18
    h, s, lightness, a = c.getHslF()
    while contrast(c, background) < minimum and 0.0 < lightness < 1.0:
        lightness = max(0.0, lightness - 0.04) if darker else min(1.0, lightness + 0.04)
        c = QColor.fromHslF(max(h, 0.0), s, lightness, a)
    return c
