"""The disk side of crash recovery: one file per window, rewritten on a timer.

``recovery.py`` decides what a record MEANS; this writes them, finds them and deletes them.
The same split as ``updates.py`` against ``ui/updater.py``, and the clock, the filesystem
and the platform's idea of where application data goes all live on this side of it.

**IT WRITES SOMEWHERE ELSE, NEVER BESIDE THE BOARD.** A sidecar in the user's own project
folder would be litter in a directory they curate, would fail on a read-only or network
location, and -- the case that matters most -- has nowhere to go at all for a board that
has never been saved. That is precisely the board with the most to lose, since there is no
file anywhere holding any of it.

**THE WRITE IS ATOMIC.** A record is written to a temporary name and moved into place, so
a crash during the write cannot leave a half-file where the good one was. That is not
theoretical: this file exists because the process stops unexpectedly, so "what if it stops
HERE" is the only question worth asking about every line of it.

**IT NEVER RAISES INTO THE WINDOW.** A full disk, a locked profile or an antivirus holding
the directory must not take somebody's board down with it -- losing the work is the exact
outcome the module exists to prevent, and doing it *from the code meant to prevent it* is
worse than not having the feature. Every operation returns a bool instead, and the window
says so once rather than failing again every thirty seconds in silence.
"""

from __future__ import annotations

import contextlib
import datetime
import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from perfstudio.recovery import (
    RECOVERY_SUFFIX,
    RecoveryRecord,
    format_record,
    is_stale,
    parse_record,
)
from perfstudio.version import __version__

#: How often a modified board is written out. Serializing the largest board in this
#: repository takes 0.6 ms, so the interval is not about cost -- it is the most work a
#: crash can take, and half a minute of routing is an annoyance rather than an evening.
INTERVAL_MS = 30_000

#: Records older than this are deleted on the next start. A record survives only until its
#: board is recovered or discarded, so anything this old belongs to a session whose user
#: has long since decided the work was not worth having.
KEEP_DAYS = 14

#: Guards against reading something enormous that happens to be in the directory. A board
#: is measured in kilobytes; the densest fixture in this repository is 12 KB.
MAX_RECORD_BYTES = 8 * 1024 * 1024


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _iso(moment: datetime.datetime) -> str:
    """The same shape ``main._now_iso`` writes, because the two are compared as text."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def default_directory() -> Path:
    """Where records go, per user and per platform.

    ``GenericDataLocation`` with the name appended by hand rather than ``AppDataLocation``,
    which folds in the application and organisation names -- names this application never
    sets, so the folder it would pick depends on Qt's fallbacks rather than on a decision
    anybody made. ``%APPDATA%``, ``~/.local/share`` and ``~/Library/Application Support``,
    plus one directory of our own, is the same answer on every platform and is one nobody
    has to configure.
    """
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(base or Path.home()) / "PerfStudio" / "recovery"


class Autosave:
    """One window's recovery file.

    Per window rather than per process, because two boards open at once are two lots of
    work and protecting one of them is not a feature. The session id is what keeps them
    apart, and what lets a running window recognise its own record among the ones left
    behind by sessions that did not come back.
    """

    #: Bumped per instance so two windows in one process never share a session id, which
    #: a pid alone would give them.
    _counter = 0

    def __init__(self, directory: Path | None = None) -> None:
        Autosave._counter += 1
        self.directory = directory if directory is not None else default_directory()
        self.session = f"{os.getpid()}-{int(_now().timestamp())}-{Autosave._counter}"
        #: True once a write has failed. The window asks, so it can say so once instead of
        #: failing silently every thirty seconds.
        self.failed = False
        #: Whether there is a record on disk for this session, so a clean state does not
        #: cost a deletion every tick.
        self.written = False

    @property
    def path(self) -> Path:
        return self.directory / f"{self.session}{RECOVERY_SUFFIX}"

    # -- writing -------------------------------------------------------------

    def write(self, document: str, document_path: Path | None) -> bool:
        """Record the document as it stands now. False if it could not be written."""
        record = RecoveryRecord(
            session=self.session,
            document_path=str(document_path) if document_path is not None else None,
            saved_at=_iso(_now()),
            version=__version__,
            document=document,
        )
        temporary = self.path.with_suffix(".tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(format_record(record), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            self.failed = True
            return False
        self.written = True
        return True

    def clear(self) -> None:
        """Forget this session's record. Called on every save and on a clean quit.

        Silent about failure, unlike :meth:`write`. A record that could not be deleted
        costs somebody one question at the next start, which they answer with Discard; a
        record that was never written costs them the board.
        """
        self.written = False
        for path in (self.path, self.path.with_suffix(".tmp")):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    # -- reading -------------------------------------------------------------

    def records(self) -> list[tuple[Path, RecoveryRecord]]:
        """Every readable record from another session, oldest first.

        This session's own is skipped: it describes the window doing the asking, which
        cannot have lost anything yet. Unreadable files are skipped without complaint --
        the commonest one is a record truncated by the very crash it was written for, and
        a half-record is not an error to report, it is a record with nothing in it.
        """
        found: list[tuple[Path, RecoveryRecord]] = []
        try:
            candidates = sorted(self.directory.glob(f"*{RECOVERY_SUFFIX}"))
        except OSError:
            return []
        for path in candidates:
            try:
                if path.stat().st_size > MAX_RECORD_BYTES:
                    continue
                record = parse_record(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if record is None or record.session == self.session:
                continue
            found.append((path, record))
        found.sort(key=lambda item: item[1].saved_at)
        return found

    def prune(self, keep_days: int = KEEP_DAYS) -> int:
        """Delete records older than ``keep_days``. Returns how many went."""
        cutoff = _iso(_now() - datetime.timedelta(days=keep_days))
        removed = 0
        for path, record in self.records():
            if is_stale(record, cutoff):
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed


def disk_state(document_path: str | None) -> tuple[str | None, str | None]:
    """What is at ``document_path`` now: ``(text, modification time)``.

    Both come back ``None`` when there is no readable file there, which is the ordinary
    case for a board that was never saved and the interesting one for a board that has
    since been moved. ``recovery.is_worth_offering`` takes exactly this pair, so the
    reading and the deciding stay on opposite sides of the line the module docstring draws.
    """
    if not document_path:
        return None, None
    path = Path(document_path)
    try:
        text = path.read_text(encoding="utf-8")
        modified = _iso(datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.UTC))
    except (OSError, UnicodeDecodeError):
        return None, None
    return text, modified


__all__ = [
    "INTERVAL_MS",
    "KEEP_DAYS",
    "Autosave",
    "default_directory",
    "disk_state",
]
