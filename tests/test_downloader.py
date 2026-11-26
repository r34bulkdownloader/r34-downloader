import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from r34dl import downloader
from r34dl.downloader import Downloader


class _State:
    def __init__(self):
        self.stop = threading.Event()
        self.open = 0
        self.max_open = 0
        self.lock = threading.Lock()
        self.fail_500 = True


@pytest.fixture
def server():
    st = _State()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            with st.lock:
                st.open += 1
                st.max_open = max(st.max_open, st.open)
                st.cookies = getattr(st, "cookies", []) + [(self.path, self.headers.get("Cookie"))]
            try:
                redirects = {"/redir": "/ok", "/to509": "/509.gif", "/toloop": "http://127.0.0.1:9/x.jpg",
                             "/tolimit": "/limit"}
                if self.path in redirects:
                    self.send_response(302)
                    self.send_header("Location", redirects[self.path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path in ("/html", "/509.gif", "/limit"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html" if self.path == "/html" else "image/gif")
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"x" * 5)
                elif self.path == "/ok" or (self.path == "/500" and not st.fail_500):
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"x" * 100)
                elif self.path == "/500":
                    self.send_error(500)
                elif self.path == "/404":
                    st.hits_404 = getattr(st, "hits_404", 0) + 1
                    self.send_error(404)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b"x" * 10)
                    self.wfile.flush()
                    st.stop.wait(30)
            except OSError:
                pass
            finally:
                with st.lock:
                    st.open -= 1

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    st.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield st
    st.stop.set()
    srv.shutdown()
    srv.server_close()
    t.join(5)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setitem(downloader.SPEEDS, "polite", (3, (0, 0, 0)))


def spin_until(cond, timeout=10):
    loop = QEventLoop()
    end = time.monotonic() + timeout
    t = QTimer()
    t.setInterval(20)

    def tick():
        if cond() or time.monotonic() > end:
            loop.quit()

    t.timeout.connect(tick)
    t.start()
    loop.exec()
    t.stop()
    return cond()


def run_until_finished(d, timeout=10):
    done = []
    d.finished.connect(lambda: done.append(1))
    assert spin_until(lambda: done, timeout), "timed out"


def track(d):
    last = []
    d.progress.connect(lambda *a: last.append(a))
    return last


def test_downloads_file(qapp, server, tmp_path):
    d = Downloader()
    saved = []
    d.item_done.connect(lambda url, target, ok: ok and saved.append(url))
    d.add([(server.url + "/ok", tmp_path / "sub" / "a.jpg")])
    run_until_finished(d)
    assert (tmp_path / "sub" / "a.jpg").stat().st_size == 100
    assert saved == [server.url + "/ok"]
    assert not list(tmp_path.rglob("*.part"))


def test_failed_download_leaves_no_file(qapp, server, tmp_path):
    d = Downloader()
    last = track(d)
    d.add([(server.url + "/500", tmp_path / "a.jpg")])
    run_until_finished(d)
    assert not list(tmp_path.iterdir())
    assert last[-1][3] == 1


def test_retry_failed_requeues(qapp, server, tmp_path):
    d = Downloader()
    last = track(d)
    d.add([(server.url + "/500", tmp_path / "a.jpg")])
    run_until_finished(d)
    server.fail_500 = False
    d.retry_failed()
    run_until_finished(d)
    assert (tmp_path / "a.jpg").stat().st_size == 100
    assert last[-1][3] == 0


def test_cancel_removes_part(qapp, server, tmp_path):
    d = Downloader()
    d.add([(server.url + "/slow", tmp_path / "a.jpg")])
    parts = lambda: list(tmp_path.glob("*.part"))  # noqa: E731
    assert spin_until(lambda: parts() and parts()[0].stat().st_size > 0)
    d.cancel()
    assert not parts()
    assert d.active is False


def test_concurrency_cap(qapp, server, tmp_path):
    d = Downloader()
    d.add([(server.url + "/slow", tmp_path / f"{i}.jpg") for i in range(10)])
    assert spin_until(lambda: server.max_open >= d.concurrency == 3)
    spin_until(lambda: False, 0.4)
    assert server.max_open == d.concurrency
    d.cancel()


def test_same_size_existing_is_skipped(qapp, server, tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"y" * 100)
    d = Downloader()
    d.add([(server.url + "/ok", tmp_path / "a.jpg")])
    run_until_finished(d)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_same_target_twice_is_safe(qapp, server, tmp_path):
    d = Downloader()
    t = tmp_path / "a.jpg"
    d.add([(server.url + "/ok", t), (server.url + "/ok", t)])
    run_until_finished(d)
    assert [p.name for p in tmp_path.iterdir()] == ["a.jpg"]
    assert t.stat().st_size == 100


def test_missing_file_is_not_retried(qapp, server, tmp_path):
    d = Downloader()
    last = track(d)
    d.add([(server.url + "/404", tmp_path / "a.jpg")])
    run_until_finished(d)
    assert server.hits_404 == 1 and last[-1][3] == 1


def test_downloader_extra_headers(qapp, server, tmp_path):
    d = Downloader()
    d.add([(server.url + "/ok", tmp_path / "a.jpg", {b"Cookie": b"nw=1"}), (server.url + "/ok", tmp_path / "b.jpg")])
    run_until_finished(d)
    assert sorted(server.cookies, key=str) == [("/ok", "nw=1"), ("/ok", None)]


@pytest.fixture
def local_ok(monkeypatch):
    monkeypatch.setattr(downloader, "safe_image_url", lambda url: True)  # the test server is on 127.0.0.1


def test_extra_headers_are_not_sent_on_after_a_redirect(qapp, server, tmp_path, local_ok):
    d = Downloader()
    saved = []
    d.item_done.connect(lambda url, target, ok: saved.append((url, ok)))
    d.add([(server.url + "/redir", tmp_path / "a.jpg", {b"Cookie": b"secret=1"})])
    run_until_finished(d)
    assert server.cookies == [("/redir", "secret=1"), ("/ok", None)]
    assert (tmp_path / "a.jpg").stat().st_size == 100 and saved == [(server.url + "/redir", True)]


def test_redirect_of_an_original_to_a_private_host_is_refused(qapp, server, tmp_path):
    d = Downloader()
    done = []
    d.item_done.connect(lambda url, target, ok: done.append(ok))
    d.add([(server.url + "/toloop", tmp_path / "a.jpg", {b"Cookie": b"secret=1"})])
    run_until_finished(d)
    assert done == [False] and not list(tmp_path.iterdir())
    assert [p for p, _ in server.cookies] == ["/toloop"]  # the refused address was never asked


def test_original_redirected_to_509_is_limited(qapp, server, tmp_path, local_ok):
    d = Downloader()
    done, limited, refused = [], [], []
    d.item_done.connect(lambda *a: done.append(a))
    d.limited.connect(lambda url, target: limited.append((url, target)))
    d.refused.connect(lambda url, target: refused.append(url))
    d.add([(server.url + "/to509", tmp_path / "a.jpg", {b"Cookie": b"c=1"}), (server.url + "/html", tmp_path / "b.htm")])
    run_until_finished(d)
    assert limited == [(server.url + "/to509", str(tmp_path / "a.jpg"))] and len(done) == 1  # b.htm: not an original
    assert refused == [] and sorted(p.name for p in tmp_path.iterdir()) == ["b.htm"] and d._failed == []
    assert "/509.gif" not in [p for p, _ in server.cookies]  # the 509 picture isn't even fetched


def test_original_answered_with_an_html_page_is_refused_not_limited(qapp, server, tmp_path, local_ok):
    # e.g. a wrong or expired login: fullimg answers with a page, not the image-limit picture
    d = Downloader()
    done, limited, refused = [], [], []
    d.item_done.connect(lambda *a: done.append(a))
    d.limited.connect(lambda url, target: limited.append(url))
    d.refused.connect(lambda url, target: refused.append((url, target)))
    d.add([(server.url + "/html", tmp_path / "a.jpg", {b"Cookie": b"c=1"})])
    run_until_finished(d)
    assert refused == [(server.url + "/html", str(tmp_path / "a.jpg"))] and limited == [] and done == []
    assert not list(tmp_path.iterdir()) and d._failed == []


def test_original_redirected_on_to_a_509_picture_is_limited(qapp, server, tmp_path, local_ok):
    d = Downloader()
    limited = []
    d.limited.connect(lambda url, target: limited.append(url))
    d.add([(server.url + "/tolimit", tmp_path / "a.jpg", {b"Cookie": b"c=1"})])
    run_until_finished(d)
    assert limited == [] and (tmp_path / "a.jpg").exists()  # a gif that isn't /509.gif is a picture


def test_fast_retry_downloads_from_the_fresh_address(qapp, server, tmp_path, local_ok, monkeypatch):
    monkeypatch.setitem(downloader.SPEEDS, "fast", (6, (0, 0, 0)))
    d = Downloader()
    d.set_speed("fast")
    asked = []
    d.add([(server.url + "/404", tmp_path / "a.jpg", None, lambda cb: asked.append(1) or cb(server.url + "/ok"))])
    run_until_finished(d)
    assert asked == [1] and server.hits_404 == 1 and (tmp_path / "a.jpg").read_bytes() == b"x" * 100


def test_fast_download_count_adapts_between_5_and_16(qapp, tmp_path):
    d = Downloader()
    d.set_speed("fast")
    it = downloader._Item("https://x/a.jpg", tmp_path / "a.jpg")
    for _ in range(20):
        it.started = time.monotonic()  # done at once
        d._adapt(it)
    assert d.concurrency == downloader.FAST_MAX == 16
    for _ in range(20):
        it.started = time.monotonic() - 2  # slow
        d._adapt(it)
    assert d.concurrency == downloader.FAST_MIN == 5
    d.set_speed("polite")
    it.started = time.monotonic()
    d._adapt(it)
    assert d.concurrency == 3  # Polite never adapts
