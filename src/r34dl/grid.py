"""The results grid: model, tile drawing, hover previews and the view (ticking, Shift ranges, zoom)."""
import logging

from PySide6.QtCore import (
    QAbstractListModel, QBuffer, QByteArray, QEasingCurve, QIODevice, QModelIndex, QObject, QPropertyAnimation, QRect,
    QSize,
    Qt,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QPalette, QMovie, QPainter, QPixmap, QPixmapCache,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtWidgets import (
    QLabel, QListView, QStyledItemDelegate, QWidget,
)

from . import api
from .models import Post
from .widgets import CELL_PAD, VIDEO_EXT, VideoView

log = logging.getLogger(__name__)

class ResultsModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.posts: list[Post] = []
        self.checked: set[int] = set()
        self.exhausted = False
        self.generation = 0
        self.thumbs: dict[int, bytes] = {}  # compressed preview bytes; decoded on demand
        self.thumb_size = 160  # px, longest side of a thumbnail in the grid
        self.downloaded: set[int] = set()  # post ids in the download history (kept across searches)
        self.favorites: dict = {}  # post id -> Post; shared with the window (membership = ♥ mark)
        self._rows: dict[int, int] = {}
        self.known: dict[int, Post] = {}  # every post shown in this search, across pages (for Download N)
        self.page_of: dict[int, int] = {}  # post id -> site page (0-based) it came from

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.posts)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        post = self.posts[index.row()]
        if role == Qt.CheckStateRole:
            return Qt.Checked if post.id in self.checked else Qt.Unchecked
        if role == Qt.UserRole:
            return post
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or not index.isValid():
            return False
        pid = self.posts[index.row()].id
        if value == Qt.Checked or value == Qt.Checked.value:
            self.checked.add(pid)
        else:
            self.checked.discard(pid)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        return True

    def set_range(self, a: int, b: int, on: bool):
        lo, hi = sorted((a, b))
        for p in self.posts[lo : hi + 1]:
            (self.checked.add if on else self.checked.discard)(p.id)
        self.dataChanged.emit(self.index(lo), self.index(hi), [Qt.CheckStateRole])

    def toggle(self, row: int):
        idx = self.index(row)
        self.setData(idx, Qt.Unchecked if idx.data(Qt.CheckStateRole) == Qt.Checked else Qt.Checked, Qt.CheckStateRole)

    def append(self, posts: list[Post], page: int = 0) -> list[Post]:
        """Append posts not already present (by id); returns the ones actually added."""
        seen = set(self._rows)
        new = []
        for p in posts:
            if p.id not in seen:
                seen.add(p.id)
                new.append(p)
        if not new:
            return new
        n = len(self.posts)
        self.beginInsertRows(QModelIndex(), n, n + len(new) - 1)
        for i, p in enumerate(new):
            self._rows[p.id] = n + i
            self.known[p.id] = p
            self.page_of[p.id] = page
        self.posts.extend(new)
        self.endInsertRows()
        return new

    def reset(self, keep_selection: bool = False):
        """Empty the grid. keep_selection: page change in page mode (ticks on other pages stay)."""
        self.beginResetModel()
        self.posts = []
        self._rows = {}
        if not keep_selection:
            self.checked = set()
            self.known = {}
            self.page_of = {}
        self.thumbs = {}
        self.exhausted = False
        self.endResetModel()

    def selected_posts(self) -> list[Post]:
        """Ticked posts in search order, including ones ticked on other pages."""
        ticked = [p for i, p in self.known.items() if i in self.checked]
        return sorted(ticked, key=lambda p: self.page_of.get(p.id, 0))  # stable: keeps order within a page

    def set_all(self, mode: str):
        for p in self.posts:
            if mode == "all":
                self.checked.add(p.id)
            elif mode == "none":
                self.checked.discard(p.id)
            else:
                self.checked ^= {p.id}
        if self.posts:
            self.dataChanged.emit(self.index(0), self.index(len(self.posts) - 1), [Qt.CheckStateRole])

    def pixmap(self, post_id: int) -> QPixmap | None:
        """Decoded thumbnail, pre-scaled to the current size; cached in QPixmapCache (bounded LRU)."""
        data = self.thumbs.get(post_id)
        if data is None:
            return None
        key = f"thumb:{self.generation}:{self.thumb_size}:{post_id}"
        pm = QPixmapCache.find(key)
        if pm is None:
            pm = QPixmap()
            if not pm.loadFromData(data):
                return None
            side = self.thumb_size
            pm = pm.scaled(QSize(side, side), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            QPixmapCache.insert(key, pm)
        return pm

    def insert_front(self, post: Post) -> list[Post]:
        """Put one post at the top (a new favorite); returns [post] if added."""
        if post.id in self._rows:
            return []
        self.beginInsertRows(QModelIndex(), 0, 0)
        self.posts.insert(0, post)
        self._rows = {p.id: i for i, p in enumerate(self.posts)}
        self.known = {post.id: post, **self.known}
        self.page_of[post.id] = 0
        self.endInsertRows()
        return [post]

    def mark_changed(self, post_id: int):
        row = self._rows.get(post_id)
        if row is not None:
            self.dataChanged.emit(self.index(row), self.index(row))

    def mark_downloaded(self, post_id: int):
        self.downloaded.add(post_id)
        row = self._rows.get(post_id)
        if row is not None:
            self.dataChanged.emit(self.index(row), self.index(row))

    def thumb_ready(self, post_id: int, data: bytes):
        self.thumbs[post_id] = data
        row = self._rows.get(post_id)
        if row is not None:
            self.dataChanged.emit(self.index(row), self.index(row))


def checkbox_rect(cell: QRect) -> QRect:
    return QRect(cell.left() + 6, cell.top() + 6, 20, 20)


class ThumbDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        side = index.model().thumb_size + CELL_PAD
        return QSize(side, side)

    def paint(self, painter: QPainter, option, index):
        model = index.model()
        post: Post = index.data(Qt.UserRole)
        r = option.rect.adjusted(4, 4, -4, -4)
        painter.save()
        pal = option.palette  # follows the theme
        alt = pal.color(QPalette.AlternateBase)  # tiles stand out from the grid in dark and light themes
        painter.fillRect(r, alt.lighter(160) if pal.color(QPalette.Base).lightness() < 128 else alt.darker(115))
        s = model.pixmap(post.id)
        if s is not None and not s.isNull():
            painter.drawPixmap(r.left() + (r.width() - s.width()) // 2, r.top() + (r.height() - s.height()) // 2, s)
        cb = checkbox_rect(r)
        painter.fillRect(cb, pal.color(QPalette.Base))
        painter.setPen(pal.color(QPalette.Text))
        painter.drawRect(cb)
        if index.data(Qt.CheckStateRole) == Qt.Checked:
            painter.setFont(QFont(painter.font().family(), 12, QFont.Bold))
            painter.drawText(cb, Qt.AlignCenter, "✓")
        if post.id in model.downloaded:  # download-history marker, bottom-left
            dm = QRect(r.left() + 6, r.bottom() - 25, 20, 20)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(46, 160, 67))
            painter.drawEllipse(dm)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont(painter.font().family(), 10, QFont.Bold))
            painter.drawText(dm, Qt.AlignCenter, "↓")
        if post.id in model.favorites:  # favorite marker, bottom-right
            fm = QRect(r.right() - 25, r.bottom() - 25, 20, 20)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(214, 51, 108))
            painter.drawEllipse(fm)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont(painter.font().family(), 10, QFont.Bold))
            painter.drawText(fm, Qt.AlignCenter, "♥")
        badge = "▶" if post.ext in VIDEO_EXT else "GIF" if post.ext == "gif" else ""
        if badge:
            br = QRect(r.right() - 36, r.top() + 4, 32, 18)
            painter.fillRect(br, QColor(0, 0, 0, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont(painter.font().family(), 9, QFont.Bold))
            painter.drawText(br, Qt.AlignCenter, badge)
        painter.restore()


class HoverPreview(QObject):
    """Plays a GIF or video inside the thumbnail the mouse rests on: one at a time, muted, gone as soon as the
    mouse leaves (or the grid scrolls). Mouse clicks pass straight through to the grid."""

    DELAY_MS = 400  # resting time before a preview starts, so sweeping across the grid starts nothing
    GIF_MAX_BYTES = 15 * 1024 * 1024  # bigger GIFs keep their still thumbnail (they must be fetched whole)

    def __init__(self, view: "ResultsView"):
        super().__init__(view)
        self.view = view
        self.nam: QNetworkAccessManager | None = None  # set by the search tab
        self.enabled = lambda: True  # set by the search tab (Settings)
        self.row = -1
        self._timer = QTimer(self, singleShot=True, interval=self.DELAY_MS)
        self._timer.timeout.connect(self._start)
        self.widget: QWidget | None = None
        self._player: QMediaPlayer | None = None
        self._movie: QMovie | None = None
        self._reply: QNetworkReply | None = None

    def hover(self, row: int):
        if row == self.row:
            return
        self.stop()
        self.row = row
        post = self._post()
        if post is not None and (post.ext in VIDEO_EXT or post.ext == "gif") and self.nam and self.enabled():
            self._timer.start()

    def _post(self) -> Post | None:
        model = self.view.model()
        return model.posts[self.row] if model is not None and 0 <= self.row < len(model.posts) else None

    def _rect(self) -> QRect:
        cell = self.view.visualRect(self.view.model().index(self.row)).adjusted(4, 4, -4, -4)
        return cell.adjusted(2, 30, -2, -2)  # below the checkbox strip, so it stays visible and clickable

    def _overlay(self, w: QWidget):
        w.setAttribute(Qt.WA_TransparentForMouseEvents)
        w.setGeometry(self._rect())
        self.widget = w
        w.show()

    def _start(self):
        post = self._post()
        if post is None:
            return
        if post.ext in VIDEO_EXT:
            video = VideoView(self.view.viewport())
            video.viewport().setAttribute(Qt.WA_TransparentForMouseEvents)  # clicks reach the grid
            self._player = QMediaPlayer(video)
            audio = QAudioOutput(video)
            audio.setMuted(True)
            self._player.setAudioOutput(audio)
            self._player.setVideoOutput(video.item)
            self._player.setLoops(QMediaPlayer.Infinite)
            self._player.errorOccurred.connect(lambda *_: self.stop(keep_row=True))
            self._overlay(video)
            self._player.setSource(QUrl(post.file_url))
            self._player.play()
        else:
            req = api.request(post.file_url)
            reply = self._reply = self.nam.get(req)
            reply.downloadProgress.connect(lambda got, total, r=reply: self._check_size(r, got, total))
            reply.finished.connect(lambda r=reply, p=post: self._gif_done(r, p))

    def _check_size(self, reply: QNetworkReply, got: int, total: int):
        if reply is self._reply and max(got, total) > self.GIF_MAX_BYTES:
            self.stop(keep_row=True)  # too big to preview: the still thumbnail stays

    def _gif_done(self, reply: QNetworkReply, post: Post):
        reply.deleteLater()
        if reply is not self._reply:
            return
        self._reply = None
        if reply.error() != QNetworkReply.NoError:
            return
        label = QLabel(self.view.viewport())
        buf = QBuffer(label)
        buf.setData(reply.readAll())
        buf.open(QIODevice.ReadOnly)
        movie = QMovie(buf, QByteArray(), label)
        if not movie.isValid():
            label.deleteLater()
            return
        label.setAlignment(Qt.AlignCenter)
        label.setMovie(movie)
        self._movie = movie
        self._overlay(label)
        if post.width > 0 and post.height > 0:
            movie.setScaledSize(QSize(post.width, post.height).scaled(label.size(), Qt.KeepAspectRatio))
        movie.start()

    def stop(self, keep_row: bool = False):
        self._timer.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.abort()  # its finished handler sees it's no longer current and lets it go
        if self._player is not None:
            self._player.stop()
            self._player = None
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        if self.widget is not None:
            self.widget.hide()
            self.widget.deleteLater()  # also deletes the player/movie parented to it
            self.widget = None
        if not keep_row:
            self.row = -1


class ResultsView(QListView):
    post_clicked = Signal(int, bool)  # row, background
    context_requested = Signal(int, object)  # row, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QSize(160 + CELL_PAD, 160 + CELL_PAD))
        self.setSelectionMode(QListView.NoSelection)
        self.setItemDelegate(ThumbDelegate(self))
        # Smooth scrolling: move by pixels, and glide each mouse-wheel notch instead of jumping a whole row.
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self._glide = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._glide.setDuration(180)
        self._glide.setEasingCurve(QEasingCurve.OutCubic)
        self._anchor: int | None = None  # last row ticked/unticked, start of a Shift+click range
        self.preview = HoverPreview(self)
        self.setMouseTracking(True)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.preview.stop())

    def setModel(self, model):
        super().setModel(model)
        model.modelReset.connect(lambda: setattr(self, "_anchor", None))
        model.modelReset.connect(self.preview.stop)

    def mouseMoveEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        self.preview.hover(idx.row() if idx.isValid() else -1)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        self.preview.hover(-1)
        super().leaveEvent(e)

    def hideEvent(self, e):
        self.preview.hover(-1)  # switching tabs stops the preview
        super().hideEvent(e)

    def resizeEvent(self, e):
        self.preview.hover(-1)  # the grid reflows
        super().resizeEvent(e)

    WHEEL_STEP_PX = 120  # per wheel notch
    zoom_requested = Signal(int)  # +1 bigger / -1 smaller (Ctrl+wheel)

    def set_thumb_size(self, side: int):
        self.preview.stop()  # the grid reflows: the overlay would sit on another cell
        self.model().thumb_size = side
        self.setGridSize(QSize(side + CELL_PAD, side + CELL_PAD))  # re-flows the grid
        self.viewport().update()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier and e.angleDelta().y():
            self.zoom_requested.emit(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
            return
        if not e.pixelDelta().isNull() or e.modifiers() or e.angleDelta().y() == 0:
            super().wheelEvent(e)  # touchpads already send smooth pixel deltas
            return
        sb = self.verticalScrollBar()
        running = self._glide.state() == QPropertyAnimation.Running
        start = self._glide.endValue() if running else sb.value()
        target = max(sb.minimum(), min(sb.maximum(), int(start - e.angleDelta().y() / 120 * self.WHEEL_STEP_PX)))
        self._glide.stop()
        self._glide.setStartValue(sb.value())
        self._glide.setEndValue(target)
        self._glide.start()
        e.accept()

    def mousePressEvent(self, e):
        self._press = self.indexAt(e.position().toPoint()).row()
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        idx = self.indexAt(e.pos())
        if idx.isValid():
            self.context_requested.emit(idx.row(), e.globalPos())

    def mouseReleaseEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid() and idx.row() == getattr(self, "_press", -1) and e.button() in (Qt.LeftButton, Qt.MiddleButton):
            cell = self.visualRect(idx).adjusted(4, 4, -4, -4)
            shift = bool(e.modifiers() & Qt.ShiftModifier)
            if e.button() == Qt.LeftButton and shift and self._anchor is not None:
                anchor_on = self.model().posts[self._anchor].id in self.model().checked
                self.model().set_range(self._anchor, idx.row(), anchor_on)
                self._anchor = idx.row()
            elif e.button() == Qt.LeftButton and (shift or checkbox_rect(cell).contains(e.position().toPoint())):
                self.model().toggle(idx.row())
                self._anchor = idx.row()
            else:
                bg = e.button() == Qt.MiddleButton or bool(e.modifiers() & Qt.ControlModifier)
                self.post_clicked.emit(idx.row(), bg)
        super().mouseReleaseEvent(e)
