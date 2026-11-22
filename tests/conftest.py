import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def pytest_sessionfinish(session, exitstatus):
    session.config._r34_exitstatus = int(exitstatus)


def pytest_unconfigure(config):
    # shortcut: PySide can crash while tearing down the many windows/fake replies tests leave behind during
    # interpreter shutdown (core dump *after* "N passed"; the real app quits cleanly). After pytest has printed
    # its summary, exit right away with its real status. Upgrade: per-test window cleanup if this ever hides
    # something real.
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(getattr(config, "_r34_exitstatus", 1))  # no status recorded = something went wrong

@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, tmp_path_factory, monkeypatch):
    # never read or write the real settings or download history during tests (settings in their own folder,
    # so tests that expect an empty tmp_path still get one)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))



@pytest.fixture(autouse=True)
def _no_real_searches(request, monkeypatch):
    # UI tests build many windows; a window left over from an earlier test can still fire a queued
    # "load more" timer later. Make sure no UI test can ever reach the real site (tests stub fetch further).
    if request.node.path.name in ("test_ui_model.py", "test_viewing.py", "test_sites.py", "test_gallery.py"):
        from r34dl import api
        # ApiClient.fetch, not Rule34Site.fetch: tests patch win.api.fetch to see what the site asked for
        monkeypatch.setattr(api.ApiClient, "fetch", lambda self, *a, **k: None)
        from r34dl.sites.ehclient import EhQueue
        monkeypatch.setattr(EhQueue, "_add", lambda self, *a, **k: None)  # e-hentai: nothing is ever sent
        # Windows built without the `win` fixture still need (fake) credentials, or they open Settings and wait.
        from r34dl import config
        config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})


@pytest.fixture(autouse=True)
def _no_screen_inhibit(monkeypatch):
    # full-screen tests must not ask the real desktop to keep the screen awake
    from r34dl import widgets
    monkeypatch.setattr(widgets.KeepAwake, "on", lambda self, why: None)
