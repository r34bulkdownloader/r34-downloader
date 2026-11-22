import re
from pathlib import Path
from string import Formatter

from .models import Post

MAX_SEGMENT_BYTES = 255
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SIMPLE = {"id", "hash", "ext", "rating", "score", "width", "height"}
_KINDS = {"artist", "character", "copyright"}  # from the post's tag types; several are joined with "+"


def _clean(value: str) -> str:
    return _CONTROL.sub("", value).replace("/", "_").replace("\\", "_")


def _cut(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _segments(template: str, post: Post, search: str, tags_cap: int | None, index: str = "001") -> list[str]:
    out = ""
    for literal, field, spec, conv in Formatter().parse(template):
        out += literal
        if field is None:
            continue
        if conv is not None:
            raise ValueError(f"conversion not supported: {field}!{conv}")
        if field == "tags":
            tags = post.tags
            if spec:
                if not spec.isdigit():
                    raise ValueError(f"bad tag count: {spec!r}")
                tags = tags[: int(spec)]
            value = "_".join(_clean(t) for t in tags)
            if tags_cap is not None:
                value = _cut(value, tags_cap)
        elif spec:
            raise ValueError(f"unexpected format spec on {{{field}}}")
        elif field in _KINDS:
            value = "+".join(_clean(t) for t in post.tags_of(field)) or "unknown"
        elif field == "index":
            value = index  # position in the download batch, zero-padded (e.g. 007)
        elif field == "search":
            value = _clean(re.sub(r"\s+", "_", search))
        elif field in _SIMPLE:
            value = _clean(str(getattr(post, field)))
        else:
            raise ValueError(f"unknown token: {{{field}}}")
        out += value
    out = _CONTROL.sub("", out)
    return [s.strip(" .") or "_" for s in out.split("/")]


def render(template: str, post: Post, search: str, index: str = "001") -> str:
    return "/".join(_segments(template, post, search, None, index))


def clean_name(name: str) -> str:
    """A user-typed folder/ZIP name made safe for one path segment."""
    return _cut(_clean(name).strip(" ."), MAX_SEGMENT_BYTES - 16)


def index_text(i: int, total: int) -> str:
    """1-based position, zero-padded so files sort in order: 001 … 120."""
    return str(i + 1).zfill(max(3, len(str(total))))


def album_template(template: str) -> str:
    """For a folder/ZIP album: numbered in order, without the template's own subfolders."""
    return "{index}_" + template.rsplit("/", 1)[-1]


def target_path(save_dir: Path, template: str, post: Post, search: str, index: str = "001") -> Path:
    suffix = f".{post.ext}" if post.ext else ""
    cap = len("_".join(post.tags).encode("utf-8"))
    while True:
        segs = _segments(template, post, search, cap, index)
        segs[-1] += suffix
        overflow = max(len(s.encode("utf-8")) for s in segs) - MAX_SEGMENT_BYTES
        if overflow <= 0 or cap == 0:
            break
        cap = max(0, cap - overflow)
    # Last resort: drop characters from the left, keeping the tail (id/hash/ext).
    segs = [
        s if len(s.encode("utf-8")) <= MAX_SEGMENT_BYTES
        else s.encode("utf-8")[-MAX_SEGMENT_BYTES:].decode("utf-8", errors="ignore")
        for s in segs
    ]
    return save_dir.joinpath(*segs)


def unique_path(path: Path, expected_size: int | None) -> Path | None:
    if not path.exists():
        return path
    if expected_size is not None and path.stat().st_size == expected_size:
        return None
    n = 1
    while (candidate := path.with_name(f"{path.stem}_{n}{path.suffix}")).exists():
        n += 1
    return candidate
