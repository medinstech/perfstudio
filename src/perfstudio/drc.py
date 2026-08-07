"""Design Rule Checker (PLAN.md §5.2).

Ported from packages/core/src/drc.ts. This is where PerfStudio earns its keep: a
general PCB tool has no idea what "dragging solder along a trace" means, so it
cannot warn about the failure modes that actually sink a perfboard build. The
rules here fall into two groups:

 - ERRORS: things that are simply wrong regardless of board type -- overlapping
   bodies, off-board placement, two pins in one hole, accidental crossings, and a
   solder-trace path that breaks the orthogonal-chain invariant (geometry.py).
 - WARNINGS: perfboard-specific physical risk, straight out of PLAN.md §4.6 -- the
   ~0.6 mm neighbour-pad bridging risk (§5.2 R5', the single most valuable rule in
   this file), phenolic pad-lifting, solder-trace feasibility, current capacity
   with an actual resistance/voltage-drop estimate, mains creepage, lead-bend
   reliability, and a minimal "pin touches nothing" connectivity check (full LVS
   is lvs.py's job, not this module's).

Pure and deterministic: no I/O, no clock, no randomness. `run_drc` sorts its
output so two calls on the same document always return the same list, in the
same order -- see `_violation_sort_key`.

This module does not reimplement hole maths or connectivity: adjacency, path
validation and placement transforms come from geometry.py, and "what's actually
electrically connected" comes from connectivity.py's union-find. Duplicating
either here would be exactly the kind of drift the TypeScript-to-Python port is
supposed to eliminate, not reintroduce.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .connectivity import FootprintLookup, PhysicalNet, PhysicalPinRef, extract_physical_nets
from .geometry import (
    all_pin_holes,
    format_hole,
    hole_key,
    hole_to_mm,
    is_inside_board,
    manhattan,
    neighbors4,
    path_length_mm,
    pin_hole,
    transform_offset,
    validate_orthogonal_chain,
)
from .model import (
    Board,
    BoardSide,
    ComponentId,
    ComponentInstance,
    ConductorId,
    Footprint,
    HoleCoord,
    LeadBendConductor,
    Net,
    NetId,
    PerfDocument,
    SolderBuildup,
    SolderTraceConductor,
    is_crossing_blocked,
    is_solder_trace,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

DrcSeverity: TypeAlias = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class DrcViolation:
    """One design-rule violation.

    ``component_ids``/``conductor_ids`` default to an empty tuple rather than
    ``None`` -- unlike the TypeScript source (where the fields are optional and
    simply omitted), there is no meaningful difference here between "not
    supplied" and "supplied empty": no rule ever emits an empty-but-present id
    list, and the golden fixtures serialise an absent field as ``[]`` anyway
    (see tools/diffcheck/generate.mjs), so a plain empty-tuple default matches
    both the TS semantics and the wire format with less machinery.
    """

    #: Stable, kebab-case rule id, e.g. 'solder-trace-proximity'. Never renamed.
    rule: str
    severity: DrcSeverity
    #: Human-readable. Names holes via format_hole ("C7"), the language the
    #: soldering guide speaks.
    message: str
    holes: tuple[HoleCoord, ...]
    component_ids: tuple[ComponentId, ...] = ()
    conductor_ids: tuple[ConductorId, ...] = ()


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrcOptions:
    """Tunable DRC thresholds.

    Every field is a physically-reasoned default, not an arbitrary number --
    the comment above each one is the reasoning, carried over from
    packages/core/src/drc.ts's DEFAULT_DRC_OPTIONS. There the type is a
    ``Partial<DrcOptions>``-accepting merge over defaults; the Python port skips
    reimplementing a partial-merge protocol and instead expects callers who want
    an override to build a full ``DrcOptions`` via
    ``dataclasses.replace(DEFAULT_DRC_OPTIONS, ...)``, which is the idiomatic
    equivalent and needs no extra code here.
    """

    #: Pure `solder-trace` pad count above which R5'' (pad-lifting risk) fires,
    #: but only on FR-2/FR-1 phenolic board (PLAN.md §5.2 R5''). Default 6:
    #: PLAN's own worked example table stops at 10 pads, and field reports of
    #: pad lift on cheap phenolic cluster around sustained iron dwell needed to
    #: bridge more than half a dozen joints in one continuous pour.
    pad_lifting_max_solder_trace_pads: int

    #: Pure `solder-trace` pad count above which R5''' (feasibility) fires,
    #: regardless of board material -- even FR-4 pads eventually fail
    #: mechanically on a pure-solder run. Default 6, matching PLAN.md §5.2 R5'''
    #: ("5-6 pad" guidance).
    solder_trace_feasibility_max_pads: int

    #: Estimated solder cross-section per buildup level, in mm². PLAN.md §4.6:
    #: "light / normal / heavy -> roughly 0.15 / 0.3 / 0.6 mm² of solder." These
    #: are rough fillet-volume estimates, not a measured standard -- hence a
    #: documented, overridable default rather than a hard-coded constant.
    solder_buildup_area_mm2: dict[SolderBuildup, float]

    #: Solder resistivity in µΩ·cm. Default 15 (Sn63Pb37, per PLAN.md §4.6) --
    #: about 8-9x copper's 1.68 µΩ·cm, which is exactly why a spine matters.
    solder_resistivity_u_ohm_cm: float

    #: Copper resistivity in µΩ·cm. Default 1.68, the standard textbook value.
    copper_resistivity_u_ohm_cm: float

    #: Current-capacity rule of thumb, in A/mm², applied to the estimated
    #: cross-section. Default 5: below the ~6-10 A/mm² commonly quoted for
    #: copper hookup wire in free air, derated because solder melts at ~183 degC
    #: (vs copper's ~1085 degC) and a perfboard has no copper pour to act as a
    #: heatsink -- deliberately conservative so the warning fires before a joint
    #: gets uncomfortably hot, not after.
    max_current_density_a_per_mm2: float

    #: Net voltage (V) above which R7 (creepage) starts checking adjacency to
    #: other nets. Default 300: PLAN.md §5.2 R7 and §4.6 both cite 2.54 mm hole
    #: spacing as "around the practical limit" for mains-level work.
    creepage_voltage_threshold_v: float

    #: Lead-bend length (Manhattan distance between its two contact holes, in
    #: hole pitches) above which R10 fires. Default 4: a bent lead longer than
    #: that has enough unsupported span to fatigue or short against a
    #: neighbouring part under handling.
    max_lead_bend_holes: int


DEFAULT_DRC_OPTIONS: DrcOptions = DrcOptions(
    pad_lifting_max_solder_trace_pads=6,
    solder_trace_feasibility_max_pads=6,
    solder_buildup_area_mm2={"light": 0.15, "normal": 0.3, "heavy": 0.6},
    solder_resistivity_u_ohm_cm=15.0,
    copper_resistivity_u_ohm_cm=1.68,
    max_current_density_a_per_mm2=5.0,
    creepage_voltage_threshold_v=300.0,
    max_lead_bend_holes=4,
)

# ---------------------------------------------------------------------------
# Small local helpers
# ---------------------------------------------------------------------------

#: Alias kept for parity with drc.ts's `safeHoleRef`: every message in this file
#: names holes via format_hole, which degrades gracefully (rather than raising)
#: on the negative/off-board coordinates several of these rules exist to report.
_safe_hole = format_hole


def _node_side_key(hole: HoleCoord, side: BoardSide) -> str:
    """String key for a (hole, side) node -- the same identity connectivity.py unions on."""
    return f"{hole_key(hole)}@{side}"


def _build_node_net_index(nets: Sequence[PhysicalNet]) -> dict[str, PhysicalNet]:
    """Index from (hole, side) to the PhysicalNet occupying it, built once and reused."""
    index: dict[str, PhysicalNet] = {}
    for net in nets:
        for node in net.nodes:
            index[_node_side_key(node.hole, node.side)] = net
    return index


def _build_conductor_net_index(nets: Sequence[PhysicalNet]) -> dict[ConductorId, str]:
    """Index from conductor id to the id of the PhysicalNet it participates in."""
    index: dict[ConductorId, str] = {}
    for net in nets:
        for conductor_id in net.conductor_ids:
            index[conductor_id] = net.id
    return index


def _physical_net_for_pin(
    nets: Sequence[PhysicalNet], pin: PhysicalPinRef
) -> PhysicalNet | None:
    for net in nets:
        if any(p.component_ref == pin.component_ref and p.pin == pin.pin for p in net.pins):
            return net
    return None


def _resistance_ohm(resistivity_u_ohm_cm: float, length_mm: float, area_mm2: float) -> float:
    """R = resistivity * length / area, in Ohms. resistivity given in µOhm*cm.

    ρ[µΩ·cm] -> ρ[Ω·mm]: R[Ω] = ρ[Ω·cm] * L[cm] / A[cm²]
      = (ρ_uOhmCm * 1e-6) * (L_mm / 10) / (A_mm2 / 100) = ρ_uOhmCm * 1e-5 * L_mm / A_mm2
    Verified against PLAN.md §4.6's worked example: 15 µΩ·cm, 25.4 mm, 0.3 mm² -> 12.7 mΩ
    (≈13 mΩ quoted).
    """
    if area_mm2 <= 0:
        return math.inf
    resistivity_ohm_mm = resistivity_u_ohm_cm * 1e-5
    return (resistivity_ohm_mm * length_mm) / area_mm2


# ---------------------------------------------------------------------------
# Rule 1 -- component body overlap (error)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Aabb:
    min_x: float
    max_x: float
    min_y: float
    max_y: float


def _component_aabb(
    component: ComponentInstance, footprint: Footprint, board: Board
) -> _Aabb | None:
    """Bounding box of a component's transformed body outline, in board-space mm.

    NOTE: axis-aligned bounding box only. A true rotated-polygon intersection test
    is future work; for v1 this is an acceptable (slightly conservative)
    approximation -- it can over-report on two skewed, non-rectangular bodies that
    are close but not truly touching, but it will never miss a genuine overlap
    between axis-aligned bodies, which covers the overwhelming majority of
    through-hole footprints.
    """
    if len(footprint.body_outline) == 0:
        return None
    anchor_mm = hole_to_mm(component.anchor, board)
    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    for p in footprint.body_outline:
        tx, ty = transform_offset(p.x, p.y, component.rotation, component.mirrored)
        x = anchor_mm.x + tx
        y = anchor_mm.y + ty
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
    return _Aabb(min_x, max_x, min_y, max_y)


def _aabb_overlap(a: _Aabb, b: _Aabb) -> bool:
    return a.min_x < b.max_x and a.max_x > b.min_x and a.min_y < b.max_y and a.max_y > b.min_y


def _check_component_body_overlap(doc: PerfDocument, lookup: FootprintLookup) -> list[DrcViolation]:
    boxes: list[tuple[ComponentInstance, _Aabb]] = []
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue
        box = _component_aabb(component, footprint, doc.board)
        if box is not None:
            boxes.append((component, box))

    violations: list[DrcViolation] = []
    for (a_component, a_box), (b_component, b_box) in itertools.combinations(boxes, 2):
        if not _aabb_overlap(a_box, b_box):
            continue
        violations.append(
            DrcViolation(
                rule="component-body-overlap",
                severity="error",
                message=(
                    f"Component {a_component.ref} (anchored at {_safe_hole(a_component.anchor)}) and "
                    f"{b_component.ref} (anchored at {_safe_hole(b_component.anchor)}) have overlapping "
                    f"body outlines [axis-aligned bounding-box check]."
                ),
                holes=(a_component.anchor, b_component.anchor),
                component_ids=(a_component.id, b_component.id),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 2 -- component partly/wholly off board (error)
# ---------------------------------------------------------------------------


def _check_components_off_board(doc: PerfDocument, lookup: FootprintLookup) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue

        pin_holes = [h for _pin, h in all_pin_holes(component, footprint)]
        check_holes = pin_holes if pin_holes else [component.anchor]
        off_board = [h for h in check_holes if not is_inside_board(h, doc.board)]
        if not off_board:
            continue

        first = off_board[0]
        whole = len(off_board) == len(check_holes)
        violations.append(
            DrcViolation(
                rule="component-off-board",
                severity="error",
                message=(
                    f"Component {component.ref} is {'entirely' if whole else 'partly'} off the board: "
                    f"{len(off_board)} of its {len(check_holes)} pin hole(s) fall outside the "
                    f"{doc.board.cols}x{doc.board.rows} grid (e.g. {_safe_hole(first)})."
                ),
                holes=tuple(off_board),
                component_ids=(component.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 3 -- two component pins in the same hole (error)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PinHoleEntry:
    component: ComponentInstance
    pin_number: str
    hole: HoleCoord


def _check_duplicate_pin_holes(doc: PerfDocument, lookup: FootprintLookup) -> list[DrcViolation]:
    by_hole: dict[str, list[_PinHoleEntry]] = {}

    for component in doc.components:
        footprint = lookup(component.footprint_id)
        if footprint is None:
            continue
        for pin, hole in all_pin_holes(component, footprint):
            by_hole.setdefault(hole_key(hole), []).append(
                _PinHoleEntry(component=component, pin_number=pin.number, hole=hole)
            )

    violations: list[DrcViolation] = []
    for entries in by_hole.values():
        if len(entries) < 2:
            continue
        first = entries[0]

        component_ids = tuple(sorted({e.component.id for e in entries}))
        names = ", ".join(f"{e.component.ref}.{e.pin_number}" for e in entries)
        violations.append(
            DrcViolation(
                rule="duplicate-pin-hole",
                severity="error",
                message=(
                    f"Hole {_safe_hole(first.hole)} has more than one component pin landing on it: "
                    f"{names}."
                ),
                holes=(first.hole,),
                component_ids=component_ids,
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 4 -- crossing conductors (error)
# ---------------------------------------------------------------------------


def _check_crossing_conductors(
    doc: PerfDocument,
    conductor_net_index: Mapping[ConductorId, str],
) -> list[DrcViolation]:
    """Two crossing-blocked conductors that share a hole in their paths but are not
    part of the same physical net there are a physical short.

    Subtlety: for 'solder-trace'/'solder-trace-wired'/'strip', connectivity.py
    unions EVERY hole along the path, so if two such conductors genuinely share a
    hole they are automatically the same physical net -- this rule correctly never
    fires for that case (a shared pad between two solder traces is a deliberate
    junction, not a short). It fires precisely where it should:
    'bare-wire'/'lead-bend' conductors that cross at a hole that is NOT one of
    their endpoints, since connectivity.py does not treat that hole as a contact
    point for them (model.py rule c) -- so two bare wires resting across each
    other away from either one's endpoint are correctly flagged as an accidental
    short, even if a netId happens to have been assigned to both.
    """
    violations: list[DrcViolation] = []
    # Filtering first (rather than nested index loops with per-element skips)
    # preserves the same i<j pairing over the qualifying conductors as the
    # original index-based loop, with less bookkeeping.
    blocked = [c for c in doc.conductors if is_crossing_blocked(c)]

    for a, b in itertools.combinations(blocked, 2):
        if a.side != b.side:
            continue

        net_a = conductor_net_index.get(a.id)
        net_b = conductor_net_index.get(b.id)
        if net_a is not None and net_a == net_b:
            continue  # same physical net: legitimate junction

        b_holes = {hole_key(h) for h in b.path}
        for h in a.path:
            if hole_key(h) not in b_holes:
                continue
            violations.append(
                DrcViolation(
                    rule="crossing-conductors",
                    severity="error",
                    message=(
                        f"Conductor {a.id} ({a.kind}) and conductor {b.id} ({b.kind}) both occupy hole "
                        f"{_safe_hole(h)} on the {a.side} side without being part of the same electrical "
                        f"net — this is a physical short. Reroute one of them, or replace it with an "
                        f"insulated conductor that can safely cross."
                    ),
                    holes=(h,),
                    conductor_ids=tuple(sorted((a.id, b.id))),
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Rule 5 -- solder-trace orthogonal-chain invariant (error)
# ---------------------------------------------------------------------------


def _check_solder_trace_paths(doc: PerfDocument) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    for conductor in doc.conductors:
        if not is_solder_trace(conductor):
            continue
        result = validate_orthogonal_chain(conductor.path)
        if result.ok:
            continue

        path = conductor.path
        offending = path[result.index] if 0 <= result.index < len(path) else None
        prev = path[result.index - 1] if result.index > 0 else None
        holes = tuple(h for h in (prev, offending) if h is not None)

        violations.append(
            DrcViolation(
                rule="solder-trace-invalid-path",
                severity="error",
                message=f"Solder trace {conductor.id} has an invalid path: {result.reason}",
                holes=holes if holes else tuple(path),
                conductor_ids=(conductor.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 6 -- solder-trace proximity risk (warning) -- PLAN.md §5.2 R5'
# ---------------------------------------------------------------------------


def _check_solder_trace_proximity(
    doc: PerfDocument,
    node_index: Mapping[str, PhysicalNet],
) -> list[DrcViolation]:
    """The single most valuable rule in this file. At 2.54 mm pitch with ~1.9 mm
    pads the orthogonal-neighbour pad-edge gap is only ~0.6 mm (PLAN.md §4.6):
    easy to bridge by accident while dragging solder along a trace. For every
    hole a solder trace touches, every orthogonal neighbour that belongs to a
    DIFFERENT physical net is a measurable physical risk point, worth naming in
    the build guide.

    A neighbour with no physical net at all (an empty, unused pad) is not a risk
    -- there is nothing there to bridge to. A neighbour that is part of the SAME
    physical net as the trace is a non-issue by definition: solder already
    legitimately joins them.

    Assumes the board actually has copper at every hole (true of 'pad-per-hole',
    the v1 target board type -- see model.py BoardType).

    The gap is computed from ``board.pitch - board.pad_diameter`` rather than a
    hardcoded 0.6 mm, so the rule (and the number quoted in its message) stays
    correct for any board's actual pitch/pad geometry, not just the 2.54 mm /
    1.9 mm default this docstring's numbers describe.
    """
    violations: list[DrcViolation] = []
    gap_mm = max(0.0, doc.board.pitch - doc.board.pad_diameter)

    for conductor in doc.conductors:
        if not is_solder_trace(conductor):
            continue
        path = conductor.path
        if len(path) == 0:
            continue
        first_hole = path[0]
        own_net = node_index.get(_node_side_key(first_hole, conductor.side))

        seen_pairs: set[str] = set()
        for hole in path:
            for neighbor in neighbors4(hole, doc.board):
                neighbor_net = node_index.get(_node_side_key(neighbor, conductor.side))
                if neighbor_net is None:
                    continue  # empty pad: nothing to bridge to
                if own_net is not None and neighbor_net.id == own_net.id:
                    continue  # same net: legitimate

                pair_key = f"{hole_key(hole)}|{hole_key(neighbor)}"
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Message names both holes explicitly: they become isolation/
                # measurement steps in the build guide, not just a description.
                violations.append(
                    DrcViolation(
                        rule="solder-trace-proximity",
                        severity="warning",
                        message=(
                            f"Solder trace {conductor.id} passes through {_safe_hole(hole)}, whose "
                            f"orthogonal neighbour {_safe_hole(neighbor)} belongs to a different net "
                            f"(~{gap_mm:.2f} mm pad-edge gap at this board's pitch/pad size). Dragging "
                            f"solder along the trace risks bridging the two nets — the most common way a "
                            f"perfboard build fails. Verify clearance between {_safe_hole(hole)} and "
                            f"{_safe_hole(neighbor)} before soldering."
                        ),
                        holes=(hole, neighbor),
                        conductor_ids=(conductor.id,),
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Rule 7 -- pad-lifting risk on phenolic board (warning) -- PLAN.md §5.2 R5''
# ---------------------------------------------------------------------------


def _check_pad_lifting_risk(doc: PerfDocument, options: DrcOptions) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    if doc.board.material not in ("FR2", "FR1"):
        return violations

    for conductor in doc.conductors:
        if conductor.kind != "solder-trace":  # pure trace only, not -wired
            continue
        if len(conductor.path) <= options.pad_lifting_max_solder_trace_pads:
            continue

        violations.append(
            DrcViolation(
                rule="pad-lifting-risk",
                severity="warning",
                message=(
                    f"Pure solder trace {conductor.id} spans {len(conductor.path)} pads on "
                    f"{doc.board.material} (phenolic) board — beyond the "
                    f"{options.pad_lifting_max_solder_trace_pads}-pad threshold. Phenolic pads lift "
                    f"under sustained soldering heat far more readily than FR-4; add a wire spine "
                    f"('solder-trace-wired') or split the run."
                ),
                holes=tuple(conductor.path),
                conductor_ids=(conductor.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 8 -- solder-trace feasibility (warning) -- PLAN.md §5.2 R5'''
# ---------------------------------------------------------------------------


def _check_solder_trace_feasibility(doc: PerfDocument, options: DrcOptions) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    for conductor in doc.conductors:
        if conductor.kind != "solder-trace":
            continue
        if len(conductor.path) <= options.solder_trace_feasibility_max_pads:
            continue

        violations.append(
            DrcViolation(
                rule="solder-trace-too-long",
                severity="warning",
                message=(
                    f"Pure solder trace {conductor.id} spans {len(conductor.path)} pads, beyond the "
                    f"{options.solder_trace_feasibility_max_pads}-pad feasibility threshold. Long "
                    f"pure-solder runs are mechanically unreliable and hard to reflow evenly; consider "
                    f"a wire spine ('solder-trace-wired')."
                ),
                holes=tuple(conductor.path),
                conductor_ids=(conductor.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 9 -- current capacity (warning) -- PLAN.md §5.2 rules 6/6'
# ---------------------------------------------------------------------------


def _check_current_capacity(doc: PerfDocument, options: DrcOptions) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    nets_by_id: dict[NetId, Net] = {n.id: n for n in doc.nets}

    for conductor in doc.conductors:
        # isinstance(), not is_solder_trace(): buildup/spine only exist on
        # SolderTraceConductor, and only isinstance() gives mypy the narrowing
        # needed to read them. The two checks are equivalent here --
        # SolderTraceConductor.kind is exactly Literal["solder-trace",
        # "solder-trace-wired"], the same pair is_solder_trace() (model.py) tests.
        if not isinstance(conductor, SolderTraceConductor):
            continue
        net_id = conductor.net_id
        if net_id is None:
            continue
        net = nets_by_id.get(net_id)
        if net is None or net.current_a is None:
            continue
        current_a = net.current_a

        buildup_area = options.solder_buildup_area_mm2[conductor.buildup]
        spine = conductor.spine
        spine_area_mm2 = math.pi * (spine.gauge / 2) ** 2 if spine is not None else 0.0
        # Cross-section for the capacity estimate: buildup area plus the spine's
        # copper area, per spec -- a simple sum used only to size the ampacity
        # threshold.
        cross_section_mm2 = buildup_area + spine_area_mm2
        capacity_a = cross_section_mm2 * options.max_current_density_a_per_mm2
        if current_a <= capacity_a:
            continue

        length_mm = path_length_mm(conductor.path, doc.board)
        solder_r = _resistance_ohm(options.solder_resistivity_u_ohm_cm, length_mm, buildup_area)
        # Resistance/voltage-drop reporting (as opposed to the capacity estimate
        # above) models the solder fillet and the copper spine as two resistors of
        # equal length in parallel -- the physically correct treatment for two
        # bonded conductive paths carrying the same current over the same run.
        # This is slightly more optimistic than PLAN.md §4.6's own worked example,
        # which (for brevity) approximates the wired case by the spine alone: for
        # its 10-pad/0.6 mm example this model gives ~1.3 mOhm against the plan's
        # quoted "~1.5 mOhm" -- well within the plan's own "≈".
        total_r = solder_r
        if spine is not None:
            copper_r = _resistance_ohm(options.copper_resistivity_u_ohm_cm, length_mm, spine_area_mm2)
            total_r = (solder_r * copper_r) / (solder_r + copper_r)
        drop_v = current_a * total_r
        spine_note = f" + {spine.gauge} mm {spine.material} spine" if spine is not None else ""
        recommendation = (
            ""
            if spine is not None
            else " Adding a wire spine typically cuts this resistance by roughly an order of magnitude."
        )

        violations.append(
            DrcViolation(
                rule="current-capacity",
                severity="warning",
                message=(
                    f"Net '{net.name}' declares {current_a} A but solder trace {conductor.id} "
                    f"({conductor.buildup} buildup{spine_note}) has an estimated cross-section of "
                    f"{cross_section_mm2:.3f} mm² (~{capacity_a:.2f} A capacity at "
                    f"{options.max_current_density_a_per_mm2} A/mm²) — inadequate. Estimated resistance "
                    f"~{total_r * 1000:.2f} mOhm over {length_mm:.1f} mm, giving a ~{drop_v * 1000:.1f} "
                    f"mV drop at rated current.{recommendation}"
                ),
                holes=tuple(conductor.path),
                conductor_ids=(conductor.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 10 -- creepage (warning) -- PLAN.md §5.2 rule 7
# ---------------------------------------------------------------------------


def _check_creepage(
    doc: PerfDocument,
    options: DrcOptions,
    node_index: Mapping[str, PhysicalNet],
) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    nets_by_id: dict[NetId, Net] = {n.id: n for n in doc.nets}
    seen_pairs: set[str] = set()

    for conductor in doc.conductors:
        net_id = conductor.net_id
        if net_id is None:
            continue
        net = nets_by_id.get(net_id)
        if net is None or net.voltage_v is None:
            continue
        voltage_v = net.voltage_v
        if voltage_v <= options.creepage_voltage_threshold_v:
            continue

        path = conductor.path
        if len(path) == 0:
            continue
        first_hole = path[0]
        own_net = node_index.get(_node_side_key(first_hole, conductor.side))

        for hole in path:
            for neighbor in neighbors4(hole, doc.board):
                neighbor_net = node_index.get(_node_side_key(neighbor, conductor.side))
                if neighbor_net is None:
                    continue
                if own_net is not None and neighbor_net.id == own_net.id:
                    continue

                pair_key = f"{conductor.id}|{hole_key(hole)}|{hole_key(neighbor)}"
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                violations.append(
                    DrcViolation(
                        rule="creepage-clearance",
                        severity="warning",
                        message=(
                            f"High voltage: net '{net.name}' ({voltage_v} V) conductor {conductor.id} "
                            f"runs through {_safe_hole(hole)}, directly next to {_safe_hole(neighbor)} on "
                            f"a different net. 2.54 mm hole spacing is near the practical creepage limit "
                            f"above {options.creepage_voltage_threshold_v} V — increase clearance (skip a "
                            f"row/column) or reroute before building."
                        ),
                        holes=(hole, neighbor),
                        conductor_ids=(conductor.id,),
                    )
                )
    return violations


# ---------------------------------------------------------------------------
# Rule 11 -- excessive lead-bend length (warning)
# ---------------------------------------------------------------------------


def _check_lead_bend_length(doc: PerfDocument, options: DrcOptions) -> list[DrcViolation]:
    violations: list[DrcViolation] = []
    for conductor in doc.conductors:
        if not isinstance(conductor, LeadBendConductor):
            continue
        if len(conductor.path) == 0:
            continue
        first = conductor.path[0]
        last = conductor.path[-1]

        length = manhattan(first, last)
        if length <= options.max_lead_bend_holes:
            continue

        violations.append(
            DrcViolation(
                rule="lead-bend-too-long",
                severity="warning",
                message=(
                    f"Lead bend on {conductor.component_id} pin {conductor.pin_number} spans {length} "
                    f"holes ({_safe_hole(first)} to {_safe_hole(last)}), beyond the "
                    f"{options.max_lead_bend_holes}-hole reliability threshold. A long bent lead is "
                    f"mechanically fragile — use a wire instead."
                ),
                holes=(first, last),
                component_ids=(conductor.component_id,),
                conductor_ids=(conductor.id,),
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Rule 12 -- pin not connected to anything (warning)
# ---------------------------------------------------------------------------


def _check_unconnected_pins(
    doc: PerfDocument,
    lookup: FootprintLookup,
    physical_nets: Sequence[PhysicalNet],
) -> list[DrcViolation]:
    """Minimal connectivity check: a schematic pin whose physical net contains no
    conductor and no other pin is definitely floating. This deliberately stops
    short of full OPEN/SHORT/FLOATING classification against the schematic
    (PLAN.md §5.1) -- that is lvs.py's job. A pin with a conductor attached that
    goes nowhere useful is an "open", not caught here; only total isolation is.
    """
    violations: list[DrcViolation] = []
    components_by_ref: dict[str, ComponentInstance] = {c.ref: c for c in doc.components}
    seen: set[str] = set()

    for net in doc.nets:
        for node in net.nodes:
            key = f"{node.component_ref}.{node.pin}"
            if key in seen:
                continue
            seen.add(key)

            phys_net = _physical_net_for_pin(
                physical_nets, PhysicalPinRef(component_ref=node.component_ref, pin=node.pin)
            )
            if phys_net is None:
                continue  # unresolvable pin: not this rule's concern
            if len(phys_net.pins) > 1 or len(phys_net.conductor_ids) > 0:
                continue  # touches something

            component = components_by_ref.get(node.component_ref)
            footprint = lookup(component.footprint_id) if component is not None else None
            hole = (
                pin_hole(component, footprint, node.pin)
                if component is not None and footprint is not None
                else None
            )

            violations.append(
                DrcViolation(
                    rule="pin-not-connected",
                    severity="warning",
                    message=(
                        f"Pin {node.pin} of {node.component_ref} (net '{net.name}') is not connected to "
                        f"anything: no conductor touches it and it shares no hole with another pin."
                    ),
                    holes=(hole,) if hole is not None else (),
                    component_ids=(component.id,) if component is not None else (),
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Aggregation and deterministic ordering
# ---------------------------------------------------------------------------


def _violation_sort_key(
    v: DrcViolation,
) -> tuple[str, str, tuple[tuple[int, int], ...], str]:
    """Total order over violations: by rule id, then severity, then the holes they
    name, then the message as a final tiebreaker. This is what makes `run_drc`'s
    output reproducible and diffable regardless of the (irrelevant) order rules
    happened to run in internally.

    The holes component is a tuple of (col, row) pairs rather than a hand-rolled
    comparator: Python's tuple ordering already compares element-by-element and
    treats a shorter tuple that is a prefix of a longer one as "less", which is
    exactly drc.ts's `compareHoleArrays` behaviour -- reusing it here needs no
    separate comparison function.
    """
    return (
        v.rule,
        v.severity,
        tuple((h.col, h.row) for h in v.holes),
        v.message,
    )


def run_drc(
    doc: PerfDocument,
    lookup: FootprintLookup,
    options: DrcOptions = DEFAULT_DRC_OPTIONS,
) -> list[DrcViolation]:
    """Runs every DRC rule over `doc` and returns all violations, sorted
    deterministically (see `_violation_sort_key`). `lookup` resolves footprints
    exactly as connectivity.py's FootprintLookup does; components with an unknown
    footprint are silently skipped by whichever rules need footprint data,
    matching connectivity.py's own behaviour.
    """
    physical_nets = extract_physical_nets(doc, lookup)
    node_index = _build_node_net_index(physical_nets)
    conductor_net_index = _build_conductor_net_index(physical_nets)

    violations: list[DrcViolation] = [
        *_check_component_body_overlap(doc, lookup),
        *_check_components_off_board(doc, lookup),
        *_check_duplicate_pin_holes(doc, lookup),
        *_check_crossing_conductors(doc, conductor_net_index),
        *_check_solder_trace_paths(doc),
        *_check_solder_trace_proximity(doc, node_index),
        *_check_pad_lifting_risk(doc, options),
        *_check_solder_trace_feasibility(doc, options),
        *_check_current_capacity(doc, options),
        *_check_creepage(doc, options, node_index),
        *_check_lead_bend_length(doc, options),
        *_check_unconnected_pins(doc, lookup, physical_nets),
    ]

    violations.sort(key=_violation_sort_key)
    return violations
