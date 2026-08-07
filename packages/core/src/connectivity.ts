/**
 * Connectivity engine (PLAN.md §4.5).
 *
 * Determines what is ACTUALLY electrically connected on the board, as opposed to what
 * the schematic *intends* to be connected (see `PerfDocument.nets`). This is the most
 * correctness-critical file in the project: DRC, LVS and the soldering guide's
 * continuity/isolation tests are all derived from its output.
 *
 * Model: a union-find (disjoint set, path compression + union by rank) over nodes
 * identified by (hole, side), where side is 'top' | 'bottom'.
 *
 * Connection semantics — the crux of this file:
 *
 *  a) Every component pin occupies a hole. Its lead passes through the board, so it
 *     creates nodes at that hole on BOTH sides and unions them together.
 *
 *  b) For 'solder-trace', 'solder-trace-wired' and 'strip': EVERY hole along `path` is
 *     an electrical contact — the conductor is soldered down at each pad it crosses.
 *     All consecutive holes on the conductor's side are unioned.
 *
 *  c) For 'bare-wire', 'insulated-wire', 'top-jumper' and 'lead-bend': ONLY the two
 *     endpoints (path[0] and path[path.length - 1]) are soldered. Intermediate points
 *     are routing geometry, not electrical contacts — a wire passing over a pad does
 *     not connect to it.
 *
 * A node exists here only if something makes electrical contact at it: a component pin,
 * or a conductor contact point. The pads a wire merely passes over are deliberately NOT
 * registered. They are electrically indistinguishable from the thousands of empty pads
 * on the board, and registering them would emit a flood of meaningless single-node nets
 * that every consumer (LVS floating-conductor reporting in particular) would have to
 * filter back out. That a wire physically occupies a hole is a geometric fact and
 * belongs to the router's occupancy index, not to the connectivity graph.
 *
 * Pure and deterministic: no I/O, no Date.now(), no Math.random(). Output ordering is
 * fully sorted so extraction is reproducible and diffable.
 */

import type {
  BoardSide,
  Conductor,
  ConductorId,
  Footprint,
  HoleCoord,
  PerfDocument,
} from './model.js';
import { allPinHoles, holeKey } from './geometry.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type FootprintLookup = (footprintId: string) => Footprint | undefined;

export interface PhysicalNodeRef {
  readonly hole: HoleCoord;
  readonly side: BoardSide;
}

export interface PhysicalPinRef {
  readonly componentRef: string;
  readonly pin: string;
}

export interface PhysicalNet {
  /** Stable, derived from its lowest-sorted node — not a counter. */
  readonly id: string;
  readonly nodes: readonly PhysicalNodeRef[];
  readonly pins: readonly PhysicalPinRef[];
  readonly conductorIds: readonly ConductorId[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * String key for a (hole, side) node, used as the union-find element identity.
 * Built on geometry.holeKey so there is exactly one hole-encoding in the codebase.
 */
function nodeKey(hole: HoleCoord, side: BoardSide): string {
  return `${holeKey(hole)}@${side}`;
}

/** Ascii-safe string compare — avoids locale-dependent ordering across platforms. */
function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function compareNode(a: PhysicalNodeRef, b: PhysicalNodeRef): number {
  if (a.hole.col !== b.hole.col) return a.hole.col - b.hole.col;
  if (a.hole.row !== b.hole.row) return a.hole.row - b.hole.row;
  return compareStrings(a.side, b.side);
}

function comparePin(a: PhysicalPinRef, b: PhysicalPinRef): number {
  const byRef = compareStrings(a.componentRef, b.componentRef);
  return byRef !== 0 ? byRef : compareStrings(a.pin, b.pin);
}

/**
 * Conductor kinds where every hole along `path` is an electrical contact point
 * (rule b). All other kinds only make contact at their two endpoints (rule c).
 */
function unionsAllPathHoles(kind: Conductor['kind']): boolean {
  return kind === 'solder-trace' || kind === 'solder-trace-wired' || kind === 'strip';
}

// ---------------------------------------------------------------------------
// Union-find (disjoint set): path compression + union by rank
// ---------------------------------------------------------------------------

class DisjointSet {
  readonly #parent = new Map<string, string>();
  readonly #rank = new Map<string, number>();

  /** Idempotent: registers `x` as its own singleton set if not already known. */
  makeSet(x: string): void {
    if (!this.#parent.has(x)) {
      this.#parent.set(x, x);
      this.#rank.set(x, 0);
    }
  }

  find(x: string): string {
    this.makeSet(x);

    let root = x;
    for (;;) {
      const parent = this.#parent.get(root);
      if (parent === undefined || parent === root) break;
      root = parent;
    }

    // Path compression: re-point every visited node directly at the root.
    let cur = x;
    while (cur !== root) {
      const next = this.#parent.get(cur);
      if (next === undefined) break;
      this.#parent.set(cur, root);
      cur = next;
    }

    return root;
  }

  union(a: string, b: string): void {
    const ra = this.find(a);
    const rb = this.find(b);
    if (ra === rb) return;

    const rankA = this.#rank.get(ra) ?? 0;
    const rankB = this.#rank.get(rb) ?? 0;

    if (rankA < rankB) {
      this.#parent.set(ra, rb);
    } else if (rankA > rankB) {
      this.#parent.set(rb, ra);
    } else {
      this.#parent.set(rb, ra);
      this.#rank.set(ra, rankA + 1);
    }
  }
}

// ---------------------------------------------------------------------------
// Extraction
// ---------------------------------------------------------------------------

interface Group {
  readonly nodes: PhysicalNodeRef[];
  readonly pins: PhysicalPinRef[];
  readonly conductorIds: ConductorId[];
}

function netIdFor(lowest: PhysicalNodeRef): string {
  return `net:${lowest.hole.col}:${lowest.hole.row}:${lowest.side}`;
}

/** Every electrically-distinct island on the board, sorted deterministically. */
export function extractPhysicalNets(doc: PerfDocument, lookup: FootprintLookup): PhysicalNet[] {
  const ds = new DisjointSet();
  const nodeInfo = new Map<string, PhysicalNodeRef>();

  function touch(hole: HoleCoord, side: BoardSide): string {
    const key = nodeKey(hole, side);
    if (!nodeInfo.has(key)) nodeInfo.set(key, { hole, side });
    ds.makeSet(key);
    return key;
  }

  // --- Pass 1: component pins bridge top and bottom at their hole (rule a). ---
  const pinEntries: Array<{ readonly ref: PhysicalPinRef; readonly key: string }> = [];

  for (const component of doc.components) {
    const footprint = lookup(component.footprintId);
    if (!footprint) continue; // Unknown footprint: skip silently, record nothing.

    for (const { pin, hole } of allPinHoles(component, footprint)) {
      const topKey = touch(hole, 'top');
      const bottomKey = touch(hole, 'bottom');
      ds.union(topKey, bottomKey);
      pinEntries.push({ ref: { componentRef: component.ref, pin: pin.number }, key: topKey });
    }
  }

  // --- Pass 2: conductors (rules b and c). ---
  // Only CONTACT holes become nodes. For rule-b kinds that is every hole along the
  // path; for rule-c kinds it is the two endpoints only. Holes a rule-c conductor
  // merely passes over are not registered — see the note in the module header.
  const conductorContact = new Map<ConductorId, string>();

  for (const conductor of doc.conductors) {
    const path = conductor.path;
    if (path.length === 0) continue;

    let contact: string | undefined;

    if (unionsAllPathHoles(conductor.kind)) {
      const keys = path.map((hole) => touch(hole, conductor.side));
      for (let i = 0; i + 1 < keys.length; i++) {
        const a = keys[i];
        const b = keys[i + 1];
        if (a !== undefined && b !== undefined) ds.union(a, b);
      }
      contact = keys[0];
    } else {
      const first = path[0];
      const last = path[path.length - 1];
      if (first !== undefined && last !== undefined) {
        const firstKey = touch(first, conductor.side);
        const lastKey = touch(last, conductor.side);
        ds.union(firstKey, lastKey);
        contact = firstKey;
      }
    }

    if (contact !== undefined) conductorContact.set(conductor.id, contact);
  }

  // --- Assemble groups by union-find root. ---
  const groups = new Map<string, Group>();
  function groupFor(root: string): Group {
    let g = groups.get(root);
    if (!g) {
      g = { nodes: [], pins: [], conductorIds: [] };
      groups.set(root, g);
    }
    return g;
  }

  for (const [key, info] of nodeInfo) {
    groupFor(ds.find(key)).nodes.push(info);
  }
  for (const entry of pinEntries) {
    groupFor(ds.find(entry.key)).pins.push(entry.ref);
  }
  for (const [conductorId, key] of conductorContact) {
    groupFor(ds.find(key)).conductorIds.push(conductorId);
  }

  // --- Build sorted, deterministic output. ---
  const withLowest: Array<{ readonly net: PhysicalNet; readonly lowest: PhysicalNodeRef }> = [];

  for (const group of groups.values()) {
    const nodes = [...group.nodes].sort(compareNode);
    const lowest = nodes[0];
    if (lowest === undefined) continue; // Unreachable: a group always has >= 1 node.

    const pins = [...group.pins].sort(comparePin);
    const conductorIds = [...new Set(group.conductorIds)].sort(compareStrings);

    withLowest.push({ net: { id: netIdFor(lowest), nodes, pins, conductorIds }, lowest });
  }

  withLowest.sort((a, b) => compareNode(a.lowest, b.lowest));
  return withLowest.map((entry) => entry.net);
}

function pinRefEquals(a: PhysicalPinRef, b: PhysicalPinRef): boolean {
  return a.componentRef === b.componentRef && a.pin === b.pin;
}

/** The physical net containing a given pin, if any. */
export function netOfPin(
  doc: PerfDocument,
  lookup: FootprintLookup,
  pin: PhysicalPinRef,
): PhysicalNet | undefined {
  const nets = extractPhysicalNets(doc, lookup);
  return nets.find((net) => net.pins.some((p) => pinRefEquals(p, pin)));
}

/** True iff the two pins end up in the same physical net. */
export function arePinsConnected(
  doc: PerfDocument,
  lookup: FootprintLookup,
  a: PhysicalPinRef,
  b: PhysicalPinRef,
): boolean {
  const nets = extractPhysicalNets(doc, lookup);
  const netOf = (pin: PhysicalPinRef): PhysicalNet | undefined =>
    nets.find((net) => net.pins.some((p) => pinRefEquals(p, pin)));

  const netA = netOf(a);
  const netB = netOf(b);
  return netA !== undefined && netB !== undefined && netA.id === netB.id;
}
