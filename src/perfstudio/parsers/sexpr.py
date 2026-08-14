"""Minimal S-expression tokenizer and parser.

KiCad's netlist, schematic and PCB file formats are all S-expressions: parenthesized
lists of atoms and double-quoted strings. This module knows nothing about KiCad
semantics -- it only turns text into a tree -- so it can be reused for any other
S-expression-based format later.

Ported from packages/parsers/src/sexpr.ts.
"""

from __future__ import annotations

type SExpr = str | list["SExpr"]

_OPEN = "("
_CLOSE = ")"
_QUOTE = '"'
#: A frozenset, not a string: membership on a *string* via `in` is a substring check,
#: under which the empty string (what `_char_at` returns at end-of-input) is trivially
#: "in" every string -- including this one -- which would make `_is_whitespace("")`
#: True and send `_skip_whitespace` into an infinite loop at end-of-input.
_WHITESPACE = frozenset(" \t\n\r\f\v")


class SExprSyntaxError(Exception):
    """Raised when the input is not well-formed S-expression syntax. `offset` is the
    0-indexed character position in the original input where the problem was found, so
    callers can point a user at the exact spot.
    """

    def __init__(self, message: str, offset: int) -> None:
        super().__init__(f"{message} (at offset {offset})")
        self.offset = offset


def _char_at(input_: str, pos: int) -> str:
    """Like JS's `String.charAt`: returns '' (rather than raising) past the end of the
    string. Every boundary check in this module leans on that, exactly as the TS
    original does.
    """
    return input_[pos] if 0 <= pos < len(input_) else ""


def _is_whitespace(ch: str) -> bool:
    return ch in _WHITESPACE


def _is_atom_boundary(ch: str) -> bool:
    """True at end-of-input (charAt returns '') or at any character that ends a bare
    atom.
    """
    return ch in (_OPEN, _CLOSE, _QUOTE) or ch == "" or _is_whitespace(ch)


def _skip_whitespace(input_: str, pos: int) -> int:
    i = pos
    while _is_whitespace(_char_at(input_, i)):
        i += 1
    return i


def _parse_quoted_string(input_: str, start: int) -> tuple[str, int]:
    """Parse a double-quoted string starting at `input_[start]` (which must be `"`).
    Supports the escapes KiCad emits: `\\"`, `\\\\`, `\\n`. Any other backslash escape
    is taken literally (the escaped character is kept as-is). Returns the decoded
    string and the index just past the closing quote.
    """
    i = start + 1
    out: list[str] = []
    while True:
        ch = _char_at(input_, i)
        if ch == "":
            raise SExprSyntaxError("Unterminated string", start)
        if ch == "\\":
            escaped = _char_at(input_, i + 1)
            if escaped == "":
                raise SExprSyntaxError("Unterminated string: dangling escape", i)
            if escaped == '"':
                out.append('"')
            elif escaped == "\\":
                out.append("\\")
            elif escaped == "n":
                out.append("\n")
            else:
                out.append(escaped)
            i += 2
            continue
        if ch == _QUOTE:
            return "".join(out), i + 1
        out.append(ch)
        i += 1


def _parse_form(input_: str, pos: int) -> tuple[SExpr, int]:
    """Parse one form (atom or list) starting at `input_[pos]`. Caller must have
    already skipped leading whitespace and verified `pos` is not past the end of
    input. Returns the parsed node and the index just past it.
    """
    ch = _char_at(input_, pos)

    if ch == _OPEN:
        items: list[SExpr] = []
        i = pos + 1
        while True:
            i = _skip_whitespace(input_, i)
            peek = _char_at(input_, i)
            if peek == "":
                raise SExprSyntaxError('Unbalanced parentheses: missing closing ")"', pos)
            if peek == _CLOSE:
                return items, i + 1
            node, i = _parse_form(input_, i)
            items.append(node)

    if ch == _CLOSE:
        raise SExprSyntaxError('Unbalanced parentheses: unexpected ")"', pos)

    if ch == _QUOTE:
        value, next_pos = _parse_quoted_string(input_, pos)
        return value, next_pos

    # Bare atom: run until the next boundary character.
    i = pos
    while not _is_atom_boundary(_char_at(input_, i)):
        i += 1
    return input_[pos:i], i


def parse_sexpr(input_: str) -> list[SExpr]:
    """Parse zero or more top-level S-expression forms from `input_`. Raises
    `SExprSyntaxError` on unbalanced parentheses or an unterminated string, with the
    character offset of the problem.
    """
    forms: list[SExpr] = []
    i = _skip_whitespace(input_, 0)
    while i < len(input_):
        if _char_at(input_, i) == _CLOSE:
            raise SExprSyntaxError('Unbalanced parentheses: unexpected ")"', i)
        node, next_pos = _parse_form(input_, i)
        forms.append(node)
        i = _skip_whitespace(input_, next_pos)
    return forms
