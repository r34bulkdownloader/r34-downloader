import pytest
from PySide6.QtGui import QColor, QPalette

from r34dl import theme
from r34dl.viewer import TAG_COLORS


@pytest.mark.parametrize("name", sorted(theme._PALETTES))
def test_every_palette_is_readable(name):
    window, base, alt, button, text, dim, link, hi, on_hi = map(QColor, theme._PALETTES[name])
    assert theme.contrast(text, base) >= 7 and theme.contrast(text, window) >= 7 and theme.contrast(text, button) >= 4.5
    assert theme.contrast(dim, window) >= 3 and theme.contrast(link, base) >= 3
    assert theme.contrast(on_hi, hi) >= 3


@pytest.mark.parametrize("name", sorted(theme._PALETTES))
def test_tag_colours_stand_out_on_every_theme(name):
    base = QColor(theme._PALETTES[name][1])
    for kind, color in TAG_COLORS.items():
        assert theme.contrast(theme.readable(color, base), base) >= 3, kind


def test_readable_keeps_colours_that_already_work():
    c = QColor("#3d7fd9")
    assert theme.readable(c, QColor("#000000")) == c


def test_every_theme_applies_and_system_restores(qapp):
    for name in theme.THEMES:
        theme.apply(name)
        if name in theme._PALETTES:
            assert qapp.palette().color(QPalette.Highlight) == QColor(theme._PALETTES[name][7])
    theme.apply("system")
