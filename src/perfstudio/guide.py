"""The soldering guide (PLAN.md Sec 7, milestone M5).

This is the module the project exists for. Every other part of the engine -- the
connectivity graph, the six conductor kinds, DRC's 0.6 mm proximity rule, LVS's
continuity and isolation lists, the router's own prose explanations -- produces material
that ends up here, turned into something a person can follow with an iron in one hand.

WHAT MAKES IT DIFFERENT FROM "SOLDER R1 HERE".

Three things, and they are the three PLAN.md Sec 3 named as the project's justification:

  IT SAYS WHERE. Every step names holes by address (``R3: C7 -> C11, 4 holes apart``),
  because that is how people talk about perfboard and it is the only unambiguous way to
  say where a part goes. The lead-bend pitch comes out of the same numbers.

  IT SAYS WHICH WAY ROUND. Polarity is read from the registry's own pin NAMES ('+', 'K',
  'A') rather than from a convention about pin 1, because the convention is not uniform:
  an electrolytic's pin 1 is its positive lead, an LED's pin 1 is its anode, and a
  diode's pin 1 is its cathode. Getting this backwards is the single commonest way a
  finished board turns out dead, so it is derived, never assumed.

  IT SAYS HOW TO CHECK. Each phase ends with measurements, and they are DERIVED, not
  generic advice. Continuity comes from the schematic's own nets. Isolation comes from
  DRC: every hole where a solder trace runs 0.6 mm from a different net (rule R5')
  becomes a specific probe -- "measure C7 to C8, must read open". So the risk the tool
  predicted and the measurement the user performs come from the same list, and the
  commonest perfboard failure is caught before the board is finished rather than after
  it does not work.

ORDERING (PLAN.md Sec 7.1). Nine phases, because a board is built in a physical order:
parts go in shortest-first (a tall part fitted early blocks the board from lying flat on
the bench while you solder the short ones), the solder side is done in blocks as each
group is fitted, long wires come after, and the ICs go in last -- heat and ESD.

WHAT IS NOT HERE YET, said plainly so nobody has to discover it: PLAN.md Sec 7.2 also
asks each step card to carry a rendered image -- the board with that one part
highlighted, from above and mirrored from the solder side. That needs the renderer, so
it belongs with M4's headless render path rather than here, and the text steps are
written to stand on their own until it lands. The 1:1 printable sheets already exist
(ui/export_pdf.py) and are what a builder holds against the board today.

PURE AND DETERMINISTIC: no I/O, no clock, no randomness, no Qt. The same document always
produces the same guide, which is what makes it diffable, testable and safe to generate
from an agent. Rendering to HTML/CSV/JSON lives in guide_export.py; nothing here knows
what the output looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .connectivity import FootprintLookup, PhysicalPinRef
from .drc import DEFAULT_DRC_OPTIONS, DrcOptions, DrcViolation, run_drc, trace_electrical
from .geometry import all_pin_holes, edge_connector_holes, format_hole, path_length_mm
from .lvs import continuity_checks, isolation_checks, run_lvs
from .model import (
    Board,
    BoardMaterial,
    BodyArchetype,
    ComponentInstance,
    Conductor,
    ConductorId,
    ConductorKind,
    Footprint,
    HoleCoord,
    Mm,
    Net,
    NetClass,
    PerfDocument,
    Rotation,
    SolderTraceConductor,
)

# ---------------------------------------------------------------------------
# Phases (PLAN.md Sec 7.1)
# ---------------------------------------------------------------------------

PhaseNumber: TypeAlias = Literal[0, 1, 2, 3, 4, 5, 6, 7, 8]

PHASE_TITLES: dict[PhaseNumber, str] = {
    0: "Preparation",
    1: "Lowest profile",
    2: "IC sockets",
    3: "Small bodies",
    4: "Medium bodies",
    5: "Tall and mechanical",
    6: "Solder side: traces and bare wire",
    7: "Long insulated wires",
    8: "Closing up",
}

PHASE_SUMMARIES: dict[PhaseNumber, str] = {
    0: "Cut the board, mark hole A1, and get the iron and the parts ready.",
    1: (
        "Anything that lies flat: axial resistors and diodes. These go first because a "
        "board with a tall part on it will not sit flat on the bench, and a part that "
        "cannot be pressed down solders at an angle."
    ),
    2: (
        "IC sockets. The ICs themselves go in at the very end -- heat and static are the "
        "two things that kill them, and both are avoidable by fitting them last."
    ),
    3: "Small bodies: ceramic and film capacitors, TO-92 transistors, LEDs.",
    4: "Medium bodies: electrolytics, TO-220 packages, crystals.",
    5: "Tall and mechanical: connectors, terminals, potentiometers, switches, relays.",
    6: (
        "Turn the board over. Solder traces and bare wire, worked one net at a time so "
        "each is finished and checked before the next crosses it."
    ),
    7: "Insulated wire, which may cross anything already on the solder side.",
    8: "Fit the ICs, run the final checks, and power up under current limit.",
}

#: Which phase each kind of body belongs in. Ordered by how tall it stands, because that
#: is what decides whether the board still lies flat while the next part is soldered.
PHASE_BY_ARCHETYPE: dict[BodyArchetype, PhaseNumber] = {
    "axial-cylinder": 1,
    "dip": 2,
    "disc-ceramic": 3,
    "box-film": 3,
    "to92": 3,
    "led-round": 3,
    "radial-electrolytic": 4,
    "to220": 4,
    "crystal-hc49": 4,
    "pin-header": 5,
    "screw-terminal": 5,
    "potentiometer": 5,
    "tactile-switch": 5,
    "relay-box": 5,
    "generic-box": 5,
}

#: Conductor kinds done on the solder side in phase 6, and in phase 7.
PHASE_BY_CONDUCTOR: dict[ConductorKind, PhaseNumber] = {
    "lead-bend": 6,
    "solder-trace": 6,
    "solder-trace-wired": 6,
    "bare-wire": 6,
    "strip": 6,
    "insulated-wire": 7,
    "top-jumper": 7,
}

# ---------------------------------------------------------------------------
# Physical constants and conventions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IronSettings:
    """What to set the iron to, and how long a pad may be held.

    Material-dependent and not a preference. FR-2 phenolic paper -- the cheap brown
    "pertinaks" most perfboard sold in Turkey is made of -- lifts pads under sustained
    heat far more readily than FR-4, which is the same fact PLAN.md Sec 5.2 R5'' turns
    into a DRC warning.
    """

    temperature_c: int
    max_dwell_s: float
    note: str


IRON_BY_MATERIAL: dict[BoardMaterial, IronSettings] = {
    "FR4": IronSettings(
        temperature_c=350,
        max_dwell_s=3.0,
        note="FR-4 tolerates heat well; the limit here is the component, not the board.",
    ),
    "FR2": IronSettings(
        temperature_c=320,
        max_dwell_s=2.0,
        note=(
            "Phenolic paper board. Its pads lift easily -- work in short touches, let a "
            "pad cool before returning to it, and never drag heat along a long run in one "
            "pass."
        ),
    ),
    "FR1": IronSettings(
        temperature_c=320,
        max_dwell_s=2.0,
        note=(
            "Phenolic paper board. Its pads lift easily -- work in short touches, let a "
            "pad cool before returning to it, and never drag heat along a long run in one "
            "pass."
        ),
    ),
}

#: Insulation colour by net class, the convention every schematic reader already knows.
COLOR_BY_NET_CLASS: dict[NetClass, str] = {"power": "red", "ground": "black"}

#: Signal colours, cycled by net order so the same board always assigns the same colours.
SIGNAL_COLORS: tuple[str, ...] = (
    "yellow", "green", "blue", "white", "orange", "violet", "grey", "brown",
)

#: Wire gauge by declared current, largest current first. Conservative: hookup wire in
#: free air, derated because a perfboard has no copper pour to spread heat into.
AWG_BY_CURRENT: tuple[tuple[float, int], ...] = ((5.0, 18), (3.0, 20), (1.5, 22), (0.0, 24))


@dataclass(frozen=True, slots=True)
class GuideOptions:
    """Everything the guide's arithmetic needs that is not in the document."""

    #: Added at each end of a wire for the bend around the board edge, in mm.
    bend_allowance_mm: Mm = 3.0
    #: Insulation stripped at each end, in mm.
    strip_length_mm: Mm = 5.0
    #: Solder traces at least this many pads long get an end-to-end resistance check.
    #: Short traces are not worth probing: the expected value is below what a hand
    #: multimeter resolves, so a "measurement" would be a ritual rather than a test.
    resistance_check_min_pads: int = 5
    #: Tolerance band quoted with a computed resistance, as a fraction. Wide on purpose:
    #: the buildup estimate is a fillet-volume guess, and a check nobody can pass is worse
    #: than no check.
    resistance_tolerance: float = 0.5
    #: Cap on isolation pairs carried into the guide, matching lvs.isolation_checks.
    isolation_cap: int = 40
    drc: DrcOptions = DEFAULT_DRC_OPTIONS


DEFAULT_GUIDE_OPTIONS = GuideOptions()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartStep:
    """Fit one component."""

    kind: Literal["part"]
    component_id: str
    ref: str
    value: str
    footprint_name: str
    archetype: BodyArchetype
    anchor: HoleCoord
    #: Pin number -> hole, in footprint pin order.
    pin_holes: tuple[tuple[str, HoleCoord], ...]
    #: "C7 -> C11, 4 holes apart", or just the hole for a single-pin part.
    span: str
    #: Lead pitch to bend to, for a part whose two leads sit in line. None otherwise.
    bend_template_mm: Mm | None
    rotation: Rotation
    mirrored: bool
    height_mm: Mm
    #: "Cathode (banded end) at D12" -- None when the part is symmetrical.
    polarity: str | None
    notes: tuple[str, ...]

    @property
    def title(self) -> str:
        value = f" {self.value}" if self.value else ""
        return f"{self.ref}{value} — {self.span}"


@dataclass(frozen=True, slots=True)
class RiskNote:
    """One R5' proximity risk: a place where solder can bridge to the wrong net."""

    hole: HoleCoord
    neighbour: HoleCoord
    message: str


@dataclass(frozen=True, slots=True)
class WireCut:
    """One length of wire to cut, for the cut list (PLAN.md Sec 7.3)."""

    conductor_id: ConductorId
    net_name: str
    from_hole: HoleCoord
    to_hole: HoleCoord
    #: Routed path length on the board.
    path_mm: Mm
    #: What to cut: path + both bends through the board + both stripped ends.
    cut_mm: Mm
    strip_mm: Mm
    awg: int
    color: str
    insulated: bool


@dataclass(frozen=True, slots=True)
class SpineCut:
    """One length of tinned wire laid along a solder trace as its spine."""

    conductor_id: ConductorId
    net_name: str
    pads: int
    length_mm: Mm
    gauge_mm: Mm
    material: str


@dataclass(frozen=True, slots=True)
class ConductorStep:
    """Make one connection."""

    kind: Literal["conductor"]
    conductor_id: ConductorId
    conductor_kind: ConductorKind
    net_name: str
    net_class: NetClass
    #: "B12 -> K12, 10 pads" for a trace; "B3 -> P9" for a wire.
    span: str
    path: tuple[HoleCoord, ...]
    pads: int
    length_mm: Mm
    #: Traces only: the electrical summary quoted back as an expectation.
    resistance_ohm: float | None
    drop_mv: float | None
    loss_mw: float | None
    spine: SpineCut | None
    cut: WireCut | None
    risks: tuple[RiskNote, ...]
    notes: tuple[str, ...]

    @property
    def title(self) -> str:
        return f"{self.net_name}: {self.span}"


GuideStep: TypeAlias = PartStep | ConductorStep


# ---------------------------------------------------------------------------
# Checkpoints -- the differentiator (PLAN.md Sec 7.5)
# ---------------------------------------------------------------------------

CheckKind: TypeAlias = Literal[
    "continuity",  # these two points must be joined
    "isolation",  # these two points must NOT be joined
    "resistance",  # this run should measure about this much
    "polarity",  # look at it before power
    "power-on",  # the procedure itself
]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    kind: CheckKind
    title: str
    instruction: str
    expected: str
    #: Where to put the probes, when the check is a measurement.
    holes: tuple[HoleCoord, ...] = ()
    pins: tuple[PhysicalPinRef, ...] = ()
    #: True for checks that must pass before power is applied. Rendered as a hard gate.
    blocking: bool = False


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BomLine:
    ref_designators: tuple[str, ...]
    value: str
    footprint_name: str
    quantity: int

    @property
    def refs(self) -> str:
        return ", ".join(self.ref_designators)


@dataclass(frozen=True, slots=True)
class GuidePhase:
    number: PhaseNumber
    title: str
    summary: str
    steps: tuple[GuideStep, ...]
    checkpoints: tuple[Checkpoint, ...]

    @property
    def is_empty(self) -> bool:
        return not self.steps and not self.checkpoints


@dataclass(frozen=True, slots=True)
class GuideWarning:
    """Something the guide could not do, said out loud rather than omitted.

    A build guide that quietly leaves out the connections it could not describe is worse
    than no guide: the user follows it to the end and has a board that does not work,
    with nothing to say which part was never covered.
    """

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Guide:
    board: Board
    document_name: str
    phases: tuple[GuidePhase, ...]
    bom: tuple[BomLine, ...]
    cut_list: tuple[WireCut, ...]
    spine_list: tuple[SpineCut, ...]
    iron: IronSettings
    tools: tuple[str, ...]
    warnings: tuple[GuideWarning, ...]
    #: Counts, for a summary line and for tests that care about coverage.
    part_steps: int
    conductor_steps: int
    checkpoint_count: int

    @property
    def total_steps(self) -> int:
        return self.part_steps + self.conductor_steps


# ---------------------------------------------------------------------------
# Building it
# ---------------------------------------------------------------------------


def build_guide(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: GuideOptions = DEFAULT_GUIDE_OPTIONS,
) -> Guide:
    """Turn a finished board into something someone can build from.

    Works on whatever the document has. A board with no netlist still gets part and
    conductor steps; it just cannot get continuity checks, because those are derived from
    schematic intent and there is none. Every such gap is reported in ``warnings`` rather
    than silently producing a shorter guide.
    """
    violations = run_drc(doc, lookup, options.drc)
    nets_by_id: dict[str, Net] = {net.id: net for net in doc.nets}
    color_by_net = _assign_colors(doc)

    part_steps = _part_steps(doc, lookup, violations)
    conductor_steps, cut_list, spine_list = _conductor_steps(
        doc, nets_by_id, color_by_net, violations, options
    )

    by_phase: dict[PhaseNumber, list[GuideStep]] = {n: [] for n in PHASE_TITLES}
    for part_step, part_phase in part_steps:
        by_phase[part_phase].append(part_step)
    for conductor_step, conductor_phase in conductor_steps:
        by_phase[conductor_phase].append(conductor_step)

    checkpoints = _checkpoints(doc, lookup, violations, conductor_steps, options)

    phases = tuple(
        GuidePhase(
            number=number,
            title=PHASE_TITLES[number],
            summary=_phase_summary(number, doc),
            steps=tuple(by_phase[number]),
            checkpoints=tuple(checkpoints.get(number, ())),
        )
        for number in sorted(PHASE_TITLES)
    )

    all_checks = sum(len(phase.checkpoints) for phase in phases)
    return Guide(
        board=doc.board,
        document_name=doc.meta.name,
        phases=phases,
        bom=_bom(doc, lookup),
        cut_list=tuple(cut_list),
        spine_list=tuple(spine_list),
        iron=IRON_BY_MATERIAL[doc.board.material],
        tools=_tools(doc, cut_list, spine_list),
        warnings=_warnings(doc, lookup, violations),
        part_steps=len(part_steps),
        conductor_steps=len(conductor_steps),
        checkpoint_count=all_checks,
    )


def _phase_summary(number: PhaseNumber, doc: PerfDocument) -> str:
    """The phase's standing summary, with what the BOARD changes about it folded in.

    Only phase 0 varies today, and it varies for a reason worth stating: every hole
    address in this guide is counted from a corner unless the board says otherwise, so
    "mark hole A1" is the step the whole document depends on. A board that prints its own
    A..Z / 01..NN legend has already done it, and telling somebody to mark a corner that
    is already labelled is how a guide starts feeling like it was not written for the
    board in front of them.
    """
    if number != 0:
        return PHASE_SUMMARIES[number]

    board = doc.board
    parts: list[str] = ["Cut the board"]
    if doc.mounting_holes:
        count = len(doc.mounting_holes)
        sizes = ", ".join(f"{d:g} mm" for d in sorted({m.diameter for m in doc.mounting_holes}))
        where = ", ".join(
            format_hole(m.at)
            for m in sorted(doc.mounting_holes, key=lambda m: (m.at.row, m.at.col))
        )
        # Parenthesised rather than run on with a dash: this clause sits inside a comma
        # list, and a second comma list beside it reads as one long ambiguous string.
        parts.append(
            f"drill the {count} mounting {'hole' if count == 1 else 'holes'} ({sizes} at {where})"
        )
    parts.append(
        "mark hole A1"
        if board.labels is None
        else "check which corner the board's printed A1 is in"
    )
    parts.append("get the iron and the parts ready")

    summary = ", ".join(parts[:-1]) + f", and {parts[-1]}."
    summary = summary[0].upper() + summary[1:]

    if doc.mounting_holes:
        # Its own sentence, because it is an instruction about ORDER rather than another
        # thing to do: swarf brushes off a bare board and digs out of a finished one, and
        # a board that still has to go in a vice should not have parts on it yet.
        summary += (
            " Drill before anything is soldered — the board can still go in a vice, and "
            "swarf brushes off a bare board instead of having to be picked out of a built one."
        )

    if board.pad_shape == "oblong" and board.pad_length is not None:
        along = "along a row" if board.pad_axis == "horizontal" else "down a column"
        across = "down a column" if board.pad_axis == "horizontal" else "along a row"
        summary += (
            f" This board's pads are oblong ({board.pad_length:g} × {board.pad_diameter:g} mm), "
            f"so neighbouring pads {along} nearly touch while pads {across} are well clear. "
            f"Solder flows between them far more easily in the first direction than the "
            f"second — which is what makes the traces below quick, and what makes an "
            f"accidental bridge {along} quick too."
        )
    return summary


# -- parts ------------------------------------------------------------------


def _part_steps(
    doc: PerfDocument, lookup: FootprintLookup, violations: list[DrcViolation]
) -> list[tuple[PartStep, PhaseNumber]]:
    """One step per placed component, in build order.

    Within a phase, shortest first and then by reference: the height rule is the one that
    matters physically, and the reference is what makes the order stable rather than
    dependent on document order.
    """
    extra_notes = _part_notes_from_drc(doc, violations)
    entries: list[tuple[PhaseNumber, Mm, str, PartStep]] = []
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue  # Reported as a warning; nothing can be said about where it goes.
        step = _part_step(
            component,
            footprint,
            doc.board,
            doc.height_limit_mm,
            extra_notes.get(component.id, ()),
        )
        phase = PHASE_BY_ARCHETYPE.get(footprint.body.archetype, 5)
        entries.append((phase, footprint.body_height, component.ref, step))

    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [(step, phase) for phase, _height, _ref, step in entries]


def _part_notes_from_drc(
    doc: PerfDocument, violations: list[DrcViolation]
) -> dict[str, tuple[str, ...]]:
    """The DRC findings that change how a part is FITTED, moved onto that part's step.

    Only the two that are instructions rather than complaints. Everything else DRC says
    is about the design and belongs in the warning list, not in the middle of a step
    somebody is following with an iron in their hand.
    """
    ref_by_id = {component.id: component.ref for component in doc.components}
    notes: dict[str, list[str]] = {}

    for violation in violations:
        if violation.rule == "jumper-under-body" and violation.component_ids:
            jumpers = ", ".join(violation.conductor_ids)
            notes.setdefault(violation.component_ids[0], []).append(
                f"Jumper {jumpers} runs underneath this part, so it has to be soldered "
                f"first — it is in phase 1 for that reason. Check it is down and lying "
                f"flat before this goes in."
            )
        elif violation.rule == "heat-proximity" and len(violation.component_ids) == 2:
            source_ref = ref_by_id.get(violation.component_ids[0], "the part next to it")
            # On the part that suffers, not the one that gets hot: this is advice about
            # which capacitor to reach for, and it is only useful while fitting that one.
            notes.setdefault(violation.component_ids[1], []).append(
                f"{source_ref} runs hot and sits close by. Fit a 105 °C-rated part here "
                f"if you have one — an 85 °C electrolytic beside a heatsink is the first "
                f"thing on the board to dry out."
            )

    return {component_id: tuple(lines) for component_id, lines in notes.items()}


def _part_step(
    component: ComponentInstance,
    footprint: Footprint,
    board: Board,
    height_limit_mm: Mm | None = None,
    extra_notes: tuple[str, ...] = (),
) -> PartStep:
    holes = all_pin_holes(component, footprint)
    pin_holes = tuple((pin.number, at) for pin, at in holes)

    notes: list[str] = []
    if footprint.body.archetype == "dip":
        notes.append(
            "Fit a socket here if you have one. If you are soldering the IC directly, "
            "leave it until phase 8 -- it is the last thing that should meet an iron."
        )
    if component.mirrored:
        notes.append(
            "This part is mirrored: it goes in from the SOLDER side, not the component "
            "side. Check the pin order against the board before soldering."
        )
    if height_limit_mm is not None and footprint.body_height > height_limit_mm:
        notes.append(
            f"{footprint.body_height:g} mm tall, and this build has {height_limit_mm:g} mm "
            f"of room. It will not fit as it stands — lay it down or swap it before you "
            f"solder it in."
        )
    elif footprint.body_height >= 10:
        # The guess, kept only for a board that has not said what it has to fit inside.
        # Once there is a real number the real number wins.
        notes.append(
            f"{footprint.body_height:.0f} mm tall — check it clears anything meant to go "
            "over the board before you solder it down."
        )
    notes.extend(extra_notes)

    return PartStep(
        kind="part",
        component_id=component.id,
        ref=component.ref,
        value=component.value,
        footprint_name=footprint.name,
        archetype=footprint.body.archetype,
        anchor=component.anchor,
        pin_holes=pin_holes,
        span=_span_of(pin_holes),
        bend_template_mm=_bend_template(pin_holes, board),
        rotation=component.rotation,
        mirrored=component.mirrored,
        height_mm=footprint.body_height,
        polarity=_polarity_note(footprint, pin_holes),
        notes=tuple(notes),
    )


def _span_of(pin_holes: tuple[tuple[str, HoleCoord], ...]) -> str:
    """Where the part sits, in the form that is useful for THAT part.

    Two leads get "C7 → C11, 4 holes apart", which is a thing you can set a pair of
    pliers to. A DIP gets the rectangle it occupies and its pin count, because pin 1 to
    pin 8 is a diagonal and "3 holes apart" would be a true statement that helps nobody.
    """
    if not pin_holes:
        return "no pins"
    if len(pin_holes) == 1:
        return format_hole(pin_holes[0][1])

    cols = [at.col for _n, at in pin_holes]
    rows = [at.row for _n, at in pin_holes]
    if len(pin_holes) == 2:
        first, last = pin_holes[0][1], pin_holes[1][1]
        steps = max(max(cols) - min(cols), max(rows) - min(rows))
        return f"{format_hole(first)} → {format_hole(last)}, {steps} holes apart"

    corner_a = HoleCoord(min(cols), min(rows))
    corner_b = HoleCoord(max(cols), max(rows))
    pin_one = dict(pin_holes).get("1")
    where = f" with pin 1 at {format_hole(pin_one)}" if pin_one is not None else ""
    return (
        f"{format_hole(corner_a)}–{format_hole(corner_b)}, "
        f"{len(pin_holes)} pins{where}"
    )


def _bend_template(pin_holes: tuple[tuple[str, HoleCoord], ...], board: Board) -> Mm | None:
    """The pitch to bend an axial part's leads to, in mm.

    Only for two-lead parts in a straight line -- the case where "bend the leads to
    10.16 mm" is a thing you can do with a pair of pliers and a ruler. A three-pin
    transistor has a fixed lead spread and nothing to bend to.
    """
    if len(pin_holes) != 2:
        return None
    a, b = pin_holes[0][1], pin_holes[1][1]
    if a.col != b.col and a.row != b.row:
        return None
    steps = abs(a.col - b.col) + abs(a.row - b.row)
    return steps * board.pitch if steps else None


#: Pin names the registry uses where the meaning is not obvious, mapped to what to say.
#: Read from the NAME, never inferred from pin 1: an electrolytic's pin 1 is '+', an
#: LED's pin 1 is 'A' (the anode), and a diode's pin 1 is its cathode. One convention
#: cannot cover all three, and guessing wrong here is what makes a finished board dead.
_PIN_NAME_MEANING: dict[str, str] = {
    "+": "Positive (long) lead",
    "-": "Negative lead — the side with the printed stripe",
    "A": "Anode (long lead)",
    "K": "Cathode (short lead, flat on the rim)",
}


def _polarity_note(
    footprint: Footprint, pin_holes: tuple[tuple[str, HoleCoord], ...]
) -> str | None:
    """How to orient this part, in words, or None if it does not matter."""
    by_number = dict(pin_holes)

    named = [
        f"{_PIN_NAME_MEANING[pin.name]} in {format_hole(by_number[pin.number])}"
        for pin in footprint.pins
        if pin.name in _PIN_NAME_MEANING and pin.number in by_number
    ]
    if named:
        return "; ".join(named)

    if footprint.body.archetype == "dip":
        first = by_number.get("1")
        return f"Pin 1 (the notched end, marked with a dot) in {format_hole(first)}" if first else None

    if footprint.polarized:
        # Unnamed but polarized: a diode, where pin 1 is the cathode by the convention
        # this registry and KiCad's DO-41 both follow.
        first = by_number.get("1")
        if first is not None:
            return f"Cathode — the banded end — in {format_hole(first)}"

    if footprint.body.archetype in ("to92", "to220"):
        first = by_number.get("1")
        if first is not None:
            return f"Pin 1 in {format_hole(first)}; check the package outline against the board"
    return None


# -- conductors -------------------------------------------------------------


def _conductor_steps(
    doc: PerfDocument,
    nets_by_id: dict[str, Net],
    color_by_net: dict[str, str],
    violations: list[DrcViolation],
    options: GuideOptions,
) -> tuple[list[tuple[ConductorStep, PhaseNumber]], list[WireCut], list[SpineCut]]:
    """One step per conductor, grouped by net and ordered ground, power, then signal.

    The same criticality order the router works in (PLAN.md Sec 6.2), for the same
    reason turned around: the rails are the connections everything else is measured
    against, so they want to exist and be verified first.
    """
    risks_by_conductor = _risks_by_conductor(violations)
    trapped = trapped_jumper_ids(violations)
    class_order: dict[NetClass, int] = {"ground": 0, "power": 1, "signal": 2}

    entries: list[tuple[PhaseNumber, int, str, str, ConductorStep]] = []
    cut_list: list[WireCut] = []
    spine_list: list[SpineCut] = []

    for conductor in doc.conductors:
        net = nets_by_id.get(conductor.net_id) if conductor.net_id else None
        net_name = net.name if net is not None else "(unassigned)"
        net_class: NetClass = net.net_class if net is not None else "signal"
        current_a = net.current_a if net is not None else None

        step = _conductor_step(
            conductor,
            doc.board,
            net_name,
            net_class,
            current_a,
            color_by_net.get(conductor.net_id or "", "grey"),
            risks_by_conductor.get(conductor.id, ()),
            options,
        )
        if step.cut is not None:
            cut_list.append(step.cut)
        if step.spine is not None:
            spine_list.append(step.spine)

        phase = PHASE_BY_CONDUCTOR.get(conductor.kind, 6)
        if conductor.id in trapped:
            # A top jumper normally goes in at phase 7, soldered to pins that are already
            # fitted. One that runs UNDER a part cannot: by phase 7 the part is on the
            # board and the wire has nowhere to go. So the ones DRC flagged move to phase
            # 1, which is where PLAN.md Sec 7.1 puts top-side jumpers in the first place,
            # and the rest stay where soldering to a fitted pin is easier.
            phase = 1
        entries.append((phase, class_order[net_class], net_name, conductor.id, step))

    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
    return (
        [(step, phase) for phase, _order, _name, _id, step in entries],
        cut_list,
        spine_list,
    )


def _conductor_step(
    conductor: Conductor,
    board: Board,
    net_name: str,
    net_class: NetClass,
    current_a: float | None,
    color: str,
    risks: tuple[RiskNote, ...],
    options: GuideOptions,
) -> ConductorStep:
    path = conductor.path
    pads = len(path)
    length_mm = path_length_mm(path, board)
    notes: list[str] = []

    resistance = drop_mv = loss_mw = None
    spine: SpineCut | None = None
    cut: WireCut | None = None

    if isinstance(conductor, SolderTraceConductor):
        electrical = trace_electrical(conductor, board, current_a, options.drc)
        resistance = electrical.resistance_ohm
        drop_mv = electrical.drop_v * 1000 if electrical.drop_v is not None else None
        loss_mw = electrical.loss_w * 1000 if electrical.loss_w is not None else None
        if conductor.spine is not None:
            spine = SpineCut(
                conductor_id=conductor.id,
                net_name=net_name,
                pads=pads,
                length_mm=length_mm,
                gauge_mm=conductor.spine.gauge,
                material=conductor.spine.material.replace("-", " "),
            )
            notes.append(
                f"Lay the {conductor.spine.gauge} mm {spine.material} along the pads first "
                "and solder it at each one. Work in one direction and do not go back over "
                "a section that has cooled."
            )
        elif pads >= 3:
            notes.append(
                "Consider laying a lead offcut along these pads as a spine rather than "
                "building the run out of solder alone: it drops the resistance by roughly "
                "an order of magnitude and is far easier to make repeatable."
            )
        notes.append(
            "Tin each pad lightly first, then join them. Flux is not optional on a run "
            "this long."
        )
    elif conductor.kind in ("bare-wire", "insulated-wire", "top-jumper"):
        insulated = conductor.kind != "bare-wire"
        cut = _wire_cut(
            conductor.id, net_name, path, board, current_a, color, insulated, options
        )
        if conductor.kind == "top-jumper":
            notes.append(
                "This one runs over the COMPONENT side, not the solder side. Keep it clear "
                "of anything that has to be reachable later."
            )
        if conductor.kind == "bare-wire":
            notes.append(
                "Bare wire: it must not touch any other conductor along its length. Keep it "
                "flat against the board and check it against the neighbouring runs."
            )
    elif conductor.kind == "lead-bend":
        notes.append(
            "No extra wire: bend the component's own lead over to reach the second hole, "
            "and solder both ends."
        )

    return ConductorStep(
        kind="conductor",
        conductor_id=conductor.id,
        conductor_kind=conductor.kind,
        net_name=net_name,
        net_class=net_class,
        span=_conductor_span(conductor.kind, path, pads),
        path=path,
        pads=pads,
        length_mm=length_mm,
        resistance_ohm=resistance,
        drop_mv=drop_mv,
        loss_mw=loss_mw,
        spine=spine,
        cut=cut,
        risks=risks,
        notes=tuple(notes),
    )


def _conductor_span(kind: ConductorKind, path: tuple[HoleCoord, ...], pads: int) -> str:
    if not path:
        return "(empty path)"
    ends = f"{format_hole(path[0])} → {format_hole(path[-1])}"
    if kind in ("solder-trace", "solder-trace-wired", "strip"):
        return f"{ends}, {pads} pads"
    return ends


def _wire_cut(
    conductor_id: ConductorId,
    net_name: str,
    path: tuple[HoleCoord, ...],
    board: Board,
    current_a: float | None,
    color: str,
    insulated: bool,
    options: GuideOptions,
) -> WireCut:
    """Cut length, per PLAN.md Sec 7.3.

    ``path + 2 x (board thickness + bend allowance) + 2 x strip length``. The board
    thickness is in there because the wire goes THROUGH the board at each end and has to
    turn over on the far side; leaving it out is how a cut list produces wires that are
    each 4 mm too short.
    """
    path_mm = path_length_mm(path, board)
    # Bare wire is not stripped, because there is nothing on it to strip. Charging it the
    # allowance anyway would pad every bare run by a centimetre.
    strip_mm = options.strip_length_mm if insulated else 0.0
    ends = 2 * (board.thickness + options.bend_allowance_mm) + 2 * strip_mm
    awg = next(gauge for threshold, gauge in AWG_BY_CURRENT if (current_a or 0.0) >= threshold)
    return WireCut(
        conductor_id=conductor_id,
        net_name=net_name,
        from_hole=path[0],
        to_hole=path[-1],
        path_mm=path_mm,
        cut_mm=path_mm + ends,
        strip_mm=strip_mm,
        awg=awg,
        color=color,
        insulated=insulated,
    )


def _assign_colors(doc: PerfDocument) -> dict[str, str]:
    """Insulation colour per net: red for power, black for ground, then a fixed cycle.

    Assigned by net order in the document so the same board always produces the same cut
    list -- a colour convention that changed between two runs would be worse than none.
    """
    colors: dict[str, str] = {}
    signal_index = 0
    for net in doc.nets:
        fixed = COLOR_BY_NET_CLASS.get(net.net_class)
        if fixed is not None:
            colors[net.id] = fixed
        else:
            colors[net.id] = SIGNAL_COLORS[signal_index % len(SIGNAL_COLORS)]
            signal_index += 1
    return colors


def trapped_jumper_ids(violations: list[DrcViolation]) -> frozenset[ConductorId]:
    """Top jumpers DRC found running under a part, read off the rule rather than
    recomputed.

    Public because both the phase order and the part-step note need the same answer, and
    a guide that worked it out its own way would eventually schedule a jumper for after
    the part it has to go under while printing a note saying it must come first.
    """
    return frozenset(
        conductor_id
        for violation in violations
        if violation.rule == "jumper-under-body"
        for conductor_id in violation.conductor_ids
    )


def _risks_by_conductor(violations: list[DrcViolation]) -> dict[ConductorId, tuple[RiskNote, ...]]:
    """R5' proximity warnings, indexed by the trace that causes them.

    This is the join that makes PLAN.md Sec 7.5 work: the risk DRC predicts and the
    measurement the user performs come from one list, so they cannot drift apart.
    """
    by_conductor: dict[ConductorId, list[RiskNote]] = {}
    for violation in violations:
        if violation.rule != "solder-trace-proximity" or len(violation.holes) < 2:
            continue
        for conductor_id in violation.conductor_ids:
            by_conductor.setdefault(conductor_id, []).append(
                RiskNote(
                    hole=violation.holes[0],
                    neighbour=violation.holes[1],
                    message=(
                        f"{format_hole(violation.holes[0])} sits about "
                        f"one pad gap from {format_hole(violation.holes[1])}, which is on "
                        "another net. Do not let solder run across."
                    ),
                )
            )
    return {key: tuple(value) for key, value in by_conductor.items()}


# -- checkpoints ------------------------------------------------------------


def _checkpoints(
    doc: PerfDocument,
    lookup: FootprintLookup,
    violations: list[DrcViolation],
    conductor_steps: list[tuple[ConductorStep, PhaseNumber]],
    options: GuideOptions,
) -> dict[PhaseNumber, list[Checkpoint]]:
    """Every measurement, attached to the phase after which it can be made.

    A continuity check goes with the phase that finishes its net -- checking earlier
    would fail for a reason that is not a fault, and checking later buries the fault
    among a dozen others. Working out which phase that is is the only interesting part.
    """
    checks: dict[PhaseNumber, list[Checkpoint]] = {n: [] for n in PHASE_TITLES}

    # Isolation, from the R5' risks: each becomes a probe right after its trace is made.
    for step, phase in conductor_steps:
        for risk in step.risks:
            checks[phase].append(
                Checkpoint(
                    kind="isolation",
                    title=f"{format_hole(risk.hole)} ↔ {format_hole(risk.neighbour)} must be open",
                    instruction=(
                        f"With the trace for {step.net_name} finished, measure resistance "
                        f"between {format_hole(risk.hole)} and {format_hole(risk.neighbour)}."
                    ),
                    expected="Open circuit. Any reading at all means solder has bridged them.",
                    holes=(risk.hole, risk.neighbour),
                )
            )

    # Continuity, from the schematic. Each goes with the phase that completes its net.
    last_phase_for_net = _last_phase_by_net(conductor_steps)
    for check in continuity_checks(doc):
        closing_phase = last_phase_for_net.get(check.net_name)
        if closing_phase is None:
            continue  # Nothing on the board builds this net; the LVS warning covers it.
        checks[closing_phase].append(
            Checkpoint(
                kind="continuity",
                title=f"{check.a.component_ref}.{check.a.pin} ↔ {check.b.component_ref}.{check.b.pin}",
                instruction=(
                    f"Probe {check.a.component_ref} pin {check.a.pin} and "
                    f"{check.b.component_ref} pin {check.b.pin}."
                ),
                expected=f"Continuous — they are both on net {check.net_name}.",
                pins=(check.a, check.b),
            )
        )

    # End-to-end resistance on the long runs: catches a cold joint or a crack, which
    # continuity alone will happily read through.
    for step, phase in conductor_steps:
        if step.resistance_ohm is None or step.pads < options.resistance_check_min_pads:
            continue
        milliohm = step.resistance_ohm * 1000
        band = options.resistance_tolerance
        checks[phase].append(
            Checkpoint(
                kind="resistance",
                title=f"{step.net_name} run {step.span}: about {milliohm:.1f} mΩ end to end",
                instruction=(
                    f"Measure between {format_hole(step.path[0])} and "
                    f"{format_hole(step.path[-1])}. Use four-wire mode if your meter has it; "
                    "otherwise subtract the reading with the probes touched together."
                ),
                expected=(
                    f"About {milliohm:.1f} mΩ (accept {milliohm * (1 - band):.1f}–"
                    f"{milliohm * (1 + band):.1f} mΩ). Much higher means a cold joint or a "
                    "crack in the run."
                ),
                holes=(step.path[0], step.path[-1]),
            )
        )

    checks[8].extend(_closing_checks(doc, lookup, options))
    del violations
    return checks


def _last_phase_by_net(
    conductor_steps: list[tuple[ConductorStep, PhaseNumber]],
) -> dict[str, PhaseNumber]:
    last: dict[str, PhaseNumber] = {}
    for step, phase in conductor_steps:
        current = last.get(step.net_name)
        if current is None or phase > current:
            last[step.net_name] = phase
    return last


def _closing_checks(
    doc: PerfDocument, lookup: FootprintLookup, options: GuideOptions
) -> list[Checkpoint]:
    """Phase 8: everything that must be true before power is applied."""
    checks: list[Checkpoint] = []

    for check in isolation_checks(doc)[: options.isolation_cap]:
        blocking = {check.net_a, check.net_b} and _is_power_pair(doc, check.net_a, check.net_b)
        checks.append(
            Checkpoint(
                kind="isolation",
                title=f"{check.net_a} ↔ {check.net_b} must be separate",
                instruction=(
                    f"Probe {check.a.component_ref} pin {check.a.pin} ({check.net_a}) and "
                    f"{check.b.component_ref} pin {check.b.pin} ({check.net_b})."
                ),
                expected="Open, or at least the circuit's own resistance — never a short.",
                pins=(check.a, check.b),
                blocking=bool(blocking),
            )
        )

    polarised = [
        component.ref
        for component in doc.components
        for footprint in [lookup(component.footprint_id)]
        if footprint is not None and footprint.polarized
    ]
    if polarised:
        checks.append(
            Checkpoint(
                kind="polarity",
                title="Polarity sweep before power",
                instruction=(
                    "Look at every polarised part once more and compare it against the "
                    f"component-side sheet: {', '.join(sorted(polarised))}."
                ),
                expected=(
                    "Every stripe, band and flat facing the way the sheet shows. A backwards "
                    "electrolytic fails loudly and a backwards diode fails silently."
                ),
                blocking=True,
            )
        )

    dips = [
        component.ref
        for component in doc.components
        for footprint in [lookup(component.footprint_id)]
        if footprint is not None and footprint.body.archetype == "dip"
    ]
    if dips:
        checks.append(
            Checkpoint(
                kind="polarity",
                title="Fit the ICs, pin 1 as shown",
                instruction=(
                    f"Only now put {', '.join(sorted(dips))} into their sockets, notch to the "
                    "end the sheet marks. Straighten the legs on a flat surface first."
                ),
                expected="Every notch and dot pointing the same way as the sheet.",
                blocking=True,
            )
        )

    checks.append(
        Checkpoint(
            kind="power-on",
            title="First power-up, current limited",
            instruction=(
                "Set the supply to the circuit's voltage and the current limit to a little "
                "above what you expect it to draw. Watch the current as it comes up, and "
                "keep a hand on the switch."
            ),
            expected=(
                "Current settles at roughly what you expected. If it runs into the limit, "
                "power down at once and go back to the isolation checks — something is "
                "bridged."
            ),
            blocking=True,
        )
    )
    return checks


def _is_power_pair(doc: PerfDocument, name_a: str, name_b: str) -> bool:
    classes = {net.name: net.net_class for net in doc.nets}
    pair = {classes.get(name_a), classes.get(name_b)}
    return "power" in pair and "ground" in pair


# -- supporting lists -------------------------------------------------------


def _bom(doc: PerfDocument, lookup: FootprintLookup) -> tuple[BomLine, ...]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        name = footprint.name if footprint is not None else component.footprint_id
        grouped.setdefault((component.value, name), []).append(component.ref)

    lines = [
        BomLine(
            ref_designators=tuple(sorted(refs)),
            value=value,
            footprint_name=name,
            quantity=len(refs),
        )
        for (value, name), refs in grouped.items()
    ]
    lines.sort(key=lambda line: (line.footprint_name, line.value, line.ref_designators))
    return tuple(lines)


def _tools(doc: PerfDocument, cuts: list[WireCut], spines: list[SpineCut]) -> tuple[str, ...]:
    """What to have on the bench, derived from what the board actually needs."""
    iron = IRON_BY_MATERIAL[doc.board.material]
    tools = [
        f"Soldering iron with a fine tip, set to {iron.temperature_c} °C",
        "60/40 or lead-free solder, 0.7–1.0 mm",
        "Flux — a pen or a small pot. Solder traces are not reliably makeable without it",
        "Side cutters and small pliers",
        "A multimeter with a continuity buzzer (every checkpoint below uses it)",
    ]
    if cuts:
        gauges = sorted({cut.awg for cut in cuts})
        colors = sorted({cut.color for cut in cuts})
        total = sum(cut.cut_mm for cut in cuts)
        tools.append(
            f"Hookup wire, AWG {', '.join(str(g) for g in gauges)} in {', '.join(colors)} "
            f"— about {total / 1000:.2f} m in total"
        )
        if any(cut.insulated for cut in cuts):
            tools.append("Wire strippers")
    if doc.mounting_holes:
        diameters = sorted({m.diameter for m in doc.mounting_holes})
        sizes = ", ".join(f"{d:g} mm" for d in diameters)
        tools.append(
            f"A drill and {sizes} bit{'s' if len(diameters) > 1 else ''} for the mounting "
            f"holes, plus a deburring tool or a larger bit turned by hand"
        )
    if spines:
        total_spine = sum(spine.length_mm for spine in spines)
        spine_gauges = sorted({spine.gauge_mm for spine in spines})
        tools.append(
            f"Tinned copper wire for the trace spines, "
            f"{', '.join(f'{g:g} mm' for g in spine_gauges)} — about {total_spine:.0f} mm "
            "(component lead offcuts do the job just as well)"
        )
    return tuple(tools)


def _warnings(
    doc: PerfDocument, lookup: FootprintLookup, violations: list[DrcViolation]
) -> tuple[GuideWarning, ...]:
    """Everything that makes this guide less than a complete description of the build."""
    warnings: list[GuideWarning] = []

    unknown = sorted(
        component.ref
        for component in doc.components
        if lookup(component.footprint_id) is None
    )
    if unknown:
        warnings.append(
            GuideWarning(
                code="unknown-footprint",
                message=(
                    f"No step was written for {', '.join(unknown)}: their footprint is not in "
                    "the library, so this guide cannot say which holes they go in."
                ),
            )
        )

    if doc.edge_connectors:
        # Said out loud because nothing else in the guide will mention it. The fingers
        # are copper the board came with, so they generate no step -- and a builder who
        # never reads that they are there is a builder who solders a connector to the
        # wrong end of the board.
        runs = ", ".join(
            f"{connector.count} fingers on the {connector.edge} edge from "
            f"{format_hole(edge_connector_holes(connector, doc.board)[0])}"
            for connector in doc.edge_connectors
            if edge_connector_holes(connector, doc.board)
        )
        warnings.append(
            GuideWarning(
                code="edge-connector",
                message=(
                    f"This board has edge-connector fingers ({runs}). No step below covers "
                    "them: they are part of the board, not something to make. Fit whatever "
                    "mates with them last, and keep the iron off them until then — a tinned "
                    "finger no longer fits a connector."
                ),
            )
        )

    if not doc.nets:
        warnings.append(
            GuideWarning(
                code="no-netlist",
                message=(
                    "No netlist has been imported, so there are no continuity checks. The "
                    "steps describe what to build; nothing here can confirm it is the right "
                    "circuit. Import the schematic's netlist to get the verification half."
                ),
            )
        )
    else:
        lvs = run_lvs(doc, lookup)
        if lvs.summary.opens:
            warnings.append(
                GuideWarning(
                    code="lvs-open",
                    message=(
                        f"{lvs.summary.opens} net(s) are not fully connected on this board. "
                        "Following this guide will reproduce the board as it is, including "
                        "those gaps — route them first."
                    ),
                )
            )
        if lvs.summary.shorts:
            warnings.append(
                GuideWarning(
                    code="lvs-short",
                    message=(
                        f"{lvs.summary.shorts} short(s) between nets the schematic keeps "
                        "apart. Do not build this board until they are gone."
                    ),
                )
            )

    errors = [v for v in violations if v.severity == "error"]
    if errors:
        warnings.append(
            GuideWarning(
                code="drc-error",
                message=(
                    f"{len(errors)} DRC error(s) on this board — overlapping parts, crossing "
                    "conductors or pins sharing a hole. The steps below describe it anyway; "
                    "some of them will not be physically possible."
                ),
            )
        )

    if not doc.conductors:
        warnings.append(
            GuideWarning(
                code="no-conductors",
                message=(
                    "Nothing is routed yet, so this guide covers fitting the parts and "
                    "nothing else."
                ),
            )
        )
    return tuple(warnings)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def describe(guide: Guide) -> str:
    """One line for a status bar."""
    phases = sum(1 for phase in guide.phases if not phase.is_empty)
    parts = [
        f"{guide.total_steps} step(s) across {phases} phase(s)",
        f"{guide.checkpoint_count} check(s)",
    ]
    if guide.cut_list:
        parts.append(f"{len(guide.cut_list)} wire(s) to cut")
    if guide.warnings:
        parts.append(f"{len(guide.warnings)} warning(s)")
    return ", ".join(parts)


def all_checkpoints(guide: Guide) -> tuple[Checkpoint, ...]:
    return tuple(check for phase in guide.phases for check in phase.checkpoints)


def all_steps(guide: Guide) -> tuple[GuideStep, ...]:
    return tuple(step for phase in guide.phases for step in phase.steps)
