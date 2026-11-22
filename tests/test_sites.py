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
