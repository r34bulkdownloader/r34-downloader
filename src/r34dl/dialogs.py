"""Windows: unlock (master password), log viewer, Settings."""
import logging
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl,
)
from PySide6.QtGui import (
    QDesktopServices, QGuiApplication, QTextCursor,
)
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from . import api, config, log as applog, naming, secret, theme
from .models import Post
from .widgets import TagEdit, expand_dir, hint_label

log = logging.getLogger(__name__)

class UnlockDialog(QDialog):
    """Startup: ask for the password that protects the User ID and API key."""

    FORGOT = 2  # exec() result: the user gave up on the password

    def __init__(self, blob: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Unlock R34 Downloader")
        self._blob = blob
        self.creds: dict | None = None
        self.vault: secret.Vault | None = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Enter your password to unlock your User ID and API key."))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.pw)
        self.msg = QLabel()
        self.msg.setStyleSheet("color: #e5484d")
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        forgot = QPushButton("Forgot password?")
        forgot.clicked.connect(self._forgot)
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.reject)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setDefault(True)
        self.unlock_btn.clicked.connect(self.try_unlock)
        row.addWidget(forgot)
        row.addStretch(1)
        row.addWidget(quit_btn)
        row.addWidget(self.unlock_btn)
        lay.addLayout(row)
        self.setMinimumWidth(420)

    def try_unlock(self):
        try:
            self.creds, self.vault = secret.unlock(self._blob, self.pw.text())
        except secret.WrongPassword:
            self.msg.setText("Wrong password — try again.")
            self.pw.selectAll()
            return
        except secret.BadSecret:
            self.msg.setText("The saved key can't be read (damaged or from another version). "
                             "Use “Forgot password?” to enter your User ID and API key again.")
            self.msg.setWordWrap(True)
            return
        self.pw.clear()  # don't keep the password text around
        self.accept()

    def _forgot(self):
        r = QMessageBox.question(
            self, "Forgot password",
            "Your password can't be recovered. You can remove the protected User ID and API key and enter them "
            "again (they're on your rule34.xxx account's Options page under API Access Credentials).\n\n"
            "Only the saved User ID and API key are removed — searches, favorites and history stay.\n\n"
            "Remove them now?")
        if r == QMessageBox.Yes:
            self.done(self.FORGOT)


class LogDialog(QDialog):
    """Recent app log lines (secrets already scrubbed), for troubleshooting and bug reports."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log")
        self.resize(800, 480)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(["Everything", "Problems only"])
        self.filter.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.filter)
        top.addStretch(1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.view.toPlainText()))
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: (applog.clear(), self.refresh()))
        top.addWidget(copy)
        top.addWidget(clear)
        lay.addLayout(top)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.view, 1)
        lay.addWidget(hint_label(f"Also saved to {api.home_short(str(applog.path()))}. API keys are removed from every line."))
        self._shown = -1
        timer = QTimer(self, interval=1000)
        timer.timeout.connect(self._maybe_refresh)  # live while open
        timer.start()
        self.refresh()

    def _maybe_refresh(self):
        if applog.count != self._shown:
            self.refresh()

    def refresh(self):
        self._shown = applog.count
        text = applog.text(errors_only=self.filter.currentIndex() == 1)
        self.view.setPlainText(text or "Nothing logged yet.")
        self.view.moveCursor(QTextCursor.End)


KOFI_URL = "https://ko-fi.com/r34bulkdownloader"


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, sample: Post | None, search: str = "", parent=None,
                 nam: QNetworkAccessManager | None = None, vault: "secret.Vault | None" = None,
                 sites: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.vault = vault  # the unlocked password this session (secret.Vault); None = not protected/unlocked
        self._theme_before = cfg.get("theme", "system")  # Cancel restores this, whatever OK attempts happened
        self._was_protected = bool(cfg.get("secret"))
        self._changing_password = False
        self._nam = nam or QNetworkAccessManager(self)
        self._cfg = dict(cfg)
        self._search = search
        self._sample = sample or Post(
            12345, "0123456789abcdef0123456789abcdef", "https://example.invalid/img/a.jpg", "", "",
            ["some_artist", "some_character", "some_series", "tag_one"], "explicit", 10, 800, 600,
            tag_info={"some_artist": ("artist", 1), "some_character": ("character", 1),
                      "some_series": ("copyright", 1), "tag_one": ("general", 1)},
        )
        self._eh_sample = Post(
            700001004, "", "", "", "", [], "", 0, 0, 0, site="e-hentai", kind="page", file_name="004.jpg",
            album={"gid": 7, "token": "", "title": "Some Album", "page": 4, "pages": 120, "category": ""})
        outer = QVBoxLayout(self)

        account = QGroupBox("Account")
        form = QFormLayout(account)
        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText("&api_key=…&user_id=…")
        self.paste_edit.setToolTip("Paste the whole line from the site; the User ID and API key are filled in for you")
        self.paste_edit.textChanged.connect(self._pasted_line)
        form.addRow("Paste from site", self.paste_edit)
        self.paste_status = hint_label("")
        self.paste_status.hide()
        form.addRow(self.paste_status)
        self.user_edit = QLineEdit(cfg["user_id"])
        self.user_edit.setPlaceholderText("e.g. 1234567")
        self.key_edit = QLineEdit(cfg["api_key"])
        self.key_edit.setEchoMode(QLineEdit.Password)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.key_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(show)
        form.addRow("User ID", self.user_edit)
        form.addRow("API key", key_row)
        for edit in (self.user_edit, self.key_edit):  # the whole line pasted into one field works too
            edit.textChanged.connect(lambda text, e=edit: self._split_field(e, text))
        form.addRow(hint_label(
            'Log in on rule34.xxx, open <a href="https://rule34.xxx/index.php?page=account&s=options">'
            "My Account → Options</a>, copy the whole line under <b>API Access Credentials</b> (it starts with "
            "<b>&amp;api_key=</b>) and paste it in the first box. They are saved only on this computer.", links=True))
        self.protect_check = QCheckBox("Protect my User ID and API key with a password")
        self.protect_check.setChecked(self._was_protected)
        self.protect_check.toggled.connect(self._update_password_rows)
        self.change_pw_btn = QPushButton("Change password…")
        self.change_pw_btn.clicked.connect(lambda: (setattr(self, "_changing_password", True),
                                                    self._update_password_rows()))
        prot_row = QHBoxLayout()
        prot_row.addWidget(self.protect_check, 1)
        prot_row.addWidget(self.change_pw_btn)
        form.addRow(prot_row)
        self.cur_pw = QLineEdit()
        self.new_pw = QLineEdit()
        self.new_pw2 = QLineEdit()
        for w in (self.cur_pw, self.new_pw, self.new_pw2):
            w.setEchoMode(QLineEdit.Password)
        form.addRow("Current password", self.cur_pw)
        form.addRow("New password", self.new_pw)
        form.addRow("Repeat new password", self.new_pw2)
        self.pw_hint = hint_label(
            "The app will ask for this password when it starts. <b>If you forget it, it can't be recovered</b> — "
            "you'd just enter your User ID and API key again (they're always on your rule34.xxx Options page). "
            "Nothing else is lost.")
        form.addRow(self.pw_hint)
        self._account_form = form
        self._update_password_rows()
        outer.addWidget(account)

        downloads = QGroupBox("Downloads")
        form = QFormLayout(downloads)
        self.dir_edit = QLineEdit(api.home_short(cfg["save_dir"]))  # ~ instead of the login name; expanded on save
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        form.addRow("Save folder", dir_row)
        self.site_box = QComboBox()
        for name in sites or ["rule34.xxx", *config.SITE_DEFAULTS]:
            self.site_box.addItem(name, name)
        form.addRow("Site", self.site_box)
        self._site = self.site_box.currentData()
        self._templates = {}  # site -> template text, so switching sites keeps each one's edits
        self.template_edit = QLineEdit(config.site_cfg(self._cfg, self._site)["template"])
        self.reset_btn = reset = QPushButton("Default")
        reset.clicked.connect(lambda: self.template_edit.setText(self._default_template()))
        reset.setToolTip(f"Reset to {self._default_template()}")
        self.site_box.currentIndexChanged.connect(self._site_changed)
        tpl_row = QHBoxLayout()
        tpl_row.addWidget(self.template_edit, 1)
        tpl_row.addWidget(reset)
        form.addRow("File name", tpl_row)
        form.addRow(hint_label(
            "Build the name from these pieces: <b>{id}</b> post number · <b>{hash}</b> · <b>{tags}</b> all tags · "
            "<b>{tags:5}</b> first 5 tags · <b>{artist}</b> · <b>{character}</b> · <b>{copyright}</b> · "
            "<b>{rating}</b> · <b>{score}</b> · <b>{width}</b> · <b>{height}</b> · <b>{search}</b> your include tags · "
            "<b>{index}</b> position in the download (001, 002…) · <b>{site}</b> · <b>{album}</b> album title · "
            "<b>{album_id}</b> · <b>{page}</b> page number (e-hentai). A <b>/</b> makes a subfolder, e.g. <b>{rating}/{id}</b>. "
            "The file extension is added for you."))
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Example", self.preview)
        self.template_edit.textChanged.connect(self._update_preview)
        self.dir_edit.textChanged.connect(self._update_preview)
        outer.addWidget(downloads)

        blacklist = QGroupBox("Blacklist")
        bll = QVBoxLayout(blacklist)
        self.blacklist_edit = TagEdit(self._nam)
        self.blacklist_edit.setPlainText(cfg.get("blacklist", ""))
        self.blacklist_edit.setPlaceholderText("for example:\nai_generated\nscat")
        self.blacklist_edit.setFixedHeight(80)
        bll.addWidget(self.blacklist_edit)
        bll.addWidget(hint_label(
            "Posts with any of these tags are never shown, in every search tab. Same rules as the tag boxes. "
            "A blacklisted tag you put in a search's Include box is allowed for that search."))
        outer.addWidget(blacklist)

        appearance = QGroupBox("Appearance")
        apl = QHBoxLayout(appearance)
        apl.addWidget(QLabel("Theme"))
        self.theme_box = QComboBox()
        for key, label in theme.THEMES.items():
            self.theme_box.addItem(label, key)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(cfg.get("theme", "system"))))
        self.theme_box.currentIndexChanged.connect(lambda _: theme.apply(self.theme_box.currentData()))  # preview
        apl.addWidget(self.theme_box, 1)
        outer.addWidget(appearance)

        behaviour = QGroupBox("Behaviour")
        bl = QVBoxLayout(behaviour)
        self.remember_check = QCheckBox("Remember include tags between sessions")
        self.remember_check.setChecked(bool(cfg.get("remember_include", False)))
        bl.addWidget(self.remember_check)
        bl.addWidget(hint_label("Exclude tags are always remembered."))
        self.hide_check = QCheckBox("Hide posts I've already downloaded")
        self.hide_check.setChecked(bool(cfg.get("hide_downloaded", False)))
        bl.addWidget(self.hide_check)
        bl.addWidget(hint_label("Downloaded posts get a green ↓ mark. Hiding applies from your next search."))
        clear = QPushButton("Clear download history…")
        clear.clicked.connect(self._clear_history)
        bl.addWidget(clear, 0, Qt.AlignLeft)
        self.infinite_check = QCheckBox("Load more results automatically while scrolling")
        self.infinite_check.setToolTip("Off: one page of 100 at a time, with Previous/Next page buttons. "
                                       "Applies from your next search.")
        self.infinite_check.setChecked(bool(cfg.get("infinite_scroll", True)))
        bl.addWidget(self.infinite_check)
        self.muted_check = QCheckBox("Start videos muted")
        self.muted_check.setChecked(bool(cfg.get("start_muted", True)))
        bl.addWidget(self.muted_check)
        self.hover_check = QCheckBox("Preview GIFs and videos when the mouse rests on a thumbnail")
        self.hover_check.setToolTip("Each preview downloads/streams from the site; turn off on slow connections")
        self.hover_check.setChecked(bool(cfg.get("hover_preview", True)))
        bl.addWidget(self.hover_check)
        self.loop_check = QCheckBox("Loop videos")
        self.loop_check.setChecked(bool(cfg.get("loop_videos", True)))
        bl.addWidget(self.loop_check)
        outer.addWidget(behaviour)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        show_log = self.buttons.addButton("Show log…", QDialogButtonBox.HelpRole)
        show_log.clicked.connect(lambda: LogDialog(self).exec())
        support = self.buttons.addButton("☕ Support on Ko-fi", QDialogButtonBox.HelpRole)
        support.setToolTip(f"Optional: buy the project a coffee ({KOFI_URL})")
        support.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(KOFI_URL)))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.setMinimumWidth(560)

    def showEvent(self, e):
        super().showEvent(e)
        # Wrapped help labels need their height computed for the real width, or the text gets clipped.
        w = max(self.width(), self.minimumWidth())
        self.resize(w, max(self.height(), self.layout().totalHeightForWidth(w)))
        self._update_preview()

    def _fill(self, uid: str | None, key: str | None):
        for edit, value in ((self.user_edit, uid), (self.key_edit, key)):
            if value is not None and edit.text() != value:
                edit.blockSignals(True)
                edit.setText(value)
                edit.blockSignals(False)

    def _pasted_line(self, text: str):
        if not text.strip():
            return
        uid, key = api.parse_credentials(text)
        if uid and key:
            self._fill(uid, key)
            self.paste_edit.blockSignals(True)
            self.paste_edit.clear()  # don't leave the key readable on screen
            self.paste_edit.blockSignals(False)
            self.paste_status.setText("✓ User ID and API key filled in. Press OK to save.")
        else:
            missing = " and ".join(n for n, v in (("api_key=", key), ("user_id=", uid)) if not v)
            self.paste_status.setText(f"That doesn't look like the whole line: no <b>{missing}</b> in it. "
                                      "Copy everything under API Access Credentials.")
        self.paste_status.show()

    def _split_field(self, edit: QLineEdit, text: str):
        """The line (or "api_key=…" / "user_id=…") pasted into the User ID or API key field: keep only the values."""
        if "=" in text:
            self._fill(*api.parse_credentials(text))
            if "=" in edit.text():  # this field's own value wasn't in the text
                edit.blockSignals(True)
                edit.clear()
                edit.blockSignals(False)

    def _update_password_rows(self, *_):
        protect = self.protect_check.isChecked()
        was = self._was_protected
        f = self._account_form
        f.setRowVisible(self.cur_pw, was and (not protect or self._changing_password))
        new = protect and (not was or self._changing_password or self.vault is None)
        f.setRowVisible(self.new_pw, new)
        f.setRowVisible(self.new_pw2, new)
        self.change_pw_btn.setVisible(was and protect and not self._changing_password)
        f.setRowVisible(self.pw_hint, protect)

    def _apply_protection(self) -> bool:
        """Encrypt / re-encrypt / unprotect the credentials in self._cfg. False = stay in the dialog."""
        protect, was = self.protect_check.isChecked(), self._was_protected
        creds = {"user_id": self._cfg["user_id"], "api_key": self._cfg["api_key"]}
        if was and (not protect or self._changing_password):  # turning off or changing: prove it's you
            try:
                secret.unlock(self._cfg["secret"], self.cur_pw.text())
            except secret.WrongPassword:
                QMessageBox.warning(self, "Password", "The current password is wrong.")
                return False
            except secret.BadSecret:
                pass  # unreadable anyway: let the user replace or remove it
        if not protect:
            self._cfg["secret"], self.vault = {}, None
            return True
        if not was or self._changing_password or self.vault is None:
            if len(self.new_pw.text()) < secret.MIN_PASSWORD:
                QMessageBox.warning(self, "Password", f"Use at least {secret.MIN_PASSWORD} characters.")
                return False
            if self.new_pw.text() != self.new_pw2.text():
                QMessageBox.warning(self, "Password", "The two new passwords don't match.")
                return False
            self.vault = secret.new_key(self.new_pw.text())
        self._cfg["secret"] = secret.seal(self.vault, creds)
        return True

    def _clear_history(self):
        if QMessageBox.question(
            self, "Clear download history",
            "Forget which posts you have downloaded? Your downloaded files are not touched.",
        ) == QMessageBox.Yes:
            try:
                config.clear_history()
            except OSError as e:
                QMessageBox.warning(self, "Clear download history", api.scrub(str(e)))

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _default_template(self) -> str:
        return config.DEFAULTS["template"] if self._site == "rule34.xxx" else config.SITE_DEFAULTS[self._site]["template"]

    def _store_template(self):
        self._templates[self._site] = self.template_edit.text()

    def _site_changed(self):
        self._store_template()
        self._site = self.site_box.currentData()
        text = self._templates.get(self._site) or config.site_cfg(self._cfg, self._site)["template"]
        self.reset_btn.setToolTip(f"Reset to {self._default_template()}")
        self.template_edit.setText(text)
        self._update_preview()

    def _sample_for(self, site: str) -> Post:
        return self._sample if site == "rule34.xxx" else self._eh_sample

    def _bad_site(self) -> str | None:
        """The first site whose stored template doesn't render, or None."""
        for site, text in self._templates.items():
            try:
                naming.target_path(Path("."), text, self._sample_for(site), self._search)
            except ValueError:
                return site
        return None

    def _update_preview(self):
        try:
            base = expand_dir(self.dir_edit.text().strip() or ".")
            path = naming.target_path(base, self.template_edit.text(), self._sample_for(self._site), self._search)
            self.preview.setStyleSheet("")
            self.preview.setText(str(path.relative_to(base)))
            ok = True
        except ValueError as e:
            self.preview.setStyleSheet("color: #e5484d")
            self.preview.setText(api.scrub(str(e)))
            ok = False
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def accept(self):
        self._store_template()
        if (bad := self._bad_site()) is not None:
            self.site_box.setCurrentIndex(self.site_box.findData(bad))  # shows its error in the preview
            QMessageBox.warning(self, "Bad filename template", f"The file name for {bad} is not valid.")
            return
        for site, text in self._templates.items():
            if site != "rule34.xxx":
                config.site_cfg(self._cfg, site)["template"] = text
        self._cfg.update(
            user_id=self.user_edit.text().strip(), api_key=self.key_edit.text().strip(),
            save_dir=str(expand_dir(self.dir_edit.text().strip() or self._cfg["save_dir"])),
            template=self._templates.get("rule34.xxx", self._cfg["template"]), start_muted=self.muted_check.isChecked(), loop_videos=self.loop_check.isChecked(),
            remember_include=self.remember_check.isChecked(), hide_downloaded=self.hide_check.isChecked(),
            blacklist=self.blacklist_edit.toPlainText(), infinite_scroll=self.infinite_check.isChecked(),
            theme=self.theme_box.currentData(), hover_preview=self.hover_check.isChecked(),
        )
        if not self._apply_protection():
            return
        for w in (self.cur_pw, self.new_pw, self.new_pw2):
            w.clear()  # don't keep password text around
        try:  # writes only the encrypted copy of the credentials when protected
            config.save(self._cfg, unprotect=self._was_protected and not self.protect_check.isChecked())
        except OSError as e:
            log.error("could not save settings: %s", e)
            QMessageBox.critical(self, "Could not save settings", api.scrub(str(e)))
            return
        super().accept()

    def reject(self):
        theme.apply(self._theme_before)  # undo the live preview
        super().reject()

    def result_cfg(self) -> dict:
        return dict(self._cfg)
