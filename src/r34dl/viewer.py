"""Single-post tabs: zoomable image, video/GIF playback, grouped tag list, Previous/Next."""
import logging
import threading

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QImage, QImageReader, QKeySequence, QMovie, QPainter, QPalette, QPixmap,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMenu, QSplitter, QPushButton, QSizePolicy,
    QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget,
)

from . import api
from .models import TAG_KINDS, Post
from .sites.ehclient import CANCELLED
from .grid import ResultsModel
from .theme import readable
from .widgets import DETAIL_MAX_PIXELS, VIDEO_EXT, FullscreenWindow, VideoView, hint_label, open_in_browser

log = logging.getLogger(__name__)
IMAGE_LIMIT_TEXT = ("e-hentai's image limit is reached. Try again later, or log in in Settings "
                    "for a higher limit.")


def resolve_error(err: str) -> str:
    return IMAGE_LIMIT_TEXT if err == "IMAGE_LIMIT" else err


def known(post: Post, cb, alive=None):
    cb(post, None)  # resolve=None: file_url is already known


class ZoomView(QGraphicsView):
    """A still image: fits the window; the mouse wheel zooms toward the pointer, drag to pan, double-click resets.
    Limits: never smaller than "whole picture visible", never more than MAX_ZOOM x the image's real size."""

    zoom_changed = Signal(int)  # percent of the image's real size
    needs_detail = Signal()  # zoomed past the screen-sized copy: a sharper decode would help

    MAX_ZOOM = 4.0  # 400%
    STEP = 1.25  # per wheel notch

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self._item)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)  # wheelEvent anchors at the pointer itself
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        # No scrollbars: drag to pan. (Bars appearing mid-zoom would shrink the view and nudge the picture.)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setFrameShape(QFrame.NoFrame)
        self.setAlignment(Qt.AlignCenter)
        self._ratio = 1.0  # original image pixels per scene pixel (the first copy is screen-sized)
        self._fitted = True
        self._detailed = True

    def set_image(self, pm: QPixmap, original_width: int):
        self._item.setPixmap(pm)
        self._item.setScale(1.0)
        self.scene().setSceneRect(self._item.boundingRect())
        self._ratio = max(1.0, original_width / pm.width()) if pm.width() else 1.0
        self._detailed = self._ratio <= 1.0
        self.reset_zoom()

    def set_detail(self, pm: QPixmap):
        """Swap in a sharper copy of the same picture; what's on screen doesn't move."""
        if pm.width():
            self._item.setPixmap(pm)
            self._item.setScale(self.sceneRect().width() / pm.width())

    def clear(self):
        self._item.setPixmap(QPixmap())

    def scale_now(self) -> float:
        return self.transform().m11()

    def fit_scale(self) -> float:
        r = self.sceneRect()
        if r.width() <= 0 or r.height() <= 0:
            return 1.0
        vp = self.viewport().size()
        return min(vp.width() / r.width(), vp.height() / r.height())

    def limits(self) -> tuple[float, float]:
        lo = self.fit_scale()
        return lo, max(lo, self.MAX_ZOOM * self._ratio)

    def zoom_percent(self) -> int:
        return round(100 * self.scale_now() / self._ratio)

    def reset_zoom(self):
        self.resetTransform()
        s = self.fit_scale()
        self.scale(s, s)
        self._fitted = True
        self.zoom_changed.emit(self.zoom_percent())

    def wheelEvent(self, e):
        notches = e.angleDelta().y() / 120 or e.pixelDelta().y() / 120
        if not notches or self._item.pixmap().isNull():
            return super().wheelEvent(e)
        lo, hi = self.limits()
        cur = self.scale_now()
        new = max(lo, min(hi, cur * self.STEP ** notches))
        e.accept()
        if abs(new - cur) < 1e-9:
            return
        pos = e.position().toPoint()
        anchor = self.mapToScene(pos)  # the picture point under the pointer stays under the pointer
        self.scale(new / cur, new / cur)
        moved = self.mapFromScene(anchor) - pos
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self._fitted = abs(new - lo) < 1e-9
        if not self._detailed and new > 1.0:
            self._detailed = True
            self.needs_detail.emit()
        self.zoom_changed.emit(self.zoom_percent())

    def mouseDoubleClickEvent(self, e):
        self.reset_zoom()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fitted:
            self.reset_zoom()  # keep "whole picture" when the window changes size
            return
        lo, hi = self.limits()  # zoomed: stay inside the limits for the new window size
        cur = self.scale_now()
        new = max(lo, min(hi, cur))
        if new != cur:
            self.scale(new / cur, new / cur)
            self._fitted = abs(new - lo) < 1e-9
            self.zoom_changed.emit(self.zoom_percent())


class SeekSlider(QSlider):
    """Clicking anywhere on the bar jumps there (Qt's default only moves one page step); dragging still works."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle.contains(e.position().toPoint()):
                groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
                x = int(e.position().x()) - groove.x() - handle.width() // 2
                span = groove.width() - handle.width()
                self.setSliderPosition(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span))
                self.triggerAction(QSlider.SliderMove)  # -> actionTriggered -> player seeks
        super().mousePressEvent(e)  # handle is now under the cursor, so a drag continues from here


TAG_COLORS = {  # like the site's sidebar; readable on light and dark backgrounds
    "copyright": QColor("#c13fc1"), "character": QColor("#2f9e44"), "artist": QColor("#d63c3c"),
    "general": QColor("#3d7fd9"), "meta": QColor("#d9822b"), "other": QColor("#888888"),
}
TAG_HEADINGS = {"copyright": "Copyright", "character": "Character", "artist": "Artist",
                "general": "General", "meta": "Meta", "other": "Other"}


class TagList(QListWidget):
    """The post's tags grouped by type like the site (Copyright, Character, Artist, General, Meta), coloured,
    with post counts. Click a tag = search it in a new tab (Ctrl/middle-click: in the background)."""

    activated_tag = Signal(str, bool)  # tag, background

    _post: Post | None = None

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.PaletteChange and self._post is not None:
            self.set_post(self._post)  # the theme changed: recolour the tags for the new background

    def set_post(self, post: Post):
        self._post = post
        self.clear()
        if not post.tag_info:  # the site sent no types: plain list
            for t in post.tags:
                self._add_tag(t, None, None)
            return
        bold = QFont(self.font())
        bold.setBold(True)
        known = set(post.tag_info)
        for kind in TAG_KINDS:
            tags = post.tags_of(kind) + (sorted(set(post.tags) - known) if kind == "other" else [])
            if not tags:
                continue
            head = QListWidgetItem(TAG_HEADINGS[kind])
            head.setFont(bold)
            head.setFlags(Qt.ItemIsEnabled)  # a heading: readable, but not selectable or a tag
            self.addItem(head)
            color = readable(TAG_COLORS[kind], self.palette().color(QPalette.Base))  # on any theme
            for t in tags:
                self._add_tag(t, color, post.tag_info.get(t, ("other", None))[1])

    def _add_tag(self, tag: str, color: QColor | None, count: int | None):
        # e-hentai sends no post counts (0): show none rather than "0"
        item = QListWidgetItem(f"  {tag}  {count:,}" if count else f"  {tag}" if color is not None else tag)
        item.setData(Qt.UserRole, tag)
        if color is not None:
            item.setForeground(color)
        self.addItem(item)

    def tag_at(self, pos) -> str | None:
        item = self.itemAt(pos)
        return item.data(Qt.UserRole) if item is not None else None

    def tags(self) -> list[str]:
        return [t for t in (self.item(i).data(Qt.UserRole) for i in range(self.count())) if t]

    def mouseReleaseEvent(self, e):
        tag = self.tag_at(e.position().toPoint())
        if tag and e.button() in (Qt.LeftButton, Qt.MiddleButton):
            bg = e.button() == Qt.MiddleButton or bool(e.modifiers() & Qt.ControlModifier)
            self.activated_tag.emit(tag, bg)
        super().mouseReleaseEvent(e)


class ViewerTab(QWidget):
    """One post at a time; ◀/▶ (or the arrow keys) step through the results of the search it came from."""

    download_requested = Signal(object)  # Post
    favorite_toggled = Signal(object)  # Post
    decoded = Signal(int, object, bool)  # load id, QImage or None, is the sharper zoom copy (background decode)
    tag_action = Signal(str, str)  # action ("search", "search_bg", "include", "exclude", "follow", "unfollow"), tag
    title_changed = Signal(str)
    is_favorite = staticmethod(lambda post_id: False)  # set by the window
    is_followed = staticmethod(lambda tag: False)  # set by the window

    def __init__(
        self, post: Post, model: ResultsModel, nam: QNetworkAccessManager, start_muted: bool = True,
        loop: bool = True, resolve=None,
    ):
        super().__init__()
        self._resolve = resolve or known
        self.post = post
        self.model = model
        self._nam = nam
        self._start_muted = start_muted
        self._loop = loop
        self.origin = None  # the SearchTab this post was opened from; None once that tab is closed
        self._reply: QNetworkReply | None = None
        self._raw = QByteArray()  # the downloaded image file, for a sharper decode when zooming in
        self._load_id = 0  # bumped per shown post: late background decodes for an older post are ignored
        self.decoded.connect(self._on_decoded)
        self._movie: QMovie | None = None
        self._buf: QBuffer | None = None
        self._player: QMediaPlayer | None = None
        self._audio: QAudioOutput | None = None
        self._released = False
        self._want_play = True
        self._pending_next = False  # Next pressed at the end of loaded results; step once more arrive
        self._fs: FullscreenWindow | None = None  # while full screen: the window borrowing stage and controls

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split)
        self.tag_list = TagList()
        self.tag_list.setToolTip("Click a tag to search it in a new tab · Ctrl+click: in the background · "
                                 "right-click for more")
        self.tag_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tag_list.customContextMenuRequested.connect(self._tag_menu)
        self.tag_list.activated_tag.connect(lambda t, bg: self.tag_action.emit("search_bg" if bg else "search", t))
        split.addWidget(self.tag_list)
        body = QWidget()
        lay = self._body = QVBoxLayout(body)
        split.addWidget(body)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 1000])
        self.stage = QWidget()  # the picture/GIF/video; moves into the full-screen window and back
        st = QVBoxLayout(self.stage)
        st.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stage, 1)
        self.media = QLabel("Loading…")
        self.media.setAlignment(Qt.AlignCenter)
        self.media.setMinimumSize(64, 64)
        self.media.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        st.addWidget(self.media, 1)
        self.zoom_view = ZoomView()
        self.zoom_view.hide()
        self.zoom_view.needs_detail.connect(self._load_detail)
        self.zoom_view.zoom_changed.connect(lambda pct: self.zoom_label.setText(f"{pct}%"))
        st.addWidget(self.zoom_view, 1)
        self.video = VideoView()
        self.video.hide()
        st.addWidget(self.video, 1)
        self.zoom_bar = QWidget()
        zb = QHBoxLayout(self.zoom_bar)
        zb.setContentsMargins(0, 0, 0, 0)
        zb.addWidget(hint_label("Mouse wheel: zoom where the pointer is · drag: move · double-click: reset"), 1)
        self.zoom_label = QLabel("100%")
        self.zoom_reset_btn = QPushButton("Reset zoom")
        self.zoom_reset_btn.clicked.connect(self.zoom_view.reset_zoom)
        zb.addWidget(self.zoom_label)
        zb.addWidget(self.zoom_reset_btn)
        self.zoom_bar.hide()
        lay.addWidget(self.zoom_bar)
        self.controls = QWidget()
        cl = QHBoxLayout(self.controls)
        cl.setContentsMargins(0, 0, 0, 0)
        self.play_btn = QPushButton("Pause")
        self.seek = SeekSlider(Qt.Horizontal)
        self.mute_btn = QPushButton("Unmute" if start_muted else "Mute")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setMaximumWidth(100)
        self.volume.setToolTip("Volume")
        for w in (self.play_btn, self.seek, self.mute_btn, self.volume):
            cl.addWidget(w)
        self.controls.hide()
        lay.addWidget(self.controls)
        self.play_btn.clicked.connect(self._toggle_play)
        self.mute_btn.clicked.connect(self._toggle_mute)
        self.volume.valueChanged.connect(lambda v: self._audio and self._audio.setVolume(v / 100))
        self.seek.actionTriggered.connect(lambda _a: self._player and self._player.setPosition(self.seek.sliderPosition()))

        self.info = QLabel()
        lay.addWidget(self.info)
        self.prev_btn = QPushButton("◀ Previous")
        self.next_btn = QPushButton("Next ▶")
        self.prev_btn.setToolTip("Previous post in this search (Left arrow)")
        self.next_btn.setToolTip("Next post in this search (Right arrow)")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        self.selected = QCheckBox("Selected")
        self.selected.toggled.connect(self._toggle_selected)
        self.browser_btn = QPushButton("Open in browser")
        self.browser_btn.setToolTip("Open this post's page on its site in your web browser")
        self.browser_btn.clicked.connect(lambda: open_in_browser(self.post))
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(lambda: self.download_requested.emit(self.post))
        self.fullscreen_btn = QPushButton("⛶ Fullscreen")
        self.fullscreen_btn.setToolTip("Show this post full screen (F, or double-click a video/GIF) · Esc leaves")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        actions = QHBoxLayout()
        actions.addWidget(self.prev_btn)
        actions.addWidget(self.next_btn)
        actions.addSpacing(12)
        actions.addWidget(self.selected)
        actions.addStretch(1)
        actions.addWidget(self.fullscreen_btn)
        self.fav_btn = QPushButton("♡ Favorite")
        self.fav_btn.setToolTip("Add this post to (or remove it from) your favorites")
        self.fav_btn.clicked.connect(lambda: self.favorite_toggled.emit(self.post))
        actions.addWidget(self.fav_btn)
        actions.addWidget(self.browser_btn)
        actions.addWidget(self.download_btn)
        # Space/arrows belong to this tab, so buttons shouldn't take keyboard focus and swallow them.
        for w in (self.play_btn, self.mute_btn, self.selected, self.browser_btn, self.download_btn, self.fav_btn,
                  self.prev_btn, self.next_btn, self.seek, self.volume, self.tag_list, self.zoom_reset_btn,
                  self.zoom_view, self.fullscreen_btn):
            w.setFocusPolicy(Qt.NoFocus)
        lay.addLayout(actions)
        model.dataChanged.connect(self._sync_selected)
        model.modelReset.connect(self._sync_selected)
        model.rowsInserted.connect(self._on_rows_inserted)
        # Click on the video/GIF, or press Space, to pause/play; arrows step through the search.
        self.media.installEventFilter(self)
        self.video.viewport().installEventFilter(self)  # clicks land on the view's viewport
        for key, slot in ((Qt.Key_Space, self.toggle_playback), (Qt.Key_Left, lambda: self.step(-1)),
                          (Qt.Key_Right, lambda: self.step(1)), (Qt.Key_F, self.toggle_fullscreen)):
            QShortcut(QKeySequence(key), self, activated=slot, context=Qt.WidgetWithChildrenShortcut)
        self.show_post(post)

    # ---- full screen
    def toggle_fullscreen(self):
        if self._fs is not None:
            self._fs.close()
            return
        fs = self._fs = FullscreenWindow()
        bar = QWidget(fs)
        fs.overlay_style(bar)
        bv = QVBoxLayout(bar)
        bv.addWidget(self.controls)  # the video controls (hidden for pictures, as in the tab)
        row = QHBoxLayout()
        for text, tip, fn in (("◀", "Previous (Left arrow)", lambda: self.step(-1)),
                              ("▶", "Next (Right arrow)", lambda: self.step(1))):
            b = QPushButton(text, bar)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.fs_info = QLabel(self.info.text(), bar)
        row.addWidget(self.fs_info, 1)
        leave = QPushButton("Exit fullscreen", bar)
        leave.setToolTip("Esc or F")
        leave.setFocusPolicy(Qt.NoFocus)
        leave.clicked.connect(fs.close)
        row.addWidget(leave)
        bv.addLayout(row)
        fs.bar = bar
        fs.set_stage(self.stage)
        fs.key(("Esc", "F"), fs.close)
        fs.key("Space", lambda: (self.toggle_playback(), fs.poke()))
        fs.key("Left", lambda: self.step(-1))  # show_post pokes
        fs.key("Right", lambda: self.step(1))
        fs.on_close = self._fullscreen_closed
        fs.showFullScreen()
        fs.activateWindow()

    def _fullscreen_closed(self):
        """Give the media and controls back to the tab, where they were."""
        self._fs = None
        self._body.insertWidget(0, self.stage, 1)
        self._body.insertWidget(self._body.indexOf(self.zoom_bar) + 1, self.controls)

    # ---- which post
    def show_post(self, post: Post):
        """Show another post in this same tab (used by Previous/Next)."""
        self._clear_media()
        self._released = False
        self.post = post
        self.tag_list.set_post(post)
        if post.site == "e-hentai":
            a = post.album
            self.info.setText(f"{a.get('title', '')} · page {a.get('page', '?')} of {a.get('pages', '?')} · "
                              f"{a.get('category', '')}")
        else:
            self.info.setText(f"{post.label} · {post.width}×{post.height} · score {post.score} · rating {post.rating}")
        if self._fs is not None:
            self.fs_info.setText(self.info.text())
            QTimer.singleShot(0, self._fs, self._fs.poke)  # after the controls show/hide: re-fit the bar
        self._sync_selected()
        self.update_favorite()
        self.title_changed.emit(post.label)
        token = self._load_id
        self._resolve(post, lambda p, err: self._on_resolved(token, p, err),
                      alive=lambda: token == self._load_id and not self._released and post is self.post)

    def _on_resolved(self, token: int, post: Post, err: str | None):
        if token != self._load_id or self._released or post is not self.post:
            return  # another post is showing now
        if err == CANCELLED:
            return  # silent: the request was dropped on purpose
        if err:
            self._show_error(resolve_error(err))
        elif post.ext in VIDEO_EXT:
            self._start_video()
        else:
            req = api.request(post.file_url)
            self._reply = self._nam.get(req)
            self._reply.finished.connect(self._on_reply)

    def update_favorite(self):
        fav = self.is_favorite(self.post.id)
        self.fav_btn.setText("♥ Favorited" if fav else "♡ Favorite")

    def _row(self) -> int | None:
        return None if self.model is None else self.model._rows.get(self.post.id)

    def step(self, delta: int):
        row = self._row()
        if row is None:
            return
        new = row + delta
        if 0 <= new < len(self.model.posts):
            self.show_post(self.model.posts[new])
        elif delta > 0 and self.origin is not None and self.origin.can_load_more():
            self._pending_next = True  # continue once the next page arrives
            self.next_btn.setText("Loading…")
            self.origin.load_more()

    def _on_rows_inserted(self, *_):
        self._sync_selected()
        if self._pending_next:
            self._pending_next = False
            self.next_btn.setText("Next ▶")
            if self._row() is not None:
                self.step(1)
            elif self.model.posts:  # a new page replaced the old one (page mode): start at its first post
                self.show_post(self.model.posts[0])

    def _on_fetch_finished(self, ok: bool):
        """The search answered while Next was waiting, but no new post appeared (page all hidden, failed...)."""
        if not self._pending_next or self.model is None:
            return
        if self._row() is None and self.model.posts:  # page mode: a new page replaced the grid
            self._pending_next = False
            self.next_btn.setText("Next ▶")
            self.show_post(self.model.posts[0])
        elif ok and self.origin is not None and self.origin.can_load_more():
            self.origin.load_more()  # that page added nothing visible: keep going (requests stay rate-limited)
        else:
            self._pending_next = False
            self.next_btn.setText("Next ▶")
            self._update_nav()

    def _update_nav(self):
        row = self._row()
        has_more = self.origin is not None and self.origin.can_load_more()
        self.prev_btn.setEnabled(row is not None and row > 0)
        self.next_btn.setEnabled(row is not None and (row + 1 < len(self.model.posts) or has_more))

    def _tag_menu(self, pos):
        tag = self.tag_list.tag_at(pos)
        if tag:
            m = self.make_tag_menu(tag)
            m.exec(self.tag_list.mapToGlobal(pos))
            m.deleteLater()

    def make_tag_menu(self, tag: str) -> QMenu:
        m = QMenu(self)
        m.addAction("Search in new tab", lambda: self.tag_action.emit("search", tag))
        m.addAction("Search in background tab", lambda: self.tag_action.emit("search_bg", tag))
        m.addSeparator()
        m.addAction("Add to include", lambda: self.tag_action.emit("include", tag))
        m.addAction("Add to exclude", lambda: self.tag_action.emit("exclude", tag))
        m.addSeparator()
        m.addAction("Copy", lambda: QGuiApplication.clipboard().setText(tag))
        m.addSeparator()
        if self.is_followed(tag):
            m.addAction("Unfollow tag", lambda: self.tag_action.emit("unfollow", tag))
        else:
            m.addAction("Follow tag", lambda: self.tag_action.emit("follow", tag))
        return m

    # ---- selection mirror
    def detach(self):
        """The search tab this post came from was closed: stop mirroring its selection."""
        for sig, slot in ((self.model.dataChanged, self._sync_selected), (self.model.modelReset, self._sync_selected),
                          (self.model.rowsInserted, self._on_rows_inserted)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.model = None
        self.origin = None
        self._pending_next = False
        self.selected.setEnabled(False)
        self.selected.setToolTip("The search this post came from was closed")
        self._update_nav()

    def _toggle_selected(self, on: bool):
        row = self._row()
        if row is not None:
            self.model.setData(self.model.index(row), Qt.Checked if on else Qt.Unchecked, Qt.CheckStateRole)

    def _sync_selected(self, *_):
        self._update_nav()
        if self.model is None:
            return
        self.selected.blockSignals(True)
        self.selected.setChecked(self.post.id in self.model.checked)
        self.selected.blockSignals(False)
        self.selected.setEnabled(self.post.id in self.model._rows)

    # ---- media
    def eventFilter(self, obj, e):
        t = e.type()
        if t == e.Type.MouseButtonRelease and e.button() == Qt.LeftButton:
            self.toggle_playback()
        elif t == e.Type.MouseButtonDblClick and e.button() == Qt.LeftButton:
            self.toggle_fullscreen()  # its two releases pause and resume: playback carries on
        elif t == e.Type.Resize and obj is self.media:
            self._fit_movie()  # also when the media moves to or from the full-screen window
        return False

    def toggle_playback(self):
        """Pause/resume a video or GIF; does nothing for still images."""
        if self._player is not None and self.post.ext in VIDEO_EXT:
            self._toggle_play()
        elif self._movie is not None:
            self._movie.setPaused(self._movie.state() == QMovie.Running)

    def _show_error(self, text: str):
        log.warning("post #%s: %s", self.post.id, text)
        self.video.hide()
        self.controls.hide()
        self.media.show()
        self.media.setPixmap(QPixmap())
        self.media.setText(api.scrub(text))

    def _on_reply(self):
        reply, self._reply = self._reply, None
        if reply is None or self._released:
            return
        err = reply.error()
        data = reply.readAll() if err == QNetworkReply.NoError else QByteArray()
        msg = reply.errorString()
        reply.deleteLater()
        if err != QNetworkReply.NoError:
            self._show_error(f"Could not load media: {msg}")
        elif self.post.ext == "gif":
            self._buf = QBuffer(self)
            self._buf.setData(data)
            self._buf.open(QIODevice.ReadOnly)
            self._movie = QMovie(self._buf, QByteArray(), self)
            if not self._movie.isValid():
                self._show_error("Could not decode GIF")
                return
            self.media.setMovie(self._movie)
            self._movie.start()
            self._fit_movie()
        else:
            self._raw = data  # kept until a sharper zoom copy is made
            self._decode_in_background(data, self._screen_side(), None, detail=False)

    def _load_detail(self):
        """Zoomed in past the screen-sized copy: decode the same file sharper (capped to bound memory)."""
        if not self._raw.isEmpty():
            self._decode_in_background(self._raw, None, DETAIL_MAX_PIXELS, detail=True)

    @staticmethod
    def _image_size(data) -> QSize:
        buf = QBuffer()
        buf.setData(data)
        buf.open(QIODevice.ReadOnly)
        return QImageReader(buf).size()

    @staticmethod
    def _screen_side() -> int | None:
        screen = QGuiApplication.primaryScreen()
        return max(screen.size().width(), screen.size().height()) if screen is not None else None

    @staticmethod
    def _decode_limited(data, max_side: int | None = None, max_pixels: int | None = None) -> QPixmap | None:
        """Decode downscaled (never upscaled): longest side <= max_side (default: the screen's longest side),
        and/or width*height <= max_pixels."""
        if max_side is None and max_pixels is None:
            max_side = ViewerTab._screen_side()
        img = ViewerTab._decode_image(data, max_side, max_pixels)
        return None if img is None else QPixmap.fromImage(img)

    @staticmethod
    def _decode_image(data, max_side: int | None, max_pixels: int | None) -> QImage | None:
        """The decoding itself. Safe on a background thread (QImage only, no screen or widget access)."""
        buf = QBuffer()
        buf.setData(data)
        buf.open(QIODevice.ReadOnly)
        reader = QImageReader(buf)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            factor = 1.0
            if max_side:
                factor = min(factor, max_side / max(size.width(), size.height()))
            if max_pixels:
                factor = min(factor, (max_pixels / max(1, size.width() * size.height())) ** 0.5)
            if factor < 1.0:
                reader.setScaledSize(QSize(max(1, int(size.width() * factor)), max(1, int(size.height() * factor))))
        img = reader.read()
        return None if img.isNull() else img

    def _decode_in_background(self, data: QByteArray, max_side: int | None, max_pixels: int | None, detail: bool):
        """Big images take a moment to decode: do it off the UI thread; the result comes back via decoded."""
        token = self._load_id

        def work():
            img = ViewerTab._decode_image(data, max_side, max_pixels)
            try:
                self.decoded.emit(token, img, detail)  # delivered on the UI thread
            except RuntimeError:
                pass  # the tab was closed meanwhile

        threading.Thread(target=work, name=f"decode #{self.post.id}", daemon=True).start()

    def _on_decoded(self, token: int, img, detail: bool):
        if token != self._load_id or self._released:
            return  # another post is showing now
        if img is None:
            if not detail:
                self._show_error("Could not decode image")
            return
        pm = QPixmap.fromImage(img)
        if detail:
            self.zoom_view.set_detail(pm)
            self._raw = QByteArray()  # the sharper copy is made; the file isn't needed any more
            return
        self.media.hide()
        self.zoom_view.show()
        self.zoom_bar.show()
        self.zoom_view.set_image(pm, self.post.width or self._image_size(self._raw).width() or pm.width())

    def _fit_movie(self):
        if self._movie is not None and self.post.width > 0 and self.post.height > 0:
            self._movie.setScaledSize(QSize(self.post.width, self.post.height).scaled(self.media.size(), Qt.KeepAspectRatio))

    def _start_video(self):
        self.media.hide()
        self.video.show()
        self.controls.show()
        if self._player is None:  # one player per tab, reused when stepping between videos
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setMuted(self._start_muted)
            self._audio.setVolume(self.volume.value() / 100)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self.video.item)
            self._player.setLoops(QMediaPlayer.Infinite if self._loop else 1)
            # Keep the button label true even when playback stops by itself (end of a non-looping video).
            self._player.playbackStateChanged.connect(self._on_state)
            self._player.errorOccurred.connect(lambda _e, msg: self._show_error(f"Playback error: {msg}"))
            self._player.durationChanged.connect(lambda d: self.seek.setRange(0, d))
            self._player.positionChanged.connect(lambda v: None if self.seek.isSliderDown() else self.seek.setValue(v))
        self._want_play = True
        self._player.setSource(QUrl(self.post.file_url))
        if self.isVisible():
            self._player.play()

    # Background tabs are hidden, so they never stream; switching tabs pauses/resumes.
    def showEvent(self, e):
        super().showEvent(e)
        if self._player is not None and self._want_play and self.post.ext in VIDEO_EXT:
            self._player.play()

    def hideEvent(self, e):
        super().hideEvent(e)
        if self._player is not None:
            self._player.pause()

    def _on_state(self, st):
        self.play_btn.setText("Pause" if st == QMediaPlayer.PlayingState else "Play")
        if st == QMediaPlayer.StoppedState:  # a non-looping video ended: don't restart it on tab switch
            self._want_play = False

    def _toggle_play(self):
        if self._player is None:
            return
        self._want_play = self._player.playbackState() != QMediaPlayer.PlayingState
        if self._want_play:
            self._player.play()
        else:
            self._player.pause()

    def _toggle_mute(self):
        if self._audio is None:
            return
        self._audio.setMuted(not self._audio.isMuted())
        self.mute_btn.setText("Unmute" if self._audio.isMuted() else "Mute")

    def _clear_media(self):
        """Stop and drop whatever is showing (before another post, or when the tab closes)."""
        self._load_id += 1
        if self._reply is not None:
            reply, self._reply = self._reply, None
            try:
                reply.finished.disconnect(self._on_reply)
            except (RuntimeError, TypeError):
                pass
            reply.abort()
            reply.deleteLater()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if self._movie is not None:
            self._movie.stop()
            self.media.setMovie(None)
            self._movie.deleteLater()
            self._movie = None
        if self._buf is not None:
            self._buf.deleteLater()
            self._buf = None
        self._raw = QByteArray()
        self.zoom_view.clear()
        self.zoom_view.hide()
        self.zoom_bar.hide()
        self.media.clear()
        self.media.setText("Loading…")
        self.video.hide()
        self.controls.hide()
        self.media.show()

    def release(self) -> None:
        if self._fs is not None:
            self._fs.close()
        self._released = True
        self._clear_media()
        self.media.clear()
