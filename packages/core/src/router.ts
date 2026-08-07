/**
 * Connection router (PLAN.md §6).
 *
 * WHAT THIS IS, AND DELIBERATELY IS NOT.
 *
 * This is not a press-the-button-and-get-a-finished-board autorouter. Every existing
 * perfboard tool that promised that produced the complaint the plan quotes: "it routed
 * most of it and left four connections that were then impossible to finish by hand."
 * The target here is an interactive assistant: route ONE connection well, fast enough
 * that dragging a part can re-route what it touched, and be honest when nothing works.
 *
 * HOW IT DECIDES.
 *
 * Perfboard offers several physically different ways to join two points, so the router
 * evaluates each as a candidate strategy and picks the cheapest feasible one. That maps
 * directly onto the cost table in PLAN.md §6.1:
 *
 *   solder trace        cheap per step, but orthogonal only, cannot cross other copper
 *   wired solder trace  same path, plus a spine — for long runs and current-carrying rails
 *   bare wire           cheap, straight, but cannot cross other copper
 *   insulated wire      crosses anything, costs preparation time
 *   top jumper          last resort: visible, and it occupies component space
 *
 * THE PART THAT MATTERS MOST.
 *
 * The solder-trace search puts R5' — the ~0.6 mm gap to a neighbouring pad of another
 * net — into the COST, not into a post-hoc warning. A router that merely avoids illegal
 * routes produces boards that are legal and unpleasant to solder. A router that prices
 * the risk produces boards that are legal AND buildable, and that is the whole argument
 * for this project existing (PLAN.md §6.1).
 *
 * Deterministic: no clock, no RNG. The same board and request always give the same route.
 */

import type {
  Board,
  ComponentId,
  Conductor,
  HoleCoord,
  NetId,
  PerfDocument,
  SolderBuildup,
} from './model.js';
import type { NewConductor } from './commands.js';
import type { FootprintLookup, PhysicalNet } from './connectivity.js';
import { extractPhysicalNets } from './connectivity.js';
import {
  coordToHoleRef,
  formatHole,
  holeKey,
  holeToMm,
  isInsideBoard,
  manhattan,
  neighbors4,
  pathLengthMm,
  sameHole,
} from './geometry.js';
import { buildOccupancy } from './occupancy.js';
import type { OccupancyIndex } from './occupancy.js';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface RouterCosts {
  /** Per hole stepped along a pure solder trace. Cheap: this is the preferred primitive. */
  readonly solderTraceStep: number;
  /** One-off cost of preparing and laying a wire spine along a trace. */
  readonly solderTraceSpineFixed: number;
  readonly bareWireFixed: number;
  readonly bareWirePerMm: number;
  readonly insulatedWireFixed: number;
  readonly insulatedWirePerMm: number;
  readonly topJumperFixed: number;
  readonly topJumperPerMm: number;
  /**
   * Charged per trace hole that has a different-net pad as an orthogonal neighbour.
   * This is DRC rule R5' expressed as money instead of a warning, so the search steers
   * around risky ground instead of merely reporting it afterwards.
   */
  readonly proximityRisk: number;
}

export interface RouterOptions {
  readonly costs: RouterCosts;
  /** Beyond this many pads, a pure solder trace is unreliable; a spine gets proposed. */
  readonly maxPureSolderTracePads: number;
  /** Top jumpers are ugly and block component space; off by default. */
  readonly allowTopJumper: boolean;
  /** Search ceiling, so a hopeless request fails fast instead of scanning the board. */
  readonly maxExpandedNodes: number;
}

export const DEFAULT_ROUTER_COSTS: RouterCosts = {
  solderTraceStep: 1,
  solderTraceSpineFixed: 6,
  bareWireFixed: 8,
  bareWirePerMm: 0.15,
  insulatedWireFixed: 18,
  insulatedWirePerMm: 0.2,
  topJumperFixed: 40,
  topJumperPerMm: 0.3,
  proximityRisk: 12,
};

export const DEFAULT_ROUTER_OPTIONS: RouterOptions = {
  costs: DEFAULT_ROUTER_COSTS,
  maxPureSolderTracePads: 6,
  allowTopJumper: false,
  maxExpandedNodes: 20000,
};

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

export type RouteStrategy =
  | 'solder-trace'
  | 'solder-trace-wired'
  | 'bare-wire'
  | 'insulated-wire'
  | 'top-jumper';

export interface RouteCandidate {
  readonly strategy: RouteStrategy;
  readonly conductors: readonly NewConductor[];
  readonly cost: number;
  /** Why this came out the way it did. Surfaced in the UI and reused by the guide. */
  readonly explanation: string;
  /** Trace holes that sit next to a different net — these become measurement steps. */
  readonly riskHoles: readonly HoleCoord[];
}

export interface RouteResult {
  readonly ok: boolean;
  readonly best?: RouteCandidate;
  /** Every feasible strategy, cheapest first. Lets the UI offer "use a wire instead". */
  readonly alternatives: readonly RouteCandidate[];
  /** Populated only when nothing was feasible. Never fails silently. */
  readonly reason?: string;
}

export interface RouteRequest {
  readonly from: HoleCoord;
  readonly to: HoleCoord;
  /** Net being routed. Holes already on this net are free to pass through. */
  readonly netId?: NetId;
  readonly buildup?: SolderBuildup;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export function routeConnection(
  doc: PerfDocument,
  lookup: FootprintLookup,
  request: RouteRequest,
  options: Partial<RouterOptions> = {},
): RouteResult {
  const opts: RouterOptions = {
    ...DEFAULT_ROUTER_OPTIONS,
    ...options,
    costs: { ...DEFAULT_ROUTER_COSTS, ...(options.costs ?? {}) },
  };
  const { from, to } = request;

  if (!isInsideBoard(from, doc.board) || !isInsideBoard(to, doc.board)) {
    return { ok: false, alternatives: [], reason: 'Endpoint is outside the board.' };
  }
  if (sameHole(from, to)) {
    return { ok: false, alternatives: [], reason: 'Start and end are the same hole.' };
  }

  const occupancy = buildOccupancy(doc, lookup);
  const netAt = buildNetIndex(doc, lookup);
  const ctx: RouteContext = { doc, occupancy, netAt, opts, ownNetId: request.netId };

  const candidates: RouteCandidate[] = [];
  const trace = solderTraceCandidate(ctx, from, to, request.buildup ?? 'normal');
  if (trace) candidates.push(trace);
  const bare = straightWireCandidate(ctx, from, to, 'bare-wire');
  if (bare) candidates.push(bare);
  const insulated = straightWireCandidate(ctx, from, to, 'insulated-wire');
  if (insulated) candidates.push(insulated);
  if (opts.allowTopJumper) {
    const jumper = straightWireCandidate(ctx, from, to, 'top-jumper');
    if (jumper) candidates.push(jumper);
  }

  candidates.sort((a, b) => a.cost - b.cost || a.strategy.localeCompare(b.strategy));
  const best = candidates[0];
  if (!best) {
    return {
      ok: false,
      alternatives: [],
      reason:
        `No route found from ${formatHole(from)} to ${formatHole(to)}. ` +
        `Every strategy was blocked — try moving a part, or allow a top jumper.`,
    };
  }
  return { ok: true, best, alternatives: candidates };
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface RouteContext {
  readonly doc: PerfDocument;
  readonly occupancy: OccupancyIndex;
  /** Physical net id occupying a hole's solder side, if any. */
  readonly netAt: (hole: HoleCoord) => string | undefined;
  readonly opts: RouterOptions;
  readonly ownNetId: NetId | undefined;
}

function buildNetIndex(
  doc: PerfDocument,
  lookup: FootprintLookup,
): (hole: HoleCoord) => string | undefined {
  const nets: PhysicalNet[] = extractPhysicalNets(doc, lookup);
  const byHole = new Map<string, string>();
  for (const net of nets) {
    for (const node of net.nodes) byHole.set(holeKey(node.hole), net.id);
  }
  return (hole) => byHole.get(holeKey(hole));
}

// ---------------------------------------------------------------------------
// Strategy 1: solder trace, via A* over the 4-neighbour grid
// ---------------------------------------------------------------------------

function solderTraceCandidate(
  ctx: RouteContext,
  from: HoleCoord,
  to: HoleCoord,
  buildup: SolderBuildup,
): RouteCandidate | undefined {
  const path = findSolderTracePath(ctx, from, to);
  if (!path) return undefined;

  const { costs, maxPureSolderTracePads } = ctx.opts;
  const riskHoles = path.filter((h) => hasForeignNeighbour(ctx, h, from, to));
  const stepCost = (path.length - 1) * costs.solderTraceStep;
  const riskCost = riskHoles.length * costs.proximityRisk;

  const needsSpine = path.length > maxPureSolderTracePads;
  const kind: Conductor['kind'] = needsSpine ? 'solder-trace-wired' : 'solder-trace';
  const spineCost = needsSpine ? costs.solderTraceSpineFixed : 0;

  const conductor: NewConductor = needsSpine
    ? {
        kind: 'solder-trace-wired',
        path,
        side: 'bottom',
        layerZ: 0,
        buildup,
        spine: { material: 'tinned-copper', gauge: 0.6 },
        ...(ctx.ownNetId !== undefined ? { netId: ctx.ownNetId } : {}),
      }
    : {
        kind: 'solder-trace',
        path,
        side: 'bottom',
        layerZ: 0,
        buildup,
        ...(ctx.ownNetId !== undefined ? { netId: ctx.ownNetId } : {}),
      };

  const parts: string[] = [
    `${path.length} pads from ${coordToHoleRef(from)} to ${coordToHoleRef(to)}`,
  ];
  if (needsSpine) {
    parts.push(
      `longer than ${maxPureSolderTracePads} pads, so a tinned-copper spine is proposed — ` +
        `it drops the resistance by roughly an order of magnitude and makes the joint repeatable`,
    );
  }
  if (riskHoles.length > 0) {
    parts.push(
      `${riskHoles.length} pad(s) sit next to a different net (${riskHoles
        .map(formatHole)
        .join(', ')}) — check isolation there after soldering`,
    );
  }

  return {
    strategy: kind === 'solder-trace-wired' ? 'solder-trace-wired' : 'solder-trace',
    conductors: [conductor],
    cost: stepCost + riskCost + spineCost,
    explanation: `Solder trace: ${parts.join('; ')}.`,
    riskHoles,
  };
}

/**
 * A* over holes, 4-connected. Blocked cells are holes whose solder-side copper already
 * belongs to something else; the two endpoints are always allowed since they are what we
 * are joining. The proximity risk is charged as step cost so the search prefers routes
 * that keep clear of foreign pads rather than merely legal ones.
 */
function findSolderTracePath(
  ctx: RouteContext,
  from: HoleCoord,
  to: HoleCoord,
): HoleCoord[] | undefined {
  const { costs, maxExpandedNodes } = ctx.opts;
  const startKey = holeKey(from);
  const goalKey = holeKey(to);

  const gScore = new Map<string, number>([[startKey, 0]]);
  const cameFrom = new Map<string, HoleCoord>();
  const open: Array<{ hole: HoleCoord; f: number }> = [
    { hole: from, f: manhattan(from, to) * costs.solderTraceStep },
  ];
  const closed = new Set<string>();
  let expanded = 0;

  while (open.length > 0) {
    // Small boards and short routes: a linear scan beats the constant factor of a heap.
    let bestIndex = 0;
    for (let i = 1; i < open.length; i++) {
      const entry = open[i];
      const bestEntry = open[bestIndex];
      if (entry && bestEntry && entry.f < bestEntry.f) bestIndex = i;
    }
    const current = open.splice(bestIndex, 1)[0];
    if (!current) break;

    const currentKey = holeKey(current.hole);
    if (currentKey === goalKey) return reconstruct(cameFrom, current.hole, from);
    if (closed.has(currentKey)) continue;
    closed.add(currentKey);

    if (++expanded > maxExpandedNodes) return undefined;

    const g = gScore.get(currentKey) ?? Infinity;
    for (const next of neighbors4(current.hole, ctx.doc.board)) {
      const nextKey = holeKey(next);
      if (closed.has(nextKey)) continue;
      const isEndpoint = nextKey === goalKey;
      if (!isEndpoint && !isTraversableByTrace(ctx, next)) continue;

      let step = costs.solderTraceStep;
      if (hasForeignNeighbour(ctx, next, from, to)) step += costs.proximityRisk;

      const tentative = g + step;
      if (tentative >= (gScore.get(nextKey) ?? Infinity)) continue;

      gScore.set(nextKey, tentative);
      cameFrom.set(nextKey, current.hole);
      open.push({ hole: next, f: tentative + manhattan(next, to) * costs.solderTraceStep });
    }
  }
  return undefined;
}

/** A trace may pass through a hole that is empty, or already on the net being routed. */
function isTraversableByTrace(ctx: RouteContext, hole: HoleCoord): boolean {
  if (ctx.occupancy.isCopperBlocked(hole, 'bottom')) return false;
  const pin = ctx.occupancy.pinAt(hole);
  // A foreign pin in the way is a hard stop: soldering across it would short it in.
  if (pin) return false;
  return true;
}

/**
 * Does this hole have an orthogonal neighbour belonging to a different net? At 2.54 mm
 * pitch the pad-edge gap is well under a millimetre, so this is where a dragged bead of
 * solder ends up somewhere it should not. DRC rule R5', priced into the search.
 */
function hasForeignNeighbour(
  ctx: RouteContext,
  hole: HoleCoord,
  from: HoleCoord,
  to: HoleCoord,
): boolean {
  const ownNets = new Set<string>();
  for (const endpoint of [from, to]) {
    const id = ctx.netAt(endpoint);
    if (id !== undefined) ownNets.add(id);
  }
  for (const neighbour of neighbors4(hole, ctx.doc.board)) {
    if (sameHole(neighbour, from) || sameHole(neighbour, to)) continue;
    const netId = ctx.netAt(neighbour);
    if (netId !== undefined && !ownNets.has(netId)) return true;
  }
  return false;
}

function reconstruct(
  cameFrom: Map<string, HoleCoord>,
  goal: HoleCoord,
  start: HoleCoord,
): HoleCoord[] {
  const path: HoleCoord[] = [goal];
  let cursor = goal;
  while (!sameHole(cursor, start)) {
    const prev = cameFrom.get(holeKey(cursor));
    if (!prev) break;
    path.push(prev);
    cursor = prev;
  }
  path.reverse();
  return path;
}

// ---------------------------------------------------------------------------
// Strategies 2-4: straight wires
// ---------------------------------------------------------------------------

function straightWireCandidate(
  ctx: RouteContext,
  from: HoleCoord,
  to: HoleCoord,
  kind: 'bare-wire' | 'insulated-wire' | 'top-jumper',
): RouteCandidate | undefined {
  const { costs } = ctx.opts;
  const crossed = holesUnderStraightLine(from, to);

  if (kind === 'bare-wire') {
    // Bare wire cannot cross another conductor's copper or sit on a foreign pad.
    for (const hole of crossed) {
      if (sameHole(hole, from) || sameHole(hole, to)) continue;
      if (ctx.occupancy.isCopperBlocked(hole, 'bottom')) return undefined;
      if (ctx.occupancy.pinAt(hole)) return undefined;
    }
  }
  if (kind === 'top-jumper') {
    // A top jumper must not have to run underneath a component body.
    for (const hole of crossed) {
      if (ctx.occupancy.bodyCovers(hole)) return undefined;
    }
  }

  const lengthMm = pathLengthMm([from, to], ctx.doc.board);
  const fixed =
    kind === 'bare-wire'
      ? costs.bareWireFixed
      : kind === 'insulated-wire'
        ? costs.insulatedWireFixed
        : costs.topJumperFixed;
  const perMm =
    kind === 'bare-wire'
      ? costs.bareWirePerMm
      : kind === 'insulated-wire'
        ? costs.insulatedWirePerMm
        : costs.topJumperPerMm;

  const conductor: NewConductor = {
    kind,
    path: [from, to],
    side: kind === 'top-jumper' ? 'top' : 'bottom',
    layerZ: kind === 'insulated-wire' ? 1 : 0,
    ...(ctx.ownNetId !== undefined ? { netId: ctx.ownNetId } : {}),
  };

  const note =
    kind === 'bare-wire'
      ? 'clear straight run on the solder side'
      : kind === 'insulated-wire'
        ? 'insulated, so it may pass over other conductors'
        : 'component-side jumper — visible, and it takes up board space';

  return {
    strategy: kind,
    conductors: [conductor],
    cost: fixed + lengthMm * perMm,
    explanation:
      `${kind.replace('-', ' ')}: ${lengthMm.toFixed(1)} mm from ` +
      `${coordToHoleRef(from)} to ${coordToHoleRef(to)} — ${note}.`,
    riskHoles: [],
  };
}

/**
 * Holes a straight wire physically passes over, so occupancy can be checked. Sampled
 * along the segment at a fraction of the pitch, which is dense enough that no hole on
 * the line is missed.
 */
function holesUnderStraightLine(from: HoleCoord, to: HoleCoord): HoleCoord[] {
  const steps = Math.max(Math.abs(to.col - from.col), Math.abs(to.row - from.row)) * 4;
  const seen = new Set<string>();
  const result: HoleCoord[] = [];
  for (let i = 0; i <= steps; i++) {
    const t = steps === 0 ? 0 : i / steps;
    const hole: HoleCoord = {
      col: Math.round(from.col + (to.col - from.col) * t),
      row: Math.round(from.row + (to.row - from.row) * t),
    };
    const k = holeKey(hole);
    if (!seen.has(k)) {
      seen.add(k);
      result.push(hole);
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Helpers used by callers
// ---------------------------------------------------------------------------

/** Straight-line distance in mm, for callers ordering work by how far apart pins are. */
export function connectionLengthMm(from: HoleCoord, to: HoleCoord, board: Board): number {
  const a = holeToMm(from, board);
  const b = holeToMm(to, board);
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export type { ComponentId };
