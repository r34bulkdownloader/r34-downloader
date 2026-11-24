import copy
import json
import os
import stat

import pytest

from r34dl import backup, config


def _r34(i):
    return {"site": "rule34.xxx", "id": i, "hash": f"h{i}", "file_url": f"https://x/{i}.jpg", "tags": ["cat"]}


def _eh_gallery(gid):
    return {"site": "e-hentai", "id": gid * 100000, "kind": "gallery", "hash": "", "file_url": "",
            "page_url": f"https://e-hentai.org/g/{gid}/0123456789/",
            "album": {"gid": gid, "token": "0123456789", "title": "T", "pages": 3}}


def _cfg(**kw):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg.update(kw)
    return cfg


@pytest.fixture
def mine():
    """This computer: a login, a followed tag, a saved search, favorites on both sites and some history."""
    cfg = _cfg(user_id="111", api_key="secretkey", followed=["cat"], blacklist="gore",
               saved_searches={"mine": {"include": "a", "exclude": ""}}, theme="dark",
               save_dir=os.path.expanduser("~/Pictures/r34"))
    eh = config.site_cfg(cfg, "e-hentai")
    eh.update(member_id="42", pass_hash="ph", followed=["female:x"])
    config.write_favorites([_r34(1), _r34(2)])
    config.write_favorites([_eh_gallery(7)], "e-hentai")
    config.write_history({1, 5})
    config.write_history({700001}, "e-hentai")
    return cfg


def test_export_leaves_logins_out_unless_asked(mine):
    data = backup.export_data(mine, include_history=False, include_login=False)
    s = data["settings"]
    assert data["format"] == "r34-downloader-backup" and data["version"] == 1
    assert s["user_id"] == s["api_key"] == "" and "secret" not in s
    assert s["sites"]["e-hentai"]["member_id"] == s["sites"]["e-hentai"]["pass_hash"] == ""
    assert mine["api_key"] == "secretkey" and config.site_cfg(mine, "e-hentai")["pass_hash"] == "ph"  # untouched
    assert s["save_dir"] == "~/Pictures/r34"  # no home folder (login name) in the file
    assert [d["id"] for d in data["favorites"]["rule34.xxx"]] == [1, 2] and data["history"] is None
    with_all = backup.export_data(mine, include_history=True, include_login=True)
    assert with_all["settings"]["api_key"] == "secretkey" and with_all["history"]["rule34.xxx"] == [1, 5]
    assert with_all["settings"]["sites"]["e-hentai"]["pass_hash"] == "ph"


def test_export_never_includes_the_password_protected_blob(mine):
    mine["secret"] = {"sealed": "x"}
    assert "secret" not in backup.export_data(mine, True, True)["settings"]


def test_written_file_is_private_and_reads_back(mine, tmp_path):
    p = tmp_path / "b.json"
    backup.write(p, backup.export_data(mine, True, False))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert backup.read(p)["favorites"]["e-hentai"][0]["id"] == 700000


@pytest.mark.parametrize("content, msg", [
    ("not json", "isn't valid JSON"),
    ('{"hello": 1}', "isn't an R34 Downloader backup"),
    ('{"format": "r34-downloader-backup", "version": 99}', "newer version"),
])
def test_read_rejects_other_files(tmp_path, content, msg):
    p = tmp_path / "x.json"
    p.write_text(content)
    with pytest.raises(ValueError, match=msg):
        backup.read(p)


def _other_computer():
    """A backup from another computer: different login, tags, searches, favorites and history."""
    settings = _cfg(user_id="222", api_key="otherkey", followed=["Cat", "dog"], blacklist="gore\nscat",
                    saved_searches={"mine": {"include": "zzz", "exclude": ""}, "theirs": {"include": "b", "exclude": ""}},
                    theme="light", save_dir="~/Downloads/other")
    config.site_cfg(settings, "e-hentai").update(followed=["female:y"])
    return {"format": "r34-downloader-backup", "version": 1, "settings": settings,
            "favorites": {"rule34.xxx": [_r34(2), _r34(3), {"broken": True}, {**_r34(4), "file_url": "file:///etc/x"}],
                          "e-hentai": [_eh_gallery(8)]},
            "history": {"rule34.xxx": [5, 6], "e-hentai": "junk"}}


def test_merge_adds_theirs_and_keeps_my_preferences(mine):
    new, notes = backup.import_data(_other_computer(), mine, "merge", protected=False)
    assert new["theme"] == "dark" and new["save_dir"] == mine["save_dir"]  # my preferences
    assert new["followed"] == ["cat", "dog"]  # "Cat" is already there (any case)
    assert new["saved_searches"]["mine"]["include"] == "a" and "theirs" in new["saved_searches"]  # mine wins
    assert new["blacklist"] == "gore\nscat"
    assert config.site_cfg(new, "e-hentai")["followed"] == ["female:x", "female:y"]
    assert new["user_id"] == "111" and new["api_key"] == "secretkey" and notes == []  # I have a login: kept
    assert [d["id"] for d in config.load_favorites()] == [1, 2, 3]  # broken and file:// entries skipped
    assert [d["id"] for d in config.load_favorites("e-hentai")] == [700000, 800000]
    assert config.load_history() == {1, 5, 6} and config.load_history("e-hentai") == {700001}  # bad list ignored


def test_replace_takes_everything_from_the_file(mine):
    new, notes = backup.import_data(_other_computer(), mine, "replace", protected=False)
    assert new["theme"] == "light" and new["followed"] == ["Cat", "dog"] and new["blacklist"] == "gore\nscat"
    assert new["save_dir"] == os.path.expanduser("~/Downloads/other")  # "~" opened up on this computer
    assert new["user_id"] == "222" and new["api_key"] == "otherkey"
    eh = config.site_cfg(new, "e-hentai")
    assert eh["member_id"] == "42" and eh["pass_hash"] == "ph"  # the file has no e-hentai login: mine stays
    assert [d["id"] for d in config.load_favorites()] == [2, 3]
    assert config.load_history() == {5, 6}


def test_a_protected_login_is_never_replaced(mine):
    mine["secret"] = {"sealed": "x"}
    new, notes = backup.import_data(_other_computer(), mine, "replace", protected=True)
    assert new["api_key"] == "secretkey" and new["secret"] == {"sealed": "x"}
    assert len(notes) == 1 and "password-protected" in notes[0]


def test_round_trip_to_an_empty_computer(mine, tmp_path, monkeypatch):
    p = tmp_path / "b.json"
    backup.write(p, backup.export_data(mine, include_history=True, include_login=True))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "new-computer"))
    new, _ = backup.import_data(backup.read(p), _cfg(), "merge", protected=False)
    assert new["api_key"] == "secretkey" and config.site_cfg(new, "e-hentai")["pass_hash"] == "ph"
    assert [d["id"] for d in config.load_favorites()] == [1, 2] and config.load_history() == {1, 5}
    assert json.loads(json.dumps(new)) == new  # saveable as it is
