"""Export and import: favorites, settings and (optionally) download history and logins in one JSON file."""
import copy
import json
from pathlib import Path

from . import config
from .api import home_short
from .models import Post
from .widgets import expand_dir

FORMAT = "r34-downloader-backup"
VERSION = 1
SITES = ("rule34.xxx", *config.SITE_DEFAULTS)
LOGIN_KEYS = {"rule34.xxx": ("user_id", "api_key"), "e-hentai": ("member_id", "pass_hash")}


def export_data(cfg: dict, include_history: bool, include_login: bool) -> dict:
    """Everything worth moving to another computer. The password-protected copy of the logins never goes in;
    the logins themselves only when asked (they'd be readable in the file)."""
    settings = copy.deepcopy({k: cfg.get(k, v) for k, v in config.DEFAULTS.items()})
    settings.pop("secret", None)
    settings["save_dir"] = home_short(settings["save_dir"])  # "~/…" works on the other computer too
    for site, keys in LOGIN_KEYS.items():
        sc = config.site_cfg(settings, site)
        for k in keys:
            if not include_login:
                sc[k] = ""
    return {
        "format": FORMAT,
        "version": VERSION,
        "settings": settings,
        "favorites": {site: config.load_favorites(site) for site in SITES},
        "history": {site: sorted(config.load_history(site)) for site in SITES} if include_history else None,
    }


def write(path: Path, data: dict) -> None:
    config._write_private(Path(path), data)  # readable only by you: it can hold logins


def read(path: Path) -> dict:
    """The backup in a file; ValueError (with a message for the user) if it isn't one this app can read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"Could not read the file: {e}") from None
    except ValueError:
        raise ValueError("This isn't an R34 Downloader backup (it isn't valid JSON).") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("This isn't an R34 Downloader backup.")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ValueError("This backup was made by a newer version of R34 Downloader. Update the app first.")
    return data


def _favorites(data: dict, site: str) -> list[dict]:
    """The file's favorites for a site that the app can actually show (same checks as its own favorites file)."""
    favs = data.get("favorites")
    entries = favs.get(site) if isinstance(favs, dict) else None
    out = []
    for d in entries if isinstance(entries, list) else []:
        try:
            if isinstance(d, dict) and Post.from_dict(d).site == site:
                out.append(d)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _history(data: dict, site: str) -> set[int] | None:
    h = data.get("history")
    if not isinstance(h, dict) or not isinstance(h.get(site), list):
        return None
    return {i for i in h[site] if isinstance(i, int) and i >= 0}


def _add_lines(mine: str, theirs: str) -> str:
    """Blacklist merge: their tags that aren't already in mine, one per line at the end."""
    have = {t.lower() for t in mine.split()}
    extra = [t for t in dict.fromkeys(theirs.split()) if t.lower() not in have]
    return "\n".join(filter(None, [mine.rstrip(), *extra]))


def import_data(data: dict, cfg: dict, mode: str, protected: bool) -> tuple[dict, list[str]]:
    """Apply a backup: mode "merge" (add the file's favorites, searches, tags and history to yours; keep your
    preferences) or "replace" (take everything from the file). Writes the favorites and history files and
    returns (the new settings to save, notes for the user)."""
    incoming = config.normalize(data.get("settings"))
    notes = []
    if mode == "replace":
        new = incoming
        new["save_dir"] = str(expand_dir(new["save_dir"]))
        new["secret"] = copy.deepcopy(cfg.get("secret", {}))
    else:
        new = copy.deepcopy(cfg)
        for site in SITES:
            mine, theirs = config.site_cfg(new, site), config.site_cfg(incoming, site)
            have = {t.lower() for t in mine["followed"]}
            mine["followed"] += [t for t in theirs["followed"] if t.lower() not in have]
            for name, s in theirs["saved_searches"].items():
                mine["saved_searches"].setdefault(name, s)
            mine["blacklist"] = _add_lines(mine["blacklist"], theirs["blacklist"])
    for site, keys in LOGIN_KEYS.items():  # logins: only a login the file actually has, never over a protected one
        theirs = {k: config.site_cfg(incoming, site)[k] for k in keys}  # copied: in "replace", new IS incoming
        mine, before = config.site_cfg(new, site), config.site_cfg(cfg, site)
        has_login = all(theirs.values())
        for k in keys:
            mine[k] = before[k]
        if has_login and protected:
            notes.append(f"The {site} login in the file wasn't imported: your login is password-protected. "
                         "Enter it in Settings if you want to change it.")
        elif has_login and (mode == "replace" or not any(before[k] for k in keys)):
            for k in keys:
                mine[k] = theirs[k]
    for site in SITES:
        theirs = _favorites(data, site)
        if mode == "replace":
            config.write_favorites(theirs, site)
        else:
            mine = config.load_favorites(site)
            have = {str(d.get("id")) for d in mine}
            config.write_favorites(mine + [d for d in theirs if str(d.get("id")) not in have], site)
        h = _history(data, site)
        if h is not None:
            config.write_history(h if mode == "replace" else config.load_history(site) | h, site)
    return new, notes
