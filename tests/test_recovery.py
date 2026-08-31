"""Crash recovery: what a record means, and the files it lives in.

Two halves, split the way ``updates.py`` and ``ui/updater.py`` are. ``perfstudio.recovery``
is pure -- no clock, no disk, no Qt -- so every decision it makes is reachable by handing it
a string, which is most of this file. ``perfstudio.ui.autosave`` is the host, and what is
worth testing there is the part that has to survive the thing the feature exists for: the
process stopping in the middle.

THE ASYMMETRY THAT MATTERS. Failing to offer a recovery costs somebody the work since
their last save. Offering the WRONG one costs them the work they already saved, because
they will accept it and press Ctrl+S. So the tests below are lopsided on purpose: there is
one for finding a record and four for refusing to offer a bad one.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from perfstudio.recovery import (
    RECOVERY_SUFFIX,
    RecoveryRecord,
    format_record,
    is_stale,
    is_worth_offering,
    parse_record,
)
from perfstudio.ui.autosave import Autosave, default_directory, disk_state

DOCUMENT = '{\n  "meta": {\n    "name": "board"\n  }\n}\n'


def record(**overrides: object) -> RecoveryRecord:
    fields: dict[str, object] = {
        "session": "1234-99-1",
        "document_path": "/home/sinan/boards/amp.perf",
        "saved_at": "2026-08-31T14:00:00.000Z",
        "version": "0.9.0.dev0",
        "document": DOCUMENT,
    }
    fields.update(overrides)
    return RecoveryRecord(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_a_record_round_trips() -> None:
    original = record()
    parsed = parse_record(format_record(original))
    assert parsed == original


def test_a_board_that_was_never_saved_round_trips_too() -> None:
    """The case with the most to lose: no file anywhere holds any of this board, so the
    record is the only copy. The missing path is an absent line rather than an empty one,
    so it cannot be confused with a path that happens to be blank."""
    original = record(document_path=None)
    text = format_record(original)
    assert "path:" not in text
    assert parse_record(text) == original


def test_the_document_comes_back_byte_for_byte() -> None:
    """The whole reason a record is a header and a separator rather than JSON with the
    document nested inside. This project asserts byte-identical round trips of .perf files
    for reasons written down in test_persist.py, and a recovery that re-serialized on the
    way back would be handing over something subtly different from what was lost.

    The document here contains a line that is exactly the separator, which is what would
    break a naive split.
    """
    awkward = DOCUMENT + "---\nnot the separator, part of the document\n"
    parsed = parse_record(format_record(record(document=awkward)))
    assert parsed is not None
    assert parsed.document == awkward


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nothing like a record",
        "PerfStudio recovery 1\nsession: x\nsaved: y\n",  # no separator
        "PerfStudio recovery 1\nsession: x\n---\n" + DOCUMENT,  # no timestamp
        "PerfStudio recovery 1\nsaved: y\n---\n" + DOCUMENT,  # no session
        "PerfStudio recovery 9\nsession: x\nsaved: y\n---\n" + DOCUMENT,  # a later format
        "PerfStudio recovery x\nsession: x\nsaved: y\n---\n" + DOCUMENT,
        "PerfStudio recovery 1\nsession: x\nsaved: y\n---\n   \n",  # nothing in it
    ],
)
def test_anything_that_is_not_a_record_reads_as_nothing(text: str) -> None:
    """``None``, never a raise. The commonest malformed file here is one truncated by the
    very crash it was written for, and a half-record is not an error to report -- it is a
    record with nothing in it, and the only useful response is to ignore it."""
    assert parse_record(text) is None


def test_a_record_knows_what_to_call_its_board() -> None:
    assert record(document_path="/home/sinan/boards/amp.perf").name == "amp.perf"
    assert record(document_path=r"C:\\Users\\sinan\\amp.perf").name == "amp.perf"
    assert record(document_path=None).name == "an unsaved board"


# ---------------------------------------------------------------------------
# Whether to offer it back
# ---------------------------------------------------------------------------


def test_a_record_the_file_does_not_have_is_offered() -> None:
    assert is_worth_offering(record(), disk_text="something older", disk_modified=None)


def test_a_board_with_no_file_at_all_is_offered() -> None:
    """Never saved, or saved and since moved. Either way there is nothing on disk holding
    this work, which is the strongest possible reason to ask."""
    assert is_worth_offering(record(document_path=None), disk_text=None, disk_modified=None)


def test_a_record_identical_to_the_file_is_not_offered() -> None:
    """The save landed and the crash beat the deletion to it. Nothing was lost, so there is
    no decision to trouble anybody with."""
    assert not is_worth_offering(record(), disk_text=DOCUMENT, disk_modified=None)


def test_a_record_older_than_the_file_is_not_offered() -> None:
    """THE ONE THAT MUST NOT BE GOT WRONG. A user who is handed an older document and
    presses Ctrl+S has overwritten good work with work they had already replaced, and
    there is nothing to undo it with."""
    assert not is_worth_offering(
        record(saved_at="2026-08-31T14:00:00.000Z"),
        disk_text="a newer board",
        disk_modified="2026-08-31T15:00:00.000Z",
    )


def test_a_record_newer_than_the_file_is_offered() -> None:
    assert is_worth_offering(
        record(saved_at="2026-08-31T15:00:00.000Z"),
        disk_text="an older board",
        disk_modified="2026-08-31T14:00:00.000Z",
    )


def test_staleness_is_a_comparison_of_two_strings() -> None:
    assert is_stale(record(saved_at="2026-08-01T00:00:00.000Z"), "2026-08-15T00:00:00.000Z")
    assert not is_stale(record(saved_at="2026-08-31T00:00:00.000Z"), "2026-08-15T00:00:00.000Z")


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def test_a_written_record_is_found_by_another_session(tmp_path: Path) -> None:
    writer = Autosave(tmp_path)
    assert writer.write(DOCUMENT, Path("/home/sinan/amp.perf"))

    reader = Autosave(tmp_path)
    found = reader.records()
    assert len(found) == 1
    path, saved = found[0]
    assert path.suffix == RECOVERY_SUFFIX
    assert saved.document == DOCUMENT
    assert saved.name == "amp.perf"


def test_a_session_does_not_offer_itself_its_own_record(tmp_path: Path) -> None:
    """A running window cannot have lost anything yet. Without this it would be asked at
    every start whether it wanted to recover the board it already has open."""
    store = Autosave(tmp_path)
    store.write(DOCUMENT, None)
    assert store.records() == []


def test_two_windows_in_one_process_do_not_share_a_record(tmp_path: Path) -> None:
    """Two boards open at once are two lots of work, and protecting one of them is not a
    feature. A process id alone would give them one file between them."""
    first, second = Autosave(tmp_path), Autosave(tmp_path)
    assert first.session != second.session
    first.write("first board\n", None)
    second.write("second board\n", None)
    assert len(list(tmp_path.glob(f"*{RECOVERY_SUFFIX}"))) == 2
    assert [r.document for _, r in first.records()] == ["second board\n"]


def test_nothing_is_left_behind_by_a_write(tmp_path: Path) -> None:
    """The write goes through a temporary name and is moved into place, so a crash during
    it cannot leave a half-file where the good one was. The temporary must not survive a
    successful write either, or the directory fills with them."""
    store = Autosave(tmp_path)
    store.write(DOCUMENT, None)
    assert list(tmp_path.glob("*.tmp")) == []


def test_clearing_removes_the_record(tmp_path: Path) -> None:
    store = Autosave(tmp_path)
    store.write(DOCUMENT, None)
    assert store.written
    store.clear()
    assert not store.written
    assert list(tmp_path.glob(f"*{RECOVERY_SUFFIX}")) == []


def test_a_write_that_cannot_happen_says_so_instead_of_raising(tmp_path: Path) -> None:
    """A full disk or a locked profile must not take somebody's board down WITH the code
    that exists to protect it. The window asks ``failed`` so it can say so once."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("this is a file", encoding="utf-8")

    store = Autosave(blocked)
    assert store.write(DOCUMENT, None) is False
    assert store.failed is True
    assert store.written is False


def test_a_directory_that_is_not_there_reads_as_no_records(tmp_path: Path) -> None:
    assert Autosave(tmp_path / "never-created").records() == []


def test_a_file_that_is_not_a_record_is_stepped_over(tmp_path: Path) -> None:
    (tmp_path / f"junk{RECOVERY_SUFFIX}").write_text("truncated by the cr", encoding="utf-8")
    good = Autosave(tmp_path)
    good.write(DOCUMENT, None)
    assert [r.document for _, r in Autosave(tmp_path).records()] == [DOCUMENT]


def test_pruning_keeps_a_record_from_this_fortnight(tmp_path: Path) -> None:
    store = Autosave(tmp_path)
    store.write(DOCUMENT, None)
    assert Autosave(tmp_path).prune(keep_days=14) == 0
    assert len(Autosave(tmp_path).records()) == 1


def test_pruning_throws_away_a_record_nobody_came_back_for(tmp_path: Path) -> None:
    old = record(saved_at="2001-01-01T00:00:00.000Z", session="ancient")
    (tmp_path / f"ancient{RECOVERY_SUFFIX}").write_text(format_record(old), encoding="utf-8")
    assert Autosave(tmp_path).prune(keep_days=14) == 1
    assert Autosave(tmp_path).records() == []


def test_disk_state_reads_the_file_and_its_time(tmp_path: Path) -> None:
    """The reading half of the decision, kept apart from the deciding half so the two can
    be tested without each other."""
    board = tmp_path / "amp.perf"
    board.write_text(DOCUMENT, encoding="utf-8")
    text, modified = disk_state(str(board))
    assert text == DOCUMENT
    assert modified is not None
    # Same shape as `saved_at`, because they are compared as text.
    datetime.datetime.strptime(modified, "%Y-%m-%dT%H:%M:%S.%fZ")


def test_disk_state_of_a_board_with_no_file_is_nothing(tmp_path: Path) -> None:
    assert disk_state(None) == (None, None)
    assert disk_state(str(tmp_path / "moved-away.perf")) == (None, None)


def test_the_records_directory_is_not_beside_anybodys_board() -> None:
    """It lives in the user's own data directory, per platform.

    A sidecar in the project folder would be litter in a directory somebody curates, would
    fail on a read-only or network location, and -- the case that matters most -- has
    nowhere to go at all for a board that was never saved.
    """
    directory = default_directory()
    assert directory.name == "recovery"
    assert directory.parent.name == "PerfStudio"
