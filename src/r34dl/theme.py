"""App themes: System (the desktop's own look), Light, Dark, Black. Applied instantly, no restart."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEMES = {"system": "System (follow the desktop)", "light": "Light", "dark": "Dark", "black": "Black (OLED)"}

_system_style: str | None = None  # the desktop's own style, captured before the first change


def _palette(window: str, base: str, alt: str, button: str, text: str, dim: str, link: str) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: window, QPalette.WindowText: text, QPalette.Base: base, QPalette.AlternateBase: alt,
        QPalette.Button: button, QPalette.ButtonText: text, QPalette.Text: text, QPalette.BrightText: "#ffffff",
        QPalette.ToolTipBase: button, QPalette.ToolTipText: text, QPalette.PlaceholderText: dim,
        QPalette.Highlight: "#3d7fd9", QPalette.HighlightedText: "#ffffff", QPalette.Link: link,
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
    if name == "light":
        app.setPalette(app.style().standardPalette())
    elif name == "dark":
        app.setPalette(_palette("#2b2b2b", "#1f1f1f", "#262626", "#353535", "#e6e6e6", "#8c8c8c", "#6aa6ff"))
    else:  # black
        app.setPalette(_palette("#000000", "#000000", "#0d0d0d", "#141414", "#dddddd", "#7a7a7a", "#6aa6ff"))
