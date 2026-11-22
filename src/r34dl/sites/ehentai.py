"""e-hentai.org: query building and parsers for search pages, gdata, gallery pages and picture pages.
Pure functions; the Qt client that fetches the pages lives elsewhere."""
import html as htmllib
import ipaddress
import json
import re
import socket
from urllib.parse import quote, urlparse

from PySide6.QtCore import QUrl

from ..models import Post

EH = "https://e-hentai.org"
EH_API = "https://api.e-hentai.org/api.php"

NAMESPACE_KINDS = {
    "artist": "artist", "group": "artist", "parody": "copyright", "character": "character",
    "female": "general", "male": "general", "mixed": "general", "other": "general",
    "language": "meta", "reclass": "meta", "cosplayer": "meta",
}


class GalleryWarning(Exception):
    """The gallery page is the content-warning page instead of the thumbnails."""


class ImageLimit(Exception):
    """The picture page shows the 509 "image limit reached" picture."""


def split_tags(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[\n,]", text) if t.strip()]


def _kind(tag: str) -> str:
    ns, sep, _ = tag.partition(":")
    return NAMESPACE_KINDS.get(ns, "general") if sep else "general"


def _term(tag: str, neg: bool) -> str:
    if tag.startswith("-"):
        tag, neg = tag[1:], True
    tag = tag.replace('"', "").strip()
    ns, sep, name = tag.partition(":")
    if not (name.strip() if sep else tag):
        return ""  # nothing left to search for ("-", "ns:", '""')
    term = f'{ns}:"{name.strip()}$"' if sep else f'"{tag}$"'
    return "-" + term if neg else term


def build_query(include: str, exclude: str, blacklist: list[str]) -> str:
    terms = [_term(t, False) for t in split_tags(include)]
    terms += [_term(t, True) for t in split_tags(exclude) + [b.strip() for b in blacklist if b.strip()]]
    return " ".join(t for t in terms if t)


def search_url(query: str, cursor: int | None) -> str:
    url = f"{EH}/?f_search={quote(query, safe='')}&nw=always"
    return url if cursor is None else f"{url}&next={cursor}"


def parse_search(html: str) -> tuple[list[tuple[int, str]], int | None]:
    pairs = dict.fromkeys((int(g), t) for g, t in re.findall(r'href="https://e-hentai\.org/g/(\d+)/([0-9a-f]{10})/"', html))
    m = re.search(r'id="[ud]next" href="[^"]*?[?&;]next=(\d+)', html)
    return list(pairs), int(m.group(1)) if m else None


def gdata_body(pairs) -> bytes:
    gidlist = [[int(g), str(t)] for g, t in list(pairs)[:25]]  # the API takes at most 25 per request
    return json.dumps({"method": "gdata", "gidlist": gidlist, "namespace": 1}).encode()


def parse_gdata(body: bytes) -> list[Post]:
    posts = []
    for e in json.loads(body).get("gmetadata", []):
        try:
            if "error" in e or not str(e["thumb"]).startswith("https://"):
                continue
            gid, token = int(e["gid"]), str(e["token"])
            if not re.fullmatch(r"[0-9a-f]{10}", token):
                continue
            tags = [str(t) for t in e.get("tags", [])]
            album = {"gid": gid, "token": token, "title": htmllib.unescape(str(e["title"])),
                     "pages": int(e["filecount"]), "category": str(e["category"])}
            posts.append(Post(gid * 100000, "", "", e["thumb"], "", tags, album["category"].lower(),
                              int(float(e["rating"]) * 10), 0, 0, {t: (_kind(t), 0) for t in tags},
                              site="e-hentai", kind="gallery", page_url=f"{EH}/g/{gid}/{token}/", album=album))
        except (KeyError, TypeError, ValueError):
            continue  # skip a malformed entry rather than lose the whole page
    return posts


def gallery_url(gid, token, p) -> str:
    # The live site answers ?nw=always on a gallery page with a redirect to the bare gallery URL (dropping ?p=)
    # and a `nw=1` cookie, so the content-warning skip is the client's `nw=1` cookie, not a URL parameter.
    return f"{EH}/g/{gid}/{token}/" + (f"?p={p}" if p else "")


_THUMB = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
_SPRITE = re.compile(r"width:(\d+)px;\s*height:(\d+)px;.*?url\(([^)]*)\)\s*(-?\d+)(?:px)?\s+(-?\d+)(?:px)?", re.S)


def parse_gallery(html: str, gallery: Post) -> tuple[list[Post], int | None]:
    start = html.find('id="gdt"')
    if start < 0:
        if "Content Warning" in html or "nw=session" in html:
            raise GalleryWarning("this gallery has a content warning")
        raise ValueError("no gallery thumbnails on the page")
    end = html.find('<table class="ptt"', start)
    pages = []
    gid = gallery.album["gid"]
    for href, inner in _THUMB.findall(html[start:end if end > 0 else None]):
        href = htmllib.unescape(href)
        title = re.search(r'title="Page (\d+): ([^"]*)"', inner)
        if not href.startswith(EH + "/s/") or not title:
            continue
        n = int(title.group(1))
        album = {**gallery.album, "page": n}
        sprite = _SPRITE.search(inner)
        if sprite:
            w, h, preview, x, y = sprite.groups()
            album["sprite"] = (-int(x), -int(y), int(w), int(h))
        else:
            src = re.search(r'src="([^"]*)"', inner)
            preview = src.group(1) if src else ""
        preview = htmllib.unescape(preview.strip("'\""))
        pages.append(Post(gid * 100000 + n, "", "", preview if preview.startswith("https://") else "", "",
                          list(gallery.tags), gallery.rating, 0, 0, 0, dict(gallery.tag_info), site="e-hentai",
                          kind="page", page_url=href, album=album, file_name=htmllib.unescape(title.group(2))))
    m = re.search(r'<a href="https://e-hentai\.org/g/\d+/[0-9a-f]+/\?p=(\d+)"[^>]*>&gt;</a>', html)
    return pages, int(m.group(1)) if m else None


def parse_picture(html: str) -> tuple[str, str | None]:
    img = re.search(r'<img\b[^>]*\bid="img"[^>]*>', html)
    src = re.search(r'\bsrc="([^"]*)"', img.group(0)) if img else None
    if not src:
        raise ValueError("no picture on the page")
    shown = htmllib.unescape(src.group(1))
    if shown.endswith("/509.gif"):
        raise ImageLimit("e-hentai image limit reached")
    # live pages link https://e-hentai.org/fullimg/<gid>/<page>/<key>/<name>; older ones used fullimg.php?...
    full = re.search(r'href="(https://e-hentai\.org/fullimg(?:\.php\?|/)[^"]*)"', html)
    return shown, htmllib.unescape(full.group(1)) if full else None


def _ip_ok(ip) -> bool:
    if ip.version == 6:  # judge an embedded IPv4 by the IPv4 rules
        n = int(ip)
        if ip.ipv4_mapped or ip.sixtofour:
            return (ip.ipv4_mapped or ip.sixtofour).is_global
        if n >> 32 == 0 or n >> 32 == 0x64FF9B << 64:  # ::/96 (IPv4-compatible), 64:ff9b::/96 (NAT64)
            return ipaddress.IPv4Address(n & 0xFFFFFFFF).is_global
    return ip.is_global


def safe_image_url(url: str) -> bool:
    try:
        raw = urlparse(url).netloc
    except ValueError:
        return False
    if not raw.isascii() or "%" in raw:
        return False  # Qt would normalise Unicode dots / %2e into a different host than we checked
    q = QUrl(url)
    host = q.host().rstrip(".").lower()  # the host Qt will actually connect to
    if not q.isValid() or q.scheme() not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    # numeric IPv4 forms (2130706433, 0x7f000001, 127.1, 0177...)
    if re.fullmatch(r"(0x[0-9a-f]*|\d+)(\.(0x[0-9a-f]*|\d+))*", host):
        try:
            return ipaddress.ip_address(socket.inet_aton(host)).is_global
        except OSError:
            return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a host name; shortcut: one that resolves to a private IP is out of scope for this pure check
    return _ip_ok(ip)
