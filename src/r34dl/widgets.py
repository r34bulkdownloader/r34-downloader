"""Shared constants, small helpers and the tag box with suggestions."""
import logging
import re
from pathlib import Path

from PySide6.QtCore import (
    QStringListModel, Qt, QTimer, QUrl,
)
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QKeySequence, QPalette, QShortcut, QTextCursor,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtWidgets import (
    QCompleter, QLabel, QPlainTextEdit, QVBoxLayout, QWidget,
)

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView

from . import api
from .models import Post

log = logging.getLogger(__name__)

PAGE_SIZE = 100
# shortcut: only this many blacklist tags go into the site query (unknown cap on query length); the rest are
# filtered out locally, which can make pages sparse. Raise it if the site's real limit turns out higher.
BLACKLIST_SERVER_MAX = 10
THUMB_MIN, THUMB_MAX, THUMB_STEP = 100, 400, 20  # thumbnail size slider range, px
CELL_PAD = 16  # space around each thumbnail in the grid


def snap_thumb(side: int) -> int:
    return round(side / THUMB_STEP) * THUMB_STEP  # 16 sizes to render while dragging, not 300
MAX_THUMBS = 6
# shortcut: the sharper copy for zooming is capped at this many pixels (~100 MB decoded, done in the background);
# bigger originals stay slightly soft at 400%. Raise if memory allows.
DETAIL_MAX_PIXELS = 24_000_000
PIXMAP_CACHE_KB = 64 * 1024  # decoded thumbnails kept in memory; the rest stay as ~5-10 KB JPEG bytes
VIDEO_EXT = {"webm", "mp4"}


def wrapped(html: str) -> QLabel:
    lab = QLabel(html)
    lab.setWordWrap(True)
    return lab


def open_in_browser(post: Post) -> None:
    if not QDesktopServices.openUrl(QUrl(api.post_page_url(post.id))):
        log.warning("could not open a web browser for post #%s", post.id)


def hint_label(html: str, links: bool = False) -> QLabel:
    """Small grey help text shown under a control."""
    lab = QLabel(html)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(links)
    lab.setStyleSheet("color: palette(placeholder-text); font-size: 90%;")
    return lab


def expand_dir(text: str) -> Path:
    """Expand ~ and make absolute, so a relative/tilde folder never lands under the CWD by accident."""
    return Path(text).expanduser().resolve()


class TagEdit(QPlainTextEdit):
    """Tag box with suggestions from the site for the word being typed (debounced, latest request wins)."""

    DEBOUNCE_MS = 350
    _WORD = re.compile(r"[^\s,]*$")

    def __init__(self, nam: QNetworkAccessManager, parent=None):
        super().__init__(parent)
        self._nam = nam
        self._reply: QNetworkReply | None = None
        self._values: dict[str, str] = {}
        self._list = QStringListModel(self)
        self.completer = QCompleter(self._list, self)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.activated[str].connect(self._insert)
        self._timer = QTimer(self, singleShot=True, interval=self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._request)
        self.textChanged.connect(self._timer.start)

    def _word(self) -> str:
        before = self.toPlainText()[: self.textCursor().position()]
        return self._WORD.search(before).group(0)

    def _request(self):
        word = self._word().lstrip("-")
        if len(word) < 2 or ":" in word:  # meta tags like rating:explicit have no suggestions
            self.completer.popup().hide()
            return
        if self._reply is not None:
            self._reply.abort()
        req = api.request(api.suggest_url(word))
        reply = self._reply = self._nam.get(req)
        reply.finished.connect(lambda: self._on_reply(reply, word))

    def _on_reply(self, reply: QNetworkReply, word: str):
        reply.deleteLater()
        if reply is self._reply:
            self._reply = None
        if reply.error() != QNetworkReply.NoError or self._word().lstrip("-") != word or not self.hasFocus():
            return
        self.show_suggestions(api.parse_suggestions(bytes(reply.readAll())))

    def show_suggestions(self, items: list[tuple[str, str]]):
        self._values = dict(items)
        self._list.setStringList(list(self._values))
        if not items:
            self.completer.popup().hide()
            return
        rect = self.cursorRect()
        rect.setWidth(max(220, self.width()))
        self.completer.complete(rect)

    def _insert(self, label: str):
        tag = self._values.get(label, label)
        word = self._word()
        cur = self.textCursor()
        cur.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(word))
        at_end = cur.selectionEnd() == len(self.toPlainText())
        cur.insertText(("-" if word.startswith("-") else "") + tag + ("\n" if at_end else ""))
        self.setTextCursor(cur)
        self._timer.stop()  # don't re-suggest for the tag just inserted

    def keyPressEvent(self, e):
        if self.completer.popup().isVisible() and e.key() in (
            Qt.Key_Enter, Qt.Key_Return, Qt.Key_Escape, Qt.Key_Tab, Qt.Key_Backtab
        ):
            e.ignore()  # the popup handles these
            return
        super().keyPressEvent(e)


class VideoView(QGraphicsView):
    """Shows a QMediaPlayer's video as an item drawn into the normal window, like everything else.

    Not QVideoWidget: that embeds a native window, which makes Qt turn the whole window into separate native
    surfaces — then a closed tab's picture or a video's last frame lingers on screen, and the grid flashes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsVideoItem()  # keeps the aspect ratio inside its size, centred
        self.scene().addItem(self.item)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor(0, 0, 0))
        self.setFocusPolicy(Qt.NoFocus)  # arrows/Space belong to the tab or window, not to scrolling this view
        self.item.nativeSizeChanged.connect(lambda _: self._fit())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        size = QSizeF(self.viewport().size())
        self.item.setSize(size)
        self.scene().setSceneRect(QRectF(QPointF(0, 0), size))


class KeepAwake:
    """Ask the desktop not to blank the screen or sleep (the org.freedesktop.ScreenSaver D-Bus service: KDE, GNOME,
    Xfce, Cinnamon...). Uses its own bus connection and releases by closing it — the desktop drops a request
    when its sender goes away, so nothing can stay stuck on (not even if the app crashes). No-op without D-Bus."""

    _count = 0

    def __init__(self):
        self._name: str | None = None

    def on(self, why: str):
        if self._name is not None:
            return
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusMessage
            KeepAwake._count += 1
            name = f"r34dl-keep-awake-{KeepAwake._count}"
            bus = QDBusConnection.connectToBus(QDBusConnection.SessionBus, name)
            if not bus.isConnected():
                log.info("can't keep the screen awake: no session bus")
                QDBusConnection.disconnectFromBus(name)
                return
            msg = QDBusMessage.createMethodCall("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                                                "org.freedesktop.ScreenSaver", "Inhibit")
            msg.setArguments(["R34 Downloader", why])
            bus.asyncCall(msg)  # never wait on the desktop; without the service this simply does nothing
            self._name = name
        except Exception as e:  # never break viewing over this
            log.info("can't keep the screen awake: %s", e)

    def off(self):
        if self._name is None:
            return
        from PySide6.QtDBus import QDBusConnection
        QDBusConnection.disconnectFromBus(self._name)
        self._name = None


class FullscreenWindow(QWidget):
    """A black full-screen window: `stage` fills it, `bar` (bottom) and `corner` (top left) float on top, show
    while the mouse moves and hide with the pointer after HIDE_MS of stillness (not while `pinned`).
    Keeps the screen awake while open. on_close runs before the window and its children go away."""

    HIDE_MS = 2000

    def __init__(self):
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle("R34 Downloader")
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(0, 0, 0))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self.bar: QWidget | None = None
        self.corner: QWidget | None = None
        self.pinned = False
        self.on_close = lambda: None
        self._closed = False
        self._awake = KeepAwake()
        # shortcut: the pointer is polled (5x/s) instead of tracking mouse moves on every child widget.
        self._last_pos = QCursor.pos()
        self._poll = QTimer(self, interval=200, timeout=self._check_pointer)
        self._hide = QTimer(self, singleShot=True, interval=self.HIDE_MS, timeout=self._hide_overlays)

    def set_stage(self, w: QWidget):
        self._lay.addWidget(w, 1)

    def key(self, keys, fn):
        for k in keys if isinstance(keys, (list, tuple)) else [keys]:
            QShortcut(QKeySequence(k), self, activated=fn)

    @staticmethod
    def overlay_style(w: QWidget):
        w.setObjectName("overlay")
        w.setAttribute(Qt.WA_StyledBackground)
        w.setStyleSheet("#overlay { background: rgba(0, 0, 0, 170); border-radius: 6px; } "
                        "#overlay QLabel { color: white; }")

    def showEvent(self, e):
        super().showEvent(e)
        self._awake.on("Watching a slideshow or video")
        self._poll.start()
        self.poke()

    def poke(self):
        """Show the overlays and pointer (again) and restart the hide countdown."""
        for w in (self.bar, self.corner):
            if w is not None:
                w.show()
                w.raise_()
        self._place()
        self.unsetCursor()
        self._hide.start()

    def _hide_overlays(self):
        if self.pinned:
            return
        for w in (self.bar, self.corner):
            if w is not None:
                w.hide()
        self.setCursor(Qt.BlankCursor)

    def _check_pointer(self):
        pos = QCursor.pos()
        if pos != self._last_pos:
            self._last_pos = pos
            self.poke()

    def _place(self):
        if self.corner is not None:
            self.corner.adjustSize()
            self.corner.move(16, 16)
        if self.bar is not None:
            h = self.bar.sizeHint().height()
            self.bar.setGeometry(0, self.height() - h, self.width(), h)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def closeEvent(self, e):
        if not self._closed:
            self._closed = True
            self._poll.stop()
            self._awake.off()
            self.on_close()  # hand borrowed widgets back before WA_DeleteOnClose deletes our children
        super().closeEvent(e)
