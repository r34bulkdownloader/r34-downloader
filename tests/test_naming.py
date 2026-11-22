from pathlib import Path

import pytest

from r34dl.models import Post
from r34dl.naming import render, target_path, unique_path

HASH = "302b97b7cd0efb3dba807a8f83567d1b"


def make_post(tags=None):
    return Post(
        12345, HASH, f"https://x/images/1/{HASH}.JPEG", "https://x/p.jpg",
        "https://x/s.jpg", tags if tags is not None else ["a", "b/c", "d"],
        "explicit", 7, 100, 200,
    )


def segments(rel: str):
    return rel.split("/")


def test_ext_lowercase():
    assert make_post().ext == "jpeg"


def test_default_template():
    p = target_path(Path("/d"), "rule34_{id}_{hash}", make_post(), "")
    assert p == Path(f"/d/rule34_12345_{HASH}.jpeg")


def test_tags_n():
    assert render("{tags:2}", make_post(), "") == "a_b_c"


def test_subfolder_literal():
    assert render("{rating}/{id}", make_post(), "") == "explicit/12345"


def test_traversal(tmp_path):
    post = make_post(["..", "../../etc"])
    out = render("{tags}", post, "")
    assert ".." not in segments(out)
    p = target_path(tmp_path, "{tags}", post, "")
    assert p.resolve().is_relative_to(tmp_path.resolve())
    p = target_path(tmp_path, "../{id}/../{search}", post, "..")
    assert p.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in p.relative_to(tmp_path).parts


def test_control_chars_stripped():
    out = render("{tags}", make_post(["a\x00b\nc"]), "")
    assert "\x00" not in out and "\n" not in out


def test_long_tags_keep_id_hash():
    post = make_post(["t" * 30 for _ in range(500)])
    p = target_path(Path("/d"), "{tags}_{id}_{hash}", post, "")
    assert len(p.name.encode()) <= 255
    assert "12345" in p.name and HASH in p.name


def test_unicode_tags_byte_limit():
    post = make_post(["日本語"] * 200)
    p = target_path(Path("/d"), "{tags}_{id}", post, "")
    assert len(p.name.encode()) <= 255
    p.name.encode("utf-8").decode("utf-8")
    assert "12345" in p.name


def test_hard_truncate_last_resort():
    p = target_path(Path("/d"), "x" * 400 + "{id}", make_post(), "")
    assert len(p.name.encode()) <= 255
    assert p.name.endswith("12345.jpeg")


def test_unknown_token():
    with pytest.raises(ValueError):
        render("{nope}", make_post(), "")


def test_bad_syntax():
    with pytest.raises(ValueError):
        render("{id", make_post(), "")
    with pytest.raises(ValueError):
        render("{id:3}", make_post(), "")


def test_leading_dot_space_trimmed():
    assert render(" .{id}", make_post(), "") == "12345"


def test_unique_path_skip_and_suffix(tmp_path):
    p = tmp_path / "stem.jpeg"
    p.write_bytes(b"abc")
    assert unique_path(p, 3) is None
    assert unique_path(p, 9) == tmp_path / "stem_1.jpeg"
    (tmp_path / "stem_1.jpeg").write_bytes(b"x")
    assert unique_path(p, 9) == tmp_path / "stem_2.jpeg"
    assert unique_path(tmp_path / "new.jpeg", 5) == tmp_path / "new.jpeg"


def test_kind_tokens():
    p = make_post(["nami_(one_piece)", "one_piece", "a/b", "x"])
    p.tag_info = {"nami_(one_piece)": ("character", 1), "one_piece": ("copyright", 1), "a/b": ("artist", 1),
                  "x": ("artist", 1)}
    assert render("{artist}/{character}_{copyright}_{id}", p, "") == "a_b+x/nami_(one_piece)_one_piece_12345"
    assert render("{artist}", make_post(), "") == "unknown"  # no type info


def test_index_token_and_album_template():
    from r34dl.naming import album_template, index_text
    assert index_text(0, 5) == "001" and index_text(41, 1200) == "0042"
    t = album_template("{artist}/rule34_{id}")
    assert t == "{index}_rule34_{id}"
    assert render(t, make_post(), "", "007") == "007_rule34_12345"
