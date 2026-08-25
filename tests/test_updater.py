"""Tests for the update check as the window performs it (src/perfstudio/ui/updater.py).

``test_updates.py`` covers every decision; this file covers the parts that need Qt --
what the strip says in each of its states, what a window does with an answer, and what it
remembers afterwards.

THE LOAD-BEARING TEST HERE IS test_building_a_window_checks_nothing. This suite builds a
great many windows, and a check that ran from a constructor would put the whole of it on
the network -- slowly, flakily, and against somebody else's rate limit. The mechanism
that prevents it is that no code path from ``MainWindow.__init__`` reaches a checker;
that is asserted rather than trusted, because it is one convenient line away from being
untrue at any time.

Nothing here opens a socket. The checker's signals are emitted by hand, which is exactly
what a reply would do, and the two functions that hand a URL or a folder to the desktop
are replaced -- a test suite that opened a browser would be a test suite people run with
the sound off and the window minimised.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEventLoop
from PySide6.QtWidgets import QApplication, QMessageBox

from perfstudio.commands import create_starter_document
from perfstudio.model import DocumentMeta
from perfstudio.ui import updater
from perfstudio.ui.main import MainWindow
from perfstudio.updates import Asset, Release

MAIN_SOURCE = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "perfstudio" / "ui" / "main.py"
).read_text(encoding="utf-8")

NEXT_RELEASE = Release(
    version="0.8.0",
    tag="v0.8.0",
    url="https://github.com/medinstech/perfstudio/releases/tag/v0.8.0",
    notes="- **Automatic updates.** The installers can now say so.\n",
    assets=(
        Asset(name="PerfStudio_0.8.0_Setup.exe", url="https://example.invalid/s.exe", size=1024),
        Asset(name="SHA256SUMS", url="https://example.invalid/SHA256SUMS", size=200),
    ),
)


@pytest.fixture(scope="session", autouse=True)
def _app():
    app = QApplication.instance() or QApplication(["perfstudio-tests"])
    yield app


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    """The session store, in a temporary file. See test_ui.py's own fixture on why."""
    from PySide6.QtCore import QSettings

    from perfstudio.ui import main as main_module

    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "app_settings", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _nothing_reaches_the_desktop(monkeypatch):
    """Neither a browser nor a file manager opens during a test run."""
    opened: list[str] = []
    monkeypatch.setattr(updater, "open_url", opened.append)
    monkeypatch.setattr(updater, "open_in_file_manager", opened.append)
    from perfstudio.ui import main as main_module

    monkeypatch.setattr(main_module.updater, "open_url", opened.append)
    monkeypatch.setattr(main_module.updater, "open_in_file_manager", opened.append)
    return opened


@pytest.fixture
def window():
    document = create_starter_document(
        DocumentMeta(name="untitled", created="2026-08-25T00:00:00", modified="2026-08-25T00:00:00")
    )
    made = MainWindow(document)
    yield made
    made.close()


# ---------------------------------------------------------------------------
# Nothing happens by itself
# ---------------------------------------------------------------------------


def test_building_a_window_checks_nothing(window) -> None:
    """The one that keeps this suite off the network. See the module docstring."""
    assert window._update_checker is None
    assert not window.update_bar.isVisibleTo(window)


def test_the_daily_check_is_scheduled_by_main_after_the_window_is_shown() -> None:
    """Deferred through the event loop, and from ``main()`` rather than a constructor.

    Read out of the source because the alternative is starting the real application: the
    claim is about WHERE the call is made from, which is exactly what a test that called
    the method itself would stop checking.
    """
    body = MAIN_SOURCE.split("def main() -> int:")[1]
    assert "QTimer.singleShot(UPDATE_CHECK_DELAY_MS, window.consider_checking_for_updates)" in body
    assert "window.show()" in body.split("QTimer.singleShot")[0]


def test_a_refused_check_asks_nobody_anything(window, monkeypatch, settings) -> None:
    updater.remember_preference(settings, False)
    started: list[bool] = []
    monkeypatch.setattr(
        window, "_start_update_check", lambda **kw: started.append(kw["by_hand"])
    )
    window.consider_checking_for_updates()
    assert started == []


def test_a_check_made_this_morning_is_not_made_again_this_afternoon(
    window, monkeypatch, settings
) -> None:
    updater.remember_preference(settings, True)
    updater.remember_check(settings, updater.now_iso())
    started: list[bool] = []
    monkeypatch.setattr(
        window, "_start_update_check", lambda **kw: started.append(kw["by_hand"])
    )
    window.consider_checking_for_updates()
    assert started == []


def test_a_check_that_is_due_runs(window, monkeypatch, settings) -> None:
    updater.remember_preference(settings, True)
    updater.remember_check(settings, "2020-01-01T00:00:00+00:00")
    started: list[bool] = []
    monkeypatch.setattr(
        window, "_start_update_check", lambda **kw: started.append(kw["by_hand"])
    )
    window.consider_checking_for_updates()
    assert started == [False], "an automatic check is not a hand-made one"


# ---------------------------------------------------------------------------
# The first run asks
# ---------------------------------------------------------------------------


def test_nobody_is_checked_up_on_before_being_asked(settings) -> None:
    """Three states, not two: 'not asked yet' is not 'said no'."""
    assert updater.stored_preference(settings) is None
    updater.remember_preference(settings, False)
    assert updater.stored_preference(settings) is False
    updater.remember_preference(settings, True)
    assert updater.stored_preference(settings) is True


def test_the_first_run_asks_and_remembers_a_yes(window, monkeypatch, settings) -> None:
    monkeypatch.setattr(QMessageBox, "exec", lambda box: box.buttons()[0].click())
    assert window._ask_about_updates(settings) is True
    assert updater.stored_preference(settings) is True
    assert window.act_auto_updates.isChecked()


def test_the_first_run_remembers_a_no_and_does_not_ask_again(
    window, monkeypatch, settings
) -> None:
    monkeypatch.setattr(QMessageBox, "exec", lambda box: box.buttons()[1].click())
    assert window._ask_about_updates(settings) is False
    assert updater.stored_preference(settings) is False
    asked: list[int] = []
    monkeypatch.setattr(window, "_ask_about_updates", lambda s: asked.append(1) or False)
    window.consider_checking_for_updates()
    assert asked == [], "the question is asked once, not on every start"


def test_the_menu_toggle_is_the_way_back(window, settings) -> None:
    window.act_auto_updates.setChecked(True)
    assert updater.stored_preference(settings) is True
    window.act_auto_updates.setChecked(False)
    assert updater.stored_preference(settings) is False


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


def test_a_new_release_puts_a_strip_above_the_board(window) -> None:
    window._update_asked_by_hand = False
    window._on_update_checked(NEXT_RELEASE)
    assert window.update_bar.isVisibleTo(window)
    headline = window.update_bar.headline.text()
    assert "0.8.0" in headline
    assert "Automatic updates" in window.update_bar.detail.text(), "the changelog's own words"


def test_nothing_newer_says_so_only_when_somebody_asked(window) -> None:
    window._update_asked_by_hand = True
    window._on_update_checked(None)
    assert "newest" in window.statusBar().currentMessage().lower()
    window.statusBar().clearMessage()
    window._update_asked_by_hand = False
    window._on_update_checked(None)
    assert window.statusBar().currentMessage() == ""
    assert not window.update_bar.isVisibleTo(window)


def test_a_check_that_failed_is_reported_only_to_whoever_asked(window) -> None:
    window._update_asked_by_hand = False
    window._on_update_check_failed("Host api.github.com not found")
    assert window.statusBar().currentMessage() == ""
    window._update_asked_by_hand = True
    window._on_update_check_failed("Host api.github.com not found")
    assert "api.github.com" in window.statusBar().currentMessage()


def test_a_finished_check_is_stamped_but_a_failed_one_is_not(window, settings) -> None:
    """So that a laptop which was offline this morning looks again this afternoon."""
    window._update_asked_by_hand = True
    window._on_update_check_failed("no network")
    assert updater.last_checked(settings) is None
    window._on_update_checked(None)
    assert updater.last_checked(settings) is not None


def test_a_hidden_version_stays_hidden_until_it_is_asked_about(window, settings) -> None:
    updater.remember_skip(settings, "0.8.0")
    window._update_asked_by_hand = False
    window._on_update_checked(NEXT_RELEASE)
    assert not window.update_bar.isVisibleTo(window)
    # From the menu, though, they have just asked about exactly that version.
    window._update_asked_by_hand = True
    window._on_update_checked(NEXT_RELEASE)
    assert window.update_bar.isVisibleTo(window)


def test_hiding_a_release_remembers_only_that_release(window, settings) -> None:
    window._update_asked_by_hand = True
    window._on_update_checked(NEXT_RELEASE)
    window.update_bar.act_hide.click()
    assert updater.skipped_version(settings) == "0.8.0"
    assert not window.update_bar.isVisibleTo(window)
    later = Release(version="0.9.0", tag="v0.9.0", url="x")
    window._update_asked_by_hand = False
    window._on_update_checked(later)
    assert window.update_bar.isVisibleTo(window), "the next release is still announced"


# ---------------------------------------------------------------------------
# The download, and what is offered instead of one
# ---------------------------------------------------------------------------


def test_a_source_install_is_sent_to_the_release_page_rather_than_an_installer(
    window, _nothing_reaches_the_desktop
) -> None:
    """An installer is no use to somebody whose update is ``pip install -U``."""
    assert not getattr(sys, "frozen", False), "the test suite is not a frozen build"
    assert updater.installable_asset(NEXT_RELEASE) is None
    window._update_asked_by_hand = True
    window._on_update_checked(NEXT_RELEASE)
    assert "Download Page" in window.update_bar.act_download.text()
    window.update_bar.act_download.click()
    assert _nothing_reaches_the_desktop == [NEXT_RELEASE.url]


def test_a_packaged_build_is_offered_the_file_for_its_own_machine(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(updater.platform, "machine", lambda: "AMD64")
    asset = updater.installable_asset(NEXT_RELEASE)
    assert asset is not None and asset.name == "PerfStudio_0.8.0_Setup.exe"


def test_the_checksum_file_is_found_by_name() -> None:
    found = updater.checksum_asset(NEXT_RELEASE)
    assert found is not None and found.name == "SHA256SUMS"
    assert updater.checksum_asset(Release(version="1", tag="v1.0.0", url="x")) is None


def test_a_download_lands_where_the_user_can_find_it() -> None:
    """Downloads, not a private cache: somebody has to double-click it afterwards."""
    directory = updater.download_directory()
    assert directory.name != ""


def test_the_finished_file_is_shown_rather_than_run(window, _nothing_reaches_the_desktop) -> None:
    """The last click is the user's. See ui/updater.py on why this stops here."""
    window._on_update_downloaded("/tmp/PerfStudio_0.8.0_Setup.exe", True)
    assert "0.8.0_Setup.exe" in window.update_bar.headline.text()
    window.update_bar.act_reveal.click()
    assert _nothing_reaches_the_desktop == ["/tmp/PerfStudio_0.8.0_Setup.exe"]


# ---------------------------------------------------------------------------
# The strip itself
# ---------------------------------------------------------------------------


def test_the_strip_names_both_versions(window) -> None:
    bar = window.update_bar
    bar.announce(NEXT_RELEASE, "0.7.0", downloadable=True)
    assert "0.8.0" in bar.headline.text() and "0.7.0" in bar.headline.text()
    assert bar.act_download.isVisibleTo(bar) and bar.act_notes.isVisibleTo(bar)
    assert not bar.act_cancel.isVisibleTo(bar)


def test_a_release_with_nothing_to_quote_shows_no_summary_line(window) -> None:
    bar = window.update_bar
    bar.announce(Release(version="0.8.0", tag="v0.8.0", url="x"), "0.7.0", downloadable=True)
    assert bar.detail.text() == ""


def test_a_download_in_progress_offers_only_cancel(window) -> None:
    bar = window.update_bar
    bar.announce(NEXT_RELEASE, "0.7.0", downloadable=True)
    bar.show_progress(50 * 1024 * 1024, 300 * 1024 * 1024)
    assert bar.progress.isVisibleTo(bar)
    assert bar.act_cancel.isVisibleTo(bar)
    assert not bar.act_download.isVisibleTo(bar) and not bar.act_hide.isVisibleTo(bar)
    assert "50" in bar.detail.text() and "300" in bar.detail.text()


def test_an_unverified_download_says_so(window) -> None:
    bar = window.update_bar
    bar.show_downloaded("/tmp/x.exe", False)
    assert "not verified" in bar.detail.text()
    bar.show_downloaded("/tmp/x.exe", True)
    assert "checksum matched" in bar.detail.text()


def test_a_failed_download_keeps_the_notes_button(window) -> None:
    """The one button still worth pressing: the release page has the file on it."""
    bar = window.update_bar
    bar.show_failure("the connection was reset")
    assert bar.isVisibleTo(window)
    assert bar.act_notes.isVisibleTo(bar) and not bar.act_download.isVisibleTo(bar)
    assert "connection was reset" in bar.detail.text()


def test_cancelling_takes_the_strip_away(window) -> None:
    bar = window.update_bar
    bar.announce(NEXT_RELEASE, "0.7.0", downloadable=True)
    bar.act_cancel.click()
    assert not bar.isVisibleTo(window)


# ---------------------------------------------------------------------------
# The transfer itself
#
# Driven over `file://`, which QNetworkAccessManager serves through the same reply
# machinery as https. That reaches every line the real download runs -- the .part file,
# the incremental hash, the checksum comparison, the rename and the deletions -- without
# a socket, a server or three hundred megabytes. What it cannot reach is TLS, and TLS is
# the one part of this nobody here wrote.
# ---------------------------------------------------------------------------

PAYLOAD = b"not really an installer, but it hashes just as well"


def pump(condition, timeout_ms: int = 5000) -> None:
    """Run the event loop until ``condition()`` holds, or fail the test saying it never did."""
    clock = QElapsedTimer()
    clock.start()
    while not condition():
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        assert clock.elapsed() < timeout_ms, "the transfer never finished"


def a_local_release(tmp_path, *, sums: str | None) -> Release:
    """A release whose assets are files on this disk."""
    installer = tmp_path / "PerfStudio_0.8.0_Setup.exe"
    installer.write_bytes(PAYLOAD)
    assets = [Asset(name=installer.name, url=installer.as_uri(), size=len(PAYLOAD))]
    if sums is not None:
        checksums = tmp_path / "SHA256SUMS"
        checksums.write_text(sums, encoding="utf-8")
        assets.append(Asset(name="SHA256SUMS", url=checksums.as_uri(), size=len(sums)))
    return Release(version="0.8.0", tag="v0.8.0", url="x", assets=tuple(assets))


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    directory = tmp_path / "downloads"
    monkeypatch.setattr(updater, "download_directory", lambda: directory)
    return directory


def run_download(release, downloads):
    checker = updater.UpdateChecker()
    outcome: list[tuple] = []
    checker.downloaded.connect(lambda path, verified: outcome.append(("ok", path, verified)))
    checker.downloadFailed.connect(lambda message: outcome.append(("failed", message)))
    checker.download(release, release.assets[0])
    pump(lambda: bool(outcome))
    return outcome[0]


def test_a_verified_download_lands_under_its_own_name(tmp_path, downloads) -> None:
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    release = a_local_release(tmp_path, sums=f"{digest}  PerfStudio_0.8.0_Setup.exe\n")
    outcome = run_download(release, downloads)
    assert outcome[0] == "ok" and outcome[2] is True
    landed = downloads / "PerfStudio_0.8.0_Setup.exe"
    assert pathlib.Path(outcome[1]) == landed
    assert landed.read_bytes() == PAYLOAD
    assert list(downloads.glob("*.part")) == [], "the partial file is renamed, not left behind"


def test_a_release_with_no_checksum_still_downloads_and_says_it_was_not_verified(
    tmp_path, downloads
) -> None:
    """Every release before 0.7.0 is in exactly this state, and its installer still works."""
    outcome = run_download(a_local_release(tmp_path, sums=None), downloads)
    assert outcome[0] == "ok" and outcome[2] is False
    assert (downloads / "PerfStudio_0.8.0_Setup.exe").read_bytes() == PAYLOAD


def test_a_download_that_fails_its_checksum_is_deleted(tmp_path, downloads) -> None:
    """Not kept with a warning beside it.

    A file that fails its checksum is either a truncated download or something this
    project did not build, and neither is a thing to leave in somebody's Downloads folder
    named like an installer.
    """
    release = a_local_release(tmp_path, sums=f"{'0' * 64}  PerfStudio_0.8.0_Setup.exe\n")
    outcome = run_download(release, downloads)
    assert outcome[0] == "failed" and "checksum" in outcome[1]
    assert list(downloads.iterdir()) == [], "neither the file nor its .part survives"


def test_a_checksum_file_that_cannot_be_fetched_does_not_stop_the_download(
    tmp_path, downloads
) -> None:
    """A missing sums file means "unverified", not "refused"."""
    release = a_local_release(tmp_path, sums="")
    gone = tmp_path / "SHA256SUMS"
    gone.unlink()
    outcome = run_download(release, downloads)
    assert outcome[0] == "ok" and outcome[2] is False


def test_an_asset_that_is_not_there_is_reported_rather_than_half_written(
    tmp_path, downloads
) -> None:
    release = Release(
        version="0.8.0",
        tag="v0.8.0",
        url="x",
        assets=(Asset(name="missing.exe", url=(tmp_path / "missing.exe").as_uri()),),
    )
    outcome = run_download(release, downloads)
    assert outcome[0] == "failed"
    assert list(downloads.iterdir()) == []


def test_the_window_shows_what_the_transfer_did(window, tmp_path, downloads) -> None:
    """The signals the checker emits are the ones the strip is wired to."""
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    release = a_local_release(tmp_path, sums=f"{digest}  PerfStudio_0.8.0_Setup.exe\n")
    window._update_release = release
    checker = window._checker()
    finished: list[str] = []
    checker.downloaded.connect(lambda path, verified: finished.append(path))
    checker.download(release, release.assets[0])
    pump(lambda: bool(finished))
    assert window._downloaded_update == finished[0]
    assert "checksum matched" in window.update_bar.detail.text()


def test_closing_the_window_abandons_a_download_in_flight(window, tmp_path, downloads) -> None:
    """And that is what clears the .part file: nothing else is watching for it."""
    release = a_local_release(tmp_path, sums=None)
    checker = window._checker()
    checker.download(release, release.assets[0])
    window.close()
    assert checker._cancelled is True
    pump(lambda: not list(downloads.glob("*.part")) if downloads.exists() else True)
