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

## Ideas
- Tag sidecar file: optional `.txt` with the post's tags next to each download.

## Known small issues
- Downloader can announce "finished" twice in a rare nested case (nothing listens to it today).
- Two app instances saving settings at the same moment could clash.
