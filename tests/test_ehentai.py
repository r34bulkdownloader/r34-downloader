import json
import re
from pathlib import Path

import pytest

from r34dl.models import TAG_KINDS, Post
from r34dl.sites.ehentai import (EH, GalleryWarning, ImageLimit, build_query, gallery_url, gdata_body,
                                 parse_gallery, parse_gdata, parse_picture, parse_search, safe_image_url,
                                 search_url, split_tags, include_term, parse_total)

FIX = Path(__file__).parent / "fixtures" / "eh"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def fixture_bytes(name):
    return (FIX / name).read_bytes()


def gallery_post():
    return [g for g in parse_gdata(fixture_bytes("gdata.json")) if g.album["gid"] == 4348914][0]


def test_split_tags():
    assert split_tags(" female:big breasts , artist:foo\n\n other ") == ["female:big breasts", "artist:foo", "other"]


def test_build_query_search_is_free_text_excludes_are_tags():
    # the search box goes as typed, like the site's (title words and tags); lines just become spaces
    q = build_query('my title\n  female:"big breasts$"', "language:english, male:big penis", ["guro"])
    assert q == 'my title female:"big breasts$" -language:"english$" -male:"big penis$" -"guro$"'


def test_build_query_empty_parts_dropped():
    assert build_query("  \n ", '-, female:, ""', []) == ""
    assert build_query("", "", ["x"]) == '-"x$"'


def test_include_term_quotes_a_clicked_tag():
    assert include_term("female:long hair") == 'female:"long hair$"'
    assert include_term("solo") == '"solo$"'


def test_parse_total():
    assert parse_total("<p>Found about 385,146 results. </p>") == (385146, True)
    assert parse_total("<p>Found 12 results.</p>") == (12, False)
    assert parse_total("<p>Found 1 result.</p>") == (1, False)
    assert parse_total("<p>No hits found</p>") == (0, False)
    assert parse_total(fixture("search.html")) is None  # trimmed fixture has no count line


def test_search_url():
    assert search_url('a:"b$"', None) == f"{EH}/?f_search=a%3A%22b%24%22&nw=always"
    assert search_url("x", 4348909).endswith("&nw=always&next=4348909")


def test_parse_search_fixture():
    pairs, nxt = parse_search(fixture("search.html"))
    assert len(pairs) >= 20 and all(isinstance(g, int) and re.fullmatch(r"[0-9a-f]{10}", t) for g, t in pairs)
    assert isinstance(nxt, int)
    assert pairs[0] == (4348999, "ff9bd7b6f9") and nxt == 4348909
    assert len(set(pairs)) == len(pairs)


def test_parse_search_no_hits():
    assert parse_search("<p>No hits found</p>") == ([], None)


def test_gdata_body():
    body = json.loads(gdata_body([(i, "abcdef0123") for i in range(30)]))
    assert body["method"] == "gdata" and body["namespace"] == 1 and len(body["gidlist"]) == 25
    assert body["gidlist"][0] == [0, "abcdef0123"]


def test_parse_gdata_maps_tags():
    g = parse_gdata(fixture_bytes("gdata.json"))[0]
    assert g.kind == "gallery" and g.id == g.album["gid"] * 100000 and g.album["pages"] > 0
    assert all(g.tag_info[t][0] in TAG_KINDS for t in g.tags)


def test_parse_gdata_values():
    gs = parse_gdata(fixture_bytes("gdata.json"))
    assert len(gs) == 3  # the error entry is skipped
    g = gs[0]
    assert g.site == "e-hentai" and g.hash == "" and g.file_url == ""
    assert g.page_url == f"{EH}/g/4348910/0de3268f6d/" and g.preview_url.startswith("https://")
    assert g.rating == "western" and g.score == 39
    assert g.album == {"gid": 4348910, "token": "0de3268f6d", "title": "[ENFman] Holly Pittstein",
                       "pages": 43, "category": "Western"}
    assert g.tag_info["artist:enfman"] == ("artist", 0)
    assert g.tag_info["language:english"] == ("meta", 0)
    assert g.tag_info["female:bbw"] == ("general", 0)


def test_parse_gdata_skips_non_https_thumb():
    body = json.dumps({"gmetadata": [{"gid": 1, "token": "abcdef0123", "title": "t", "category": "Misc",
                                      "thumb": "http://x/y.jpg", "filecount": "1", "rating": "1", "tags": []}]})
    assert parse_gdata(body.encode()) == []


def test_tag_kinds_namespaces():
    body = json.dumps({"gmetadata": [{"gid": 1, "token": "abcdef0123", "title": "t", "category": "Misc",
                                      "thumb": "https://x/y.jpg", "filecount": "1", "rating": "4.5",
                                      "tags": ["group:g", "parody:p", "character:c", "reclass:r",
                                               "cosplayer:z", "mixed:m", "plain", "weird:w"]}]})
    g = parse_gdata(body.encode())[0]
    assert {t: k for t, (k, _) in g.tag_info.items()} == {
        "group:g": "artist", "parody:p": "copyright", "character:c": "character", "reclass:r": "meta",
        "cosplayer:z": "meta", "mixed:m": "general", "plain": "general", "weird:w": "general"}
    assert g.score == 45


def test_gallery_url():
    assert gallery_url(4348914, "5b28fed69e", 1) == f"{EH}/g/4348914/5b28fed69e/?p=1"
    assert gallery_url(4348914, "5b28fed69e", 0) == f"{EH}/g/4348914/5b28fed69e/"


def test_parse_gallery_pages_and_next():
    g = gallery_post()
    pages, nxt = parse_gallery(fixture("gallery.html"), g)
    assert len(pages) == 20 and nxt == 1
    p = pages[3]
    assert p.id == 4348914 * 100000 + 4 and p.kind == "page" and p.site == "e-hentai"
    assert p.page_url == f"{EH}/s/1218b3f3be/4348914-4"
    assert p.file_name == "2.png" and p.ext == "png"
    assert p.preview_url == "https://node2.example.hath.network/c2/111111111111111111/4348914-0.webp"
    assert p.album["sprite"] == (600, 0, 200, 113)
    assert p.album["page"] == 4 and p.album["gid"] == 4348914 and p.album["title"] == g.album["title"]
    assert p.tags == g.tags and p.tag_info == g.tag_info
    assert "sprite" not in g.album  # the gallery's album is not shared/mutated
    pages2, nxt2 = parse_gallery(fixture("gallery_p2.html"), g)
    assert [p.album["page"] for p in pages2] == list(range(21, 41)) and nxt2 == 2


def test_parse_gallery_last_page_and_foreign_links():
    html = fixture("gallery.html")
    html = re.sub(r'<td onclick="[^"]*"><a href="[^"]*\?p=1" onclick="return false">&gt;</a></td>',
                  '<td class="ptdd">&gt;</td>', html)
    html = html.replace("https://e-hentai.org/s/b2e9e709e1/", "https://evil.example/s/b2e9e709e1/")
    pages, nxt = parse_gallery(html, gallery_post())
    assert nxt is None and len(pages) == 19 and pages[0].album["page"] == 2


def test_parse_gallery_content_warning():
    with pytest.raises(GalleryWarning):
        parse_gallery(fixture("warning.html"), gallery_post())


def test_parse_picture_shown_and_original():
    shown, orig = parse_picture(fixture("picture_original.html"))
    assert shown.startswith("https://node1.example.hath.network:31209/h/") and shown.endswith("/2.webp")
    assert orig == f"{EH}/fullimg/4348914/4/0examplekey/2.png"


def test_parse_picture_without_original():
    shown, orig = parse_picture(fixture("picture.html"))
    assert shown.endswith("/2.webp") and orig is None


def test_parse_picture_509():
    with pytest.raises(ImageLimit):
        parse_picture(fixture("picture_509.html"))


def test_parse_picture_no_img():
    with pytest.raises(ValueError):
        parse_picture("<html></html>")


def test_safe_image_url():
    assert safe_image_url("http://81.2.3.4:8080/h/x.jpg")
    for bad in ("http://127.0.0.1/x", "http://192.168.1.2/x", "http://localhost/x", "file:///etc/passwd",
                "http://[::1]/x", "http://169.254.1.1/x", "http://2130706433/", "http://0x7f000001/",
                "http://127.1/", "http://017700000001/", "http://100.64.0.1/", "http://127\uff0e0\uff0e0\uff0e1/x",
                "http://127%2e0.0.1/x", "http://127\uff610\uff610\uff611/x", "http://127\u30020\u30020\u30021/x",
                "http://[::127.0.0.1]/x", "http://[64:ff9b::7f00:1]/x", "http://[::ffff:10.0.0.1]/x",
                "http://[2002:7f00:1::]/x"):
        assert not safe_image_url(bad)
    assert safe_image_url("http://8.8.8.8/x") and safe_image_url("http://[2606:4700::1111]/x")
    assert safe_image_url("http://[64:ff9b::808:808]/x")  # NAT64 of a public IPv4
    assert safe_image_url("https://beef.cafe/x.jpg")  # hex-looking host name is still a name
    assert safe_image_url("https://node1.example.hath.network:31209/h/a/2.webp")
    assert not safe_image_url("http://10.0.0.1/x") and not safe_image_url("http://LOCALHOST./x")


def test_parse_gdata_skips_an_infinite_rating():
    data = json.loads(fixture_bytes("gdata.json"))
    before = len(parse_gdata(json.dumps(data).encode()))
    data["gmetadata"][0]["rating"] = "inf"
    posts = parse_gdata(json.dumps(data).encode())
    assert len(posts) == before - 1
    assert data["gmetadata"][0]["gid"] not in [p.album["gid"] for p in posts]
