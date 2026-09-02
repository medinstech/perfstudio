"""The host side of the update check: the network, the disk, the clock and the strip.

``updates.py`` holds every decision -- which release is newer, which file suits this
machine, what the release notes say -- and holds no I/O at all. This module is the other
half: it fetches the feed, downloads the installer, hashes what it wrote, remembers when
it last looked, and puts one dismissible strip above the board. Split that way for the
reason the rest of the engine is: the interesting failures are in the deciding, and a
test should be able to reach them by handing a function a string rather than by standing
up a web server.

WHAT THIS DELIBERATELY DOES NOT DO IS INSTALL ANYTHING. The downloaded file is verified,
put in the user's Downloads folder, and shown to them in their file manager -- and there
it stops. Running an installer on somebody's behalf means elevation on Windows, replacing
a bundle inside /Applications on macOS, and overwriting a running AppImage on Linux;
doing that with an installer nobody has signed (PLAN.md §12, unbought) is the kind of
mechanism that is indistinguishable from malware and has no way back when it goes wrong.
The last click is the user's.

QtNetwork rather than ``urllib``, for two reasons that are both about the frozen build.
Qt uses the platform's own TLS -- Schannel on Windows, the system store on macOS -- so
there is no CA bundle to forget to pack, which is the classic way a PyInstaller build
fails at exactly the point a developer machine cannot reproduce. And the download is
asynchronous without a thread, so a three hundred megabyte installer arrives with a
progress bar and a working Cancel instead of a frozen window.

NOTHING HERE RUNS BY ITSELF. The checker is created when a check is asked for -- from the
Help menu, or once a day from ``main()`` after the window is up -- and never from a
window's constructor. That is what keeps the test suite, which builds a great many
windows, entirely off the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetrics, QResizeEvent
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..updates import (
    CHECKSUM_ASSET,
    RELEASES_API_URL,
    Asset,
    Release,
    asset_for,
    expected_digest,
    highlights,
    newer_release,
    parse_releases,
)
from ..version import __version__
from . import theme
from .i18n import t

#: The session store's keys, kept here rather than in ``main.py`` with the window's own
#: because they are this module's business -- but read and written through a QSettings
#: the caller passes in, so that ``main.app_settings()`` stays the one store and the test
#: suite's temporary one reaches here too.
CHECK_AUTOMATICALLY_KEY = "updates/checkAutomatically"
LAST_CHECKED_KEY = "updates/lastChecked"
SKIPPED_VERSION_KEY = "updates/skippedVersion"

#: No bytes for this long and the transfer is over. Qt applies it per transfer as an
#: inactivity timeout rather than a deadline, so a slow download is not punished for
#: being slow -- only a dead one is.
TRANSFER_TIMEOUT_MS = 30_000


def now_iso() -> str:
    """The current time, stamped by the host. See ``updates.py`` on why not there."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def stored_preference(settings: Any) -> bool | None:
    """Whether automatic checks are on, or ``None`` for "nobody has been asked yet".

    Three states and not two, because the difference matters exactly once: on first run
    the application asks, and it must be able to tell "not yet asked" from "asked, and
    told no". QSettings has no tri-state, and on Windows it is the registry, which does
    not keep a bool a bool -- so this compares the stored text rather than trusting
    ``value(..., type=bool)`` to survive the round trip.
    """
    raw = settings.value(CHECK_AUTOMATICALLY_KEY, None)
    if raw is None:
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def remember_preference(settings: Any, enabled: bool) -> None:
    settings.setValue(CHECK_AUTOMATICALLY_KEY, "true" if enabled else "false")


def last_checked(settings: Any) -> str | None:
    raw = settings.value(LAST_CHECKED_KEY, None)
    return None if raw is None else str(raw)


def remember_check(settings: Any, when: str) -> None:
    settings.setValue(LAST_CHECKED_KEY, when)


def skipped_version(settings: Any) -> str:
    """The version the user pressed Hide on, which is not offered again.

    Only the version, not a flag: dismissing 0.8.0 says nothing about 0.9.0, and an
    application that takes one Hide as "never mention updates again" is one that has
    quietly stopped doing the job it was switched on to do.
    """
    raw = settings.value(SKIPPED_VERSION_KEY, "")
    return "" if raw is None else str(raw)


def remember_skip(settings: Any, version: str) -> None:
    settings.setValue(SKIPPED_VERSION_KEY, version)


def download_directory() -> Path:
    """Where a downloaded installer lands: the user's own Downloads folder.

    Not a private cache. A file somebody has to double-click has to be somewhere they can
    find it again after they close this window, and Downloads is the folder every
    platform's file manager already has a shortcut to.
    """
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if location:
        return Path(location)
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation))


def installable_asset(release: Release) -> Asset | None:
    """The asset for this machine, or ``None`` if this build cannot use one.

    Two ways to get ``None`` and they are one answer to the user -- "here are the notes,
    the download is not for you". Either the project publishes nothing for this platform
    (an Intel Mac), or this is not a packaged build at all: an installer is no use to
    somebody who installed with ``pip``, whose update is ``pip install -U perfstudio``.
    """
    if not getattr(sys, "frozen", False):
        return None
    return asset_for(release, platform=sys.platform, machine=platform.machine())


def checksum_asset(release: Release) -> Asset | None:
    for asset in release.assets:
        if asset.name.strip().lower() == CHECKSUM_ASSET.lower():
            return asset
    return None


def _request(url: str, *, json: bool = False) -> QNetworkRequest:
    """One outgoing request, with the headers GitHub asks callers for.

    The user agent is not decoration: the API answers 403 to a request without one. It
    names the version so that a release that breaks the check can be told apart in a log
    from one that does not.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", f"PerfStudio/{__version__}".encode())
    if json:
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"X-GitHub-Api-Version", b"2022-11-28")
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
    )
    return request


class UpdateChecker(QObject):
    """Asks GitHub what the newest release is, and fetches it if asked to.

    Every outcome is a signal and none of them is an exception: an update check that
    interrupts somebody's work to report that a network is down has misunderstood its own
    importance. The window shows a failure only for a check the user asked for by hand.
    """

    #: A newer release, or None for "nothing newer" -- both are a finished check.
    checked = Signal(object)
    #: The check could not be made: no network, a captive portal, a rate limit.
    checkFailed = Signal(str)
    downloadProgress = Signal(int, int)
    #: The finished file, and whether its SHA-256 matched the published one.
    downloaded = Signal(str, bool)
    downloadFailed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self._manager.setTransferTimeout(TRANSFER_TIMEOUT_MS)
        self._reply: QNetworkReply | None = None
        self._file: Any = None
        self._hash: Any = None
        self._target: Path | None = None
        self._expected: str | None = None
        self._cancelled = False

    # -- checking ------------------------------------------------------------

    def check(self, current_version: str, *, allow_prerelease: bool = False) -> None:
        reply = self._manager.get(_request(RELEASES_API_URL, json=True))
        reply.finished.connect(
            lambda: self._on_feed(reply, current_version, allow_prerelease)
        )

    def _on_feed(self, reply: QNetworkReply, current: str, allow_prerelease: bool) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.checkFailed.emit(reply.errorString())
            return
        try:
            releases = parse_releases(bytes(reply.readAll().data()).decode("utf-8", "replace"))
        except ValueError as exc:
            self.checkFailed.emit(str(exc))
            return
        self.checked.emit(newer_release(current, releases, allow_prerelease=allow_prerelease))

    # -- downloading ---------------------------------------------------------

    def download(self, release: Release, asset: Asset) -> None:
        """Fetch ``asset`` into the Downloads folder, checksum first if there is one.

        The sums file is a few hundred bytes and the installer a few hundred megabytes,
        so the small one is fetched first: knowing what the hash should be before
        spending the bandwidth means a mismatch is reported as a mismatch rather than as
        "downloaded, and by the way it is wrong".
        """
        self._cancelled = False
        sums = checksum_asset(release)
        if sums is None:
            self._start(asset, expected=None)
            return
        reply = self._manager.get(_request(sums.url))
        reply.finished.connect(lambda: self._on_sums(reply, asset))

    def _on_sums(self, reply: QNetworkReply, asset: Asset) -> None:
        reply.deleteLater()
        if self._cancelled:
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            # A missing checksum file is not a reason to refuse the download; it is a
            # reason to say the download was not verified, which is what None means here.
            self._start(asset, expected=None)
            return
        text = bytes(reply.readAll().data()).decode("utf-8", "replace")
        self._start(asset, expected=expected_digest(text, asset.name))

    def _start(self, asset: Asset, *, expected: str | None) -> None:
        directory = download_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Written under .part and renamed at the end, so that an interrupted download
            # cannot leave something in Downloads that looks like a working installer.
            self._target = directory / asset.name
            self._file = (directory / f"{asset.name}.part").open("wb")
        except OSError as exc:
            self.downloadFailed.emit(str(exc))
            return
        self._hash = hashlib.sha256()
        self._expected = expected
        reply = self._manager.get(_request(asset.url))
        self._reply = reply
        reply.readyRead.connect(lambda: self._on_chunk(reply))
        reply.downloadProgress.connect(self.downloadProgress.emit)
        reply.finished.connect(lambda: self._on_downloaded(reply))

    def _on_chunk(self, reply: QNetworkReply) -> None:
        if self._file is None:
            return
        data = bytes(reply.readAll().data())
        try:
            self._file.write(data)
        except OSError as err:
            # A disk that fills part-way through 300 MB. Every other outcome in this
            # class is a signal; this write was the one place an exception could reach
            # the event loop, with the .part file left behind because nothing cancelled.
            self.cancel()
            self.downloadFailed.emit(f"Could not write the download: {err}")
            return
        self._hash.update(data)

    def _on_downloaded(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        self._reply = None
        partial = self._close_file()
        if self._cancelled:
            self._discard(partial)
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._discard(partial)
            self.downloadFailed.emit(reply.errorString())
            return
        digest = self._hash.hexdigest() if self._hash is not None else ""
        if self._expected is not None and digest != self._expected:
            # Deleted rather than kept with a warning. A file that fails its checksum is
            # either a truncated download or something this project did not build, and
            # neither is a thing to leave sitting in somebody's Downloads folder named
            # like an installer.
            self._discard(partial)
            self.downloadFailed.emit(
                t("The download did not match its published checksum, so it was deleted.")
            )
            return
        target = self._target
        if partial is None or target is None:  # pragma: no cover - defensive
            self.downloadFailed.emit(t("The download could not be saved."))
            return
        try:
            partial.replace(target)
        except OSError as exc:
            self.downloadFailed.emit(str(exc))
            return
        self.downloaded.emit(str(target), self._expected is not None)

    def cancel(self) -> None:
        self._cancelled = True
        if self._reply is not None:
            self._reply.abort()

    def _close_file(self) -> Path | None:
        if self._file is None:
            return None
        name = Path(self._file.name)
        self._file.close()
        self._file = None
        return name

    def _discard(self, partial: Path | None) -> None:
        if partial is None:
            return
        # Suppressed rather than reported: the only realistic failure is the file
        # manager holding the .part file open, and a download that already failed has
        # nothing to gain from a second complaint about its leftovers.
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)


BAR_STYLE = f"""
QFrame#updateBar {{
    background: {theme.PANEL_ALT};
    border-bottom: 1px solid {theme.BORDER};
}}
QFrame#updateBar QLabel#updateHeadline {{ color: {theme.TEXT}; font-weight: 600; }}
QFrame#updateBar QLabel#updateDetail {{ color: {theme.TEXT_DIM}; }}
"""


class ElidingLabel(QLabel):
    """A one-line label that ends in an ellipsis rather than at the edge of its box.

    Both of the strip's lines are as long as somebody else felt like making them: the
    second quotes the release notes, and the first can be the full path a download landed
    at. A QLabel does not elide on its own -- it clips, mid word, with nothing to say it
    has done so, which reads as a rendering fault rather than as "there is more, and the
    button beside me opens it".

    ``text()`` returns what was set rather than what is being shown, so that what the
    strip is CARRYING and what happens to fit in today's window stay separate questions.

    The path case is why the mode is a parameter: eliding a file name from the right
    removes the one part of a path somebody is reading it for.
    """

    def __init__(
        self,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width(), 0)
        shown = QFontMetrics(self.font()).elidedText(self._full, self._mode, width)
        super().setText(shown)


class UpdateBar(QFrame):
    """One strip above the board, from "0.8.0 is out" to "here is the file".

    A strip and not a dialog. An update is news, not a question: a modal box in front of
    a board somebody is in the middle of routing gets dismissed unread, and whichever
    button is under the pointer at the time is the one that gets pressed.
    """

    downloadRequested = Signal()
    notesRequested = Signal()
    dismissed = Signal()
    cancelRequested = Signal()
    #: Close the strip and remember nothing -- for the states where there is no version
    #: to skip: the file is already downloaded, or the download failed.
    closeRequested = Signal()
    revealRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBar")
        self.setStyleSheet(BAR_STYLE)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 7, 8, 7)
        row.setSpacing(10)

        text = QVBoxLayout()
        text.setSpacing(1)
        # Middle for the headline because the long case is a file path, where the name
        # at the end is the half being read; right for the summary, which is prose.
        self.headline = ElidingLabel(Qt.TextElideMode.ElideMiddle)
        self.headline.setObjectName("updateHeadline")
        self.detail = ElidingLabel(Qt.TextElideMode.ElideRight)
        self.detail.setObjectName("updateDetail")
        text.addWidget(self.headline)
        text.addWidget(self.detail)
        row.addLayout(text, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(190)
        self.progress.setTextVisible(False)
        self.progress.hide()
        row.addWidget(self.progress)

        self.act_download = QPushButton()
        self.act_download.clicked.connect(self.downloadRequested)
        self.act_notes = QPushButton(t("What Changed"))
        self.act_notes.setToolTip(t("Open this release's notes on GitHub."))
        self.act_notes.clicked.connect(self.notesRequested)
        self.act_reveal = QPushButton(t("Show the File"))
        self.act_reveal.setToolTip(t("Open the folder the installer was saved in."))
        self.act_reveal.clicked.connect(self.revealRequested)
        self.act_cancel = QPushButton(t("Cancel"))
        self.act_cancel.clicked.connect(self.cancelRequested)
        self.act_hide = QPushButton(t("Hide"))
        self.act_hide.setToolTip(
            t(
                "Stop mentioning this version. The next one will still be announced when "
                "it is released."
            )
        )
        self.act_hide.clicked.connect(self.dismissed)
        # Hide is a decision about the VERSION ("stop mentioning it"), and it was the only
        # way to close the strip after a download had landed -- so closing it told the
        # application never to mention the release the user had just fetched. Close is
        # a decision about the strip.
        self.act_close = QPushButton(t("Close"))
        self.act_close.clicked.connect(self.closeRequested)
        for button in (
            self.act_download,
            self.act_notes,
            self.act_reveal,
            self.act_cancel,
            self.act_hide,
            self.act_close,
        ):
            row.addWidget(button)

    # -- states --------------------------------------------------------------

    def _show_buttons(self, *visible: QPushButton) -> None:
        for button in (
            self.act_download,
            self.act_notes,
            self.act_reveal,
            self.act_cancel,
            self.act_hide,
            self.act_close,
        ):
            button.setVisible(button in visible)

    def announce(self, release: Release, current: str, *, downloadable: bool) -> None:
        """Name the release, quote its own summary of itself, offer the one useful button."""
        self.headline.setText(
            t("PerfStudio {new} is available — you have {old}.").format(
                new=release.version, old=current
            )
        )
        summary = highlights(release.notes)
        self.detail.setText(" · ".join(summary) if summary else "")
        self.detail.setVisible(bool(summary))
        if downloadable:
            self.act_download.setText(t("Download"))
            self.act_download.setToolTip(
                t("Fetch the installer into your Downloads folder and check it arrived intact.")
            )
        else:
            # Not a packaged build, or a platform this project does not build for. The
            # release page is where both of those are answered, so the button goes there
            # rather than pretending there is a file to fetch.
            self.act_download.setText(t("Open the Download Page"))
            self.act_download.setToolTip(
                t(
                    "There is no installer for this build. If you installed with pip, the "
                    "update is “pip install -U perfstudio”; the release page carries "
                    "everything else."
                )
            )
        self.progress.hide()
        self._show_buttons(self.act_download, self.act_notes, self.act_hide)
        self.show()

    def show_progress(self, received: int, total: int) -> None:
        self.progress.setRange(0, max(total, 0))
        self.progress.setValue(max(received, 0))
        self.progress.show()
        megabytes = received / (1024 * 1024)
        if total > 0:
            self.detail.setText(
                t("Downloading… {done:.0f} of {all:.0f} MB").format(
                    done=megabytes, all=total / (1024 * 1024)
                )
            )
        else:
            self.detail.setText(t("Downloading… {done:.0f} MB").format(done=megabytes))
        self.detail.show()
        self._show_buttons(self.act_cancel)

    def show_downloaded(self, path: str, verified: bool) -> None:
        self.headline.setText(t("Downloaded to {path}").format(path=path))
        self.detail.setText(
            t("Its checksum matched. Close PerfStudio before you run it.")
            if verified
            else t("This release published no checksum, so the file was not verified.")
        )
        self.detail.show()
        self.progress.hide()
        self._show_buttons(self.act_reveal, self.act_close)
        self.show()

    def show_failure(self, message: str) -> None:
        self.headline.setText(t("The update could not be downloaded."))
        self.detail.setText(message)
        self.detail.show()
        self.progress.hide()
        self._show_buttons(self.act_notes, self.act_close)
        self.show()

    def dismiss(self) -> None:
        self.hide()


def open_in_file_manager(path: str) -> None:
    """Show a downloaded file where the user can double-click it.

    The containing folder rather than the file: handing the desktop an installer means
    asking the operating system to RUN it, which is the one thing this mechanism does not
    do on somebody's behalf.
    """
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


__all__ = [
    "CHECK_AUTOMATICALLY_KEY",
    "LAST_CHECKED_KEY",
    "SKIPPED_VERSION_KEY",
    "ElidingLabel",
    "UpdateBar",
    "UpdateChecker",
    "checksum_asset",
    "download_directory",
    "installable_asset",
    "last_checked",
    "now_iso",
    "open_in_file_manager",
    "open_url",
    "remember_check",
    "remember_preference",
    "remember_skip",
    "skipped_version",
    "stored_preference",
]
