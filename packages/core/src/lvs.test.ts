import { describe, expect, it } from 'vitest';

import type { FootprintLookup } from './connectivity.js';
import { continuityChecks, isolationChecks, runLvs } from './lvs.js';
import type {
  Board,
  ComponentInstance,
  Conductor,
  Footprint,
  HoleCoord,
  Net,
  NetClass,
  NetNode,
  PerfDocument,
  Rotation,
  SolderTraceConductor,
} from './model.js';

// ---------------------------------------------------------------------------
// Fixture builders — minimal, only the fields LVS (via connectivity.ts) actually reads.
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

/** A footprint with a single pin at the anchor (dCol=0, dRow=0). */
function onePinFootprint(id: string): Footprint {
  return {
    id,
    name: id,
    pins: [{ number: '1', dCol: 0, dRow: 0 }],
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

function netNode(componentRef: string, pin: string): NetNode {
  return { componentRef, pin };
}

function net(id: string, name: string, cls: NetClass, nodes: readonly NetNode[]): Net {
  return { id, name, nodes, class: cls };
}

function makeDoc(
  components: readonly ComponentInstance[],
  conductors: readonly Conductor[],
  nets: readonly Net[] = [],
): PerfDocument {
  return {
    formatVersion: 1,
    meta: { name: 'test', created: '2024-01-01T00:00:00.000Z', modified: '2024-01-01T00:00:00.000Z' },
    board: BOARD,
    components,
    conductors,
    cuts: [],
    nets,
  };
}

function makeLookup(footprints: readonly Footprint[]): FootprintLookup {
  const map = new Map(footprints.map((f) => [f.id, f]));
  return (footprintId: string) => map.get(footprintId);
}

// ---------------------------------------------------------------------------
// runLvs
// ---------------------------------------------------------------------------

describe('runLvs', () => {
  it('reports ok for a correctly wired 2-component net', () => {
    const a = hole(0, 0);
    const b = hole(1, 0);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [makeComponent('c1', 'R1', 'fp1', a), makeComponent('c2', 'R2', 'fp1', b)],
      [solderTrace('t1', [a, b])],
      [net('n1', 'NET1', 'signal', [netNode('R1', '1'), netNode('R2', '1')])],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(true);
    expect(result.issues).toEqual([]);
    expect(result.summary).toEqual({
      schematicNets: 1,
      physicalNets: 1,
      matchedNets: 1,
      opens: 0,
      shorts: 0,
    });
  });

  it("reports 'unrouted-net' when none of a net's pins are connected to each other", () => {
    const a = hole(0, 0);
    const b = hole(5, 5);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [makeComponent('c1', 'R1', 'fp1', a), makeComponent('c2', 'R2', 'fp1', b)],
      [], // no conductor at all: R1.1 and R2.1 are never connected
      [net('n1', 'NET1', 'signal', [netNode('R1', '1'), netNode('R2', '1')])],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    expect(result.issues).toHaveLength(1);
    const issue = result.issues[0];
    expect(issue?.kind).toBe('unrouted-net');
    expect(issue?.netNames).toEqual(['NET1']);
    expect(issue?.physicalNetIds).toHaveLength(2);
    expect(issue?.pins.map((p) => `${p.componentRef}.${p.pin}`).sort()).toEqual(['R1.1', 'R2.1']);
    // summary.opens covers under-connection of BOTH shapes, so an unrouted net still counts.
    expect(result.summary.opens).toBe(1);
    expect(result.summary.matchedNets).toBe(0);
  });

  it("reports 'open' when a net is PARTLY wired — some pins joined, one left out", () => {
    const a = hole(0, 0);
    const b = hole(1, 0); // joined to a by a solder trace
    const c = hole(8, 8); // left stranded
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'R1', 'fp1', a),
        makeComponent('c2', 'R2', 'fp1', b),
        makeComponent('c3', 'R3', 'fp1', c),
      ],
      [solderTrace('t1', [a, b])],
      [net('n1', 'NET1', 'signal', [netNode('R1', '1'), netNode('R2', '1'), netNode('R3', '1')])],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    const opens = result.issues.filter((i) => i.kind === 'open');
    expect(opens).toHaveLength(1);
    expect(opens[0]?.netNames).toEqual(['NET1']);
    // Two groups: {R1,R2} joined, {R3} alone. This is what distinguishes open from unrouted.
    expect(opens[0]?.physicalNetIds).toHaveLength(2);
    expect(result.summary.opens).toBe(1);
    expect(result.summary.matchedNets).toBe(0);
  });

  it('a freshly imported netlist reports one consistent kind regardless of net size', () => {
    // The bug this guards: keying the kind off pin count made a 2-pin net say 'open'
    // and a 3-pin net say 'unrouted' for the identical "nothing wired yet" situation.
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'R1', 'fp1', hole(0, 0)),
        makeComponent('c2', 'R2', 'fp1', hole(2, 0)),
        makeComponent('c3', 'R3', 'fp1', hole(4, 0)),
        makeComponent('c4', 'R4', 'fp1', hole(6, 0)),
        makeComponent('c5', 'R5', 'fp1', hole(8, 0)),
      ],
      [],
      [
        net('n1', 'TWO_PIN', 'signal', [netNode('R1', '1'), netNode('R2', '1')]),
        net('n2', 'THREE_PIN', 'signal', [
          netNode('R3', '1'),
          netNode('R4', '1'),
          netNode('R5', '1'),
        ]),
      ],
    );

    const result = runLvs(doc, makeLookup([fp]));
    const kinds = new Set(result.issues.map((i) => i.kind));
    expect(kinds).toEqual(new Set(['unrouted-net']));
    expect(result.summary.opens).toBe(2);
  });

  it('reports a single short issue naming both nets for an accidental GND/V+ solder bridge', () => {
    const g1 = hole(0, 0);
    const g2 = hole(1, 0);
    const p1 = hole(2, 0); // adjacent to g2 — this is where the accidental bridge lands
    const p2 = hole(3, 0);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'G1', 'fp1', g1),
        makeComponent('c2', 'G2', 'fp1', g2),
        makeComponent('c3', 'P1', 'fp1', p1),
        makeComponent('c4', 'P2', 'fp1', p2),
      ],
      [
        solderTrace('t1', [g1, g2]),
        solderTrace('t2', [p1, p2]),
        solderTrace('bridge', [g2, p1]), // accidental bridge between the GND and V+ rails
      ],
      [
        net('gnd', 'GND', 'ground', [netNode('G1', '1'), netNode('G2', '1')]),
        net('vplus', 'V+', 'power', [netNode('P1', '1'), netNode('P2', '1')]),
      ],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    const shorts = result.issues.filter((i) => i.kind === 'short');
    expect(shorts).toHaveLength(1);
    expect(shorts[0]?.netNames).toEqual(['GND', 'V+']);
    expect(shorts[0]?.message).toContain('GND');
    expect(shorts[0]?.message).toContain('V+');
    expect(result.issues.filter((i) => i.kind === 'open')).toHaveLength(0);
    expect(result.summary.shorts).toBe(1);
  });

  it('reports both an open and a short when a net is internally split AND bridged to another net', () => {
    const u1 = hole(0, 0);
    const u2 = hole(1, 0);
    const u4 = hole(2, 0); // AUX component, adjacent to u2 — the accidental bridge
    const u3 = hole(10, 10); // CLK's third pin, left isolated — the "open" half
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'U1', 'fp1', u1),
        makeComponent('c2', 'U2', 'fp1', u2),
        makeComponent('c3', 'U3', 'fp1', u3),
        makeComponent('c4', 'U4', 'fp1', u4),
      ],
      [solderTrace('t1', [u1, u2, u4])], // connects CLK's U1-U2 but runs on into AUX's U4
      [
        net('clk', 'CLK', 'signal', [netNode('U1', '1'), netNode('U2', '1'), netNode('U3', '1')]),
        net('aux', 'AUX', 'signal', [netNode('U4', '1')]),
      ],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    const open = result.issues.find((i) => i.kind === 'open');
    const short = result.issues.find((i) => i.kind === 'short');
    expect(open).toBeDefined();
    expect(open?.netNames).toEqual(['CLK']);
    expect(short).toBeDefined();
    expect(short?.netNames).toEqual(['AUX', 'CLK']);
    expect(result.summary.matchedNets).toBe(0);
  });

  it('reports a floating conductor that connects to no component pin', () => {
    const a = hole(20, 20);
    const b = hole(21, 20);
    const doc = makeDoc([], [solderTrace('stray', [a, b])], []);
    const lookup = makeLookup([]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    const issue = result.issues.find((i) => i.kind === 'floating-conductor');
    expect(issue).toBeDefined();
    expect(issue?.pins).toEqual([]);
    expect(issue?.conductorIds).toEqual(['stray']);
  });

  it('reports an unplaced component referenced by a schematic net', () => {
    const doc = makeDoc([], [], [net('missing', 'MISSING', 'signal', [netNode('U99', '1')])]);
    const lookup = makeLookup([]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(false);
    const issue = result.issues.find((i) => i.kind === 'unplaced-component');
    expect(issue).toBeDefined();
    expect(issue?.netNames).toEqual(['MISSING']);
    expect(issue?.pins).toEqual([{ componentRef: 'U99', pin: '1' }]);
  });

  it('reports unknown-footprint for a placed component whose footprintId cannot be resolved', () => {
    const a = hole(0, 0);
    const doc = makeDoc(
      [makeComponent('c1', 'U1', 'does-not-exist', a)],
      [],
      [net('n1', 'NET1', 'signal', [netNode('U1', '1')])],
    );
    const lookup = makeLookup([]); // empty: nothing resolves

    const result = runLvs(doc, lookup);

    const issue = result.issues.find((i) => i.kind === 'unknown-footprint');
    expect(issue).toBeDefined();
    expect(issue?.netNames).toEqual(['NET1']);
    expect(issue?.pins).toEqual([{ componentRef: 'U1', pin: '1' }]);
  });

  it('reports unrouted-net when a 3+ pin net has no connections between any of its pins', () => {
    const a = hole(0, 0);
    const b = hole(5, 5);
    const c = hole(9, 9);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [
        makeComponent('c1', 'R1', 'fp1', a),
        makeComponent('c2', 'R2', 'fp1', b),
        makeComponent('c3', 'R3', 'fp1', c),
      ],
      [],
      [net('n1', 'BUS3', 'signal', [netNode('R1', '1'), netNode('R2', '1'), netNode('R3', '1')])],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    const issue = result.issues.find((i) => i.kind === 'unrouted-net');
    expect(issue).toBeDefined();
    expect(issue?.netNames).toEqual(['BUS3']);
    expect(result.issues.some((i) => i.kind === 'open')).toBe(false);
  });

  it('produces no issues for a single-pin schematic net', () => {
    const a = hole(0, 0);
    const fp = onePinFootprint('fp1');
    const doc = makeDoc(
      [makeComponent('c1', 'TP1', 'fp1', a)],
      [],
      [net('tp', 'TESTPOINT', 'signal', [netNode('TP1', '1')])],
    );
    const lookup = makeLookup([fp]);

    const result = runLvs(doc, lookup);

    expect(result.ok).toBe(true);
    expect(result.issues).toEqual([]);
  });

  it('is deterministic across repeated runs and input reordering', () => {
    const a = hole(0, 0);
    const b = hole(1, 0);
    const c = hole(5, 5);
    const fp = onePinFootprint('fp1');
    const components = [
      makeComponent('c1', 'R1', 'fp1', a),
      makeComponent('c2', 'R2', 'fp1', b),
      makeComponent('c3', 'R3', 'fp1', c),
    ];
    const conductors: Conductor[] = [solderTrace('t1', [a, b])];
    const nets = [
      net('n1', 'NET1', 'signal', [netNode('R1', '1'), netNode('R2', '1')]),
      net('n2', 'NET2', 'signal', [netNode('R3', '1')]),
    ];
    const doc = makeDoc(components, conductors, nets);
    const lookup = makeLookup([fp]);

    const first = runLvs(doc, lookup);
    const second = runLvs(doc, lookup);
    expect(second).toEqual(first);

    const reorderedDoc = makeDoc([...components].reverse(), [...conductors], [...nets].reverse());
    const reordered = runLvs(reorderedDoc, lookup);
    expect(reordered).toEqual(first);
  });
});

// ---------------------------------------------------------------------------
// continuityChecks
// ---------------------------------------------------------------------------

describe('continuityChecks', () => {
  it('returns a spanning chain of n-1 checks for a 4-pin net', () => {
    const doc = makeDoc(
      [],
      [],
      [
        net('n1', 'BUS', 'signal', [
          netNode('U1', '1'),
          netNode('U2', '2'),
          netNode('U3', '3'),
          netNode('U4', '4'),
        ]),
      ],
    );

    const checks = continuityChecks(doc);

    expect(checks).toHaveLength(3);
    for (const c of checks) expect(c.netName).toBe('BUS');
    // Spanning chain: each check's `a` matches the previous check's `b`.
    for (let i = 1; i < checks.length; i++) {
      expect(checks[i]?.a).toEqual(checks[i - 1]?.b);
    }
  });

  it('produces no checks for single-pin or empty nets', () => {
    const doc = makeDoc(
      [],
      [],
      [net('n1', 'SINGLE', 'signal', [netNode('U1', '1')]), net('n2', 'EMPTY', 'signal', [])],
    );
    expect(continuityChecks(doc)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// isolationChecks
// ---------------------------------------------------------------------------

describe('isolationChecks', () => {
  it('prioritises a power/ground pair over other pairs', () => {
    const doc = makeDoc(
      [],
      [],
      [
        net('sig', 'SIG', 'signal', [netNode('U1', '1')]),
        net('gnd', 'GND', 'ground', [netNode('U2', '1')]),
        net('vcc', 'V+', 'power', [netNode('U3', '1')]),
      ],
    );

    const checks = isolationChecks(doc);

    expect(checks.length).toBeGreaterThan(0);
    const first = checks[0];
    expect(first).toBeDefined();
    expect([first?.netA, first?.netB].sort()).toEqual(['GND', 'V+']);
  });
});
