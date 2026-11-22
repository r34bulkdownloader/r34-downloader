import json
import logging
import os
import re
from collections import deque
from urllib.parse import urlencode

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .models import Post, tag_kind

BASE = "https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1"
USER_AGENT = b"r34-downloader/0.1"
TIMEOUT_MS = 20000
TICK_MS = 500
RETRY_DELAYS_MS = (2000, 4000, 8000)

log = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"(api_key|user_id)=[^&\s\"']*")


class ApiError(Exception):
    pass


def scrub(text: str) -> str:
    """Remove API credentials, and show the home folder as ~ (it holds the user's login name), so logs, error
    messages and screenshots can be shared."""
    return home_short(_SECRET_RE.sub(r"\1=***", text))


def home_short(text: str) -> str:
    home = os.path.expanduser("~").rstrip("/")
    if len(home) <= 1:
        return text
    return re.sub(re.escape(home) + r"(?=/|\b|$)", "~", text)


def split_tags(text: str) -> list[str]:
    """Users type tags one per line, space- or comma-separated; rule34 tags use _ instead of spaces."""
    return [t for t in re.split(r"[\s,]+", text) if t]


def build_tags(include: str, exclude: str) -> str:
    parts = split_tags(include) + [t if t.startswith("-") else "-" + t for t in split_tags(exclude)]
    return " ".join(dict.fromkeys(parts))


def post_url(tags: str, pid: int, user_id: str, api_key: str) -> str:
    q = urlencode({"tags": tags, "limit": 100, "pid": pid, "user_id": user_id, "api_key": api_key,
                   "fields": "tag_info"})  # tag types and counts come with each post, no extra requests
    return f"{BASE}&{q}"


AUTOCOMPLETE = "https://api.rule34.xxx/autocomplete.php"


def request(url: str) -> QNetworkRequest:
    """Every request the app makes: its own User-Agent and a 20 s timeout."""
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"User-Agent", USER_AGENT)
    req.setTransferTimeout(TIMEOUT_MS)
    return req


def post_page_url(post_id: int) -> str:
    """The post's page on the site, for opening in a web browser."""
    return f"https://rule34.xxx/index.php?page=post&s=view&id={int(post_id)}"


def suggest_url(prefix: str) -> str:
    return f"{AUTOCOMPLETE}?{urlencode({'q': prefix})}"


def parse_suggestions(body: bytes) -> list[tuple[str, str]]:
    """[(label, tag)] from the site's autocomplete JSON, e.g. ("long_hair (3081032)", "long_hair")."""
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [
        (d.get("label") or d["value"], d["value"])
        for d in data
        if isinstance(d, dict) and isinstance(d.get("value"), str) and d["value"] and isinstance(d.get("label", ""), str)
    ]


def _auth_error(body: bytes) -> ApiError:
    msg = "The API rejected the request. Check your User ID and API key in Settings."
    text = scrub(body.decode("utf-8", "replace").strip().strip('"'))
    if text and len(text) <= 120 and not text.startswith(("<", "{", "[")):
        msg += f" ({text})"
    return ApiError(msg)


def _tag_info(raw) -> dict[str, tuple[str, int]]:
    """[{"tag": "blush", "type": "tag", "count": 4010685}, ...] -> {"blush": ("general", 4010685)}; bad entries skipped."""
    out = {}
    for t in raw if isinstance(raw, list) else []:
        try:
            out[str(t["tag"])] = (tag_kind(t.get("type")), int(t.get("count", 0)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _https(url) -> bool:
    return isinstance(url, str) and url.startswith("https://")


def parse_page(body: bytes) -> tuple[list[Post], int]:
    """Return (valid posts, raw entry count). The raw count decides whether more pages exist."""
    if not body.strip():
        return [], 0
    try:
        data = json.loads(body)
    except ValueError:
        raise _auth_error(body) from None
    if not isinstance(data, list):
        raise _auth_error(body)
    posts = []
    for d in data:
        try:
            if not _https(d["file_url"]):
                continue  # untrusted input: never let the API point us at file:// or other schemes
            posts.append(
                Post(
                    id=int(d["id"]),
                    hash=str(d["hash"]),
                    file_url=d["file_url"],
                    preview_url=d.get("preview_url") if _https(d.get("preview_url")) else "",
                    sample_url=d.get("sample_url") if _https(d.get("sample_url")) else "",
                    tags=str(d.get("tags", "")).split(),
                    rating=str(d.get("rating", "")),
                    score=int(d.get("score", 0)),
                    width=int(d.get("width", 0)),
                    height=int(d.get("height", 0)),
                    tag_info=_tag_info(d.get("tag_info")),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return posts, len(data)


class ApiClient(QObject):
    page_ready = Signal(int, list, int)
    failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._queue: deque = deque()
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def fetch(self, tags: str, pid: int, generation: int, user_id: str, api_key: str) -> None:
        self._enqueue((post_url(tags, pid, user_id, api_key), generation, 0))

    def _enqueue(self, job) -> None:
        self._queue.append(job)
        if not self._timer.isActive():
            self._tick()
            self._timer.start()

    def _tick(self) -> None:
        if not self._queue:
            self._timer.stop()
            return
        self._send(*self._queue.popleft())

    def _send(self, url: str, gen: int, attempt: int) -> None:
        reply = self._nam.get(request(url))
        reply.finished.connect(lambda: self._done(reply, url, gen, attempt))

    def _fail(self, gen: int, msg: str) -> None:
        log.warning("search failed: %s", msg)
        self.failed.emit(gen, msg)

    def _done(self, reply: QNetworkReply, url: str, gen: int, attempt: int) -> None:
        reply.deleteLater()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if status in (429, 503):
            log.info("server busy (HTTP %s), retry %d", status, attempt + 1)
            if attempt < len(RETRY_DELAYS_MS):
                QTimer.singleShot(
                    RETRY_DELAYS_MS[attempt], self, lambda: self._enqueue((url, gen, attempt + 1))
                )
            else:
                self._fail(gen, "The server is rate limiting requests. Try again later.")
            return
        if status in (401, 403):
            self._fail(gen, str(_auth_error(bytes(reply.readAll()))))
            return
        if reply.error() != QNetworkReply.NoError:
            self._fail(gen, scrub(reply.errorString()))
            return
        try:
            posts, raw = parse_page(bytes(reply.readAll()))
        except ApiError as e:
            self._fail(gen, scrub(str(e)))
            return
        self.page_ready.emit(gen, posts, raw)
