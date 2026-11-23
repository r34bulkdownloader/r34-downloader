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

## exhentai.org
- e-hentai.org support is done (1.2.0). exhentai.org (content only shown to logged-in accounts, on its own
  domain) is not supported yet: it would reuse the e-hentai site with the login cookies sent to exhentai.org.

## Ideas
- **Export and import favorites and settings:** one file you can back up or move to another computer.
  - Includes favorites, saved searches, followed tags, the blacklist and preferences.
  - Leaves out the User ID and API key unless you tick "include my login". The download history can be included
    or left out.
  - On import, choose whether to merge with what's there or replace it.
- **Bulk download favorites:** a "Download all" button in the Favorites tab that downloads every favorite (or
  every one matching the filter) in one click, as files, a folder or a ZIP. Already-downloaded favorites are
  skipped unless you choose otherwise. (Today it takes Favorites → All → Download.)
- Sharper thumbnails at large sizes: above ~250 px, load the post's `sample_url` instead of the small preview
  (more data per thumbnail; make it a setting).
- Tag sidecar file: optional `.txt` with the post's tags next to each download.
- Download speed limit / choice of how many downloads run at once.

## Known small issues
- Downloader can announce "finished" twice in a rare nested case (nothing listens to it today).
- Two app instances saving settings at the same moment could clash.
