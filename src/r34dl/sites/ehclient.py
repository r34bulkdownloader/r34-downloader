"""e-hentai.org over QtNetwork: a paced request queue (EhQueue) and the Site the search tabs talk to."""
import itertools
import logging
import re
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .. import api
from ..models import Post
from . import Site
from . import ehentai as eh

EH_GAP_MS = 1000
EH_BURST_PAUSE_MS = 5000
EH_BURST = 4
EH_WARN_PICTURES = 200  # a bigger download batch asks first
CANCELLED = "cancelled"  # error given to jobs dropped by clear()
WARNING_MSG = "This gallery is behind a content warning that the app couldn't skip."
_ORIGINS = ("https://e-hentai.org/", "https://api.e-hentai.org/")
_list_gens = itertools.count(-1, -1)  # list_all_pages' generations: search tabs use positive ones

log = logging.getLogger(__name__)


class EhQueue(QObject):
    """One e-hentai request at a time, EH_GAP_MS apart, EH_BURST_PAUSE_MS after every EH_BURST, with a backoff on
    429/503. cb(status or None, body, error) where error "" means success."""

    def __init__(self, parent=None, nam=None):
        super().__init__(parent)
        self._nam = nam or QNetworkAccessManager(self)
        self.gap_ms, self.burst_pause_ms, self.retry_delays_ms = EH_GAP_MS, EH_BURST_PAUSE_MS, api.RETRY_DELAYS_MS
        self._jobs: deque = deque()  # (url, body or None, cb, attempt)
        self._low: deque = deque()  # download jobs: sent only when nothing else waits (viewing comes first)
        self._busy = False
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

    def get(self, url: str, cb, low: bool = False) -> None:
        if low:
            self._low.append((url, None, cb, 0))
            return self._pump()
        self._add((url, None, cb, 0))

    def post(self, url: str, body: bytes, cb) -> None:
        self._add((url, body, cb, 0))

    def clear(self) -> None:
        """Drop pending jobs; each callback still fires once, with CANCELLED."""
        jobs, self._jobs, self._low = list(self._jobs) + list(self._low), deque(), deque()
        for _url, _body, cb, _attempt in jobs:
            cb(None, b"", CANCELLED)

    def _add(self, job, front=False) -> None:
        self._jobs.appendleft(job) if front else self._jobs.append(job)
        self._pump()

    def _pump(self) -> None:
        if self._busy or not (self._jobs or self._low):
            return
        wait_ms = (self._next_at - time.monotonic()) * 1000
        if wait_ms > 0:
            self._timer.start(int(wait_ms) + 1)
            return
        url, body, cb, attempt = job = (self._jobs or self._low).popleft()
        self._busy = True
        self._sent += 1
        gap = self.burst_pause_ms if self._sent % EH_BURST == 0 else self.gap_ms
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
        url, body, cb, attempt = job
        reply.deleteLater()
        self._busy = False
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        data = bytes(reply.readAll())
        if status in (429, 503) and attempt < len(self.retry_delays_ms):
            log.info("e-hentai busy (HTTP %s), retry %d", status, attempt + 1)
            self._next_at = max(self._next_at, time.monotonic() + self.retry_delays_ms[attempt] / 1000)
            self._add((url, body, cb, attempt + 1), front=True)
            return
        if status in (429, 503):
            err = "e-hentai is rate limiting requests. Try again later."
        elif not reply.url().toString().startswith(_ORIGINS):
            err = "e-hentai redirected to another site."
        elif reply.error() != QNetworkReply.NoError:
            err = api.scrub(reply.errorString())
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
    tag_hint = ("One tag per line or comma-separated; spaces are part of the tag. Namespaces work: "
                "<b>artist:name</b>, <b>female:long hair</b>, <b>language:english</b>.")
    include_placeholder = "for example:\nfemale:long hair\nlanguage:english"
    exclude_placeholder = "for example:\nmale:yaoi"

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
                return None if err == CANCELLED else self.failed.emit(generation, err)
            pairs, nxt = eh.parse_search(body.decode("utf-8", "replace"))
            self._gdata(pairs, [], generation, nxt)
        self.queue.get(eh.search_url(query, cursor), on_search)

    def _gdata(self, pairs, done: list, gen: int, nxt) -> None:
        """Ask gdata for 25 galleries at a time, then emit them all."""
        if not pairs:
            return self.page_ready.emit(gen, done, nxt)

        def on_gdata(status, body, err):
            if err:
                return None if err == CANCELLED else self.failed.emit(gen, err)  # cancelled = stale generation
            try:
                posts = eh.parse_gdata(body)
            except (ValueError, AttributeError, TypeError):
                return self.failed.emit(gen, "Could not read e-hentai's gallery data.")
            for p in posts:
                if not eh.safe_image_url(p.preview_url):
                    p.preview_url = ""
            self._gdata(pairs[25:], done + posts, gen, nxt)
        self.queue.post(eh.EH_API, eh.gdata_body(pairs[:25]), on_gdata)

    def fetch_gallery(self, gallery: Post, p: int | None, gen: int) -> None:
        def on_page(status, body, err):
            if err:
                return None if err == CANCELLED else self.failed.emit(gen, err)  # cancelled = stale generation
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

    def suggest_request(self, word: str):
        return None

    def post_page_url(self, post: Post) -> str:
        return post.page_url

    def resolve(self, post: Post, for_download: bool, cb) -> None:
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
        self.queue.get(post.page_url, on_page, low=for_download)

    def list_all_pages(self, gallery: Post, cb) -> None:
        """Every page post of the gallery, walking fetch_gallery until there is no next p; then
        cb(pages, error or None). On an error, pages holds what was listed before it."""
        gen, got = next(_list_gens), []

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

        def done(err):
            self.page_ready.disconnect(ready)
            self.failed.disconnect(bad)
            cb(got, err)
        self.page_ready.connect(ready)
        self.failed.connect(bad)
        self.fetch_gallery(gallery, None, gen)

    def _pick(self, post: Post, for_download: bool, cb) -> None:
        original, shown = post.album.get("original"), post.album.get("shown")
        url = original if for_download and self.queue.logged_in and original else shown
        if not isinstance(url, str) or not eh.safe_image_url(url):  # the album may come from a saved favorite
            return cb(post, "The picture's address was refused.")
        post.file_url = url
        cb(post, None)


class EhDownloadFeeder(QObject):
    """Hands e-hentai pages to the downloader one at a time: resolve (paced by the site queue), download, and
    only after that item_done the next. Originals get the login cookie; resized H@H copies never do."""

    paused = Signal(str)  # "IMAGE_LIMIT": stopped, the rest is kept until resume()
    failed = Signal(str, str)  # picture page, target: it couldn't be resolved (so ZIP jobs still complete)

    def __init__(self, site: EhentaiSite, downloader, parent=None):
        super().__init__(parent)
        self.site, self.downloader = site, downloader
        self._jobs: deque = deque()  # (post, target path)
        self._busy: str | bool = False  # True: resolving; a target text: downloading it
        self._paused = False
        self._gen = 0
        downloader.item_done.connect(self._on_item_done)

    @property
    def active(self) -> bool:
        return bool(self._jobs or self._busy)

    @property
    def waiting(self) -> int:
        return len(self._jobs) - (self._busy is True)

    def add(self, jobs) -> None:
        self._jobs.extend(jobs)
        self._next()

    def resume(self) -> None:
        self._paused = False
        self._next()

    def cancel(self) -> None:
        # shortcut: a resolve already in the site queue is still sent once and its answer ignored
        self._gen += 1
        self._jobs.clear()
        self._busy = self._paused = False

    def _next(self) -> None:
        if self._busy or self._paused or not self._jobs:
            return
        post, target = self._jobs[0]
        self._busy, gen = True, self._gen
        self.site.resolve(post, True, lambda p, err: self._resolved(p, target, err, gen))

    def _resolved(self, post: Post, target, err, gen: int) -> None:
        if gen != self._gen:
            return
        self._busy = False
        if err == CANCELLED:  # someone cleared the site queue: silent, ask again
            return self._next()
        if err == "IMAGE_LIMIT":
            self._paused = True
            return self.paused.emit(err)
        self._jobs.popleft()
        if err:
            log.warning("e-hentai picture not downloaded: %s", api.scrub(err))
            self.failed.emit(post.page_url, str(target))
            return self._next()
        q = self.site.queue
        original = q.logged_in and post.file_url == post.album.get("original")
        self._busy = str(target)
        self.downloader.add([(post.file_url, target, {b"Cookie": q._cookie})] if original else
                            [(post.file_url, target)])

    def _on_item_done(self, url: str, target: str, ok: bool) -> None:
        if self._busy and target == self._busy:
            self._busy = False
            self._next()
