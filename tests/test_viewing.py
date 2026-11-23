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


# ---- lazy file address (e-hentai pages are resolved just before loading)
from r34dl.sites.ehclient import CANCELLED  # noqa: E402
from r34dl.together import TogetherTab  # noqa: E402
from r34dl.viewer import ViewerTab  # noqa: E402


def page(i):
    return Post(100000 + i, "", "", "", "", [], "", 0, 0, 0, site="e-hentai", kind="page",
                page_url=f"https://e-hentai.org/s/abc/1-{i}", file_name=f"{i}.jpg",
                album={"gid": 1, "title": "My Gallery", "page": i, "pages": 9, "category": "Manga"})


class _Resolver:
    """Holds the callbacks until fire(); records which posts were asked for."""

    def __init__(self, auto=False):
        self.calls, self.auto = [], auto

    def __call__(self, post, cb):
        self.calls.append((post, cb))
        if self.auto:
            self.fire(post)

    def fire(self, post, err=None):
        cb = next(c for p, c in self.calls if p is post)
        if err is None:
            post.file_url = f"https://img.test/{post.id}.jpg"
        cb(post, err)


@pytest.fixture
def got(win, monkeypatch):
    urls = []

    def get(self, req):
        urls.append(req.url().toString())
        return _NeverReply()
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", get)
    return urls


def _viewer(win, posts, resolve):
    win.first.model.append(posts)
    return ViewerTab(posts[0], win.first.model, win.nam, resolve=resolve)


def test_viewer_resolves_before_loading(win, got):
    r = _Resolver()
    tab = _viewer(win, [page(1)], r)
    assert got == [] and len(r.calls) == 1
    r.fire(tab.post)
    assert got == ["https://img.test/100001.jpg"]


def test_viewer_ignores_late_resolve(win, got):
    r = _Resolver()
    tab = _viewer(win, [page(1), page(2)], r)
    first = tab.post
    tab.step(1)
    r.fire(first)
    assert got == []  # the old post's address came too late
    r.fire(tab.post)
    assert got == ["https://img.test/100002.jpg"]


def test_viewer_image_limit_message(win, got):
    r = _Resolver()
    tab = _viewer(win, [page(1), page(2)], r)
    r.fire(tab.post, "IMAGE_LIMIT")
    assert tab.media.text() == ("e-hentai's image limit is reached. Try again later, or log in in Settings "
                                "for a higher limit.")
    tab.step(1)
    r.fire(tab.post, CANCELLED)
    assert "cancel" not in tab.media.text().lower() and got == []


def test_viewer_info_for_an_ehentai_page(win, got):
    tab = _viewer(win, [page(3)], _Resolver())
    titles = []
    tab.title_changed.connect(titles.append)
    tab.show_post(tab.post)
    text = tab.info.text()
    assert "My Gallery" in text and "page 3 of 9" in text and "Manga" in text and "score" not in text
    assert titles == [tab.post.label]


def test_main_window_resolve_goes_to_the_posts_site(win, monkeypatch):
    seen = []
    monkeypatch.setattr(win.sites["e-hentai"], "resolve", lambda p, dl, cb: seen.append((p, dl)))
    p = page(1)
    win.resolve(p, lambda *_: None)
    assert seen == [(p, False)]


def test_slideshow_resolves_current_and_only_the_next_one(win, got):
    r = _Resolver()
    posts = [page(1), page(2), page(3)]
    s = slideshow.Slideshow(posts, win.nam, OPTS, resolve=r)
    s.go(1)
    assert [p.id for p, _ in r.calls] == [100001, 100002] and got == []
    r.fire(posts[0])
    assert got == ["https://img.test/100001.jpg"]
    s.go(1)
    r.fire(posts[0])  # nothing new: post 1 was already resolved and is not shown/next
    assert [p.id for p, _ in r.calls] == [100001, 100002, 100003]
    r.fire(posts[1], "IMAGE_LIMIT")
    assert "image limit" in s.picture.text()


def test_slideshow_ignores_a_resolve_for_a_post_it_left(win, got):
    r = _Resolver()
    posts = [page(1), page(2), page(3), page(4)]
    s = slideshow.Slideshow(posts, win.nam, OPTS, resolve=r)
    s.go(1)
    s.go(1)
    s.go(1)  # now on 3; 1 is neither shown nor next
    r.fire(posts[0])
    assert "https://img.test/100001.jpg" not in got


def test_together_resolves_ehentai_pages_but_keeps_rule34_samples(win, got):
    r34 = Post(7, "h", "https://x/7.jpg", "", "https://x/s7.jpg", [], "s", 0, 10, 10)
    r = _Resolver(auto=True)
    TogetherTab([r34, page(1)], win.nam, resolve=r)
    assert got == ["https://x/s7.jpg", "https://img.test/100001.jpg"]


def test_slideshow_pauses_on_the_image_limit(win):
    s = slideshow.Slideshow([mk(1), mk(2)], win.nam, OPTS, resolve=lambda p, cb: cb(p, "IMAGE_LIMIT"))
    s.go(1)
    assert s._paused and not s._timer.isActive() and s.current().id == 1
    assert "image limit" in s.picture.text()


def test_slideshow_decode_relay_carries_eh_sized_ids(win):
    big = 4349020 * 100000 + 2  # e-hentai page ids pass 32 bits: a Signal(int) argument overflowed (live check)
    s = _show([mk(1), mk(2)], win=win)
    s._decoding.add(big)
    slideshow.relay().decoded.emit(s._serial, big, None)
    assert big not in s._decoding
