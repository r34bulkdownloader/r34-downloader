"""Full screen post view and the slideshow."""
import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog

from r34dl import config, slideshow, ui
from r34dl.models import Post
from r34dl.ui import MainWindow


def mk(i, ext="jpg"):
    return Post(i, "h", f"https://x/{i}.{ext}", "", "", [], "s", 0, 10, 10)


class _NeverReply(QObject):
    finished = Signal()
    downloadProgress = Signal("qlonglong", "qlonglong")

    def abort(self): pass


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    return MainWindow()


OPTS = {**config.SLIDESHOW, "shuffle": False, "seconds": 5}


def test_slideshow_options_fall_back_one_by_one():
    got = config.slideshow_options({"seconds": 3, "shuffle": "yes", "video_max": True, "junk": 1})
    assert got == {**config.SLIDESHOW, "seconds": 3}
    assert config.slideshow_options(None) == config.SLIDESHOW


def test_fullscreen_borrows_media_and_gives_it_back(win):
    win.first.model.append([mk(1), mk(2)])
    win.open_viewer(win.first.model.posts[0], False, win.first)
    tab = win.tabs.currentWidget()
    tab.toggle_fullscreen()
    fs = tab._fs
    assert tab.stage.window() is fs and tab.controls.window() is fs
    tab.step(1)  # stepping works while full screen
    assert tab.post.id == 2 and "#2" in tab.fs_info.text()
    fs.close()
    assert tab._fs is None
    assert tab.stage.window() is win and tab._body.indexOf(tab.stage) == 0
    assert tab._body.indexOf(tab.controls) == tab._body.indexOf(tab.zoom_bar) + 1


def test_closing_a_fullscreen_tab_closes_its_window(win):
    win.first.model.append([mk(1)])
    win.open_viewer(win.first.model.posts[0], False, win.first)
    tab = win.tabs.currentWidget()
    tab.toggle_fullscreen()
    fs = tab._fs
    win._close_tab(win.tabs.indexOf(tab))
    assert tab._fs is None and fs._closed


def _show(posts, opts=OPTS, origin=None, win=None):
    s = slideshow.Slideshow(posts, win.nam, opts, origin=origin)
    s.go(1)
    return s


def test_slideshow_leaves_out_videos_and_gifs_when_asked(win):
    posts = [mk(1), mk(2, "mp4"), mk(3, "gif"), mk(4)]
    s = _show(posts, {**OPTS, "videos": False, "gifs": False}, win=win)
    assert [p.id for p in s._order] == [1, 4]


def test_slideshow_end_stops_or_loops(win):
    s = _show([mk(1), mk(2)], {**OPTS, "loop": False}, win=win)
    s.go(1)
    s.go(1)
    assert s._ended and s.current() is None and "End" in s.picture.text()
    s.go(-1)
    assert s.current().id == 2
    s = _show([mk(1), mk(2)], {**OPTS, "loop": True}, win=win)
    s.go(1)
    s.go(1)
    assert s.current().id == 1 and not s._ended


def test_slideshow_timer_runs_only_once_the_picture_is_shown_and_pause_keeps_time(win):
    s = _show([mk(1), mk(2)], win=win)
    assert not s._timer.isActive()  # still downloading
    from PySide6.QtGui import QPixmap
    pm = QPixmap(4, 4)
    s._ready(1, pm)
    assert s._timer.isActive() and s._timer.interval() == 5000
    s.toggle_pause()
    assert not s._timer.isActive() and s.pinned and "paused" in s.info.text()
    s.toggle_pause()
    assert s._timer.isActive() and s._timer.remainingTime() <= 5000 and not s.pinned
    s._on_timer()
    assert s.current().id == 2


def test_slideshow_downloads_ahead_only_the_next_post(win):
    s = _show([mk(1), mk(2), mk(3, "mp4"), mk(4)], win=win)
    assert set(s._replies) == {1, 2}
    s.go(1)
    assert set(s._replies) == {2}  # 1 dropped; 3 is a video (streamed when shown)
    s.go(1)
    assert set(s._replies) == {4}


class _Origin(QObject):
    fetch_finished = Signal(bool)

    def __init__(self, posts):
        super().__init__()
        self._closed = False
        self.more = True
        self.asked = 0
        self.model = type("M", (), {"posts": list(posts)})()

    def can_load_more(self):
        return self.more

    def load_more(self):
        self.asked += 1


def test_slideshow_fetches_the_next_page_at_the_end(win):
    o = _Origin([mk(1)])
    s = _show(o.model.posts, origin=o, win=win)
    s.go(1)
    assert s._waiting and o.asked == 1 and s.current().id == 1
    o.model.posts += [mk(2), mk(3)]
    o.fetch_finished.emit(True)
    assert not s._waiting and s.current().id == 2 and len(s._order) == 3


def test_slideshow_keeps_asking_past_empty_pages_but_stops_on_failure(win):
    o = _Origin([mk(1)])
    s = _show(o.model.posts, {**OPTS, "loop": False}, origin=o, win=win)
    s.go(1)
    o.model.posts += [mk(2, "mp4")]  # nothing it wants (videos left out below) -> asks again
    s._opts["videos"] = False
    o.fetch_finished.emit(True)
    assert s._waiting and o.asked == 2
    o.fetch_finished.emit(False)  # failed: no new request loop, the show ends
    assert not s._waiting and o.asked == 2 and s._ended


def test_start_slideshow_saves_options_and_quitting_closes_it(win, monkeypatch):
    win.first.model.append([mk(1), mk(2)])
    win.first.model.checked = {2}
    monkeypatch.setattr(slideshow.SlideshowDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(slideshow.Slideshow, "showFullScreen", lambda self: None)
    win.start_slideshow(win.first)
    show = win.slideshow
    assert [p.id for p in show._order] == [2] and show._origin is None  # only the ticked post
    assert config.load()["slideshow"]["seconds"] == config.SLIDESHOW["seconds"]
    win.close()
    assert win.slideshow is None and show._closed


def test_slideshow_stops_waiting_when_its_search_tab_goes_away(win):
    o = _Origin([mk(1)])
    s = _show(o.model.posts, {**OPTS, "loop": False}, origin=o, win=win)
    s.go(1)
    assert s._waiting
    o.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not s._waiting and s._ended and s._origin is None


def test_closed_slideshow_ignores_late_events(win):
    s = _show([mk(1), mk(2)], win=win)
    s.close()
    s.go(1)
    slideshow.relay().decoded.emit(s._serial, 1, None)
    assert s._i == 0 and not s._replies


def test_slideshow_decodes_only_for_itself_and_never_downloads_twice(win):
    s = _show([mk(1), mk(2)], win=win)
    s._decoding.add(2)
    s._replies.pop(2)
    s._prefetch()
    assert 2 not in s._replies  # still decoding: no second download
    slideshow.relay().decoded.emit(s._serial + 1000, 2, None)  # another show's result
    assert 2 in s._decoding


def test_whole_video_mode_has_a_watchdog(win):
    s = _show([mk(1, "mp4")], win=win)
    assert s._timer.isActive() and s._timer.interval() == slideshow.VIDEO_WAIT_MS
    s._on_media_status(slideshow.QMediaPlayer.LoadedMedia)
    assert not s._timer.isActive()
