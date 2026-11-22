# e-hentai.org support — design

Date: 2026-11-22 · Status: draft for approval

## Why

The user wants to browse e-hentai.org galleries in R34 Downloader, search them with tags, and download whole
albums as well as single pictures. As many existing rule34.xxx features as make sense should work on e-hentai
too. The chosen route is to teach the app about "sites" first, with rule34.xxx as site one and e-hentai as site
two, so that features are shared rather than copied and more boorus become cheap later (see `TODO.md`).

## Goals

1. **Site layer.** Search, autocomplete, post pages and credentials go through a small per-site description
   instead of hard-coded rule34 calls. rule34.xxx keeps working exactly as today.
2. **Site picker** above the tag boxes in each search tab. Each tab searches one site. A new tab starts on the
   last site used.
3. **Search e-hentai galleries** with the include and exclude boxes and the blacklist. The grid shows one
   cover per gallery, with its page count and title.
4. **Open a gallery** in a new tab that shows its pages as a thumbnail grid, with the same ticking, ◀/▶,
   viewer, fullscreen, slideshow and "Open together" as a search tab.
5. **Download** ticked pictures, or a whole gallery as a folder or ZIP, from the search grid (gallery right-click
   menu or ticked galleries) and from the gallery tab. This reuses the existing downloader and ZIP code.
6. **Per-site data.** Download history, favorites, saved searches and followed tags remember which site they
   belong to. Existing data becomes rule34.xxx on upgrade, with no user action needed.
7. **Filename tokens:** `{site}`, plus `{album}` (gallery title), `{album_id}` (gid) and `{page}` (page
   number in the gallery). The default template stays `rule34_{id}_{hash}` for rule34.xxx. e-hentai gets its
   own default template, `{album_id}_{page}` (builder's call on the exact form).
8. **Optional e-hentai login** in Settings: the `ipb_member_id` and `ipb_pass_hash` cookies, stored and
   password-protected the same way as the rule34 User ID and API key (`secret.py`). Without them, everything
   public still works.
9. **Polite and safe:** requests to e-hentai are paced (see Constraints), the app stops and says so plainly
   when the image limit is hit, and a large album download asks first.

## Non-goals (for now)

- exhentai.org, torrents, the e-hentai account favorites stored on the server, comments, and uploading.
- Other boorus such as Gelbooru and Danbooru. The site layer must make them easy to add later, but they are not
  built now.
- Hover preview for e-hentai pages (they are still images, and it would spend the image limit).
- Jumping to page N of an e-hentai search. Its paging is a cursor, so only Previous/Next and infinite scroll
  are offered.

## Anti-goals (failure even if it works)

- Breaking or slowing down anything on rule34.xxx, or losing or garbling existing history, favorites, saved
  searches, settings or the protected login.
- Hammering e-hentai: bursts of requests, retrying through an image limit, or a download that quietly burns a
  whole limit without warning.
- Leaking the e-hentai cookies into logs, error messages, file names or URLs. `scrub()` must cover them.
- Following links from scraped HTML to anything other than https on known e-hentai hosts, and their image
  hosts.

## Constraints and facts about e-hentai

- **Official API:** `POST https://api.e-hentai.org/api.php` (JSON).
  - `gdata`: `{"method":"gdata","gidlist":[[gid,"token"],…],"namespace":1}`, at most 25 galleries per call.
    It returns title, category, thumb (cover URL), uploader, posted, filecount, filesize, rating and tags with
    namespaces, such as `artist:foo` and `female:big breasts`. A bad token comes back as an `error` entry.
  - `gtoken`: maps page tokens to gallery tokens, also 25 per call.
  - Pacing advice from the wiki: 4–5 calls in a row are usually fine, then about 5 s pause.
- **Not in the API, so the HTML has to be scraped. These are the fragile parts and need their own parsers and
  fixture tests:**
  - Search: `https://e-hentai.org/?f_search=<query>`. The next page is a cursor (`next=<gid>` from the
    "Next" link). Collect the `/g/{gid}/{token}/` links from the result page, then get the details with `gdata`.
  - Gallery pages: `https://e-hentai.org/g/{gid}/{token}/?p=N` list `/s/{page_token}/{gid}-{n}` links, with
    thumbnails. In the default view the thumbnails are sprite sheets (one image plus an offset per page), so the
    grid must crop them.
  - Picture page: `/s/{page_token}/{gid}-{n}`. The shown picture is the `<img id="img">` src, a resized copy
    of about 1280 px. When the image limit is exceeded, the site serves a "509" placeholder image instead.
    Detect it and stop.
  - **Which file is saved (the user's decision):** when the e-hentai login is set, downloads save the original
    file through the picture page's "Download original" link (`fullimg.php`, which costs more of the image
    limit). Without a login, or when the page has no such link, downloads save the resized copy. Viewing and
    the slideshow always use the resized copy. The large-download warning's estimate counts originals as
    costing more.
  - Galleries flagged as offensive show a content warning first. Pass `nw=always` (or the `nw=1` cookie) to
    skip it.
- **One request per picture** to find its full-size address. This is resolved lazily, only when the picture is
  viewed, shown in the slideshow or downloaded, and the result is cached on the post.
- **Tag syntax differs:** e-hentai tags contain spaces and namespaces. In the e-hentai tab, one tag per line
  (or comma-separated), and spaces are allowed inside a tag. The query sent is `ns:"tag$"`, with `-` for
  excludes. rule34 keeps its current whitespace splitting.
- **Terms:** check e-hentai's terms on automated access before release. Keep the defaults conservative: one
  e-hentai request at a time, at least about 1 s apart, a pause after a burst, and a backoff on 429/503 that
  reuses `RETRY_DELAYS_MS`.
- **Existing code shape (what this touches):**
  - `api.py`: rule34 URL building and parsing, plus the paced `ApiClient` queue.
  - `models.Post`: rule34 fields; `to_dict` hard-codes `site`.
  - `config.py`: history is a set of ints, favorites are matched by id, credentials are top-level keys.
  - `search.SearchTab`: calls `win.api.fetch(tags, pid, …)` and rule34 paging.
  - `naming.py`: tokens.
  - `downloader.Downloader`: takes `(url, target)` with 3 parallel downloads.
  - `ui.MainWindow`: `_url_ids`, history and favorites keyed by `post.id`.
  - `widgets.TagEdit`: rule34 autocomplete.
  - `viewer.py`, `slideshow.py`, `together.py` and `grid.HoverPreview`: read `post.file_url` directly.
- Stack stays as is: Python ≥ 3.10, PySide6 (QtNetwork for all HTTP), pytest. No new dependencies. HTML is
  parsed with the standard library (`html.parser`, or a careful regex where the markup is tiny).

## Approaches considered

- **A. Site layer first, e-hentai as the second site (chosen).** Every feature is shared and future boorus are
  cheap. The cost is the biggest upfront change, touching `api`, `models`, `config`, `search`, `naming`,
  `downloader` and `ui`.
- **B. Separate e-hentai tabs next to the rule34 ones.** Results would arrive soonest, but tag menus, saved
  searches, favorites, history and the blacklist would all need copies that drift apart. Rejected because it
  goes against "as many features as possible".
- **C. `if site == …` branches inside the existing search tab.** Rejected: it spreads site checks everywhere,
  and history and favorites would mix ids from both sites.

## Decided shape

- **Identity:** every post has a `site` and a key that is unique across sites. For example, rule34 posts stay
  `id` while e-hentai pages are `gid-page`. Everything that is keyed by `post.id` today (history, favorites,
  the grid's thumbnail and downloaded marks, `_url_ids`) is keyed by `(site, key)` instead.
- **Two kinds of e-hentai items in the grid:** *gallery* (a search result, which opens a gallery tab when
  clicked) and *page* (a picture inside a gallery, which opens the viewer). Downloading a gallery item means
  downloading all of its pages.
- **Lazy file address:** an e-hentai page post starts with no `file_url` and with the address of its picture
  page. There is one shared, paced "resolve" step that fills in `file_url`. The viewer, slideshow, "Open
  together" and the downloader call it before they need the file. rule34 posts already have `file_url`, so
  for them resolving is a no-op.
- **Downloader:** items can be resolved just before they start, and the concurrency is per site (rule34 stays
  at 3, e-hentai is 1 with spacing). When the image-limit placeholder appears, the e-hentai queue pauses with
  a clear message, and nothing already downloaded is lost.
- **Tag kinds:** e-hentai namespaces map onto the existing `TAG_KINDS`, so tag menus, the info panel and
  `{artist}`/`{character}` tokens keep working:
  - `artist`, `group` → artist
  - `parody` → copyright
  - `character` → character
  - `female`, `male`, `mixed`, `other` and no namespace → general
  - `language`, `reclass`, `cosplayer` → meta
- **Migration (my call: separate files, so nothing old is touched):** rule34.xxx keeps `history.txt` and
  `favorites.json` exactly as they are. Every other site gets its own files: `history-e-hentai.txt` and
  `favorites-e-hentai.json`. Upgrading needs no migration, and going back to 1.1.0 still works, because the
  old version never sees the e-hentai entries. Saved searches and followed tags are stored per site, in the same
  per-site config section as below. rule34's existing keys stay where they are, so they need no conversion.
- **Per-site settings (my call):** the default Exclude box, the blacklist and the filename template are stored
  per site, because e-hentai's tag syntax is different. rule34's existing keys stay where they are; e-hentai's
  live under a `sites.e-hentai` key in the config. Settings shows a section for each site.
- **Favorites (my call):** both galleries and pages can be favorited. A favorited gallery opens a gallery tab.
  A favorited page stores its picture-page address, never the temporary image address, and looks the image up
  again when it's opened.
- **Thumbnails (my call):** covers, thumbnails and sprite sheets come from the thumbnail CDN (ehgt.org). They
  use the existing thumbnail queue (`MAX_THUMBS` at a time), not the slow page pacing, so the grid fills at
  today's speed. The 1 s pacing applies to the API, the HTML pages and the full-size images.
- **Image hosts (my call):** full-size images come from H@H nodes. These are many hosts, some of them bare IP
  addresses with odd ports, and some on plain http. An image address is accepted only when it comes from a
  picture page we fetched. It may be http or https, but loopback, private and link-local addresses are
  refused, so a hostile page can't make the app call your own network. Scraped links to the search, gallery and
  picture pages must be https on `e-hentai.org`.
- **Large download warning (my call: checked for the whole batch):** the check happens in one shared step
  before anything is queued. If the total number of e-hentai pictures across everything ticked is more than
  200 (builder's call on the exact number), the app asks first, says that it uses the image limit, and gives a
  rough time at the paced rate.

## Left to the builder

- Module layout, for example `sites/` with `rule34.py` and `ehentai.py`, or one file per site. Also the exact
  interface for a site and how `ApiClient` becomes per-site queues.
- How the sprite-sheet thumbnails are cropped and cached.
- The exact e-hentai default filename template, the large-album threshold and the pacing numbers, within the
  constraints above.
- Whether e-hentai autocomplete is worth doing (the API has a `tagsuggest` method, but check it), or whether
  the box just has no suggestions on that site.
- Test fixtures: save real e-hentai HTML and JSON samples, scrubbed, under `tests/fixtures/`. Every parser gets
  fixture tests, and the migration of old history and favorites files gets tests too.
