# R34 Downloader

A desktop app for Linux to search rule34.xxx, preview posts (pictures, GIFs and videos), and download them one
at a time or in bulk. It ships as a single AppImage that brings its own Python and Qt, so it runs on any
reasonably recent distribution without installing anything.

**18+ only.** This is a tool for an adult website.

Current version: **1.0.1**

![Search tab: tag boxes on the left, results grid with tick boxes, favorite and downloaded marks](screenshots/search.png)

| Post tab: coloured tag list, zoomable picture | Settings | Slideshow options |
|---|---|---|
| ![Post tab](screenshots/post.png) | ![Settings](screenshots/settings.png) | ![Slideshow options](screenshots/slideshow-options.png) |

*Screenshots use made-up sample posts.*

## Features

### Searching
- **Search tabs:** each tab has its own include and exclude tag boxes, its own results, ticks and paging.
  Open a new one with **＋ New search** (Ctrl+T).
- **Tag boxes:** one tag per line (spaces and commas work too). Inside a tag, use `_` instead of a space
  (`long_hair`). Site extras work: `rating:questionable`, `rating:explicit`, `sort:score`, `score:>=50`.
  Ctrl+Enter searches.
- **Tag suggestions:** both boxes suggest tags as you type, with how many posts each has.
- **Universal blacklist** (Settings): tags left out of every search. A tag you explicitly include wins.
- **Infinite scroll** or **page mode** (Settings): page mode has ◀/▶ page buttons and jump-to-page, and ticks are
  kept across pages. The count line shows which page you're on.
- **★ Searches menu:** saved searches (save, load and delete the current tab's tags) and **followed tags**
  (one-click searches; follow a tag from its right-click menu in a post tab).
- The first tab's exclude list is always remembered. A setting also remembers the include tags.

### The results grid
- Thumbnails with a tick box in the corner. **All / None / Invert**, and Shift+click to tick a range.
- **Size** slider in the toolbar, or Ctrl + mouse wheel over the grid.
- **Hover previews:** GIFs and videos play in place when the mouse rests on them (can be turned off).
- A green **↓** marks posts you downloaded before; a setting hides them from new searches.
- **♥** marks favorites.
- Right-click a thumbnail: open in a tab or background tab, open in browser, download, add to favorites.

### Post tabs
- Click a thumbnail to open the post in its own tab; Ctrl+click or middle-click opens it in the background.
  Nothing else is interrupted.
- **Pictures:** mouse-wheel zoom toward the pointer (up to 400%), drag to move, double-click or **Reset zoom**.
  Zooming past screen size loads a sharper copy.
- **GIFs and videos:** click or Space to pause, a seek bar you can click anywhere on, mute and volume. Settings
  choose whether videos start muted and whether they loop.
- **◀ Previous / Next ▶** (or the arrow keys) step through the search the post came from, loading more results
  at the end.
- **⛶ Fullscreen** (F, or double-click a video or GIF): only the media on black. The controls appear when the
  mouse moves and hide after 2 seconds. Esc or F leaves.
- **Tag list** grouped and coloured like the site (Copyright, Character, Artist, General, Meta) with post counts.
  Click a tag to search it in a new tab; right-click to add it to include/exclude, copy it, or follow it.
- Buttons for **Download**, **♥ Favorite**, **Open in browser**, and a **Selected** tick that mirrors the grid.

### Favorites
- **♥ Favorites** in the toolbar opens your favorites as a grid with the same ticking and downloading.
- Its tag boxes filter your favorites on this computer (no searching the site). `rating:` and `-tag` work, and
  the blacklist applies.

### Slideshow
- **▶ Slideshow** on a search tab or Favorites shows the posts full screen, one after another.
- Options (remembered): seconds per picture, shuffle, include GIFs (each plays through at least once), include
  videos (whole or capped to N seconds), sound, start again at the end, or only the ticked posts.
- Keys: Space pause · ←/→ previous/next · F favorite · D download · Esc leave.
- It keeps fetching the search's next pages by itself, and downloads the next picture ahead so changes are
  instant.
- Fullscreen and the slideshow keep the screen from blanking while open.

### Downloading
- **Download N** downloads the ticked posts. Three downloads run at once, failed ones are retried, and the
  status bar has **Retry failed** and **Cancel**.
- **Download as folder…** or **Download as ZIP…** (the arrow on the Download button) saves the ticked posts as
  one numbered album. The name is suggested from the shared artist.
- **Open together:** the ticked posts side by side in one tab; ◀/▶ in posts opened from it step through just
  those.
- Every download is remembered in a history (the green ↓ mark).

### File names
The name template is set in Settings, with a live preview. The default is `rule34_{id}_{hash}`.
A `/` in the template makes subfolders.

| Token | Meaning |
|---|---|
| `{id}` | post number |
| `{hash}` | the file's hash (md5) |
| `{ext}` | file extension (added automatically at the end) |
| `{tags}` / `{tags:5}` | all tags / the first 5 tags, joined with `_` |
| `{artist}` `{character}` `{copyright}` | from the post's tag types; several are joined with `+` |
| `{search}` | the include tags of the search the post came from |
| `{rating}` `{score}` `{width}` `{height}` | post details |
| `{index}` | position in an album download (001, 002, …) |

Names are cleaned for the file system and shortened if too long; an existing file is never overwritten
(`_1`, `_2`… is added).

### Look and privacy
- **Themes:** System (follows the desktop), Light, Dark, Black (OLED). Applied instantly.
- **Optional master password** for the User ID and API key: encrypted with scrypt + AES-GCM, asked once at
  startup. A forgotten password can't be recovered; you enter the key again from your rule34.xxx account.
- **Log viewer** (Settings → Show log…). API keys are removed from every log line.

## Getting an API key

On rule34.xxx: My Account → Options → **API Access Credentials**. Copy the whole line shown there (it looks
like `&api_key=…&user_id=…`) and paste it into **Paste from site** in the app's Settings, which opens by itself
the first time. The User ID and API key are filled in for you.

## Run

    chmod +x R34-Downloader-x86_64.AppImage
    ./R34-Downloader-x86_64.AppImage

From source (Python 3.10+):

    pip install -e .
    python -m r34dl

## Build the AppImage

    bash packaging/build-appimage.sh

This produces `R34-Downloader-x86_64.AppImage` (x86_64, glibc 2.34 or newer: Ubuntu 22.04+, Debian 12+,
Fedora 35+). It bundles python-build-standalone, PySide6 (Qt 6, including multimedia) and `cryptography`, all
pinned by version and checksum, so the build needs network access. The build checks that every library resolves
inside the AppImage and that the app starts with the host's Qt hidden.

## Tests

    pip install -e '.[dev]'
    python -m pytest

## Where things are stored

All files are readable only by you (mode 0600).

| What | Where |
|---|---|
| Settings (and the credentials, encrypted if a password is set) | `~/.config/r34-downloader/config.json` |
| Download history | `~/.local/share/r34-downloader/history.txt` |
| Favorites | `~/.local/share/r34-downloader/favorites.json` |
| Log | `~/.local/state/r34-downloader/log.txt` |

These follow `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME` and `$XDG_STATE_HOME` when set. Downloads go to
`~/Downloads/rule34` by default (changeable in Settings or with **Choose folder…**).

Without a master password, the User ID and API key are stored in plain text in the settings file.

## Support

If the app is useful to you, you can buy the project a coffee on
[Ko-fi](https://ko-fi.com/r34bulkdownloader). It's entirely optional.

## License

R34 Downloader is free software under the [GNU General Public License v3.0](LICENSE). The AppImage bundles
Python, Qt (PySide6), FFmpeg and a few small libraries under their own licenses; see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

This project isn't affiliated with rule34.xxx. Follow the site's rules and your local laws.

## Plans

See [TODO.md](TODO.md): more themes, other boorus, and e-hentai.org album support.
