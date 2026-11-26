"""e-hentai.org over QtNetwork: a paced request queue (EhQueue) and the Site the search tabs talk to."""
import itertools
import logging
import re
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .. import api
from ..models import Post
from . import Site
from . import ehentai as eh

# Settings → Download speed. "fast" is the pace of the widely used "[E/Ex-Hentai] Downloader" userscript:
# a page request every 100 ms, 6 at a time, and when throttled up to 10 retries 2 s, 3 s, … 11 s apart.
# parallel: e-hentai requests in flight, and pictures the feeder works on at once.
SPEEDS = {
    "polite": dict(gap_ms=1000, parallel=1, burst=4, burst_pause_ms=5000, retry_delays_ms=api.RETRY_DELAYS_MS),
    "fast": dict(gap_ms=100, parallel=6, burst=0, burst_pause_ms=0, retry_delays_ms=tuple(range(2000, 12000, 1000))),
}
EH_WARN_PICTURES = 200  # a bigger download batch asks first
CANCELLED = "cancelled"  # error given to a job whose alive() said it is no longer wanted
WARNING_MSG = "This gallery is behind a content warning that the app couldn't skip."
BLOCKED_MSG = ("e-hentai says pages are being opened too fast, or has blocked this IP address for a while. "
               "Wait (it can take an hour) and try again.")
_BLOCKED_TEXTS = (b"opening pages too fast", b"temporarily banned")  # its short plain-text warning/ban pages
EH_BLOCKED_PAUSE_MS = 60000  # after such a page nothing is sent for a minute
_IMAGE_EXT = ("jpg", "jpeg", "png", "gif", "webp")
_ORIGINS = ("https://e-hentai.org/", "https://api.e-hentai.org/")
_list_gens = itertools.count(-1, -1)  # list_all_pages' generations: search tabs use positive ones

log = logging.getLogger(__name__)


class EhQueue(QObject):
    """Up to `parallel` e-hentai requests at once, sent gap_ms apart (burst_pause_ms after every `burst`), with a
    backoff on 429/503; at the fast speed also on network errors and "too fast" pages, as the userscript does. cb(status or None, body, error) where error "" means success."""

    def __init__(self, parent=None, nam=None):
        super().__init__(parent)
        self._nam = nam or QNetworkAccessManager(self)
        self.set_speed("polite")
        self._jobs: deque = deque()  # (url, body or None, cb, attempt, alive or None)
        self._low: deque = deque()  # download jobs: sent only when nothing else waits (viewing comes first)
        self._in_flight = 0
        self._sent = 0
        self._next_at = 0.0  # monotonic seconds before which nothing is sent
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._pump)
        self.set_login("", "")

    def set_login(self, member_id: str, pass_hash: str) -> None:
        ok = all(re.fullmatch(r"[\w-]+", str(v)) for v in (member_id, pass_hash))  # no header injection
        self.logged_in = ok
        self._cookie = f"ipb_member_id={member_id}; ipb_pass_hash={pass_hash}; nw=1".encode() if ok else b"nw=1"

    def set_speed(self, speed: str) -> None:
        self.fast = speed == "fast"
        for k, v in SPEEDS["fast" if self.fast else "polite"].items():
            setattr(self, k, v)

    def get(self, url: str, cb, low: bool = False, alive=None) -> None:
        """alive(): checked just before sending; once it says False the job is dropped, cb gets CANCELLED."""
        if low:
            self._low.append((url, None, cb, 0, alive))
            return self._pump()
        self._add((url, None, cb, 0, alive))

    def post(self, url: str, body: bytes, cb) -> None:
        self._add((url, body, cb, 0, None))

    def _add(self, job, front=False) -> None:
        self._jobs.appendleft(job) if front else self._jobs.append(job)
        self._pump()

    def _pump(self) -> None:
        while self._in_flight < self.parallel and (self._jobs or self._low):
            wait_ms = (self._next_at - time.monotonic()) * 1000
            if wait_ms > 0:
                self._timer.start(int(wait_ms) + 1)
                return
            job = (self._jobs or self._low).popleft()
            if job[4] is not None and not job[4]():
                job[2](None, b"", CANCELLED)  # e.g. the viewer moved on: no paced request is spent on it
                continue
            self._send(job)

    def _send(self, job) -> None:
        url, body, cb, attempt, alive = job
        self._in_flight += 1
        self._sent += 1
        gap = self.burst_pause_ms if self.burst and self._sent % self.burst == 0 else self.gap_ms
        self._next_at = time.monotonic() + gap / 1000
        req = api.request(url)
        req.setRawHeader(b"Cookie", self._cookie)
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.SameOriginRedirectPolicy)
        if body is None:
            reply = self._nam.get(req)
        else:
            req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
            reply = self._nam.post(req, body)
        reply.finished.connect(lambda: self._done(reply, job))

    def _done(self, reply, job) -> None:
        url, body, cb, attempt, alive = job
        reply.deleteLater()
        self._in_flight -= 1
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        data = bytes(reply.readAll())
        blocked = len(data) < 4096 and any(t in data for t in _BLOCKED_TEXTS)  # real pages are far bigger
        throttled = status in (429, 503) or (self.fast and (
            blocked or (reply.error() != QNetworkReply.NoError and not (isinstance(status, int) and status < 500))))
        if throttled and attempt < len(self.retry_delays_ms):
            log.info("e-hentai busy (HTTP %s), retry %d", status, attempt + 1)
            self._next_at = max(self._next_at, time.monotonic() + self.retry_delays_ms[attempt] / 1000)
            self._add((url, body, cb, attempt + 1, alive), front=True)
            return
        if status in (429, 503):
            err = "e-hentai is rate limiting requests. Try again later."
        elif not reply.url().toString().startswith(_ORIGINS):
            err = "e-hentai redirected to another site."
        elif reply.error() != QNetworkReply.NoError:
            err = api.scrub(reply.errorString())
        elif blocked:
            err = BLOCKED_MSG
            self._next_at = max(self._next_at, time.monotonic() + EH_BLOCKED_PAUSE_MS / 1000)
        else:
            err = ""
        if err:
            log.warning("e-hentai request failed: %s", err)
        try:
            cb(status, data, err)
        finally:
            self._pump()


class EhentaiSite(Site):
    name = "e-hentai"
    label = "e-hentai"
    page_jump = False
    tag_hint = ("<b>Search</b> works like the site's search box: words match gallery titles, and tags use "
                '<b>female:"long hair$"</b> or <b>language:english</b>. '
                "<b>Exclude</b>: one tag per line or comma-separated; spaces are part of the tag.")
    include_placeholder = 'for example:\nmy title words\nlanguage:english female:"long hair$"'
    exclude_placeholder = "for example:\nmale:yaoi"

    def include_term(self, tag: str) -> str:
        return eh.include_term(tag)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue = EhQueue(self)

    def split_tags(self, text: str) -> list[str]:
        return eh.split_tags(text)

    def build_query(self, include: str, exclude: str, blacklist: list[str]) -> str:
        return eh.build_query(include, exclude, blacklist)

    def fetch(self, query: str, cursor, generation: int, cfg: dict) -> None:
        from .. import config
        sc = config.site_cfg(cfg, self.name)
        self.queue.set_login(sc["member_id"], sc["pass_hash"])

        def on_search(status, body, err):
            if err:
                return self.failed.emit(generation, err)
            html = body.decode("utf-8", "replace")
            pairs, nxt = eh.parse_search(html)
            total = eh.parse_total(html) if cursor is None else None
            if total is not None:
                self.total_found.emit(generation, total[0], total[1])
            self._gdata(pairs, [], generation, nxt)
        self.queue.get(eh.search_url(query, cursor), on_search)

    def _gdata(self, pairs, done: list, gen: int, nxt) -> None:
        """Ask gdata for 25 galleries at a time, then emit them all."""
        if not pairs:
            return self.page_ready.emit(gen, done, nxt)

        def on_gdata(status, body, err):
            if err:
                return self.failed.emit(gen, err)
            try:
                posts = eh.parse_gdata(body)
            except (ValueError, AttributeError, TypeError, OverflowError):
                return self.failed.emit(gen, "Could not read e-hentai's gallery data.")
            for p in posts:
                if not eh.safe_image_url(p.preview_url):
                    p.preview_url = ""
            self._gdata(pairs[25:], done + posts, gen, nxt)
        self.queue.post(eh.EH_API, eh.gdata_body(pairs[:25]), on_gdata)

    def fetch_gallery(self, gallery: Post, p: int | None, gen: int) -> None:
        def on_page(status, body, err):
            if err:
                return self.failed.emit(gen, err)
            try:
                pages, nxt = eh.parse_gallery(body.decode("utf-8", "replace"), gallery)
            except eh.GalleryWarning:
                return self.failed.emit(gen, WARNING_MSG)
            except ValueError:
                return self.failed.emit(gen, "Could not read the gallery page.")
            pages = [q for q in pages if 1 <= q.album["page"] < 100000]  # keeps ids gid*100000+n unique
            for q in pages:
                if not eh.safe_image_url(q.preview_url):
                    q.preview_url = ""
            self.page_ready.emit(gen, pages, nxt)
        self.queue.get(eh.gallery_url(gallery.album["gid"], gallery.album["token"], p), on_page)

    def post_page_url(self, post: Post) -> str:
        return post.page_url

    def resolve(self, post: Post, for_download: bool, cb, alive=None) -> None:
        if post.kind != "page":
            return super().resolve(post, for_download, cb)
        if not post.page_url.startswith(eh.EH + "/s/"):
            return cb(post, "This picture's page is not an e-hentai.org link.")
        # a copy seen while logged out has no original: a logged-in download asks again
        if "shown" in post.album and not (for_download and self.queue.logged_in and not post.album.get("original")):
            return self._pick(post, for_download, cb)

        def on_page(status, body, err):
            if err:
                return cb(post, err)
            try:
                shown, original = eh.parse_picture(body.decode("utf-8", "replace"))
            except eh.ImageLimit:
                return cb(post, "IMAGE_LIMIT")
            except ValueError:
                return cb(post, "Could not read the picture page.")
            post.album["shown"] = shown
            post.album["original"] = original if original and eh.safe_image_url(original) else ""
            self._pick(post, for_download, cb)
        self.queue.get(post.page_url, on_page, low=for_download, alive=alive)

    def list_all_pages(self, gallery: Post, cb):
        """Every page post of the gallery, walking fetch_gallery until there is no next p; then
        cb(pages, error or None). On an error, pages holds what was listed before it.
        Returns stop(): no further pages are asked for and cb is never called."""
        gen, got, over = next(_list_gens), [], []

        def ready(g, pages, nxt):
            if g != gen:
                return
            got.extend(pages)
            if nxt is None:
                return done(None)
            self.fetch_gallery(gallery, nxt, gen)

        def bad(g, msg):
            if g == gen:
                done(msg)

        def stop():
            if not over:
                over.append(1)
                self.page_ready.disconnect(ready)
                self.failed.disconnect(bad)

        def done(err):
            stop()
            cb(got, err)
        self.page_ready.connect(ready)
        self.failed.connect(bad)
        self.fetch_gallery(gallery, None, gen)
        return stop

    def _pick(self, post: Post, for_download: bool, cb) -> None:
        original, shown = post.album.get("original"), post.album.get("shown")
        url = original if for_download and self.queue.logged_in and original else shown
        if not isinstance(url, str) or not eh.safe_image_url(url):  # the album may come from a saved favorite
            return cb(post, "The picture's address was refused.")
        post.file_url = url
        cb(post, None)


class EhDownloadFeeder(QObject):
    """Hands e-hentai pages to the downloader, the queue's `parallel` at a time: resolve (paced by the site queue), download,
    and after an item_done the next. Originals get the login cookie; resized H@H copies never do."""

    paused = Signal(str)  # "IMAGE_LIMIT" or BLOCKED_MSG: stopped, the rest is kept until resume()
    failed = Signal(str, str)  # picture page, target: it couldn't be resolved (so ZIP jobs still complete)
    retargeted = Signal(str, str)  # old target, new one: the picture found is another type than the page's name said
    originals_refused = Signal()  # once: e-hentai answered an original with a page; resized copies from now on

    def __init__(self, site: EhentaiSite, downloader, parent=None):
        super().__init__(parent)
        self.site, self.downloader = site, downloader
        self._jobs: deque = deque()  # (post, target path)
        self._resolving = 0
        self._active: dict[str, tuple] = {}  # target text -> (post, target) being downloaded, for limit answers
        self._posts: dict[str, Post] = {}  # target -> post, handed to the downloader and not done yet (for retry)
        self._unresolved: list = []  # (post, target) whose picture page couldn't be resolved
        self._paused = False
        self._no_originals = False  # set once an original was refused (a wrong login): resized copies only
        self._gen = 0
        downloader.item_done.connect(self._on_item_done)
        downloader.limited.connect(self._on_limited)
        downloader.refused.connect(self._on_refused)

    @property
    def active(self) -> bool:
        return bool(self._jobs or self._resolving or self._active)

    @property
    def waiting(self) -> int:
        return len(self._jobs)

    def add(self, jobs) -> None:
        self._jobs.extend(jobs)
        self._next()

    def resume(self) -> None:
        self._paused = False
        self._next()

    def cancel(self) -> None:
        self._gen += 1  # a resolve still waiting in the site queue is dropped there, unsent
        self._jobs.clear()
        self._unresolved.clear()
        self._active.clear()
        self._resolving = 0
        self._paused = self._no_originals = False

    def retry_failed(self) -> None:
        """Failed pictures again, through the paced lookups: their addresses may have expired, and the
        downloader would send them all at once."""
        jobs = self._unresolved + [(self._posts.pop(t), t) for t in self.downloader.take_failed(self._posts)]
        self._unresolved = []
        for post, _ in jobs:
            post.album.pop("shown", None)
            post.album.pop("original", None)
        if jobs:
            self.add(jobs)

    def forget_failed(self, under: Path) -> None:
        """Like Downloader.forget_failed: a finished or cancelled ZIP's folder is gone."""
        self._unresolved = [(p, t) for p, t in self._unresolved if under not in Path(t).parents]
        self._posts = {t: p for t, p in self._posts.items() if under not in Path(t).parents}

    def _next(self) -> None:
        while not self._paused and self._jobs and self._resolving + len(self._active) < self._limit():
            post, target = self._jobs.popleft()
            self._resolving += 1
            gen = self._gen
            self.site.resolve(post, True, lambda p, err, t=target, g=gen: self._resolved(p, t, err, g),
                              alive=lambda g=gen: g == self._gen)

    def _limit(self) -> int:
        # Fast: as many as the downloader runs at once (it adapts 5–16); the queue still paces the page loads
        return self.downloader.concurrency if self.site.queue.fast else self.site.queue.parallel

    def _refresh(self, post: Post, cb) -> None:
        """Before a Fast retry: the picture's page again, for a fresh address. cb(address or None)."""
        post.album.pop("shown", None)
        post.album.pop("original", None)
        gen = self._gen

        def done(p, err):
            if err or gen != self._gen:
                return cb(None)
            if self._no_originals and p.file_url == p.album.get("original"):
                p.file_url = p.album.get("shown") or p.file_url
            cb(p.file_url)
        self.site.resolve(post, True, done, alive=lambda: gen == self._gen)

    def _pause(self, job, why: str) -> None:
        self._jobs.appendleft(job)
        if not self._paused:  # the other pictures in flight may answer the same
            self._paused = True
            self.paused.emit(why)

    def _resolved(self, post: Post, target, err, gen: int) -> None:
        if gen != self._gen:
            return
        self._resolving -= 1
        # going on would only make it worse; the fast speed, like the userscript, skips a "too fast" picture
        if err == "IMAGE_LIMIT" or (err == BLOCKED_MSG and not self.site.queue.fast):
            return self._pause((post, target), err)
        if err:
            log.warning("e-hentai picture not downloaded: %s", api.scrub(err))
            self._unresolved.append((post, target))
            self.failed.emit(post.page_url, str(target))
            return self._next()
        if self._no_originals and post.file_url == post.album.get("original"):
            post.file_url = post.album.get("shown") or post.file_url
        self._download(post, target)

    def _download(self, post: Post, target) -> None:
        ext = post.ext  # now from the resolved address: a resized copy is often another type (page_1.png -> .webp)
        if ext in _IMAGE_EXT and Path(target).suffix.lower() != f".{ext}":
            new = Path(target).with_suffix(f".{ext}")
            self.retargeted.emit(str(target), str(new))
            target = new
        q = self.site.queue
        # the album may come from a favorites file: the login cookie only ever goes to e-hentai's fullimg
        original = (q.logged_in and post.file_url == post.album.get("original")
                    and post.file_url.startswith(eh.EH + "/fullimg"))
        self._active[str(target)] = (post, target)
        self._posts[str(target)] = post
        refresh = (lambda cb, p=post: self._refresh(p, cb)) if q.fast else None
        item = (post.file_url, target, {b"Cookie": q._cookie} if original else None, refresh)
        self.downloader.add([item if refresh else item[:3] if original else item[:2]])

    def _on_limited(self, url: str, target: str) -> None:
        if target in self._active:
            self._pause(self._active.pop(target), "IMAGE_LIMIT")

    def _on_refused(self, url: str, target: str) -> None:
        if target not in self._active:
            return
        post, _ = self._active.pop(target)
        shown = post.album.get("shown")
        if not self._no_originals:
            self._no_originals = True
            log.warning("e-hentai refused the original of %s; downloading resized copies instead. "
                        "Is the e-hentai login in Settings right?", api.scrub(post.page_url))
            self.originals_refused.emit()
        if not (isinstance(shown, str) and eh.safe_image_url(shown)):
            self._posts.pop(target, None)
            self._unresolved.append((post, target))
            self.failed.emit(post.page_url, target)
            return self._next()
        post.file_url = shown
        self._download(post, target)

    def _on_item_done(self, url: str, target: str, ok: bool) -> None:
        if ok:
            self._posts.pop(target, None)
        if self._active.pop(target, None):
            self._next()
