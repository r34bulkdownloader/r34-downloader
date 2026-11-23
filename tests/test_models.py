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


def test_old_favorite_drops_saved_addresses():
    d = eh_page().to_dict()
    d["album"].update(shown="https://h.example.org/old.jpg", original="https://e-hentai.org/fullimg/x")  # saved before to_dict dropped them
    album = Post.from_dict(d).album
    assert "shown" not in album and "original" not in album and album["page"] == 3


@pytest.mark.parametrize("site,field,value", [
    ("rule34.xxx", "page_url", "file:///etc/passwd"),  # open_in_browser opens it
    ("e-hentai", "gid", "7"),
    ("e-hentai", "gid", True),
    ("e-hentai", "token", 5),
    ("e-hentai", "pages", "many"),
    ("e-hentai", "page", None),
])
def test_bad_favorite_values_are_refused(site, field, value):
    p = eh_page() if site == "e-hentai" else Post(5, "h", "https://x/a.jpg", "", "", [], "s", 0, 1, 1)
    d = p.to_dict()
    if field == "page_url":
        d["page_url"] = value
    else:
        d["album"][field] = value
    with pytest.raises(ValueError):
        Post.from_dict(d)


def test_rule34_favorite_with_an_https_page_url_is_kept():
    d = Post(5, "h", "https://x/a.jpg", "", "", [], "s", 0, 1, 1, page_url="https://rule34.xxx/p").to_dict()
    assert Post.from_dict(d).page_url == "https://rule34.xxx/p"
