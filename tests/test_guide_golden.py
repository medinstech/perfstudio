"""The whole build guide, frozen (PLAN.md §7.6, §10).

WHAT THIS CATCHES AND WHY THERE WAS NOTHING. `test_guide.py` is thorough about the guide
*model* and asks the exporters targeted questions -- does the HTML mention the cut list,
does the JSON carry both spellings of a hole, does the BOM group on value. Every one of
those assertions names the thing it is looking for, so between them they cannot notice
what nobody thought to name: a phase that swapped places, a checkpoint that stopped being
generated, a sentence that lost its polarity warning, a BOM row that quietly vanished
when a footprint was renamed. The guide is the OUTPUT of this application -- the thing a
person prints and follows with an iron in their hand -- and nothing compared one against
a known-good one.

So the four exports of one real board are stored whole and compared whole. The board is
the NE555 fixture with the autorouter's output committed: 8 parts, 10 wires, a trace
spine, continuity and isolation checkpoints, a BOM, a cut list, and warnings. It exercises
every branch of `guide_export` that a pad-per-hole board can reach.

THIS IS NOT THE DIFFERENTIAL PROOF. `tools/diffcheck/golden/` holds the TypeScript
engine's output and the tests there assert "identical to the implementation we are
replacing". The TypeScript side never had a guide exporter, so there is nothing to be
differential against: these files are OUR output, blessed deliberately, in the same
spirit as `render_signatures.json`. That is why they live here and not there.

FLOATS ARE COMPARED AT 12 SIGNIFICANT DIGITS, not bit for bit, and the reason is written
down in `test_footprints.py`: this suite runs on three platforms and macOS arm64's libm
disagrees with x86-64's in the last ULP. The JSON emits full-precision lengths
(`20.478134680678316`, a diagonal through `math.hypot`), so a byte comparison would fail
there for a reason that has nothing to do with the guide. Twelve digits absorbs a ULP at
double precision -- about 1e-16 relative -- and still fails on any change a person could
care about. The other three formats print to one decimal place and are compared as text.

The VERSION is substituted out (`{VERSION}`) rather than dropped: the generator line is
part of the output and worth pinning, but it is not worth re-blessing four files on every
release. `test_version.py` is what guards the version itself.

To re-bless after a deliberate change: run this file with PERFSTUDIO_BLESS_GUIDE=1 and
READ THE DIFF. The point of the test is that the diff is readable -- if it is one wording
change, the change was one wording; if it is four hundred lines, something moved that was
not meant to.
"""

from __future__ import annotations

import difflib
import functools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from perfstudio import persist
from perfstudio.autoroute import plan_autoroute
from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import create_document_id_generator, create_standard_registry
from perfstudio.connectivity import FootprintLookup
from perfstudio.footprints import footprint_lookup
from perfstudio.guide import build_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.model import PerfDocument
from perfstudio.version import __version__

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "diffcheck" / "golden"
EXPECTED_DIR = Path(__file__).resolve().parent / "guide_golden"
REGISTRY: FootprintLookup = footprint_lookup()

#: Significant digits every float is reduced to before comparison. See the module
#: docstring: enough to absorb a libm ULP, far too few to hide a real change.
FLOAT_DIGITS = 12

#: How many lines of the first difference to show. Enough to see what moved without
#: printing a 1500-line file into the failure report.
DIFF_LINES = 40


def routed_ne555() -> PerfDocument:
    """The fixture with the autorouter's output committed, as `test_guide` builds it.

    Deterministic twice over: the fixture is a file, and the router's output on it is
    itself golden (`test_autoroute.py`), so a change here is a change in the guide and
    never a change in the input.
    """
    result = persist.deserialize_document(
        (GOLDEN_DIR / "ne555.perf").read_text(encoding="utf-8")
    )
    assert result.ok, result.message
    doc = result.document
    plan = plan_autoroute(doc, REGISTRY)
    bus = CommandBus(
        doc, create_standard_registry(), CommandContext(next_id=create_document_id_generator(doc))
    )
    dispatched = bus.dispatch("conductor.addMany", plan.payload())
    assert dispatched.ok, dispatched.message
    return bus.document


#: Named here rather than derived, so that adding a fifth export format and forgetting to
#: bless it fails ``test_every_export_is_covered`` instead of silently testing three.
EXPORTS = ("ne555_bom.csv", "ne555_cut_list.csv", "ne555_guide.html", "ne555_guide.json")


def build_exports() -> dict[str, str]:
    """Every format the guide is published in, from one build of one board."""
    guide = build_guide(routed_ne555(), REGISTRY)
    return {
        "ne555_guide.json": guide_to_json(guide),
        "ne555_guide.html": guide_to_html(guide),
        "ne555_cut_list.csv": cut_list_to_csv(guide),
        "ne555_bom.csv": bom_to_csv(guide),
    }


@functools.cache
def exports() -> dict[str, str]:
    """The same four, built once for the whole file: routing the board is the expensive
    part and it produces the same board every time -- which is what
    ``test_the_guide_is_the_same_guide_twice`` is there to keep true."""
    return build_exports()


def normalise(text: str) -> str:
    """Take out the two things that change without the guide changing."""
    return text.replace(__version__, "{VERSION}").replace("\r\n", "\n")


def _round_floats(node: Any) -> Any:
    if isinstance(node, float):
        return float(f"{node:.{FLOAT_DIGITS}g}")
    if isinstance(node, dict):
        return {key: _round_floats(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_round_floats(value) for value in node]
    return node


def comparable(name: str, text: str) -> str:
    """The form two runs of the same guide are compared in.

    JSON goes round through a parse so its floats can be rounded -- which also means key
    ORDER survives (dicts keep insertion order), so a reordered writer still fails. The
    other formats are already printed to one decimal and are their own comparable form.
    """
    if not name.endswith(".json"):
        return text
    return json.dumps(_round_floats(json.loads(text)), indent=2, ensure_ascii=False)


def _diff(name: str, expected: str, actual: str) -> str:
    lines = list(
        difflib.unified_diff(
            expected.splitlines(), actual.splitlines(), "stored", "produced", lineterm="", n=2
        )
    )
    shown = "\n".join(lines[:DIFF_LINES])
    if len(lines) > DIFF_LINES:
        shown += f"\n... and {len(lines) - DIFF_LINES} more lines of difference"
    return f"{name} is not what was blessed:\n{shown}"


@pytest.mark.parametrize("name", EXPORTS)
def test_the_guide_is_what_was_blessed(name: str) -> None:
    produced = normalise(exports()[name])
    stored = EXPECTED_DIR / name

    if os.environ.get("PERFSTUDIO_BLESS_GUIDE"):
        EXPECTED_DIR.mkdir(exist_ok=True)
        with stored.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(produced)
        pytest.skip(f"blessed {name}")

    assert stored.exists(), (
        f"no stored guide for {name}. Run with PERFSTUDIO_BLESS_GUIDE=1 to write one, "
        f"after reading what it contains."
    )
    expected = normalise(stored.read_text(encoding="utf-8"))
    want = comparable(name, expected)
    got = comparable(name, produced)
    assert got == want, _diff(name, want, got)


def test_every_export_is_covered() -> None:
    """A format nobody blessed is a format nobody is checking."""
    assert sorted(build_exports()) == sorted(EXPORTS)


def test_the_guide_is_the_same_guide_twice() -> None:
    """The engine is pure, so two builds of one document are one guide.

    Two real builds, not the cached one: a golden of something that varies between runs
    would just be the first run's noise, and this is what says it does not.
    """
    assert build_exports() == build_exports()


def test_every_export_carries_the_version_that_wrote_it() -> None:
    """Which the comparison above substitutes out, so something has to say it is there.

    Not every format: the two CSVs are meant to be pasted into a spreadsheet, and a
    provenance line at the top of one is a row somebody has to delete.
    """
    produced = exports()
    assert f"PerfStudio {__version__}" in produced["ne555_guide.json"]
    assert __version__ in produced["ne555_guide.html"]
    assert __version__ not in produced["ne555_bom.csv"]
    assert __version__ not in produced["ne555_cut_list.csv"]
