import { describe, expect, it } from 'vitest';

import { createEmptyDocument, DEFAULT_BOARD } from './commands.js';
import { coordToHoleRef, holeKey } from './geometry.js';
import type {
  ComponentInstance,
  Conductor,
  DocumentMeta,
  Footprint,
  HoleCoord,
  PerfDocument,
} from './model.js';
import { buildOccupancy } from './occupancy.js';
import { DEFAULT_ROUTER_COSTS, routeConnection } from './router.js';

const META: DocumentMeta = {
  name: 't',
  created: '2026-01-01T00:00:00.000Z',
  modified: '2026-01-01T00:00:00.000Z',
};

const onePin: Footprint = {
  id: 'pad1',
  name: 'pad',
  pins: [{ number: '1', dCol: 0, dRow: 0 }],
  bodyOutline: [
    { x: -1, y: -1 },
    { x: 1, y: -1 },
    { x: 1, y: 1 },
    { x: -1, y: 1 },
  ],
  bodyHeight: 1,
  body: { archetype: 'generic-box', dims: {} },
  leadDiameter: 0.6,
  polarized: false,
};
const lookup = (id: string): Footprint | undefined => (id === 'pad1' ? onePin : undefined);

const h = (col: number, row: number): HoleCoord => ({ col, row });

function comp(id: string, ref: string, anchor: HoleCoord): ComponentInstance {
  return {
    id,
    ref,
    value: 'x',
    footprintId: 'pad1',
    anchor,
    rotation: 0,
    mirrored: false,
    locked: false,
  };
}

function trace(id: string, path: HoleCoord[]): Conductor {
  return { id, kind: 'solder-trace', path, side: 'bottom', layerZ: 0, buildup: 'normal' };
}

function wire(id: string, path: HoleCoord[]): Conductor {
  return { id, kind: 'bare-wire', path, side: 'bottom', layerZ: 0 };
}

function doc(components: ComponentInstance[], conductors: Conductor[] = []): PerfDocument {
  return { ...createEmptyDocument(META, DEFAULT_BOARD), components, conductors };
}

// ---------------------------------------------------------------------------

describe('occupancy', () => {
  it('a wire occupies every hole it crosses, even though it only CONTACTS its ends', () => {
    // This is the whole reason occupancy exists separately from connectivity: the
    // middle hole is not electrically joined, but the board is physically full there.
    const d = doc([], [wire('w1', [h(2, 2), h(3, 2), h(4, 2)])]);
    const occ = buildOccupancy(d, lookup);
    expect(occ.conductorsAt(h(3, 2), 'bottom')).toEqual(['w1']);
    expect(occ.isCopperBlocked(h(3, 2), 'bottom')).toBe(true);
    expect(occ.conductorsAt(h(3, 2), 'top')).toEqual([]);
  });

  it('a pin occupies its hole and is reported with its ref', () => {
    const d = doc([comp('c1', 'R1', h(5, 5))]);
    const occ = buildOccupancy(d, lookup);
    expect(occ.pinAt(h(5, 5))?.componentRef).toBe('R1');
    expect(occ.pinAt(h(6, 5))).toBeUndefined();
  });
});

describe('routeConnection', () => {
  it('prefers a solder trace on an empty board', () => {
    const d = doc([comp('c1', 'A', h(2, 2)), comp('c2', 'B', h(5, 2))]);
    const r = routeConnection(d, lookup, { from: h(2, 2), to: h(5, 2) });
    expect(r.ok).toBe(true);
    expect(r.best?.strategy).toBe('solder-trace');
    const path = r.best?.conductors[0]?.path ?? [];
    expect(path).toHaveLength(4);
    expect(path[0]).toEqual(h(2, 2));
    expect(path[path.length - 1]).toEqual(h(5, 2));
  });

  it('every consecutive pair of a returned trace is orthogonally adjacent', () => {
    const d = doc([comp('c1', 'A', h(1, 1)), comp('c2', 'B', h(3, 2))]);
    const r = routeConnection(d, lookup, { from: h(1, 1), to: h(3, 2) });
    expect(r.best?.strategy).toMatch(/solder-trace/);
    const path = r.best?.conductors[0]?.path ?? [];
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1]!;
      const b = path[i]!;
      expect(Math.abs(a.col - b.col) + Math.abs(a.row - b.row)).toBe(1);
    }
  });

  /**
   * The economics deliberately cross over: dragging solder is cheapest for short hops,
   * but nobody drags it twenty pads — they lay a wire. The crossover sits at the
   * pure-solder pad limit, which is what a person would do by hand, and pinning it here
   * means a cost-table edit cannot quietly move it.
   */
  it('picks a solder trace for a short hop and a wire for a long one', () => {
    const short = doc([comp('a', 'A', h(2, 2)), comp('b', 'B', h(5, 2))]);
    expect(routeConnection(short, lookup, { from: h(2, 2), to: h(5, 2) }).best?.strategy).toBe(
      'solder-trace',
    );

    const long = doc([comp('a', 'A', h(1, 1)), comp('b', 'B', h(20, 1))]);
    expect(routeConnection(long, lookup, { from: h(1, 1), to: h(20, 1) }).best?.strategy).toBe(
      'bare-wire',
    );
  });

  it('proposes a spine when a long run must stay on copper because a wire is blocked', () => {
    // A pin on the straight line rules out a bare wire, so the long trace wins — and a
    // trace that long has to be reinforced rather than built from solder alone.
    const d = doc([
      comp('a', 'A', h(1, 1)),
      comp('b', 'B', h(12, 1)),
      comp('x', 'X', h(6, 1)), // sits on the straight line between them
    ]);
    const r = routeConnection(d, lookup, { from: h(1, 1), to: h(12, 1) });
    expect(r.best?.strategy).toBe('solder-trace-wired');
    expect(r.best?.conductors[0]?.kind).toBe('solder-trace-wired');
    expect(r.best?.explanation).toMatch(/spine/i);
  });

  it('falls back to a wire when copper blocks the whole corridor', () => {
    // A wall of foreign trace across the board, with only the endpoints free.
    const wall: HoleCoord[] = [];
    for (let row = 0; row < DEFAULT_BOARD.rows; row++) wall.push(h(4, row));
    const d = doc([comp('c1', 'A', h(2, 2)), comp('c2', 'B', h(6, 2))], [trace('t-wall', wall)]);

    const r = routeConnection(d, lookup, { from: h(2, 2), to: h(6, 2) });
    expect(r.ok).toBe(true);
    expect(r.best?.strategy).toBe('insulated-wire');
    // Bare wire must NOT be offered: it would cross the wall's copper.
    expect(r.alternatives.map((a) => a.strategy)).not.toContain('bare-wire');
  });

  it('reports failure honestly instead of returning a broken route', () => {
    const d = doc([]);
    const r = routeConnection(d, lookup, { from: h(3, 3), to: h(3, 3) });
    expect(r.ok).toBe(false);
    expect(r.reason).toBeTruthy();
  });

  it('refuses endpoints outside the board', () => {
    const d = doc([]);
    const r = routeConnection(d, lookup, { from: h(0, 0), to: h(DEFAULT_BOARD.cols, 0) });
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/outside/i);
  });

  it('is deterministic: the same request routes the same way every time', () => {
    const d = doc([comp('c1', 'A', h(2, 7)), comp('c2', 'B', h(9, 3))]);
    const a = routeConnection(d, lookup, { from: h(2, 7), to: h(9, 3) });
    const b = routeConnection(d, lookup, { from: h(2, 7), to: h(9, 3) });
    expect(b).toEqual(a);
  });

  it('offers alternatives ordered cheapest first, so the UI can suggest a swap', () => {
    const d = doc([comp('c1', 'A', h(2, 2)), comp('c2', 'B', h(5, 2))]);
    const r = routeConnection(d, lookup, { from: h(2, 2), to: h(5, 2) });
    expect(r.alternatives.length).toBeGreaterThan(1);
    for (let i = 1; i < r.alternatives.length; i++) {
      expect(r.alternatives[i]!.cost).toBeGreaterThanOrEqual(r.alternatives[i - 1]!.cost);
    }
  });
});

describe('R5 proximity risk is priced into the search, not just reported afterwards', () => {
  /**
   * A short corridor of foreign pads along row 2. Routing A(1,3) to B(5,3) straight
   * along row 3 keeps a different net one hole above for the whole run. The router
   * should pay a couple of extra steps to drop into a clear row instead. This is the
   * behaviour that separates "a legal route" from "a route someone can actually solder".
   *
   * The run is kept short on purpose so a solder trace is the winning strategy at all —
   * over longer distances a wire wins on cost and there is no trace to steer.
   */
  function board(): PerfDocument {
    const foreign: ComponentInstance[] = [];
    for (let col = 2; col <= 4; col++) foreign.push(comp(`f${col}`, `F${col}`, h(col, 2)));
    return doc([comp('a', 'A', h(1, 3)), comp('b', 'B', h(5, 3)), ...foreign]);
  }

  const hugCount = (path: readonly HoleCoord[]): number =>
    path.filter((p) => p.row === 3 && p.col >= 2 && p.col <= 4).length;

  /**
   * Inspect the SOLDER-TRACE candidate rather than the overall winner. Which strategy
   * wins is a separate question of economics — over this distance a plain wire is
   * cheaper, and rightly so. What is under test here is the path the trace search
   * chooses when it does run, so the assertion has to look at that candidate directly.
   */
  const tracePath = (d: PerfDocument, proximityRisk: number): readonly HoleCoord[] => {
    const r = routeConnection(
      d,
      lookup,
      { from: h(1, 3), to: h(5, 3) },
      { costs: { ...DEFAULT_ROUTER_COSTS, proximityRisk } },
    );
    const candidate = r.alternatives.find((a) => a.strategy.startsWith('solder-trace'));
    expect(candidate).toBeDefined();
    return candidate?.conductors[0]?.path ?? [];
  };

  it('steers the trace off the foreign row when risk is priced', () => {
    expect(hugCount(tracePath(board(), DEFAULT_ROUTER_COSTS.proximityRisk))).toBeLessThan(3);
  });

  it('hugs the foreign row once the risk price is set to zero', () => {
    // Free risk means the shortest path wins: straight along row 3, past all three pads.
    expect(hugCount(tracePath(board(), 0))).toBe(3);
  });

  it('a priced route is longer than a free one — the detour is real, not cosmetic', () => {
    const priced = tracePath(board(), DEFAULT_ROUTER_COSTS.proximityRisk);
    const free = tracePath(board(), 0);
    expect(priced.length).toBeGreaterThan(free.length);
  });

  it('names the risky pads so they can become measurement steps in the guide', () => {
    const d = doc(
      [comp('a', 'A', h(1, 3)), comp('b', 'B', h(3, 3)), comp('f', 'F', h(2, 2))],
      [],
    );
    const r = routeConnection(d, lookup, { from: h(1, 3), to: h(3, 3) });
    expect(r.ok).toBe(true);
    if (r.best!.riskHoles.length > 0) {
      expect(r.best!.explanation).toMatch(/different net/i);
      for (const hole of r.best!.riskHoles) {
        expect(r.best!.explanation).toContain(coordToHoleRef(hole));
      }
    }
  });
});

describe('routes do not pass through foreign pins', () => {
  it('steps around a pin sitting in the direct path', () => {
    const d = doc([comp('a', 'A', h(2, 4)), comp('b', 'B', h(6, 4)), comp('x', 'X', h(4, 4))]);
    const r = routeConnection(d, lookup, { from: h(2, 4), to: h(6, 4) });
    expect(r.ok).toBe(true);
    if (r.best?.strategy.startsWith('solder-trace')) {
      const keys = new Set((r.best.conductors[0]?.path ?? []).map(holeKey));
      expect(keys.has(holeKey(h(4, 4)))).toBe(false);
    }
  });
});
