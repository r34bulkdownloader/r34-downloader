"""The "Open together" tab: ticked posts side by side."""
import logging
from collections import deque

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QRect, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QMovie, QPainter, QPixmap,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtWidgets import (
    QHBoxLayout, QGridLayout, QLabel, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from . import api
from .models import Post
from .grid import HoverPreview, ResultsModel
from .viewer import ViewerTab
from .widgets import MAX_THUMBS, VIDEO_EXT

log = logging.getLogger(__name__)

class _Cell(QLabel):
    """One post in a Together tab. Click = open it on its own."""

    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class TogetherTab(QWidget):
    """The ticked posts side by side (1–4 per row). Images use the site's medium-size copy, GIFs play in place,
    videos show their thumbnail with ▶. Clicking a post opens it alone; ◀/▶ there step through these posts."""

    open_requested = Signal(object)  # Post

    def __init__(self, posts: list[Post], nam: QNetworkAccessManager, per_row: int = 2):
        super().__init__()
        self.model = ResultsModel(self)  # just these posts, in order (Previous/Next in single-post tabs)
        self.model.append(posts)
        self.model.exhausted = True
        self._nam = nam
        # Memory stays small however many posts are ticked: only the downloaded files are kept, and pictures
        # are decoded at the cell's size for cells on (or near) the screen; off-screen cells drop their pixels.
        self._files: dict[int, QByteArray] = {}
        self._shown: set[int] = set()  # posts whose cell currently holds decoded pixels
        self._movies: dict[int, QMovie] = {}
        self._replies: set[QNetworkReply] = set()
        self._queue: deque[Post] = deque(posts)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(f"{len(posts)} posts side by side · click one to open it on its own"), 1)
        top.addWidget(QLabel("Per row"))
        self.per_row = QSpinBox()
        self.per_row.setRange(1, 4)
        self.per_row.setValue(per_row)
        self.per_row.valueChanged.connect(self._relayout)
        top.addWidget(self.per_row)
        lay.addLayout(top)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)  # constant width: no reflow when it appears
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.scroll.setWidget(self.grid_host)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda _: self._render_visible())
        lay.addWidget(self.scroll, 1)
        self._refit = QTimer(self, singleShot=True, interval=120)  # rescale once resizing pauses
        self._refit.timeout.connect(self._relayout)
        self.cells: dict[int, _Cell] = {}
        for p in posts:
            cell = _Cell("Loading…")
            cell.setAlignment(Qt.AlignCenter)
            cell.setMinimumHeight(60)
            cell.setCursor(Qt.PointingHandCursor)
            cell.setToolTip(f"#{p.id} · click to open")
            cell.clicked.connect(lambda p=p: self.open_requested.emit(p))
            self.cells[p.id] = cell
        self._relayout()
        self._pump()

    def _url(self, post: Post) -> str:
        if post.ext in VIDEO_EXT:
            return post.preview_url  # a still thumbnail; the video plays when opened on its own
        if post.ext == "gif":
            return post.file_url
        return post.sample_url or post.file_url  # medium-size copy is plenty side by side

    def _pump(self):
        while len(self._replies) < MAX_THUMBS and self._queue:
            post = self._queue.popleft()
            url = self._url(post)
            if not url:
                self.cells[post.id].setText(f"#{post.id}")
                continue
            reply = self._nam.get(api.request(url))
            self._replies.add(reply)
            if post.ext == "gif":  # GIFs come whole: same size cap as hover previews
                reply.downloadProgress.connect(lambda got, total, r=reply, p=post: self._check_size(r, p, got, total))
            reply.finished.connect(lambda r=reply, p=post: self._done(r, p))

    def _check_size(self, reply: QNetworkReply, post: Post, got: int, total: int):
        if reply in self._replies and max(got, total) > HoverPreview.GIF_MAX_BYTES:
            self._replies.discard(reply)
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
            self.cells[post.id].setText(f"#{post.id}: large GIF — click to open it")
            self._pump()

    def _done(self, reply: QNetworkReply, post: Post):
        self._replies.discard(reply)
        ok = reply.error() == QNetworkReply.NoError
        data = reply.readAll() if ok else QByteArray()
        if not ok:
            log.warning("together view: post #%s: %s", post.id, reply.errorString())
        reply.deleteLater()
        cell = self.cells[post.id]
        if not ok:
            cell.setText(f"#{post.id}: could not load")
        elif post.ext == "gif":
            buf = QBuffer(self)
            buf.setData(data)
            buf.open(QIODevice.ReadOnly)
            movie = QMovie(buf, QByteArray(), self)
            if movie.isValid():
                self._movies[post.id] = movie
                cell.setMovie(movie)
                movie.start()
            else:
                cell.setText(f"#{post.id}: could not decode")
        else:
            self._files[post.id] = data
        self._fit(post.id)
        self._render_visible()
        self._pump()

    def _picture(self, post: Post, w: int) -> QPixmap | None:
        """Decode the post's file at cell width (▶ painted on video thumbnails)."""
        pm = ViewerTab._decode_limited(self._files[post.id], max_side=max(1, w * 2))  # a little sharper than needed
        if pm is None:
            return None
        pm = pm.scaledToWidth(w, Qt.SmoothTransformation)
        if post.ext in VIDEO_EXT:
            painter = QPainter(pm)
            painter.fillRect(QRect(pm.width() - 34, 4, 30, 20), QColor(0, 0, 0, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(pm.width() - 34, 4, 30, 20), Qt.AlignCenter, "▶")
            painter.end()
        return pm

    def _render_visible(self):
        """Decode cells within a screen of the visible area; free the pixels of cells farther away."""
        vp = self.scroll.viewport().height()
        top = self.scroll.verticalScrollBar().value() - vp
        bottom = top + 3 * vp
        w = self._cell_width()
        for post_id, cell in self.cells.items():
            near = cell.y() + cell.height() >= top and cell.y() <= bottom
            if post_id in self._movies:
                self._movies[post_id].setPaused(not near or not self.isVisible())
                continue
            if post_id not in self._files:
                continue
            if near and post_id not in self._shown:
                pm = self._picture(self.model.known[post_id], w)
                if pm is None:
                    cell.setText(f"#{post_id}: could not decode")
                    del self._files[post_id]
                    continue
                cell.setPixmap(pm)
                self._shown.add(post_id)
            elif not near and post_id in self._shown:
                cell.setPixmap(QPixmap())
                self._shown.discard(post_id)

    def _cell_width(self) -> int:
        n = self.per_row.value()
        m = self.grid.contentsMargins()
        room = self.scroll.viewport().width() - m.left() - m.right() - self.grid.spacing() * (n - 1)
        return max(80, room // n)

    def _fit(self, post_id: int):
        """Size the cell from the post's known dimensions, so the layout never jumps as pictures arrive."""
        w = self._cell_width()
        post = self.model.known[post_id]
        h = max(60, w * post.height // post.width) if post.width > 0 and post.height > 0 else w * 3 // 4
        self.cells[post_id].setFixedSize(w, h)
        if post_id in self._movies:
            self._movies[post_id].setScaledSize(QSize(w, h))
        elif post_id in self._shown:  # new width: decode again at the new size
            self._shown.discard(post_id)

    def _relayout(self, *_):
        for cell in self.cells.values():
            self.grid.removeWidget(cell)
        n = self.per_row.value()
        for i, post in enumerate(self.model.posts):
            self.grid.addWidget(self.cells[post.id], i // n, i % n, Qt.AlignTop)
        for post_id in self.cells:
            self._fit(post_id)
        QTimer.singleShot(0, self, self._render_visible)  # after the layout has placed the cells

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self, self._render_visible)  # resumes the GIFs on screen

    def hideEvent(self, e):
        super().hideEvent(e)
        for movie in self._movies.values():  # background Together tabs don't animate
            movie.setPaused(True)

    def release(self):
        self._queue.clear()
        for reply in list(self._replies):
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        self._replies.clear()
        for movie in self._movies.values():
            movie.stop()
