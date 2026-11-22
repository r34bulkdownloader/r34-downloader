import copy
import json
import stat

import pytest

from r34dl import config


@pytest.fixture(autouse=True)
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_path_location(tmp_path):
    assert config.path() == tmp_path / "r34-downloader" / "config.json"


def test_load_defaults_when_missing():
    assert config.load() == config.DEFAULTS


def test_roundtrip():
    cfg = config.load()
    cfg["api_key"] = "k"
    config.save(cfg)
    assert config.load()["api_key"] == "k"


def test_file_mode_0600():
    config.save(config.load())
    assert stat.S_IMODE(config.path().stat().st_mode) == 0o600


def test_file_mode_tightened_when_existing():
    config.path().parent.mkdir(parents=True)
    config.path().write_text("{}")
    config.path().chmod(0o644)
    config.save(config.load())
    assert stat.S_IMODE(config.path().stat().st_mode) == 0o600


def test_unknown_keys_and_missing_keys():
    config.path().parent.mkdir(parents=True)
    config.path().write_text('{"template": "x", "bogus": 1}')
    cfg = config.load()
    assert cfg == {**config.DEFAULTS, "template": "x"}


def test_corrupt_file_falls_back():
    config.path().parent.mkdir(parents=True)
    config.path().write_text("{not json")
    assert config.load() == config.DEFAULTS
    assert config.path().read_text() == "{not json"


def test_wrong_types_fall_back_to_defaults():
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text('{"api_key": 5, "user_id": null, "start_muted": false, "template": "t"}')
    cfg = config.load()
    assert cfg["api_key"] == "" and cfg["user_id"] == ""
    assert cfg["start_muted"] is False and cfg["template"] == "t"


def test_saved_searches_roundtrip_and_validation():
    cfg = config.load()
    cfg["saved_searches"]["cats"] = {"include": "cat", "exclude": "dog"}
    assert config.DEFAULTS["saved_searches"] == {}  # default never mutated
    config.save(cfg)
    assert config.load()["saved_searches"] == {"cats": {"include": "cat", "exclude": "dog"}}
    config.path().write_text('{"saved_searches": {"ok": {"include": "a"}, "bad": 5, "worse": {"include": 3}}}')
    assert config.load()["saved_searches"] == {"ok": {"include": "a", "exclude": ""}}


def test_history_append_load_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config.load_history() == set()
    config.add_history(5)
    config.add_history(7)
    config.history_path().write_text(config.history_path().read_text() + "junk\n")
    assert config.load_history() == {5, 7}
    assert oct(config.history_path().stat().st_mode & 0o777) == "0o600"
    config.clear_history()
    assert config.load_history() == set()


def test_favorites_roundtrip_private(tmp_path, monkeypatch):
    from r34dl.models import Post
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config.load_favorites() == []
    p = Post(7, "h", "https://x/7.jpg", "https://x/p.jpg", "", ["a", "b"], "e", 3, 10, 20,
             tag_info={"a": ("artist", 5)})
    config.update_favorites(add=p.to_dict())
    assert oct(config.favorites_path().stat().st_mode & 0o777) == "0o600"
    d = config.load_favorites()
    assert d[0]["site"] == "rule34.xxx" and Post.from_dict(d[0]) == p
    # another window added one meanwhile, and an entry this version can't read: both survive our change
    config._write_private(config.favorites_path(), [{"id": 9, "future": True}] + d)
    out = config.update_favorites(add={**p.to_dict(), "id": 8})
    assert [e["id"] for e in out] == [8, 9, 7]
    assert [e["id"] for e in config.update_favorites(remove_id=7)] == [8, 9]
    # a corrupt file is set aside, not overwritten
    config.favorites_path().write_text("{broken")
    assert config.load_favorites() == []
    config.update_favorites(add=p.to_dict())
    assert config.favorites_path().with_name("favorites.json.bad").read_text() == "{broken"


def test_favorite_entries_must_be_https():
    import pytest
    from r34dl.models import Post
    base = Post(1, "h", "https://x/1.jpg", "file:///etc/x", "http://x/s", [], "e", 0, 1, 1).to_dict()
    p = Post.from_dict(base)
    assert p.preview_url == "" and p.sample_url == ""
    with pytest.raises(ValueError):
        Post.from_dict({**base, "file_url": "file:///etc/passwd"})


def test_followed_cleaned():
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text('{"followed": ["a", "a", 5, " ", "b"]}')
    assert config.load()["followed"] == ["a", "b"]


def test_protected_credentials_never_written_plain():
    cfg = config.load()
    cfg.update(user_id="42", api_key="SECRETKEY", secret={"v": 1, "data": "x"})
    config.save(cfg)
    text = config.path().read_text()
    assert "SECRETKEY" not in text and config.load()["secret"]["data"] == "x"


def test_secret_roundtrip_and_wrong_password():
    import pytest
    from r34dl import secret
    vault = secret.new_key("hunter22")
    blob = secret.seal(vault, {"user_id": "42", "api_key": "APIKEYVALUE"})
    assert "APIKEYVALUE" not in str(blob) and blob["kdf"] == "scrypt"
    creds, vault2 = secret.unlock(blob, "hunter22")
    assert creds == {"user_id": "42", "api_key": "APIKEYVALUE"} and vault2 == vault
    with pytest.raises(secret.WrongPassword):
        secret.unlock(blob, "nope")
    with pytest.raises(secret.BadSecret):
        secret.unlock({**blob, "n": 2 ** 30}, "hunter22")  # a hand-edited cost can't hang the app
    with pytest.raises(secret.BadSecret):
        secret.unlock({"garbage": 1}, "x")


def test_reseal_keeps_the_blobs_own_settings():
    from r34dl import secret
    other = secret.Vault(*(lambda salt: (secret._derive("pw123456", salt, 2 ** 14, 8, 1), salt))(b"s" * 16), 2 ** 14)
    blob = secret.seal(other, {"user_id": "1", "api_key": "a"})
    creds, vault = secret.unlock(blob, "pw123456")
    resealed = secret.seal(vault, {**creds, "api_key": "b"})  # e.g. the key changed in Settings
    assert resealed["n"] == 2 ** 14 and secret.unlock(resealed, "pw123456")[0]["api_key"] == "b"


def test_other_window_cannot_silently_unprotect():
    cfg = config.load()
    config.save({**cfg, "secret": {"v": 1, "data": "x"}})  # window B turned protection on
    config.save({**cfg, "user_id": "777", "api_key": "PLAINKEY", "secret": {}})  # window A quits later
    disk = config.load()
    assert disk["secret"] == {"v": 1, "data": "x"} and disk["api_key"] == ""
    config.save({**cfg, "secret": {}}, unprotect=True)  # turning it off on purpose still works
    assert config.load()["secret"] == {}


def test_settings_paste_box_fills_both_fields(qapp):
    from r34dl.dialogs import SettingsDialog
    key = "cd34" * 32
    d = SettingsDialog({**config.DEFAULTS}, None)
    d.paste_edit.setText(f"&api_key={key}&user_id=7654321")
    assert (d.user_edit.text(), d.key_edit.text()) == ("7654321", key)
    assert d.paste_edit.text() == "" and "filled in" in d.paste_status.text()
    d.paste_edit.setText("just some text")
    assert "api_key=" in d.paste_status.text() and d.key_edit.text() == key  # nothing overwritten


def test_settings_whole_line_pasted_into_one_field(qapp):
    from r34dl.dialogs import SettingsDialog
    key = "ef56" * 32
    d = SettingsDialog({**config.DEFAULTS}, None)
    d.user_edit.setText(f"&api_key={key}&user_id=1112223")
    assert (d.user_edit.text(), d.key_edit.text()) == ("1112223", key)
    d.key_edit.setText("api_key=")  # half a line: the field doesn't keep "api_key="
    assert d.key_edit.text() == ""


def test_settings_support_button_opens_kofi(qapp, monkeypatch):
    from r34dl import dialogs
    opened = []
    monkeypatch.setattr(dialogs.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    d = dialogs.SettingsDialog({**config.DEFAULTS}, None)
    [b for b in d.buttons.buttons() if "Ko-fi" in b.text()][0].click()
    assert opened == [dialogs.KOFI_URL] and d.result() == 0  # the dialog stays open


def test_old_config_gets_site_defaults(tmp_path):
    config.path().parent.mkdir(parents=True, exist_ok=True)
    config.path().write_text('{"template": "x_{id}", "blacklist": "scat"}')
    before = config.path().read_text()
    cfg = config.load()
    eh = config.site_cfg(cfg, "e-hentai")
    assert cfg["template"] == "x_{id}" and config.site_cfg(cfg, "rule34.xxx") is cfg
    assert eh["template"] == "ehentai_{album_id}_{page}" and eh["blacklist"] == ""
    assert config.path().read_text() == before  # loading never rewrites


def test_site_cfg_bad_types_fall_back():
    cfg = {**config.DEFAULTS, "sites": {"e-hentai": {"template": 5, "followed": ["a"]}}}
    eh = config.site_cfg(cfg, "e-hentai")
    assert eh["template"] == "ehentai_{album_id}_{page}" and eh["followed"] == ["a"]


def test_history_per_site():
    config.add_history(5)
    config.add_history(500001, site="e-hentai")
    assert config.load_history() == {5}
    assert config.load_history("e-hentai") == {500001}
    assert config.history_path("e-hentai").name == "history-e-hentai.txt"
    config.clear_history()
    assert config.load_history("e-hentai") == set() and config.load_history() == set()


def test_favorites_per_site_keep_rule34_file_untouched():
    config.update_favorites(add={"site": "rule34.xxx", "id": 1, "hash": "h", "file_url": "https://x/a.jpg"})
    before = config.favorites_path().read_text()
    config.update_favorites(add={"site": "e-hentai", "id": 700000, "kind": "gallery",
                                 "page_url": "https://e-hentai.org/g/7/abc/", "hash": "", "file_url": ""},
                            site="e-hentai")
    assert config.favorites_path().read_text() == before
    assert [d["id"] for d in config.load_favorites("e-hentai")] == [700000]


def test_secret_blanks_eh_cookies(tmp_path):
    cfg = {**config.DEFAULTS, "secret": {"x": 1}}
    config.site_cfg(cfg, "e-hentai").update(member_id="42", pass_hash="p")
    config.save(cfg)
    disk = json.loads(config.path().read_text())
    assert disk["sites"]["e-hentai"]["member_id"] == "" and disk["sites"]["e-hentai"]["pass_hash"] == ""
    assert config.site_cfg(cfg, "e-hentai")["member_id"] == "42"  # the in-memory copy is untouched


def test_site_cfg_returns_same_live_dict():
    cfg = copy.deepcopy(config.DEFAULTS)
    eh = config.site_cfg(cfg, "e-hentai")
    assert eh is config.site_cfg(cfg, "e-hentai")
    eh["member_id"] = "9"
    assert config.site_cfg(cfg, "e-hentai")["member_id"] == "9" and cfg["sites"]["e-hentai"] is eh


def test_settings_template_per_site(qapp):
    from r34dl import dialogs
    d = dialogs.SettingsDialog({**config.DEFAULTS, "sites": {}}, None)
    assert d.site_box.findData("e-hentai") >= 0
    d.template_edit.setText("r_{id}")
    d.site_box.setCurrentIndex(d.site_box.findData("e-hentai"))
    assert d.template_edit.text() == config.SITE_DEFAULTS["e-hentai"]["template"]
    d.template_edit.setText("eh_{page}")
    d.site_box.setCurrentIndex(d.site_box.findData("rule34.xxx"))
    assert d.template_edit.text() == "r_{id}"
    d.site_box.setCurrentIndex(d.site_box.findData("e-hentai"))
    d.reset_btn.click()
    assert d.template_edit.text() == config.SITE_DEFAULTS["e-hentai"]["template"]
    d.template_edit.setText("eh_{page}")
    d.accept()
    out = d.result_cfg()
    assert out["template"] == "r_{id}" and out["sites"]["e-hentai"]["template"] == "eh_{page}"
