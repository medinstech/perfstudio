"""The .perf project file format: turning a PerfDocument into a string, and a string
back into a PerfDocument (or a structured error).

This module is PURE. It performs no filesystem access and no I/O of any kind — it only
turns documents into strings and strings back into documents. Reading the .perf file
from disk and writing it back are the host's job (the desktop app, the CLI, or the MCP
server), never this module's.

Ported from packages/core/src/persist.ts. The format exists to be read by both humans
and agents (PLAN.md Section 9.3), and that drives every decision here:

 - GIT-DIFFABLE. Keys are emitted in a fixed, hand-declared order per object type
   (never Python dict construction order by accident), with 2-space indentation and a
   trailing newline. ``components``, ``conductors`` and ``nets`` are sorted by ``id``;
   ``cuts`` are sorted by hole then ``id``. That sorting exists ONLY for diff
   stability — it carries no semantic meaning, unlike a conductor's ``path``, whose
   order IS meaningful (it is the physical chain of holes) and is therefore never
   reordered. Moving one component should produce a tiny, readable diff.
 - HAND-EDITABLE. Deserialization is forgiving where it safely can be — older format
   versions upgrade through an explicit migration chain, and a solder-trace path with
   a diagonal step loads with a warning instead of locking the user out of their own
   file — and precise where it must not be: every structural error carries a ``path``
   (e.g. "components[3].anchor.col") pointing at the exact offending value.

The wire format is camelCase (``formatVersion``, ``padDiameter``, ``dCol``, ``netId``,
``class`` for net class, ...); the Python model (model.py) is snake_case. This module,
and only this module, owns that mapping — nothing in model.py changes to accommodate
JSON.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .geometry import validate_orthogonal_chain
from .model import (
    DOCUMENT_FORMAT_VERSION,
    VALID_ROTATIONS,
    Board,
    BoardEdge,
    BoardFace,
    BoardLabels,
    BoardMaterial,
    BoardSide,
    BoardType,
    ComponentInstance,
    Conductor,
    DocumentMeta,
    EdgeConnector,
    HoleCoord,
    LeadBendConductor,
    MountingHole,
    Net,
    NetClass,
    NetNode,
    PadAxis,
    PadShape,
    PerfDocument,
    Rotation,
    SolderBuildup,
    SolderTraceConductor,
    SpineSpec,
    StripConductor,
    TrackCut,
    WireConductor,
)

#: Re-exported so callers of persist.py don't also need to import model.py.
CURRENT_FORMAT_VERSION: int = DOCUMENT_FORMAT_VERSION

# ---------------------------------------------------------------------------
# JSON value plumbing
# ---------------------------------------------------------------------------

type JsonPrimitive = str | int | float | bool
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]
type JsonObj = dict[str, JsonValue]

# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeserializeOk:
    document: PerfDocument
    warnings: tuple[str, ...]
    ok: Literal[True] = True


@dataclass(frozen=True, slots=True)
class DeserializeErr:
    code: str
    message: str
    path: str | None = None
    ok: Literal[False] = False


type DeserializeResult = DeserializeOk | DeserializeErr

# ---------------------------------------------------------------------------
# Ordered-object construction
# ---------------------------------------------------------------------------


def _build_ordered(order: tuple[str, ...], values: dict[str, JsonValue]) -> JsonObj:
    """Builds a plain dict with keys inserted in exactly ``order``, skipping any key not
    present in ``values``. This is the mechanism behind every stable-key-order guarantee
    in this file: callers never rely on the shape or insertion order of the input, only
    on this explicit, hand-written tuple. A key that should be omitted (an unset
    optional field) is simply never added to ``values`` by the caller.
    """
    return {key: values[key] for key in order if key in values}


def _num(path: str, n: int | float) -> int | float:
    """Validates and normalizes a number for serialization.

    A naive ``json.dumps`` silently turns NaN and Infinity into the text ``NaN`` /
    ``Infinity`` (which isn't even valid JSON) rather than raising — a real trap, since
    the resulting file would not parse as JSON at all, or worse, a different naive
    encoder might silently write ``null``. This raises instead, with a path pointing at
    the offending field. It also normalizes -0.0 to 0.0: they are numerically identical
    everywhere, but -0.0 is a nuisance under strict/deep-equality checks and has no
    business surviving a round trip through a hand-edited file.
    """
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        raise TypeError(f"Cannot serialize non-numeric value at {path}: {n!r}.")
    if not math.isfinite(n):
        raise ValueError(
            f"Cannot serialize non-finite number at {path}: {n}. "
            f"PerfStudio documents must contain only finite numbers (a naive JSON "
            f"encoder would otherwise silently write invalid or lossy JSON here)."
        )
    if n == 0:
        return 0.0 if isinstance(n, float) else 0
    return n


# ---------------------------------------------------------------------------
# Path helpers, shared by serialize (error paths) and deserialize (error paths)
# ---------------------------------------------------------------------------


def _field_path(parent: str, key: str) -> str:
    return key if parent == "" else f"{parent}.{key}"


def _index_path(parent: str, i: int) -> str:
    return f"{parent}[{i}]"


# ---------------------------------------------------------------------------
# Sorting -- for diff stability only, never semantic
# ---------------------------------------------------------------------------

# Python's default string ``<`` compares by code point, exactly like the ASCII-safe
# `compareStrings` in persist.ts (both avoid locale-dependent ordering). No helper is
# needed here beyond using `.id` / `.at` as a sort key directly.


def _cut_sort_key(c: TrackCut) -> tuple[int, int, str]:
    """Cuts have no other natural ordering, so they sort by hole position (row-major:
    row then col, matching the reading order documented on HoleCoord in model.py) and
    fall back to `id` only to break a tie between two cuts on the same hole. Purely for
    predictable diffs -- cuts are independent of one another.
    """
    return (c.at.row, c.at.col, c.id)


def _mounting_hole_sort_key(m: MountingHole) -> tuple[int, int, str]:
    """Same reasoning as :func:`_cut_sort_key`: mounting holes are independent of one
    another, so they sort by position for a readable diff and fall back to `id`.
    """
    return (m.at.row, m.at.col, m.id)


# ---------------------------------------------------------------------------
# Key order declarations -- the single source of truth for field order
# ---------------------------------------------------------------------------

DOCUMENT_KEY_ORDER: tuple[str, ...] = (
    "formatVersion",
    "meta",
    "board",
    "components",
    "conductors",
    "cuts",
    "mountingHoles",
    "edgeConnectors",
    "nets",
)
META_KEY_ORDER: tuple[str, ...] = ("name", "created", "modified")
BOARD_KEY_ORDER: tuple[str, ...] = (
    "type",
    "cols",
    "rows",
    "pitch",
    "thickness",
    "material",
    "padDiameter",
    "drillDiameter",
    "stripAxis",
    "padShape",
    "padLength",
    "padAxis",
    "borderXMm",
    "borderYMm",
    "singleSided",
    "labels",
)
BOARD_LABELS_KEY_ORDER: tuple[str, ...] = ("face", "rowDigits", "allEdges")
MOUNTING_HOLE_KEY_ORDER: tuple[str, ...] = (
    "id",
    "at",
    "offsetXMm",
    "offsetYMm",
    "diameter",
    "headDiameter",
)
EDGE_CONNECTOR_KEY_ORDER: tuple[str, ...] = (
    "id",
    "edge",
    "start",
    "count",
    "fingerWidth",
    "fingerLength",
    "insetMm",
    "face",
)
HOLE_KEY_ORDER: tuple[str, ...] = ("col", "row")
COMPONENT_KEY_ORDER: tuple[str, ...] = (
    "id",
    "ref",
    "value",
    "footprintId",
    "anchor",
    "rotation",
    "mirrored",
    "locked",
)
CONDUCTOR_KEY_ORDER: tuple[str, ...] = (
    "id",
    "kind",
    "path",
    "side",
    "netId",
    "layerZ",
    "buildup",
    "spine",
    "gaugeAwg",
    "color",
    "componentId",
    "pinNumber",
)
SPINE_KEY_ORDER: tuple[str, ...] = ("material", "gauge")
CUT_KEY_ORDER: tuple[str, ...] = ("id", "at")
NET_KEY_ORDER: tuple[str, ...] = ("id", "name", "nodes", "class", "currentA", "voltageV")
NET_NODE_KEY_ORDER: tuple[str, ...] = ("componentRef", "pin")

# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _ordered_hole(h: HoleCoord, path: str) -> JsonObj:
    return _build_ordered(
        HOLE_KEY_ORDER,
        {
            "col": _num(_field_path(path, "col"), h.col),
            "row": _num(_field_path(path, "row"), h.row),
        },
    )


def _ordered_board(b: Board) -> JsonObj:
    path = "board"
    values: dict[str, JsonValue] = {
        "type": b.type,
        "cols": _num(_field_path(path, "cols"), b.cols),
        "rows": _num(_field_path(path, "rows"), b.rows),
        "pitch": _num(_field_path(path, "pitch"), b.pitch),
        "thickness": _num(_field_path(path, "thickness"), b.thickness),
        "material": b.material,
        "padDiameter": _num(_field_path(path, "padDiameter"), b.pad_diameter),
        "drillDiameter": _num(_field_path(path, "drillDiameter"), b.drill_diameter),
    }
    if b.strip_axis is not None:
        values["stripAxis"] = b.strip_axis
    # OMITTED AT THEIR DEFAULT, exactly as `stripAxis` is, and for a reason that is not
    # cosmetic: a board that uses none of these features must serialize to the same bytes
    # a build predating them wrote, or every golden fixture stops round-tripping and the
    # differential proof goes with it. It is also what lets the document format version
    # stay at 1 -- an older build reads such a file unchanged, so there is nothing to
    # migrate. The test is equality with the DEFAULT, never "is this meaningful", so a
    # pad axis set on a round-pad board still survives the trip.
    if b.pad_shape != "round":
        values["padShape"] = b.pad_shape
    if b.pad_length is not None:
        values["padLength"] = _num(_field_path(path, "padLength"), b.pad_length)
    if b.pad_axis != "vertical":
        values["padAxis"] = b.pad_axis
    if b.border_x_mm != 0.0:
        values["borderXMm"] = _num(_field_path(path, "borderXMm"), b.border_x_mm)
    if b.border_y_mm != 0.0:
        values["borderYMm"] = _num(_field_path(path, "borderYMm"), b.border_y_mm)
    if b.single_sided:
        values["singleSided"] = b.single_sided
    if b.labels is not None:
        labels_path = _field_path(path, "labels")
        values["labels"] = _build_ordered(
            BOARD_LABELS_KEY_ORDER,
            {
                "face": b.labels.face,
                "rowDigits": _num(_field_path(labels_path, "rowDigits"), b.labels.row_digits),
                "allEdges": b.labels.all_edges,
            },
        )
    return _build_ordered(BOARD_KEY_ORDER, values)


def _ordered_mounting_hole(m: MountingHole, index: int) -> JsonObj:
    path = _index_path("mountingHoles", index)
    return _build_ordered(
        MOUNTING_HOLE_KEY_ORDER,
        {
            "id": m.id,
            "at": _ordered_hole(m.at, _field_path(path, "at")),
            "offsetXMm": _num(_field_path(path, "offsetXMm"), m.offset_x_mm),
            "offsetYMm": _num(_field_path(path, "offsetYMm"), m.offset_y_mm),
            "diameter": _num(_field_path(path, "diameter"), m.diameter),
            "headDiameter": _num(_field_path(path, "headDiameter"), m.head_diameter),
        },
    )


def _ordered_edge_connector(e: EdgeConnector, index: int) -> JsonObj:
    path = _index_path("edgeConnectors", index)
    values: dict[str, JsonValue] = {
        "id": e.id,
        "edge": e.edge,
        "start": _num(_field_path(path, "start"), e.start),
        "count": _num(_field_path(path, "count"), e.count),
        "fingerWidth": _num(_field_path(path, "fingerWidth"), e.finger_width),
        "face": e.face,
    }
    # Omitted when derived from the board, like every other defaulted field here.
    if e.finger_length is not None:
        values["fingerLength"] = _num(_field_path(path, "fingerLength"), e.finger_length)
    if e.inset_mm != 0.0:
        values["insetMm"] = _num(_field_path(path, "insetMm"), e.inset_mm)
    return _build_ordered(EDGE_CONNECTOR_KEY_ORDER, values)


def _ordered_component(c: ComponentInstance, index: int) -> JsonObj:
    path = _index_path("components", index)
    return _build_ordered(
        COMPONENT_KEY_ORDER,
        {
            "id": c.id,
            "ref": c.ref,
            "value": c.value,
            "footprintId": c.footprint_id,
            "anchor": _ordered_hole(c.anchor, _field_path(path, "anchor")),
            "rotation": _num(_field_path(path, "rotation"), c.rotation),
            "mirrored": c.mirrored,
            "locked": c.locked,
        },
    )


def _ordered_conductor(c: Conductor, index: int) -> JsonObj:
    path = _index_path("conductors", index)
    path_field_path = _field_path(path, "path")

    values: dict[str, JsonValue] = {
        "id": c.id,
        "kind": c.kind,
        "path": [_ordered_hole(h, _index_path(path_field_path, i)) for i, h in enumerate(c.path)],
        "side": c.side,
        "layerZ": _num(_field_path(path, "layerZ"), c.layer_z),
    }
    if c.net_id is not None:
        values["netId"] = c.net_id

    if isinstance(c, SolderTraceConductor):
        values["buildup"] = c.buildup
        if c.spine is not None:
            spine_path = _field_path(path, "spine")
            values["spine"] = _build_ordered(
                SPINE_KEY_ORDER,
                {
                    "material": c.spine.material,
                    "gauge": _num(_field_path(spine_path, "gauge"), c.spine.gauge),
                },
            )
    elif isinstance(c, WireConductor):
        if c.gauge_awg is not None:
            values["gaugeAwg"] = _num(_field_path(path, "gaugeAwg"), c.gauge_awg)
        if c.color is not None:
            values["color"] = c.color
    elif isinstance(c, LeadBendConductor):
        values["componentId"] = c.component_id
        values["pinNumber"] = c.pin_number
    # StripConductor has no fields beyond the base.

    return _build_ordered(CONDUCTOR_KEY_ORDER, values)


def _ordered_cut(c: TrackCut, index: int) -> JsonObj:
    path = _index_path("cuts", index)
    return _build_ordered(
        CUT_KEY_ORDER,
        {
            "id": c.id,
            "at": _ordered_hole(c.at, _field_path(path, "at")),
        },
    )


def _ordered_net(n: Net, index: int) -> JsonObj:
    path = _index_path("nets", index)
    values: dict[str, JsonValue] = {
        "id": n.id,
        "name": n.name,
        "nodes": [
            _build_ordered(NET_NODE_KEY_ORDER, {"componentRef": node.component_ref, "pin": node.pin})
            for node in n.nodes
        ],
        "class": n.net_class,
    }
    if n.current_a is not None:
        values["currentA"] = _num(_field_path(path, "currentA"), n.current_a)
    if n.voltage_v is not None:
        values["voltageV"] = _num(_field_path(path, "voltageV"), n.voltage_v)
    return _build_ordered(NET_KEY_ORDER, values)


def serialize_document(doc: PerfDocument) -> str:
    """Serializes a document to its .perf text form: pretty-printed JSON, fixed key
    order, diff-stable array sorting, trailing newline. Raises if the document contains
    a non-finite number (see :func:`_num`) rather than silently writing something a
    reader can't trust.
    """
    components = sorted(doc.components, key=lambda c: c.id)
    conductors = sorted(doc.conductors, key=lambda c: c.id)
    cuts = sorted(doc.cuts, key=_cut_sort_key)
    mounting_holes = sorted(doc.mounting_holes, key=_mounting_hole_sort_key)
    edge_connectors = sorted(doc.edge_connectors, key=lambda e: e.id)
    nets = sorted(doc.nets, key=lambda n: n.id)

    root = _build_ordered(
        DOCUMENT_KEY_ORDER,
        {
            "formatVersion": _num("formatVersion", doc.format_version),
            "meta": _build_ordered(
                META_KEY_ORDER,
                {
                    "name": doc.meta.name,
                    "created": doc.meta.created,
                    "modified": doc.meta.modified,
                },
            ),
            "board": _ordered_board(doc.board),
            "components": [_ordered_component(c, i) for i, c in enumerate(components)],
            "conductors": [_ordered_conductor(c, i) for i, c in enumerate(conductors)],
            "cuts": [_ordered_cut(c, i) for i, c in enumerate(cuts)],
            "nets": [_ordered_net(n, i) for i, n in enumerate(nets)],
            # Unlike the four arrays above, these are omitted entirely when empty --
            # see the note in `_ordered_board` about why an unused feature has to leave
            # no trace in the file.
            **(
                {
                    "mountingHoles": [
                        _ordered_mounting_hole(m, i) for i, m in enumerate(mounting_holes)
                    ]
                }
                if mounting_holes
                else {}
            ),
            **(
                {
                    "edgeConnectors": [
                        _ordered_edge_connector(e, i) for i, e in enumerate(edge_connectors)
                    ]
                }
                if edge_connectors
                else {}
            ),
        },
    )

    return f"{_write_json(root, 0)}\n"


# ---------------------------------------------------------------------------
# Hand-rolled JSON writer -- matches ``JSON.stringify(root, null, 2)`` byte for byte
# ---------------------------------------------------------------------------
#
# `json.dumps` cannot be reused here: Python's json encoder renders a whole-number
# float like 1.0 as "1.0", where JS's JSON.stringify (which has no separate float type)
# renders it as "1". Every number in this format therefore goes through
# `_format_js_number`, which reimplements the ECMA-262 Number::toString algorithm, and
# the object/array/string handling below mirrors JSON.stringify's own behaviour
# (unescaped non-ASCII, 2-space indent) rather than `ensure_ascii`-flavoured Python
# defaults.


def _format_js_digits_and_point(n: float) -> tuple[str, int]:
    """Returns (digits, point) with `digits` containing no leading or trailing zeros
    (unless the value is exactly zero) such that ``n == 0.<digits> * 10**point`` -- this
    is exactly the (s, n) pair from the ECMA-262 Number::toString algorithm (s = the
    digit string, n = the decimal point position). `repr(n)` already gives Python's
    shortest-round-trip digit sequence for a double, which is the same digit sequence
    JS's shortest-round-trip algorithm produces, so parsing it via Decimal and then
    reformatting per the ECMA rules below reproduces JS's Number::toString exactly,
    including for magnitudes where Python and JS disagree about *when* to switch to
    exponential notation.
    """
    d = Decimal(repr(n))
    _sign, digit_tuple, exponent = d.as_tuple()
    digits = "".join(str(x) for x in digit_tuple)
    stripped = digits.rstrip("0")
    if stripped == "":
        return "0", 1
    removed = len(digits) - len(stripped)
    exponent = int(exponent) + removed
    point = len(stripped) + exponent
    return stripped, point


def _format_js_number(n: int | float) -> str:
    """Formats a number exactly as JS's ``Number.prototype.toString`` (equivalently,
    ``JSON.stringify``) would: no distinction between integers and whole-number floats
    ("1" not "1.0"), shortest round-trip decimal digits, and JS's own thresholds for
    switching to exponential notation.
    """
    if isinstance(n, int):
        return str(n)
    if n == 0.0:
        return "0"
    sign = "-" if n < 0 else ""
    digits, point = _format_js_digits_and_point(abs(n))
    k = len(digits)
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    exp_val = point - 1
    mantissa = digits[0] + ("." + digits[1:] if k > 1 else "")
    exp_sign = "+" if exp_val >= 0 else "-"
    return f"{sign}{mantissa}e{exp_sign}{abs(exp_val)}"


def _escape_json_string(s: str) -> str:
    """JSON string escaping matching ``JSON.stringify``: control characters are
    escaped, but non-ASCII characters are written literally (no ``\\uXXXX`` escaping),
    since JSON text is UTF-8 and JS's stringify does not ASCII-escape by default.
    """
    out: list[str] = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _write_json(value: JsonValue, indent: int) -> str:
    """Recursive pretty-printer reproducing ``JSON.stringify(value, null, 2)``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_js_number(value)
    if isinstance(value, str):
        return _escape_json_string(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        pad = "  " * indent
        pad_inner = "  " * (indent + 1)
        items = [
            f"{pad_inner}{_escape_json_string(k)}: {_write_json(v, indent + 1)}" for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        pad = "  " * indent
        pad_inner = "  " * (indent + 1)
        items = [f"{pad_inner}{_write_json(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    raise TypeError(f"Cannot serialize value of type {type(value)!r} to JSON.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Deserialization: structural validation
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    """Internal-only: carries a machine-readable code and a precise path to the
    failure.
    """

    def __init__(self, code: str, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _describe_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__  # pragma: no cover


def _expect_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError("not-an-object", f'Expected an object at "{path}", got {_describe_type(value)}.', path)
    return value


def _expect_array(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError("not-an-array", f'Expected an array at "{path}", got {_describe_type(value)}.', path)
    return value


def _expect_string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("invalid-type", f'Expected a string at "{path}", got {_describe_type(value)}.', path)
    return value


def _expect_boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("invalid-type", f'Expected a boolean at "{path}", got {_describe_type(value)}.', path)
    return value


def _expect_number(value: object, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("invalid-type", f'Expected a number at "{path}", got {_describe_type(value)}.', path)
    if not math.isfinite(value):
        raise ValidationError("invalid-value", f'Expected a finite number at "{path}", got {value}.', path)
    return value


def _expect_integer(value: object, path: str) -> int:
    n = _expect_number(value, path)
    if not float(n).is_integer():
        raise ValidationError("invalid-value", f'Expected an integer at "{path}", got {n}.', path)
    return int(n)


def _expect_enum(value: object, path: str, allowed: tuple[str, ...]) -> str:
    s = _expect_string(value, path)
    if s not in allowed:
        options = ", ".join(f'"{a}"' for a in allowed)
        raise ValidationError(
            "invalid-value", f'Expected one of {options} at "{path}", got {json.dumps(s)}.', path
        )
    return s


def _require_field(obj: dict[str, object], key: str, parent_path: str) -> object:
    """Note: a JSON ``null`` is a *present* value, not a missing one -- it will fail
    whichever ``_expect_*`` validator receives it, with an ``invalid-type`` error, not
    ``missing-field``. Only an absent key is ``missing-field``.
    """
    if key not in obj:
        raise ValidationError("missing-field", f'Missing required field "{key}".', _field_path(parent_path, key))
    return obj[key]


def _check_unknown_keys(obj: dict[str, object], known: tuple[str, ...], path: str, warnings: list[str]) -> None:
    """Emits a warning (not an error) naming any property not part of the schema at
    `path`.
    """
    for key in obj:
        if key not in known:
            warnings.append(f'Unknown property "{_field_path(path, key)}" was ignored.')


BOARD_TYPES: tuple[BoardType, ...] = ("pad-per-hole", "stripboard", "plain")
BOARD_MATERIALS: tuple[BoardMaterial, ...] = ("FR4", "FR2", "FR1")
STRIP_AXES: tuple[str, ...] = ("horizontal", "vertical")
PAD_SHAPES: tuple[PadShape, ...] = ("round", "oblong")
PAD_AXES: tuple[PadAxis, ...] = ("horizontal", "vertical")
BOARD_FACES: tuple[BoardFace, ...] = ("top", "bottom", "both")
BOARD_EDGES: tuple[BoardEdge, ...] = ("top", "bottom", "left", "right")
BOARD_SIDES: tuple[BoardSide, ...] = ("top", "bottom")
SOLDER_BUILDUPS: tuple[SolderBuildup, ...] = ("light", "normal", "heavy")
SPINE_MATERIALS: tuple[str, ...] = ("tinned-copper", "lead-offcut")
CONDUCTOR_KINDS: tuple[str, ...] = (
    "lead-bend",
    "solder-trace",
    "solder-trace-wired",
    "bare-wire",
    "insulated-wire",
    "top-jumper",
    "strip",
)
NET_CLASSES: tuple[NetClass, ...] = ("power", "ground", "signal")


def _parse_hole(raw: object, path: str, warnings: list[str]) -> HoleCoord:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, HOLE_KEY_ORDER, path, warnings)
    return HoleCoord(
        col=_expect_integer(_require_field(obj, "col", path), _field_path(path, "col")),
        row=_expect_integer(_require_field(obj, "row", path), _field_path(path, "row")),
    )


def _parse_rotation(value: object, path: str) -> Rotation:
    n = _expect_integer(value, path)
    if n not in VALID_ROTATIONS:
        raise ValidationError(
            "invalid-value", f'Expected rotation to be one of 0, 90, 180, 270 at "{path}", got {n}.', path
        )
    return n


def _parse_meta(raw: object, warnings: list[str]) -> DocumentMeta:
    path = "meta"
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, META_KEY_ORDER, path, warnings)
    return DocumentMeta(
        name=_expect_string(_require_field(obj, "name", path), _field_path(path, "name")),
        created=_expect_string(_require_field(obj, "created", path), _field_path(path, "created")),
        modified=_expect_string(_require_field(obj, "modified", path), _field_path(path, "modified")),
    )


def _parse_board_labels(raw: object, path: str, warnings: list[str]) -> BoardLabels:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, BOARD_LABELS_KEY_ORDER, path, warnings)
    face_raw = obj.get("face")
    row_digits_raw = obj.get("rowDigits")
    row_digits = (
        1 if row_digits_raw is None else _expect_integer(row_digits_raw, _field_path(path, "rowDigits"))
    )
    if row_digits < 1:
        raise ValidationError(
            "invalid-value",
            f'A printed row label cannot be narrower than one digit, got {row_digits}.',
            _field_path(path, "rowDigits"),
        )
    all_edges_raw = obj.get("allEdges")
    return BoardLabels(
        face="both" if face_raw is None else _expect_enum(face_raw, _field_path(path, "face"), BOARD_FACES),  # type: ignore[arg-type]
        row_digits=row_digits,
        all_edges=(
            True if all_edges_raw is None else _expect_boolean(all_edges_raw, _field_path(path, "allEdges"))
        ),
    )


def _parse_board(raw: object, warnings: list[str]) -> Board:
    path = "board"
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, BOARD_KEY_ORDER, path, warnings)

    strip_axis_raw = obj.get("stripAxis")
    strip_axis = (
        None if strip_axis_raw is None else _expect_enum(strip_axis_raw, _field_path(path, "stripAxis"), STRIP_AXES)
    )

    pad_shape_raw = obj.get("padShape")
    pad_shape = (
        "round" if pad_shape_raw is None else _expect_enum(pad_shape_raw, _field_path(path, "padShape"), PAD_SHAPES)
    )
    pad_length_raw = obj.get("padLength")
    pad_length = (
        None if pad_length_raw is None else _expect_number(pad_length_raw, _field_path(path, "padLength"))
    )
    pad_axis_raw = obj.get("padAxis")
    pad_axis = (
        "vertical" if pad_axis_raw is None else _expect_enum(pad_axis_raw, _field_path(path, "padAxis"), PAD_AXES)
    )
    borders: dict[str, float] = {}
    for key, attr in (("borderXMm", "border_x_mm"), ("borderYMm", "border_y_mm")):
        raw = obj.get(key)
        value = 0.0 if raw is None else _expect_number(raw, _field_path(path, key))
        if value < 0:
            raise ValidationError(
                "invalid-value",
                f"A board border cannot be negative, got {value}.",
                _field_path(path, key),
            )
        borders[attr] = float(value)
    single_sided_raw = obj.get("singleSided")
    single_sided = (
        False if single_sided_raw is None else _expect_boolean(single_sided_raw, _field_path(path, "singleSided"))
    )

    labels_raw = obj.get("labels")
    labels = (
        None
        if labels_raw is None
        else _parse_board_labels(labels_raw, _field_path(path, "labels"), warnings)
    )

    pad_diameter = _expect_number(_require_field(obj, "padDiameter", path), _field_path(path, "padDiameter"))
    # A warning, not an error, for the same reason a diagonal solder-trace step is one:
    # a hand-edited file must still open. `geometry.pad_extent_mm` falls back to a round
    # pad, so the board draws and checks sensibly meanwhile.
    if pad_shape == "oblong" and (pad_length is None or pad_length <= pad_diameter):
        warnings.append(
            f'board.padShape is "oblong" but padLength is '
            f"{'missing' if pad_length is None else pad_length}, which is not longer than the "
            f"{pad_diameter} mm pad width. The pads will be treated as round until it is."
        )

    return Board(
        type=_expect_enum(_require_field(obj, "type", path), _field_path(path, "type"), BOARD_TYPES),  # type: ignore[arg-type]
        cols=_expect_integer(_require_field(obj, "cols", path), _field_path(path, "cols")),
        rows=_expect_integer(_require_field(obj, "rows", path), _field_path(path, "rows")),
        pitch=_expect_number(_require_field(obj, "pitch", path), _field_path(path, "pitch")),
        thickness=_expect_number(_require_field(obj, "thickness", path), _field_path(path, "thickness")),
        material=_expect_enum(_require_field(obj, "material", path), _field_path(path, "material"), BOARD_MATERIALS),  # type: ignore[arg-type]
        pad_diameter=pad_diameter,
        drill_diameter=_expect_number(_require_field(obj, "drillDiameter", path), _field_path(path, "drillDiameter")),
        strip_axis=strip_axis,  # type: ignore[arg-type]
        pad_shape=pad_shape,  # type: ignore[arg-type]
        pad_length=pad_length,
        pad_axis=pad_axis,  # type: ignore[arg-type]
        border_x_mm=borders["border_x_mm"],
        border_y_mm=borders["border_y_mm"],
        single_sided=single_sided,
        labels=labels,
    )


def _parse_mounting_hole(raw: object, path: str, warnings: list[str]) -> MountingHole:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, MOUNTING_HOLE_KEY_ORDER, path, warnings)
    diameter_raw = obj.get("diameter")
    head_raw = obj.get("headDiameter")
    return MountingHole(
        id=_expect_string(_require_field(obj, "id", path), _field_path(path, "id")),
        at=_parse_hole(_require_field(obj, "at", path), _field_path(path, "at"), warnings),
        offset_x_mm=(
            0.0 if obj.get("offsetXMm") is None
            else _expect_number(obj["offsetXMm"], _field_path(path, "offsetXMm"))
        ),
        offset_y_mm=(
            0.0 if obj.get("offsetYMm") is None
            else _expect_number(obj["offsetYMm"], _field_path(path, "offsetYMm"))
        ),
        diameter=(
            3.2 if diameter_raw is None else _expect_number(diameter_raw, _field_path(path, "diameter"))
        ),
        head_diameter=(
            6.0 if head_raw is None else _expect_number(head_raw, _field_path(path, "headDiameter"))
        ),
    )


def _parse_edge_connector(raw: object, path: str, warnings: list[str]) -> EdgeConnector:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, EDGE_CONNECTOR_KEY_ORDER, path, warnings)
    width_raw = obj.get("fingerWidth")
    length_raw = obj.get("fingerLength")
    face_raw = obj.get("face")
    return EdgeConnector(
        id=_expect_string(_require_field(obj, "id", path), _field_path(path, "id")),
        edge=_expect_enum(_require_field(obj, "edge", path), _field_path(path, "edge"), BOARD_EDGES),  # type: ignore[arg-type]
        start=_expect_integer(_require_field(obj, "start", path), _field_path(path, "start")),
        count=_expect_integer(_require_field(obj, "count", path), _field_path(path, "count")),
        finger_width=(
            2.0 if width_raw is None else _expect_number(width_raw, _field_path(path, "fingerWidth"))
        ),
        finger_length=(
            None if length_raw is None else _expect_number(length_raw, _field_path(path, "fingerLength"))
        ),
        inset_mm=(
            0.0 if obj.get("insetMm") is None
            else _expect_number(obj["insetMm"], _field_path(path, "insetMm"))
        ),
        face="both" if face_raw is None else _expect_enum(face_raw, _field_path(path, "face"), BOARD_FACES),  # type: ignore[arg-type]
    )


def _parse_component(raw: object, path: str, warnings: list[str]) -> ComponentInstance:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, COMPONENT_KEY_ORDER, path, warnings)
    return ComponentInstance(
        id=_expect_string(_require_field(obj, "id", path), _field_path(path, "id")),
        ref=_expect_string(_require_field(obj, "ref", path), _field_path(path, "ref")),
        value=_expect_string(_require_field(obj, "value", path), _field_path(path, "value")),
        footprint_id=_expect_string(_require_field(obj, "footprintId", path), _field_path(path, "footprintId")),
        anchor=_parse_hole(_require_field(obj, "anchor", path), _field_path(path, "anchor"), warnings),
        rotation=_parse_rotation(_require_field(obj, "rotation", path), _field_path(path, "rotation")),
        mirrored=_expect_boolean(_require_field(obj, "mirrored", path), _field_path(path, "mirrored")),
        locked=_expect_boolean(_require_field(obj, "locked", path), _field_path(path, "locked")),
    )


def _validate_solder_trace_chain(c: SolderTraceConductor, path: str, warnings: list[str]) -> None:
    """Checks a solder-trace path against the orthogonal-chain invariant -- solder
    cannot reliably span a diagonal gap (PLAN.md Section 4.6, and the path doc comment
    on SolderTraceConductor in model.py).

    Deliberately a warning rather than an error: a hand-edited file must still load, so
    the user sees the problem in DRC instead of being locked out of their own project.
    Uses `validate_orthogonal_chain` from geometry.py -- the one and only adjacency
    check in the codebase -- rather than a bespoke one here.
    """
    result = validate_orthogonal_chain(c.path)
    if result.ok:
        return
    warnings.append(
        f"{_index_path(_field_path(path, 'path'), result.index)}: {result.reason} "
        f"The document still loaded -- this will be reported by DRC."
    )


def _parse_conductor(raw: object, path: str, warnings: list[str]) -> Conductor:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, CONDUCTOR_KEY_ORDER, path, warnings)

    kind = _expect_enum(_require_field(obj, "kind", path), _field_path(path, "kind"), CONDUCTOR_KINDS)
    conductor_id = _expect_string(_require_field(obj, "id", path), _field_path(path, "id"))
    path_field_path = _field_path(path, "path")
    path_raw = _expect_array(_require_field(obj, "path", path), path_field_path)
    hole_path = tuple(_parse_hole(h, _index_path(path_field_path, i), warnings) for i, h in enumerate(path_raw))
    side = _expect_enum(_require_field(obj, "side", path), _field_path(path, "side"), BOARD_SIDES)
    net_id_raw = obj.get("netId")
    net_id = None if net_id_raw is None else _expect_string(net_id_raw, _field_path(path, "netId"))
    layer_z = _expect_integer(_require_field(obj, "layerZ", path), _field_path(path, "layerZ"))

    if kind in ("solder-trace", "solder-trace-wired"):
        if side != "bottom":
            raise ValidationError(
                "invalid-value",
                f'Conductors of kind "{kind}" must have side "bottom", got "{side}".',
                _field_path(path, "side"),
            )
        buildup = _expect_enum(_require_field(obj, "buildup", path), _field_path(path, "buildup"), SOLDER_BUILDUPS)
        spine_raw = obj.get("spine")
        spine: SpineSpec | None = None
        if spine_raw is not None:
            spine_path = _field_path(path, "spine")
            spine_obj = _expect_object(spine_raw, spine_path)
            _check_unknown_keys(spine_obj, SPINE_KEY_ORDER, spine_path, warnings)
            spine = SpineSpec(
                material=_expect_enum(  # type: ignore[arg-type]
                    _require_field(spine_obj, "material", spine_path), _field_path(spine_path, "material"), SPINE_MATERIALS
                ),
                gauge=_expect_number(_require_field(spine_obj, "gauge", spine_path), _field_path(spine_path, "gauge")),
            )
        solder_conductor = SolderTraceConductor(
            id=conductor_id,
            kind=kind,  # type: ignore[arg-type]
            path=hole_path,
            side="bottom",
            layer_z=layer_z,
            buildup=buildup,  # type: ignore[arg-type]
            spine=spine,
            net_id=net_id,
        )
        _validate_solder_trace_chain(solder_conductor, path, warnings)
        return solder_conductor

    if kind in ("bare-wire", "insulated-wire", "top-jumper"):
        gauge_awg_raw = obj.get("gaugeAwg")
        gauge_awg = None if gauge_awg_raw is None else _expect_number(gauge_awg_raw, _field_path(path, "gaugeAwg"))
        color_raw = obj.get("color")
        color = None if color_raw is None else _expect_string(color_raw, _field_path(path, "color"))
        return WireConductor(
            id=conductor_id,
            kind=kind,  # type: ignore[arg-type]
            path=hole_path,
            side=side,  # type: ignore[arg-type]
            layer_z=layer_z,
            net_id=net_id,
            gauge_awg=gauge_awg,  # type: ignore[arg-type]
            color=color,
        )

    if kind == "lead-bend":
        if side != "bottom":
            raise ValidationError(
                "invalid-value",
                f'Conductors of kind "lead-bend" must have side "bottom", got "{side}".',
                _field_path(path, "side"),
            )
        component_id = _expect_string(_require_field(obj, "componentId", path), _field_path(path, "componentId"))
        pin_number = _expect_string(_require_field(obj, "pinNumber", path), _field_path(path, "pinNumber"))
        return LeadBendConductor(
            id=conductor_id,
            path=hole_path,
            component_id=component_id,
            pin_number=pin_number,
            net_id=net_id,
            layer_z=layer_z,
        )

    if kind == "strip":
        return StripConductor(
            id=conductor_id,
            path=hole_path,
            net_id=net_id,
            layer_z=layer_z,
            side=side,  # type: ignore[arg-type]
        )

    raise ValidationError("invalid-value", "Unhandled conductor kind.", _field_path(path, "kind"))  # pragma: no cover


def _parse_cut(raw: object, path: str, warnings: list[str]) -> TrackCut:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, CUT_KEY_ORDER, path, warnings)
    return TrackCut(
        id=_expect_string(_require_field(obj, "id", path), _field_path(path, "id")),
        at=_parse_hole(_require_field(obj, "at", path), _field_path(path, "at"), warnings),
    )


def _parse_net_node(raw: object, path: str, warnings: list[str]) -> NetNode:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, NET_NODE_KEY_ORDER, path, warnings)
    return NetNode(
        component_ref=_expect_string(_require_field(obj, "componentRef", path), _field_path(path, "componentRef")),
        pin=_expect_string(_require_field(obj, "pin", path), _field_path(path, "pin")),
    )


def _parse_net(raw: object, path: str, warnings: list[str]) -> Net:
    obj = _expect_object(raw, path)
    _check_unknown_keys(obj, NET_KEY_ORDER, path, warnings)
    nodes_path = _field_path(path, "nodes")
    nodes_raw = _expect_array(_require_field(obj, "nodes", path), nodes_path)
    nodes = tuple(_parse_net_node(n, _index_path(nodes_path, i), warnings) for i, n in enumerate(nodes_raw))
    current_a_raw = obj.get("currentA")
    current_a = None if current_a_raw is None else _expect_number(current_a_raw, _field_path(path, "currentA"))
    voltage_v_raw = obj.get("voltageV")
    voltage_v = None if voltage_v_raw is None else _expect_number(voltage_v_raw, _field_path(path, "voltageV"))
    return Net(
        id=_expect_string(_require_field(obj, "id", path), _field_path(path, "id")),
        name=_expect_string(_require_field(obj, "name", path), _field_path(path, "name")),
        nodes=nodes,
        net_class=_expect_enum(_require_field(obj, "class", path), _field_path(path, "class"), NET_CLASSES),  # type: ignore[arg-type]
        current_a=current_a,
        voltage_v=voltage_v,
    )


# ---------------------------------------------------------------------------
# Format migrations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Migration:
    from_version: int
    to_version: int
    migrate: Callable[[dict[str, object]], dict[str, object]]


#: Ordered chain of migrations, oldest first. Empty today because format version 1 is
#: the only version that has ever existed. The seam is built now, on purpose, so that
#: the day format 2 exists there is somewhere to put its migration -- retrofitting a
#: migration chain after users already have version-1 files on disk is how projects
#: lose data.
MIGRATIONS: tuple[_Migration, ...] = ()


def _migrate(doc: dict[str, object], from_version: int) -> dict[str, object]:
    current = doc
    version = from_version
    for step in MIGRATIONS:
        if version == step.from_version:
            current = step.migrate(current)
            version = step.to_version
    return current


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------


def _parse_document(raw_input: object) -> tuple[PerfDocument, list[str]]:
    warnings: list[str] = []
    root = _expect_object(raw_input, "")

    format_version = _expect_integer(_require_field(root, "formatVersion", ""), "formatVersion")
    if format_version > CURRENT_FORMAT_VERSION:
        raise ValidationError(
            "format-too-new",
            f"This file was saved by a newer version of PerfStudio (file format {format_version}); "
            f"this build understands up to format {CURRENT_FORMAT_VERSION}. Please upgrade PerfStudio "
            f"to open it.",
            "formatVersion",
        )

    migrated = _migrate(root, format_version)
    _check_unknown_keys(migrated, DOCUMENT_KEY_ORDER, "", warnings)

    meta = _parse_meta(_require_field(migrated, "meta", ""), warnings)
    board = _parse_board(_require_field(migrated, "board", ""), warnings)

    components_raw = _expect_array(_require_field(migrated, "components", ""), "components")
    components = tuple(
        _parse_component(item, _index_path("components", i), warnings) for i, item in enumerate(components_raw)
    )

    conductors_raw = _expect_array(_require_field(migrated, "conductors", ""), "conductors")
    conductors = tuple(
        _parse_conductor(item, _index_path("conductors", i), warnings) for i, item in enumerate(conductors_raw)
    )

    cuts_raw = _expect_array(_require_field(migrated, "cuts", ""), "cuts")
    cuts = tuple(_parse_cut(item, _index_path("cuts", i), warnings) for i, item in enumerate(cuts_raw))

    # Optional, unlike the arrays above: every file written before these features existed
    # simply has no such key, and requiring one would make this build unable to open its
    # own back catalogue. That is the same property that keeps the format version at 1.
    mounting_raw = _expect_array(migrated.get("mountingHoles", []), "mountingHoles")
    mounting_holes = tuple(
        _parse_mounting_hole(item, _index_path("mountingHoles", i), warnings)
        for i, item in enumerate(mounting_raw)
    )

    connectors_raw = _expect_array(migrated.get("edgeConnectors", []), "edgeConnectors")
    edge_connectors = tuple(
        _parse_edge_connector(item, _index_path("edgeConnectors", i), warnings)
        for i, item in enumerate(connectors_raw)
    )

    nets_raw = _expect_array(_require_field(migrated, "nets", ""), "nets")
    nets = tuple(_parse_net(item, _index_path("nets", i), warnings) for i, item in enumerate(nets_raw))

    document = PerfDocument(
        meta=meta,
        board=board,
        components=components,
        conductors=conductors,
        cuts=cuts,
        nets=nets,
        mounting_holes=mounting_holes,
        edge_connectors=edge_connectors,
        format_version=CURRENT_FORMAT_VERSION,
    )
    return document, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _reject_json_constant(token: str) -> float:
    """Passed to `json.loads` as `parse_constant`: Python's json module, unlike JSON
    itself (and unlike JS's `JSON.parse`), accepts the non-standard tokens `NaN`,
    `Infinity` and `-Infinity` by default. Rejecting them here keeps `invalid-json`
    behaviour identical to the TypeScript implementation, where such a file would
    simply fail to parse.
    """
    raise ValueError(f'Unexpected token "{token}" is not valid JSON.')


def deserialize_document(json_text: str) -> DeserializeOk | DeserializeErr:
    """Parses and validates a .perf file's contents. Never raises: JSON syntax errors
    and structural problems both come back as a `DeserializeErr` with a
    machine-readable `code` and, wherever the failure can be localized, a `path` such
    as `"components[3].anchor.col"`. Non-fatal issues (an unknown property, a
    solder-trace path with a diagonal step) are reported as `warnings` on a successful
    result rather than blocking the load -- a hand-edited file should still open.
    """
    try:
        raw = json.loads(json_text, parse_constant=_reject_json_constant)
    except ValueError as err:
        return DeserializeErr(code="invalid-json", message=f"Could not parse file as JSON: {err}")

    try:
        document, warnings = _parse_document(raw)
        return DeserializeOk(document=document, warnings=tuple(warnings))
    except ValidationError as err:
        return DeserializeErr(code=err.code, message=str(err), path=err.path)


def parse_document_or_throw(json_text: str) -> PerfDocument:
    """Throwing variant of :func:`deserialize_document` for callers that want it."""
    result = deserialize_document(json_text)
    if not result.ok:
        location = f" (at {result.path})" if result.path is not None else ""
        raise ValueError(f"{result.code}: {result.message}{location}")
    return result.document
