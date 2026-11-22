"""Slideshow: a full-screen show of a search's posts (or Favorites, or the ticked posts) that moves on by itself."""
import itertools
import random
import threading
from typing import Callable

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QMovie, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from . import api
from .models import Post
from .viewer import ViewerTab
from .widgets import VIDEO_EXT, FullscreenWindow, VideoView, hint_label

ERROR_MS = 1500  # a post that can't be shown stays this long (with the reason) before the show moves on
VIDEO_WAIT_MS = 30_000  # a video that hasn't started loading by then is skipped (the player has no timeout)


class _Relay(QObject):
    """Lives for the whole run, so decode threads never emit on a slideshow window that's being deleted."""
    decoded = Signal(int, int, object)  # show serial, post id, QImage or None


_relay: _Relay | None = None
_serials = itertools.count(1)


def relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


class SlideshowDialog(QDialog):
    """The options, asked when a slideshow starts (pre-filled with the last ones)."""

    def __init__(self, parent, opts: dict, ticked: int):
        super().__init__(parent)
        self.setWindowTitle("Slideshow")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.seconds = QSpinBox()
        self.seconds.setRange(2, 600)
        self.seconds.setSuffix(" s")
        self.seconds.setValue(opts["seconds"])
        form.addRow("Each picture shows for", self.seconds)
        self.shuffle = QCheckBox("Shuffle")
        self.shuffle.setChecked(opts["shuffle"])
        form.addRow(self.shuffle)
        self.gifs = QCheckBox("Include GIFs (each plays through at least once)")
        self.gifs.setChecked(opts["gifs"])
        form.addRow(self.gifs)
        self.videos = QCheckBox("Include videos")
        self.videos.setChecked(opts["videos"])
        form.addRow(self.videos)
        self.video_max = QSpinBox()
        self.video_max.setRange(0, 3600)
        self.video_max.setSuffix(" s")
        self.video_max.setSpecialValueText("whole video")
        self.video_max.setValue(opts["video_max"])
        self.video_max.setEnabled(opts["videos"])
        self.videos.toggled.connect(self.video_max.setEnabled)
        form.addRow("Videos play for at most", self.video_max)
        self.sound = QCheckBox("Play sound")
        self.sound.setChecked(opts["sound"])
        form.addRow(self.sound)
        self.loop = QCheckBox("Start again at the end")
        self.loop.setChecked(opts["loop"])
        form.addRow(self.loop)
        self.ticked = QCheckBox(f"Only the {ticked} ticked posts")
        self.ticked.setChecked(ticked > 0)
        self.ticked.setVisible(ticked > 0)
        self._ticked = ticked
        form.addRow(self.ticked)
        lay.addLayout(form)
        lay.addWidget(hint_label(
            "During the show: Space pause · ← → previous/next · F favorite · D download · Esc leave. "
            "It keeps going into the search's next pages by itself."))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("▶ Start")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def options(self) -> dict:
        return {"seconds": self.seconds.value(), "shuffle": self.shuffle.isChecked(), "videos": self.videos.isChecked(),
                "video_max": self.video_max.value(), "gifs": self.gifs.isChecked(), "sound": self.sound.isChecked(),
                "loop": self.loop.isChecked()}

    def only_ticked(self) -> bool:
        return self._ticked > 0 and self.ticked.isChecked()


class Slideshow(FullscreenWindow):
    """Shows posts one after another. `origin` (a search tab) is asked for more pages at the end; new posts it
    gets are added to the show. The next picture is downloaded and decoded ahead, so changes are instant."""

    download_requested = Signal(object)  # Post
    favorite_toggled = Signal(object)  # Post
    is_favorite: Callable[[int], bool] = staticmethod(lambda post_id: False)  # set by the window

    def __init__(self, posts: list[Post], nam: QNetworkAccessManager, opts: dict, origin=None):
        super().__init__()
        self._nam = nam
        self._opts = opts
        self._origin = origin
        self._order: list[Post] = []
        self._seen: set[int] = set()
        self._i = -1
        self._add(posts)
        self._paused = False
        self._waiting = False  # at the end of the loaded posts, waiting for the search's next page
        self._ended = False
        self._cache: dict[int, object] = {}  # post id -> QPixmap (picture) or QByteArray (GIF): current + next
        self._replies: dict[int, QNetworkReply] = {}
        self._shown = False  # the current post is on screen (its timer may run)
        self._left = -1  # ms left on the timer when paused
        self._movie: QMovie | None = None
        self._buf: QBuffer | None = None
        self._pixmap = QPixmap()
        self._gif_done = False
        self._after_gif = False  # the time is up, waiting for the GIF to finish its play-through
        self._queued: set[int] = set()  # downloaded with D during this show
        self._serial = next(_serials)
        self._decoding: set[int] = set()  # downloaded, being decoded on a thread
        relay().decoded.connect(self._on_decoded)

        stage = QWidget()
        sl = QVBoxLayout(stage)
        sl.setContentsMargins(0, 0, 0, 0)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.picture.setStyleSheet("color: white; font-size: 18px;")
        sl.addWidget(self.picture, 1)
        self.video = VideoView()
        self.video.hide()
        sl.addWidget(self.video, 1)
        self.set_stage(stage)
        self.corner = QWidget(self)
        self.overlay_style(self.corner)
        cv = QVBoxLayout(self.corner)
        self.info = QLabel()
        self.info.setStyleSheet("font-size: 15px;")
        cv.addWidget(self.info)
        keys = QLabel("Space pause · ← → previous/next · F favorite · D download · Esc leave")
        cv.addWidget(keys)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(not opts["sound"])
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self.video.item)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        # Both handled a moment later: stopping the player from inside its own signal isn't safe.
        self._player.errorOccurred.connect(lambda _e, msg: self._later(lambda: self._failed(f"Can't play this video: {msg}")))
        self._timer = QTimer(self, singleShot=True, timeout=self._on_timer)
        self._timer.setTimerType(Qt.PreciseTimer)  # the default coarse timer may run 5% long

        self.key("Esc", self.close)
        self.key("Space", self.toggle_pause)
        self.key("Left", lambda: self.go(-1))
        self.key("Right", lambda: self.go(1))
        self.key("F", self._favorite)
        self.key("D", self._download)
        if origin is not None:
            origin.fetch_finished.connect(self._on_fetch_finished)
            origin.destroyed.connect(self._origin_gone)
        QTimer.singleShot(0, self, lambda: self.go(1))

    # ---- which posts
    def _wanted(self, post: Post) -> bool:
        if post.ext in VIDEO_EXT:
            return self._opts["videos"]
        return self._opts["gifs"] or post.ext != "gif"

    def _add(self, posts: list[Post]) -> int:
        """Append posts not in the show yet (shuffled among themselves); returns how many were added."""
        fresh = [p for p in posts if p.id not in self._seen and self._wanted(p)]
        self._seen.update(p.id for p in posts)
        if self._opts["shuffle"]:
            random.shuffle(fresh)
        self._order += fresh
        return len(fresh)

    def _origin_gone(self, *_):
        self._origin = None
        if self._waiting:  # the page it was waiting for will never come
            self._waiting = False
            self.go(1, fetch=False)

    def _ask_more(self) -> bool:
        """Ask the search for its next page; False if there's none to get."""
        o = self._origin
        if o is None or o._closed or not o.can_load_more():
            return False
        o.load_more()
        return True

    def _on_fetch_finished(self, ok: bool):
        if self._origin is None or self._closed:
            return
        added = self._add(self._origin.model.posts)  # also pages the user scrolled in meanwhile
        if not self._waiting:
            if added:
                self._prefetch()  # the new page's first post may be next
            return
        if added:
            self._waiting = False
            self.go(1)
        elif not (ok and self._ask_more()):  # that page had nothing for us: try the next one, else stop
            self._waiting = False
            self.go(1, fetch=False)

    def current(self) -> Post | None:
        return self._order[self._i] if 0 <= self._i < len(self._order) else None

    def go(self, delta: int, fetch: bool = True):
        if self._closed:
            return
        self.poke()  # show where we are, also after a key press
        new = self._i + delta
        if new < 0:
            return
        if new >= len(self._order):
            if fetch and self._ask_more():
                self._waiting = True
                self._update_info()
                return
            if not self._order:
                self._clear()
                self._message("Nothing to show here (are GIFs or videos left out?) · Esc to leave")
                return
            if not self._opts["loop"]:
                self._clear()
                self._i = len(self._order)  # ← goes back to the last post
                self._ended = True
                self._message("End of the slideshow · ← to go back · Esc to leave")
                self._update_info()
                return
            if self._opts["shuffle"]:
                random.shuffle(self._order)
            new = 0
        self._waiting = False
        self._ended = False
        self._i = new
        self._show(self._order[new])

    # ---- showing one post
    def _clear(self):
        self._timer.stop()
        self._shown = False
        self._left = -1
        self._gif_done = self._after_gif = False
        if self._player.source().isValid():
            self._player.stop()
            self._player.setSource(QUrl())
        if self._movie is not None:
            self._movie.stop()
            self.picture.setMovie(None)
            self._movie.deleteLater()
            self._movie = None
        if self._buf is not None:
            self._buf.deleteLater()
            self._buf = None
        self._pixmap = QPixmap()
        self.picture.clear()

    def _message(self, text: str):
        self.video.hide()
        self.picture.show()
        self.picture.setText(text)

    def _show(self, post: Post):
        self._clear()
        self._update_info()
        if post.ext in VIDEO_EXT:
            self.picture.hide()
            self.video.show()
            self._player.setLoops(1)
            self._player.setSource(QUrl(post.file_url))
            self._shown = True
            if not self._paused:
                self._player.play()
                # Capped: move on after video_max. Whole video: a watchdog until it starts loading.
                self._timer.start((self._opts["video_max"] * 1000) or VIDEO_WAIT_MS)
        else:
            got = self._cache.get(post.id)
            if got is None:
                self._message("Loading…")
                self._fetch(post)
            else:
                self._display(post, got)
        self._prefetch()

    def _display(self, post: Post, got):
        self.video.hide()
        self.picture.show()
        self.picture.setText("")
        if post.ext == "gif":
            self._buf = QBuffer(self)
            self._buf.setData(got)
            self._buf.open(QIODevice.ReadOnly)
            self._movie = QMovie(self._buf, QByteArray(), self)
            if not self._movie.isValid():
                self._failed("Can't decode this GIF")
                return
            self._movie.frameChanged.connect(self._on_gif_frame)
            self.picture.setMovie(self._movie)
            self._fit()
            self._movie.start()
            if self._paused:
                self._movie.setPaused(True)
        else:
            self._pixmap = got
            self._fit()
        self._shown = True
        if not self._paused:
            self._timer.start(self._opts["seconds"] * 1000)

    def _fit(self):
        size = self.picture.size()
        if self._movie is not None:
            post = self.current()
            if post is not None and post.width > 0 and post.height > 0:
                self._movie.setScaledSize(QSize(post.width, post.height).scaled(size, Qt.KeepAspectRatio))
        elif not self._pixmap.isNull():
            self.picture.setPixmap(self._pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _failed(self, text: str):
        """The current post can't be shown: say why, then move on."""
        self._clear()
        self._message(api.scrub(text))
        self._shown = True
        if not self._paused:
            self._timer.start(ERROR_MS)

    # ---- moving on by itself
    def _on_timer(self):
        post = self.current()
        if post is not None and post.ext == "gif" and self._movie is not None and not self._gif_done:
            self._after_gif = True  # let it finish its play-through first
            return
        self.go(1)

    def _on_gif_frame(self, n: int):
        if self._movie is None or self._gif_done:
            return
        if n >= self._movie.frameCount() - 1:
            self._gif_done = True
            if self._after_gif and not self._paused:
                self._later(lambda: self.go(1))  # not from inside the GIF's own signal

    def _on_media_status(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and not self._opts["video_max"]:
            self._timer.stop()  # it plays: the watchdog's job is done, the end of the video moves on
        if status == QMediaPlayer.EndOfMedia and not self._paused:
            self._later(lambda: self.go(1))

    def _later(self, fn):
        """Run fn in a moment, unless the show has moved to another post by then."""
        i = self._i
        QTimer.singleShot(0, self, lambda: fn() if self._i == i and not self._closed else None)

    def toggle_pause(self):
        self._paused = not self._paused
        self.pinned = self._paused  # the overlay stays while paused
        post = self.current()
        if self._paused:
            self._left = self._timer.remainingTime() if self._timer.isActive() else -1
            self._timer.stop()
            self._player.pause()
            if self._movie is not None:
                self._movie.setPaused(True)
        elif post is not None and self._shown and not self._ended:
            if post.ext in VIDEO_EXT and self._player.source().isValid():
                self._player.play()
                if self._left > 0:
                    self._timer.start(self._left)
                elif self._opts["video_max"] and self._left < 0:
                    self._timer.start(self._opts["video_max"] * 1000)
            else:
                if self._movie is not None:
                    self._movie.setPaused(False)
                if self._after_gif and self._gif_done:
                    self.go(1)
                    return
                if not self._after_gif:
                    self._timer.start(self._left if self._left > 0 else self._opts["seconds"] * 1000)
        self._left = -1
        self._update_info()
        self.poke()

    # ---- downloading ahead
    def _peek_next(self) -> Post | None:
        i = self._i + 1
        return self._order[i] if i < len(self._order) else None

    def _prefetch(self):
        cur, nxt = self.current(), self._peek_next()
        keep = {p.id for p in (cur, nxt) if p is not None}
        for pid in [k for k in self._cache if k not in keep]:
            del self._cache[pid]
        for pid in [k for k in self._replies if k not in keep]:
            self._drop_reply(pid)
        if nxt is not None and nxt.ext not in VIDEO_EXT and nxt.id not in self._cache:
            self._fetch(nxt)
        self._decoding &= keep

    def _drop_reply(self, pid: int):
        reply = self._replies.pop(pid)
        reply.finished.disconnect()
        reply.abort()
        reply.deleteLater()

    def _fetch(self, post: Post):
        if post.id in self._replies or post.id in self._decoding or self._closed:
            return
        reply = self._nam.get(api.request(post.file_url))
        self._replies[post.id] = reply
        reply.finished.connect(lambda p=post: self._fetched(p))

    def _fetched(self, post: Post):
        reply = self._replies.pop(post.id, None)
        if reply is None:
            return
        ok = reply.error() == QNetworkReply.NoError
        data = reply.readAll() if ok else QByteArray()
        msg = reply.errorString()
        reply.deleteLater()
        if self._closed:
            return
        if not ok:
            if self._is_current(post.id):
                self._failed(f"Can't load #{post.id}: {msg}")
            return
        if post.ext == "gif":
            self._ready(post.id, data)
            return
        side = ViewerTab._screen_side()
        self._decoding.add(post.id)
        rel, serial = relay(), self._serial

        def work():
            rel.decoded.emit(serial, post.id, ViewerTab._decode_image(data, side, None))  # to the UI thread

        threading.Thread(target=work, name=f"slideshow #{post.id}", daemon=True).start()

    def _on_decoded(self, serial: int, pid: int, img):
        if serial != self._serial or self._closed or pid not in self._decoding:
            return
        self._decoding.discard(pid)
        if img is None:
            if self._is_current(pid):
                self._failed(f"Can't decode #{pid}")
            return
        self._ready(pid, QPixmap.fromImage(img))

    def _is_current(self, pid: int) -> bool:
        post = self.current()
        return post is not None and post.id == pid and not self._ended

    def _ready(self, pid: int, got):
        post, nxt = self.current(), self._peek_next()
        if not any(p is not None and p.id == pid for p in (post, nxt)):
            return  # moved on meanwhile
        self._cache[pid] = got
        if self._is_current(pid) and not self._shown:
            self._display(post, got)

    # ---- keys and overlay
    def _favorite(self):
        post = self.current()
        if post is not None and not self._ended:
            self.favorite_toggled.emit(post)
            self._update_info()
            self.poke()

    def _download(self):
        post = self.current()
        if post is not None and not self._ended and post.id not in self._queued:
            self._queued.add(post.id)
            self.download_requested.emit(post)
            self._update_info()
            self.poke()

    def _update_info(self):
        post = self.current()
        more = "+" if self._origin is not None and not self._origin._closed and self._origin.can_load_more() else ""
        parts = []
        if post is not None and not self._ended:
            parts.append(f"#{post.id}")
            parts.append(f"{self._i + 1} / {len(self._order)}{more}")
            if self.is_favorite(post.id):
                parts.append("♥")
            if post.id in self._queued:
                parts.append("↓ downloading")
        if self._waiting:
            parts.append("loading more…")
        if self._paused:
            parts.append("⏸ paused")
        self.info.setText(" · ".join(parts) or "Slideshow")
        self._place()

    def closeEvent(self, e):
        self._clear()
        for pid in list(self._replies):
            self._drop_reply(pid)
        super().closeEvent(e)
