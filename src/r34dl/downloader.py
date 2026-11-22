import itertools
import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .api import request, scrub
from .naming import unique_path
from .sites.ehentai import safe_image_url

log = logging.getLogger(__name__)

CONCURRENCY = 3
RETRY_DELAYS = (1000, 2000, 4000)  # ms


_counter = itertools.count()


class _Item:
    def __init__(self, url: str, target: Path, headers: dict | None = None):
        self.n = next(_counter)
        self.url = url  # as queued: item_done reports this one
        self.fetch_url = url  # where a redirect sent us
        self.headers = headers or {}
        self.original = bool(headers)  # an e-hentai original: its answer may be a limit picture or error page
        self.target = target
        self.retries = 0
        self.reply: QNetworkReply | None = None
        self.file = None
        self.written = 0
        self.error = ""

    @property
    def part(self) -> Path:
        return self.target.with_name(f"{self.target.name}.{self.n}.part")


class Downloader(QObject):
    progress = Signal(int, int, int, int)  # done, total, active, failed
    finished = Signal()
    # One per queued item: url, target path, ok. ok = the file is now on disk (downloaded, or an identical file
    # was already there); not ok = failed for good after retries. Albums track by target, history by url.
    item_done = Signal(str, str, bool)
    # An original came back as e-hentai's image-limit picture or an error page: dropped (no file, no item_done,
    # not in "Retry failed") so the feeder can keep the job and pause.
    limited = Signal(str, str)  # url, target

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._pending: list[_Item] = []
        self._running: list[_Item] = []  # includes items waiting for a retry
        self._failed: list[_Item] = []
        self._done = 0
        self._total = 0
        self._gen = 0

    @property
    def active(self) -> bool:
        return bool(self._pending or self._running)

    def add(self, items):
        """items: (url, target) or (url, target, extra request headers)."""
        for url, target, *headers in items:
            self._pending.append(_Item(url, Path(target), *headers))
            self._total += 1
        self._pump()

    def forget_failed(self, targets_under: Path):
        """Drop failed items that were headed into a folder that no longer exists (a finished/cancelled ZIP)."""
        self._failed = [it for it in self._failed if targets_under not in it.target.parents]
        self._emit()

    def retry_failed(self):
        items, self._failed = self._failed, []
        for it in items:
            it.retries = 0
        self._pending.extend(items)
        self._total += len(items)
        self._pump()

    def cancel(self):
        self._gen += 1
        for it in self._running:
            self._close(it, remove=True)
        self._pending.clear()
        self._running.clear()
        self._done = self._total = 0
        self._emit()
        self.finished.emit()

    def _emit(self):
        self.progress.emit(self._done, self._total, len(self._running), len(self._failed))

    def _pump(self):
        while self._pending and len(self._running) < CONCURRENCY:
            it = self._pending.pop(0)
            self._running.append(it)
            self._start(it)
        self._emit()
        if not self.active:
            self._done = self._total = 0
            self.finished.emit()

    def _start(self, it: _Item):
        try:
            it.target.parent.mkdir(parents=True, exist_ok=True)
            it.file = open(it.part, "wb")
        except OSError as e:
            it.file = None
            it.error = str(e)
            self._error(it)
            return
        it.written = 0
        it.error = ""
        req = request(it.fetch_url)
        for k, v in it.headers.items():
            req.setRawHeader(k, v)
        if it.headers:  # Qt would send these (login cookies) on to whatever host a redirect names
            req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        reply = self._nam.get(req)
        it.reply = reply
        reply.readyRead.connect(lambda it=it: self._on_ready(it))
        reply.finished.connect(lambda it=it: self._on_finished(it))

    def _on_ready(self, it: _Item):
        data = bytes(it.reply.readAll())
        try:
            it.file.write(data)
            it.file.flush()
            it.written += len(data)
        except OSError as e:
            it.error = str(e)
            it.reply.abort()

    def _close(self, it: _Item, remove: bool):
        reply, it.reply = it.reply, None
        if reply is not None:
            reply.readyRead.disconnect()
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        if it.file is not None:
            it.file.close()
            it.file = None
        if remove:
            it.part.unlink(missing_ok=True)

    def _on_finished(self, it: _Item):
        if it.reply is None:
            return
        if it.reply.bytesAvailable():
            self._on_ready(it)
        redirect = it.reply.attribute(QNetworkRequest.RedirectionTargetAttribute)
        if it.headers and redirect is not None and not it.error:
            it.fetch_url, it.headers = it.reply.url().resolved(redirect).toString(), {}
            self._close(it, remove=True)
            if it.fetch_url.endswith("/509.gif"):
                return self._limited(it)
            if not safe_image_url(it.fetch_url):
                it.error, it.retries = "redirected to a refused address", len(RETRY_DELAYS)
                return self._error(it)
            self._start(it)
            return
        err = it.error or (it.reply.errorString() if it.reply.error() != QNetworkReply.NoError else "")
        if err:
            it.error = err
            status = it.reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                it.retries = len(RETRY_DELAYS)  # gone or forbidden: asking again won't help
            self._close(it, remove=True)
            self._error(it)
            return
        ctype = str(it.reply.header(QNetworkRequest.ContentTypeHeader) or "").lower()
        if it.original and ("text/html" in ctype or it.reply.url().path().endswith("/509.gif")):
            self._close(it, remove=True)
            return self._limited(it)
        self._close(it, remove=False)
        try:
            final = unique_path(it.target, it.written)
            if final is None:
                it.part.unlink(missing_ok=True)
            else:
                os.replace(it.part, final)
        except OSError as e:
            it.part.unlink(missing_ok=True)
            it.error = str(e)
            self._error(it)
            return
        self.item_done.emit(it.url, str(it.target), True)
        self._finish(it, failed=False)

    def _error(self, it: _Item):
        if it.retries < len(RETRY_DELAYS):
            log.info("download retry %d: %s: %s", it.retries + 1, scrub(it.url), scrub(it.error))
            delay = RETRY_DELAYS[it.retries]
            it.retries += 1
            gen = self._gen
            QTimer.singleShot(delay, self, lambda: self._retry(it, gen))
            return
        log.warning("download failed: %s: %s", scrub(it.url), scrub(it.error))
        self._finish(it, failed=True)  # first, so a listener's forget_failed() sees this item in the failed list
        self.item_done.emit(it.url, str(it.target), False)

    def _limited(self, it: _Item):
        log.warning("e-hentai sent no picture for %s (image limit or an error page)", scrub(it.url))
        self._running.remove(it)
        self._total -= 1
        self.limited.emit(it.url, str(it.target))
        self._pump()

    def _retry(self, it: _Item, gen: int):
        if gen == self._gen:
            self._start(it)

    def _finish(self, it: _Item, failed: bool):
        self._running.remove(it)
        self._done += 1
        if failed:
            self._failed.append(it)
        self._pump()
