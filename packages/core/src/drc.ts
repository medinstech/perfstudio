/**
 * Design Rule Checker (PLAN.md §5.2).
 *
 * This is where PerfStudio earns its keep: a general PCB tool has no idea what
 * "dragging solder along a trace" means, so it cannot warn about the failure modes
 * that actually sink a perfboard build. The rules here fall into two groups:
 *
 *  - ERRORS: things that are simply wrong regardless of board type — overlapping
 *    bodies, off-board placement, two pins in one hole, accidental crossings, and a
 *    solder-trace path that breaks the orthogonal-chain invariant (geometry.ts).
 *  - WARNINGS: perfboard-specific physical risk, straight out of PLAN.md §4.6 — the
 *    0.6 mm neighbour-pad bridging risk (§5.2 R5', the single most valuable rule in
 *    this file), phenolic pad-lifting, solder-trace feasibility, current capacity
 *    with an actual resistance/voltage-drop estimate, mains creepage, lead-bend
 *    reliability, and a minimal "pin touches nothing" connectivity check (full LVS
 *    is lvs.ts's job, not this file's).
 *
 * Pure and deterministic: no I/O, no Date.now(), no Math.random(). `runDrc` sorts its
 * output so two calls on the same document always return the same array, in the same
 * order — see `compareViolations`.
 *
 * This module does not reimplement hole maths or connectivity: adjacency, path
 * validation and placement transforms come from geometry.ts, and "what's actually
 * electrically connected" comes from connectivity.ts's union-find. Duplicating either
 * here would be exactly the kind of drift the project just finished removing.
 */

import type {
  Board,
  ComponentId,
  ComponentInstance,
  Conductor,
  ConductorId,
  Footprint,
  HoleCoord,
  Net,
  NetId,
  PerfDocument,
  SolderBuildup,
} from './model.js';
import { isCrossingBlocked, isLeadBend, isSolderTrace } from './model.js';
import {
  allPinHoles,
  formatHole,
  holeKey,
  holeToMm,
  isInsideBoard,
  manhattan,
  neighbors4,
  pathLengthMm,
  pinHole,
  transformOffset,
  validateOrthogonalChain,
} from './geometry.js';
import { extractPhysicalNets } from './connectivity.js';
import type { FootprintLookup, PhysicalNet, PhysicalPinRef } from './connectivity.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type DrcSeverity = 'error' | 'warning';

export interface DrcViolation {
  /** Stable, kebab-case rule id, e.g. 'solder-trace-proximity'. Never renamed. */
  readonly rule: string;
  readonly severity: DrcSeverity;
  /** Human-readable. Names holes via coordToHoleRef ("C7"), the language the guide speaks. */
  readonly message: string;
  readonly holes: readonly HoleCoord[];
  readonly componentIds?: readonly ComponentId[];
  readonly conductorIds?: readonly ConductorId[];
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface DrcOptions {
  /**
   * Pure `solder-trace` pad count above which R5'' (pad-lifting risk) fires, but only
   * on FR-2/FR-1 phenolic board (PLAN.md §5.2 R5''). Default 6: PLAN's own worked
   * example table stops at 10 pads, and field reports of pad lift on cheap phenolic
   * cluster around sustained iron dwell needed to bridge more than half a dozen joints
   * in one continuous pour.
   */
  readonly padLiftingMaxSolderTracePads: number;

  /**
   * Pure `solder-trace` pad count above which R5''' (feasibility) fires, regardless of
   * board material — even FR-4 pads eventually fail mechanically on a pure-solder run.
   * Default 6, matching PLAN.md §5.2 R5''' ("5-6 pad" guidance).
   */
  readonly solderTraceFeasibilityMaxPads: number;

  /**
   * Estimated solder cross-section per buildup level, in mm². PLAN.md §4.6: "light /
   * normal / heavy → roughly 0.15 / 0.3 / 0.6 mm² of solder." These are rough fillet-
   * volume estimates, not a measured standard — hence "roughly", and hence they are a
   * documented, overridable default rather than a hard-coded constant.
   */
  readonly solderBuildupAreaMm2: Readonly<Record<SolderBuildup, number>>;

  /**
   * Solder resistivity in µΩ·cm. Default 15 (Sn63Pb37, per PLAN.md §4.6) — about 8-9x
   * copper's 1.68 µΩ·cm, which is exactly why a spine matters.
   */
  readonly solderResistivityUOhmCm: number;

  /** Copper resistivity in µΩ·cm. Default 1.68, the standard textbook value. */
  readonly copperResistivityUOhmCm: number;

  /**
   * Current-capacity rule of thumb, in A/mm², applied to the estimated cross-section.
   * Default 5: below the ~6-10 A/mm² commonly quoted for copper hookup wire in free
   * air, derated because solder melts at ~183 degC (vs copper's ~1085 degC) and a
   * perfboard has no copper pour to act as a heatsink — deliberately conservative so
   * the warning fires before a joint gets uncomfortably hot, not after.
   */
  readonly maxCurrentDensityAPerMm2: number;

  /**
   * Net voltage (V) above which R7 (creepage) starts checking adjacency to other nets.
   * Default 300: PLAN.md §5.2 R7 and §4.6 both cite 2.54 mm hole spacing as "around the
   * practical limit" for mains-level work.
   */
  readonly creepageVoltageThresholdV: number;

  /**
   * Lead-bend length (Manhattan distance between its two contact holes, in hole
   * pitches) above which R10 fires. Default 4: a bent lead longer than that has enough
   * unsupported span to fatigue or short against a neighbouring part under handling.
   */
  readonly maxLeadBendHoles: number;
}

export const DEFAULT_DRC_OPTIONS: DrcOptions = {
  padLiftingMaxSolderTracePads: 6,
  solderTraceFeasibilityMaxPads: 6,
  solderBuildupAreaMm2: { light: 0.15, normal: 0.3, heavy: 0.6 },
  solderResistivityUOhmCm: 15,
  copperResistivityUOhmCm: 1.68,
  maxCurrentDensityAPerMm2: 5,
  creepageVoltageThresholdV: 300,
  maxLeadBendHoles: 4,
};

// ---------------------------------------------------------------------------
// Small local helpers
// ---------------------------------------------------------------------------

/** Ascii-safe string compare — avoids locale-dependent ordering across platforms. */
function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** Builds a DrcViolation, only including componentIds/conductorIds when supplied. */
function makeViolation(input: {
  readonly rule: string;
  readonly severity: DrcSeverity;
  readonly message: string;
  readonly holes: readonly HoleCoord[];
  readonly componentIds?: readonly ComponentId[];
  readonly conductorIds?: readonly ConductorId[];
}): DrcViolation {
  const { rule, severity, message, holes, componentIds, conductorIds } = input;
  const base: DrcViolation = { rule, severity, message, holes };
  return {
    ...base,
    ...(componentIds !== undefined ? { componentIds } : {}),
    ...(conductorIds !== undefined ? { conductorIds } : {}),
  };
}

/** String key for a (hole, side) node — the same identity connectivity.ts unions on. */
function nodeSideKey(hole: HoleCoord, side: Conductor['side']): string {
  return `${holeKey(hole)}@${side}`;
}

/**
 * Hole naming for messages. Lives in core as `formatHole` so every consumer degrades
 * the same way on an off-board (negative) coordinate rather than each inventing its own
 * wrapper — see the note on formatHole in geometry.ts.
 */
const safeHoleRef = formatHole;

/** Index from (hole,side) to the PhysicalNet occupying it, built once and reused. */
function buildNodeNetIndex(nets: readonly PhysicalNet[]): Map<string, PhysicalNet> {
  const index = new Map<string, PhysicalNet>();
  for (const net of nets) {
    for (const node of net.nodes) {
      index.set(nodeSideKey(node.hole, node.side), net);
    }
  }
  return index;
}

/** Index from conductor id to the id of the PhysicalNet it participates in. */
function buildConductorNetIndex(nets: readonly PhysicalNet[]): Map<ConductorId, string> {
  const index = new Map<ConductorId, string>();
  for (const net of nets) {
    for (const id of net.conductorIds) index.set(id, net.id);
  }
  return index;
}

function physicalNetForPin(
  nets: readonly PhysicalNet[],
  pin: PhysicalPinRef,
): PhysicalNet | undefined {
  return nets.find((net) =>
    net.pins.some((p) => p.componentRef === pin.componentRef && p.pin === pin.pin),
  );
}

/** R = resistivity * length / area, in Ohms. resistivity given in µOhm*cm. */
function resistanceOhm(resistivityUOhmCm: number, lengthMm: number, areaMm2: number): number {
  if (areaMm2 <= 0) return Infinity;
  // ρ[µΩ·cm] -> ρ[Ω·mm]: 1 µΩ·cm = 1e-6 Ω·cm = 1e-6 * 10 Ω·mm-per-cm-of-length... worked
  // from first principles: R[Ω] = ρ[Ω·cm] * L[cm] / A[cm²]
  //   = (ρ_uOhmCm * 1e-6) * (L_mm / 10) / (A_mm2 / 100)
  //   = ρ_uOhmCm * 1e-5 * L_mm / A_mm2
  // Verified against PLAN.md §4.6's worked example: 15 µΩ·cm, 25.4 mm, 0.3 mm² -> 12.7 mΩ (≈13 mΩ quoted).
  const resistivityOhmMm = resistivityUOhmCm * 1e-5;
  return (resistivityOhmMm * lengthMm) / areaMm2;
}

// ---------------------------------------------------------------------------
// Rule 1 — component body overlap (error)
// ---------------------------------------------------------------------------

interface Aabb {
  readonly minX: number;
  readonly maxX: number;
  readonly minY: number;
  readonly maxY: number;
}

/**
 * Bounding box of a component's transformed body outline, in board-space mm.
 * NOTE: axis-aligned bounding box only. A true rotated-polygon intersection test is
 * future work; for v1 this is an acceptable (slightly conservative) approximation —
 * it can over-report on two skewed, non-rectangular bodies that are close but not
 * truly touching, but it will never miss a genuine overlap between axis-aligned
 * bodies, which covers the overwhelming majority of through-hole footprints.
 */
function componentAabb(
  component: ComponentInstance,
  footprint: Footprint,
  board: Board,
): Aabb | undefined {
  if (footprint.bodyOutline.length === 0) return undefined;
  const anchorMm = holeToMm(component.anchor, board);
  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const p of footprint.bodyOutline) {
    const t = transformOffset(p.x, p.y, component.rotation, component.mirrored);
    const x = anchorMm.x + t.x;
    const y = anchorMm.y + t.y;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return { minX, maxX, minY, maxY };
}

function aabbOverlap(a: Aabb, b: Aabb): boolean {
  return a.minX < b.maxX && a.maxX > b.minX && a.minY < b.maxY && a.maxY > b.minY;
}

function checkComponentBodyOverlap(doc: PerfDocument, lookup: FootprintLookup): DrcViolation[] {
  const boxes: Array<{ readonly component: ComponentInstance; readonly box: Aabb }> = [];
  for (const component of doc.components) {
    const footprint = lookup(component.footprintId);
    if (!footprint) continue;
    const box = componentAabb(component, footprint, doc.board);
    if (box) boxes.push({ component, box });
  }

  const violations: DrcViolation[] = [];
  for (let i = 0; i < boxes.length; i++) {
    const a = boxes[i];
    if (!a) continue;
    for (let j = i + 1; j < boxes.length; j++) {
      const b = boxes[j];
      if (!b) continue;
      if (!aabbOverlap(a.box, b.box)) continue;
      violations.push(
        makeViolation({
          rule: 'component-body-overlap',
          severity: 'error',
          message:
            `Component ${a.component.ref} (anchored at ${safeHoleRef(a.component.anchor)}) and ` +
            `${b.component.ref} (anchored at ${safeHoleRef(b.component.anchor)}) have overlapping body ` +
            `outlines [axis-aligned bounding-box check].`,
          holes: [a.component.anchor, b.component.anchor],
          componentIds: [a.component.id, b.component.id],
        }),
      );
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 2 — component partly/wholly off board (error)
// ---------------------------------------------------------------------------

function checkComponentsOffBoard(doc: PerfDocument, lookup: FootprintLookup): DrcViolation[] {
  const violations: DrcViolation[] = [];
  for (const component of doc.components) {
    const footprint = lookup(component.footprintId);
    if (!footprint) continue;

    const pinHoles = allPinHoles(component, footprint).map((p) => p.hole);
    const checkHoles = pinHoles.length > 0 ? pinHoles : [component.anchor];
    const offBoard = checkHoles.filter((h) => !isInsideBoard(h, doc.board));
    if (offBoard.length === 0) continue;

    const first = offBoard[0];
    if (first === undefined) continue; // unreachable: offBoard.length > 0 above

    const whole = offBoard.length === checkHoles.length;
    violations.push(
      makeViolation({
        rule: 'component-off-board',
        severity: 'error',
        message:
          `Component ${component.ref} is ${whole ? 'entirely' : 'partly'} off the board: ${offBoard.length} of ` +
          `its ${checkHoles.length} pin hole(s) fall outside the ${doc.board.cols}x${doc.board.rows} grid ` +
          `(e.g. ${safeHoleRef(first)}).`,
        holes: offBoard,
        componentIds: [component.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 3 — two component pins in the same hole (error)
// ---------------------------------------------------------------------------

function checkDuplicatePinHoles(doc: PerfDocument, lookup: FootprintLookup): DrcViolation[] {
  interface Entry {
    readonly component: ComponentInstance;
    readonly pinNumber: string;
    readonly hole: HoleCoord;
  }
  const byHole = new Map<string, Entry[]>();

  for (const component of doc.components) {
    const footprint = lookup(component.footprintId);
    if (!footprint) continue;
    for (const { pin, hole } of allPinHoles(component, footprint)) {
      const key = holeKey(hole);
      const list = byHole.get(key) ?? [];
      list.push({ component, pinNumber: pin.number, hole });
      byHole.set(key, list);
    }
  }

  const violations: DrcViolation[] = [];
  for (const list of byHole.values()) {
    if (list.length < 2) continue;
    const first = list[0];
    if (!first) continue; // unreachable: list.length >= 2 above

    const componentIds = [...new Set(list.map((e) => e.component.id))].sort(compareStrings);
    const names = list.map((e) => `${e.component.ref}.${e.pinNumber}`).join(', ');
    violations.push(
      makeViolation({
        rule: 'duplicate-pin-hole',
        severity: 'error',
        message: `Hole ${safeHoleRef(first.hole)} has more than one component pin landing on it: ${names}.`,
        holes: [first.hole],
        componentIds,
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 4 — crossing conductors (error)
// ---------------------------------------------------------------------------

/**
 * Two crossing-blocked conductors (isCrossingBlocked) that share a hole in their
 * paths but are not part of the same physical net there are a physical short.
 *
 * Subtlety: for 'solder-trace'/'solder-trace-wired'/'strip', connectivity.ts unions
 * EVERY hole along the path, so if two such conductors genuinely share a hole they
 * are automatically the same physical net — this rule correctly never fires for that
 * case (a shared pad between two solder traces is a deliberate junction, not a short).
 * It fires precisely where it should: 'bare-wire'/'lead-bend' conductors that cross
 * at a hole that is NOT one of their endpoints, since connectivity.ts does not treat
 * that hole as a contact point for them (model.ts rule c) — so two bare wires resting
 * across each other away from either one's endpoint are correctly flagged as an
 * accidental short, even if a netId happens to have been assigned to both.
 */
function checkCrossingConductors(
  doc: PerfDocument,
  conductorNetIndex: ReadonlyMap<ConductorId, string>,
): DrcViolation[] {
  const violations: DrcViolation[] = [];
  const conductors = doc.conductors;

  for (let i = 0; i < conductors.length; i++) {
    const a = conductors[i];
    if (!a || !isCrossingBlocked(a)) continue;

    for (let j = i + 1; j < conductors.length; j++) {
      const b = conductors[j];
      if (!b || !isCrossingBlocked(b)) continue;
      if (a.side !== b.side) continue;

      const netA = conductorNetIndex.get(a.id);
      const netB = conductorNetIndex.get(b.id);
      if (netA !== undefined && netA === netB) continue; // same physical net: legitimate junction

      const bHoles = new Set(b.path.map(holeKey));
      for (const h of a.path) {
        if (!bHoles.has(holeKey(h))) continue;
        violations.push(
          makeViolation({
            rule: 'crossing-conductors',
            severity: 'error',
            message:
              `Conductor ${a.id} (${a.kind}) and conductor ${b.id} (${b.kind}) both occupy hole ` +
              `${safeHoleRef(h)} on the ${a.side} side without being part of the same electrical net — ` +
              `this is a physical short. Reroute one of them, or replace it with an insulated conductor that ` +
              `can safely cross.`,
            holes: [h],
            conductorIds: [a.id, b.id].sort(compareStrings),
          }),
        );
      }
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 5 — solder-trace orthogonal-chain invariant (error)
// ---------------------------------------------------------------------------

function checkSolderTracePaths(doc: PerfDocument): DrcViolation[] {
  const violations: DrcViolation[] = [];
  for (const conductor of doc.conductors) {
    if (!isSolderTrace(conductor)) continue;
    const result = validateOrthogonalChain(conductor.path);
    if (result.ok) continue;

    const offending = conductor.path[result.index];
    const prev = result.index > 0 ? conductor.path[result.index - 1] : undefined;
    const holes = [prev, offending].filter((h): h is HoleCoord => h !== undefined);

    violations.push(
      makeViolation({
        rule: 'solder-trace-invalid-path',
        severity: 'error',
        message: `Solder trace ${conductor.id} has an invalid path: ${result.reason}`,
        holes: holes.length > 0 ? holes : conductor.path,
        conductorIds: [conductor.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 6 — solder-trace proximity risk (warning) — PLAN.md §5.2 R5'
// ---------------------------------------------------------------------------

/**
 * The single most valuable rule in this file. At 2.54 mm pitch with ~1.9 mm pads the
 * orthogonal-neighbour pad-edge gap is only ~0.6 mm (PLAN.md §4.6): easy to bridge by
 * accident while dragging solder along a trace. For every hole a solder trace touches,
 * every orthogonal neighbour that belongs to a DIFFERENT physical net is a measurable
 * physical risk point, worth naming in the build guide.
 *
 * A neighbour with no physical net at all (an empty, unused pad) is not a risk — there
 * is nothing there to bridge to. A neighbour that is part of the SAME physical net as
 * the trace is a non-issue by definition: solder already legitimately joins them.
 *
 * Assumes the board actually has copper at every hole (true of 'pad-per-hole', the v1
 * target board type — see model.ts BoardType).
 */
function checkSolderTraceProximity(
  doc: PerfDocument,
  nodeIndex: ReadonlyMap<string, PhysicalNet>,
): DrcViolation[] {
  const violations: DrcViolation[] = [];
  const gapMm = Math.max(0, doc.board.pitch - doc.board.padDiameter);

  for (const conductor of doc.conductors) {
    if (!isSolderTrace(conductor)) continue;
    const firstHole = conductor.path[0];
    if (firstHole === undefined) continue;
    const ownNet = nodeIndex.get(nodeSideKey(firstHole, conductor.side));

    const seenPairs = new Set<string>();
    for (const hole of conductor.path) {
      for (const neighbor of neighbors4(hole, doc.board)) {
        const neighborNet = nodeIndex.get(nodeSideKey(neighbor, conductor.side));
        if (neighborNet === undefined) continue; // empty pad: nothing to bridge to
        if (ownNet !== undefined && neighborNet.id === ownNet.id) continue; // same net: legitimate

        const pairKey = `${holeKey(hole)}|${holeKey(neighbor)}`;
        if (seenPairs.has(pairKey)) continue;
        seenPairs.add(pairKey);

        violations.push(
          makeViolation({
            rule: 'solder-trace-proximity',
            severity: 'warning',
            message:
              `Solder trace ${conductor.id} passes through ${safeHoleRef(hole)}, whose orthogonal ` +
              `neighbour ${safeHoleRef(neighbor)} belongs to a different net (~${gapMm.toFixed(2)} mm pad-edge ` +
              `gap at this board's pitch/pad size). Dragging solder along the trace risks bridging the two nets ` +
              `— the most common way a perfboard build fails. Verify clearance between ` +
              `${safeHoleRef(hole)} and ${safeHoleRef(neighbor)} before soldering.`,
            holes: [hole, neighbor],
            conductorIds: [conductor.id],
          }),
        );
      }
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 7 — pad-lifting risk on phenolic board (warning) — PLAN.md §5.2 R5''
// ---------------------------------------------------------------------------

function checkPadLiftingRisk(doc: PerfDocument, options: DrcOptions): DrcViolation[] {
  const violations: DrcViolation[] = [];
  if (doc.board.material !== 'FR2' && doc.board.material !== 'FR1') return violations;

  for (const conductor of doc.conductors) {
    if (conductor.kind !== 'solder-trace') continue; // pure trace only, not -wired
    if (conductor.path.length <= options.padLiftingMaxSolderTracePads) continue;

    violations.push(
      makeViolation({
        rule: 'pad-lifting-risk',
        severity: 'warning',
        message:
          `Pure solder trace ${conductor.id} spans ${conductor.path.length} pads on ${doc.board.material} ` +
          `(phenolic) board — beyond the ${options.padLiftingMaxSolderTracePads}-pad threshold. Phenolic pads ` +
          `lift under sustained soldering heat far more readily than FR-4; add a wire spine ` +
          `('solder-trace-wired') or split the run.`,
        holes: [...conductor.path],
        conductorIds: [conductor.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 8 — solder-trace feasibility (warning) — PLAN.md §5.2 R5'''
// ---------------------------------------------------------------------------

function checkSolderTraceFeasibility(doc: PerfDocument, options: DrcOptions): DrcViolation[] {
  const violations: DrcViolation[] = [];
  for (const conductor of doc.conductors) {
    if (conductor.kind !== 'solder-trace') continue;
    if (conductor.path.length <= options.solderTraceFeasibilityMaxPads) continue;

    violations.push(
      makeViolation({
        rule: 'solder-trace-too-long',
        severity: 'warning',
        message:
          `Pure solder trace ${conductor.id} spans ${conductor.path.length} pads, beyond the ` +
          `${options.solderTraceFeasibilityMaxPads}-pad feasibility threshold. Long pure-solder runs are ` +
          `mechanically unreliable and hard to reflow evenly; consider a wire spine ('solder-trace-wired').`,
        holes: [...conductor.path],
        conductorIds: [conductor.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 9 — current capacity (warning) — PLAN.md §5.2 rules 6/6'
// ---------------------------------------------------------------------------

function checkCurrentCapacity(doc: PerfDocument, options: DrcOptions): DrcViolation[] {
  const violations: DrcViolation[] = [];
  const netsById = new Map<NetId, Net>(doc.nets.map((n): [NetId, Net] => [n.id, n]));

  for (const conductor of doc.conductors) {
    if (!isSolderTrace(conductor)) continue;
    if (conductor.netId === undefined) continue;
    const net = netsById.get(conductor.netId);
    if (net === undefined || net.currentA === undefined) continue;

    const buildupArea = options.solderBuildupAreaMm2[conductor.buildup];
    const spineAreaMm2 =
      conductor.spine !== undefined ? Math.PI * (conductor.spine.gauge / 2) ** 2 : 0;
    // Cross-section for the capacity estimate: buildup area plus the spine's copper
    // area, per spec — a simple sum used only to size the ampacity threshold.
    const crossSectionMm2 = buildupArea + spineAreaMm2;
    const capacityA = crossSectionMm2 * options.maxCurrentDensityAPerMm2;
    if (net.currentA <= capacityA) continue;

    const lengthMm = pathLengthMm(conductor.path, doc.board);
    const solderR = resistanceOhm(options.solderResistivityUOhmCm, lengthMm, buildupArea);
    // Resistance/voltage-drop reporting (as opposed to the capacity estimate above)
    // models the solder fillet and the copper spine as two resistors of equal length
    // in parallel — the physically correct treatment for two bonded conductive paths
    // carrying the same current over the same run. This is slightly more optimistic
    // than PLAN.md §4.6's own worked example, which (for brevity) approximates the
    // wired case by the spine alone: for its 10-pad/0.6 mm example this model gives
    // ~1.3 mOhm against the plan's quoted "~1.5 mOhm" — well within the plan's own "≈".
    let totalR = solderR;
    if (conductor.spine !== undefined) {
      const copperR = resistanceOhm(options.copperResistivityUOhmCm, lengthMm, spineAreaMm2);
      totalR = (solderR * copperR) / (solderR + copperR);
    }
    const dropV = net.currentA * totalR;
    const spineNote =
      conductor.spine !== undefined
        ? ` + ${conductor.spine.gauge} mm ${conductor.spine.material} spine`
        : '';
    const recommendation =
      conductor.spine !== undefined
        ? ''
        : ' Adding a wire spine typically cuts this resistance by roughly an order of magnitude.';

    violations.push(
      makeViolation({
        rule: 'current-capacity',
        severity: 'warning',
        message:
          `Net '${net.name}' declares ${net.currentA} A but solder trace ${conductor.id} ` +
          `(${conductor.buildup} buildup${spineNote}) has an estimated cross-section of ` +
          `${crossSectionMm2.toFixed(3)} mm² (~${capacityA.toFixed(2)} A capacity at ` +
          `${options.maxCurrentDensityAPerMm2} A/mm²) — inadequate. Estimated resistance ~` +
          `${(totalR * 1000).toFixed(2)} mOhm over ${lengthMm.toFixed(1)} mm, giving a ~` +
          `${(dropV * 1000).toFixed(1)} mV drop at rated current.${recommendation}`,
        holes: [...conductor.path],
        conductorIds: [conductor.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 10 — creepage (warning) — PLAN.md §5.2 rule 7
// ---------------------------------------------------------------------------

function checkCreepage(
  doc: PerfDocument,
  options: DrcOptions,
  nodeIndex: ReadonlyMap<string, PhysicalNet>,
): DrcViolation[] {
  const violations: DrcViolation[] = [];
  const netsById = new Map<NetId, Net>(doc.nets.map((n): [NetId, Net] => [n.id, n]));
  const seenPairs = new Set<string>();

  for (const conductor of doc.conductors) {
    if (conductor.netId === undefined) continue;
    const net = netsById.get(conductor.netId);
    if (net === undefined || net.voltageV === undefined) continue;
    if (net.voltageV <= options.creepageVoltageThresholdV) continue;

    const firstHole = conductor.path[0];
    if (firstHole === undefined) continue;
    const ownNet = nodeIndex.get(nodeSideKey(firstHole, conductor.side));

    for (const hole of conductor.path) {
      for (const neighbor of neighbors4(hole, doc.board)) {
        const neighborNet = nodeIndex.get(nodeSideKey(neighbor, conductor.side));
        if (neighborNet === undefined) continue;
        if (ownNet !== undefined && neighborNet.id === ownNet.id) continue;

        const pairKey = `${conductor.id}|${holeKey(hole)}|${holeKey(neighbor)}`;
        if (seenPairs.has(pairKey)) continue;
        seenPairs.add(pairKey);

        violations.push(
          makeViolation({
            rule: 'creepage-clearance',
            severity: 'warning',
            message:
              `High voltage: net '${net.name}' (${net.voltageV} V) conductor ${conductor.id} runs through ` +
              `${safeHoleRef(hole)}, directly next to ${safeHoleRef(neighbor)} on a different net. ` +
              `2.54 mm hole spacing is near the practical creepage limit above ` +
              `${options.creepageVoltageThresholdV} V — increase clearance (skip a row/column) or reroute ` +
              `before building.`,
            holes: [hole, neighbor],
            conductorIds: [conductor.id],
          }),
        );
      }
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 11 — excessive lead-bend length (warning)
// ---------------------------------------------------------------------------

function checkLeadBendLength(doc: PerfDocument, options: DrcOptions): DrcViolation[] {
  const violations: DrcViolation[] = [];
  for (const conductor of doc.conductors) {
    if (!isLeadBend(conductor)) continue;
    const first = conductor.path[0];
    const last = conductor.path[conductor.path.length - 1];
    if (first === undefined || last === undefined) continue;

    const length = manhattan(first, last);
    if (length <= options.maxLeadBendHoles) continue;

    violations.push(
      makeViolation({
        rule: 'lead-bend-too-long',
        severity: 'warning',
        message:
          `Lead bend on ${conductor.componentId} pin ${conductor.pinNumber} spans ${length} holes ` +
          `(${safeHoleRef(first)} to ${safeHoleRef(last)}), beyond the ${options.maxLeadBendHoles}-hole ` +
          `reliability threshold. A long bent lead is mechanically fragile — use a wire instead.`,
        holes: [first, last],
        componentIds: [conductor.componentId],
        conductorIds: [conductor.id],
      }),
    );
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Rule 12 — pin not connected to anything (warning)
// ---------------------------------------------------------------------------

/**
 * Minimal connectivity check: a schematic pin whose physical net contains no
 * conductor and no other pin is definitely floating. This deliberately stops short
 * of full OPEN/SHORT/FLOATING classification against the schematic (PLAN.md §5.1) —
 * that is lvs.ts's job. A pin with a conductor attached that goes nowhere useful is
 * an "open", not caught here; only total isolation is.
 */
function checkUnconnectedPins(
  doc: PerfDocument,
  lookup: FootprintLookup,
  physicalNets: readonly PhysicalNet[],
): DrcViolation[] {
  const violations: DrcViolation[] = [];
  const componentsByRef = new Map<string, ComponentInstance>(
    doc.components.map((c): [string, ComponentInstance] => [c.ref, c]),
  );
  const seen = new Set<string>();

  for (const net of doc.nets) {
    for (const node of net.nodes) {
      const key = `${node.componentRef}.${node.pin}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const physNet = physicalNetForPin(physicalNets, {
        componentRef: node.componentRef,
        pin: node.pin,
      });
      if (physNet === undefined) continue; // unresolvable pin: not this rule's concern
      if (physNet.pins.length > 1 || physNet.conductorIds.length > 0) continue; // touches something

      const component = componentsByRef.get(node.componentRef);
      const footprint = component ? lookup(component.footprintId) : undefined;
      const hole = component && footprint ? pinHole(component, footprint, node.pin) : undefined;

      violations.push(
        makeViolation({
          rule: 'pin-not-connected',
          severity: 'warning',
          message:
            `Pin ${node.pin} of ${node.componentRef} (net '${net.name}') is not connected to anything: no ` +
            `conductor touches it and it shares no hole with another pin.`,
          holes: hole !== undefined ? [hole] : [],
          ...(component !== undefined ? { componentIds: [component.id] } : {}),
        }),
      );
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Aggregation and deterministic ordering
// ---------------------------------------------------------------------------

function compareHoleArrays(a: readonly HoleCoord[], b: readonly HoleCoord[]): number {
  const len = Math.min(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const ha = a[i];
    const hb = b[i];
    if (!ha || !hb) break;
    if (ha.col !== hb.col) return ha.col - hb.col;
    if (ha.row !== hb.row) return ha.row - hb.row;
  }
  return a.length - b.length;
}

/**
 * Total order over violations: by rule id, then severity, then the holes they name,
 * then the message as a final tiebreaker. This is what makes `runDrc`'s output
 * reproducible and diffable regardless of the (irrelevant) order rules happened to
 * run in internally.
 */
function compareViolations(a: DrcViolation, b: DrcViolation): number {
  if (a.rule !== b.rule) return compareStrings(a.rule, b.rule);
  if (a.severity !== b.severity) return compareStrings(a.severity, b.severity);
  const holeCmp = compareHoleArrays(a.holes, b.holes);
  if (holeCmp !== 0) return holeCmp;
  return compareStrings(a.message, b.message);
}

/**
 * Runs every DRC rule over `doc` and returns all violations, sorted deterministically
 * (see `compareViolations`). `lookup` resolves footprints exactly as connectivity.ts's
 * FootprintLookup does; components with an unknown footprint are silently skipped by
 * whichever rules need footprint data, matching connectivity.ts's own behaviour.
 */
export function runDrc(
  doc: PerfDocument,
  lookup: FootprintLookup,
  options?: Partial<DrcOptions>,
): DrcViolation[] {
  const resolved: DrcOptions = { ...DEFAULT_DRC_OPTIONS, ...options };

  const physicalNets = extractPhysicalNets(doc, lookup);
  const nodeIndex = buildNodeNetIndex(physicalNets);
  const conductorNetIndex = buildConductorNetIndex(physicalNets);

  const violations: DrcViolation[] = [
    ...checkComponentBodyOverlap(doc, lookup),
    ...checkComponentsOffBoard(doc, lookup),
    ...checkDuplicatePinHoles(doc, lookup),
    ...checkCrossingConductors(doc, conductorNetIndex),
    ...checkSolderTracePaths(doc),
    ...checkSolderTraceProximity(doc, nodeIndex),
    ...checkPadLiftingRisk(doc, resolved),
    ...checkSolderTraceFeasibility(doc, resolved),
    ...checkCurrentCapacity(doc, resolved),
    ...checkCreepage(doc, resolved, nodeIndex),
    ...checkLeadBendLength(doc, resolved),
    ...checkUnconnectedPins(doc, lookup, physicalNets),
  ];

  return violations.sort(compareViolations);
}
