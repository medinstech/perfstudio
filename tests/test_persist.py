"""Tests for perfstudio.persist: the .perf project file format.

The load-bearing test in this file is `test_golden_round_trip_byte_identical`: the
acceptance criterion for the whole port is that every real document the TypeScript
engine ever serialized (tools/diffcheck/golden/*.perf) loads without error and
re-serializes to a string that is BYTE-IDENTICAL to the file on disk, not merely
structurally equivalent. Everything else here checks the specific documented
behaviours (fixed key order, sorting, -0 normalization, non-finite rejection, optional
fields omitted rather than null, warnings-not-errors for recoverable problems) in
isolation, in case the golden fixtures never happen to exercise a given path.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from perfstudio import persist
from perfstudio.model import (
    Board,
    ComponentInstance,
    DocumentMeta,
    HoleCoord,
    Net,
    PerfDocument,
    SolderTraceConductor,
    SpineSpec,
    WireConductor,
)

GOLDEN_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools" / "diffcheck" / "golden"
PERF_FILES = sorted(GOLDEN_DIR.glob("*.perf"))

# ---------------------------------------------------------------------------
# Fixtures shared by the hand-written (non-golden) tests
# ---------------------------------------------------------------------------


def _minimal_board() -> Board:
    return Board(
        type="pad-per-hole",
        cols=10,
        rows=10,
        pitch=2.54,
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=1.0,
    )


def _minimal_meta() -> DocumentMeta:
    return DocumentMeta(name="t", created="2026-01-01T00:00:00.000Z", modified="2026-01-01T00:00:00.000Z")


def _minimal_document(**overrides: object) -> PerfDocument:
    base: dict[str, object] = dict(meta=_minimal_meta(), board=_minimal_board())
    base.update(overrides)
    return PerfDocument(**base)  # type: ignore[arg-type]


def _minimal_document_json() -> dict[str, object]:
    """A minimal, valid .perf JSON structure as a plain dict, for tests that want to
    mutate a field and feed the result through `deserialize_document`.
    """
    return {
        "formatVersion": 1,
        "meta": {"name": "t", "created": "2026-01-01T00:00:00.000Z", "modified": "2026-01-01T00:00:00.000Z"},
        "board": {
            "type": "pad-per-hole",
            "cols": 10,
            "rows": 10,
            "pitch": 2.54,
            "thickness": 1.6,
            "material": "FR4",
            "padDiameter": 1.9,
            "drillDiameter": 1,
        },
        "components": [],
        "conductors": [],
        "cuts": [],
        "nets": [],
    }


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_exactly_fifteen_golden_files_present() -> None:
    assert len(PERF_FILES) == 15, (
        f"Expected 15 golden .perf files, found {len(PERF_FILES)}: {[p.name for p in PERF_FILES]}"
    )


def _first_diff(original: str, serialized: str) -> str:
    a = original.splitlines()
    b = serialized.splitlines()
    for i, (la, lb) in enumerate(zip(a, b), start=1):
        if la != lb:
            return f"first differing line {i}:\n  golden:     {la!r}\n  round-trip: {lb!r}"
    if len(a) != len(b):
        return f"line count differs: golden has {len(a)}, round-trip has {len(b)}"
    return "<no textual difference found>"  # pragma: no cover


@pytest.mark.parametrize("perf_path", PERF_FILES, ids=lambda p: p.name)
def test_golden_round_trip_byte_identical(perf_path: pathlib.Path) -> None:
    """Load every golden .perf file and re-serialize it; the result must be
    byte-identical (not merely equivalent JSON) to the file on disk.
    """
    original = perf_path.read_text(encoding="utf-8")

    result = persist.deserialize_document(original)
    if not result.ok:
        pytest.fail(f"{perf_path.name} failed to load: [{result.code}] {result.message} (path={result.path})")

    assert not result.warnings, f"{perf_path.name} produced unexpected warnings on a known-good file: {result.warnings}"

    serialized = persist.serialize_document(result.document)
    if serialized != original:
        pytest.fail(f"{perf_path.name} did not round-trip byte-identically\n{_first_diff(original, serialized)}")


@pytest.mark.parametrize("perf_path", PERF_FILES, ids=lambda p: p.name)
def test_golden_round_trip_via_throwing_variant(perf_path: pathlib.Path) -> None:
    """The throwing convenience wrapper must agree with the non-throwing one."""
    original = perf_path.read_text(encoding="utf-8")
    document = persist.parse_document_or_throw(original)
    assert persist.serialize_document(document) == original


# ---------------------------------------------------------------------------
# Key order, sorting, and the "path is never sorted" invariant
# ---------------------------------------------------------------------------


def test_document_key_order_is_fixed() -> None:
    doc = _minimal_document()
    text = persist.serialize_document(doc)
    parsed = json.loads(text)
    assert list(parsed.keys()) == ["formatVersion", "meta", "board", "components", "conductors", "cuts", "nets"]
    assert list(parsed["meta"].keys()) == ["name", "created", "modified"]
    assert list(parsed["board"].keys()) == [
        "type",
        "cols",
        "rows",
        "pitch",
        "thickness",
        "material",
        "padDiameter",
        "drillDiameter",
    ]


def test_components_conductors_nets_sorted_by_id() -> None:
    board = _minimal_board()
    meta = _minimal_meta()
    components = (
        ComponentInstance(id="cmp-9", ref="R9", value="1k", footprint_id="r-axial-4", anchor=HoleCoord(0, 0)),
        ComponentInstance(id="cmp-2", ref="R2", value="1k", footprint_id="r-axial-4", anchor=HoleCoord(1, 0)),
        ComponentInstance(id="cmp-10", ref="R10", value="1k", footprint_id="r-axial-4", anchor=HoleCoord(2, 0)),
    )
    nets = (
        Net(id="net-b", name="B", nodes=()),
        Net(id="net-a", name="A", nodes=()),
    )
    doc = PerfDocument(meta=meta, board=board, components=components, nets=nets)
    parsed = json.loads(persist.serialize_document(doc))
    # ASCII/codepoint sort, not numeric: "cmp-10" < "cmp-2" < "cmp-9".
    assert [c["id"] for c in parsed["components"]] == ["cmp-10", "cmp-2", "cmp-9"]
    assert [n["id"] for n in parsed["nets"]] == ["net-a", "net-b"]


def test_cuts_sorted_by_hole_then_id() -> None:
    from perfstudio.model import TrackCut

    cuts = (
        TrackCut(id="cut-z", at=HoleCoord(col=5, row=1)),
        TrackCut(id="cut-b", at=HoleCoord(col=1, row=1)),
        TrackCut(id="cut-a", at=HoleCoord(col=1, row=1)),  # same hole as cut-b: id tiebreak
        TrackCut(id="cut-y", at=HoleCoord(col=0, row=0)),
    )
    doc = _minimal_document(cuts=cuts)
    parsed = json.loads(persist.serialize_document(doc))
    assert [c["id"] for c in parsed["cuts"]] == ["cut-y", "cut-a", "cut-b", "cut-z"]


def test_conductor_path_order_is_never_sorted() -> None:
    """Path order is the physical chain of holes -- it must survive exactly as given,
    even though it looks "unsorted" by coordinate.
    """
    zigzag_path = (
        HoleCoord(col=5, row=5),
        HoleCoord(col=5, row=4),
        HoleCoord(col=6, row=4),
        HoleCoord(col=6, row=3),
    )
    conductor = WireConductor(id="cond-1", path=zigzag_path, kind="bare-wire", side="bottom")
    doc = _minimal_document(conductors=(conductor,))
    parsed = json.loads(persist.serialize_document(doc))
    got_path = [(h["col"], h["row"]) for h in parsed["conductors"][0]["path"]]
    assert got_path == [(5, 5), (5, 4), (6, 4), (6, 3)]


# ---------------------------------------------------------------------------
# Number handling
# ---------------------------------------------------------------------------


def test_negative_zero_normalized_to_zero() -> None:
    board = Board(
        type="pad-per-hole",
        cols=1,
        rows=1,
        pitch=2.54,
        thickness=-0.0,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=1.0,
    )
    doc = _minimal_document(board=board)
    text = persist.serialize_document(doc)
    assert '"thickness": 0,' in text
    assert '"thickness": -0' not in text


def test_non_finite_number_rejected_on_serialize() -> None:
    board = Board(
        type="pad-per-hole",
        cols=1,
        rows=1,
        pitch=float("nan"),
        thickness=1.6,
        material="FR4",
        pad_diameter=1.9,
        drill_diameter=1.0,
    )
    doc = _minimal_document(board=board)
    with pytest.raises(ValueError, match="non-finite"):
        persist.serialize_document(doc)
    # And infinities are rejected the same way, not turned into a bare "null".
    board2 = dataclasses.replace(board, pitch=float("inf"))
    with pytest.raises(ValueError, match="non-finite"):
        persist.serialize_document(_minimal_document(board=board2))


def test_whole_number_floats_serialize_without_decimal_point() -> None:
    """Byte-identical round-tripping depends on this: JS has one numeric type, so
    JSON.stringify(1.0) is "1", never "1.0". `drillDiameter: 1` in every golden file
    depends on exactly this.
    """
    doc = _minimal_document()
    text = persist.serialize_document(doc)
    assert '"drillDiameter": 1\n' in text
    assert '"drillDiameter": 1.0' not in text


# ---------------------------------------------------------------------------
# Optional fields: omitted, never emitted as null
# ---------------------------------------------------------------------------


def test_optional_none_fields_are_omitted_not_null() -> None:
    net = Net(id="net-1", name="N", nodes=(), current_a=None, voltage_v=None)
    doc = _minimal_document(nets=(net,))
    text = persist.serialize_document(doc)
    assert "null" not in text
    assert "currentA" not in text
    assert "voltageV" not in text


def test_optional_field_present_when_set() -> None:
    net = Net(id="net-1", name="N", nodes=(), current_a=0.5, voltage_v=5.0)
    doc = _minimal_document(nets=(net,))
    parsed = json.loads(persist.serialize_document(doc))
    assert parsed["nets"][0]["currentA"] == 0.5
    assert parsed["nets"][0]["voltageV"] == 5


# ---------------------------------------------------------------------------
# Deserialize: result shape, error codes, paths
# ---------------------------------------------------------------------------


def test_deserialize_never_raises_on_malformed_json() -> None:
    result = persist.deserialize_document("{ this is not json")
    assert result.ok is False
    assert result.code == "invalid-json"


def test_deserialize_rejects_nan_and_infinity_tokens_like_js() -> None:
    """Python's json module accepts the non-standard NaN/Infinity tokens by default;
    JS's JSON.parse does not. A hand-edited file containing a bare `NaN` must fail to
    parse the same way in both implementations.
    """
    text = json.dumps(_minimal_document_json()).replace('"pitch": 2.54', '"pitch": NaN')
    result = persist.deserialize_document(text)
    assert result.ok is False
    assert result.code == "invalid-json"


def test_format_too_new_is_a_distinct_error_code() -> None:
    doc = _minimal_document_json()
    doc["formatVersion"] = persist.CURRENT_FORMAT_VERSION + 1
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is False
    assert result.code == "format-too-new"
    assert result.path == "formatVersion"


def test_missing_field_error_has_precise_path() -> None:
    doc = _minimal_document_json()
    doc["components"] = [
        {
            "id": "cmp-1",
            # "ref" deliberately omitted
            "value": "10k",
            "footprintId": "r-axial-4",
            "anchor": {"col": 0, "row": 0},
            "rotation": 0,
            "mirrored": False,
            "locked": False,
        }
    ]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is False
    assert result.code == "missing-field"
    assert result.path == "components[0].ref"


def test_deep_error_path_points_at_offending_value() -> None:
    doc = _minimal_document_json()
    doc["components"] = [
        {
            "id": "cmp-1",
            "ref": "R1",
            "value": "10k",
            "footprintId": "r-axial-4",
            "anchor": {"col": "not-a-number", "row": 0},
            "rotation": 0,
            "mirrored": False,
            "locked": False,
        }
    ]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is False
    assert result.path == "components[0].anchor.col"


def test_null_field_is_invalid_type_not_missing_field() -> None:
    """A JSON `null` is a present value, not an absent key -- it must fail whichever
    field validator receives it (invalid-type), not be treated as though the key were
    never there.
    """
    doc = _minimal_document_json()
    doc["meta"]["name"] = None  # type: ignore[index]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is False
    assert result.code == "invalid-type"
    assert result.path == "meta.name"


def test_parse_document_or_throw_raises_with_code_and_path() -> None:
    doc = _minimal_document_json()
    del doc["board"]
    with pytest.raises(ValueError, match="missing-field"):
        persist.parse_document_or_throw(json.dumps(doc))


def test_parse_document_or_throw_returns_document_on_success() -> None:
    text = json.dumps(_minimal_document_json())
    document = persist.parse_document_or_throw(text)
    assert document.meta.name == "t"


# ---------------------------------------------------------------------------
# Warnings: unknown keys, solder-trace diagonal step
# ---------------------------------------------------------------------------


def test_unknown_key_is_a_warning_not_an_error() -> None:
    doc = _minimal_document_json()
    doc["board"]["extraField"] = 42  # type: ignore[index]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is True
    assert any("board.extraField" in w for w in result.warnings)


def test_solder_trace_diagonal_step_loads_with_warning_not_error() -> None:
    """A hand-edited file with a diagonal solder-trace step must still open -- the
    user is meant to see the problem in DRC, not be locked out of the project.
    """
    doc = _minimal_document_json()
    doc["conductors"] = [
        {
            "id": "cond-1",
            "kind": "solder-trace",
            "path": [{"col": 0, "row": 0}, {"col": 1, "row": 1}],  # diagonal step
            "side": "bottom",
            "layerZ": 0,
            "buildup": "normal",
        }
    ]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is True, f"diagonal solder-trace path must not be a hard error, got: {result!r}"
    assert len(result.document.conductors) == 1
    assert any("not 4-adjacent" in w or "orthogonal" in w for w in result.warnings), result.warnings


def test_solder_trace_orthogonal_path_has_no_warning() -> None:
    doc = _minimal_document_json()
    doc["conductors"] = [
        {
            "id": "cond-1",
            "kind": "solder-trace",
            "path": [{"col": 0, "row": 0}, {"col": 1, "row": 0}],
            "side": "bottom",
            "layerZ": 0,
            "buildup": "normal",
        }
    ]
    result = persist.deserialize_document(json.dumps(doc))
    assert result.ok is True
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# Round trip through hand-built documents (not just golden files)
# ---------------------------------------------------------------------------


def test_round_trip_solder_trace_with_spine() -> None:
    conductor = SolderTraceConductor(
        id="cond-1",
        path=(HoleCoord(0, 0), HoleCoord(1, 0), HoleCoord(2, 0)),
        buildup="heavy",
        spine=SpineSpec(material="tinned-copper", gauge=0.6),
    )
    doc = _minimal_document(conductors=(conductor,))
    text = persist.serialize_document(doc)
    result = persist.deserialize_document(text)
    assert result.ok is True
    assert persist.serialize_document(result.document) == text


def test_round_trip_is_idempotent_across_all_golden_files() -> None:
    """Serializing an already-round-tripped document a second time must be a no-op."""
    for perf_path in PERF_FILES:
        original = perf_path.read_text(encoding="utf-8")
        once = persist.parse_document_or_throw(original)
        twice = persist.parse_document_or_throw(persist.serialize_document(once))
        assert persist.serialize_document(once) == persist.serialize_document(twice), perf_path.name


# ---------------------------------------------------------------------------
# The build height limit -- a document-level scalar, optional like every field
# added after format version 1 was frozen
# ---------------------------------------------------------------------------


def test_no_height_limit_leaves_no_trace_in_the_file() -> None:
    """The same rule every field added since format 1 follows: a document that does not
    use it serializes to the bytes a build predating it wrote. This is what lets the 15
    golden fixtures keep round-tripping byte for byte."""
    assert "heightLimitMm" not in persist.serialize_document(_minimal_document())


def test_a_height_limit_survives_a_round_trip() -> None:
    doc = _minimal_document(height_limit_mm=18.5)
    text = persist.serialize_document(doc)
    result = persist.deserialize_document(text)

    assert result.ok is True
    assert not result.warnings
    assert result.document.height_limit_mm == 18.5
    assert persist.serialize_document(result.document) == text


def test_a_height_limit_nobody_can_build_under_warns_rather_than_refusing() -> None:
    """A hand-edited zero or negative would report every part on the board as too tall.
    Dropped with a warning, the same way a diagonal solder-trace step is: the user should
    see the problem, not be locked out of their project."""
    doc = _minimal_document_json()
    doc["heightLimitMm"] = 0

    result = persist.deserialize_document(json.dumps(doc))

    assert result.ok is True
    assert result.document.height_limit_mm is None
    assert any("heightLimitMm" in w for w in result.warnings)


def test_a_file_written_before_the_height_limit_existed_still_loads() -> None:
    doc = _minimal_document_json()
    assert "heightLimitMm" not in doc

    result = persist.deserialize_document(json.dumps(doc))

    assert result.ok is True
    assert result.document.height_limit_mm is None
