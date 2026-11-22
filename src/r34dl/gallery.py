"""An e-hentai gallery's pages in a grid of their own (a search tab without the tag boxes)."""
import html
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QPushButton

from .models import Post
from .search import SearchTab
from .viewer import TagList
from .widgets import open_in_browser, wrapped

if TYPE_CHECKING:
    from .ui import MainWindow


def crop_sprite(data: bytes, rect) -> bytes:
    """One thumbnail out of a sprite sheet, as PNG bytes; b"" if the sheet can't be read."""
    img = QImage.fromData(data)
    if img.isNull():
        return b""
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.copy(QRect(*rect)).save(buf, "PNG")
    return bytes(buf.data())


class GalleryTab(SearchTab):
    """The pages of one gallery; paging follows the gallery's ?p= pages."""

    def __init__(self, win: "MainWindow", gallery: Post):
        self.gallery = gallery
        super().__init__(win, "", "", gallery.site)
        self.site_box.hide()
        for w in self.tag_widgets:
            w.hide()
        a = gallery.album
        sv = self.side_layout
        title = wrapped(f"<b>{html.escape(str(a.get('title', '')))}</b>")
        self.info_label = wrapped(f"{a.get('category', '')} · {a.get('pages', '?')} pages")
        self.info_label.setTextFormat(Qt.PlainText)
        self.tag_list = TagList()
        self.tag_list.set_post(gallery)
        self.tag_list.activated_tag.connect(
            lambda tag, bg: win._tag_action(self, "search_bg" if bg else "search", tag))
        browser = QPushButton("Open on e-hentai")
        browser.clicked.connect(lambda: open_in_browser(gallery))
        for i, w in enumerate((title, self.info_label, self.tag_list, browser)):
            sv.insertWidget(i, w, 3 if w is self.tag_list else 0)

    def title(self) -> str:
        return self.gallery.label

    def _request(self, cursor):
        self.site.fetch_gallery(self.gallery, cursor, self.model.generation)
