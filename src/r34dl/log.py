"""App log: last lines in memory (for the in-app viewer) and a small rotating file. Secrets are scrubbed."""
import logging
import os
import sys
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .api import scrub

MAX_LINES = 1000
lines: deque[tuple[int, str]] = deque(maxlen=MAX_LINES)  # (level, formatted line)
count = 0  # lines ever logged; lets the viewer notice new lines once the ring is full


class _ScrubFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


class _MemoryHandler(logging.Handler):
    def emit(self, record):
        global count
        lines.append((record.levelno, self.format(record)))
        count += 1


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        f = super()._open()
        os.chmod(self.baseFilename, 0o600)  # also after each rollover, which recreates the file
        return f


def path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path("~/.local/state").expanduser())
    return Path(base) / "r34-downloader" / "log.txt"


def setup() -> None:
    """Send the app's log records (logger 'r34dl') to memory and to the log file; log uncaught exceptions."""
    root = logging.getLogger("r34dl")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = _ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    mem = _MemoryHandler()
    mem.setFormatter(fmt)
    root.addHandler(mem)
    try:
        p = path()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fh = _PrivateRotatingFileHandler(p, maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("log file unavailable: %s", e)

    def hook(kind, value, tb):  # errors inside Qt slots otherwise only reach an invisible terminal
        root.error("unexpected error", exc_info=(kind, value, tb))
        sys.__excepthook__(kind, value, tb)

    sys.excepthook = hook


def text(errors_only: bool = False) -> str:
    return "\n".join(t for lvl, t in lines if not errors_only or lvl >= logging.WARNING)


def clear() -> None:
    lines.clear()
    try:
        path().write_text("")
        path().with_name(path().name + ".1").unlink(missing_ok=True)  # the rotated older half
    except OSError:
        pass
