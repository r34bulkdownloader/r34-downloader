from r34dl import api


def test_rule34_next_cursor(qapp, monkeypatch):
    from r34dl.sites.rule34 import Rule34Site
    site = Rule34Site()
    monkeypatch.setattr(site.api, "fetch", lambda *a: None)
    got = []
    site.page_ready.connect(lambda gen, posts, nxt: got.append((gen, nxt)))
    cfg = {"user_id": "1", "api_key": "k"}
    site.fetch("q", 3, 7, cfg)
    site.api.page_ready.emit(7, [], 100)
    site.fetch("q", 4, 8, cfg)
    site.api.page_ready.emit(8, [], 37)
    site.fetch("q", None, 9, cfg)  # None = first page (pid 0)
    site.api.page_ready.emit(9, [], 100)
    assert got == [(7, 4), (8, None), (9, 1)]


def test_rule34_query_matches_api():
    from r34dl.sites.rule34 import Rule34Site
    assert Rule34Site().build_query("a b", "c", ["d"]) == api.build_tags("a b", "c d")


def test_search_tab_site_switch(qapp, tmp_path, monkeypatch):
    from r34dl import config
    from r34dl.sites import Site
    from r34dl.ui import MainWindow
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    win = MainWindow()

    class Fake(Site):
        name, label, page_jump, tag_hint = "fake", "Fake site", False, "fake hint"

    monkeypatch.setitem(config.SITE_DEFAULTS, "fake", {**config.SITE_DEFAULTS["e-hentai"], "exclude": "x"})
    win.sites["fake"] = Fake(win)
    win.downloaded["fake"] = set()
    win.favorites["fake"] = {}
    tab = win.open_search_tab()
    assert tab.site.name == "rule34.xxx" and tab.site_box.count() == 3  # rule34, e-hentai, fake
    tab.site_box.setCurrentIndex(tab.site_box.findData("fake"))
    assert tab.site.name == "fake" and win.cfg["site"] == "fake"
    assert tab.model.downloaded is win.downloaded["fake"] and tab.model.favorites is win.favorites["fake"]
    assert tab.tags_hint.text() == "fake hint" and tab.exclude_edit.toPlainText() == "x"
    assert tab.page_jump.isHidden()  # this site cannot jump to page N


def test_rule34_total_count(qapp, monkeypatch):
    from r34dl.sites.rule34 import Rule34Site
    assert api.parse_count(b'<?xml version="1.0"?><posts count="12345" offset="0"></posts>') == 12345
    assert api.parse_count(b"not xml") is None
    assert "json" not in api.count_url("a b", "1", "k") and "limit=0" in api.count_url("a b", "1", "k")
    site = Rule34Site()
    monkeypatch.setattr(site.api, "fetch", lambda *a: None)
    asked, got = [], []
    monkeypatch.setattr(site.api, "count", lambda *a: asked.append(a))
    site.total_found.connect(lambda *a: got.append(a))
    cfg = {"user_id": "1", "api_key": "k"}
    site.fetch("q", None, 5, cfg)
    site.fetch("q", 2, 5, cfg)  # later pages don't ask again
    assert asked == [("q", 5, "1", "k")]
    site.api.counted.emit(5, 12345)
    assert got == [(5, 12345, False)]


def test_ehentai_total_on_first_page_only(qapp, monkeypatch):
    from r34dl.sites.ehclient import EhentaiSite
    site = EhentaiSite()
    html = b"<p>Found about 1,234 results. </p>"
    monkeypatch.setattr(site.queue, "get", lambda url, cb, **k: cb(200, html, ""))
    got, pages = [], []
    site.total_found.connect(lambda *a: got.append(a))
    site.page_ready.connect(lambda *a: pages.append(a))
    cfg = {"sites": {}}
    site.fetch("x", None, 3, cfg)
    site.fetch("x", 99, 3, cfg)
    assert got == [(3, 1234, True)] and len(pages) == 2


def _post(i):
    from r34dl.models import Post
    return Post(i, "h", f"https://x/{i}.jpg", "", "", ["a"], "s", 0, 1, 1)


def test_infinite_scroll_stops_at_cap_and_load_more_continues(qapp, tmp_path, monkeypatch):
    from r34dl import config, search
    from r34dl.ui import MainWindow
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    monkeypatch.setattr(search, "AUTO_PAGES", 3)
    win = MainWindow()
    tab = win.first
    asked = []
    monkeypatch.setattr(tab.site, "fetch", lambda q, cursor, gen, cfg: asked.append(cursor))
    tab.include_edit.setPlainText("a")
    tab.search()
    for n in range(6):  # every page comes back with a next page; tiny grid = "near the bottom" each time
        if len(asked) == n:
            break
        tab._on_site_page(tab.model.generation, [_post(n)], n + 1)
        qapp.processEvents()
    assert asked == [None, 1, 2] and tab._pages == 3  # stopped by itself after 3 pages
    assert tab.more_btn.isVisibleTo(tab)
    tab.more_btn.click()
    assert asked == [None, 1, 2, 3] and not tab.more_btn.isVisibleTo(tab)
    tab.site.total_found.emit(tab.model.generation, 4567, True)
    assert "about 4,567 results in all" in tab.count_label.text()


def _win(tmp_path, monkeypatch):
    from r34dl import config
    from r34dl.ui import MainWindow
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    return MainWindow()


def _titles(win):
    return [win.tabs.tabText(i) for i in range(win.tabs.count())]


def test_new_tabs_open_after_the_current_one(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget
    win = _win(tmp_path, monkeypatch)
    assert win.tabs.isMovable()
    for t in ("A", "B", "C"):
        win.add_tab(QWidget(), t)  # each opened in front: each goes after the one before
    assert _titles(win)[1:] == ["A", "B", "C"]
    win.tabs.setCurrentIndex(1)  # back on A
    win.add_tab(QWidget(), "A1")
    assert _titles(win)[1:] == ["A", "A1", "B", "C"]
    win.tabs.setCurrentIndex(2)  # on A1: background tabs from here line up after it, in opening order
    win.add_tab(QWidget(), "x", background=True)
    win.add_tab(QWidget(), "y", background=True)
    assert _titles(win)[1:] == ["A", "A1", "x", "y", "B", "C"] and win.tabs.currentIndex() == 2


def test_first_search_tab_stays_open_wherever_it_is_moved(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget
    win = _win(tmp_path, monkeypatch)
    other = QWidget()
    other.release = lambda: None
    win.add_tab(other, "other")
    win.tabs.tabBar().moveTab(0, 1)  # the first search tab is now second
    assert win.tabs.indexOf(win.first) == 1
    win._close_tab(1)
    assert win.tabs.indexOf(win.first) == 1  # not closed
    win._close_tab(0)
    assert win.tabs.indexOf(other) == -1 and win.tabs.count() == 1
