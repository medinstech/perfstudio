import { describe, expect, it } from 'vitest';

import {
  arePinsConnected,
  extractPhysicalNets,
  netOfPin,
  type FootprintLookup,
  type PhysicalNet,
  type PhysicalNodeRef,
} from './connectivity.js';
import type {
  BoardSide,
  Board,
  Conductor,
  ComponentInstance,
  Footprint,
  HoleCoord,
  PerfDocument,
  Rotation,
  SolderTraceConductor,
  StripConductor,
  WireConductor,
} from './model.js';

// ---------------------------------------------------------------------------
// Fixture builders — minimal, only the fields connectivity.ts actually reads.
// ---------------------------------------------------------------------------

function hole(col: number, row: number): HoleCoord {
  return { col, row };
}

const BOARD: Board = {
  type: 'pad-per-hole',
  cols: 40,
  rows: 40,
  pitch: 2.54,
  thickness: 1.6,
  material: 'FR4',
  padDiameter: 1.9,
  drillDiameter: 0.8,
};

/** A footprint with a single pin at the anchor (dCol=0, dRow=0) unless overridden. */
function onePinFootprint(id: string, dCol = 0, dRow = 0): Footprint {
  return {
    id,
    name: id,
    pins: [{ number: '1', dCol, dRow }],
    bodyOutline: [],
    bodyHeight: 0,
    body: { archetype: 'generic-box', dims: {} },
    leadDiameter: 0.5,
    polarized: false,
  };
}

function twoPinFootprint(
  id: string,
  pin1: { dCol: number; dRow: number },
  pin2: { dCol: number; dRow: number },
): Footprint {
  return {
    id,
    name: id,
    pins: [
      { number: '1', dCol: pin1.dCol, dRow: pin1.dRow },
      { number: '2', dCol: pin2.dCol, dRow: pin2.dRow },
    ],
    bodyOutline: [],
    bodyHeight: 0,
    body: { archetype: 'generic-box', dims: {} },
    leadDiameter: 0.5,
    polarized: false,
  };
}

function makeComponent(
  id: string,
  ref: string,
  footprintId: string,
  anchor: HoleCoord,
  opts?: { rotation?: Rotation; mirrored?: boolean },
): ComponentInstance {
  return {
    id,
    ref,
    value: '',
    footprintId,
    anchor,
    rotation: opts?.rotation ?? 0,
    mirrored: opts?.mirrored ?? false,
    locked: false,
  };
}

// SolderTraceConductor.side is fixed to 'bottom' by the model, so this helper takes
// no side parameter.
function solderTrace(id: string, path: readonly HoleCoord[]): SolderTraceConductor {
  return { id, kind: 'solder-trace', path, side: 'bottom', layerZ: 0, buildup: 'normal' };
}

function bareWire(id: string, path: readonly HoleCoord[], side: BoardSide = 'bottom'): WireConductor {
  return { id, kind: 'bare-wire', path, side, layerZ: 0 };
}

function topJumper(id: string, path: readonly HoleCoord[]): WireConductor {
  return { id, kind: 'top-jumper', path, side: 'top', layerZ: 0 };
}

function strip(id: string, path: readonly HoleCoord[], side: BoardSide = 'bottom'): StripConductor {
  return { id, kind: 'strip', path, side, layerZ: 0 };
}

function makeDoc(
  components: readonly ComponentInstance[],
  conductors: readonly Conductor[],
): PerfDocument {
  return {
    formatVersion: 1,
    meta: { name: 'test', created: '2024-01-01T00:00:00.000Z', modified: '2024-01-01T00:00:00.000Z' },
    board: BOARD,
    components,
    conductors,
    cuts: [],
    nets: [],
  };
}

function makeLookup(footprints: readonly Footprint[]): FootprintLookup {
  const map = new Map(footprints.map((f) => [f.id, f]));
  return (footprintId: string) => map.get(footprintId);
}

function findNetWithNode(nets: readonly PhysicalNet[], node: PhysicalNodeRef): PhysicalNet | undefined {
  return nets.find((net) =>
    net.nodes.some((n) => n.hole.col === node.hole.col && n.hole.row === node.hole.row && n.side === node.side),
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('extractPhysicalNets', () => {
  it('joins two adjacent pads via a 2-hole solder-trace, leaving an unrelated pad separate', () => {
    const A = hole(0, 0);
    const B = hole(1, 0);
    const C = hole(5, 5);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'R1', 'fp1', A),
        makeComponent('c2', 'R2', 'fp1', B),
        makeComponent('c3', 'R3', 'fp1', C),
      ],
      [solderTrace('t1', [A, B])],
    );
    const lookup = makeLookup([fp]);

    const netA = netOfPin(doc, lookup, { componentRef: 'R1', pin: '1' });
    const netB = netOfPin(doc, lookup, { componentRef: 'R2', pin: '1' });
    const netC = netOfPin(doc, lookup, { componentRef: 'R3', pin: '1' });

    expect(netA).toBeDefined();
    expect(netB).toBeDefined();
    expect(netC).toBeDefined();
    expect(netA?.id).toBe(netB?.id);
    expect(netA?.id).not.toBe(netC?.id);

    expect(arePinsConnected(doc, lookup, { componentRef: 'R1', pin: '1' }, { componentRef: 'R2', pin: '1' })).toBe(
      true,
    );
    expect(arePinsConnected(doc, lookup, { componentRef: 'R1', pin: '1' }, { componentRef: 'R3', pin: '1' })).toBe(
      false,
    );
  });

  it('connects all 5 holes of a 5-hole solder-trace', () => {
    const path = [hole(0, 0), hole(1, 0), hole(2, 0), hole(3, 0), hole(4, 0)];
    const doc = makeDoc([], [solderTrace('t1', path)]);
    const lookup = makeLookup([]);

    const nets = extractPhysicalNets(doc, lookup);
    expect(nets).toHaveLength(1);
    expect(nets[0]?.nodes).toEqual(path.map((h) => ({ hole: h, side: 'bottom' as const })));
  });

  it('CRITICAL: a bare-wire only connects its endpoints; the same path as a solder-trace connects everything', () => {
    const A = hole(0, 0);
    const B = hole(1, 0);
    const C = hole(2, 0);

    // bare-wire: A and C connected. B is merely passed over, so it makes no contact
    // and is not a node at all — it is electrically indistinguishable from any of the
    // board's empty pads. What must never happen is B sharing a net with A or C.
    const wireDoc = makeDoc([], [bareWire('w1', [A, B, C])]);
    const lookup = makeLookup([]);
    const wireNets = extractPhysicalNets(wireDoc, lookup);

    const netA = findNetWithNode(wireNets, { hole: A, side: 'bottom' });
    const netB = findNetWithNode(wireNets, { hole: B, side: 'bottom' });
    const netC = findNetWithNode(wireNets, { hole: C, side: 'bottom' });

    expect(netA).toBeDefined();
    expect(netC).toBeDefined();
    expect(netA?.id).toBe(netC?.id);
    expect(netB).toBeUndefined();
    expect(wireNets).toHaveLength(1);
    expect(netA?.nodes).toEqual([
      { hole: A, side: 'bottom' },
      { hole: C, side: 'bottom' },
    ]);

    // same path, but as a solder-trace: every hole is a contact point.
    const traceDoc = makeDoc([], [solderTrace('t1', [A, B, C])]);
    const traceNets = extractPhysicalNets(traceDoc, lookup);
    expect(traceNets).toHaveLength(1);
    expect(traceNets[0]?.nodes).toEqual([
      { hole: A, side: 'bottom' },
      { hole: B, side: 'bottom' },
      { hole: C, side: 'bottom' },
    ]);
  });

  it('bridges top and bottom at a component pin hole', () => {
    const A = hole(3, 3);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc([makeComponent('c1', 'U1', 'fp1', A)], []);
    const lookup = makeLookup([fp]);

    const net = netOfPin(doc, lookup, { componentRef: 'U1', pin: '1' });
    expect(net).toBeDefined();
    expect(net?.nodes).toEqual([
      { hole: A, side: 'bottom' },
      { hole: A, side: 'top' },
    ]);
    expect(net?.pins).toEqual([{ componentRef: 'U1', pin: '1' }]);
  });

  it('shows both components in a net`s pins list when joined by a solder trace', () => {
    const A = hole(0, 0);
    const B = hole(1, 0);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [makeComponent('c1', 'U1', 'fp1', A), makeComponent('c2', 'U2', 'fp1', B)],
      [solderTrace('t1', [A, B])],
    );
    const lookup = makeLookup([fp]);

    const net = netOfPin(doc, lookup, { componentRef: 'U1', pin: '1' });
    expect(net?.pins).toEqual([
      { componentRef: 'U1', pin: '1' },
      { componentRef: 'U2', pin: '1' },
    ]);
    expect(net?.conductorIds).toEqual(['t1']);
  });

  it('keeps a top-jumper from connecting the bottom side except through a pin', () => {
    const A = hole(0, 0);
    const B = hole(1, 0);
    const C = hole(2, 0);
    // top-jumper A-B on top; unrelated solder-trace A-C on bottom. Nothing bridges
    // top and bottom at A because there is no component pin there.
    const doc = makeDoc([], [topJumper('j1', [A, B]), solderTrace('t1', [A, C])]);
    const lookup = makeLookup([]);

    const nets = extractPhysicalNets(doc, lookup);
    const netTopA = findNetWithNode(nets, { hole: A, side: 'top' });
    const netBottomA = findNetWithNode(nets, { hole: A, side: 'bottom' });

    expect(netTopA).toBeDefined();
    expect(netBottomA).toBeDefined();
    expect(netTopA?.id).not.toBe(netBottomA?.id);
    expect(netTopA?.nodes).toContainEqual({ hole: B, side: 'top' });
    expect(netBottomA?.nodes).toContainEqual({ hole: C, side: 'bottom' });
  });

  it('is deterministic: repeated extraction and input reordering yield the same output', () => {
    const A = hole(0, 0);
    const B = hole(1, 0);
    const C = hole(2, 0);
    const D = hole(3, 3);
    const fp = onePinFootprint('fp1');
    const components = [
      makeComponent('c1', 'U1', 'fp1', A),
      makeComponent('c2', 'U2', 'fp1', B),
      makeComponent('c3', 'U3', 'fp1', D),
    ];
    const conductors: Conductor[] = [
      solderTrace('t1', [A, B]),
      topJumper('j1', [B, C]),
      strip('s1', [D]),
    ];
    const doc = makeDoc(components, conductors);
    const lookup = makeLookup([fp]);

    const first = extractPhysicalNets(doc, lookup);
    const second = extractPhysicalNets(doc, lookup);
    expect(second).toEqual(first);
    expect(second.map((n) => n.id)).toEqual(first.map((n) => n.id));

    // Reordering components/conductors must not change the result.
    const reorderedDoc = makeDoc([...components].reverse(), [...conductors].reverse());
    const reordered = extractPhysicalNets(reorderedDoc, lookup);
    expect(reordered).toEqual(first);
  });

  it('skips components with an unknown footprint instead of throwing', () => {
    const fp = onePinFootprint('fp1');
    const known = makeComponent('c1', 'R1', 'fp1', hole(0, 0));
    const unknown = makeComponent('c2', 'X1', 'does-not-exist', hole(9, 9));
    const doc = makeDoc([known, unknown], []);
    const lookup = makeLookup([fp]);

    expect(() => extractPhysicalNets(doc, lookup)).not.toThrow();

    const nets = extractPhysicalNets(doc, lookup);
    expect(nets.some((n) => n.pins.some((p) => p.componentRef === 'X1'))).toBe(false);
    expect(netOfPin(doc, lookup, { componentRef: 'R1', pin: '1' })).toBeDefined();
  });

  it('computes pin holes under mirror-then-rotation correctly', () => {
    // pin1 at the anchor, pin2 offset by (dCol=2, dRow=0).
    const fp = twoPinFootprint('fp2', { dCol: 0, dRow: 0 }, { dCol: 2, dRow: 0 });
    const anchor = hole(5, 5);
    // mirror first: (2,0) -> (-2,0); rotate 90 CW: (x,y)->(-y,x): (-2,0) -> (0,-2).
    const expectedPin2Hole = hole(5, 3);
    const doc = makeDoc([makeComponent('c1', 'Q1', 'fp2', anchor, { rotation: 90, mirrored: true })], []);
    const lookup = makeLookup([fp]);

    const net1 = netOfPin(doc, lookup, { componentRef: 'Q1', pin: '1' });
    const net2 = netOfPin(doc, lookup, { componentRef: 'Q1', pin: '2' });

    expect(net1?.nodes.map((n) => n.hole)).toEqual([anchor, anchor]);
    expect(net2?.nodes.map((n) => n.hole)).toEqual([expectedPin2Hole, expectedPin2Hole]);
  });
});
