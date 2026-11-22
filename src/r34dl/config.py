import copy
import json
import os
from pathlib import Path

SLIDESHOW = {  # slideshow options and their defaults
    "seconds": 8,  # per picture (GIFs: at least one full play-through)
    "shuffle": True,
    "videos": True,
    "video_max": 0,  # longest a video plays, in seconds; 0 = the whole video
    "gifs": True,
    "sound": False,
    "loop": True,
}

DEFAULTS = {
    "user_id": "",
    "api_key": "",
    "secret": {},  # password-protected user ID + API key (secret.seal); when set, the two fields above stay empty
    "save_dir": str(Path("~/Downloads/rule34").expanduser()),
    "template": "rule34_{id}_{hash}",
    "exclude": "",
    "start_muted": True,
    "loop_videos": True,
    "hover_preview": True,  # play GIFs/videos in the grid when the mouse rests on them
    "infinite_scroll": True,  # off = one page at a time with Previous/Next
    "thumb_size": 160,
    "theme": "system",  # system / light / dark / black
    "remember_include": False,
    "include": "",
    "saved_searches": {},  # name -> {"include": str, "exclude": str}
    "followed": [],  # followed tags, one-click searches in the Searches menu
    "hide_downloaded": False,
    "blacklist": "",  # tags excluded from every search, same syntax as the tag boxes
    "slideshow": dict(SLIDESHOW),  # last slideshow options; missing or bad values fall back one by one
    "sites": {},  # per-site settings for every site except rule34.xxx (see SITE_DEFAULTS, site_cfg)
    "site": "rule34.xxx",  # the site used last
}

SITE_DEFAULTS = {
    "e-hentai": {"exclude": "", "blacklist": "", "template": "ehentai_{album_id}_{page}",
                 "followed": [], "saved_searches": {}, "member_id": "", "pass_hash": ""},
}

# Keys of the password-protected credentials dict (secret.seal); the first two are rule34.xxx's.
CRED_KEYS = ("user_id", "api_key", "eh_member_id", "eh_pass_hash")


def site_cfg(cfg: dict, site: str) -> dict:
    """The settings dict for a site: cfg itself for rule34.xxx, else cfg["sites"][site] filled with defaults
    (missing or wrong-typed keys fall back one by one). Fills in memory only; load() never writes."""
    if site == "rule34.xxx":
        return cfg
    if not isinstance(cfg.get("sites"), dict):
        cfg["sites"] = {}
    saved = cfg["sites"].get(site)
    saved = saved if isinstance(saved, dict) else {}
    out = cfg["sites"][site] = {**saved}
    for k, v in SITE_DEFAULTS[site].items():
        if not isinstance(out.get(k), type(v)):
            out[k] = copy.deepcopy(v)
    return out


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path("~/.config").expanduser())
    return Path(base) / "r34-downloader" / "config.json"


def _data_file(name: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path("~/.local/share").expanduser())
    return Path(base) / "r34-downloader" / name


def history_path(site: str = "rule34.xxx") -> Path:
    return _data_file("history.txt" if site == "rule34.xxx" else f"history-{site}.txt")


def load_history(site: str = "rule34.xxx") -> set[int]:
    """Post ids downloaded before (one per line; bad lines are ignored)."""
    try:
        lines = history_path(site).read_text().split()
    except OSError:
        return set()
    return {int(x) for x in lines if x.isdigit()}


def add_history(post_id: int, site: str = "rule34.xxx") -> None:
    p = history_path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"{post_id}\n")


def clear_history() -> None:
    for site in ("rule34.xxx", *SITE_DEFAULTS):
        history_path(site).unlink(missing_ok=True)


def load() -> dict:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULTS)
    # A hand-edited value of the wrong type (e.g. "api_key": 5 or null) falls back to the default.
    cfg = {k: data[k] if isinstance(data.get(k), type(v)) else copy.deepcopy(v) for k, v in DEFAULTS.items()}
    cfg["followed"] = [t for t in dict.fromkeys(cfg["followed"]) if isinstance(t, str) and t.strip()]
    cfg["saved_searches"] = {
        str(n): {"include": s.get("include", ""), "exclude": s.get("exclude", "")}
        for n, s in cfg["saved_searches"].items()
        if isinstance(s, dict) and isinstance(s.get("include", ""), str) and isinstance(s.get("exclude", ""), str)
    }
    cfg["slideshow"] = slideshow_options(cfg["slideshow"])
    cfg["sites"] = {k: v for k, v in cfg["sites"].items() if isinstance(v, dict)}
    return cfg


def slideshow_options(saved) -> dict:
    saved = saved if isinstance(saved, dict) else {}
    return {k: saved[k] if type(saved.get(k)) is type(v) else v for k, v in SLIDESHOW.items()}


def _write_private(p: Path, data) -> None:
    """Write JSON atomically (temp file + rename), readable only by the user."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, p)


def save(cfg: dict, unprotect: bool = False) -> None:
    """unprotect: the user just turned password protection off (or forgot it). Without it, a window that never
    saw protection being turned on (e.g. a second copy of the app) can't silently undo it."""
    data = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    if not data.get("secret") and not unprotect:
        try:
            on_disk = json.loads(path().read_text()).get("secret")
        except (OSError, ValueError, AttributeError):
            on_disk = None
        if isinstance(on_disk, dict) and on_disk:
            data["secret"] = on_disk
    if data.get("secret"):  # protected: the readable copies never touch the disk
        data["user_id"] = data["api_key"] = ""
        data["sites"] = copy.deepcopy(data["sites"])
        for site_data in data["sites"].values():
            if isinstance(site_data, dict):
                site_data.update({k: "" for k in ("member_id", "pass_hash") if k in site_data})
    _write_private(path(), data)


def favorites_path(site: str = "rule34.xxx") -> Path:
    return _data_file("favorites.json" if site == "rule34.xxx" else f"favorites-{site}.json")


def load_favorites(site: str = "rule34.xxx") -> list[dict]:
    """Saved favorite posts, newest first (see Post.to_dict). Unreadable file or non-object entries: skipped."""
    try:
        data = json.loads(favorites_path(site).read_text())
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def update_favorites(add: dict | None = None, remove_id: int | None = None, site: str = "rule34.xxx") -> list[dict]:
    """Change the favorites file in one step and return its new contents. Re-reads the file first, so another
    window's changes and entries this version can't read are kept. A file that can't be parsed is renamed to
    favorites.json.bad (not overwritten)."""
    p = favorites_path(site)
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, list):
            raise ValueError("not a list")
    except FileNotFoundError:
        data = []
    except (OSError, ValueError):
        os.replace(p, p.with_name(p.name + ".bad"))
        data = []
    def same(d, post_id):
        return isinstance(d, dict) and str(d.get("id")) == str(post_id)
    if remove_id is not None:
        data = [d for d in data if not same(d, remove_id)]
    if add is not None:
        data = [add] + [d for d in data if not same(d, add["id"])]
    _write_private(p, data)
    return data
