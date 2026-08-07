import { describe, expect, it } from 'vitest';

import { DEFAULT_DRC_OPTIONS, runDrc, type DrcViolation } from './drc.js';
import type { FootprintLookup } from './connectivity.js';
import type {
  Board,
  BoardMaterial,
  ComponentInstance,
  Conductor,
  Footprint,
  HoleCoord,
  LeadBendConductor,
  Net,
  PerfDocument,
  Point2,
  Rotation,
  SolderTraceConductor,
  WireConductor,
} from './model.js';

// ---------------------------------------------------------------------------
// Fixture builders — minimal, mirroring connectivity.test.ts's style.
// ---------------------------------------------------------------------------

function hole(col: number, row: number): HoleCoord {
  return { col, row };
}

function board(overrides?: Partial<Board>): Board {
  return {
    type: 'pad-per-hole',
    cols: 40,
    rows: 40,
    pitch: 2.54,
    thickness: 1.6,
    material: 'FR4',
    padDiameter: 1.9,
    drillDiameter: 0.8,
    ...overrides,
  };
}

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

/** A footprint with a rectangular body outline (mm, relative to anchor) and one pin. */
function boxFootprint(id: string, halfWidth: number, halfHeight: number): Footprint {
  const outline: Point2[] = [
    { x: -halfWidth, y: -halfHeight },
    { x: halfWidth, y: -halfHeight },
    { x: halfWidth, y: halfHeight },
    { x: -halfWidth, y: halfHeight },
  ];
  return {
    id,
    name: id,
    pins: [{ number: '1', dCol: 0, dRow: 0 }],
    bodyOutline: outline,
    bodyHeight: 3,
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

function solderTrace(
  id: string,
  path: readonly HoleCoord[],
  opts?: { buildup?: 'light' | 'normal' | 'heavy'; netId?: string; spine?: SolderTraceConductor['spine'] },
): SolderTraceConductor {
  const base = {
    id,
    kind: 'solder-trace' as const,
    path,
    side: 'bottom' as const,
    layerZ: 0,
    buildup: opts?.buildup ?? 'normal',
  };
  return {
    ...base,
    ...(opts?.netId !== undefined ? { netId: opts.netId } : {}),
    ...(opts?.spine !== undefined ? { spine: opts.spine } : {}),
  };
}

function solderTraceWired(
  id: string,
  path: readonly HoleCoord[],
  spine: NonNullable<SolderTraceConductor['spine']>,
  opts?: { buildup?: 'light' | 'normal' | 'heavy'; netId?: string },
): SolderTraceConductor {
  return {
    id,
    kind: 'solder-trace-wired',
    path,
    side: 'bottom',
    layerZ: 0,
    buildup: opts?.buildup ?? 'normal',
    spine,
    ...(opts?.netId !== undefined ? { netId: opts.netId } : {}),
  };
}

function bareWire(id: string, path: readonly HoleCoord[]): WireConductor {
  return { id, kind: 'bare-wire', path, side: 'bottom', layerZ: 0 };
}

function leadBend(
  id: string,
  path: readonly HoleCoord[],
  componentId: string,
  pinNumber: string,
): LeadBendConductor {
  return { id, kind: 'lead-bend', path, side: 'bottom', layerZ: 0, componentId, pinNumber };
}

function makeDoc(opts: {
  board?: Board;
  components?: readonly ComponentInstance[];
  conductors?: readonly Conductor[];
  nets?: readonly Net[];
}): PerfDocument {
  return {
    formatVersion: 1,
    meta: { name: 'test', created: '2024-01-01T00:00:00.000Z', modified: '2024-01-01T00:00:00.000Z' },
    board: opts.board ?? board(),
    components: opts.components ?? [],
    conductors: opts.conductors ?? [],
    cuts: [],
    nets: opts.nets ?? [],
  };
}

function makeLookup(footprints: readonly Footprint[]): FootprintLookup {
  const map = new Map(footprints.map((f) => [f.id, f]));
  return (footprintId: string) => map.get(footprintId);
}

function byRule(violations: readonly DrcViolation[], rule: string): DrcViolation[] {
  return violations.filter((v) => v.rule === rule);
}

// ---------------------------------------------------------------------------
// Rule 1: component-body-overlap
// ---------------------------------------------------------------------------

describe('component-body-overlap', () => {
  it('flags two components whose bodies overlap', () => {
    const fp = boxFootprint('box', 3, 3); // 6x6mm body
    const doc = makeDoc({
      components: [
        makeComponent('c1', 'R1', 'box', hole(0, 0)),
        makeComponent('c2', 'R2', 'box', hole(1, 0)), // 2.54mm away, bodies overlap
      ],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'component-body-overlap');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('error');
    expect(violations[0]?.componentIds).toEqual(['c1', 'c2']);
  });

  it('does not flag two components placed far enough apart', () => {
    const fp = boxFootprint('box', 1, 1); // 2x2mm body
    const doc = makeDoc({
      components: [
        makeComponent('c1', 'R1', 'box', hole(0, 0)),
        makeComponent('c2', 'R2', 'box', hole(10, 10)),
      ],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'component-body-overlap');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 2: component-off-board
// ---------------------------------------------------------------------------

describe('component-off-board', () => {
  it('flags a component with a pin outside the board grid', () => {
    const fp = onePinFootprint('fp1', -1, 0); // pin one hole to the left of anchor
    const doc = makeDoc({
      components: [makeComponent('c1', 'R1', 'fp1', hole(0, 0))], // pin lands at col -1
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'component-off-board');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('error');
  });

  it('does not flag a component fully within the board', () => {
    const fp = onePinFootprint('fp1');
    const doc = makeDoc({
      components: [makeComponent('c1', 'R1', 'fp1', hole(5, 5))],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'component-off-board');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 3: duplicate-pin-hole
// ---------------------------------------------------------------------------

describe('duplicate-pin-hole', () => {
  it('flags two components with a pin landing on the same hole', () => {
    const fp = onePinFootprint('fp1');
    const doc = makeDoc({
      components: [
        makeComponent('c1', 'R1', 'fp1', hole(3, 3)),
        makeComponent('c2', 'R2', 'fp1', hole(3, 3)),
      ],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'duplicate-pin-hole');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.componentIds).toEqual(['c1', 'c2']);
  });

  it('does not flag components with pins on distinct holes', () => {
    const fp = onePinFootprint('fp1');
    const doc = makeDoc({
      components: [
        makeComponent('c1', 'R1', 'fp1', hole(3, 3)),
        makeComponent('c2', 'R2', 'fp1', hole(4, 3)),
      ],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'duplicate-pin-hole');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 4: crossing-conductors
// ---------------------------------------------------------------------------

describe('crossing-conductors', () => {
  it('flags two bare wires that cross at a hole that is not either one`s endpoint', () => {
    // w1 runs A-B-C horizontally; w2 runs D-B-E vertically. Both pass over B without
    // terminating there, so B is not a registered contact for either — an accidental
    // crossing, and thus a short.
    const A = hole(0, 2);
    const B = hole(2, 2);
    const C = hole(4, 2);
    const D = hole(2, 0);
    const E = hole(2, 4);
    const doc = makeDoc({
      conductors: [bareWire('w1', [A, B, C]), bareWire('w2', [D, B, E])],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'crossing-conductors');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('error');
    expect(violations[0]?.holes).toEqual([B]);
    expect(violations[0]?.conductorIds).toEqual(['w1', 'w2']);
  });

  it('does not flag two bare wires that share a genuine endpoint junction', () => {
    const A = hole(0, 2);
    const B = hole(2, 2);
    const C = hole(4, 2);
    const doc = makeDoc({
      conductors: [bareWire('w1', [A, B]), bareWire('w2', [B, C])],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'crossing-conductors');
    expect(violations).toHaveLength(0);
  });

  it('does not flag two solder traces that share a pad (automatically the same physical net)', () => {
    const A = hole(0, 2);
    const B = hole(2, 2);
    const C = hole(4, 2);
    const D = hole(2, 0);
    const doc = makeDoc({
      conductors: [solderTrace('t1', [A, hole(1, 2), B]), solderTrace('t2', [D, hole(2, 1), B])],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'crossing-conductors');
    expect(violations).toHaveLength(0);
    void C; // unused fixture value kept for readability of the layout above
  });
});

// ---------------------------------------------------------------------------
// Rule 5: solder-trace-invalid-path
// ---------------------------------------------------------------------------

describe('solder-trace-invalid-path', () => {
  it('flags a solder trace whose path takes a diagonal step', () => {
    const doc = makeDoc({
      conductors: [solderTrace('t1', [hole(0, 0), hole(1, 1)])], // diagonal: not 4-adjacent
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'solder-trace-invalid-path');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('error');
  });

  it('does not flag a valid orthogonal-chain solder trace', () => {
    const doc = makeDoc({
      conductors: [solderTrace('t1', [hole(0, 0), hole(1, 0), hole(2, 0)])],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'solder-trace-invalid-path');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 6: solder-trace-proximity — the headline rule, extra attention per spec.
// ---------------------------------------------------------------------------

describe('solder-trace-proximity', () => {
  it('flags exactly once when the trace runs past a DIFFERENT-net pad', () => {
    // Trace along row 2: (1,2)-(2,2)-(3,2). A lone component pin sits at (2,1), the
    // orthogonal neighbour of (2,2) above it, belonging to its own (different) net.
    const fp = onePinFootprint('fp1');
    const doc = makeDoc({
      components: [makeComponent('c1', 'U1', 'fp1', hole(2, 1))],
      conductors: [solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2)])],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'solder-trace-proximity');
    expect(violations).toHaveLength(1);
    const v = violations[0];
    expect(v?.severity).toBe('warning');
    expect(v?.holes).toEqual([hole(2, 2), hole(2, 1)]);
    // Message must name both holes (they become physical measurement steps in the guide).
    expect(v?.message).toContain('C3'); // (2,2) -> col C, row 3
    expect(v?.message).toContain('C2'); // (2,1) -> col C, row 2
  });

  it('flags nothing when the trace runs past a SAME-net pad', () => {
    // Same layout as above, but this time the pin at (2,1) is bridged onto the trace's
    // own net via a short bare wire, so it is legitimately the same physical net.
    const fp = onePinFootprint('fp1');
    const doc = makeDoc({
      components: [makeComponent('c1', 'U1', 'fp1', hole(2, 1))],
      conductors: [
        solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2)]),
        bareWire('w1', [hole(2, 1), hole(1, 2)]), // joins U1.1 onto the trace's net at (1,2)
      ],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'solder-trace-proximity');
    expect(violations).toHaveLength(0);
  });

  it('flags nothing when the neighbour hole is empty (no net at all)', () => {
    const doc = makeDoc({
      conductors: [solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2)])],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'solder-trace-proximity');
    expect(violations).toHaveLength(0);
  });

  it('produces no violations for a trace in open board with nothing near it', () => {
    const doc = makeDoc({
      conductors: [solderTrace('t1', [hole(20, 20), hole(21, 20), hole(22, 20)])],
    });
    const violations = runDrc(doc, makeLookup([]));
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 7: pad-lifting-risk
// ---------------------------------------------------------------------------

describe('pad-lifting-risk', () => {
  const longPath: HoleCoord[] = Array.from({ length: 8 }, (_, i) => hole(i, 0)); // 8 pads

  it('flags a long pure solder trace on FR2 board', () => {
    const doc = makeDoc({ board: board({ material: 'FR2' }), conductors: [solderTrace('t1', longPath)] });
    const violations = byRule(runDrc(doc, makeLookup([])), 'pad-lifting-risk');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
  });

  it('does not flag a long pure solder trace on FR4 board', () => {
    const doc = makeDoc({ board: board({ material: 'FR4' }), conductors: [solderTrace('t1', longPath)] });
    const violations = byRule(runDrc(doc, makeLookup([])), 'pad-lifting-risk');
    expect(violations).toHaveLength(0);
  });

  it('does not flag a short pure solder trace on FR2 board', () => {
    const shortPath = longPath.slice(0, 3);
    const doc = makeDoc({ board: board({ material: 'FR2' }), conductors: [solderTrace('t1', shortPath)] });
    const violations = byRule(runDrc(doc, makeLookup([])), 'pad-lifting-risk');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 8: solder-trace-too-long
// ---------------------------------------------------------------------------

describe('solder-trace-too-long', () => {
  it('flags a long pure solder trace regardless of material', () => {
    const longPath: HoleCoord[] = Array.from({ length: 8 }, (_, i) => hole(i, 0));
    const doc = makeDoc({ board: board({ material: 'FR4' }), conductors: [solderTrace('t1', longPath)] });
    const violations = byRule(runDrc(doc, makeLookup([])), 'solder-trace-too-long');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
  });

  it('does not flag a short pure solder trace', () => {
    const shortPath: HoleCoord[] = Array.from({ length: 4 }, (_, i) => hole(i, 0));
    const doc = makeDoc({ conductors: [solderTrace('t1', shortPath)] });
    const violations = byRule(runDrc(doc, makeLookup([])), 'solder-trace-too-long');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 9: current-capacity
// ---------------------------------------------------------------------------

describe('current-capacity', () => {
  it('flags a pure solder trace whose cross-section cannot carry the declared current', () => {
    // light buildup = 0.15mm^2 -> capacity = 0.15 * 5 A/mm^2 = 0.75A at defaults.
    const net: Net = { id: 'n1', name: 'VBUS', nodes: [], class: 'power', currentA: 3 };
    const path = [hole(0, 0), hole(1, 0), hole(2, 0)];
    const doc = makeDoc({
      nets: [net],
      conductors: [solderTrace('t1', path, { buildup: 'light', netId: 'n1' })],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'current-capacity');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
    expect(violations[0]?.message).toContain('mOhm');
    expect(violations[0]?.message).toContain('mV');
  });

  it('does not flag a solder trace with adequate cross-section for the declared current', () => {
    // heavy buildup = 0.6mm^2 -> capacity = 0.6 * 5 A/mm^2 = 3A at defaults; net draws 0.5A.
    const net: Net = { id: 'n1', name: 'VBUS', nodes: [], class: 'power', currentA: 0.5 };
    const path = [hole(0, 0), hole(1, 0), hole(2, 0)];
    const doc = makeDoc({
      nets: [net],
      conductors: [solderTrace('t1', path, { buildup: 'heavy', netId: 'n1' })],
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'current-capacity');
    expect(violations).toHaveLength(0);
  });

  it('a wired spine reduces the reported resistance versus a pure trace of the same length', () => {
    const netHot: Net = { id: 'n1', name: 'VBUS', nodes: [], class: 'power', currentA: 10 };
    const path = [hole(0, 0), hole(1, 0), hole(2, 0), hole(3, 0)];
    const pureDoc = makeDoc({
      nets: [netHot],
      conductors: [solderTrace('t1', path, { buildup: 'normal', netId: 'n1' })],
    });
    const wiredDoc = makeDoc({
      nets: [netHot],
      conductors: [
        solderTraceWired('t1', path, { material: 'tinned-copper', gauge: 0.6 }, { buildup: 'normal', netId: 'n1' }),
      ],
    });
    const pureMsg = byRule(runDrc(pureDoc, makeLookup([])), 'current-capacity')[0]?.message ?? '';
    const wiredMsg = byRule(runDrc(wiredDoc, makeLookup([])), 'current-capacity')[0]?.message ?? '';
    const extractMOhm = (msg: string): number => Number(/~([\d.]+) mOhm/.exec(msg)?.[1] ?? NaN);
    expect(extractMOhm(wiredMsg)).toBeLessThan(extractMOhm(pureMsg));
  });
});

// ---------------------------------------------------------------------------
// Rule 10: creepage-clearance
// ---------------------------------------------------------------------------

describe('creepage-clearance', () => {
  it('flags a high-voltage conductor running next to a different net', () => {
    const fp = onePinFootprint('fp1');
    const net: Net = { id: 'n1', name: 'MAINS', nodes: [], class: 'power', voltageV: 400 };
    const doc = makeDoc({
      components: [makeComponent('c1', 'U1', 'fp1', hole(2, 1))], // neighbour of (2,2)
      nets: [net],
      conductors: [solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2)], { netId: 'n1' })],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'creepage-clearance');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
  });

  it('does not flag a low-voltage conductor running next to a different net', () => {
    const fp = onePinFootprint('fp1');
    const net: Net = { id: 'n1', name: 'SIGNAL', nodes: [], class: 'signal', voltageV: 12 };
    const doc = makeDoc({
      components: [makeComponent('c1', 'U1', 'fp1', hole(2, 1))],
      nets: [net],
      conductors: [solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2)], { netId: 'n1' })],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'creepage-clearance');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 11: lead-bend-too-long
// ---------------------------------------------------------------------------

describe('lead-bend-too-long', () => {
  it('flags a lead bend spanning more than the default threshold', () => {
    const doc = makeDoc({
      conductors: [leadBend('lb1', [hole(0, 0), hole(6, 0)], 'c1', '1')], // 6 holes > 4
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'lead-bend-too-long');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
    expect(violations[0]?.componentIds).toEqual(['c1']);
  });

  it('does not flag a short lead bend', () => {
    const doc = makeDoc({
      conductors: [leadBend('lb1', [hole(0, 0), hole(2, 0)], 'c1', '1')], // 2 holes <= 4
    });
    const violations = byRule(runDrc(doc, makeLookup([])), 'lead-bend-too-long');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Rule 12: pin-not-connected
// ---------------------------------------------------------------------------

describe('pin-not-connected', () => {
  it('flags a schematic pin whose physical net has no conductor and no other pin', () => {
    const fp = onePinFootprint('fp1');
    const net: Net = { id: 'n1', name: 'NET1', nodes: [{ componentRef: 'R1', pin: '1' }], class: 'signal' };
    const doc = makeDoc({
      components: [makeComponent('c1', 'R1', 'fp1', hole(5, 5))],
      nets: [net],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'pin-not-connected');
    expect(violations).toHaveLength(1);
    expect(violations[0]?.severity).toBe('warning');
    expect(violations[0]?.holes).toEqual([hole(5, 5)]);
  });

  it('does not flag a pin that has a conductor attached, even if it reaches nowhere useful', () => {
    // Simplified rule per spec: any conductor touching the pin counts as "not isolated",
    // even though this would still be an OPEN net for full LVS (lvs.ts's job, not this file's).
    const fp = onePinFootprint('fp1');
    const net: Net = { id: 'n1', name: 'NET1', nodes: [{ componentRef: 'R1', pin: '1' }], class: 'signal' };
    const doc = makeDoc({
      components: [makeComponent('c1', 'R1', 'fp1', hole(5, 5))],
      nets: [net],
      conductors: [bareWire('w1', [hole(5, 5), hole(6, 5)])],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'pin-not-connected');
    expect(violations).toHaveLength(0);
  });

  it('does not flag a pin connected to another pin of the same net', () => {
    const fp = onePinFootprint('fp1');
    const net: Net = {
      id: 'n1',
      name: 'NET1',
      nodes: [
        { componentRef: 'R1', pin: '1' },
        { componentRef: 'R2', pin: '1' },
      ],
      class: 'signal',
    };
    const doc = makeDoc({
      components: [
        makeComponent('c1', 'R1', 'fp1', hole(5, 5)),
        makeComponent('c2', 'R2', 'fp1', hole(6, 5)),
      ],
      nets: [net],
      conductors: [solderTrace('t1', [hole(5, 5), hole(6, 5)])],
    });
    const violations = byRule(runDrc(doc, makeLookup([fp])), 'pin-not-connected');
    expect(violations).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

describe('determinism', () => {
  it('runDrc is deterministic and stably sorted across repeated runs', () => {
    const fp = onePinFootprint('fp1');
    const boxFp = boxFootprint('box', 3, 3);
    const net: Net = { id: 'n1', name: 'VBUS', nodes: [{ componentRef: 'R1', pin: '1' }], class: 'power', currentA: 3 };
    const doc = makeDoc({
      board: board({ material: 'FR2' }),
      components: [
        makeComponent('c1', 'R1', 'box', hole(0, 0)),
        makeComponent('c2', 'R2', 'box', hole(1, 0)), // overlaps c1
        makeComponent('c3', 'U1', 'fp1', hole(2, 1)),
      ],
      nets: [net],
      conductors: [
        solderTrace('t1', [hole(1, 2), hole(2, 2), hole(3, 2), hole(4, 2), hole(5, 2), hole(6, 2), hole(7, 2)], {
          buildup: 'light',
          netId: 'n1',
        }),
      ],
    });
    const lookup = makeLookup([fp, boxFp]);

    const first = runDrc(doc, lookup);
    const second = runDrc(doc, lookup);
    expect(second).toEqual(first);
    expect(first.length).toBeGreaterThan(1); // sanity: this fixture exercises multiple rules

    // Stably sorted: rule ids must be non-decreasing across the whole output.
    for (let i = 1; i < first.length; i++) {
      const prevRule = first[i - 1]?.rule ?? '';
      const curRule = first[i]?.rule ?? '';
      expect(prevRule <= curRule).toBe(true);
    }

    const third = runDrc(doc, lookup);
    expect(third).toEqual(first);
  });

  it('DEFAULT_DRC_OPTIONS is a stable, fully-populated defaults object', () => {
    expect(DEFAULT_DRC_OPTIONS.padLiftingMaxSolderTracePads).toBe(6);
    expect(DEFAULT_DRC_OPTIONS.solderTraceFeasibilityMaxPads).toBe(6);
    expect(DEFAULT_DRC_OPTIONS.creepageVoltageThresholdV).toBe(300);
    expect(DEFAULT_DRC_OPTIONS.maxLeadBendHoles).toBe(4);
  });
});

// Keep BoardMaterial referenced so a future board-material option change fails loudly at compile time.
const _materials: readonly BoardMaterial[] = ['FR4', 'FR2', 'FR1'];
void _materials;
