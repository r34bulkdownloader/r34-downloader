import pytest

from r34dl.models import Post


def eh_page(**kw):
    d = dict(id=700003, hash="", file_url="", preview_url="", sample_url="", tags=[], rating="", score=0,
             width=0, height=0, site="e-hentai", kind="page", page_url="https://e-hentai.org/s/a/7-3",
             album={"gid": 7, "token": "t", "title": "T" * 50, "page": 3, "pages": 9, "category": "Manga"},
             file_name="003.png")
    return Post(**{**d, **kw})


def test_eh_page_roundtrip():
    p = eh_page()
    assert Post.from_dict(p.to_dict()) == p


def test_eh_page_url_must_be_eh():
    d = eh_page().to_dict()
    d["page_url"] = "https://evil.example/s/a/1-1"
    with pytest.raises(ValueError):
        Post.from_dict(d)


def test_ext_from_file_name():
    assert eh_page().ext == "png"


def test_rule34_dict_unchanged_and_label():
    p = Post(5, "h", "https://x/a.jpg", "", "", [], "s", 0, 1, 1)
    assert set(p.to_dict()) == {"site", "id", "hash", "file_url", "preview_url", "sample_url", "tags", "rating",
                                "score", "width", "height", "tag_info"}
    assert p.label == "#5"
    assert eh_page().label == "T" * 30 + " · p3"
    assert eh_page(kind="gallery").label == "T" * 40
