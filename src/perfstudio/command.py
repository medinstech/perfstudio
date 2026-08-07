"""Command bus.

The single rule of this architecture (PLAN.md Sec 8.1): the UI never mutates the
document directly. Every action -- from the GUI, the CLI, the MCP server or a
replayed journal -- is a command that goes through this bus.

Four things fall out of that for free:
  - undo/redo
  - macro recording
  - deterministic replay tests
  - an agent and a user driving the same document concurrently

Core stays deterministic: no time.time(), no random, no I/O in here. Ids are
injected through CommandContext so replays reproduce exactly.

DIVISION OF RESPONSIBILITY (also stated in commands.py, worth repeating here because
it is *why* this bus exists rather than a validation-in-the-UI approach):

  Commands enforce DOCUMENT INTEGRITY -- unique ids, resolvable references, paths on
  the board, the invariants model.py declares. Failing any of these means the result
  is not a document, so the mutation is REFUSED (a handler raises CommandError, which
  dispatch() catches and turns into a structured, non-raising result).

  DRC reports DESIGN QUALITY -- overlaps, proximity risk, current capacity. Those
  describe a legal document you probably do not want, so they are REPORTED elsewhere,
  never refused here. That split is why this module (and commands.py) needs geometry
  but not the footprint library.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from .model import PerfDocument

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

#: Deterministic id source, e.g. next_id('cond') -> 'cond-7'.
NextId = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Host-supplied capabilities a command may need.

    Injected rather than imported so that replaying a journal produces
    byte-identical documents: nothing in a handler may reach for a module-global
    counter, a clock, or any other ambient state.
    """

    next_id: NextId


def create_id_generator(start: int = 0) -> NextId:
    """Deterministic, monotonically increasing id source.

    Each prefix gets its own counter (``cmp-1``, ``cmp-2``, ``cond-1``, ...). The
    counter lives in a closure, never a module global -- two independent generators
    (e.g. one per replay) must not see each other's state, which is what makes
    replay reproducible rather than merely "usually correct".
    """
    counters: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        n = counters.get(prefix, start) + 1
        counters[prefix] = n
        return f"{prefix}-{n}"

    return next_id


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """A command as it travels over the wire and gets written to the journal."""

    type: str
    payload: Any


class CommandError(Exception):
    """Raised by a command handler when the requested change is not valid.

    Carries a machine-readable ``code`` so the MCP server and CLI can report it
    structurally instead of parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


TPayload = TypeVar("TPayload")


@runtime_checkable
class CommandDefinition(Protocol[TPayload]):
    """A registered command type.

    ``apply`` is pure: it returns a new document and must not mutate ``doc``. It
    raises CommandError if the payload is invalid against the current document.

    ``describe`` is a human-readable one-liner. It doubles as the undo-stack label
    and as source material for the soldering guide, so implementations should write
    it in the imperative: "Place R1 at C7".

    An implementation may also define ``validate(payload) -> bool``, checked by
    CommandBus.dispatch before ``apply`` runs (mirrors the optional ``validate?`` on
    the TS interface). None of the standard commands in commands.py need it; it
    exists for command types added later whose payload cannot be trusted to already
    be the right shape (e.g. one deserialised from an external MCP call).
    """

    type: str

    def apply(
        self, doc: PerfDocument, payload: TPayload, ctx: CommandContext
    ) -> PerfDocument: ...

    def describe(self, payload: TPayload, doc: PerfDocument) -> str: ...


# The TS DispatchResult is a discriminated union `{ok: true, ...} | {ok: false, ...}`.
# Python has no structural union like that for dataclasses without extra machinery,
# so DispatchResult below is the single result type; `ok` discriminates which of the
# other fields are meaningful, exactly as callers already do in the TS tests
# (`if (!result.ok) expect(result.code)...`).
@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Result of :meth:`CommandBus.dispatch`. Never raises -- callers branch on ``ok``."""

    ok: bool
    document: PerfDocument | None = None
    description: str = ""
    code: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CommandRegistry:
    """Maps a command's wire-format ``type`` string to its definition."""

    def __init__(self) -> None:
        self._defs: dict[str, CommandDefinition[Any]] = {}

    def register(self, definition: CommandDefinition[Any]) -> CommandRegistry:
        if definition.type in self._defs:
            raise ValueError(f"Duplicate command type: {definition.type}")
        self._defs[definition.type] = definition
        return self

    def get(self, type_: str) -> CommandDefinition[Any] | None:
        return self._defs.get(type_)

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._defs.keys()))


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    record: CommandRecord
    #: Document snapshots. Cheap because documents are immutable and commands
    #: replace only the tuples they touch, so unchanged subtrees are shared by
    #: reference -- this must never become a deep copy.
    before: PerfDocument
    after: PerfDocument
    description: str


#: A bus listener: called with the current document and, for a dispatch/redo, the
#: HistoryEntry that produced it (None for an undo, matching the TS bus's `#emit`).
BusListener = Callable[["PerfDocument", "HistoryEntry | None"], None]


class CommandBus:
    """Dispatches commands, and keeps the undo/redo stacks and journal that fall
    out of routing every mutation through one place."""

    def __init__(
        self,
        initial: PerfDocument,
        registry: CommandRegistry,
        ctx: CommandContext,
    ) -> None:
        self._document = initial
        self._registry = registry
        self._ctx = ctx
        self._undo_stack: list[HistoryEntry] = []
        self._redo_stack: list[HistoryEntry] = []
        self._listeners: list[BusListener] = []

    @property
    def document(self) -> PerfDocument:
        return self._document

    def dispatch(self, type_: str, payload: Any) -> DispatchResult:
        definition = self._registry.get(type_)
        if definition is None:
            return DispatchResult(ok=False, code="unknown-command", message=f"Unknown command: {type_}")

        # `validate` is an optional member of CommandDefinition (mirrors the optional
        # `validate?` on the TS interface). None of the standard commands define one,
        # but the bus still has to honour it for anything that does.
        validate = getattr(definition, "validate", None)
        if validate is not None and not validate(payload):
            return DispatchResult(
                ok=False, code="invalid-payload", message=f"Invalid payload for {type_}"
            )

        before = self._document
        try:
            after = definition.apply(before, payload, self._ctx)
        except CommandError as err:
            return DispatchResult(ok=False, code=err.code, message=err.message)

        description = definition.describe(payload, before)
        entry = HistoryEntry(
            record=CommandRecord(type=type_, payload=payload),
            before=before,
            after=after,
            description=description,
        )

        self._document = after
        self._undo_stack.append(entry)
        self._redo_stack.clear()
        self._emit(entry)

        return DispatchResult(ok=True, document=after, description=description)

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def undo(self) -> PerfDocument:
        if not self._undo_stack:
            return self._document
        entry = self._undo_stack.pop()
        self._redo_stack.append(entry)
        self._document = entry.before
        self._emit(None)
        return self._document

    def redo(self) -> PerfDocument:
        if not self._redo_stack:
            return self._document
        entry = self._redo_stack.pop()
        self._undo_stack.append(entry)
        self._document = entry.after
        self._emit(entry)
        return self._document

    def journal(self) -> tuple[CommandRecord, ...]:
        """The command journal, for macro export and deterministic replay tests."""
        return tuple(e.record for e in self._undo_stack)

    def history(self) -> tuple[str, ...]:
        """Undo-stack labels, newest last. Used by the UI and by guide generation."""
        return tuple(e.description for e in self._undo_stack)

    def subscribe(self, listener: BusListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, entry: HistoryEntry | None) -> None:
        for listener in tuple(self._listeners):
            listener(self._document, entry)


def replay(
    initial: PerfDocument,
    records: Iterable[CommandRecord],
    registry: CommandRegistry,
    ctx: CommandContext,
) -> DispatchResult:
    """Replay a journal onto a fresh document.

    Given the same initial document, the same registry and a fresh id generator,
    this must reproduce the original document exactly -- the property the replay
    tests assert. Stops and surfaces the first failing command rather than
    continuing past it.
    """
    bus = CommandBus(initial, registry, ctx)
    last = DispatchResult(ok=True, document=initial, description="initial")
    for record in records:
        last = bus.dispatch(record.type, record.payload)
        if not last.ok:
            return last
    return last
