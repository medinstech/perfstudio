"""What an autosaved document means, decided without a clock, a disk or Qt.

Same split as ``updates.py`` against ``ui/updater.py``, and for the same reason: every
question worth getting right here is a question about STRINGS, so all of it is reachable
from a test that hands it one. What a record contains, whether one is worth offering back,
which of two versions is newer, what to call it -- none of that needs a filesystem.
``ui/autosave.py`` is the host: the timer, the directory, the writes and the deletions.

**IT PROTECTS WORK AND NEVER RESTORES ANY.** A recovered document is OFFERED; the user
decides, and the file on disk is not touched until they save. This is the shape
``ui/updater.py`` already takes with the download it does not install, and it matters more
here: the thing at risk is the user's own board, and an automatic restore that guessed
wrong would overwrite the good copy with the stale one. There is no way back from that.

**A RECORD'S EXISTENCE MEANS "THERE WAS UNSAVED WORK".** The host writes one only while
the document differs from what is on disk, and deletes it on every save and on a clean
quit. That is what makes the question at startup a simple one -- a record that is still
there is one whose session did not end normally.

Two things still have to be checked rather than assumed, because a crash can land between
a save and the deletion that follows it:

  - the record may be BYTE-IDENTICAL to the file, in which case nothing was lost;
  - the file may be NEWER than the record, in which case the record is the stale copy and
    offering it invites somebody to overwrite good work with old work.

:func:`is_worth_offering` is those two questions and nothing else, so both are visible in
one place instead of spread across the code that happens to call it.

**THE DOCUMENT IS CARRIED VERBATIM.** A record is a small text header, a ``---`` line and
then the serialized document exactly as ``persist.serialize_document`` produced it. Not a
JSON object with the document nested inside: that would mean parsing and re-serializing on
the way back, and this project asserts byte-for-byte round trips (``test_persist.py``) for
good reasons. Recovering has to hand back the bytes that would have been saved.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Extension for a record on disk. Its own rather than ``.perf`` so a recovery file can
#: never be opened by mistake as a board, and so a directory listing says what it is.
RECOVERY_SUFFIX = ".perfrecover"

#: The header's format version, bumped only if an older record needs reading differently.
#: A record nobody can read is simply not offered -- there is no migration to write here,
#: because the whole file is at most one session old by the time anybody looks at it.
RECORD_FORMAT = 1

#: The line between the header and the document. On its own line, and chosen because it
#: cannot appear at the start of a line in the JSON a document serializes to.
SEPARATOR = "---"


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """One autosaved document and where it came from."""

    #: The window that wrote it. Unique per session, so a running window can tell its own
    #: record from one left behind by a session that did not come back.
    session: str
    #: The file this document belongs to, or ``None`` for a board never saved anywhere.
    #: The second case is the one that matters most: a board with no file is a board with
    #: nowhere for the work to have gone.
    document_path: str | None
    #: ISO 8601, stamped by the host. The engine has no clock.
    saved_at: str
    #: The application version that wrote it, for the same reason the guide carries one.
    version: str
    #: The document, exactly as ``persist.serialize_document`` produced it.
    document: str

    @property
    def name(self) -> str:
        """What to call this board in a sentence.

        String work rather than ``Path``: this module has no filesystem, and a path written
        on Windows has to read correctly when the record is inspected somewhere else.
        """
        if not self.document_path:
            return "an unsaved board"
        return self.document_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def format_record(record: RecoveryRecord) -> str:
    """A record as text: header, separator, document."""
    lines = [
        f"PerfStudio recovery {RECORD_FORMAT}",
        f"session: {record.session}",
        f"version: {record.version}",
        f"saved: {record.saved_at}",
    ]
    if record.document_path:
        # Omitted rather than written empty when there is no file, so "never saved" is the
        # absence of a line and cannot be confused with a path that happens to be blank.
        lines.append(f"path: {record.document_path}")
    lines.append(SEPARATOR)
    return "\n".join(lines) + "\n" + record.document


def parse_record(text: str) -> RecoveryRecord | None:
    """Read a record, or ``None`` if this is not one.

    ``None`` for anything unexpected, including a file truncated by the very crash the
    record exists for. A half-written record is not an error to report; it is a record with
    nothing in it, and the only useful response is to ignore it.
    """
    head, separator, document = text.partition("\n" + SEPARATOR + "\n")
    if not separator:
        return None
    lines = head.splitlines()
    if not lines or not lines[0].startswith("PerfStudio recovery "):
        return None
    try:
        if int(lines[0].rsplit(" ", 1)[1]) != RECORD_FORMAT:
            return None
    except (ValueError, IndexError):
        return None

    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, colon, value = line.partition(": ")
        if colon:
            fields[key] = value
    if "session" not in fields or "saved" not in fields:
        return None
    if not document.strip():
        return None
    return RecoveryRecord(
        session=fields["session"],
        document_path=fields.get("path") or None,
        saved_at=fields["saved"],
        version=fields.get("version", ""),
        document=document,
    )


def is_worth_offering(
    record: RecoveryRecord,
    disk_text: str | None,
    disk_modified: str | None = None,
) -> bool:
    """Whether this record has anything the file does not.

    ``disk_text`` is what is at ``record.document_path`` now, or ``None`` if there is no
    file there -- either because the board was never saved or because it has since been
    moved or deleted. ``disk_modified`` is that file's modification time as ISO 8601, in
    the same form as ``saved_at``, so the two compare as text: ISO 8601 sorts
    chronologically, which is most of the reason to use it.

    Two refusals, and they are different failures:

      - IDENTICAL means nothing was lost. The record was written, the user saved, and the
        crash landed in the gap before it was deleted. Offering it would ask somebody about
        a decision that does not exist.
      - OLDER means the record is the stale copy. This is the one that must not be got
        wrong: a user who accepts an older document and presses Ctrl+S has overwritten good
        work with work they had already replaced, and there is nothing to undo it with.
    """
    if disk_text is not None and record.document == disk_text:
        return False
    return disk_modified is None or disk_modified < record.saved_at


def is_stale(record: RecoveryRecord, cutoff: str) -> bool:
    """Whether a record is old enough to throw away, ``cutoff`` being an ISO 8601 instant.

    Kept as a function of two strings so the age policy lives with the host that has the
    clock, and the comparison lives here with everything else that reads a record.
    """
    return record.saved_at < cutoff


__all__ = [
    "RECORD_FORMAT",
    "RECOVERY_SUFFIX",
    "SEPARATOR",
    "RecoveryRecord",
    "format_record",
    "is_stale",
    "is_worth_offering",
    "parse_record",
]
