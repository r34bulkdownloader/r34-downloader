"""The main window: tabs, toolbar, downloads (incl. albums/ZIPs), favorites and followed tags."""
import datetime
import logging
import math
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QKeySequence, QPixmapCache,
)
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QInputDialog, QLabel, QMenu, QToolButton, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSlider, QTabWidget,
    QToolBar, QWidget,
)

from . import api, backup, config, naming, secret, theme
from .downloader import Downloader
from .models import Post
from .dialogs import ExportDialog, SettingsDialog, UnlockDialog
from .gallery import GalleryTab
from .grid import ResultsModel
from .search import FavoritesTab, SearchTab
from .sites import Site, make_sites
from .sites.ehclient import EH_GAP_MS, EH_WARN_PICTURES, EhDownloadFeeder
from .slideshow import Slideshow, SlideshowDialog
from .together import TogetherTab
from .viewer import ViewerTab, resolve_error
from .widgets import PIXMAP_CACHE_KB, THUMB_MAX, THUMB_MIN, THUMB_STEP, expand_dir, snap_thumb

log = logging.getLogger(__name__)

# Kept importable from r34dl.ui (tests and older code refer to them here).
from PySide6.QtGui import QDesktopServices, QMovie, QTextCursor  # noqa: E402,F401
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402,F401
from .widgets import VideoView  # noqa: E402,F401
from PySide6.QtNetwork import QNetworkReply  # noqa: E402,F401
from PySide6.QtWidgets import QListView  # noqa: E402,F401
from .dialogs import LogDialog  # noqa: E402,F401
from .grid import HoverPreview, ResultsView, checkbox_rect  # noqa: E402,F401
from .viewer import TAG_COLORS, SeekSlider, TagList, ZoomView  # noqa: E402,F401
from .widgets import BLACKLIST_SERVER_MAX, CELL_PAD  # noqa: E402,F401

@dataclass
class _ZipJob:
    name: str
    parts: Path  # hidden temporary folder the files download into
    pending: set[str]  # target paths (as text) not finished yet — paths, not urls, so a post also queued
    # elsewhere (a normal download, another ZIP) can't tick this job off
    failed: int = 0


def _write_zip(target: Path, files: list[Path]) -> str | None:
    """Runs on a background thread. Returns an error text, or None when the ZIP is written."""
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as z:  # images/videos are already compressed
            for f in files:
                z.write(f, f.name)
    except OSError as e:
        target.unlink(missing_ok=True)  # no half-written ZIP
        return str(e)
    return None


class MainWindow(QMainWindow):
    zip_written = Signal(object, object, int, object)  # job, target, file count, error (or None)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("R34 Downloader")
        QPixmapCache.setCacheLimit(PIXMAP_CACHE_KB)
        self.resize(1200, 800)
        self.cfg = config.load()
        theme.apply(self.cfg["theme"])
        self.sites: dict[str, Site] = make_sites(self)
        self.api = self.sites["rule34.xxx"].api
        # per site, shared by every search tab's model on that site
        self.downloaded: dict[str, set[int]] = {s: config.load_history(s) for s in self.sites}
        self.favorites: dict[str, dict[int, Post]] = {s: {} for s in self.sites}  # newest first
        for s in self.sites:
            self._load_favorites(config.load_favorites(s), s)
        self._url_ids: dict[str, tuple[str, int]] = {}  # queued file url -> (site, post id), for the history
        self._target_ids: dict[str, tuple[str, int]] = {}  # same for e-hentai pages (url known only later)
        self._zip_jobs: list[_ZipJob] = []
        self._cancels = 0  # bumped by Cancel: gallery listings started before it are dropped
        self._listings: list = []  # stop() of each gallery listing in progress (for Cancel)
        self._eh_failed = 0  # e-hentai pictures that couldn't be resolved (the downloader never saw them)
        self._last_progress = (0, 0, 0, 0)
        self._zip_threads: list[threading.Thread] = []  # ZIPs being packed in the background
        self._opened_from: QWidget | None = None  # the tab the last new tab was opened from…
        self._last_opened: QWidget | None = None  # …and that new tab: the next one from there goes after it
        self.zip_written.connect(self._on_zip_written)
        self.slideshow: Slideshow | None = None  # the running one (one at a time)
        self.downloader = Downloader(self)
        self.feeder = EhDownloadFeeder(self.sites["e-hentai"], self.downloader, self)
        self.feeder.paused.connect(self._on_eh_paused)
        self.feeder.failed.connect(self._on_eh_failed)
        self.feeder.retargeted.connect(self._on_eh_retargeted)
        self.feeder.originals_refused.connect(lambda: self.statusBar().showMessage(
            "e-hentai refused an original picture, so resized copies are downloaded instead. "
            "Check the e-hentai login in Settings.", 30000))
        self.nam = QNetworkAccessManager(self)
        self._build_ui()
        self.downloader.progress.connect(self._on_progress)
        self.downloader.item_done.connect(self._on_item_done)
        self.downloader.item_done.connect(self._album_progress)
        self._vault: secret.Vault | None = None  # unlocked password key (never written anywhere)
        self._eh_login()
        if self.cfg["secret"] or not self.cfg["user_id"] or not self.cfg["api_key"]:
            QTimer.singleShot(0, self, self._startup)

    def _eh_login(self):
        sc = config.site_cfg(self.cfg, "e-hentai")
        self.sites["e-hentai"].queue.set_login(sc["member_id"], sc["pass_hash"])

    def _startup(self):
        """Unlock protected credentials first; ask for credentials if there are none. Runs once."""
        if getattr(self, "_started", False):
            return
        self._started = True
        if self.cfg["secret"]:
            dlg = UnlockDialog(self.cfg["secret"], self)
            r = dlg.exec()
            creds, vault = dlg.creds, dlg.vault
            dlg.deleteLater()
            if r == QDialog.Accepted:  # in memory only
                config.site_cfg(self.cfg, "e-hentai").update(member_id=creds.pop("eh_member_id", ""),
                                                             pass_hash=creds.pop("eh_pass_hash", ""))
                self.cfg.update(creds)
                self._vault = vault
                self._eh_login()
            elif r == UnlockDialog.FORGOT:
                self.cfg.update(secret={}, user_id="", api_key="")
                config.site_cfg(self.cfg, "e-hentai").update(member_id="", pass_hash="")
                self._eh_login()
                try:
                    config.save(self.cfg, unprotect=True)
                except OSError as e:
                    log.error("could not save settings: %s", e)
                log.info("protected credentials removed (forgotten password)")
            else:
                self.close()  # Quit
                return
        if not self.cfg["user_id"] or not self.cfg["api_key"]:
            self.open_settings()

    # ---- construction
    def _build_ui(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        act = QAction("⚙ Settings", self)
        act.triggered.connect(self.open_settings)
        tb.addAction(act)
        new = QAction("＋ New search", self)
        new.setShortcut(QKeySequence("Ctrl+T"))
        new.setToolTip("Open an empty search tab (Ctrl+T)")
        new.triggered.connect(lambda: self.open_search_tab(focus=True))
        tb.addAction(new)
        fav = QAction("♥ Favorites", self)
        fav.setToolTip("Your favorite posts (♥ in a post tab or right-click a thumbnail to add)")
        fav.triggered.connect(self.open_favorites)
        tb.addAction(fav)
        self.saved_btn = QToolButton()
        self.saved_btn.setText("★ Searches")
        self.saved_btn.setToolTip("Followed tags and saved searches")
        self.saved_btn.setPopupMode(QToolButton.InstantPopup)
        self.saved_menu = QMenu(self.saved_btn)
        self.saved_menu.aboutToShow.connect(self._fill_saved_menu)
        self.saved_btn.setMenu(self.saved_menu)
        tb.addWidget(self.saved_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Size "))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(THUMB_MIN, THUMB_MAX)
        self.size_slider.setSingleStep(THUMB_STEP)
        self.size_slider.setPageStep(THUMB_STEP * 2)
        self.size_slider.setMaximumWidth(140)
        self.size_slider.setToolTip("Thumbnail size (Ctrl + mouse wheel over the grid also works)")
        self.size_slider.setValue(max(THUMB_MIN, min(THUMB_MAX, self.cfg["thumb_size"])))
        self.size_slider.valueChanged.connect(self._set_thumb_size)
        tb.addWidget(self.size_slider)
        self.addToolBar(tb)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)  # drag tabs to reorder them
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)
        site = self.cfg["site"] if self.cfg["site"] in self.sites else "rule34.xxx"
        include = config.site_cfg(self.cfg, site)["include"] if self.cfg["remember_include"] else ""
        self.first = SearchTab(self, include, config.site_cfg(self.cfg, site)["exclude"], site)
        self.tabs.addTab(self.first, "Search")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.LeftSide, None)
        self._set_thumb_size(self.size_slider.value())

        sb = self.statusBar()
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(260)
        self.progress_label = QLabel("Downloads: idle")
        self.retry_btn = QPushButton("Retry failed")
        self.retry_btn.clicked.connect(self._retry_failed)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_downloads)
        self.resume_btn = QPushButton("Resume e-hentai downloads")
        self.resume_btn.clicked.connect(self._resume_eh)
        self.resume_btn.hide()
        for w in (self.progress_bar, self.progress_label, self.retry_btn, self.cancel_btn, self.resume_btn):
            sb.addWidget(w)

    # ---- tabs
    def site(self, name: str) -> Site:
        return self.sites[name]

    def search_tabs(self) -> list[SearchTab]:
        return [w for w in map(self.tabs.widget, range(self.tabs.count())) if isinstance(w, SearchTab)]

    def current_search(self) -> SearchTab:
        w = self.tabs.currentWidget()
        return w if isinstance(w, SearchTab) else self.first

    def current_site_search(self) -> SearchTab:
        """The search tab showing, but never Favorites or a gallery (saved searches are site searches)."""
        w = self.current_search()
        return self.first if isinstance(w, (FavoritesTab, GalleryTab)) else w

    def set_tab_title(self, tab: QWidget, title: str):
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, title)
            self.tabs.setTabToolTip(i, title)

    def open_search_tab(self, include: str = "", background: bool = False, focus: bool = False,
                        site: str | None = None) -> SearchTab:
        """New search tab (on the site used last unless given); starts with the remembered exclude list.
        Searches right away if given tags."""
        site = site or (self.cfg["site"] if self.cfg["site"] in self.sites else self.first.site.name)
        exclude = (self.first.exclude_edit.toPlainText() if site == self.first.site.name
                   else config.site_cfg(self.cfg, site)["exclude"])
        tab = SearchTab(self, include, exclude, site)
        self.add_tab(tab, tab.title(), background)
        if include.strip():
            tab.search()
        elif focus:
            tab.include_edit.setFocus()
        return tab

    def open_gallery(self, gallery: Post, background: bool) -> GalleryTab:
        tab = GalleryTab(self, gallery)
        self.add_tab(tab, tab.title(), background)
        tab.search()
        return tab

    def add_tab(self, tab: QWidget, title: str, background: bool = False) -> int:
        """Insert a new tab right after the current one, like a web browser. Several opened from the same tab
        (e.g. Ctrl+click on a few posts) line up in the order they were opened."""
        cur = self.tabs.currentWidget()
        anchor = cur
        if self._opened_from is cur and self._last_opened is not None and self.tabs.indexOf(self._last_opened) >= 0:
            anchor = self._last_opened
        i = self.tabs.insertTab(self.tabs.indexOf(anchor) + 1 if anchor is not None else self.tabs.count(), tab, title)
        self._opened_from, self._last_opened = cur, tab
        if not background:
            self.tabs.setCurrentIndex(i)
        return i

    def resolve(self, post: Post, cb, alive=None):
        """Find the post's file address for viewing, then cb(post, error or None); alive() as in Site.resolve."""
        self.sites[post.site].resolve(post, False, cb, alive)

    def open_viewer(self, post: Post, background: bool, origin: SearchTab | None = None,
                    model: ResultsModel | None = None):
        """model: step through these posts instead of a search's results (a Together tab's selection)."""
        if model is None:
            origin = origin or self.current_search()
            model = origin.model
        tab = ViewerTab(post, model, self.nam, start_muted=self.cfg["start_muted"], loop=self.cfg["loop_videos"],
                        resolve=self.resolve)
        tab.origin = origin
        tab.title_changed.connect(lambda t: self.set_tab_title(tab, t))
        if origin is not None:
            origin.fetch_finished.connect(tab._on_fetch_finished)
        else:
            tab.selected.hide()  # a Together tab's posts aren't a search selection
        tab._update_nav()
        search = origin._include if origin else ""  # the post's search, even if that tab searches again later
        tab.download_requested.connect(lambda p: self.download_posts([p], search))
        tab.is_favorite = lambda pid: pid in self.favorites[tab.post.site]  # the post shown now
        tab.is_followed = lambda tag: self.is_followed(tag, tab.post.site)
        tab.update_favorite()
        tab.favorite_toggled.connect(self.toggle_favorite)
        tab.tag_action.connect(lambda action, tag: self._tag_action(tab.origin, action, tag))
        self.add_tab(tab, post.label, background)

    def start_slideshow(self, tab: SearchTab):
        """Ask for the options, then show the tab's posts (or its ticked ones) full screen."""
        if not tab.model.posts:
            return
        ticked = tab.model.selected_posts()
        dlg = SlideshowDialog(self, config.slideshow_options(self.cfg["slideshow"]), len(ticked))
        if dlg.exec() != SlideshowDialog.Accepted:
            return
        opts = self.cfg["slideshow"] = dlg.options()
        self._save_cfg()
        only = dlg.only_ticked()
        dlg.deleteLater()
        if self.slideshow is not None:
            self.slideshow.close()
        show = self.slideshow = Slideshow(ticked if only else list(tab.model.posts), self.nam, opts,
                                          origin=None if only else tab, resolve=self.resolve)
        search, site = tab._include, tab.site.name  # the tab may switch site while the show runs
        show.is_favorite = lambda pid: pid in self.favorites[site]
        show.favorite_toggled.connect(self.toggle_favorite)
        show.download_requested.connect(lambda p: self.download_posts([p], search))
        show.on_close = self._slideshow_closed
        show.showFullScreen()
        show.activateWindow()

    def _slideshow_closed(self):
        self.slideshow = None

    def open_together(self, posts: list[Post]):
        if len(posts) < 2:
            return
        tab = TogetherTab(posts, self.nam, resolve=self.resolve)
        tab.open_requested.connect(lambda p: self.open_viewer(p, False, None, tab.model))
        self.add_tab(tab, f"Together ({len(posts)})")

    def _tag_action(self, origin: SearchTab, action: str, tag: str):
        target = origin if origin is not None else self.first  # origin is None once its tab closed
        site = target.site.name
        if action in ("search", "search_bg"):
            self.open_search_tab(tag, background=action == "search_bg", site=site)
        elif action == "follow":
            self.follow_tag(tag, site)
        elif action == "unfollow":
            self.unfollow_tag(tag, site)
        elif action in ("include", "exclude"):
            if isinstance(target, GalleryTab):  # its tag boxes are hidden: use a search tab on that site
                target = self.first if self.first.site.name == site else self.open_search_tab(site=site)
            target.add_tag(action, tag)

    def _close_tab(self, i: int):
        if self.tabs.widget(i) is not self.first:  # the first search tab stays (it may be moved anywhere)
            w = self.tabs.widget(i)
            if w is self._last_opened or w is self._opened_from:  # it's deleted soon: add_tab must not use it
                self._opened_from = self._last_opened = None
            self.tabs.removeTab(i)
            # Draw the tab that's now showing *before* tearing the old one down: stopping a stream can take a
            # moment, and until the window repaints, the closed tab's picture stays on screen.
            self.tabs.repaint()
            model = getattr(w, "model", None)  # posts opened from it must stop using its (soon deleted) model
            for v in map(self.tabs.widget, range(self.tabs.count())):
                if isinstance(v, ViewerTab) and model is not None and v.model is model:
                    v.detach()
            w.release()
            w.deleteLater()

    def _set_thumb_size(self, side: int):
        side = snap_thumb(side)
        for tab in self.search_tabs():
            tab.set_thumb_size(side)
        self.cfg["thumb_size"] = side  # saved with the rest of the settings (search, Settings OK, quit)

    def zoom_thumbs(self, direction: int):
        self.size_slider.setValue(self.size_slider.value() + direction * THUMB_STEP)

    # ---- settings
    def _refresh_settings_labels(self):
        for tab in self.search_tabs():
            tab.refresh_settings_labels()

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Save folder", self.cfg["save_dir"])
        if d:
            self.cfg["save_dir"] = d
            self._save_cfg()
            self._refresh_settings_labels()

    def _save_cfg(self) -> bool:
        try:
            config.save(self.cfg)
            return True
        except OSError as e:
            log.error("could not save settings: %s", e)
            QMessageBox.warning(self, "Could not save settings", api.scrub(str(e)))
            return False

    def _store_tags(self):
        """Remember the first tab's tags (include only if the user wants it remembered), for its site."""
        sc = config.site_cfg(self.cfg, self.first.site.name)
        sc["exclude"] = self.first.exclude_edit.toPlainText()
        sc["include"] = self.first.include_edit.toPlainText() if self.cfg["remember_include"] else ""

    def search_started(self, tab: SearchTab):
        if tab is self.first:
            self._store_tags()
            self._save_cfg()

    def open_settings(self):
        self._store_tags()
        cur = self.current_search()
        dlg = SettingsDialog(self.cfg, cur.model.posts[0] if cur.model.posts else None, cur._include, self, self.nam,
                             self._vault, list(self.sites))
        accepted = dlg.exec() == QDialog.Accepted
        request = dlg.import_request
        dlg.deleteLater()  # its password fields go with it
        if request is not None:
            self.import_backup(*request)
            return
        if accepted:
            self.cfg = dlg.result_cfg()  # not config.load(): protected credentials are only in memory
            self._vault = dlg.vault
            self._eh_login()
            self._refresh_settings_labels()
            for tab in self.search_tabs():
                if isinstance(tab, FavoritesTab):
                    tab.search()  # the blacklist may have changed
        for site, ids in self.downloaded.items():  # the dialog may have cleared the history; refill in place
            ids.clear()
            ids.update(config.load_history(site))
        for tab in self.search_tabs():
            tab.refresh_thumbs()  # "sharper thumbnails" may have been switched
            tab.view.viewport().update()

    # ---- backup
    def export_backup(self, parent: QWidget | None = None):
        parent = parent or self
        dlg = ExportDialog(parent)
        if dlg.exec() != QDialog.Accepted:
            return
        history, logins = dlg.history_check.isChecked(), dlg.login_check.isChecked()
        dlg.deleteLater()
        name = f"r34-downloader-backup-{datetime.date.today().isoformat()}.json"
        path, _ = QFileDialog.getSaveFileName(parent, "Export backup", str(Path.home() / name),
                                              "Backup (*.json)")
        if not path:
            return
        data = backup.export_data(self.cfg, history, logins)
        try:
            backup.write(Path(path), data)
        except OSError as e:
            QMessageBox.warning(parent, "Export backup", f"Could not write the backup: {api.scrub(str(e))}")
            return
        n = sum(len(v) for v in data["favorites"].values())
        log.info("exported a backup (%d favorites%s%s)", n, ", history" if history else "", ", logins" if logins else "")
        self.statusBar().showMessage(f"Exported {n} favorites and your settings to {Path(path).name}", 15000)

    def import_backup(self, path: str, mode: str):
        try:
            data = backup.read(Path(path))
            new, notes = backup.import_data(data, self.cfg, mode, protected=bool(self.cfg["secret"]))
            config.save(new)
        except ValueError as e:
            QMessageBox.warning(self, "Import backup", api.scrub(str(e)))
            return
        except OSError as e:
            log.error("could not import a backup: %s", e)
            QMessageBox.warning(self, "Import backup", f"Could not import: {api.scrub(str(e))}")
            return
        self.cfg = new
        theme.apply(self.cfg["theme"])
        self._eh_login()
        self._refresh_settings_labels()
        for site in self.sites:
            self._load_favorites(config.load_favorites(site), site)
            self.downloaded[site].clear()
            self.downloaded[site].update(config.load_history(site))
        for tab in self.search_tabs():
            if isinstance(tab, FavoritesTab):
                tab.search()
            tab.refresh_thumbs()
            tab.view.viewport().update()
        n = sum(len(f) for f in self.favorites.values())
        log.info("imported a backup (%s)", mode)
        QMessageBox.information(self, "Import backup", "\n\n".join(
            [f"Imported ({'merged' if mode == 'merge' else 'replaced'}). You now have {n} favorites."] + notes))

    # ---- followed tags
    def is_followed(self, tag: str, site: str = "rule34.xxx") -> bool:
        return tag.lower() in {t.lower() for t in config.site_cfg(self.cfg, site)["followed"]}

    def follow_tag(self, tag: str, site: str = "rule34.xxx"):
        tag = tag.strip().lstrip("-")
        if tag and not self.is_followed(tag, site):
            config.site_cfg(self.cfg, site)["followed"].append(tag)
            self._save_cfg()
            self.statusBar().showMessage(f"Following {tag} — find it under ★ Searches", 8000)

    def unfollow_tag(self, tag: str, site: str = "rule34.xxx"):
        sc = config.site_cfg(self.cfg, site)
        keep = [t for t in sc["followed"] if t.lower() != tag.lower()]
        if len(keep) != len(sc["followed"]):
            sc["followed"] = keep
            self._save_cfg()

    def ask_follow_tag(self):
        site = self.current_site_search().site
        tag, ok = QInputDialog.getText(self, "Follow a tag", "Tag to follow (use _ instead of spaces):")
        tags = site.split_tags(tag)
        if ok and len(tags) == 1:
            self.follow_tag(tags[0], site.name)
        elif ok and tags:
            QMessageBox.information(self, "Follow a tag", "Please enter one tag. To follow a combination of tags, "
                                    "use ★ Searches → Save current search… instead.")

    # ---- favorites
    def open_favorites(self):
        for i in range(self.tabs.count()):
            if isinstance(self.tabs.widget(i), FavoritesTab):
                self.tabs.setCurrentIndex(i)
                return
        tab = FavoritesTab(self)
        self.add_tab(tab, tab.title())
        tab.search()

    def _load_favorites(self, entries: list[dict], site: str):
        """Refill the site's shared favorites dict in place (every model on that site holds this same object)."""
        posts = {}
        for d in entries:
            try:
                p = Post.from_dict(d)
            except (KeyError, TypeError, ValueError):
                continue  # kept in the file, just not shown
            if p.site != site:
                continue  # hand-edited into the wrong file: it would be looked up in the wrong dict
            posts.setdefault(p.id, p)
        self.favorites[site].clear()
        self.favorites[site].update(posts)

    def toggle_favorite(self, post: Post):
        adding = post.id not in self.favorites[post.site]
        try:  # re-reads the file, so another window's changes are kept
            entries = config.update_favorites(add=post.to_dict() if adding else None,
                                              remove_id=None if adding else post.id, site=post.site)
        except OSError as e:
            log.error("could not save favorites: %s", e)
            QMessageBox.warning(self, "Favorites", f"Could not save favorites: {api.scrub(str(e))}")
            return
        self._load_favorites(entries, post.site)
        for w in map(self.tabs.widget, range(self.tabs.count())):
            if isinstance(w, FavoritesTab):
                w.favorite_changed(post)
            elif isinstance(w, SearchTab) and w.site.name == post.site:
                w.model.mark_changed(post.id)
                if isinstance(w, GalleryTab):
                    w.update_favorite()
            elif isinstance(w, ViewerTab):
                w.update_favorite()

    # ---- saved searches
    def _fill_saved_menu(self):
        m = self.saved_menu
        m.clear()
        site = self.current_site_search().site.name
        sc = config.site_cfg(self.cfg, site)
        m.addSection("Followed tags")
        followed = sc["followed"]
        for tag in sorted(followed, key=str.lower):
            m.addAction(tag.replace("&", "&&"), lambda t=tag: self.open_search_tab(t, site=site))
        if not followed:
            m.addAction("None yet — right-click a tag in a post tab → Follow tag").setEnabled(False)
        m.addAction("Follow a tag…", self.ask_follow_tag)
        if followed:
            unfollow = m.addMenu("Unfollow")
            for tag in sorted(followed, key=str.lower):
                unfollow.addAction(tag.replace("&", "&&"), lambda t=tag: self.unfollow_tag(t, site))
        m.addSection("Saved searches")
        m.addAction("Save current search…", self.save_current_search)
        saved = sc["saved_searches"]
        if not saved:
            m.addAction("No saved searches yet").setEnabled(False)
            return
        for name in sorted(saved, key=str.lower):
            m.addAction(name, lambda n=name: self.load_saved_search(n))
        m.addSeparator()
        delete = m.addMenu("Delete")
        for name in sorted(saved, key=str.lower):
            delete.addAction(name, lambda n=name: self.delete_saved_search(n))

    def save_current_search(self):
        tab = self.current_site_search()
        include, exclude = tab.include_edit.toPlainText(), tab.exclude_edit.toPlainText()
        if not (include.strip() or exclude.strip()):
            QMessageBox.information(self, "Save search", "Type some tags first, then save them as a search.")
            return
        suggestion = " ".join(include.split())[:40]
        name, ok = QInputDialog.getText(self, "Save search", "Name for this search:", text=suggestion)
        name = name.strip()
        if not ok or not name:
            return
        saved = config.site_cfg(self.cfg, tab.site.name)["saved_searches"]
        if name in saved and QMessageBox.question(
            self, "Save search", f"Replace the saved search “{name}”?"
        ) != QMessageBox.Yes:
            return
        saved[name] = {"include": include, "exclude": exclude}
        self._save_cfg()

    def load_saved_search(self, name: str):
        tab = self.current_site_search()
        s = config.site_cfg(self.cfg, tab.site.name)["saved_searches"][name]
        self.tabs.setCurrentWidget(tab)
        tab.include_edit.setPlainText(s["include"])
        tab.exclude_edit.setPlainText(s["exclude"])
        tab.search()

    def delete_saved_search(self, name: str):
        if QMessageBox.question(self, "Delete search", f"Delete the saved search “{name}”?") == QMessageBox.Yes:
            config.site_cfg(self.cfg, self.current_site_search().site.name)["saved_searches"].pop(name, None)
            self._save_cfg()

    # ---- downloads
    def download_posts(self, posts: list[Post], search: str = "", album: Path | None = None, queued=None) -> list[Path]:
        """Queue posts; returns their target paths ([] = nothing queued, or not yet). album: all into that folder,
        numbered. e-hentai galleries are listed into their pages first; queued(targets) is called once the
        targets are known (also with [] when nothing is queued), before any of them can finish."""
        queued = queued or (lambda targets: None)
        if not posts or not self._confirm_eh(posts):
            queued([])
            return []
        if any(p.kind == "gallery" for p in posts):
            self._expand(posts, lambda pages: self._queue_posts(pages, search, album, queued))
            return []  # Cancel while listing: queued is never called (cancel_downloads drops the ZIP job itself)
        return self._queue_posts(posts, search, album, queued)

    @staticmethod
    def _pictures(posts: list[Post]) -> int:
        return sum(int(p.album.get("pages", 0) or 0) if p.kind == "gallery" else 1 for p in posts)

    def _confirm_eh(self, posts: list[Post]) -> bool:
        n = self._pictures([p for p in posts if p.site == "e-hentai"])  # a gallery counts its pages
        if n <= EH_WARN_PICTURES:
            return True
        minutes = math.ceil(n * (2 * EH_GAP_MS) / 60000)
        return QMessageBox.question(
            self, "Download from e-hentai",
            f"Download {n} pictures from e-hentai? This uses your image limit (originals use more) and takes about "
            f"{minutes} minutes at a polite pace.") == QMessageBox.Yes

    def _expand(self, posts: list[Post], cb):
        """Galleries become their pages (listed one gallery at a time, in order); then cb(posts)."""
        todo, out, cancels = list(posts), [], self._cancels
        site = self.sites["e-hentai"]
        sb = self.statusBar()

        def step():
            while todo and todo[0].kind != "gallery":
                out.append(todo.pop(0))
            if not todo:
                if sb.currentMessage().startswith("Listing pages of"):
                    sb.clearMessage()
                return cb(out)
            g = todo.pop(0)
            sb.showMessage(f"Listing pages of {g.label}…")

            running = []  # this listing's stop() while it runs; "done" once it answered

            def listed(pages, err):
                if running and running[-1] in self._listings:  # Cancel already cleared the list
                    self._listings.remove(running[-1])
                running.append("done")
                if cancels != self._cancels:
                    return  # Cancel was pressed meanwhile: queue nothing
                out.extend(pages)
                if err:
                    log.warning("could not list the pages of gallery %s: %s", g.album.get("gid"), err)
                    sb.showMessage(f"Could not list every page of {g.label}: {err}", 15000)
                step()
            stop = site.list_all_pages(g, listed)
            if not running:  # not answered right away
                running.append(stop)
                self._listings.append(stop)
        step()

    def _queue_posts(self, posts: list[Post], search: str, album: Path | None, queued) -> list[Path]:
        base = album or expand_dir(self.cfg["save_dir"])

        def template(p: Post) -> str:
            t = config.site_cfg(self.cfg, p.site)["template"]
            return naming.album_template(t) if album else t
        try:
            targets = [naming.target_path(base, template(p), p, search, naming.index_text(i, len(posts)))
                       for i, p in enumerate(posts)]
        except ValueError as e:
            QMessageBox.critical(self, "Bad filename template", api.scrub(str(e)))
            targets = []
        queued(targets)
        if not targets:
            return []
        plain = [(p.file_url, t) for p, t in zip(posts, targets) if p.site != "e-hentai"]
        eh = [(p, t) for p, t in zip(posts, targets) if p.site == "e-hentai"]  # resolved one by one, paced
        self._url_ids.update((p.file_url, (p.site, p.id)) for p in posts if p.site != "e-hentai")
        self._target_ids.update((str(t), (p.site, p.id)) for p, t in eh)
        if plain:
            self.downloader.add(plain)
        if eh:
            self.feeder.add(eh)
        return targets

    def _on_eh_paused(self, msg: str):
        self.statusBar().showMessage(resolve_error(msg))
        self.resume_btn.show()

    def _resume_eh(self):
        self.resume_btn.hide()
        self.statusBar().clearMessage()
        self.feeder.resume()

    def _on_eh_retargeted(self, old: str, new: str):
        if old in self._target_ids:
            self._target_ids[new] = self._target_ids.pop(old)
        for job in self._zip_jobs:
            if old in job.pending:
                job.pending.discard(old)
                job.pending.add(new)

    def _on_eh_failed(self, page: str, target: str):
        self._eh_failed += 1
        self._album_progress(page, target, False)  # a ZIP still completes
        self._on_progress(*self._last_progress)

    # ---- albums: download as one folder or one ZIP
    @staticmethod
    def album_name_suggestion(posts: list[Post], search: str) -> str:
        titles = {str(p.album.get("title", "")) for p in posts}
        if len(titles) == 1 and (title := naming.clean_name(titles.pop())):  # one e-hentai gallery
            return title
        shared = set.intersection(*(set(p.tags_of("artist")) for p in posts)) if posts else set()
        if shared:
            return "+".join(sorted(shared))
        return "_".join(api.split_tags(search))[:60] or "album"

    def download_album(self, posts: list[Post], search: str, zipped: bool):
        if not posts:
            QMessageBox.information(self, "Download as…", "Tick the posts you want first.")
            return
        what = "ZIP" if zipped else "folder"
        name, ok = QInputDialog.getText(self, f"Download as {what}",
                                        f"Name for the {what} ({self._pictures(posts)} posts):",
                                        text=self.album_name_suggestion(posts, search))
        if not ok:
            return
        name = naming.clean_name(name)
        if not name:
            QMessageBox.information(self, f"Download as {what}", "Please type a name with letters or numbers.")
            return
        save_dir = expand_dir(self.cfg["save_dir"])
        if not zipped:
            self.download_posts(posts, search, album=save_dir / name)
            return
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            parts = Path(tempfile.mkdtemp(prefix=".r34-zip-parts-", dir=save_dir))  # short: any name length fits
        except OSError as e:
            QMessageBox.warning(self, "Download as ZIP", api.scrub(str(e)))
            return
        job = _ZipJob(name, parts, set())
        self._zip_jobs.append(job)

        def queued(targets):  # before queueing: an item can finish (e.g. "already there") right away
            job.pending = {str(t) for t in targets}
            if not job.pending and job in self._zip_jobs:
                self._zip_jobs.remove(job)
                shutil.rmtree(parts, ignore_errors=True)
        self.download_posts(posts, search, album=parts, queued=queued)

    def _album_progress(self, url: str, target: str, ok: bool):
        for job in list(self._zip_jobs):
            if target in job.pending:
                job.pending.discard(target)
                job.failed += not ok
                if not job.pending:
                    self._finish_zip(job)

    def _finish_zip(self, job: _ZipJob):
        self._zip_jobs.remove(job)
        files = sorted(f for f in job.parts.rglob("*") if f.is_file() and not f.name.endswith(".part"))
        failed = f", {job.failed} failed (Settings → Show log… has details)" if job.failed else ""
        if not files:
            shutil.rmtree(job.parts, ignore_errors=True)
            self._forget_failed(job.parts)
            self.statusBar().showMessage(f"ZIP “{job.name}” not created: nothing downloaded{failed}", 15000)
            return
        target = naming.unique_path(job.parts.parent / f"{job.name}.zip", None)
        self.statusBar().showMessage(f"Packing {target.name}…")

        def work():  # a big album takes seconds to pack: keep the window responsive meanwhile
            self.zip_written.emit(job, target, len(files), _write_zip(target, files))  # delivered on the UI thread

        t = threading.Thread(target=work, name=f"zip {job.name}")
        self._zip_threads.append(t)
        t.start()

    def _on_zip_written(self, job: _ZipJob, target: Path, files: int, error: str | None):
        self._zip_threads = [t for t in self._zip_threads if t.is_alive()]  # forget finished ones
        failed = f", {job.failed} failed (Settings → Show log… has details)" if job.failed else ""
        if error is not None:
            log.error("could not write %s: %s", target, error)
            QMessageBox.warning(self, "Download as ZIP", f"Could not write the ZIP: {api.scrub(error)}\n"
                                f"The downloaded files are kept in {job.parts}")
            return
        shutil.rmtree(job.parts, ignore_errors=True)
        self._forget_failed(job.parts)  # "Retry failed" can't refill a ZIP that's already written
        log.info("saved %s (%d files%s)", target.name, files, f", {job.failed} failed" if job.failed else "")
        self.statusBar().showMessage(f"Saved {target.name}: {files} files{failed}", 15000)

    def _retry_failed(self):
        self._eh_failed = 0
        self.feeder.retry_failed()  # first: takes its pictures out of the downloader's failed list
        self.downloader.retry_failed()
        self._on_progress(*self._last_progress)

    def _forget_failed(self, parts: Path):
        self.downloader.forget_failed(parts)
        self.feeder.forget_failed(parts)

    def cancel_downloads(self):
        self._cancels += 1
        for stop in self._listings:
            stop()
        if self._listings and self.statusBar().currentMessage().startswith("Listing pages of"):
            self.statusBar().clearMessage()
        self._listings.clear()
        self.feeder.cancel()
        self.resume_btn.hide()
        self._eh_failed = 0
        self.downloader.cancel()
        for job in self._zip_jobs:
            shutil.rmtree(job.parts, ignore_errors=True)
            self._forget_failed(job.parts)
        self._zip_jobs.clear()

    def _on_item_done(self, url: str, target: str, ok: bool):
        site_id = (self._url_ids.get(url) or self._target_ids.get(target)) if ok else None
        if site_id is None or site_id[1] in self.downloaded[site_id[0]]:
            return
        site, post_id = site_id
        try:
            config.add_history(post_id, site)
        except OSError as e:  # the mark still shows this session; history just won't remember it
            log.warning("could not write download history: %s", e)
        self.downloaded[site].add(post_id)  # also when no tab on that site is open
        for tab in self.search_tabs():
            if tab.site.name == site:
                tab.model.mark_downloaded(post_id)

    def _on_progress(self, done: int, total: int, active: int, failed: int):
        self._last_progress = (done, total, active, failed)
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(done)
        waiting = f" · {self.feeder.waiting} e-hentai waiting" if self.feeder.waiting else ""
        self.progress_label.setText(f"{done}/{total} · {active} active · {failed + self._eh_failed} failed{waiting}")

    def closeEvent(self, e):
        if self.downloader.active or self.feeder.active or self._listings or self._zip_jobs:
            r = QMessageBox.question(self, "Quit", "Downloads are in progress. Cancel them and quit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.cancel_downloads()  # also removes unfinished ZIPs' temporary folders
        if any(t.is_alive() for t in self._zip_threads):
            self.statusBar().showMessage("Finishing the ZIP before quitting…")
            for t in self._zip_threads:
                t.join()  # never leave a half-written ZIP behind
        if self.slideshow is not None:
            self.slideshow.close()
        for i in range(self.tabs.count()):
            fs = getattr(self.tabs.widget(i), "_fs", None)
            if fs is not None:
                fs.close()  # a post shown full screen
        self._store_tags()
        try:
            config.save(self.cfg)
        except OSError as err:
            log.error("could not save settings on quit: %s", err)
        e.accept()
