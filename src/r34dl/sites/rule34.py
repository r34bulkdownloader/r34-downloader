"""rule34.xxx: the JSON API, cursor = page number (pid)."""
from PySide6.QtNetwork import QNetworkRequest

from .. import api
from ..models import Post
from ..widgets import PAGE_SIZE
from . import Site


class Rule34Site(Site):
    name = "rule34.xxx"
    label = "rule34.xxx"
    page_jump = True
    tag_hint = (
        "One tag per line (spaces or commas work too). Inside a tag, use _ instead of a space: "
        "<b>long_hair</b>. Extras: <b>rating:questionable</b> or <b>rating:explicit</b>, <b>sort:score</b>, <b>score:&gt;=50</b>. "
        "The first tab's exclude list is remembered.")
    include_placeholder = "for example:\nlong_hair\nblue_eyes\nrating:questionable"
    exclude_placeholder = "for example:\nai_generated\n3d"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = api.ApiClient(self)
        self._pids: dict[int, int] = {}  # generation -> pid asked for (one request per search at a time)
        self.api.page_ready.connect(self._on_page)
        self.api.failed.connect(self._on_failed)
        self.api.counted.connect(lambda gen, n: self.total_found.emit(gen, n, False))

    def build_query(self, include: str, exclude: str, blacklist: list[str]) -> str:
        return api.build_tags(include, " ".join([exclude, *blacklist]))

    def fetch(self, query: str, cursor, generation: int, cfg: dict) -> None:
        pid = cursor or 0
        self._pids[generation] = pid
        self.api.fetch(query, pid, generation, cfg["user_id"], cfg["api_key"])
        if cursor is None:  # first page of a search: also ask how many there are in all
            self.api.count(query, generation, cfg["user_id"], cfg["api_key"])

    def _on_page(self, gen: int, posts: list, raw: int):
        pid = self._pids.pop(gen, 0)
        self.page_ready.emit(gen, posts, pid + 1 if raw >= PAGE_SIZE else None)  # a short page is the last

    def _on_failed(self, gen: int, msg: str):
        self._pids.pop(gen, None)
        self.failed.emit(gen, msg)

    def suggest_request(self, word: str) -> QNetworkRequest | None:
        return api.request(api.suggest_url(word))

    def parse_suggestions(self, body: bytes) -> list[tuple[str, str]]:
        return api.parse_suggestions(body)

    def post_page_url(self, post: Post) -> str:
        return api.post_page_url(post.id)
