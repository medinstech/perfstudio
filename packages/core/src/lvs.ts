/**
 * LVS — Layout versus Schematic (PLAN.md §5.1, §1).
 *
 * `doc.nets` is the schematic's INTENT, imported from a netlist. `extractPhysicalNets`
 * (connectivity.ts) is what the board ACTUALLY connects, derived purely from conductors
 * and component pins. LVS compares the two and answers the question a builder actually
 * cares about before reaching for a soldering iron: does this board implement my
 * circuit, yes or no — and if not, exactly where does it diverge.
 *
 * Failure classes fall out directly from comparing an intent graph to a reality graph:
 *   - OPEN                — the schematic says "one net", the board says "more than one".
 *   - SHORT                — the schematic says "more than one net", the board says "one".
 *   - FLOATING CONDUCTOR   — the board has a connection the schematic never asked for.
 *   - UNPLACED COMPONENT / UNKNOWN FOOTPRINT — the schematic refers to hardware the
 *     board doesn't have (yet), so nothing about its connectivity can be judged.
 *   - UNROUTED NET         — every pin exists, but not a single wire or trace has been
 *     run between any of them; this is the common state right after a fresh netlist
 *     import and deserves a plainer, less alarming message than "open" (PLAN.md §13:
 *     unrouted nets must be reported explicitly, never silently dropped).
 *
 * Pure and deterministic: no I/O, no Date.now(), no Math.random(). Issue ordering is
 * fully sorted so results are reproducible and diffable across runs.
 */

import { extractPhysicalNets, type FootprintLookup, type PhysicalNet, type PhysicalPinRef } from './connectivity.js';
import { coordToHoleRef } from './geometry.js';
import type { ComponentInstance, ConductorId, Net, NetClass, NetId, NetNode, PerfDocument } from './model.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type LvsIssueKind =
  | 'open' // pins the schematic says are one net are split across >1 physical net
  | 'short' // pins from >1 schematic net share one physical net
  | 'floating-conductor' // a conductor in a physical net containing no pins at all
  | 'unplaced-component' // a schematic net references a component ref not present on the board
  | 'unknown-footprint' // a placed component whose footprintId the lookup cannot resolve
  | 'unrouted-net'; // a schematic net whose pins are all present but none are connected

export interface LvsIssue {
  readonly kind: LvsIssueKind;
  readonly message: string;
  readonly netNames: readonly string[];
  readonly pins: readonly PhysicalPinRef[];
  readonly physicalNetIds: readonly string[];
  readonly conductorIds?: readonly ConductorId[];
}

export interface LvsResult {
  readonly ok: boolean;
  readonly issues: readonly LvsIssue[];
  readonly summary: {
    readonly schematicNets: number;
    readonly physicalNets: number;
    readonly matchedNets: number;
    /** Under-connected nets: 'open' AND 'unrouted-net' together, never just one. */
    readonly opens: number;
    readonly shorts: number;
  };
}

export interface ContinuityCheck {
  readonly a: PhysicalPinRef;
  readonly b: PhysicalPinRef;
  readonly netName: string;
}

export interface IsolationCheck {
  readonly a: PhysicalPinRef;
  readonly b: PhysicalPinRef;
  readonly netA: string;
  readonly netB: string;
}

// ---------------------------------------------------------------------------
// Small deterministic helpers.
//
// These are plain string/pin comparators local to LVS's own output ordering and
// messages — not a re-implementation of the union-find or hole maths that live in
// connectivity.ts / geometry.ts, which this module only ever calls into.
// ---------------------------------------------------------------------------

/** Ascii-safe string compare — avoids locale-dependent ordering across platforms. */
function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function comparePinRef(a: PhysicalPinRef, b: PhysicalPinRef): number {
  const byRef = compareStrings(a.componentRef, b.componentRef);
  return byRef !== 0 ? byRef : compareStrings(a.pin, b.pin);
}

function formatPinRef(p: PhysicalPinRef): string {
  return `${p.componentRef}.${p.pin}`;
}

function toPinRef(node: NetNode): PhysicalPinRef {
  return { componentRef: node.componentRef, pin: node.pin };
}

/** Map key for a (componentRef, pin) pair — works for both NetNode and PhysicalPinRef. */
function pinKey(p: { readonly componentRef: string; readonly pin: string }): string {
  return `${p.componentRef}::${p.pin}`;
}

/** Sort order for the final issues list: kind, then first net name, then first pin. */
function compareIssue(a: LvsIssue, b: LvsIssue): number {
  const byKind = compareStrings(a.kind, b.kind);
  if (byKind !== 0) return byKind;

  const aNet = a.netNames[0] ?? '';
  const bNet = b.netNames[0] ?? '';
  const byNet = compareStrings(aNet, bNet);
  if (byNet !== 0) return byNet;

  const aPin = a.pins[0];
  const bPin = b.pins[0];
  if (aPin === undefined && bPin === undefined) return 0;
  if (aPin === undefined) return -1;
  if (bPin === undefined) return 1;
  return comparePinRef(aPin, bPin);
}

/** Human label for a physical net group in an OPEN message: a hole ref, or "nowhere". */
function physicalNetLabel(id: string | undefined, physicalNetById: ReadonlyMap<string, PhysicalNet>): string {
  if (id === undefined) return 'not connected to any other pin in this net';
  const pn = physicalNetById.get(id);
  const lowest = pn?.nodes[0];
  return lowest === undefined ? `physical net ${id}` : `near ${coordToHoleRef(lowest.hole)}`;
}

// ---------------------------------------------------------------------------
// LVS
// ---------------------------------------------------------------------------

type PinStatus = 'ok' | 'unplaced' | 'unknown-footprint';

interface ClassifiedNode {
  readonly node: NetNode;
  readonly status: PinStatus;
  /** Only meaningful when status === 'ok'; undefined means the pin resolved to no hole. */
  readonly physicalNetId: string | undefined;
}

export function runLvs(doc: PerfDocument, lookup: FootprintLookup): LvsResult {
  const physicalNets = extractPhysicalNets(doc, lookup);
  const physicalNetById = new Map<string, PhysicalNet>(physicalNets.map((pn) => [pn.id, pn]));

  const pinToPhysicalNetId = new Map<string, string>();
  for (const pn of physicalNets) {
    for (const pin of pn.pins) pinToPhysicalNetId.set(pinKey(pin), pn.id);
  }

  const componentsByRef = new Map<string, ComponentInstance>();
  for (const c of doc.components) {
    if (!componentsByRef.has(c.ref)) componentsByRef.set(c.ref, c);
  }

  // Schematic pin -> the one net that declares it. Used to attribute physical-net
  // membership back to schematic intent for short detection.
  const pinToNet = new Map<string, Net>();
  for (const net of doc.nets) {
    for (const node of net.nodes) pinToNet.set(pinKey(node), net);
  }

  const issues: LvsIssue[] = [];

  // --- Pass 1: floating conductors — physical islands with no pin at all. ---
  for (const pn of physicalNets) {
    if (pn.pins.length > 0) continue;

    const holeRefs = [...new Set(pn.nodes.map((n) => coordToHoleRef(n.hole)))].sort(compareStrings);
    issues.push({
      kind: 'floating-conductor',
      message:
        `Conductor(s) ${pn.conductorIds.join(', ')} form an isolated island at ` +
        `${holeRefs.join(', ')} with no component pin attached — likely a stray solder ` +
        `trace, wire or bridge left over from editing.`,
      netNames: [],
      pins: [],
      physicalNetIds: [pn.id],
      conductorIds: pn.conductorIds,
    });
  }

  // --- Pass 2: shorts — physical nets that swallow pins from more than one schematic net. ---
  const shortedPhysicalNetIds = new Set<string>();

  for (const pn of physicalNets) {
    const perNet = new Map<NetId, { readonly net: Net; readonly pins: PhysicalPinRef[] }>();
    for (const pin of pn.pins) {
      const net = pinToNet.get(pinKey(pin));
      if (net === undefined) continue; // A pin absent from every schematic net is not an error.
      const bucket = perNet.get(net.id);
      if (bucket) bucket.pins.push(pin);
      else perNet.set(net.id, { net, pins: [pin] });
    }
    if (perNet.size < 2) continue;

    shortedPhysicalNetIds.add(pn.id);

    const buckets = [...perNet.values()];
    const allPins = buckets.flatMap((b) => b.pins).sort(comparePinRef);
    const netNames = buckets.map((b) => b.net.name).sort(compareStrings);
    const isPowerGroundShort =
      buckets.some((b) => b.net.class === 'ground') && buckets.some((b) => b.net.class === 'power');

    const lowest = pn.nodes[0];
    const holeRef = lowest === undefined ? pn.id : coordToHoleRef(lowest.hole);
    const prefix = isPowerGroundShort ? 'CRITICAL SHORT (power tied to ground): ' : 'SHORT: ';

    issues.push({
      kind: 'short',
      message:
        `${prefix}the physical connection at ${holeRef} ties together schematic nets ` +
        `${netNames.map((n) => `'${n}'`).join(' and ')} — pins ${allPins.map(formatPinRef).join(', ')}. ` +
        `This is almost certainly a solder bridge; separate the pads and re-measure isolation ` +
        `before applying power.`,
      netNames,
      pins: allPins,
      physicalNetIds: [pn.id],
      conductorIds: pn.conductorIds,
    });
  }

  // --- Pass 3: per schematic net — unplaced / unknown-footprint / open / unrouted,
  // and which nets are MATCHED (realised exactly, no more, no less). ---
  let matchedNets = 0;

  for (const net of doc.nets) {
    const classified: ClassifiedNode[] = net.nodes.map((node) => {
      const comp = componentsByRef.get(node.componentRef);
      if (comp === undefined) return { node, status: 'unplaced', physicalNetId: undefined };

      const footprint = lookup(comp.footprintId);
      if (footprint === undefined) return { node, status: 'unknown-footprint', physicalNetId: undefined };

      return { node, status: 'ok', physicalNetId: pinToPhysicalNetId.get(pinKey(node)) };
    });

    const unplaced = classified.filter((c) => c.status === 'unplaced');
    const unknownFootprint = classified.filter((c) => c.status === 'unknown-footprint');
    const ok = classified.filter((c) => c.status === 'ok');

    if (unplaced.length > 0) {
      const pins = unplaced.map((c) => toPinRef(c.node)).sort(comparePinRef);
      const refs = [...new Set(pins.map((p) => p.componentRef))].sort(compareStrings);
      issues.push({
        kind: 'unplaced-component',
        message:
          `Net '${net.name}' references pin(s) ${pins.map(formatPinRef).join(', ')}, but ` +
          `component(s) ${refs.join(', ')} are not placed on the board.`,
        netNames: [net.name],
        pins,
        physicalNetIds: [],
      });
    }

    if (unknownFootprint.length > 0) {
      const pins = unknownFootprint.map((c) => toPinRef(c.node)).sort(comparePinRef);
      const refs = [...new Set(pins.map((p) => p.componentRef))].sort(compareStrings);
      issues.push({
        kind: 'unknown-footprint',
        message:
          `Net '${net.name}' references pin(s) ${pins.map(formatPinRef).join(', ')} on ` +
          `component(s) ${refs.join(', ')}, whose footprint could not be resolved.`,
        netNames: [net.name],
        pins,
        physicalNetIds: [],
      });
    }

    let sound = unplaced.length === 0 && unknownFootprint.length === 0;
    let solePhysicalNetId: string | undefined;

    if (ok.length >= 2) {
      const groups = new Map<
        string,
        { readonly physicalNetId: string | undefined; readonly members: ClassifiedNode[] }
      >();
      let unresolvedCounter = 0;
      for (const c of ok) {
        const key = c.physicalNetId ?? `__unresolved-${unresolvedCounter++}`;
        const existing = groups.get(key);
        if (existing) existing.members.push(c);
        else groups.set(key, { physicalNetId: c.physicalNetId, members: [c] });
      }

      if (groups.size > 1) {
        sound = false;

        const groupList = [...groups.values()]
          .map((g) => ({
            physicalNetId: g.physicalNetId,
            pins: g.members.map((m) => toPinRef(m.node)).sort(comparePinRef),
          }))
          .sort((a, b) => {
            const pa = a.pins[0];
            const pb = b.pins[0];
            if (pa === undefined || pb === undefined) return 0; // unreachable: every group has >= 1 pin
            return comparePinRef(pa, pb);
          });

        const allPins = groupList.flatMap((g) => g.pins).sort(comparePinRef);
        const physicalNetIds = [
          ...new Set(groupList.map((g) => g.physicalNetId).filter((id): id is string => id !== undefined)),
        ].sort(compareStrings);

        // UNROUTED vs OPEN describes the STATE of the net, not its size: unrouted means
        // no two of its pins are connected to each other at all ("haven't started"),
        // open means some are and some aren't ("started and missed one"). Deliberately
        // independent of pin count — if it keyed off the number of pins, then straight
        // after a netlist import the two-pin nets would report 'open' while the
        // three-pin nets reported 'unrouted', which is two names for one situation.
        //
        // Note the consequence: a two-pin net can never be 'open', since two pins are
        // either joined or they are not. `summary.opens` therefore counts both kinds,
        // so nothing goes missing from the under-connection total.
        const kind: LvsIssueKind = groups.size === ok.length ? 'unrouted-net' : 'open';

        const groupText = groupList
          .map((g) => `{${g.pins.map(formatPinRef).join(', ')}} ${physicalNetLabel(g.physicalNetId, physicalNetById)}`)
          .join('; ');

        const message =
          kind === 'unrouted-net'
            ? `Net '${net.name}' is unrouted: all ${ok.length} of its pins are present on the ` +
              `board but none of them are connected to each other yet.`
            : `Net '${net.name}' is open: its pins are split across ${groups.size} separate ` +
              `physical connections instead of one — ${groupText}.`;

        issues.push({ kind, message, netNames: [net.name], pins: allPins, physicalNetIds });
      } else {
        const only = [...groups.values()][0];
        solePhysicalNetId = only?.physicalNetId;
      }
    } else if (ok.length === 1) {
      const single = ok[0];
      solePhysicalNetId = single?.physicalNetId;
      if (solePhysicalNetId === undefined) sound = false;
    }

    if (sound && solePhysicalNetId !== undefined && shortedPhysicalNetIds.has(solePhysicalNetId)) {
      sound = false;
    }

    if (sound && ok.length > 0) matchedNets++;
  }

  issues.sort(compareIssue);

  // Counts BOTH 'open' and 'unrouted-net': they are the two shapes of the same defect,
  // a net the board under-connects. Counting only 'open' would silently omit every
  // two-pin net, which can only ever be unrouted (see the kind selection above).
  const opens = issues.filter((i) => i.kind === 'open' || i.kind === 'unrouted-net').length;
  const shorts = issues.filter((i) => i.kind === 'short').length;

  return {
    ok: issues.length === 0,
    issues,
    summary: {
      schematicNets: doc.nets.length,
      physicalNets: physicalNets.length,
      matchedNets,
      opens,
      shorts,
    },
  };
}

// ---------------------------------------------------------------------------
// Soldering-guide helpers — the payoff of modelling schematic intent as data.
// ---------------------------------------------------------------------------

/**
 * Pairs that MUST read continuous, per schematic net: a spanning chain
 * (pin0-pin1, pin1-pin2, ...), not the full O(n^2) cross product. n-1 measurements
 * prove the same fact as n(n-1)/2 and a human will actually perform them.
 *
 * Purely derived from schematic intent (`doc.nets`) — it does not need a
 * FootprintLookup or the physical board, because it defines what a human should go
 * measure, independent of whether the board currently satisfies it yet.
 */
export function continuityChecks(doc: PerfDocument): ContinuityCheck[] {
  const checks: ContinuityCheck[] = [];

  for (const net of doc.nets) {
    if (net.nodes.length < 2) continue; // A single-pin net has nothing to prove continuous.

    const pins = [...net.nodes].map(toPinRef).sort(comparePinRef);
    for (let i = 0; i + 1 < pins.length; i++) {
      const a = pins[i];
      const b = pins[i + 1];
      if (a === undefined || b === undefined) continue; // unreachable given the loop bound
      checks.push({ a, b, netName: net.name });
    }
  }

  return checks;
}

/**
 * Default cap on the number of isolation pairs returned by {@link isolationChecks}.
 * The full cross product of distinct schematic net pairs is O(n^2) and unusable as a
 * manual checklist once a design has more than a handful of nets, so the list below
 * is a bounded, PRIORITISED SAMPLE — not an exhaustive isolation matrix. A caller must
 * not treat a list at this length as proof every pair was considered.
 */
const DEFAULT_ISOLATION_CHECK_CAP = 40;

/** Priority bucket for a net-class pair: lower sorts first. */
function pairPriority(classA: NetClass, classB: NetClass): number {
  const classes = new Set<NetClass>([classA, classB]);
  if (classes.has('power') && classes.has('ground')) return 0; // the one that matters before power-on
  if (classes.has('power') && classes.has('signal')) return 1;
  return 2; // everything else: ground/signal, power/power, signal/signal, ground/ground, ...
}

/**
 * Pairs that MUST read open (isolated): one representative pin per pair of distinct
 * schematic nets, power/ground pairs first, then power/signal, then the rest — see
 * {@link DEFAULT_ISOLATION_CHECK_CAP} for why the result is capped rather than exhaustive.
 */
export function isolationChecks(doc: PerfDocument): IsolationCheck[] {
  const representative = new Map<NetId, PhysicalPinRef>();
  for (const net of doc.nets) {
    if (net.nodes.length === 0) continue;
    const sorted = [...net.nodes].map(toPinRef).sort(comparePinRef);
    const first = sorted[0];
    if (first === undefined) continue; // unreachable given the length check above
    representative.set(net.id, first);
  }

  const nets = doc.nets.filter((n) => representative.has(n.id));

  const candidates: Array<{
    readonly priority: number;
    readonly netA: Net;
    readonly netB: Net;
    readonly pinA: PhysicalPinRef;
    readonly pinB: PhysicalPinRef;
  }> = [];

  for (let i = 0; i < nets.length; i++) {
    for (let j = i + 1; j < nets.length; j++) {
      const netA = nets[i];
      const netB = nets[j];
      if (netA === undefined || netB === undefined) continue; // unreachable given the loop bounds
      const pinA = representative.get(netA.id);
      const pinB = representative.get(netB.id);
      if (pinA === undefined || pinB === undefined) continue; // unreachable: nets was filtered above
      candidates.push({ priority: pairPriority(netA.class, netB.class), netA, netB, pinA, pinB });
    }
  }

  candidates.sort((x, y) => {
    if (x.priority !== y.priority) return x.priority - y.priority;
    const byA = compareStrings(x.netA.name, y.netA.name);
    if (byA !== 0) return byA;
    return compareStrings(x.netB.name, y.netB.name);
  });

  return candidates.slice(0, DEFAULT_ISOLATION_CHECK_CAP).map((c) => ({
    a: c.pinA,
    b: c.pinB,
    netA: c.netA.name,
    netB: c.netB.name,
  }));
}
