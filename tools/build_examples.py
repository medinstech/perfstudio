"""Turn the example netlists into ready-to-open boards.

    python tools/build_examples.py            # rebuild every examples/*.perf
    python tools/build_examples.py --check     # verify only, write nothing

Each example ships twice: as the `.net` a schematic tool exports, and as the `.perf` that
importing, placing and routing it produces. The netlist alone would demonstrate the
importer; the board is what somebody wants to look at before installing anything.

The footprints are named here rather than left to ``ui.main.guess_footprint_id``, which
reads a reference designator and a pin count and can do no better than a first guess:
an LM317 is `U1` with three pins and guesses to a DIP-8, and a TO-220 regulator standing
in for an 8-pin DIP would make the heat-proximity rule and the 3D height check both
wrong. An example is the one place the answer should already be right.

Running this is also the closest thing the repository has to an end-to-end test of the
advertised workflow: import, place, route, check, export. It fails loudly if any example
stops routing cleanly, which is the point -- a broken example on the front page is worse
than no example.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from perfstudio import persist  # noqa: E402
from perfstudio.autoroute import plan_autoroute  # noqa: E402
from perfstudio.command import CommandBus, CommandContext  # noqa: E402
from perfstudio.commands import (  # noqa: E402
    ImportNetlistPayload,
    PlaceComponentPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.drc import run_drc  # noqa: E402
from perfstudio.footprints import footprint_lookup  # noqa: E402
from perfstudio.guide import build_guide  # noqa: E402
from perfstudio.lvs import run_lvs  # noqa: E402
from perfstudio.model import Board, DocumentMeta, HoleCoord  # noqa: E402
from perfstudio.parsers.kicad import parse_kicad_netlist  # noqa: E402
from perfstudio.placer import PlacementOptions, plan_placement  # noqa: E402

EXAMPLES = REPO_ROOT / "examples"

#: A fixed timestamp, because the engine has no clock and this script is a host. A real
#: date here would rewrite every example on every run and put the diff in the way of
#: whatever the commit was actually about.
STAMP = "2026-01-01T00:00:00Z"


class Example:
    """One example: its netlist, the board to put it on, and what each part really is."""

    def __init__(
        self,
        stem: str,
        title: str,
        cols: int,
        rows: int,
        footprints: dict[str, str],
        material: str = "FR4",
        seed: int = 0,
    ) -> None:
        self.stem = stem
        self.title = title
        self.cols = cols
        self.rows = rows
        self.footprints = footprints
        self.material = material
        self.seed = seed


CATALOGUE: tuple[Example, ...] = (
    Example(
        stem="ne555-astable",
        title="NE555 Astable",
        cols=32,
        rows=22,
        footprints={
            "U1": "dip-8",
            "R1": "r-axial-3",
            "R2": "r-axial-3",
            "R3": "r-axial-3",
            "C1": "c-elec-d5-p2",
            "C2": "c-disc-p2",
            "LED1": "led-5mm",
            "J1": "hdr-1x2",
        },
    ),
    Example(
        stem="lm317-supply",
        title="LM317 Adjustable Supply",
        cols=30,
        rows=20,
        footprints={
            # A TO-220 on its own, which is the whole reason this file names footprints:
            # the regulator is the hot part, and heat-proximity measures from its body
            # box. Guessed as a DIP-8 it would be neither the right shape nor the right
            # archetype, and the rule that matters most on this board would go quiet.
            "U1": "to220",
            "C1": "c-disc-p2",
            "C2": "c-elec-d5-p2",
            "C3": "c-elec-d6.3-p2",
            "R1": "r-axial-3",
            "R2": "r-axial-3",
            "RV1": "pot-3",
            "D1": "d-do41",
            "LED1": "led-5mm",
            "J1": "hdr-1x2",
            "J2": "hdr-1x2",
        },
    ),
    Example(
        stem="lpb1-booster",
        title="One-Transistor Guitar Booster",
        cols=24,
        rows=18,
        # FR-2 phenolic, deliberately: this is the board a pedal actually gets built on,
        # and it is the material whose pads lift. Choosing it here is what makes the
        # guide drop the iron temperature and DRC's pad-lifting rule speak up at all.
        material="FR2",
        footprints={
            "Q1": "to92",
            "R1": "r-axial-3",
            "R2": "r-axial-3",
            "R3": "r-axial-3",
            "R4": "r-axial-3",
            "C1": "c-film-p3",
            "C2": "c-elec-d5-p2",
            "C3": "c-elec-d6.3-p2",
            "RV1": "pot-3",
            "J1": "hdr-1x2",
            "J2": "hdr-1x2",
            "J3": "hdr-1x2",
        },
    ),
    Example(
        stem="arduino-io-shield",
        title="Arduino I/O Shield",
        cols=28,
        rows=20,
        footprints={
            "J1": "hdr-1x8",
            "J2": "hdr-1x6",
            "LED1": "led-5mm",
            "LED2": "led-5mm",
            "LED3": "led-5mm",
            "R1": "r-axial-3",
            "R2": "r-axial-3",
            "R3": "r-axial-3",
            "R4": "r-axial-3",
            "SW1": "sw-tactile",
            "C1": "c-disc-p2",
        },
    ),
)


def _board(example: Example) -> Board:
    return Board(
        type="pad-per-hole",
        cols=example.cols,
        rows=example.rows,
        pitch=2.54,
        thickness=1.6,
        material=example.material,  # type: ignore[arg-type]
        pad_diameter=1.9,
        drill_diameter=0.8,
    )


def build(example: Example, lookup, *, write: bool) -> bool:
    net_path = EXAMPLES / f"{example.stem}.net"
    parsed = parse_kicad_netlist(net_path.read_text(encoding="utf-8"))

    document = create_empty_document(
        DocumentMeta(name=example.title, created=STAMP, modified=STAMP),
        _board(example),
    )
    bus = CommandBus(
        document,
        create_standard_registry(),
        CommandContext(next_id=create_document_id_generator(document)),
    )

    # Parts first, then the netlist: importing nets that name a component which is not
    # on the board yet is legal but leaves the nets pointing at nothing, and the placer
    # would have no bodies to arrange.
    #
    # They go down in a column at the left edge and are immediately rearranged by the
    # placer, so the starting anchors only have to be legal, not good.
    refs = sorted({node.component_ref for net in parsed.nets for node in net.nodes})
    missing = [ref for ref in refs if ref not in example.footprints]
    if missing:
        print(f"  {example.stem}: netlist names {missing} with no footprint in CATALOGUE")
        return False

    col, row = 0, 0
    for ref in refs:
        footprint_id = example.footprints[ref]
        footprint = lookup(footprint_id)
        if footprint is None:
            print(f"  {example.stem}: no such footprint {footprint_id!r} for {ref}")
            return False
        result = bus.dispatch(
            "component.place",
            PlaceComponentPayload(
                ref=ref,
                value=next(
                    (c.value or "" for c in parsed.components if c.ref == ref), ""
                ),
                footprint_id=footprint_id,
                anchor=HoleCoord(col, row),
                id=f"c-{ref.lower()}",
            ),
        )
        if not result.ok:
            print(f"  {example.stem}: placing {ref} refused [{result.code}] {result.message}")
            return False
        row += 3
        if row >= example.rows - 2:
            row = 0
            col += 4

    result = bus.dispatch("netlist.import", ImportNetlistPayload(nets=parsed.nets))
    if not result.ok:
        print(f"  {example.stem}: netlist import refused [{result.code}] {result.message}")
        return False

    plan = plan_placement(bus.document, lookup, PlacementOptions(seed=example.seed))
    if not plan.is_empty:
        result = bus.dispatch("component.moveMany", plan.payload())
        if not result.ok:
            print(f"  {example.stem}: placement refused [{result.code}] {result.message}")
            return False

    plan = plan_autoroute(bus.document, lookup)
    if not plan.is_empty:
        result = bus.dispatch("conductor.addMany", plan.payload())
        if not result.ok:
            print(f"  {example.stem}: routing refused [{result.code}] {result.message}")
            return False

    document = bus.document
    violations = run_drc(document, lookup)
    errors = [v for v in violations if v.severity == "error"]
    lvs = run_lvs(document, lookup)
    guide = build_guide(document, lookup)

    routing = plan.summary
    status = (
        f"  {example.stem:20} {len(document.components):2} parts  "
        f"{len(document.conductors):2} conductors  "
        f"{routing.nets_closed}/{routing.nets_considered} nets closed  "
        f"DRC {len(errors)} err / {len(violations) - len(errors)} warn  "
        f"LVS {'ok' if lvs.ok else 'MISMATCH'}  "
        f"{guide.total_steps} steps / {guide.checkpoint_count} checks"
    )
    print(status)

    ok = not errors and lvs.ok and routing.links_unrouted == 0
    if not ok:
        for v in errors[:5]:
            print(f"      DRC error {v.rule}: {v.message}")
        for issue in list(lvs.issues)[:5]:
            print(f"      LVS {issue.kind}: {issue.message}")
        if routing.links_unrouted:
            print(f"      {routing.links_unrouted} connection(s) not routed")

    if write and ok:
        # The modified stamp is the host's job, and this host is deterministic on
        # purpose -- see STAMP.
        document = replace(document, meta=replace(document.meta, modified=STAMP))
        # newline="\n" explicitly: .gitattributes stores every text file as LF, and the
        # default on Windows would write CRLF, so the file on disk would differ from the
        # file in the repository the moment it was committed.
        (EXAMPLES / f"{example.stem}.perf").write_text(
            persist.serialize_document(document), encoding="utf-8", newline="\n"
        )

    return ok


def main(argv: list[str]) -> int:
    write = "--check" not in argv
    lookup = footprint_lookup()
    print(f"{'building' if write else 'checking'} {len(CATALOGUE)} examples\n")
    results = [build(example, lookup, write=write) for example in CATALOGUE]
    failed = results.count(False)
    print()
    if failed:
        print(f"{failed} of {len(results)} examples did NOT come out clean")
        return 1
    print(f"all {len(results)} examples route cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
