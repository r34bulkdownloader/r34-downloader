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

    def to_dict(self) -> dict:
        """Everything needed to show and re-download the post later without searching again."""
        return {"site": "rule34.xxx", "id": self.id, "hash": self.hash, "file_url": self.file_url,
                "preview_url": self.preview_url, "sample_url": self.sample_url, "tags": self.tags,
                "rating": self.rating, "score": self.score, "width": self.width, "height": self.height,
                "tag_info": {t: [k, c] for t, (k, c) in self.tag_info.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        """Inverse of to_dict; raises KeyError/TypeError/ValueError on a broken entry. Like posts from the API,
        only https links are accepted (a hand-edited file can't point the app at file:// or other schemes)."""
        def https(v) -> str:
            return v if isinstance(v, str) and v.startswith("https://") else ""
        if not https(d["file_url"]):
            raise ValueError("file_url must be https")
        return cls(int(d["id"]), str(d["hash"]), d["file_url"], https(d.get("preview_url")),
                   https(d.get("sample_url")), [str(t) for t in d.get("tags", [])], str(d.get("rating", "")),
                   int(d.get("score", 0)), int(d.get("width", 0)), int(d.get("height", 0)),
                   {str(t): (str(v[0]), int(v[1])) for t, v in dict(d.get("tag_info", {})).items()})

    def tags_of(self, kind: str) -> list[str]:
        return sorted(t for t, (k, _) in self.tag_info.items() if k == kind)

    @property
    def ext(self) -> str:
        name = urlparse(self.file_url).path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext if re.fullmatch(r"[a-z0-9]{1,10}", ext) else "bin"
