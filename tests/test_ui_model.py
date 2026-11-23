import pytest

from r34dl.models import Post
from r34dl.ui import MainWindow


def mk(i):
    return Post(i, "h", f"http://x/{i}.jpg", "", "", [], "s", 0, 1, 1)


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from r34dl import config
    config.save({**config.DEFAULTS, "user_id": "1", "api_key": "k"})
    w = MainWindow()
    w.first._inflight = True
    return w


def test_stale_generation_ignored(win):
    win.first.model.generation = 2
    win.first._on_page_ready(1, [mk(1)])
    assert win.first.model.posts == []


def test_short_page_sets_exhausted(win):
    win.first._on_page_ready(win.first.model.generation, [mk(1), mk(2)])
    assert len(win.first.model.posts) == 2
    assert win.first.model.exhausted


from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from r34dl import api, ui


@pytest.fixture
def fresh(win):
    win.first._inflight = False
    return win


def test_in_flight_guard(fresh, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: calls.append(a))
    fresh.first._fetch_next()
    fresh.first._fetch_next()
    assert len(calls) == 1


def test_failed_clears_inflight_keeps_pid(fresh, monkeypatch):
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: boxes.append(a))
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    fresh.first._fetch_next()
    fresh.first._on_failed(fresh.first.model.generation, "boom")
    assert not fresh.first._inflight and fresh.first._next_pid == 0 and len(boxes) == 1


def test_stale_failed_is_silent(fresh, monkeypatch):
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: boxes.append(a))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: boxes.append(a))
    fresh.first._inflight = True
    fresh.first._on_failed(fresh.first.model.generation - 1, "boom")
    assert boxes == [] and fresh.first._inflight


def test_stale_thumb_ignored(fresh):
    class R:
        def error(self): return ui.QNetworkReply.NoError
        def readAll(self):
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice
            pm = QPixmap(4, 4); pm.fill(Qt.red)
            ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); pm.save(b, "PNG")
            return ba
        def deleteLater(self): pass
    p = mk(1)
    fresh.first.model.append([p])
    fresh.first._thumb_active = 1
    fresh.first._thumb_done(R(), p, fresh.first.model.generation - 1)
    assert fresh.first.model.thumbs == {}
    fresh.first._thumb_active = 1
    fresh.first._thumb_done(R(), p, fresh.first.model.generation)
    assert 1 in fresh.first.model.thumbs


def test_bad_template_queues_nothing(fresh, monkeypatch):
    errs, added = [], []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: errs.append(a))
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.append(items))
    fresh.cfg["template"] = "{nope}"
    fresh.first.model.append([mk(1)])
    fresh.first.model.checked.add(1)
    fresh.first.download_selected()
    assert len(errs) == 1 and added == []


def test_click_checkbox_vs_viewer(fresh, monkeypatch):
    opened = []
    monkeypatch.setattr(fresh, "open_viewer", lambda p, bg, *a: opened.append((p.id, bg)))
    fresh.show()
    fresh.first.model.append([mk(1)])
    v = fresh.first.view
    cell = v.visualRect(v.model().index(0)).adjusted(4, 4, -4, -4)
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, ui.checkbox_rect(cell).center())
    assert 1 in fresh.first.model.checked and opened == []
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, cell.center())
    assert opened == [(1, False)]
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.ControlModifier, cell.center())
    assert opened[-1] == (1, True)


def test_settings_rejects_bad_template(win):
    from PySide6.QtWidgets import QDialogButtonBox
    dlg = ui.SettingsDialog(win.cfg, None)
    ok = dlg.buttons.button(QDialogButtonBox.Ok)
    assert ok.isEnabled()
    dlg.template_edit.setText("{nope}")
    assert not ok.isEnabled()
    dlg.template_edit.setText("{id}")
    assert ok.isEnabled()


def test_background_viewer_tab_and_release(win, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtNetwork import QNetworkReply

    class FakeReply(QObject):
        finished = Signal()

        def abort(self):
            pass

        def error(self):
            return QNetworkReply.NoError

    monkeypatch.setattr(win.nam, "get", lambda req: FakeReply())
    released = []
    monkeypatch.setattr(ui.ViewerTab, "release", lambda self: released.append(self.post.id))
    win.open_viewer(mk(7), background=True)
    assert win.tabs.count() == 2
    assert win.tabs.tabText(1) == "#7"
    assert win.tabs.currentIndex() == 0
    win._close_tab(1)
    assert released == [7]
    assert win.tabs.count() == 1
    win._close_tab(0)
    assert win.tabs.count() == 1


def _fake_reply_cls():
    from PySide6.QtCore import QObject, Signal

    class FakeReply(QObject):
        finished = Signal()

        def __init__(self):
            super().__init__()
            self.aborted = False
            self.deleted = False

        def abort(self):
            self.aborted = True
            self.finished.emit()

        def deleteLater(self):
            self.deleted = True

    return FakeReply


def test_real_release_aborts_and_deletes_reply(win, monkeypatch):
    reply = _fake_reply_cls()()
    monkeypatch.setattr(win.nam, "get", lambda req: reply)
    win.open_viewer(mk(5), background=False)
    assert win.tabs.currentIndex() == 1
    win._close_tab(1)
    assert reply.aborted and reply.deleted
    assert win.tabs.count() == 1


def test_viewer_checkbox_mirror_across_reset(win, monkeypatch):
    monkeypatch.setattr(win.nam, "get", lambda req: _fake_reply_cls()())
    win.first.model.append([mk(1), mk(2)])
    win.open_viewer(mk(1), background=True)
    tab = win.tabs.widget(1)
    assert tab.selected.isEnabled() and not tab.selected.isChecked()
    tab.selected.setChecked(True)
    assert 1 in win.first.model.checked
    win.first.model.reset()
    assert not tab.selected.isEnabled() and not tab.selected.isChecked()
    tab.selected.setChecked(True)
    assert win.first.model.checked == set()
    win.first.model.append([mk(1)])
    assert tab.selected.isEnabled()
    win._close_tab(1)


def test_tilde_save_dir_expanded_on_accept(win, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    dlg = ui.SettingsDialog(win.cfg, None)
    dlg.dir_edit.setText("~/Pictures/r34")
    dlg.accept()
    assert dlg.result_cfg()["save_dir"] == str(tmp_path / "Pictures" / "r34")


def test_download_expands_tilde_and_relative(fresh, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.append(items))
    fresh.first.model.append([mk(1)])
    fresh.first.model.checked.add(1)
    for raw in ("~/Pictures/r34", "rel/dir"):
        fresh.cfg["save_dir"] = raw
        fresh.first.download_selected()
    assert str(added[0][0][1]).startswith(str(tmp_path / "Pictures" / "r34"))
    assert str(added[1][0][1]).startswith(str(tmp_path / "rel" / "dir"))


def test_append_dedupes_by_id(fresh):
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(1), mk(2)], 100)
    fresh.first._inflight = True
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(2), mk(3)], 100)
    assert [p.id for p in fresh.first.model.posts] == [1, 2, 3]
    fresh.first.model.set_all("all")
    assert len(fresh.first.model.checked) == len(fresh.first.model.posts) == 3


def test_exhausted_uses_raw_count(fresh):
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(1)], 100)
    assert not fresh.first.model.exhausted
    fresh.first._inflight = True
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(2)], 40)
    assert fresh.first.model.exhausted


def test_failure_modal_once_per_page(fresh, monkeypatch):
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: boxes.append(a))
    for _ in range(3):
        fresh.first._inflight = True
        fresh.first._on_failed(fresh.first.model.generation, "boom")
    assert len(boxes) == 1 and not fresh.first._inflight


def test_decode_limited_downscales_huge_image(qapp):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication, QImage

    img = QImage(9000, 3000, QImage.Format_RGB32)
    img.fill(Qt.blue)
    ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); img.save(b, "PNG")
    pm = ui.ViewerTab._decode_limited(ba)
    sz = QGuiApplication.primaryScreen().size()
    limit = max(sz.width(), sz.height())
    assert pm is not None and max(pm.width(), pm.height()) <= limit
    assert pm.width() * 3000 == pm.height() * 9000 or abs(pm.width() / pm.height() - 3) < 0.01


def test_video_plays_only_while_tab_visible(fresh, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.QMediaPlayer, "play", lambda self: calls.append("play"))
    monkeypatch.setattr(ui.QMediaPlayer, "pause", lambda self: calls.append("pause"))
    monkeypatch.setattr(ui.QMediaPlayer, "setSource", lambda self, url: None)  # no real streaming in tests
    vid = Post(9, "h", "https://x/9.webm", "", "", [], "s", 0, 1, 1)
    fresh.open_viewer(vid, background=True)
    tab = fresh.tabs.widget(fresh.tabs.count() - 1)
    assert "play" not in calls  # background tab never starts streaming
    from PySide6.QtGui import QShowEvent, QHideEvent
    calls.clear()
    tab.showEvent(QShowEvent())
    tab.hideEvent(QHideEvent())
    assert calls == ["play", "pause"]
    fresh._close_tab(fresh.tabs.count() - 1)


def test_thumb_pixmap_prescaled_and_cached(fresh):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    big = QPixmap(600, 300); big.fill(Qt.red)
    ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); big.save(b, "PNG")
    fresh.first.model.append([mk(1)])
    fresh.first.model.thumb_ready(1, bytes(ba))
    pm = fresh.first.model.pixmap(1)
    side = fresh.first.model.thumb_size
    assert pm is not None and pm.width() <= side and pm.height() <= side
    assert fresh.first.model.pixmap(1).cacheKey() == pm.cacheKey()  # second call served from QPixmapCache


def test_viewer_download_button_queues_that_post(fresh, monkeypatch):
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    p = Post(7, "abc", "https://x/7.jpg", "", "", [], "s", 0, 1, 1)
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    fresh.open_viewer(p, background=True)
    tab = fresh.tabs.widget(fresh.tabs.count() - 1)
    tab.download_btn.click()
    assert [(u, t.name) for u, t in added] == [("https://x/7.jpg", "rule34_7_abc.jpg")]
    fresh._close_tab(fresh.tabs.count() - 1)


from PySide6.QtCore import QObject, Signal  # noqa: E402


class _NeverReply(QObject):
    finished = Signal()
    downloadProgress = Signal("qlonglong", "qlonglong")

    def abort(self): pass


def test_shift_click_selects_range(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "open_viewer", lambda p, bg, *a: None)
    fresh.show()
    fresh.first.model.append([mk(i) for i in range(1, 7)])
    v = fresh.first.view

    def cb(row):
        return ui.checkbox_rect(v.visualRect(v.model().index(row)).adjusted(4, 4, -4, -4)).center()

    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, cb(1))  # tick row 1 (id 2)
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.ShiftModifier, cb(4))  # range 1..4 on
    assert fresh.first.model.checked == {2, 3, 4, 5}
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, cb(3))  # untick id 4 -> anchor off
    QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.ShiftModifier, cb(1))  # range 1..3 off
    assert fresh.first.model.checked == {5}


def test_tag_edit_inserts_suggestion(fresh):
    ed = fresh.first.exclude_edit
    ed.setPlainText("cat\n-long_ha")
    ed.moveCursor(ui.QTextCursor.End)
    ed.show_suggestions([("long_hair (3081032)", "long_hair")])
    ed._insert("long_hair (3081032)")
    assert ed.toPlainText() == "cat\n-long_hair\n"


def test_remember_include_setting(fresh, tmp_path):
    from r34dl import config
    fresh.first.include_edit.setPlainText("cat")
    fresh._store_tags()
    assert fresh.cfg["include"] == ""  # off by default
    fresh.cfg["remember_include"] = True
    fresh._store_tags()
    config.save(fresh.cfg)
    again = ui.MainWindow()
    assert again.first.include_edit.toPlainText() == "cat"


def test_saved_search_save_load_delete(fresh, monkeypatch):
    searched = []
    monkeypatch.setattr(fresh.first, "search", lambda: searched.append(fresh.first.include_edit.toPlainText()))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("cats", True)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    fresh.first.include_edit.setPlainText("cat")
    fresh.first.exclude_edit.setPlainText("dog")
    fresh.save_current_search()
    from r34dl import config
    assert config.load()["saved_searches"] == {"cats": {"include": "cat", "exclude": "dog"}}
    fresh.first.include_edit.setPlainText("")
    fresh.load_saved_search("cats")
    assert searched == ["cat"] and fresh.first.exclude_edit.toPlainText() == "dog"
    fresh._fill_saved_menu()
    assert "cats" in [a.text() for a in fresh.saved_menu.actions()]
    fresh.delete_saved_search("cats")
    assert config.load()["saved_searches"] == {}


def test_download_marks_history_and_persists(fresh, monkeypatch):
    from r34dl import config
    monkeypatch.setattr(fresh.downloader, "add", lambda items: None)
    p = Post(42, "h", "https://x/42.jpg", "", "", [], "s", 0, 1, 1)
    fresh.first.model.append([p])
    fresh.download_posts([p])
    fresh.downloader.item_done.emit("https://x/42.jpg", "", True)
    assert 42 in fresh.first.model.downloaded
    assert config.load_history() == {42}
    fresh.downloader.item_done.emit("https://x/42.jpg", "", True)  # same file again: no duplicate line
    assert config.history_path().read_text() == "42\n"
    assert 42 in ui.MainWindow().first.model.downloaded  # remembered next start


def test_hide_downloaded_filters_and_keeps_paging(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: fetched.append(a[1]))
    fresh.cfg["hide_downloaded"] = True
    fresh.first.model.downloaded = {i for i in range(100)}
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(i) for i in range(100)], 100)  # full page, all hidden
    assert fresh.first.model.posts == [] and fetched == [1]  # asked for the next page by itself
    fresh.first._on_page_ready(fresh.first.model.generation, [mk(5), mk(500)], 2)
    assert [p.id for p in fresh.first.model.posts] == [500] and fresh.first.model.exhausted
    assert "101 already downloaded (hidden)" in fresh.first.count_label.text()


def test_wheel_scroll_glides_by_pixels(fresh):
    import time
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    fresh.resize(900, 500)
    fresh.show()
    fresh.first.model.append([mk(i) for i in range(1, 200)])
    QApplication.processEvents()
    v = fresh.first.view
    sb = v.verticalScrollBar()
    assert v.verticalScrollMode() == ui.QListView.ScrollPerPixel and sb.maximum() > 300
    sb.setValue(0)
    for _ in range(2):  # two quick notches add up
        ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, -120),
                         Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        v.wheelEvent(ev)
    end = time.monotonic() + 2
    while sb.value() != 2 * v.WHEEL_STEP_PX and time.monotonic() < end:
        QApplication.processEvents()
    assert sb.value() == 2 * v.WHEEL_STEP_PX


def test_seek_click_jumps_to_position(qapp):
    from PySide6.QtWidgets import QApplication
    s = ui.SeekSlider(Qt.Horizontal)
    s.setRange(0, 1000)
    s.resize(400, 24)
    s.show()
    QApplication.processEvents()
    seen = []
    s.actionTriggered.connect(lambda a: seen.append(s.sliderPosition()))
    QTest.mouseClick(s, Qt.LeftButton, Qt.NoModifier, QPoint(300, 12))
    assert seen and abs(seen[0] - 750) <= 30, seen
    assert abs(s.value() - 750) <= 30


def _video_tab(fresh, monkeypatch, loop=True):
    calls = []
    monkeypatch.setattr(ui.QMediaPlayer, "play", lambda self: calls.append("play"))
    monkeypatch.setattr(ui.QMediaPlayer, "pause", lambda self: calls.append("pause"))
    monkeypatch.setattr(ui.QMediaPlayer, "setSource", lambda self, url: None)  # no real streaming in tests
    fresh.cfg["loop_videos"] = loop
    fresh.open_viewer(Post(9, "h", "https://x/9.webm", "", "", [], "s", 0, 1, 1), background=True)
    return fresh.tabs.widget(fresh.tabs.count() - 1), calls


def test_click_or_space_toggles_video(fresh, monkeypatch):
    tab, calls = _video_tab(fresh, monkeypatch)
    calls.clear()
    tab.toggle_playback()  # player isn't playing in tests -> play
    assert calls == ["play"]
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5), Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    tab.eventFilter(tab.video, ev)
    assert calls == ["play", "play"]


def test_loop_setting_and_label_sync(fresh, monkeypatch):
    tab, _ = _video_tab(fresh, monkeypatch, loop=False)
    assert tab._player.loops() == 1
    tab._player.playbackStateChanged.emit(ui.QMediaPlayer.PlayingState)
    assert tab.play_btn.text() == "Pause"
    tab._player.playbackStateChanged.emit(ui.QMediaPlayer.StoppedState)  # video ended by itself
    assert tab.play_btn.text() == "Play"
    tab2, _ = _video_tab(fresh, monkeypatch, loop=True)
    assert tab2._player.loops() == ui.QMediaPlayer.Infinite


def test_gif_click_pauses(fresh):
    class FakeMovie:
        def __init__(self): self.paused = False
        def state(self): return ui.QMovie.NotRunning if self.paused else ui.QMovie.Running
        def setPaused(self, p): self.paused = p
    fresh.open_viewer(Post(8, "h", "https://x/8.gif", "", "", [], "s", 0, 1, 1), background=True)
    tab = fresh.tabs.widget(fresh.tabs.count() - 1)
    tab._movie = FakeMovie()
    tab.toggle_playback()
    assert tab._movie.paused
    tab.toggle_playback()
    assert not tab._movie.paused
    tab._movie = None


def test_log_dialog_shows_and_filters(qapp):
    from r34dl import log
    log.lines.clear()
    log.lines.append((logging.INFO, "info line"))
    log.lines.append((logging.WARNING, "warn line"))
    d = ui.LogDialog()
    assert "info line" in d.view.toPlainText()
    d.filter.setCurrentIndex(1)
    assert "info line" not in d.view.toPlainText() and "warn line" in d.view.toPlainText()
    log.lines.clear()


import logging  # noqa: E402


def test_open_in_browser_from_tab_and_grid(fresh, monkeypatch):
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString()) or True))
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    p = Post(77, "h", "https://x/77.jpg", "", "", [], "s", 0, 1, 1)
    fresh.first.model.append([p])
    fresh.open_viewer(p, background=True)
    fresh.tabs.widget(fresh.tabs.count() - 1).browser_btn.click()
    assert opened == ["https://rule34.xxx/index.php?page=post&s=view&id=77"]
    menu = fresh.first._make_post_menu(p)
    actions = {a.text(): a for a in menu.actions()}
    assert {"Open in tab", "Open in browser", "Download"} <= set(actions)
    actions["Open in browser"].trigger()
    assert len(opened) == 2


def test_thumbnail_size_slider_and_ctrl_wheel(fresh):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
    from PySide6.QtGui import QWheelEvent
    from r34dl import config
    big = QPixmap(600, 600); big.fill(Qt.red)
    ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); big.save(b, "PNG")
    fresh.first.model.append([mk(1)])
    fresh.first.model.thumb_ready(1, bytes(ba))
    fresh.size_slider.setValue(300)
    assert fresh.first.model.thumb_size == 300 and fresh.first.view.gridSize().width() == 300 + ui.CELL_PAD
    assert fresh.first.model.pixmap(1).width() == 300
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
                     Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
    fresh.first.view.wheelEvent(ev)  # Ctrl+wheel down = smaller
    assert fresh.size_slider.value() == 300 - ui.THUMB_STEP
    fresh.close()
    assert config.load()["thumb_size"] == 300 - ui.THUMB_STEP  # remembered


def test_ended_video_does_not_restart_on_tab_switch(fresh, monkeypatch):
    from PySide6.QtGui import QShowEvent
    tab, calls = _video_tab(fresh, monkeypatch, loop=False)
    tab._player.playbackStateChanged.emit(ui.QMediaPlayer.StoppedState)  # reached the end
    calls.clear()
    tab.showEvent(QShowEvent())
    assert "play" not in calls
    assert tab.download_btn.focusPolicy() == Qt.NoFocus  # Space stays play/pause


# ---- search tabs (Phase 2)

def test_tag_click_opens_search_tab_with_remembered_exclude(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, pid, gen, *a: fetched.append((tags, gen)))
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    fresh.first.exclude_edit.setPlainText("3d")
    p = Post(5, "h", "https://x/5.jpg", "", "", ["blue_eyes", "cat"], "s", 0, 1, 1)
    fresh.first.model.append([p])
    fresh.open_viewer(p, background=False)
    viewer = fresh.tabs.currentWidget()
    assert viewer.tag_list.tags() == ["blue_eyes", "cat"]  # no type info: plain list in post order
    viewer.tag_action.emit("search", "cat")
    tab = fresh.tabs.currentWidget()
    assert isinstance(tab, ui.SearchTab) and tab is not fresh.first
    assert tab.include_edit.toPlainText() == "cat" and tab.exclude_edit.toPlainText() == "3d"
    assert fresh.tabs.tabText(fresh.tabs.indexOf(tab)) == "cat"
    assert fetched == [("cat -3d", tab.model.generation)]
    before = fresh.tabs.currentIndex()
    viewer.tag_action.emit("search_bg", "blue_eyes")
    assert fresh.tabs.currentIndex() == before and len(fresh.search_tabs()) == 3


def test_search_tabs_are_independent(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    fresh.first.include_edit.setPlainText("a")
    fresh.first.search()
    other = fresh.open_search_tab("b")
    assert other.model.generation != fresh.first.model.generation
    other._on_page_ready(other.model.generation, [mk(1), mk(2)], 2)
    assert len(other.model.posts) == 2 and fresh.first.model.posts == []
    other.model.toggle(0)
    assert other.model.checked == {1} and fresh.first.model.checked == set()


def test_tag_menu_adds_to_origin_tab(fresh, monkeypatch):
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    origin = fresh.open_search_tab("x")
    p = Post(6, "h", "https://x/6.jpg", "", "", ["dog"], "s", 0, 1, 1)
    origin.model.append([p])
    fresh.open_viewer(p, False, origin)
    viewer = fresh.tabs.currentWidget()
    acts = {a.text(): a for a in viewer.make_tag_menu("dog").actions()}
    assert {"Search in new tab", "Add to include", "Add to exclude", "Copy"} <= set(acts)
    acts["Add to include"].trigger()
    acts["Add to include"].trigger()  # no duplicate
    acts["Add to exclude"].trigger()
    assert origin.include_edit.toPlainText() == "x\ndog"
    assert origin.exclude_edit.toPlainText().split("\n")[-1] == "dog"
    assert "dog" not in fresh.first.include_edit.toPlainText()


def test_close_search_tab_but_never_the_first(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    tab = fresh.open_search_tab("y")
    fresh._close_tab(0)
    assert fresh.tabs.widget(0) is fresh.first
    released = []
    monkeypatch.setattr(tab, "release", lambda: released.append(1))
    fresh._close_tab(fresh.tabs.indexOf(tab))
    assert released == [1] and tab not in fresh.search_tabs()


def test_size_slider_applies_to_every_search_tab(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    tab = fresh.open_search_tab("z")
    fresh.size_slider.setValue(220)
    assert fresh.first.model.thumb_size == tab.model.thumb_size == 220
    third = fresh.open_search_tab("w")
    assert third.model.thumb_size == 220  # new tabs start at the current size


def test_saved_search_loads_into_current_tab_and_only_first_is_remembered(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    fresh.cfg["saved_searches"]["s"] = {"include": "cat", "exclude": "dog"}
    tab = fresh.open_search_tab("other")
    fresh.load_saved_search("s")
    assert tab.include_edit.toPlainText() == "cat" and fresh.first.include_edit.toPlainText() == ""
    fresh.first.exclude_edit.setPlainText("keep")
    fresh._store_tags()
    assert fresh.cfg["exclude"] == "keep"  # from the first tab, not the current one


def test_viewer_download_uses_its_search_for_search_token(fresh, monkeypatch):
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    fresh.cfg["template"] = "{search}/{id}"
    tab = fresh.open_search_tab("cat_ears")
    p = Post(3, "h", "https://x/3.jpg", "", "", [], "s", 0, 1, 1)
    fresh.open_viewer(p, False, tab)
    fresh.tabs.currentWidget().download_btn.click()
    assert added[0][1].parent.name == "cat_ears"


def test_closing_origin_tab_detaches_its_viewers(fresh, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    origin = fresh.open_search_tab("x")
    p = Post(6, "h", "https://x/6.jpg", "", "", ["dog"], "s", 0, 1, 1)
    origin.model.append([p])
    fresh.open_viewer(p, False, origin)
    viewer = fresh.tabs.currentWidget()
    assert viewer.selected.isEnabled()
    fresh._close_tab(fresh.tabs.indexOf(origin))
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)  # the closed tab is really deleted now
    assert not viewer.selected.isEnabled()
    viewer.selected.setChecked(True)  # must not touch the deleted model
    viewer.tag_action.emit("include", "dog")  # falls back to the first tab
    assert fresh.first.include_edit.toPlainText().endswith("dog")


def test_background_search_failure_does_not_pop_up(fresh, monkeypatch):
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append(a)))
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    bg = fresh.open_search_tab("q", background=True)
    bg._on_failed(bg.model.generation, "boom")
    assert boxes == [] and fresh.tabs.tabText(fresh.tabs.indexOf(bg)).startswith("⚠")
    bg.release()
    bg._on_page_ready(bg.model.generation, [mk(1)], 1)  # closed tab ignores late results
    assert bg.model.posts == []


def test_tag_list_grouped_like_the_site(qapp):
    tl = ui.TagList()
    info = {"blush": ("general", 4010685), "nami_(one_piece)": ("character", 59460), "one_piece": ("copyright", 165590),
            "ai_generated": ("meta", 3662895), "animevixens": ("artist", 915), "1boy": ("general", 3325529)}
    p = Post(1, "h", "https://x/1.jpg", "", "", list(info) + ["unknown_tag"], "s", 0, 1, 1, tag_info=info)
    tl.set_post(p)
    rows = [tl.item(i).text().strip() for i in range(tl.count())]
    assert rows == ["Copyright", "one_piece  165,590", "Character", "nami_(one_piece)  59,460", "Artist",
                    "animevixens  915", "General", "1boy  3,325,529", "blush  4,010,685", "Meta",
                    "ai_generated  3,662,895", "Other", "unknown_tag"]
    assert tl.tags() == ["one_piece", "nami_(one_piece)", "animevixens", "1boy", "blush", "ai_generated", "unknown_tag"]
    assert not tl.item(0).flags() & Qt.ItemIsSelectable and tl.item(0).data(Qt.UserRole) is None  # heading
    assert tl.item(1).foreground().color() == ui.TAG_COLORS["copyright"]


def test_blacklist_sent_and_filtered_but_include_wins(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, *a: fetched.append(tags))
    fresh.cfg["blacklist"] = "scat, 3d\n" + " ".join(f"b{i}" for i in range(20))
    fresh.first.include_edit.setPlainText("3d cat")
    fresh.first.exclude_edit.setPlainText("dog")
    fresh.first.search()
    sent = fetched[-1].split()
    assert sent[:3] == ["3d", "cat", "-dog"] and "-scat" in sent and "-3d" not in sent  # included tag allowed
    assert len([t for t in sent if t.startswith("-b")]) == ui.BLACKLIST_SERVER_MAX - 1
    p_ok = Post(1, "h", "https://x/1.jpg", "", "", ["cat"], "s", 0, 1, 1)
    p_bad = Post(2, "h", "https://x/2.jpg", "", "", ["cat", "b19"], "s", 0, 1, 1)  # beyond the server cap
    fresh.first._on_page_ready(fresh.first.model.generation, [p_ok, p_bad], 2)
    assert [p.id for p in fresh.first.model.posts] == [1]
    assert "1 hidden by blacklist" in fresh.first.count_label.text()


def test_settings_saves_blacklist(win):
    from r34dl import config
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    dlg.blacklist_edit.setPlainText("scat\nai_generated")
    dlg.accept()
    assert config.load()["blacklist"] == "scat\nai_generated"


def test_previous_next_steps_through_search(fresh, monkeypatch):
    gets = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: gets.append(req.url().toString()) or _NeverReply())
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, pid, gen, *a: fetched.append(pid))
    st = fresh.first
    st.include_edit.setPlainText("a")
    st.search()
    st._on_page_ready(st.model.generation, [mk(1), mk(2), mk(3)], 100)  # full page: more exist
    fresh.open_viewer(st.model.posts[0], False, st)
    v = fresh.tabs.currentWidget()
    assert not v.prev_btn.isEnabled() and v.next_btn.isEnabled()
    v.step(1)
    assert v.post.id == 2 and fresh.tabs.tabText(fresh.tabs.indexOf(v)) == "#2" and gets[-1].endswith("/2.jpg")
    st.model.toggle(1)
    assert v.selected.isChecked()  # mirrors the post now shown
    v.step(1); v.step(1)  # past the end: asks the search for the next page
    assert v.post.id == 3 and fetched == [0, 1] and v.next_btn.text() == "Loading…"
    st._on_page_ready(st.model.generation, [mk(4)], 1)
    assert v.post.id == 4 and not v.next_btn.isEnabled()  # last page reached
    v.step(-1)
    assert v.post.id == 3


def test_next_on_video_reuses_one_player(fresh, monkeypatch):
    monkeypatch.setattr(ui.QMediaPlayer, "setSource", lambda self, url: None)
    monkeypatch.setattr(ui.QMediaPlayer, "play", lambda self: None)
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    st = fresh.first
    vids = [Post(i, "h", f"https://x/{i}.webm", "", "", [], "s", 0, 1, 1) for i in (1, 2)]
    st.model.append(vids + [mk(3)])
    fresh.open_viewer(vids[0], False, st)
    v = fresh.tabs.currentWidget()
    player = v._player
    v.step(1)
    assert v._player is player and v.controls.isVisibleTo(v)
    v.step(1)  # an image after a video: video controls hidden
    assert not v.controls.isVisibleTo(v) and v.media.isVisibleTo(v)


def test_page_mode_keeps_ticks_across_pages(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, pid, *a: fetched.append(pid))
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    fresh.cfg["infinite_scroll"] = False
    st = fresh.first
    st.include_edit.setPlainText("a")
    st.search()
    assert st.page_bar.isVisibleTo(st) and fetched == [0]
    st._on_page_ready(st.model.generation, [mk(1), mk(2)], 100)
    st.model.toggle(0)
    st.next_page_btn.click()
    assert fetched == [0, 1] and st.model.posts == [] and st.model.checked == {1}  # tick kept
    st._on_page_ready(st.model.generation, [mk(3)], 1)  # short page = last page
    assert [p.id for p in st.model.posts] == [3] and not st.next_page_btn.isEnabled()
    assert st.count_label.text().startswith("Page 2 · 1 posts · 1 selected")
    st.model.toggle(0)
    st.download_selected()
    assert [u for u, _ in added] == ["http://x/1.jpg", "http://x/3.jpg"]  # both pages, in search order
    st.prev_page_btn.click()
    assert fetched[-1] == 0
    st.page_jump.setValue(5)


def test_infinite_mode_page_indicator(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    st = fresh.first
    st.include_edit.setPlainText("a")
    st.search()
    assert not st.page_bar.isVisibleTo(st)
    st._on_page_ready(st.model.generation, [mk(i) for i in range(1, 101)], 100)
    st._on_page_ready(st.model.generation, [mk(i) for i in range(101, 151)], 50)
    assert "150 loaded · Page 1 of 2 loaded" in st.count_label.text()


def test_failed_page_reenables_page_buttons(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    fresh.cfg["infinite_scroll"] = False
    st = fresh.first
    st.include_edit.setPlainText("a")
    st.search()
    st._on_page_ready(st.model.generation, [mk(1)], 100)
    st.go_page(1)
    assert not st.next_page_btn.isEnabled()  # in flight
    st._on_failed(st.model.generation, "boom")
    assert st.prev_page_btn.isEnabled() and st.next_page_btn.isEnabled() and "loading" not in st.count_label.text()


def test_viewer_next_keeps_going_past_hidden_pages_and_stops_on_failure(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, pid, *a: fetched.append(pid))
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    st = fresh.first
    fresh.cfg["blacklist"] = "bad"
    st.include_edit.setPlainText("a")
    st.search()
    st._on_page_ready(st.model.generation, [mk(1)], 100)
    fresh.open_viewer(st.model.posts[0], False, st)
    v = fresh.tabs.currentWidget()
    v.step(1)  # end of loaded results -> asks for page 1
    bad = Post(2, "h", "https://x/2.jpg", "", "", ["bad"], "s", 0, 1, 1)
    st._on_page_ready(st.model.generation, [bad], 100)  # whole page blacklisted
    assert fetched == [0, 1, 2] and v.next_btn.text() == "Loading…"  # kept going by itself
    st._on_failed(st.model.generation, "boom")
    assert v.next_btn.text() == "Next ▶" and v.post.id == 1  # gave up cleanly


def test_blacklist_case_and_dash_insensitive(fresh, monkeypatch):
    fetched = []
    monkeypatch.setattr(fresh.api, "fetch", lambda tags, *a: fetched.append(tags))
    fresh.cfg["blacklist"] = "Scat -Gore"
    st = fresh.first
    st.include_edit.setPlainText("cat")
    st.exclude_edit.setPlainText("gore")
    st.search()
    assert fetched[-1].split() == ["cat", "-gore", "-scat"]  # gore not sent twice
    st._on_page_ready(st.model.generation, [Post(1, "h", "https://x/1.jpg", "", "", ["SCAT"], "s", 0, 1, 1)], 1)
    assert st.model.posts == []


def test_selected_posts_in_search_order(qapp):
    m = ui.ResultsModel()
    m.append([mk(50)], page=4)
    m.append([mk(10)], page=1)
    m.checked = {50, 10}
    assert [p.id for p in m.selected_posts()] == [10, 50]


def _wheel(view, x, y, notches):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    view.wheelEvent(QWheelEvent(QPointF(x, y), QPointF(x, y), QPoint(), QPoint(0, int(120 * notches)),
                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))


def test_zoom_follows_pointer_with_limits_and_reset(qapp):
    from PySide6.QtWidgets import QApplication
    z = ui.ZoomView()
    z.resize(400, 300)
    z.show()
    QApplication.processEvents()
    pm = QPixmap(1000, 500); pm.fill(Qt.red)
    detail = []
    z.needs_detail.connect(lambda: detail.append(1))
    z.set_image(pm, 4000)  # real image is 4x bigger than this copy
    fit = z.scale_now()
    assert abs(fit - z.fit_scale()) < 1e-9 and z.zoom_percent() == round(100 * fit / 4)
    _wheel(z, 50, 50, -3)  # can't zoom out past "whole picture"
    assert abs(z.scale_now() - fit) < 1e-9
    vp = z.viewport()
    p = QPoint(300, 120)
    before = z.mapToScene(vp.mapFrom(z, p))
    _wheel(z, vp.mapFrom(z, p).x(), vp.mapFrom(z, p).y(), 3)  # picture now overflows both ways
    after = z.mapToScene(vp.mapFrom(z, p))
    assert abs(z.scale_now() - fit * 1.25 ** 3) < 1e-6
    assert abs(before.x() - after.x()) < 2 and abs(before.y() - after.y()) < 2  # point under pointer stays
    for _ in range(40):
        _wheel(z, 100, 100, 1)
    assert abs(z.scale_now() - ui.ZoomView.MAX_ZOOM * 4) < 1e-6 and z.zoom_percent() == 400  # upper limit
    assert detail == [1]  # sharper copy requested once
    z.reset_zoom()
    assert abs(z.scale_now() - fit) < 1e-9


def test_image_tab_uses_zoom_view(fresh, monkeypatch):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    class Reply(QObject):
        finished = Signal()
        def error(self): return ui.QNetworkReply.NoError
        def errorString(self): return ""
        def readAll(self):
            pm = QPixmap(64, 32); pm.fill(Qt.blue)
            ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); pm.save(b, "PNG")
            return ba
        def abort(self): pass

    replies = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: replies.append(Reply()) or replies[-1])
    p = Post(9, "h", "https://x/9.png", "", "", [], "s", 0, 64, 32)
    fresh.first.model.append([p])
    fresh.open_viewer(p, False, fresh.first)
    v = fresh.tabs.currentWidget()
    replies[-1].finished.emit()
    _wait_until(lambda: v.zoom_view.isVisibleTo(v))  # decoded on a background thread
    assert v.zoom_view.isVisibleTo(v) and v.zoom_bar.isVisibleTo(v) and not v.media.isVisibleTo(v)
    v._clear_media()
    assert not v.zoom_view.isVisibleTo(v) and not v.zoom_bar.isVisibleTo(v)


def _artist_post(i, artist="neko"):
    return Post(i, f"h{i}", f"https://x/{i}.jpg", "", "", [artist, "cat"], "s", 0, 1, 1,
                tag_info={artist: ("artist", 1), "cat": ("general", 1)})


def test_download_as_folder_numbers_files_in_order(fresh, monkeypatch, tmp_path):
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    asked = []
    monkeypatch.setattr(ui.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: asked.append(k["text"]) or ("My/Album", True)))
    fresh.cfg["save_dir"] = str(tmp_path)
    fresh.cfg["template"] = "{artist}/rule34_{id}"
    posts = [_artist_post(i) for i in (5, 3, 9)]
    fresh.download_album(posts, "cat", zipped=False)
    assert asked == ["neko"]  # shared artist suggested
    assert [t.relative_to(tmp_path).as_posix() for _, t in added] == [
        "My_Album/001_rule34_5.jpg", "My_Album/002_rule34_3.jpg", "My_Album/003_rule34_9.jpg"]


def test_download_as_zip_packs_what_arrived(fresh, monkeypatch, tmp_path):
    import zipfile
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("set", True)))
    fresh.cfg["save_dir"] = str(tmp_path)
    posts = [_artist_post(i) for i in (1, 2, 3)]
    fresh.download_album(posts, "", zipped=True)
    parts = added[0][1].parent
    assert parts.name.startswith(".r34-zip-parts-") and parts.is_dir()
    fresh.downloader.item_done.emit(added[0][0], "/elsewhere/normal_download.jpg", True)  # same post, other batch
    for (url, target), body in zip(added[:2], (b"one", b"two")):
        target.write_bytes(body)
        fresh.downloader.item_done.emit(url, str(target), True)
    assert not (tmp_path / "set.zip").exists()  # still waiting for the third
    fresh.downloader.item_done.emit(added[2][0], str(added[2][1]), False)
    _wait_for_zips(fresh)
    with zipfile.ZipFile(tmp_path / "set.zip") as z:
        assert z.namelist() == ["001_rule34_1_h1.jpg", "002_rule34_2_h2.jpg"]
        assert z.read("002_rule34_2_h2.jpg") == b"two"
    assert not parts.exists() and "1 failed" in fresh.statusBar().currentMessage()


def test_cancel_discards_unfinished_zip(fresh, monkeypatch, tmp_path):
    added = []
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("set", True)))
    fresh.cfg["save_dir"] = str(tmp_path)
    fresh.download_album([_artist_post(1), _artist_post(2)], "", zipped=True)
    parts = added[0][1].parent
    fresh.cancel_btn.click()
    assert not parts.exists() and fresh._zip_jobs == [] and not list(tmp_path.glob("*.zip"))


def test_open_together_and_step_through_selection(fresh, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    gets = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get",
                        lambda self, req: gets.append(req.url().toString()) or _NeverReply())
    st = fresh.first
    img = Post(1, "h", "https://x/1.jpg", "https://x/p1.jpg", "https://x/s1.jpg", [], "s", 0, 10, 10)
    vid = Post(2, "h", "https://x/2.webm", "https://x/p2.jpg", "", [], "s", 0, 10, 10)
    gif = Post(3, "h", "https://x/3.gif", "https://x/p3.jpg", "", [], "s", 0, 10, 10)
    st.model.append([img, vid, gif, mk(4)])
    st.model.checked = {1, 2, 3}
    st._update_counts()
    assert st.together_btn.isEnabled()
    st.together_btn.click()
    tab = fresh.tabs.currentWidget()
    assert isinstance(tab, ui.TogetherTab) and fresh.tabs.tabText(fresh.tabs.indexOf(tab)) == "Together (3)"
    assert gets == ["https://x/s1.jpg", "https://x/p2.jpg", "https://x/3.gif"]  # sample / thumbnail / gif
    tab.per_row.setValue(3)
    assert [tab.grid.getItemPosition(tab.grid.indexOf(tab.cells[i]))[:2] for i in (1, 2, 3)] == [(0, 0), (0, 1), (0, 2)]
    tab.cells[2].clicked.emit()
    v = fresh.tabs.currentWidget()
    assert isinstance(v, ui.ViewerTab) and v.post.id == 2 and not v.selected.isVisibleTo(v)
    v.step(1)
    assert v.post.id == 3 and not v.next_btn.isEnabled()  # only the selected posts
    fresh._close_tab(fresh.tabs.indexOf(tab))
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert v.model is None and not v.prev_btn.isEnabled()  # let go of the closed tab's posts



def test_zip_last_item_failing_leaves_nothing_to_retry(qapp, tmp_path, monkeypatch):
    """Real downloader: the failed item must be forgotten, or "Retry failed" would recreate the temp folder."""
    from r34dl import downloader as dl
    monkeypatch.setattr(dl, "RETRY_DELAYS", ())
    win = ui.MainWindow()
    win.cfg["save_dir"] = str(tmp_path)
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("set", True)))
    monkeypatch.setattr(win.downloader, "_start", lambda it: None)  # no network; we finish items by hand
    win.download_album([mk(1)], "", zipped=True)
    item = win.downloader._running[0]
    item.error = "boom"
    win.downloader._error(item)  # gives up -> job finishes with nothing -> folder removed
    assert win.downloader._failed == [] and not list(tmp_path.iterdir())


def test_zoom_stays_in_limits_after_resize(qapp):
    from PySide6.QtWidgets import QApplication
    z = ui.ZoomView()
    z.resize(400, 300)
    z.show()
    QApplication.processEvents()
    pm = QPixmap(1000, 500); pm.fill(Qt.red)
    z.set_image(pm, 1000)
    for _ in range(3):
        _wheel(z, 100, 100, 1)
    z.resize(1600, 1200)  # much bigger window: the zoomed scale is now below "whole picture"
    QApplication.processEvents()
    lo, hi = z.limits()
    assert lo - 1e-9 <= z.scale_now() <= hi + 1e-9


def test_decode_limits_by_side_and_pixels(qapp):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage
    img = QImage(4000, 2000, QImage.Format_RGB32); img.fill(Qt.blue)
    ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); img.save(b, "PNG")
    assert ui.ViewerTab._decode_limited(ba, max_side=1600).size().toTuple() == (1600, 800)
    pm = ui.ViewerTab._decode_limited(ba, max_pixels=2_000_000)
    assert pm.width() * pm.height() <= 2_000_000 and abs(pm.width() / pm.height() - 2) < 0.01
    assert ui.ViewerTab._image_size(ba).toTuple() == (4000, 2000)


def _fav_post(i, tags=("cat",), rating="explicit"):
    return Post(i, f"h{i}", f"https://x/{i}.jpg", f"https://x/p{i}.jpg", "", list(tags), rating, 0, 1, 1)


def test_favorite_from_post_tab_marks_saves_and_lists(fresh, monkeypatch):
    from r34dl import config
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    st = fresh.first
    p = _fav_post(1)
    st.model.append([p])
    fresh.open_viewer(p, False, st)
    v = fresh.tabs.currentWidget()
    assert v.fav_btn.text() == "♡ Favorite"
    v.fav_btn.click()
    assert v.fav_btn.text() == "♥ Favorited" and 1 in st.model.favorites
    assert [d["id"] for d in config.load_favorites()] == [1] and config.load_favorites()[0]["site"] == "rule34.xxx"
    menu = {a.text() for a in st._make_post_menu(p).actions()}
    assert "Remove from favorites" in menu
    fresh.toggle_favorite(_fav_post(2, ("dog",)))  # newest first
    fresh.open_favorites()
    fav = fresh.tabs.currentWidget()
    assert isinstance(fav, ui.FavoritesTab) and [q.id for q in fav.model.posts] == [2, 1]
    assert fav.count_label.text().startswith("2 favorites")
    fresh.open_favorites()
    assert len([w for w in map(fresh.tabs.widget, range(fresh.tabs.count())) if isinstance(w, ui.FavoritesTab)]) == 1
    assert {d["id"] for d in config.load_favorites()} == {1, 2}
    assert 2 in ui.MainWindow().favorites["rule34.xxx"]  # remembered next start


def test_favorites_filter_locally_with_blacklist_and_ticks(fresh, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: calls.append(a))
    for p in (_fav_post(1, ("cat", "solo")), _fav_post(2, ("cat", "gore")), _fav_post(3, ("dog",), "questionable")):
        fresh.toggle_favorite(p)
    fresh.cfg["blacklist"] = "gore"
    fresh.open_favorites()
    fav = fresh.tabs.currentWidget()
    assert {p.id for p in fav.model.posts} == {1, 3} and "1 hidden by blacklist" in fav.count_label.text()
    fav.model.checked = {1, 3}
    fav.include_edit.setPlainText("cat")
    fav.search()
    assert [p.id for p in fav.model.posts] == [1] and fav.model.checked == {1}
    assert fav.count_label.text().startswith("1 of 3 favorites")
    fav.include_edit.setPlainText("rating:questionable")
    fav.search()
    assert [p.id for p in fav.model.posts] == [3]
    fresh.toggle_favorite(fresh.favorites["rule34.xxx"][3])  # unfavorite: stays (without ♥) until the next Filter
    assert [p.id for p in fav.model.posts] == [3] and 3 not in fav.model.favorites
    fav.search()
    assert fav.model.posts == [] and calls == []  # never asked the site


def test_follow_tag_and_searches_menu(fresh, monkeypatch):
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    p = _fav_post(1, ("artist_x",))
    fresh.first.model.append([p])
    fresh.open_viewer(p, False, fresh.first)
    v = fresh.tabs.currentWidget()
    acts = {a.text(): a for a in v.make_tag_menu("artist_x").actions()}
    acts["Follow tag"].trigger()
    assert fresh.cfg["followed"] == ["artist_x"]
    assert "Unfollow tag" in {a.text() for a in v.make_tag_menu("artist_x").actions()}
    assert fresh.saved_btn.text() == "★ Searches"
    fresh._fill_saved_menu()
    items = {a.text(): a for a in fresh.saved_menu.actions()}
    assert {"Followed tags", "artist_x", "Follow a tag…", "Saved searches", "Save current search…"} <= set(items)
    items["artist_x"].trigger()
    tab = fresh.tabs.currentWidget()
    assert isinstance(tab, ui.SearchTab) and tab.include_edit.toPlainText() == "artist_x"
    fresh.unfollow_tag("artist_x")
    from r34dl import config
    assert config.load()["followed"] == []


def test_unfavorite_from_favorites_keeps_post_tab_place(fresh, monkeypatch):
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    for i in (1, 2, 3):
        fresh.toggle_favorite(_fav_post(i))
    fresh.open_favorites()
    fav = fresh.tabs.currentWidget()
    gen = fav.model.generation
    fresh.open_viewer(fav.model.posts[1], False, fav)
    v = fresh.tabs.currentWidget()
    v.fav_btn.click()  # unfavorite the post being viewed
    assert v.fav_btn.text() == "♡ Favorite" and fav.model.generation == gen  # no reload
    v.step(1)
    assert v.post.id == fav.model.posts[2].id  # Next still works
    fresh.toggle_favorite(_fav_post(4))  # a new favorite appears at the top, in place
    assert fav.model.posts[0].id == 4 and fav.model.generation == gen


def test_favorites_rating_and_dash_filters(fresh):
    fresh.toggle_favorite(_fav_post(1, ("cat",), "explicit"))
    fresh.toggle_favorite(_fav_post(2, ("cat", "dog"), "questionable"))
    fresh.cfg["blacklist"] = "rating:explicit"
    fresh.open_favorites()
    fav = fresh.tabs.currentWidget()
    assert [p.id for p in fav.model.posts] == [2]  # rating: in the blacklist works locally too
    fresh.cfg["blacklist"] = ""
    fav.include_edit.setPlainText("cat -dog")
    fav.search()
    assert [p.id for p in fav.model.posts] == [1]


def test_saved_search_never_loads_into_favorites(fresh, monkeypatch):
    monkeypatch.setattr(fresh.api, "fetch", lambda *a: None)
    fresh.cfg["saved_searches"]["s"] = {"include": "cat", "exclude": ""}
    fresh.open_favorites()
    fav = fresh.tabs.currentWidget()
    fresh.load_saved_search("s")
    assert fav.include_edit.toPlainText() == "" and fresh.first.include_edit.toPlainText() == "cat"
    assert fresh.tabs.currentWidget() is fresh.first


def test_follow_tag_case_and_dash(fresh):
    fresh.follow_tag("-Foo")
    fresh.follow_tag("foo")
    assert fresh.cfg["followed"] == ["Foo"] and fresh.is_followed("FOO")
    fresh.unfollow_tag("foo")
    assert fresh.cfg["followed"] == []


def test_themes_apply_and_restore(win):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication
    from r34dl import theme
    app = QApplication.instance()
    theme.apply("system")
    system_style = app.style().name()
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    dlg.theme_box.setCurrentIndex(dlg.theme_box.findData("black"))  # live preview
    assert app.palette().color(QPalette.Window).name() == "#000000" and app.style().name().lower() == "fusion"
    dlg.reject()  # cancel undoes the preview
    assert app.style().name() == system_style
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    dlg.theme_box.setCurrentIndex(dlg.theme_box.findData("dark"))
    dlg.accept()
    from r34dl import config
    assert config.load()["theme"] == "dark" and app.palette().color(QPalette.Window).name() == "#2b2b2b"
    theme.apply("system")


def test_hover_preview_video_one_at_a_time(fresh, monkeypatch):
    from PySide6.QtWidgets import QApplication
    sources = []
    monkeypatch.setattr(ui.QMediaPlayer, "setSource", lambda self, url: sources.append(url.toString()))
    monkeypatch.setattr(ui.QMediaPlayer, "play", lambda self: None)
    fresh.show()
    st = fresh.first
    vid = Post(2, "h", "https://x/2.webm", "", "", [], "s", 0, 16, 9)
    st.model.append([mk(1), vid, Post(3, "h", "https://x/3.mp4", "", "", [], "s", 0, 16, 9)])
    QApplication.processEvents()
    pv = st.view.preview
    pv.hover(0)  # a still image: nothing to preview
    assert not pv._timer.isActive()
    pv.hover(1)
    assert pv._timer.isActive() and pv.widget is None  # waits for the mouse to rest
    pv._timer.timeout.emit()
    assert isinstance(pv.widget, ui.VideoView) and sources == ["https://x/2.webm"]
    cell = st.view.visualRect(st.model.index(1))
    assert cell.contains(pv.widget.geometry()) and pv.widget.testAttribute(Qt.WA_TransparentForMouseEvents)
    pv.hover(2)  # moving on drops the first preview right away
    assert pv.widget is None and pv._timer.isActive()
    st.view.verticalScrollBar().valueChanged.emit(5)  # scrolling stops it too
    assert not pv._timer.isActive()
    fresh.cfg["hover_preview"] = False
    pv.hover(-1); pv.hover(1)
    assert not pv._timer.isActive()  # setting off


def test_hover_preview_skips_big_gifs(fresh, monkeypatch):
    class GifReply(QObject):
        finished = Signal()
        downloadProgress = Signal("qlonglong", "qlonglong")
        aborted = False
        def abort(self): self.aborted = True
        def error(self): return ui.QNetworkReply.NoError
        def deleteLater(self): pass
    replies = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: replies.append(GifReply()) or replies[-1])
    st = fresh.first
    st.model.append([Post(4, "h", "https://x/4.gif", "", "", [], "s", 0, 10, 10)])
    pv = st.view.preview
    pv.hover(0)
    pv._timer.timeout.emit()
    replies[-1].downloadProgress.emit(1000, ui.HoverPreview.GIF_MAX_BYTES + 1)
    assert replies[-1].aborted and pv._reply is None and pv.widget is None


# ---- master password (Phase 4)

def _protect(win, password="hunter22"):
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam, vault=win._vault)
    dlg.protect_check.setChecked(True)
    dlg.new_pw.setText(password)
    dlg.new_pw2.setText(password)
    dlg.accept()
    assert dlg.result() == ui.QDialog.Accepted
    win.cfg, win._vault = dlg.result_cfg(), dlg.vault


def test_turn_on_protection_encrypts_on_disk(win, monkeypatch):
    from r34dl import config, secret
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append(a[2])))
    win.cfg.update(user_id="777", api_key="APIKEYVALUE")
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    f = dlg._account_form
    assert not f.isRowVisible(dlg.new_pw) and not f.isRowVisible(dlg.cur_pw)
    dlg.protect_check.setChecked(True)
    assert f.isRowVisible(dlg.new_pw) and f.isRowVisible(dlg.new_pw2) and not f.isRowVisible(dlg.cur_pw)
    dlg.new_pw.setText("hunter22"); dlg.new_pw2.setText("different")
    dlg.accept()
    assert boxes and "match" in boxes[-1] and dlg.result() != ui.QDialog.Accepted  # stays open
    _protect(win)
    disk = config.path().read_text()
    assert "APIKEYVALUE" not in disk and "777" not in disk.split('"secret"')[0]
    creds, _ = secret.unlock(config.load()["secret"], "hunter22")
    assert creds == {"user_id": "777", "api_key": "APIKEYVALUE"} and win.cfg["api_key"] == "APIKEYVALUE"


def test_startup_unlock_wrong_then_right(win, monkeypatch):
    from r34dl import config
    win.cfg.update(user_id="777", api_key="APIKEYVALUE")
    _protect(win)
    dlg = ui.UnlockDialog(config.load()["secret"])
    dlg.pw.setText("wrong")
    dlg.try_unlock()
    assert "Wrong password" in dlg.msg.text() and dlg.creds is None
    dlg.pw.setText("hunter22")
    dlg.try_unlock()
    assert dlg.creds == {"user_id": "777", "api_key": "APIKEYVALUE"} and dlg.vault

    def fake_exec(self):
        self.pw.setText("hunter22")
        self.try_unlock()
        return ui.QDialog.Accepted
    monkeypatch.setattr(ui.UnlockDialog, "exec", fake_exec)
    w2 = ui.MainWindow()
    assert w2.cfg["api_key"] == ""  # locked until unlocked
    w2._startup()
    assert w2.cfg["api_key"] == "APIKEYVALUE" and w2._vault is not None


def test_forgot_password_clears_only_credentials(win, monkeypatch):
    from r34dl import config
    win.cfg.update(user_id="777", api_key="APIKEYVALUE")
    win.cfg["saved_searches"]["keep"] = {"include": "a", "exclude": ""}
    _protect(win)
    win._save_cfg()
    monkeypatch.setattr(ui.UnlockDialog, "exec", lambda self: ui.UnlockDialog.FORGOT)
    opened = []
    w2 = ui.MainWindow()
    monkeypatch.setattr(w2, "open_settings", lambda: opened.append(1))
    w2._startup()
    cfg = config.load()
    assert cfg["secret"] == {} and cfg["api_key"] == "" and "keep" in cfg["saved_searches"] and opened == [1]


def test_turn_off_needs_current_password_and_key_change_reseals(win, monkeypatch):
    from r34dl import config, secret
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append(a[2])))
    win.cfg.update(user_id="777", api_key="OLDKEY")
    _protect(win)
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam, vault=win._vault)
    dlg.key_edit.setText("NEWKEY")  # change the key while protected: no password needed (already unlocked)
    dlg.accept()
    win.cfg, win._vault = dlg.result_cfg(), dlg.vault
    assert secret.unlock(config.load()["secret"], "hunter22")[0]["api_key"] == "NEWKEY"
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam, vault=win._vault)
    dlg.protect_check.setChecked(False)
    dlg.cur_pw.setText("wrong")
    dlg.accept()
    assert "wrong" in boxes[-1] and config.load()["secret"]  # still protected
    dlg.cur_pw.setText("hunter22")
    dlg.accept()
    assert config.load()["secret"] == {} and config.load()["api_key"] == "NEWKEY"  # stored plainly again


def test_together_decodes_only_cells_near_the_screen(fresh, monkeypatch):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    posts = [Post(i, "h", f"https://x/{i}.jpg", "", f"https://x/s{i}.jpg", [], "s", 0, 400, 400) for i in range(1, 41)]
    tab = ui.TogetherTab(posts, fresh.nam, per_row=1)
    tab.resize(500, 600)
    tab.show()
    QApplication.processEvents()
    img = QPixmap(1600, 1600); img.fill(Qt.red)
    ba = QByteArray(); b = QBuffer(ba); b.open(QIODevice.WriteOnly); img.save(b, "JPG")
    for p in posts:
        tab._files[p.id] = QByteArray(ba)
    tab._relayout()
    QApplication.processEvents()
    tab._render_visible()
    near = set(tab._shown)
    assert near and len(near) < 10  # 40 posts, one per row: only those around the screen are decoded
    cell = tab.cells[next(iter(near))]
    assert cell.pixmap().width() <= tab._cell_width()  # decoded at cell size, not 1600 px
    tab.scroll.verticalScrollBar().setValue(tab.scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    assert tab._shown and not (tab._shown & near)  # scrolled away: the first ones were freed
    tab.release()


def test_together_skips_huge_gifs(fresh, monkeypatch):
    class GifReply(QObject):
        finished = Signal()
        downloadProgress = Signal("qlonglong", "qlonglong")
        aborted = False
        def abort(self): self.aborted = True
        def deleteLater(self): pass
    replies = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: replies.append(GifReply()) or replies[-1])
    gif = Post(7, "h", "https://x/7.gif", "", "", [], "s", 0, 10, 10)
    tab = ui.TogetherTab([gif, mk(8)], fresh.nam)
    replies[0].downloadProgress.emit(10, ui.HoverPreview.GIF_MAX_BYTES + 1)
    assert replies[0].aborted and "large GIF" in tab.cells[7].text()
    tab.release()



def _wait_for_zips(win, timeout=10):
    """ZIPs are packed on a background thread; wait for it and for its result to reach the window."""
    import time
    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + timeout
    while any(t.is_alive() for t in win._zip_threads) and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()


def test_zip_is_packed_off_the_ui_thread(fresh, monkeypatch, tmp_path):
    import threading as th
    seen = []
    real = ui._write_zip
    monkeypatch.setattr(ui, "_write_zip", lambda target, files: seen.append(th.current_thread()) or real(target, files))
    monkeypatch.setattr(fresh.downloader, "add", lambda items: added.extend(items))
    monkeypatch.setattr(ui.QInputDialog, "getText", staticmethod(lambda *a, **k: ("bg", True)))
    added = []
    fresh.cfg["save_dir"] = str(tmp_path)
    fresh.download_album([_artist_post(1), _artist_post(2)], "", zipped=True)
    for url, target in added:
        target.write_bytes(b"x")
        fresh.downloader.item_done.emit(url, str(target), True)
    _wait_for_zips(fresh)
    assert seen and seen[0] is not th.main_thread()
    assert (tmp_path / "bg.zip").exists() and "Saved bg.zip" in fresh.statusBar().currentMessage()



def _wait_until(cond, timeout=10):
    import time
    from PySide6.QtWidgets import QApplication
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def test_late_decode_for_previous_post_is_ignored(fresh, monkeypatch):
    from PySide6.QtGui import QImage
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    st = fresh.first
    st.model.append([mk(1), mk(2)])
    fresh.open_viewer(st.model.posts[0], False, st)
    v = fresh.tabs.currentWidget()
    old = v._load_id
    v.step(1)  # moved on before post 1's picture finished decoding
    img = QImage(10, 10, QImage.Format_RGB32)
    v.decoded.emit(old, img, False)
    assert not v.zoom_view.isVisibleTo(v) and v.post.id == 2  # stale picture not shown for post 2


def test_detail_decode_frees_the_file(fresh, monkeypatch):
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QImage
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    st = fresh.first
    st.model.append([mk(1)])
    fresh.open_viewer(st.model.posts[0], False, st)
    v = fresh.tabs.currentWidget()
    v._raw = QByteArray(b"file bytes")
    v.decoded.emit(v._load_id, QImage(20, 20, QImage.Format_RGB32), True)
    assert v._raw.isEmpty()


def test_hover_preview_gif_plays_on_its_tile(fresh, monkeypatch):
    import base64
    from PySide6.QtCore import QByteArray
    from PySide6.QtWidgets import QApplication
    gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")  # a real 1x1 GIF

    class GifReply(QObject):
        finished = Signal()
        downloadProgress = Signal("qlonglong", "qlonglong")
        def error(self): return ui.QNetworkReply.NoError
        def readAll(self): return QByteArray(gif)
        def abort(self): pass
        def deleteLater(self): pass
    replies = []
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: replies.append(GifReply()) or replies[-1])
    fresh.show()
    st = fresh.first
    st.model.append([mk(1), Post(2, "h", "https://x/2.gif", "", "", [], "s", 0, 10, 10)])
    QApplication.processEvents()
    pv = st.view.preview
    pv.hover(1)
    pv._timer.timeout.emit()
    replies[-1].finished.emit()
    assert isinstance(pv.widget, ui.QLabel) and pv._movie is not None
    assert pv._movie.state() == ui.QMovie.Running and pv.widget.movie() is pv._movie
    assert st.view.visualRect(st.model.index(1)).contains(pv.widget.geometry())
    pv.hover(-1)
    assert pv.widget is None and pv._movie is None


def test_closing_a_tab_repaints_before_tearing_down(fresh, monkeypatch):
    """The old tab's teardown (stopping streams can take a while) must not keep its picture on screen."""
    from PySide6.QtCore import QEvent, QObject as _QObject
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    fresh.show()
    st = fresh.first
    st.model.append([mk(1)])
    fresh.open_viewer(st.model.posts[0], False, st)
    v = fresh.tabs.currentWidget()
    QApplication.processEvents()

    class PaintCounter(_QObject):
        n = 0
        def eventFilter(self, obj, e):
            if e.type() == QEvent.Paint:
                self.n += 1
            return False
    counter = PaintCounter()
    st.installEventFilter(counter)
    painted_before_release = []
    real_release = v.release
    monkeypatch.setattr(v, "release", lambda: (painted_before_release.append(counter.n), real_release()))
    fresh._close_tab(fresh.tabs.indexOf(v))
    assert painted_before_release and painted_before_release[0] > 0  # the search tab was drawn first


def test_closed_search_tab_ignores_queued_checks(fresh, monkeypatch):
    import sys
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: errors.append(a[1]))
    tab = fresh.open_search_tab("x")
    tab._on_page_ready(tab.model.generation, [mk(i) for i in range(1, 101)], 100)  # queues a "load more?" check
    fresh._close_tab(fresh.tabs.indexOf(tab))
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    assert errors == []


def test_no_native_video_windows(fresh, monkeypatch):
    """Qt's QVideoWidget embeds a native window, which turns the whole window into separate native surfaces
    (closed tabs and the last video frame then linger on screen, and the grid flashes). Never create one."""
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(ui.QMediaPlayer, "setSource", lambda self, url: None)
    monkeypatch.setattr(ui.QMediaPlayer, "play", lambda self: None)
    monkeypatch.setattr(ui.QNetworkAccessManager, "get", lambda self, req: _NeverReply())
    fresh.show()
    st = fresh.first
    vid = Post(2, "h", "https://x/2.webm", "", "", [], "s", 0, 16, 9)
    st.model.append([mk(1), vid])
    QApplication.processEvents()
    st.view.preview.hover(1)
    st.view.preview._timer.timeout.emit()  # video hover preview
    fresh.open_viewer(vid, False, st)  # video post tab
    fresh.open_viewer(st.model.posts[0], False, st)  # picture post tab
    QApplication.processEvents()
    embedded = [w.metaObject().className() for w in QApplication.allWindows()
                if w.metaObject().className() not in ("QWidgetWindow",)]
    assert embedded == [], embedded


# ---- e-hentai login, per-site blacklist and favorites (Task 9)

def _eh_gallery(gid=7):
    album = {"gid": gid, "token": "0123456789", "title": "A gallery", "pages": 40, "category": "Doujinshi"}
    return Post(gid * 100000, "", "", "https://ehgt.org/t/cover.jpg", "", ["female:long_hair"], "doujinshi", 0, 0, 0,
                site="e-hentai", kind="gallery", page_url=f"https://e-hentai.org/g/{gid}/0123456789/", album=album)


def _eh_page(gid=7, page=3):
    album = {"gid": gid, "token": "0123456789", "title": "A gallery", "pages": 40, "category": "Doujinshi",
             "page": page, "sprite": (-100, 0, 100, 140),
             "shown": "https://h.example.org/shown.jpg", "original": "https://e-hentai.org/fullimg/x"}
    return Post(gid * 100000 + page, "", "https://h.example.org/shown.jpg", "https://ehgt.org/m/1.jpg", "",
                ["female:long_hair"], "doujinshi", 0, 0, 0, site="e-hentai", kind="page",
                page_url=f"https://e-hentai.org/s/abc/{gid}-{page}", album=album)


def test_eh_login_sealed_with_password(win, monkeypatch):
    from r34dl import config, secret
    win.cfg.update(user_id="777", api_key="APIKEYVALUE")
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    dlg.eh_member_edit.setText("4242")
    dlg.eh_hash_edit.setText("PASSHASHVALUE")
    dlg.protect_check.setChecked(True)
    dlg.new_pw.setText("hunter22")
    dlg.new_pw2.setText("hunter22")
    dlg.accept()
    assert dlg.result() == ui.QDialog.Accepted
    assert config.site_cfg(win.cfg, "e-hentai")["member_id"] == ""  # the caller's cfg is untouched
    win.cfg, win._vault = dlg.result_cfg(), dlg.vault
    assert config.site_cfg(win.cfg, "e-hentai")["pass_hash"] == "PASSHASHVALUE"  # in memory
    disk = config.path().read_text()
    assert "PASSHASHVALUE" not in disk and "4242" not in disk
    sc = config.load()["sites"]["e-hentai"]
    assert sc["member_id"] == "" and sc["pass_hash"] == ""
    creds, _ = secret.unlock(config.load()["secret"], "hunter22")
    assert creds["eh_member_id"] == "4242" and creds["eh_pass_hash"] == "PASSHASHVALUE"

    def fake_exec(self):
        self.pw.setText("hunter22")
        self.try_unlock()
        return ui.QDialog.Accepted
    monkeypatch.setattr(ui.UnlockDialog, "exec", fake_exec)
    w2 = ui.MainWindow()
    assert not w2.sites["e-hentai"].queue.logged_in  # locked
    w2._startup()
    sc = config.site_cfg(w2.cfg, "e-hentai")
    assert (sc["member_id"], sc["pass_hash"]) == ("4242", "PASSHASHVALUE") and "eh_member_id" not in w2.cfg
    assert w2.sites["e-hentai"].queue.logged_in and w2.cfg["api_key"] == "APIKEYVALUE"
    assert "PASSHASHVALUE" not in config.path().read_text()

    monkeypatch.setattr(ui.UnlockDialog, "exec", lambda self: ui.UnlockDialog.FORGOT)
    w3 = ui.MainWindow()
    monkeypatch.setattr(w3, "open_settings", lambda: None)
    w3._startup()  # forgotten password: the e-hentai login goes too
    assert config.site_cfg(w3.cfg, "e-hentai")["member_id"] == "" and not w3.sites["e-hentai"].queue.logged_in


def test_eh_login_applies_on_save_and_bad_value_refused(win, monkeypatch):
    from r34dl import config
    boxes = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: boxes.append(a[2])))
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam)
    dlg.eh_member_edit.setText("42")
    dlg.eh_hash_edit.setText("bad; value")
    dlg.accept()
    assert boxes and dlg.result() != ui.QDialog.Accepted
    assert "- and _" in boxes[-1]
    dlg.eh_hash_edit.setText("")  # only one of the two filled in
    dlg.accept()
    assert len(boxes) == 2 and "both" in boxes[-1] and dlg.result() != ui.QDialog.Accepted

    def fake_exec(self):
        self.eh_member_edit.setText(" 42 ")
        self.eh_hash_edit.setText("abc123")
        self.accept()
        return self.result()
    monkeypatch.setattr(ui.SettingsDialog, "exec", fake_exec)
    win.open_settings()
    assert win.sites["e-hentai"].queue.logged_in
    assert config.load()["sites"]["e-hentai"]["member_id"] == "42"  # not protected: saved plainly


def test_settings_blacklist_per_site(win):
    from r34dl import config
    win.cfg["blacklist"] = "old"
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam, sites=list(win.sites))
    assert dlg.blacklist_edit.toPlainText() == "old"
    dlg.bl_site_box.setCurrentIndex(dlg.bl_site_box.findData("e-hentai"))
    assert dlg.blacklist_edit.toPlainText() == ""
    dlg.blacklist_edit.setPlainText("male:yaoi")
    dlg.reject()
    assert config.site_cfg(win.cfg, "e-hentai")["blacklist"] == ""  # Cancel changes nothing
    dlg = ui.SettingsDialog(win.cfg, None, nam=win.nam, sites=list(win.sites))
    dlg.bl_site_box.setCurrentIndex(dlg.bl_site_box.findData("e-hentai"))
    dlg.blacklist_edit.setPlainText("male:yaoi")
    dlg.bl_site_box.setCurrentIndex(dlg.bl_site_box.findData("rule34.xxx"))
    assert dlg.blacklist_edit.toPlainText() == "old"
    dlg.blacklist_edit.setPlainText("")
    dlg.accept()
    cfg = config.load()
    assert cfg["blacklist"] == "" and cfg["sites"]["e-hentai"]["blacklist"] == "male:yaoi"


def test_favorite_eh_page_not_storing_file_url(win):
    import json
    from r34dl import config
    p = _eh_page()
    win.toggle_favorite(p)
    saved = json.loads(config.favorites_path("e-hentai").read_text())
    assert len(saved) == 1 and "file_url" not in saved[0]
    assert "shown" not in saved[0]["album"] and "original" not in saved[0]["album"]
    assert "h.example.org" not in config.favorites_path("e-hentai").read_text()
    assert config.load_favorites() == []  # rule34's file is untouched
    back = ui.MainWindow().favorites["e-hentai"][p.id]
    assert back.kind == "page" and back.page_url == p.page_url and back.preview_url == p.preview_url
    assert back.file_url == "" and back.album["gid"] == 7 and back.album["page"] == 3
    assert list(back.album["sprite"]) == [-100, 0, 100, 140]
    g = _eh_gallery()
    win.toggle_favorite(g)
    assert ui.MainWindow().favorites["e-hentai"][g.id].album == g.album
    assert p.file_url and "shown" in p.album  # the live post keeps its cached addresses


def test_favorites_tab_site_switch(win):
    win.toggle_favorite(_fav_post(1))
    win.toggle_favorite(_eh_gallery())
    win.open_favorites()
    fav = win.tabs.currentWidget()
    assert [p.id for p in fav.model.posts] == [1]
    fav.site_box.setCurrentIndex(fav.site_box.findData("e-hentai"))
    assert fav.site.name == "e-hentai" and [p.id for p in fav.model.posts] == [700000]
    assert "ehentai_" in fav.template_label.text()
    assert "female:long hair" in fav.tags_hint.text() and "language:" in fav.include_edit.placeholderText()
    assert "Filters your saved favorites" in fav.tags_hint.text()
    fav.include_edit.setPlainText("female:long_hair")  # namespaced e-hentai tags filter locally
    fav.search()
    assert [p.id for p in fav.model.posts] == [700000]
    fav.include_edit.setPlainText("female:short_hair")
    fav.search()
    assert fav.model.posts == []
    eh = win.sites["e-hentai"]
    assert eh.page_ready.disconnect(fav._on_site_page)  # connected to the new site
    eh.page_ready.connect(fav._on_site_page)


def test_favorite_gallery_opens_gallery_tab(win, monkeypatch):
    from r34dl.gallery import GalleryTab
    monkeypatch.setattr(win.sites["e-hentai"], "fetch_gallery", lambda *a: None)
    g = _eh_gallery()
    win.toggle_favorite(g)
    win.open_favorites()
    fav = win.tabs.currentWidget()
    fav.site_box.setCurrentIndex(fav.site_box.findData("e-hentai"))
    fav._on_post_clicked(0, False)
    gt = win.tabs.currentWidget()
    assert isinstance(gt, GalleryTab) and gt.gallery.id == g.id


def test_eh_tag_list_shows_no_zero_counts(qapp):
    tl = ui.TagList()
    g = _eh_gallery()
    g.tag_info = {"female:long_hair": ("general", 0)}  # as parse_gdata makes it: e-hentai sends no counts
    tl.set_post(g)
    assert [tl.item(i).text() for i in range(tl.count())] == ["General", "  female:long_hair"]


def test_eh_retarget_keeps_history_and_zip_tracking(win, tmp_path):
    job = ui._ZipJob("z", tmp_path, {str(tmp_path / "a.png"), str(tmp_path / "b.png")})
    win._zip_jobs.append(job)
    win._target_ids[str(tmp_path / "a.png")] = ("e-hentai", 700001)
    win.feeder.retargeted.emit(str(tmp_path / "a.png"), str(tmp_path / "a.webp"))
    assert job.pending == {str(tmp_path / "a.webp"), str(tmp_path / "b.png")}
    assert win._target_ids == {str(tmp_path / "a.webp"): ("e-hentai", 700001)}
    win._zip_jobs.remove(job)
