"""The paced e-hentai client and Site, against a fake network manager (never the real site)."""
import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from r34dl import api
from r34dl.models import Post
from r34dl.sites import ehclient, ehentai
from r34dl.sites.ehclient import EhentaiSite, EhQueue

FIX = Path(__file__).parent / "fixtures" / "eh"


class FakeReply(QObject):
    finished = Signal()

    def __init__(self, url, status, body, err=QNetworkReply.NoError):
        super().__init__()
        self._url, self._status, self._body, self._err = QUrl(url), status, body, err

    def attribute(self, a):
        return self._status if a == QNetworkRequest.HttpStatusCodeAttribute else None

    def error(self):
        return self._err

    def errorString(self):
        return "boom"

    def readAll(self):
        return self._body

    def url(self):
        return self._url


class FakeNam:
    """Serves `routes` (url substring -> (status, body) or list of them, used in turn); records each send."""

    def __init__(self, routes):
        self.routes, self.sent, self.open, self.max_open = routes, [], 0, 0

    def _reply(self, req, body=None):
        url = req.url().toString()
        self.sent.append((time.monotonic(), url, bytes(req.rawHeader("Cookie")), body))
        self.open += 1
        self.max_open = max(self.max_open, self.open)
        key = next(k for k in self.routes if k in url)
        r = self.routes[key]
        status, data = r.pop(0) if isinstance(r, list) else r
        reply = FakeReply(url, status, data)

        def fin():
            self.open -= 1
            reply.finished.emit()
        QTimer.singleShot(5, fin)
        return reply

    def get(self, req):
        return self._reply(req)

    def post(self, req, body):
        return self._reply(req, bytes(body))


def spin_until(cond, timeout=10):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        loop = QEventLoop()
        QTimer.singleShot(10, loop.quit)
        loop.exec()
    assert cond()


def fast(q: EhQueue, gap=30, pause=150):
    q.gap_ms, q.burst_pause_ms, q.retry_delays_ms = gap, pause, (10, 10, 10)
    return q


def fix(name):
    return (FIX / name).read_bytes()


def test_queue_paces_requests(qapp):
    nam = FakeNam({"e-hentai.org": (200, b"ok")})
    q = fast(EhQueue(nam=nam))
    done = []
    for i in range(6):
        q.get(f"https://e-hentai.org/{i}", lambda s, b, e: done.append((s, e)))
    spin_until(lambda: len(done) == 6)
    assert done == [(200, "")] * 6 and nam.max_open == 1
    times = [t for t, *_ in nam.sent]
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    assert min(gaps) >= q.gap_ms - 2
    assert gaps[3] >= q.burst_pause_ms - 2  # before the 5th
    assert all(c == b"nw=1" for *_, c, _ in nam.sent)  # always skip content warnings, no login


def test_queue_defaults_and_login_cookie(qapp):
    q = EhQueue(nam=FakeNam({}))
    assert (q.gap_ms, q.burst_pause_ms, q.retry_delays_ms) == (1000, 5000, api.RETRY_DELAYS_MS)
    assert ehclient.EH_BURST == 4
    q.set_login("9", "abc")
    assert q.logged_in and q._cookie == b"ipb_member_id=9; ipb_pass_hash=abc; nw=1"
    q.set_login("9", "a;b")  # header injection / junk is not a login
    assert not q.logged_in and q._cookie == b"nw=1"


def test_queue_retries_busy_then_succeeds(qapp):
    nam = FakeNam({"e-hentai.org": [(503, b""), (429, b""), (200, b"ok")]})
    q = fast(EhQueue(nam=nam))
    got = []
    q.get("https://e-hentai.org/x", lambda s, b, e: got.append((s, b, e)))
    spin_until(lambda: got)
    assert got == [(200, b"ok", "")] and len(nam.sent) == 3


def test_queue_rejects_offsite_redirect(qapp):
    nam = FakeNam({"e-hentai.org": (200, b"ok")})
    orig = nam._reply

    def redirected(req, body=None):
        r = orig(req, body)
        r._url = QUrl("https://evil.example/")
        return r
    nam._reply = redirected
    q = fast(EhQueue(nam=nam))
    got = []
    q.get("https://e-hentai.org/x", lambda s, b, e: got.append(e))
    spin_until(lambda: got)
    assert got[0]


def test_queue_clear_drops_pending(qapp):
    nam = FakeNam({"e-hentai.org": (200, b"ok")})
    q = fast(EhQueue(nam=nam))
    first, got = [], []
    q.get("https://e-hentai.org/busy", lambda s, b, e: first.append(s))  # in flight, not dropped
    for i in range(3):
        q.get(f"https://e-hentai.org/{i}", lambda s, b, e, i=i: got.append((i, s, e)))
    q.clear()
    assert got == [(i, None, ehclient.CANCELLED) for i in range(3)]  # each fires once, right away
    spin_until(lambda: first)
    loop = QEventLoop()
    QTimer.singleShot(200, loop.quit)
    loop.exec()
    assert len(nam.sent) == 1 and first == [200] and len(got) == 3


def test_resolve_cancelled_by_clear(qapp):
    site = _site({"e-hentai.org": (200, b"ok")})
    site.queue.get("https://e-hentai.org/busy", lambda *a: None)
    out = []
    site.resolve(_page(), False, lambda p, err: out.append(err))
    site.queue.clear()
    assert out == [ehclient.CANCELLED]


def test_cancelled_search_is_silent_and_bad_gdata_fails(qapp):
    site = _site({"busy": (200, b""), "api.e-hentai.org": (200, b'{"gmetadata": 5}'), "f_search": (200, fix("search.html"))})
    got, bad = [], []
    site.page_ready.connect(lambda *a: got.append(a))
    site.failed.connect(lambda g, m: bad.append((g, m)))
    site.queue.get("https://e-hentai.org/busy", lambda *a: None)
    site.fetch("x", None, 1, {})
    site.queue.clear()  # generation 1 is stale: no page_ready, no failed
    site.fetch("x", None, 2, {})
    spin_until(lambda: bad)
    assert not got and [g for g, _ in bad] == [2]


def _site(routes):
    site = EhentaiSite()
    site.queue._nam = FakeNam(routes)
    fast(site.queue)
    return site


def test_fetch_search_then_gdata(qapp):
    site = _site({"api.e-hentai.org": (200, fix("gdata.json")), "f_search": (200, fix("search.html"))})
    got, bad = [], []
    site.page_ready.connect(lambda g, posts, nxt: got.append((g, posts, nxt)))
    site.failed.connect(lambda g, m: bad.append(m))
    site.fetch('"x$"', None, 5, {"sites": {"e-hentai": {"member_id": "", "pass_hash": ""}}})
    spin_until(lambda: got or bad)
    assert not bad
    sent = site.queue._nam.sent
    assert "f_search=" in sent[0][1] and sent[1][1] == ehentai.EH_API and b'"gdata"' in sent[1][3]
    gen, posts, nxt = got[0]
    assert gen == 5 and posts and all(p.kind == "gallery" for p in posts)
    assert nxt is not None
    assert all(p.preview_url == "" or p.preview_url.startswith("https://") for p in posts)


def test_fetch_gallery_and_warning(qapp):
    from r34dl.sites.ehentai import parse_gdata
    gallery = [g for g in parse_gdata(fix("gdata.json")) if g.album["gid"] == 4348914][0]
    site = _site({"/g/": [(200, fix("gallery.html")), (200, fix("warning.html"))]})
    got, bad = [], []
    site.page_ready.connect(lambda g, posts, nxt: got.append((posts, nxt)))
    site.failed.connect(lambda g, m: bad.append(m))
    site.fetch_gallery(gallery, None, 1)
    spin_until(lambda: got)
    assert got[0][0] and all(p.kind == "page" and 1 <= p.album["page"] < 100000 for p in got[0][0])
    site.fetch_gallery(gallery, None, 2)
    spin_until(lambda: bad)
    assert bad == ["This gallery is behind a content warning that the app couldn't skip."]


def _page():
    return Post(4348914 * 100000 + 5, "", "", "", "", [], "", 0, 0, 0, site="e-hentai", kind="page",
                page_url="https://e-hentai.org/s/af53eed799/4348914-5", album={"gid": 4348914, "page": 5},
                file_name="2.png")


def _resolve(site, post, for_download):
    out = []
    site.resolve(post, for_download, lambda p, err: out.append(err))
    spin_until(lambda: out)
    return out[0]


def test_resolve_prefers_original_only_when_logged_in_and_downloading(qapp):
    site = _site({"/s/": (200, fix("picture_original.html"))})
    full = "https://e-hentai.org/fullimg/4348914/4/14j5vtiaotb/2.png"
    p = _page()
    assert _resolve(site, p, True) is None and p.file_url != full and "hath.network" in p.file_url  # not logged in
    site.queue.set_login("9", "abc")
    p = _page()
    assert _resolve(site, p, False) is None and "hath.network" in p.file_url  # viewing: the resized copy
    p2 = _page()
    assert _resolve(site, p2, True) is None and p2.file_url == full
    n = len(site.queue._nam.sent)
    assert _resolve(site, p, False) is None and len(site.queue._nam.sent) == n  # cached: no second request
    assert p.album["shown"] == p.file_url


def test_resolve_rejects_private_host(qapp):
    html = b'<img id="img" src="http://192.168.0.5/x.jpg">'
    site = _site({"/s/": (200, html)})
    p = _page()
    assert _resolve(site, p, False) is not None and p.file_url == ""


def test_resolve_image_limit(qapp):
    site = _site({"/s/": (200, fix("picture_509.html"))})
    assert _resolve(site, _page(), False) == "IMAGE_LIMIT"


def test_site_basics(qapp):
    from r34dl.sites import make_sites
    sites = make_sites(None)
    s = sites["e-hentai"]
    assert isinstance(s, EhentaiSite) and s.name == "e-hentai" and not s.page_jump
    assert s.suggest_request("x") is None and s.post_page_url(_page()) == _page().page_url


def test_open_in_browser_uses_page_url(qapp, monkeypatch):
    from r34dl import widgets
    opened = []
    monkeypatch.setattr(widgets.QDesktopServices, "openUrl", lambda u: opened.append(u.toString()) or True)
    widgets.open_in_browser(_page())
    widgets.open_in_browser(Post(7, "", "", "", "", [], "", 0, 0, 0))
    assert opened == [_page().page_url, api.post_page_url(7)]


def test_scrub_masks_cookies():
    s = api.scrub("ipb_pass_hash=abc; ipb_member_id=9 api_key=k")
    assert "abc" not in s and "9" not in s and "=k" not in s


def test_view_resolve_jumps_queued_downloads(qapp):
    site = _site({"/s/": (200, fix("picture.html"))})
    for n in range(1, 6):
        p = _page()
        p.page_url = f"https://e-hentai.org/s/af53eed799/4348914-{n}"
        site.resolve(p, True, lambda *a: None)
    view = _page()
    view.page_url = "https://e-hentai.org/s/af53eed799/4348914-99"
    site.resolve(view, False, lambda *a: None)
    spin_until(lambda: len(site.queue._nam.sent) >= 2)
    assert site.queue._nam.sent[1][1] == view.page_url


def test_download_resolves_again_when_logged_in_without_a_cached_original(qapp):
    site = _site({"/s/": (200, fix("picture_original.html"))})
    p = _page()
    p.album.update(shown="https://x.hath.network/a.png", original="")  # seen while logged out
    site.queue.set_login("9", "abc")
    assert _resolve(site, p, True) is None and "/fullimg/" in p.file_url


# ---- the download feeder

class FakeDownloader(QObject):
    item_done = Signal(str, str, bool)
    limited = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.added = []

    def add(self, items):
        self.added += items

    def finish(self, ok=True):
        url, target = self.added[-1][:2]
        Path(target).write_bytes(b"x")
        self.item_done.emit(url, str(target), ok)


class FakeSite:
    def __init__(self, logged_in=False):
        self.queue = EhQueue(nam=FakeNam({}))
        self.queue.set_login("9", "abc") if logged_in else None
        self.calls, self.answers = [], {}

    def resolve(self, post, for_download, cb):
        assert for_download
        self.calls.append(post)
        post.file_url = f"https://h.hath.network/{post.id}.jpg"
        cb(post, self.answers.get(post.id))


def _pages(n):
    out = []
    for i in range(1, n + 1):
        p = _page()
        p.id, p.album = 4348914 * 100000 + i, {"gid": 4348914, "page": i}
        out.append(p)
    return out


def test_feeder_one_at_a_time(qapp, tmp_path):
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    f.add([(p, tmp_path / f"{i}.jpg") for i, p in enumerate(_pages(3))])
    assert len(site.calls) == 1 and len(dl.added) == 1
    dl.item_done.emit("https://other/x.jpg", str(tmp_path / "other.jpg"), True)  # someone else's item
    assert len(site.calls) == 1
    dl.finish()
    assert len(site.calls) == 2 and len(dl.added) == 2
    dl.finish(ok=False)  # a failed download moves on too
    assert len(site.calls) == 3
    f.cancel()
    dl.finish()
    assert len(site.calls) == 3 and not f.active


def test_feeder_pauses_on_509(qapp, tmp_path):
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    pages = _pages(3)
    site.answers[pages[1].id] = "IMAGE_LIMIT"
    paused = []
    f.paused.connect(paused.append)
    f.add([(p, tmp_path / f"{i}.jpg") for i, p in enumerate(pages)])
    dl.finish()
    assert paused == ["IMAGE_LIMIT"] and site.calls == pages[:2] and len(dl.added) == 1
    assert (tmp_path / "0.jpg").exists() and f.active
    del site.answers[pages[1].id]
    f.resume()
    assert site.calls[2] is pages[1] and len(dl.added) == 2 and paused == ["IMAGE_LIMIT"]


def test_feeder_failed_resolve_reports_and_moves_on(qapp, tmp_path):
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    pages = _pages(2)
    site.answers[pages[0].id] = "Could not read the picture page."
    site.answers[pages[1].id] = ehclient.CANCELLED
    bad = []
    f.failed.connect(lambda page, target: bad.append((page, target)))
    f.add([(pages[0], tmp_path / "0.jpg")])
    assert bad == [(pages[0].page_url, str(tmp_path / "0.jpg"))] and not f.active


def test_feeder_original_has_cookie(qapp, tmp_path):
    for logged_in in (True, False):
        site, dl = _site({"/s/": (200, fix("picture_original.html"))}), FakeDownloader()
        if logged_in:
            site.queue.set_login("9", "abc")
        f = ehclient.EhDownloadFeeder(site, dl)
        p = _page()
        f.add([(p, tmp_path / "a.png")])
        spin_until(lambda: dl.added)
        item = dl.added[0]
        if logged_in:
            assert "/fullimg/" in item[0] and item[2] == {b"Cookie": b"ipb_member_id=9; ipb_pass_hash=abc; nw=1"}
        else:
            # the resized copy of 2.png is a .webp: saved under that type
            assert item == (p.album["shown"], tmp_path / "a.webp") and "hath.network" in item[0]


def test_large_batch_asks(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from r34dl import config
    from r34dl.ui import MainWindow
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    win = MainWindow()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    win.download_posts(_pages(201))
    assert len(asked) == 1 and asked[0].startswith("Download 201 pictures from e-hentai?")
    assert "about 7 minutes" in asked[0]
    assert not win.feeder.active and not win.downloader.active
    gal = _page()
    gal.kind, gal.album = "gallery", {"gid": 1, "token": "t", "title": "T", "pages": 300}
    win.download_posts([gal])  # asked from the gallery's page count, before listing
    assert len(asked) == 2 and "300 pictures" in asked[1] and not win.feeder.active
    win.download_posts(_pages(200))  # not more than EH_WARN_PICTURES: no question
    assert len(asked) == 2 and win.feeder.active
    win.cancel_downloads()
    assert not win.feeder.active


def test_list_all_pages_walks_until_no_next_p(qapp):
    site = EhentaiSite()
    asked = []

    def fake_fetch(gallery, p, gen):
        asked.append(p)
        site.page_ready.emit(gen + 1000, _pages(1), None)  # someone else's generation: ignored
        nxt = (p or 0) + 1
        QTimer.singleShot(1, lambda: site.page_ready.emit(gen, _pages(2)[nxt - 1:nxt], nxt if nxt < 2 else None))
    site.fetch_gallery = fake_fetch
    out = []
    site.list_all_pages(_page(), lambda pages, err: out.append(([p.album["page"] for p in pages], err)))
    spin_until(lambda: out)
    assert out == [([1, 2], None)] and asked == [None, 1]
    site.page_ready.emit(-1, _pages(1), None)  # disconnected once done: no second answer
    assert len(out) == 1


def test_list_all_pages_reports_failure_with_what_it_has(qapp):
    from r34dl.sites.ehentai import parse_gdata
    gallery = [g for g in parse_gdata(fix("gdata.json")) if g.album["gid"] == 4348914][0]
    site = _site({"/g/": [(200, fix("gallery.html")), (200, fix("warning.html"))]})
    out = []
    site.list_all_pages(gallery, lambda pages, err: out.append((pages, err)))
    spin_until(lambda: out)
    assert len(out[0][0]) == 20 and out[0][1] == ehclient.WARNING_MSG


def test_cookie_only_goes_to_e_hentai_fullimg(qapp, tmp_path):
    site, dl = _site({}), FakeDownloader()
    site.queue.set_login("9", "abc")
    f = ehclient.EhDownloadFeeder(site, dl)
    p = _page()
    p.album.update(shown="https://h.hath.network/a.jpg", original="https://attacker.example/x")  # tampered favorite
    f.add([(p, tmp_path / "a.png")])
    assert dl.added == [("https://attacker.example/x", tmp_path / "a.png")]  # no Cookie header


def test_feeder_pauses_when_the_downloader_reports_the_limit(qapp, tmp_path):
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    pages = _pages(2)
    paused = []
    f.paused.connect(paused.append)
    f.add([(p, tmp_path / f"{i}.jpg") for i, p in enumerate(pages)])
    dl.limited.emit(dl.added[0][0], str(tmp_path / "0.jpg"))
    assert paused == ["IMAGE_LIMIT"] and len(site.calls) == 1 and f.active
    f.resume()
    assert site.calls == [pages[0], pages[0]] and len(dl.added) == 2  # the same picture again


def test_queue_too_fast_page_is_an_error_and_holds_the_queue(qapp):
    # ehwiki "Technical Issues": e-hentai answers 200 with a short plain-text warning or temporary IP ban
    warn = (b"You are opening pages too fast, thus placing a heavy load on the server. "
            b"Back down, or your IP address will be automatically banned.")
    nam = FakeNam({"e-hentai.org": [(200, warn), (200, b"ok")]})
    q = fast(EhQueue(nam=nam))
    got = []
    q.get("https://e-hentai.org/a", lambda s, b, e: got.append(e))
    spin_until(lambda: got)
    assert got == [ehclient.BLOCKED_MSG]
    assert q._next_at - time.monotonic() > 50  # nothing else is sent for about a minute
    big = b"<html>" + b"x" * 5000 + b"a comment: I was temporarily banned once</html>"
    nam.routes["e-hentai.org"] = (200, big)
    q._next_at = 0
    q.get("https://e-hentai.org/g/1/0123456789/", lambda s, b, e: got.append(e))
    spin_until(lambda: len(got) == 2)
    assert got[1] == ""  # a real page that merely mentions it is fine


def test_feeder_pauses_when_blocked(qapp, tmp_path):
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    pages = _pages(2)
    site.answers[pages[0].id] = ehclient.BLOCKED_MSG
    paused = []
    f.paused.connect(paused.append)
    f.add([(p, tmp_path / f"{i}.jpg") for i, p in enumerate(pages)])
    assert paused == [ehclient.BLOCKED_MSG] and len(site.calls) == 1 and not dl.added and f.active


def test_feeder_names_the_file_by_the_type_it_got(qapp, tmp_path):
    # live check: page_1.png's resized copy is a .webp; it must not be saved as .png
    site, dl = FakeSite(), FakeDownloader()
    f = ehclient.EhDownloadFeeder(site, dl)
    moved = []
    f.retargeted.connect(lambda old, new: moved.append((old, new)))
    f.add([(_pages(1)[0], tmp_path / "ehentai_1_001.png")])
    assert dl.added[0][1] == tmp_path / "ehentai_1_001.jpg"
    assert moved == [(str(tmp_path / "ehentai_1_001.png"), str(tmp_path / "ehentai_1_001.jpg"))]
    dl.finish()
    f.add([(_pages(2)[1], tmp_path / "ehentai_1_002.jpg")])  # same type: kept
    assert dl.added[1][1] == tmp_path / "ehentai_1_002.jpg" and len(moved) == 1
