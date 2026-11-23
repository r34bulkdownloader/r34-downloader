"""An e-hentai gallery's pages in a grid of their own (a search tab without the tag boxes)."""
import html
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMenu, QPushButton, QSizePolicy, QToolButton

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

    filters = False  # opened on purpose: show every page (blacklisted galleries are left out of searches)

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
        self.fav_btn = QPushButton()
        self.fav_btn.clicked.connect(lambda: win.toggle_favorite(gallery))
        self.gallery_dl_btn = QToolButton()
        self.gallery_dl_btn.setText("Download gallery")
        self.gallery_dl_btn.setToolTip("Download every page of this gallery · arrow: as one folder or ZIP")
        self.gallery_dl_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.gallery_dl_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        menu = QMenu(self.gallery_dl_btn)
        menu.addAction("Download gallery", self.download_gallery)
        menu.addAction("Download gallery as folder…", lambda: self.download_gallery_album(zipped=False))
        menu.addAction("Download gallery as ZIP…", lambda: self.download_gallery_album(zipped=True))
        self.gallery_dl_btn.setMenu(menu)
        self.gallery_dl_btn.clicked.connect(self.download_gallery)
        for i, w in enumerate((title, self.info_label, self.tag_list, self.fav_btn, self.gallery_dl_btn, browser)):
            sv.insertWidget(i, w, 3 if w is self.tag_list else 0)
        self.update_favorite()

    def update_favorite(self):
        fav = self.gallery.id in self.win.favorites[self.gallery.site]
        self.fav_btn.setText("♥ Favorited gallery" if fav else "♡ Favorite gallery")

    # the whole gallery, not just the pages loaded so far (same as the gallery's menu in a search grid)
    def download_gallery(self):
        self.win.download_posts([self.gallery], self.gallery.album.get("title", ""))

    def download_gallery_album(self, zipped: bool):
        self.win.download_album([self.gallery], self.gallery.album.get("title", ""), zipped)

    def title(self) -> str:
        return self.gallery.label

    def _request(self, cursor):
        self.site.fetch_gallery(self.gallery, cursor, self.model.generation)
