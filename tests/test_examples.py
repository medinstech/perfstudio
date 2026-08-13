"""The shipped examples must stay buildable.

These are the boards the README points a stranger at, so "it opens" is not enough: an
example that loads but no longer passes its own checks is worse than no example, because
somebody following it will conclude the tool is wrong about their board too.

Deliberately cheap. Each `.perf` is loaded and checked, and nothing is re-placed or
re-routed -- `tools/build_examples.py` does that, takes about half a minute, and is run
by hand when an example is regenerated. What is asserted here is the property that has to
hold on every commit: the documents on disk are valid, they still match their schematics,
and they carry no design error.
"""

from __future__ import annotations

import pathlib

import pytest

from perfstudio import persist
from perfstudio.drc import run_drc
from perfstudio.footprints import footprint_lookup
from perfstudio.guide import build_guide
from perfstudio.lvs import run_lvs
from perfstudio.parsers.kicad import parse_kicad_netlist

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"
PERF_FILES = sorted(EXAMPLES.glob("*.perf"))
NET_FILES = sorted(EXAMPLES.glob("*.net"))


def test_every_netlist_has_a_board() -> None:
    """The two halves of an example ship together, or the README links to nothing."""
    assert {p.stem for p in NET_FILES} == {p.stem for p in PERF_FILES}
    assert len(PERF_FILES) >= 4


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_example_loads_without_warnings(path: pathlib.Path) -> None:
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok, f"{path.name}: [{result.code}] {result.message}"
    # A warning here means a hand-edit that DRC will report -- legitimate in a user's
    # document (see validate_orthogonal_chain), never in one this repository ships.
    assert not result.warnings, f"{path.name}: {result.warnings}"


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_example_round_trips_byte_identical(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    result = persist.deserialize_document(text)
    assert result.ok
    assert persist.serialize_document(result.document) == text


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_example_has_no_drc_errors(path: pathlib.Path) -> None:
    """Warnings are allowed and expected -- the LM317 board carries an R5' proximity
    warning, which is the rule doing its job and becomes a checkpoint in the guide.
    An *error* is a board that cannot be built as drawn."""
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok
    errors = [v for v in run_drc(result.document, footprint_lookup()) if v.severity == "error"]
    assert not errors, [f"{v.rule}: {v.message}" for v in errors]


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_example_matches_its_schematic(path: pathlib.Path) -> None:
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok
    lvs = run_lvs(result.document, footprint_lookup())
    assert lvs.ok, [f"{i.kind}: {i.message}" for i in lvs.issues]


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_example_produces_a_guide_with_checkpoints(path: pathlib.Path) -> None:
    """The guide is the point of the tool, and checkpoints are the point of the guide."""
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok
    guide = build_guide(result.document, footprint_lookup())
    assert guide.total_steps > 0
    assert guide.checkpoint_count > 0


@pytest.mark.parametrize("path", NET_FILES, ids=lambda p: p.stem)
def test_netlist_parses(path: pathlib.Path) -> None:
    """Parses, and complains about nothing except the one thing every real export has.

    KiCad emits a net per unconnected pin (`unconnected-(U1-Pad4)`), and the importer
    skips them with a warning saying how many. That warning is the importer working, so
    it is allowed by name rather than by loosening the check to nothing.
    """
    parsed = parse_kicad_netlist(path.read_text(encoding="utf-8"))
    assert parsed.nets
    unexpected = [w for w in parsed.warnings if "unconnected net" not in w]
    assert not unexpected, unexpected


@pytest.mark.parametrize("path", PERF_FILES, ids=lambda p: p.stem)
def test_board_carries_every_part_the_netlist_names(path: pathlib.Path) -> None:
    """The `.perf` is built from the `.net` beside it, so a part cannot go missing from
    one without the other noticing."""
    result = persist.deserialize_document(path.read_text(encoding="utf-8"))
    assert result.ok
    parsed = parse_kicad_netlist((EXAMPLES / f"{path.stem}.net").read_text(encoding="utf-8"))
    wanted = {node.component_ref for net in parsed.nets for node in net.nodes}
    placed = {component.ref for component in result.document.components}
    assert wanted <= placed, f"missing from the board: {sorted(wanted - placed)}"
