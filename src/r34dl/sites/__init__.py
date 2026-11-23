"""Sites the app can search. A search tab talks to its site only through this interface."""
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkRequest

from .. import api
from ..models import Post


class Site(QObject):
    """One site: how its tags are typed and searched, how pages are fetched and how posts get their file."""

    name = ""  # "rule34.xxx" / "e-hentai"; also the key for per-site settings, history and favorites
    label = ""  # shown in the picker
    page_jump = True  # can jump straight to page N
    tag_hint = ""  # HTML hint under the tag boxes
    include_placeholder = ""
    exclude_placeholder = ""

    page_ready = Signal(int, list, object)  # generation, posts, next cursor (None = no more)
    failed = Signal(int, str)  # generation, message
    total_found = Signal(int, object, bool)  # generation, result count of the whole search, approximate

    def split_tags(self, text: str) -> list[str]:
        return api.split_tags(text)

    def include_term(self, tag: str) -> str:
        """A clicked tag as it goes into the include box."""
        return tag

    def build_query(self, include: str, exclude: str, blacklist: list[str]) -> str:
        raise NotImplementedError

    def fetch(self, query: str, cursor, generation: int, cfg: dict) -> None:
        """Ask for one page; cursor None = the first page. Answers with page_ready or failed."""
        raise NotImplementedError

    def suggest_request(self, word: str) -> QNetworkRequest | None:
        return None  # None = this site has no tag suggestions

    def parse_suggestions(self, body: bytes) -> list[tuple[str, str]]:
        return []

    def post_page_url(self, post: Post) -> str:
        raise NotImplementedError

    def resolve(self, post: Post, for_download: bool, cb, alive=None) -> None:
        """Make sure post.file_url is known, then cb(post, error or None). alive(): False once the answer is
        no longer wanted (a site may then skip the request and answer CANCELLED)."""
        cb(post, None)  # default: file_url already known


def make_sites(parent) -> dict[str, Site]:
    from .ehclient import EhentaiSite
    from .rule34 import Rule34Site
    return {"rule34.xxx": Rule34Site(parent), "e-hentai": EhentaiSite(parent)}
