import json
from pathlib import Path

import pytest

from r34dl import api
from r34dl.api import ApiError, build_tags, parse_page, post_url, scrub

FIX = Path(__file__).parent / "fixtures" / "posts.json"


def test_build_tags():
    assert build_tags("a  b\nc", "x -y") == "a b c -x -y"
    assert build_tags("a a", "-a") == "a -a"
    assert build_tags("a, b,c\n d", "x,\ny") == "a b c d -x -y"
    assert build_tags(" , ", "") == ""


def test_post_url():
    u = post_url("a -b", 2, "42", "KEY")
    assert u.startswith("https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&json=1")
    assert "limit=100" in u and "pid=2" in u and "tags=a+-b" in u


def test_parse_fixture():
    posts = parse_page(FIX.read_bytes())[0]
    assert len(posts) == 2
    p = posts[0]
    assert (p.id, p.score, p.width, p.height) == (101, 12, 800, 600)
    assert p.tags == ["tag_a", "tag_b"]
    assert posts[1].ext == "webm"
    assert posts[1].sample_url == ""


@pytest.mark.parametrize("body", [b"", b"[]", b"  \n"])
def test_parse_empty_variants(body):
    assert parse_page(body)[0] == []


@pytest.mark.parametrize(
    "body",
    [b'"Missing authentication. Go to api.rule34.xxx for more information"', b'{"a": 1}', b"<html>"],
)
def test_parse_auth_error(body):
    with pytest.raises(ApiError, match="Settings"):
        parse_page(body)


def test_parse_skips_malformed_post():
    data = json.loads(FIX.read_text())
    bad = {"id": 5, "hash": "x"}
    posts = parse_page(json.dumps([bad, *data]).encode())[0]
    assert [p.id for p in posts] == [101, 102]


def test_scrub():
    s = scrub("http://x/?a=1&api_key=SECRET&user_id=42")
    assert "SECRET" not in s and "42" not in s


def test_client_constructs(qapp):
    from r34dl.api import ApiClient

    assert ApiClient() is not None


def test_parse_page_reports_raw_count():
    from r34dl.api import parse_page

    data = json.loads(FIX.read_text())
    posts, raw = parse_page(json.dumps([{"id": 5, "hash": "x"}, *data]).encode())
    assert len(posts) == 2 and raw == 3


@pytest.mark.parametrize(
    "url,ext",
    [("http://x/a.JPG", "jpg"), ("http://x/a.j/pg", "bin"), ("http://x/a.p%20g", "bin"),
     ("http://x/a.waytoolongext1", "bin"), ("http://x/noext", "bin"), ("http://x/a.", "bin")],
)
def test_post_ext_restricted(url, ext):
    from r34dl.models import Post

    assert Post(1, "h", url, "", "", [], "s", 0, 1, 1).ext == ext


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_maps_to_settings_message(qapp, status):
    from r34dl.api import ApiClient
    from PySide6.QtNetwork import QNetworkRequest

    class R:
        def deleteLater(self): pass
        def attribute(self, a): return status
        def readAll(self): return b"Missing authentication"
        def error(self): return 204

    c = ApiClient()
    out = []
    c.failed.connect(lambda g, m: out.append(m))
    c._done(R(), "http://x", 1, 0)
    assert len(out) == 1 and "Settings" in out[0]


def test_parse_rejects_non_https_urls():
    data = json.loads(FIX.read_text())
    data[0]["file_url"] = "file:///home/me/.ssh/id_rsa"
    data[1]["preview_url"] = "file:///etc/passwd"
    posts, raw = parse_page(json.dumps(data).encode())
    assert [p.id for p in posts] == [102] and raw == 2
    assert posts[0].preview_url == ""


def test_rate_limit_retries_and_spacing(qapp, monkeypatch):
    """429 twice, then a page: 3 requests, each >= ~TICK_MS apart, one page_ready."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PySide6.QtCore import QEventLoop, QTimer

    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(time.monotonic())
            if len(hits) <= 2:
                self.send_error(429)
                return
            body = FIX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(api, "BASE", f"http://127.0.0.1:{srv.server_address[1]}/?x=1")
    monkeypatch.setattr(api, "RETRY_DELAYS_MS", (0, 0, 0))
    try:
        c = api.ApiClient()
        pages, fails = [], []
        c.page_ready.connect(lambda g, p, r: pages.append(len(p)))
        c.failed.connect(lambda g, m: fails.append(m))
        c.fetch("a", 0, 1, "u", "k")
        loop = QEventLoop()
        end = time.monotonic() + 10
        t = QTimer(interval=20)
        t.timeout.connect(lambda: (pages or fails or time.monotonic() > end) and loop.quit())
        t.start()
        loop.exec()
        t.stop()
    finally:
        srv.shutdown()
        srv.server_close()
    assert pages == [2] and fails == []
    assert len(hits) == 3
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    assert all(g >= api.TICK_MS / 1000 * 0.9 for g in gaps), gaps


def test_parse_suggestions():
    from r34dl.api import parse_suggestions, suggest_url

    body = b'[{"label":"long_hair (3081032)","value":"long_hair"},{"value":"x"},{"label":"bad"},5]'
    assert parse_suggestions(body) == [("long_hair (3081032)", "long_hair"), ("x", "x")]
    assert parse_suggestions(b"<html>") == [] and parse_suggestions(b'{"a":1}') == []
    assert suggest_url("a b&c").endswith("autocomplete.php?q=a+b%26c")


def test_post_url_asks_for_tag_info():
    assert "fields=tag_info" in post_url("a", 0, "1", "k")


def test_parse_tag_info():
    posts, _ = parse_page(FIX.read_bytes())
    assert posts[0].tag_info == {"tag_a": ("character", 59460), "tag_b": ("general", 4010685)}
    assert posts[0].tags_of("character") == ["tag_a"]
    assert posts[1].tag_info == {}  # no type info: still parses
    data = json.loads(FIX.read_text())
    data[0]["tag_info"] = [{"tag": "x", "type": "metadata", "count": "7"}, {"type": "artist"}, 5,
                           {"tag": "y", "type": "brand_new_kind", "count": 1}]
    p = parse_page(json.dumps(data).encode())[0][0]
    assert p.tag_info == {"x": ("meta", 7), "y": ("other", 1)}


def test_scrub_hides_the_home_folder(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    assert api.scrub("can't write /home/someone/Downloads/x.jpg") == "can't write ~/Downloads/x.jpg"
    assert api.scrub("/home/someone2/x") == "/home/someone2/x"  # only the real home, not a prefix of another
    assert api.home_short("/home/someone") == "~"


FAKE_KEY = "ab12" * 32


@pytest.mark.parametrize("text", [
    f"&api_key={FAKE_KEY}&user_id=7654321",
    f"&user_id=7654321&api_key={FAKE_KEY}",
    f"  https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&api_key={FAKE_KEY}&user_id=7654321\n",
])
def test_parse_credentials_reads_the_sites_line(text):
    assert api.parse_credentials(text) == ("7654321", FAKE_KEY)


def test_parse_credentials_partial_and_garbage():
    assert api.parse_credentials("user_id=42") == ("42", None)
    assert api.parse_credentials("hello") == (None, None)
