import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


# Display order and names, like the site's sidebar. "other" catches any kind the site adds later.
TAG_KINDS = ("copyright", "character", "artist", "general", "meta", "other")
_SITE_KINDS = {"copyright": "copyright", "character": "character", "artist": "artist",
               "tag": "general", "general": "general", "metadata": "meta", "meta": "meta"}


def tag_kind(site_type) -> str:
    return _SITE_KINDS.get(str(site_type).lower(), "other")


@dataclass
class Post:
    id: int
    hash: str
    file_url: str
    preview_url: str
    sample_url: str
    tags: list[str]
    rating: str
    score: int
    width: int
    height: int
    # tag -> (kind, post count); kind is one of TAG_KINDS. Empty if the site sent no type info.
    tag_info: dict[str, tuple[str, int]] = field(default_factory=dict)
    site: str = "rule34.xxx"
    kind: str = "post"  # "post" | "gallery" | "page" (the last two are e-hentai only)
    page_url: str = ""  # the picture page, or the gallery page for a gallery
    album: dict = field(default_factory=dict)  # gid, token, title, page, pages, category
    file_name: str = ""  # name hint for ext while file_url is still empty

    @property
    def label(self) -> str:
        if self.kind == "gallery":
            return str(self.album.get("title", ""))[:40]
        if self.kind == "page":
            return f"{str(self.album.get('title', ''))[:30]} · p{self.album.get('page', '')}"
        return f"#{self.id}"

    def to_dict(self) -> dict:
        """Everything needed to show and re-download the post later without searching again."""
        d = {"site": self.site, "id": self.id, "hash": self.hash, "file_url": self.file_url,
             "preview_url": self.preview_url, "sample_url": self.sample_url, "tags": self.tags,
             "rating": self.rating, "score": self.score, "width": self.width, "height": self.height,
             "tag_info": {t: [k, c] for t, (k, c) in self.tag_info.items()}}
        extra = {"kind": self.kind, "page_url": self.page_url, "album": self.album, "file_name": self.file_name}
        d.update({k: v for k, v in extra.items() if v and not (k == "kind" and v == "post")})
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        """Inverse of to_dict; raises KeyError/TypeError/ValueError on a broken entry. Like posts from the API,
        only https links are accepted (a hand-edited file can't point the app at file:// or other schemes)."""
        def https(v) -> str:
            return v if isinstance(v, str) and v.startswith("https://") else ""
        site, kind = str(d.get("site", "rule34.xxx")), str(d.get("kind", "post"))
        page_url = str(d.get("page_url", ""))
        if kind == "post" and not https(d["file_url"]):
            raise ValueError("file_url must be https")
        if site == "e-hentai" and not page_url.startswith("https://e-hentai.org/"):
            raise ValueError("page_url must be an e-hentai.org link")
        return cls(int(d["id"]), str(d["hash"]), d["file_url"] if kind == "post" else https(d["file_url"]),
                   https(d.get("preview_url")),
                   https(d.get("sample_url")), [str(t) for t in d.get("tags", [])], str(d.get("rating", "")),
                   int(d.get("score", 0)), int(d.get("width", 0)), int(d.get("height", 0)),
                   {str(t): (str(v[0]), int(v[1])) for t, v in dict(d.get("tag_info", {})).items()},
                   site, kind, page_url, dict(d.get("album", {})), str(d.get("file_name", "")))

    def tags_of(self, kind: str) -> list[str]:
        return sorted(t for t, (k, _) in self.tag_info.items() if k == kind)

    @property
    def ext(self) -> str:
        name = urlparse(self.file_url).path.rsplit("/", 1)[-1]
        if "." not in name:
            name = self.file_name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext if re.fullmatch(r"[a-z0-9]{1,10}", ext) else "bin"
