"""Search tabs and the Favorites tab."""
import itertools
from typing import TYPE_CHECKING
import logging
import re
from collections import deque

from PySide6.QtCore import (
    QPoint, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QKeySequence, QShortcut,
)
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMenu, QToolButton, QMessageBox, QPushButton, QSizePolicy,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import api, config
from .models import Post
from .sites import Site
from .grid import ResultsModel, ResultsView
from .widgets import BLACKLIST_SERVER_MAX, CELL_PAD, MAX_THUMBS, PAGE_SIZE, TagEdit, hint_label, open_in_browser, snap_thumb, wrapped

if TYPE_CHECKING:  # type hints only: ui imports this module
    from .ui import MainWindow

log = logging.getLogger(__name__)

_generations = itertools.count(1)  # search generations are unique across all search tabs
AUTO_PAGES = 10  # infinite scroll loads this many pages on its own, then waits for "Load more"
SHARP_FROM = 250  # px: above this grid size, "sharper thumbnails" loads the sample picture


class SearchTab(QWidget):
    """One search: its own include/exclude boxes, results grid, selection and paging."""

    fetch_finished = Signal(bool)  # a page request ended (True = a page arrived, False = it failed)
    filters = True  # blacklist and hide-downloaded leave posts out of the results
    always_more = False  # show the load-more button whenever more pages exist, not only at the cap

    def __init__(self, win: "MainWindow", include: str = "", exclude: str = "", site: str = "rule34.xxx"):
        super().__init__()
        self.win = win
        self.model = ResultsModel(self)
        self._use_site(site)
        self.model.thumb_size = snap_thumb(win.size_slider.value())
        self._tags = ""
        self._include = ""
        self._cursor = None  # cursor of the page asked for last (None = first page)
        self._next_cursor = None  # infinite scroll: cursor of the next page
        self._pages = 0  # infinite scroll: pages loaded so far
        self._auto_limit = AUTO_PAGES  # infinite scroll stops loading by itself at this many pages
        self._sharp_tier = False  # thumbnails now loaded are the sample pictures (see refresh_thumbs)
        self._total: tuple[int, bool] | None = None  # the site's result count for this search, approximate
        self._inflight = False
        self._failed_shown: tuple | None = None
        self._hidden = 0  # posts left out of this search because they were already downloaded
        self._blacklisted = 0  # posts left out because of the blacklist
        self._blacklist: set[str] = set()
        self._paged = False  # page-at-a-time mode (Settings: infinite scroll off), fixed per search
        self._page = 0  # current site page in page mode
        self._thumb_queue: deque[Post] = deque()
        self._thumb_active = 0
        self._thumb_replies: set[QNetworkReply] = set()
        self._thumb_waiting: dict[tuple, list[Post]] = {}  # (generation, url) -> posts sharing that thumbnail
        self._page_cursors: list = [None]  # page mode on a site without page jump: cursor of each page seen
        self._page_failed = False  # the last page asked for failed
        self._closed = False
        self._build_ui(include, exclude)
        self._connect_site(True)
        self.view.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        self.model.dataChanged.connect(lambda *_: self._update_counts())
        self.model.modelReset.connect(self._update_counts)
        self.model.rowsInserted.connect(lambda *_: self._update_counts())
        self._update_counts()

    def _build_ui(self, include: str, exclude: str):
        win = self.win
        row = QHBoxLayout(self)
        side = QWidget()
        side.setFixedWidth(260)
        sv = self.side_layout = QVBoxLayout(side)
        self.site_box = QComboBox()
        for name, s in win.sites.items():
            self.site_box.addItem(s.label, name)
        self.site_box.setCurrentIndex(self.site_box.findData(self.site.name))
        self.site_box.currentIndexChanged.connect(lambda _: self._site_changed(self.site_box.currentData()))
        sv.addWidget(self.site_box)
        include_label = wrapped("<b>Include tags</b> — posts must have all of these")
        sv.addWidget(include_label)
        self.include_edit = TagEdit(win.nam, lambda: self.site)
        self.include_edit.setPlainText(include)
        sv.addWidget(self.include_edit, 2)
        exclude_label = wrapped("<b>Exclude tags</b> — hide posts with any of these")
        sv.addWidget(exclude_label)
        self.exclude_edit = TagEdit(win.nam, lambda: self.site)
        self.exclude_edit.setPlainText(exclude)
        sv.addWidget(self.exclude_edit, 1)
        self.tags_hint = hint_label("")
        sv.addWidget(self.tags_hint)
        self.search_btn = QPushButton("Search")
        self.tag_widgets = [include_label, self.include_edit, exclude_label, self.exclude_edit, self.tags_hint,
                            self.search_btn]
        self.search_btn.setToolTip("Ctrl+Enter in either box also searches")
        self.search_btn.clicked.connect(self.search)
        sv.addWidget(self.search_btn)
        for ed in (self.include_edit, self.exclude_edit):
            QShortcut(QKeySequence("Ctrl+Return"), ed, activated=self.search, context=Qt.WidgetShortcut)
            QShortcut(QKeySequence("Ctrl+Enter"), ed, activated=self.search, context=Qt.WidgetShortcut)
        self.save_label = QLabel()
        self.save_label.setWordWrap(True)
        pick = QPushButton("Choose folder…")
        pick.clicked.connect(win._pick_folder)
        sv.addWidget(self.save_label)
        sv.addWidget(pick)
        self.template_label = QLabel()
        self.template_label.setWordWrap(True)
        sv.addWidget(self.template_label)
        self.dl_btn = QToolButton()
        self.dl_btn.setText("Download 0")
        self.dl_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.dl_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.dl_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.dl_btn.setToolTip("Download the ticked posts · arrow: download them as one folder or ZIP (albums)")
        dl_menu = QMenu(self.dl_btn)
        dl_menu.addAction("Download", self.download_selected)
        dl_menu.addAction("Download as folder…", lambda: self.win.download_album(
            self.model.selected_posts(), self._include, zipped=False))
        dl_menu.addAction("Download as ZIP…", lambda: self.win.download_album(
            self.model.selected_posts(), self._include, zipped=True))
        self.dl_btn.setMenu(dl_menu)
        self.dl_btn.clicked.connect(self.download_selected)
        sv.addWidget(self.dl_btn)
        row.addWidget(side)

        right = QVBoxLayout()
        bar = QHBoxLayout()
        self.count_label = QLabel()
        bar.addWidget(self.count_label, 1)
        for text, mode in (("All", "all"), ("None", "none"), ("Invert", "invert")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, m=mode: self.model.set_all(m))
            bar.addWidget(b)
        self.together_btn = QPushButton("Open together")
        self.together_btn.setToolTip("Open the ticked posts side by side in one tab")
        self.together_btn.clicked.connect(lambda: self.win.open_together(self.model.selected_posts()))
        bar.addWidget(self.together_btn)
        self.slideshow_btn = QPushButton("▶ Slideshow")
        self.slideshow_btn.setToolTip("Show these posts full screen, one after another (or only the ticked ones)")
        self.slideshow_btn.clicked.connect(lambda: self.win.start_slideshow(self))
        bar.addWidget(self.slideshow_btn)
        right.addLayout(bar)
        right.addWidget(hint_label(
            "Click a picture to open it in a tab · Ctrl+click or middle-click opens it in the background · "
            "tick the box in the corner to select it for Download · Shift+click selects a range · "
            "a green ↓ means you downloaded it before · ♥ marks a favorite."))
        self.view = ResultsView()
        self.view.setModel(self.model)
        self.view.preview.nam = win.nam
        # e-hentai posts have no file address until opened, so nothing to preview
        self.view.preview.enabled = lambda: self.win.cfg["hover_preview"] and self.site.name != "e-hentai"
        self.view.set_thumb_size(self.model.thumb_size)
        self.view.post_clicked.connect(self._on_post_clicked)
        self.view.context_requested.connect(self._post_menu)
        self.view.zoom_requested.connect(win.zoom_thumbs)
        right.addWidget(self.view, 1)
        self.more_btn = QPushButton(f"Load more ({AUTO_PAGES} pages)")
        self.more_btn.setToolTip("Scrolling stops loading new pages by itself after a while, so a big search "
                                 "doesn't load everything")
        self.more_btn.clicked.connect(self._load_more_pages)
        self.more_btn.hide()
        right.addWidget(self.more_btn)
        self.page_bar = QWidget()
        pb = QHBoxLayout(self.page_bar)
        pb.setContentsMargins(0, 0, 0, 0)
        self.prev_page_btn = QPushButton("◀ Previous page")
        self.next_page_btn = QPushButton("Next page ▶")
        self.page_label = QLabel()
        self.page_jump = QSpinBox()
        self.page_jump.setRange(1, 200000)
        self.page_jump.setPrefix("Page ")
        self.page_go = QPushButton("Go")
        self.prev_page_btn.clicked.connect(lambda: self.go_page(self._page - 1))
        self.next_page_btn.clicked.connect(lambda: self.go_page(self._page + 1))
        self.page_go.clicked.connect(lambda: self.go_page(self.page_jump.value() - 1))
        pb.addWidget(self.prev_page_btn)
        pb.addStretch(1)
        pb.addWidget(self.page_label)
        pb.addStretch(1)
        pb.addWidget(self.page_jump)
        pb.addWidget(self.page_go)
        pb.addWidget(self.next_page_btn)
        self.page_bar.hide()
        right.addWidget(self.page_bar)
        row.addLayout(right, 1)
        self._show_site()
        self.refresh_settings_labels()

    def title(self) -> str:
        text = " ".join(self._include.split())
        return (text[:28] + "…" if len(text) > 29 else text) or "Search"

    def refresh_settings_labels(self):
        self.save_label.setText(f"Save to: {api.home_short(self.win.cfg['save_dir'])}")
        self.template_label.setText(f"Name: {config.site_cfg(self.win.cfg, self.site.name)['template']}")

    # ---- site
    def _use_site(self, name: str):
        self.site: Site = self.win.site(name)
        self.model.downloaded = self.win.downloaded[name]  # shared set: one history per site for every tab
        self.model.favorites = self.win.favorites[name]  # shared dict: one favorites list per site for every tab

    def _connect_site(self, on: bool):
        for sig, slot in ((self.site.page_ready, self._on_site_page), (self.site.failed, self._on_failed),
                          (self.site.total_found, self._on_total)):
            if on:
                sig.connect(slot)
                continue
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _show_site(self):
        self.tags_hint.setText(self.site.tag_hint)
        self.include_edit.setPlaceholderText(self.site.include_placeholder)
        self.exclude_edit.setPlaceholderText(self.site.exclude_placeholder)
        self.page_jump.setVisible(self.site.page_jump)
        self.page_go.setVisible(self.site.page_jump)

    def _site_changed(self, name: str):
        """Picker: search this site from now on. The grid empties (its posts belong to the old site) and the
        exclude box takes the new site's remembered list."""
        if self.model.checked and QMessageBox.question(
                self, "Change site", "Discard the current selection?") != QMessageBox.Yes:
            self.site_box.blockSignals(True)
            self.site_box.setCurrentIndex(self.site_box.findData(self.site.name))
            self.site_box.blockSignals(False)
            return
        if self is self.win.first:
            self.win._store_tags()  # keep the old site's exclude list
        self._connect_site(False)  # a late page from the old site must not land here
        self._use_site(name)
        self._connect_site(True)
        self.win.cfg["site"] = name
        self.exclude_edit.setPlainText(config.site_cfg(self.win.cfg, name)["exclude"])
        self._show_site()
        self.refresh_settings_labels()
        self.model.generation = 0  # no search yet
        self.model.reset()
        self._thumb_queue.clear()
        self._inflight = False
        self._hidden = self._blacklisted = 0
        self.page_bar.hide()
        self._update_counts()

    def release(self):
        """Tab is closing: ignore further search results and stop its thumbnail downloads and preview."""
        self._closed = True
        self.view.preview.stop()
        self._connect_site(False)
        self._thumb_queue.clear()
        for reply in list(self._thumb_replies):
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        self._thumb_replies.clear()
        self._thumb_waiting.clear()

    # ---- search / paging
    def search(self):
        if self.model.checked:
            r = QMessageBox.question(self, "New search", "Discard the current selection and search again?")
            if r != QMessageBox.Yes:
                return
        self.win.search_started(self)
        self.model.generation = next(_generations)
        self.model.reset()
        self._thumb_queue.clear()
        self._include = self.include_edit.toPlainText()
        split = self.site.split_tags
        included = {t.lower() for t in split(self._include)}
        excluded = {t.lower().lstrip("-") for t in split(self.exclude_edit.toPlainText())}
        active = [t.lower().lstrip("-") for t in split(config.site_cfg(self.win.cfg, self.site.name)["blacklist"])]
        active = [t for t in dict.fromkeys(active) if t and t not in included]
        self._blacklist = set(active)
        server_side = [t for t in active if t not in excluded][:BLACKLIST_SERVER_MAX]
        self._tags = self.site.build_query(self._include, self.exclude_edit.toPlainText(), server_side)
        self._cursor = self._next_cursor = None
        self._pages = 0
        self._auto_limit = AUTO_PAGES
        self._total = None
        self._sharp_tier = self._wants_sharp()
        self.more_btn.hide()
        self._inflight = False
        self._failed_shown = None
        self._hidden = 0
        self._blacklisted = 0
        self._paged = not self.win.cfg["infinite_scroll"]
        self._page = 0
        self._page_cursors = [None]
        self._page_failed = False
        self.page_bar.setVisible(self._paged)
        self.win.set_tab_title(self, self.title())
        self._fetch_next()

    def _fetch_next(self):
        if self._inflight or self.model.exhausted:
            return
        self._inflight = True
        if not self._paged:
            self._cursor = self._next_cursor
        elif self.site.page_jump:
            self._cursor = self._page or None
        else:
            self._cursor = self._page_cursors[self._page]
        self._request(self._cursor)
        self._update_counts()

    def _request(self, cursor):
        self.site.fetch(self._tags, cursor, self.model.generation, self.win.cfg)

    def go_page(self, page: int):
        """Page mode: show site page `page` (0-based) instead of the current one; ticks are kept."""
        if not self._paged or self._inflight or page < 0 or self.model.generation == 0:
            return
        if not self.site.page_jump and page >= len(self._page_cursors):
            if not self._page_failed:
                return  # only pages already reached, or the next one, have a known cursor
            page = self._page  # the page that failed has no next cursor yet: ask for it again
        self._page = page
        self._hidden = self._blacklisted = 0
        self._thumb_queue.clear()
        self.model.reset(keep_selection=True)
        self.view.scrollToTop()
        self._fetch_next()

    def _on_page_ready(self, gen: int, posts: list, raw: int | None = None):
        """Test helper: a page given by its raw entry count, rule34-style (a full page means more). Page-number
        cursors only exist on page-jump sites; elsewhere the next cursor is unknown, so it is the last page."""
        full = (len(posts) if raw is None else raw) >= PAGE_SIZE
        self._on_site_page(gen, posts, (self._cursor or 0) + 1 if full and self.site.page_jump else None)

    def _on_site_page(self, gen: int, posts: list, next_cursor):
        if gen != self.model.generation or self._closed:
            return
        self._inflight = self._page_failed = False
        page = self._page if self._paged else self._pages
        if not self._paged:
            self._pages += 1
            self._next_cursor = next_cursor
        elif next_cursor is not None and page + 1 == len(self._page_cursors):
            self._page_cursors.append(next_cursor)
        if next_cursor is None:
            self.model.exhausted = True
        if self.filters and self._blacklist:
            kept = [p for p in posts if not self._blacklist.intersection(t.lower() for t in p.tags)]
            self._blacklisted += len(posts) - len(kept)
            posts = kept
        if self.filters and self.win.cfg["hide_downloaded"]:
            kept = [p for p in posts if p.id not in self.model.downloaded]
            self._hidden += len(posts) - len(kept)
            posts = kept
        new = self.model.append(posts, page)
        self._thumb_queue.extend(new)
        self._pump_thumbs()
        self._update_counts()
        if not self._paged and not self.model.posts and not self.model.exhausted and not self._at_auto_limit():
            self._fetch_next()  # whole page was hidden; scrolling can't trigger more with an empty grid
        QTimer.singleShot(0, self, self._maybe_load_more)  # tied to the tab: dropped if it's closed first
        self.fetch_finished.emit(True)

    def _on_failed(self, gen: int, msg: str):
        if gen != self.model.generation or self._closed:
            return
        self._inflight = False
        self._page_failed = True
        self._update_counts()  # re-enable page buttons, drop "loading…"
        self.fetch_finished.emit(False)
        msg = api.scrub(msg)
        key = (gen, self._cursor)
        if self._failed_shown == key:
            return  # already told the user about this page; the next scroll retries silently
        self._failed_shown = key
        if self.win.tabs.currentWidget() is not self:  # don't pop up over the tab the user is looking at
            self.win.set_tab_title(self, "⚠ " + self.title())
            self.win.tabs.setTabToolTip(self.win.tabs.indexOf(self), f"Search failed: {msg}")
            return
        if "Settings" in msg:
            r = QMessageBox.question(self, "Search failed", f"{msg}\n\nOpen Settings?")
            if r == QMessageBox.Yes:
                self.win.open_settings()
        else:
            QMessageBox.warning(self, "Search failed", msg)

    def can_load_more(self) -> bool:
        return not self.model.exhausted and self.model.generation > 0

    def load_more(self):
        """Fetch the next page (Next ▶ in a post tab at the end of loaded results)."""
        if self._paged:
            self.go_page(self._page + 1)
        else:
            self._fetch_next()

    def _at_auto_limit(self) -> bool:
        """Infinite scroll has loaded its share of pages: "Load more" instead of loading on."""
        stop = not self._paged and not self.model.exhausted and self._pages >= self._auto_limit
        self._update_more_btn()
        return stop

    def _update_more_btn(self):
        """Shown when scrolling won't load on by itself: the page cap was reached or the last page failed (a
        grid already at the bottom never scrolls again to retry). Gallery tabs always show it while pages remain."""
        can = not self._paged and not self.model.exhausted and not self._inflight and self.model.generation > 0
        need = self.always_more or self._page_failed or self._pages >= self._auto_limit
        self.more_btn.setText(self._more_text())
        self.more_btn.setVisible(bool(can and need))

    def _more_text(self) -> str:
        return "Try again" if self._page_failed else f"Load more ({AUTO_PAGES} pages)"

    def _load_more_pages(self):
        self._auto_limit = self._pages + AUTO_PAGES
        self.more_btn.hide()
        self._fetch_next()

    def _on_total(self, gen: int, count, approx: bool):
        if gen == self.model.generation and not self._closed:
            self._total = (count, approx)
            self._update_counts()

    def _maybe_load_more(self, *_):
        if self._closed:
            return
        self._update_counts()  # the "Page X of Y" indicator follows the scroll position
        if self._paged or self._inflight or self.model.exhausted or not self.model.posts or self._at_auto_limit():
            return
        sb = self.view.verticalScrollBar()
        if sb.maximum() - sb.value() <= 1.5 * self.view.viewport().height():
            self._fetch_next()

    # ---- thumbnails
    def _wants_sharp(self) -> bool:
        return bool(self.win.cfg.get("sharp_thumbs")) and self.model.thumb_size > SHARP_FROM

    def _thumb_url(self, post: Post) -> str:
        """The small preview, or at big grid sizes (Settings: sharper thumbnails) the post's sample picture.
        Only a still image will do: a video's or GIF's sample can be the whole file."""
        sample = post.sample_url
        if self._wants_sharp() and re.search(r"\.(jpe?g|png|webp)$", sample.split("?")[0], re.I):
            return sample
        return post.preview_url

    def refresh_thumbs(self):
        """The grid size or the setting crossed the sharp/small line: load the other kind for every post."""
        if self._wants_sharp() != self._sharp_tier:
            self._sharp_tier = self._wants_sharp()
            self._thumb_queue = deque(self.model.posts)
            self._pump_thumbs()

    def _pump_thumbs(self):
        while self._thumb_active < MAX_THUMBS and self._thumb_queue:
            post = self._thumb_queue.popleft()
            url = self._thumb_url(post)
            if not url:
                continue
            gen = self.model.generation
            key = (gen, url)
            if key in self._thumb_waiting:  # e-hentai: one sprite sheet holds many pages' thumbnails
                self._thumb_waiting[key].append(post)
                continue
            self._thumb_waiting[key] = [post]
            req = api.request(url)
            reply = self.win.nam.get(req)
            self._thumb_active += 1
            self._thumb_replies.add(reply)
            reply.finished.connect(lambda r=reply, p=post, g=gen, u=url: self._thumb_done(r, p, g, u))

    def _thumb_done(self, reply: QNetworkReply, post: Post, gen: int, url: str | None = None):
        url = url or post.preview_url
        from .gallery import crop_sprite  # here: gallery imports this module
        self._thumb_active -= 1
        self._thumb_replies.discard(reply)
        data = bytes(reply.readAll()) if reply.error() == QNetworkReply.NoError else b""
        if not data:
            log.warning("thumbnail failed for post #%s: %s", post.id, reply.errorString())
        reply.deleteLater()
        for p in self._thumb_waiting.pop((gen, url), [post]):
            if gen == self.model.generation and data:
                sprite = p.album.get("sprite")
                thumb = crop_sprite(data, sprite) if sprite else data
                if thumb:
                    self.model.thumb_ready(p.id, thumb)
        self._pump_thumbs()

    # ---- interaction
    def set_thumb_size(self, side: int):
        self.view.set_thumb_size(side)
        self.refresh_thumbs()

    def _post_menu(self, row: int, pos):
        m = self._make_post_menu(self.model.posts[row])
        m.exec(pos)
        m.deleteLater()

    def _make_post_menu(self, post: Post) -> QMenu:
        m = QMenu(self)
        m.addAction("Open in tab", lambda: self._open(post, False))
        m.addAction("Open in background tab", lambda: self._open(post, True))
        m.addAction("Open in browser", lambda: open_in_browser(post))
        m.addSeparator()
        if post.kind == "gallery":
            m.addAction("Download gallery", lambda: self.win.download_posts([post], self._include))
            m.addAction("Download gallery as folder…", lambda: self.win.download_album([post], self._include, False))
            m.addAction("Download gallery as ZIP…", lambda: self.win.download_album([post], self._include, True))
        else:
            m.addAction("Download", lambda: self.win.download_posts([post], self._include))
        m.addAction("Remove from favorites" if post.id in self.model.favorites else "Add to favorites",
                    lambda: self.win.toggle_favorite(post))
        return m

    def _on_post_clicked(self, row: int, background: bool):
        self._open(self.model.posts[row], background)

    def _open(self, post: Post, background: bool):
        if post.kind == "gallery":
            self.win.open_gallery(post, background)
        else:
            self.win.open_viewer(post, background, self)

    def _current_page(self) -> int:
        """Site page (1-based) of the first post visible in the grid."""
        idx = self.view.indexAt(QPoint(CELL_PAD, CELL_PAD))
        post = self.model.posts[idx.row()] if idx.isValid() else (self.model.posts[0] if self.model.posts else None)
        return self.model.page_of.get(post.id, 0) + 1 if post else 1

    def _update_counts(self):
        self._update_more_btn()  # runs on every load start, page, failure and search
        n = len(self.model.checked)
        hidden = f" · {self._hidden} already downloaded (hidden)" if self._hidden else ""
        hidden += f" · {self._blacklisted} hidden by blacklist" if self._blacklisted else ""
        loading = " · loading…" if self._inflight else ""
        if self._total is not None:
            count, approx = self._total
            loading = f" · {'about ' if approx else ''}{count:,} result{'' if count == 1 else 's'} in all{loading}"
        if self._paged:
            page = f"Page {self._page + 1}"
            self.count_label.setText(f"{page} · {len(self.model.posts)} posts · {n} selected{hidden}{loading}")
            self.page_label.setText(page)
            self.prev_page_btn.setEnabled(self._page > 0 and not self._inflight)
            self.next_page_btn.setEnabled(not self.model.exhausted and not self._inflight)
        else:
            pages = f" · Page {self._current_page()} of {self._pages} loaded" if self._pages else ""
            self.count_label.setText(f"{len(self.model.posts)} loaded{pages} · {n} selected{hidden}{loading}")
        self.dl_btn.setText(f"Download {n}")
        self.together_btn.setEnabled(n >= 2)
        self.slideshow_btn.setEnabled(bool(self.model.posts))

    def download_selected(self):
        self.win.download_posts(self.model.selected_posts(), self._include)

    def add_tag(self, box: str, tag: str):
        """Append a tag to this tab's include or exclude box (one per line, no duplicates)."""
        edit = self.include_edit if box == "include" else self.exclude_edit
        term = self.site.include_term(tag) if box == "include" else tag  # e-hentai's search box wants ns:"tag$"
        text = edit.toPlainText()
        present = term in text if term != tag else tag in re.split(r"[\s,]+", text)  # quoted terms hold spaces
        if not present:
            text = text.rstrip()
            edit.setPlainText((text + "\n" if text else "") + term)


class FavoritesTab(SearchTab):
    """Your favorites as a grid (same ticking, Download, Download as…, Open together). The tag boxes filter
    locally — no requests to the site. The blacklist applies here too."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win, "", "")
        self.search_btn.setText("Filter")
        self.together_btn.setToolTip("Open the ticked favorites side by side")

    def title(self) -> str:
        return "♥ Favorites"

    def _show_site(self):
        super()._show_site()
        eh = self.site.name == "e-hentai"
        self.include_edit.setPlaceholderText("filter by tag, e.g.\n" + (
            "female:long hair\nlanguage:english" if eh else "long_hair\nrating:explicit"))
        self.exclude_edit.setPlaceholderText("hide favorites with\nany of these")
        self.tags_hint.setText(
            "Filters your saved favorites on this computer (no searching the site). " + (
                "One tag per line or comma-separated, with its namespace: <b>female:long hair</b>. " if eh else
                "One tag per line; <b>rating:explicit</b> / <b>rating:questionable</b> work, site extras like "
                "<b>sort:</b> don't. ") + "Your blacklist applies here too.")

    def can_load_more(self) -> bool:
        return False

    def _site_changed(self, name: str):
        """Picker: show that site's favorites (one site at a time)."""
        self._connect_site(False)
        self._use_site(name)
        self._connect_site(True)
        self._show_site()
        self.refresh_settings_labels()
        self.search()

    def _set_filter(self):
        """Read the boxes and the blacklist into sets (plain tags and rating:…; other meta tags only work on the
        site). A -tag in Include means exclude, as in a site search."""
        self._include = self.include_edit.toPlainText()
        typed = [t.lower() for t in self.site.split_tags(self._include)]
        need = {t for t in typed if not t.startswith("-")}
        avoid = {t.lstrip("-") for t in typed if t.startswith("-")}
        avoid |= {t.lower().lstrip("-") for t in self.site.split_tags(self.exclude_edit.toPlainText())}
        black = {t.lower().lstrip("-") for t in self.site.split_tags(
            config.site_cfg(self.win.cfg, self.site.name)["blacklist"])} - need
        self._need, self._avoid, self._black = need, avoid, black

    @staticmethod
    def _hits(post: Post, wanted: set[str]) -> bool:
        """Does the post have any of these tags (rating:x compares the post's rating)?"""
        tags = {t.lower() for t in post.tags}
        rating = post.rating.lower()
        return bool(tags & wanted) or any(t.startswith("rating:") and t[7:] in (rating, rating[:1]) for t in wanted)

    def _matches(self, post: Post) -> bool:
        tags = {t.lower() for t in post.tags}
        # e-hentai tags all have a namespace; on rule34.xxx a ":" is a site extra (sort:, score:) we can't check
        plain = {t for t in self._need if ":" not in t or self.site.name == "e-hentai"}
        ratings = {t for t in self._need if t.startswith("rating:")}
        return plain <= tags and (not ratings or self._hits(post, ratings)) and not self._hits(post, self._avoid)

    def search(self):
        """Show the favorites matching the boxes (ticks on favorites still shown are kept)."""
        kept = set(self.model.checked)
        self.model.generation = next(_generations)
        self.model.reset()
        self._sharp_tier = self._wants_sharp()
        self._thumb_queue.clear()
        self._set_filter()
        posts, hidden = [], 0
        for p in self.model.favorites.values():
            if not self._matches(p):
                continue
            if self._hits(p, self._black):
                hidden += 1
                continue
            posts.append(p)
        self._blacklisted, self._hidden, self._paged, self._inflight = hidden, 0, False, False
        self.page_bar.hide()
        new = self.model.append(posts)
        self.model.exhausted = True
        self.model.checked = kept & {p.id for p in posts}
        self._thumb_queue.extend(new)
        self._pump_thumbs()
        self._update_counts()

    def favorite_changed(self, post: Post):
        """Update in place (no reload, no re-downloading thumbnails, open post tabs keep their place): a new
        favorite goes to the top if it matches; an unfavorited one stays until the next Filter, without its ♥."""
        if post.site != self.site.name:
            return  # another site's favorites list
        if post.id in self.model.favorites and post.id not in self.model._rows and self.model.generation:
            if self._matches(post) and not self._hits(post, self._black):
                self._thumb_queue.extend(self.model.insert_front(post))
                self._pump_thumbs()
        else:
            self.model.mark_changed(post.id)
        self._update_counts()

    def _update_counts(self):
        n = len(self.model.checked)
        hidden = f" · {self._blacklisted} hidden by blacklist" if self._blacklisted else ""
        total = len(self.model.favorites)
        shown = sum(p.id in self.model.favorites for p in self.model.posts)
        what = f"{shown} of {total} favorites" if shown != total else f"{total} favorites"
        self.count_label.setText(f"{what} · {n} selected{hidden}")
        self.dl_btn.setText(f"Download {n}")
        self.together_btn.setEnabled(n >= 2)
        self.slideshow_btn.setEnabled(bool(self.model.posts))
