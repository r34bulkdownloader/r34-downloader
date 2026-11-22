# To do

Plans after 1.0. Nothing here is promised or scheduled.

## Support for other boorus
- Candidates: Gelbooru, Safebooru, Xbooru, Realbooru (same API family as rule34.xxx, so mostly a different base
  URL), then Danbooru and e621 (different APIs and login rules; e621 requires its own User-Agent format).
- Each site = a small description: API URL and paging, how to read a post, autocomplete URL, post page URL,
  which credentials it needs. Sites in the same family share code.
- UI: a site picker above the tag boxes; credentials per site in Settings.
- Things that change underneath:
  - Download history, favorites, saved searches and followed tags must remember **which site** a post or search
    belongs to (ids clash between sites). Existing history becomes "rule34.xxx" when upgrading.
  - New filename token `{site}`; default template stays `rule34_{id}_{hash}` for rule34.xxx.
  - The rate limit applies per site.
- Favorites already record `site: "rule34.xxx"`, so they need no migration.

## Support for e-hentai.org (albums)
- Works like the rest of the app, but its unit is an **album** (a "gallery") instead of a single post:
  - **Search albums:** the same include/exclude tag boxes. E-hentai tags have namespaces (`artist:`, `parody:`,
    `character:`, `female:`, `language:`...); the grid shows one cover thumbnail per album, with its page count.
  - **Open an album:** a new tab with the album's pages as a thumbnail grid, using the same ticking, ◀/▶ steps,
    fullscreen and slideshow as a search.
  - **Download:** ticked pictures, or the whole album as a folder or ZIP, reusing the existing album download.
- Underneath, it's a different kind of site from the boorus:
  - There's a small official JSON API (`api.e-hentai.org`, "gdata") for album details and tags. Searching and the
    page lists come from the normal HTML pages, so they have to be parsed and can break when the site changes.
  - Each picture's full-size file is found through its own page, so an album costs one request per picture.
    The site limits image views per account and IP ("image limits"), so downloads must be paced, and an album
    download should warn when it's large.
  - Some content is only on exhentai.org and needs a logged-in account (cookies); start with e-hentai.org only.
  - Check the site's terms on automated downloading first, and keep requests slow and polite.
- Builds on the "other boorus" work: site picker, per-site history and favorites, and the `{site}` filename token,
  plus new tokens such as `{album}`, `{page}` and `{album_id}`.

## Ideas
- Sharper thumbnails at large sizes: above ~250 px, load the post's `sample_url` instead of the small preview
  (more data per thumbnail; make it a setting).
- Tag sidecar file: optional `.txt` with the post's tags next to each download.
- Download speed limit / choice of how many downloads run at once.

## Known small issues
- Downloader can announce "finished" twice in a rare nested case (nothing listens to it today).
- Two app instances saving settings at the same moment could clash.
