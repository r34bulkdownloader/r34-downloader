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
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
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
    monkeypatch.setattr(downloader, "RETRY_DELAYS", (0, 0, 0))


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
    assert spin_until(lambda: server.max_open >= 3)
    spin_until(lambda: False, 0.4)
    assert server.max_open == 3
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


def test_extra_headers_are_not_sent_on_after_a_redirect(qapp, server, tmp_path):
    d = Downloader()
    saved = []
    d.item_done.connect(lambda url, target, ok: saved.append((url, ok)))
    d.add([(server.url + "/redir", tmp_path / "a.jpg", {b"Cookie": b"secret=1"})])
    run_until_finished(d)
    assert server.cookies == [("/redir", "secret=1"), ("/ok", None)]
    assert (tmp_path / "a.jpg").stat().st_size == 100 and saved == [(server.url + "/redir", True)]
