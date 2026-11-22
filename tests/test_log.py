import logging
import sys

import pytest

from r34dl import log


@pytest.fixture
def applog():
    root = logging.getLogger("r34dl")
    old_hook = sys.excepthook
    log.lines.clear()
    log.setup()
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    sys.excepthook = old_hook
    log.lines.clear()


def test_lines_scrubbed_in_memory_and_file(applog):
    logging.getLogger("r34dl.api").warning("failed: https://x/?api_key=SECRET&user_id=UID9876543&pid=1")
    assert "SECRET" not in log.text() and "UID9876543" not in log.text() and "pid=1" in log.text()
    content = log.path().read_text()
    assert "pid=1" in content and "SECRET" not in content
    assert oct(log.path().stat().st_mode & 0o777) == "0o600"


def test_filter_cap_and_clear(applog):
    applog.info("just info")
    applog.warning("a problem")
    assert "just info" not in log.text(errors_only=True) and "a problem" in log.text(errors_only=True)
    for i in range(log.MAX_LINES + 5):
        applog.info("x %d", i)
    assert len(log.lines) == log.MAX_LINES
    log.clear()
    assert log.text() == "" and log.path().read_text() == ""


def test_uncaught_exception_is_logged(applog, monkeypatch):
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)
    try:
        raise ValueError("boom api_key=SECRET")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    t = log.text()
    assert "unexpected error" in t and "ValueError" in t and "SECRET" not in t


def test_rotated_log_stays_private_and_clear_removes_backup(applog):
    fh = next(h for h in applog.handlers if hasattr(h, "doRollover"))
    applog.warning("before rollover")
    fh.doRollover()
    applog.warning("after rollover")
    backup = log.path().with_name(log.path().name + ".1")
    assert backup.exists()
    assert oct(log.path().stat().st_mode & 0o777) == "0o600"
    assert oct(log.path().parent.stat().st_mode & 0o777) == "0o700"
    log.clear()
    assert not backup.exists()


def test_count_keeps_growing_when_ring_full(applog):
    start = log.count
    for i in range(log.MAX_LINES + 3):
        applog.info("x %d", i)
    assert len(log.lines) == log.MAX_LINES and log.count == start + log.MAX_LINES + 3
