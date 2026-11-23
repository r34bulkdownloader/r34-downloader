from pathlib import Path

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QMessageBox

from r34dl.models import Post

SPRITE = "https://ehgt.org/m/001/1-00.jpg"


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from r34dl import config
    from r34dl.ui import MainWindow
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    w = MainWindow()
    w.first._inflight = True
    w.got = []  # thumbnail requests
    monkeypatch.setattr(w.nam, "get", lambda req: w.got.append(FakeReply(req.url().toString())) or w.got[-1])
    return w


class FakeReply(QObject):
    finished = Signal()

    def __init__(self, url, data=b""):
        super().__init__()
        self.url, self.data = url, data

    def error(self):
        return QNetworkReply.NoError

    def readAll(self):
        return QByteArray(self.data)

    def errorString(self):
        return ""

    def abort(self):
        pass


def png(w, h, left=QColor(255, 0, 0), right=QColor(0, 0, 255)) -> bytes:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(left)
    for x in range(w // 2, w):
        for y in range(h):
            img.setPixelColor(x, y, right)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def gallery(gid=7, pages=40) -> Post:
    album = {"gid": gid, "token": "0123456789", "title": "A gallery", "pages": pages, "category": "Doujinshi"}
    return Post(gid * 100000, "", "", "https://ehgt.org/t/cover.jpg", "", ["female:long hair"], "doujinshi", 0, 0, 0,
                {"female:long hair": ("general", 0)}, site="e-hentai", kind="gallery",
                page_url=f"https://e-hentai.org/g/{gid}/0123456789/", album=album)


def pages(g: Post, start: int, n: int, url=SPRITE) -> list[Post]:
    return [Post(g.id + i, "", "", url, "", list(g.tags), g.rating, 0, 0, 0, site="e-hentai", kind="page",
                 page_url=f"https://e-hentai.org/s/abc/{g.album['gid']}-{i}",
                 album={**g.album, "page": i, "sprite": (100 * ((i - 1) % 2), 0, 100, 100)})
            for i in range(start, start + n)]


def eh_tab(win):
    tab = win.open_search_tab(site="e-hentai")
    tab.model.generation = 5
    return tab


def test_gallery_click_opens_gallery_tab(win, monkeypatch):
    from r34dl.gallery import GalleryTab
    site = win.sites["e-hentai"]
    asked = []
    monkeypatch.setattr(site, "fetch_gallery", lambda g, p, gen: asked.append((g, p)))
    tab = eh_tab(win)
    g = gallery()
    tab._on_site_page(5, [g], None)
    tab.view.resize(400, 400)
    tab.view.grab()  # draws the gallery cell ("40 p" badge, title strip) without errors
    tab._on_post_clicked(0, False)
    gt = win.tabs.currentWidget()
    assert isinstance(gt, GalleryTab) and gt.gallery is g
    assert asked == [(g, None)]
    assert gt.site_box.isHidden() and gt.include_edit.isHidden() and gt.exclude_edit.isHidden()


def test_gallery_tab_lists_pages(win, monkeypatch):
    site = win.sites["e-hentai"]
    g = gallery()
    monkeypatch.setattr(site, "fetch_gallery", lambda gal, p, gen: site.page_ready.emit(gen, pages(gal, 1, 40), None))
    gt = win.open_gallery(g, False)
    assert len(gt.model.posts) == 40
    assert gt.title() == g.label and win.tabs.tabText(win.tabs.indexOf(gt)) == g.label
    assert "Doujinshi" in gt.info_label.text() and "40" in gt.info_label.text()
    assert gt.tag_list.tags() == ["female:long hair"]


def test_crop_sprite():
    from r34dl.gallery import crop_sprite
    out = QImage.fromData(crop_sprite(png(200, 100), (100, 0, 100, 100)))
    assert (out.width(), out.height()) == (100, 100)
    assert out.pixelColor(50, 50) == QColor(0, 0, 255)  # the right half
    assert crop_sprite(b"not an image", (0, 0, 1, 1)) == b""


def test_sprite_fetched_once(win):
    tab = eh_tab(win)
    g = gallery()
    tab._on_site_page(5, pages(g, 1, 20), None)
    assert [r.url for r in win.got] == [SPRITE]
    reply = win.got[0]
    reply.data = png(200, 100)
    reply.finished.emit()
    assert len(tab.model.thumbs) == 20
    assert QImage.fromData(tab.model.thumbs[g.id + 2]).pixelColor(50, 50) == QColor(0, 0, 255)


def test_hover_preview_off_for_ehentai(win):
    win.cfg["hover_preview"] = True
    assert win.first.view.preview.enabled()
    assert not eh_tab(win).view.preview.enabled()


def test_gallery_paged_previous_next(win, monkeypatch):
    site = win.sites["e-hentai"]
    asked = []

    def fetch(gal, p, gen):
        asked.append(p)
        start = 1 + 20 * (p or 0)
        site.page_ready.emit(gen, pages(gal, start, 20), None if p == 1 else (p or 0) + 1)
    monkeypatch.setattr(site, "fetch_gallery", fetch)
    win.cfg["infinite_scroll"] = False
    gt = win.open_gallery(gallery(), False)
    assert gt.page_jump.isHidden() and gt.page_go.isHidden()
    assert gt.next_page_btn.isEnabled() and not gt.prev_page_btn.isEnabled()
    gt.next_page_btn.click()
    assert not gt.next_page_btn.isEnabled() and gt.prev_page_btn.isEnabled()
    assert gt.model.posts[0].album["page"] == 21
    gt.prev_page_btn.click()
    assert asked == [None, 1, None]
    assert gt.model.posts[0].album["page"] == 1


def test_ehentai_search_paged_uses_cursors(win, monkeypatch):
    site = win.sites["e-hentai"]
    asked = []
    monkeypatch.setattr(site, "fetch", lambda q, cursor, gen, cfg: (asked.append(cursor),
                        site.page_ready.emit(gen, [gallery(len(asked))], f"c{len(asked)}")))
    win.cfg["infinite_scroll"] = False
    tab = win.open_search_tab(site="e-hentai")
    tab.include_edit.setPlainText("x")
    tab.search()
    tab.next_page_btn.click()
    tab.next_page_btn.click()
    tab.prev_page_btn.click()
    assert asked == [None, "c1", "c2", "c1"]


def test_gallery_tab_release(win, monkeypatch):
    site = win.sites["e-hentai"]
    monkeypatch.setattr(site, "fetch_gallery", lambda *a: None)
    gt = win.open_gallery(gallery(), True)
    gen = gt.model.generation
    win._close_tab(win.tabs.indexOf(gt))
    site.page_ready.emit(gen, pages(gallery(), 1, 3), None)  # must not reach the closed tab
    assert gt.model.posts == []


def test_site_switch_with_ticks_asks(win, monkeypatch):
    tab = win.open_search_tab()
    tab.model.generation = 3
    tab._on_page_ready(3, [Post(1, "h", "https://x/1.jpg", "", "", [], "s", 0, 0, 0)])
    tab.model.toggle(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.No)
    tab.site_box.setCurrentIndex(tab.site_box.findData("e-hentai"))
    assert tab.site.name == "rule34.xxx" and tab.site_box.currentData() == "rule34.xxx" and tab.model.checked
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Yes)
    tab.site_box.setCurrentIndex(tab.site_box.findData("e-hentai"))
    assert tab.site.name == "e-hentai" and not tab.model.checked


def test_saved_search_skips_gallery_tab(win, monkeypatch):
    from r34dl import config
    monkeypatch.setattr(win.sites["e-hentai"], "fetch_gallery", lambda *a: None)
    config.site_cfg(win.cfg, "rule34.xxx")["saved_searches"]["s"] = {"include": "cat", "exclude": "dog"}
    gt = win.open_gallery(gallery(), False)
    searched = []
    monkeypatch.setattr(win.first, "search", lambda: searched.append(win.first.include_edit.toPlainText()))
    assert win.current_site_search() is win.first
    win.load_saved_search("s")
    assert searched == ["cat"] and win.tabs.currentWidget() is win.first
    assert gt.include_edit.toPlainText() == ""


def test_include_exclude_from_gallery_viewer(win, monkeypatch):
    monkeypatch.setattr(win.sites["e-hentai"], "fetch_gallery", lambda *a: None)
    gt = win.open_gallery(gallery(), False)
    win._tag_action(gt, "exclude", "male:yaoi")  # first tab is on rule34: a new e-hentai tab gets it
    new = win.tabs.currentWidget()
    assert new is not gt and new.site.name == "e-hentai" and new.exclude_edit.toPlainText().endswith("male:yaoi")
    win.first.site_box.setCurrentIndex(win.first.site_box.findData("e-hentai"))
    win._tag_action(gt, "include", "female:x")  # first tab is on e-hentai now: it gets it
    assert win.first.include_edit.toPlainText().endswith('female:"x$"')  # quoted for the search box
    assert gt.include_edit.toPlainText() == "" and gt.exclude_edit.toPlainText() == ""


def test_gallery_tab_ignores_blacklist_and_hide_downloaded(win, monkeypatch):
    from r34dl import config
    site = win.sites["e-hentai"]
    g = gallery()
    config.site_cfg(win.cfg, "e-hentai")["blacklist"] = "female:long hair"
    win.cfg["hide_downloaded"] = True
    win.downloaded["e-hentai"].update({g.id + 1, g.id + 2})
    monkeypatch.setattr(site, "fetch_gallery", lambda gal, p, gen: site.page_ready.emit(gen, pages(gal, 1, 10), None))
    gt = win.open_gallery(g, False)
    assert len(gt.model.posts) == 10 and gt._blacklisted == 0 and gt._hidden == 0
    tab = eh_tab(win)  # a search tab still filters
    tab._blacklist = {"female:long hair"}
    tab._on_site_page(5, pages(g, 1, 10), None)
    assert tab.model.posts == []


def test_gallery_menu_downloads_whole_gallery_as_zip(win, monkeypatch, tmp_path):
    import time
    import zipfile
    from PySide6.QtCore import QCoreApplication
    from r34dl import ui
    g = gallery(pages=3)
    win.cfg["save_dir"] = str(tmp_path)
    tab = eh_tab(win)
    menu = {a.text(): a for a in tab._make_post_menu(g).actions()}
    assert {"Download gallery", "Download gallery as folder…", "Download gallery as ZIP…"} <= set(menu)
    assert "Download" not in menu
    site = win.sites["e-hentai"]
    listed = []
    monkeypatch.setattr(site, "list_all_pages", lambda gal, cb: listed.append(win.statusBar().currentMessage())
                        or cb(pages(gal, 1, 3), None))

    def resolve(post, for_download, cb, alive=None):
        if post.album["page"] == 2:
            return cb(post, "Could not read the picture page.")
        post.file_url = f"https://h.hath.network/{post.id}.jpg"
        cb(post, None)
    monkeypatch.setattr(site, "resolve", resolve)
    added = []
    monkeypatch.setattr(win.downloader, "add", lambda items: added.extend(items))
    asked = []
    monkeypatch.setattr(ui.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: asked.append(k["text"]) or ("gal", True)))
    menu["Download gallery as ZIP…"].trigger()
    assert asked == ["A gallery"] and listed == ["Listing pages of A gallery…"]
    assert len(added) == 1  # one at a time
    for _ in range(2):
        url, target = added[-1][:2]
        Path(target).write_bytes(b"x")
        win.downloader.item_done.emit(url, str(target), True)
    assert len(added) == 2 and "1 failed" in win.progress_label.text()
    assert {g.id + 1, g.id + 3} <= win.downloaded["e-hentai"] and not win.feeder.active
    end = time.monotonic() + 10
    while not (tmp_path / "gal.zip").exists() or win._zip_threads and win._zip_threads[0].is_alive():
        QCoreApplication.processEvents()
        assert time.monotonic() < end
    QCoreApplication.processEvents()
    with zipfile.ZipFile(tmp_path / "gal.zip") as z:
        assert len(z.namelist()) == 2


def test_image_limit_pauses_and_resume_button_continues(win, monkeypatch, tmp_path):
    win.cfg["save_dir"] = str(tmp_path)
    site = win.sites["e-hentai"]
    answer = ["IMAGE_LIMIT"]
    monkeypatch.setattr(site, "resolve", lambda post, d, cb, alive=None: cb(post, answer.pop() if answer else None))
    added = []
    monkeypatch.setattr(win.downloader, "add", lambda items: added.extend(items))
    win.download_posts(pages(gallery(), 1, 2))
    assert not added and not win.resume_btn.isHidden() and "image limit" in win.statusBar().currentMessage()
    win.resume_btn.click()
    assert len(added) == 1 and win.resume_btn.isHidden()


def test_cancel_while_listing_a_gallery_queues_nothing(win, monkeypatch, tmp_path):
    from r34dl import ui
    win.cfg["save_dir"] = str(tmp_path)
    site = win.sites["e-hentai"]
    waiting, stopped = [], []

    def list_all_pages(gal, cb):
        waiting.append(cb)
        return lambda: stopped.append(1)
    monkeypatch.setattr(site, "list_all_pages", list_all_pages)
    monkeypatch.setattr(site, "resolve", lambda post, d, cb, alive=None: cb(post, None))
    added = []
    monkeypatch.setattr(win.downloader, "add", lambda items: added.extend(items))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("gal", True)))
    win.download_album([gallery(pages=3)], "", zipped=True)
    parts = win._zip_jobs[0].parts
    win.cancel_btn.click()
    assert stopped == [1] and not parts.exists() and not win._zip_jobs
    waiting[0](pages(gallery(), 1, 3), None)  # a listing that answers anyway
    assert not added and not win.feeder.active and not parts.exists()
    assert not list(tmp_path.glob(".r34-zip-parts-*")) and not list(tmp_path.glob("*.zip"))


def test_quitting_while_listing_a_gallery_asks_and_cleans_up(win, monkeypatch, tmp_path):
    from PySide6.QtGui import QCloseEvent
    from r34dl import ui
    win.cfg["save_dir"] = str(tmp_path)
    site = win.sites["e-hentai"]
    stopped = []
    monkeypatch.setattr(site, "list_all_pages", lambda gal, cb: lambda: stopped.append(1))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("gal", True)))
    win.download_album([gallery(pages=3)], "", zipped=True)
    parts = win._zip_jobs[0].parts
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    e = QCloseEvent()
    win.closeEvent(e)
    assert asked and not e.isAccepted() and parts.exists()  # No: keeps going
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    e = QCloseEvent()
    win.closeEvent(e)
    assert e.isAccepted() and stopped == [1] and not parts.exists()


def test_retry_failed_sends_ehentai_pictures_back_through_the_paced_lookups(win, monkeypatch, tmp_path):
    from r34dl.downloader import _Item
    win.cfg["save_dir"] = str(tmp_path)
    site = win.sites["e-hentai"]
    resolved, bad = [], ["Could not read the picture page."]  # only the very first lookup fails
    monkeypatch.setattr(site, "resolve", lambda post, d, cb, alive=None: resolved.append(post) or cb(
        post, bad.pop() if bad else None))
    sent = []
    monkeypatch.setattr(win.downloader, "_start", lambda it: sent.append(it))
    a, b = pages(gallery(), 1, 2)
    win.download_posts([a, b])
    # a: lookup failed; b: resolved, then its download failed for good
    assert resolved == [a, b] and win._eh_failed == 1 and len(sent) == 1
    item = sent[0]
    win.downloader._running.remove(item)
    win.downloader._failed.append(item)
    win.downloader.item_done.emit(item.url, str(item.target), False)
    b.album["shown"] = "https://h.hath.network/old.jpg"  # expires: must be looked up again
    sent.clear()
    resolved.clear()
    win.retry_btn.click()
    assert win._eh_failed == 0 and win.downloader._failed == []
    assert resolved == [a]  # one at a time, paced: b waits for a's download
    assert len(sent) == 1 and sent[0].url == a.file_url
    assert "shown" not in b.album


def test_favorite_in_the_wrong_sites_file_is_skipped(win):
    r34 = Post(5, "h", "https://x/a.jpg", "", "", [], "s", 0, 1, 1).to_dict()
    eh = pages(gallery(), 1, 1)[0].to_dict()
    win._load_favorites([r34, eh], "e-hentai")
    assert list(win.favorites["e-hentai"]) == [eh["id"]]
    win._load_favorites([r34, eh], "rule34.xxx")
    assert list(win.favorites["rule34.xxx"]) == [5]


def test_ehentai_search_next_after_a_failed_page_asks_for_it_again(win, monkeypatch):
    site = win.sites["e-hentai"]
    asked, fail = [], []

    def fetch(q, cursor, gen, cfg):
        asked.append(cursor)
        if fail:
            return site.failed.emit(gen, fail.pop())
        site.page_ready.emit(gen, [gallery(len(asked))], f"c{len(asked)}")
    monkeypatch.setattr(site, "fetch", fetch)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    win.cfg["infinite_scroll"] = False
    tab = win.open_search_tab(site="e-hentai")
    tab.include_edit.setPlainText("x")
    tab.search()
    fail.append("e-hentai is rate limiting requests. Try again later.")
    tab.next_page_btn.click()  # page 2 fails
    assert asked == [None, "c1"]
    tab.next_page_btn.click()  # asks for page 2 again (its cursor is known), not for an unknown page 3
    assert asked == [None, "c1", "c1"] and tab.model.posts


def test_new_tabs_start_on_the_site_used_last(win):
    other = win.open_search_tab()
    other._site_changed("e-hentai")
    assert win.first.site.name == "rule34.xxx"
    assert win.open_search_tab().site.name == "e-hentai"


def test_remembered_include_belongs_to_the_site_it_was_typed_for(qapp, monkeypatch):
    from r34dl import config
    from r34dl.ui import MainWindow
    cfg = {**config.DEFAULTS, "user_id": "1", "api_key": "k", "remember_include": True, "include": "long_hair",
           "site": "e-hentai"}
    config.site_cfg(cfg, "e-hentai")["include"] = "female:glasses"
    config.save(cfg)
    w = MainWindow()
    assert w.first.site.name == "e-hentai" and w.first.include_edit.toPlainText() == "female:glasses"
    w.first.include_edit.setPlainText("female:hat")
    w._store_tags()
    assert config.site_cfg(w.cfg, "e-hentai")["include"] == "female:hat" and w.cfg["include"] == "long_hair"
