import { describe, expect, it } from 'vitest';
import type { Board, ComponentInstance, Footprint, HoleCoord } from './model.js';
import {
  allPinHoles,
  boardOutlineMm,
  boardSizeMm,
  chebyshev,
  coordToHoleRef,
  holeKey,
  holeRefToCoord,
  holeSpanMm,
  holeToMm,
  isAdjacent4,
  isAdjacent8,
  isInsideBoard,
  manhattan,
  neighbors4,
  neighbors8,
  pathLengthMm,
  pinHole,
  sameHole,
  transformPinOffset,
  validateOrthogonalChain,
} from './geometry.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const board: Board = {
  type: 'pad-per-hole',
  cols: 20,
  rows: 15,
  pitch: 2.54,
  thickness: 1.6,
  material: 'FR4',
  padDiameter: 1.8,
  drillDiameter: 0.9,
};

/** A DIP-8 style footprint: two rows of 4 pins, 0.1" apart, 0.3" (3 grid steps) wide. */
const dip8Footprint: Footprint = {
  id: 'dip8',
  name: 'DIP-8',
  pins: [
    { number: '1', dCol: 0, dRow: 0 },
    { number: '2', dCol: 0, dRow: 1 },
    { number: '3', dCol: 0, dRow: 2 },
    { number: '4', dCol: 0, dRow: 3 },
    { number: '5', dCol: 3, dRow: 3 },
    { number: '6', dCol: 3, dRow: 2 },
    { number: '7', dCol: 3, dRow: 1 },
    { number: '8', dCol: 3, dRow: 0 },
  ],
  bodyOutline: [
    { x: -1.27, y: -1.27 },
    { x: 8.89, y: -1.27 },
    { x: 8.89, y: 8.89 },
    { x: -1.27, y: 8.89 },
  ],
  bodyHeight: 5,
  body: { archetype: 'dip', dims: { length: 9.8, width: 7.6 } },
  leadDiameter: 0.4,
  polarized: false,
};

function makeComponent(overrides: Partial<ComponentInstance> = {}): ComponentInstance {
  return {
    id: 'c1',
    ref: 'U1',
    value: 'NE555',
    footprintId: 'dip8',
    anchor: { col: 2, row: 2 },
    rotation: 0,
    mirrored: false,
    locked: false,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// coordToHoleRef / holeRefToCoord
// ---------------------------------------------------------------------------

describe('coordToHoleRef / holeRefToCoord', () => {
  it('round-trips col 0..2000 across several rows', () => {
    for (const row of [0, 1, 5, 41, 999]) {
      for (let col = 0; col <= 2000; col++) {
        const c: HoleCoord = { col, row };
        expect(holeRefToCoord(coordToHoleRef(c))).toEqual(c);
      }
    }
  });

  it('produces "A1" for the origin hole', () => {
    expect(coordToHoleRef({ col: 0, row: 0 })).toBe('A1');
    expect(holeRefToCoord('A1')).toEqual({ col: 0, row: 0 });
  });

  it.each([
    [24, 'Y1'],
    [25, 'Z1'],
    [26, 'AA1'],
    [50, 'AY1'],
    [51, 'AZ1'],
    [52, 'BA1'],
    [700, 'ZY1'],
    [701, 'ZZ1'],
    [702, 'AAA1'],
  ])('boundary: col %i <-> %s', (col, ref) => {
    expect(coordToHoleRef({ col, row: 0 })).toBe(ref);
    expect(holeRefToCoord(ref)).toEqual({ col, row: 0 });
  });

  it('maps 1-indexed row in the ref to 0-indexed row in the coord', () => {
    expect(coordToHoleRef({ col: 2, row: 11 })).toBe('C12');
    expect(holeRefToCoord('C12')).toEqual({ col: 2, row: 11 });
  });

  it.each(['1A', '', 'A0', 'A-1', 'A1.5', 'a1', 'AB', ' A1', 'A1 ', '-A1', 'A01'])(
    'throws a descriptive Error on malformed ref %j',
    (bad) => {
      expect(() => holeRefToCoord(bad)).toThrow(Error);
    },
  );
});

// ---------------------------------------------------------------------------
// holeKey
// ---------------------------------------------------------------------------

describe('holeKey', () => {
  it('is equal for equal coordinates and usable as a Map key', () => {
    const a: HoleCoord = { col: 3, row: 7 };
    const b: HoleCoord = { col: 3, row: 7 };
    expect(holeKey(a)).toBe(holeKey(b));

    const map = new Map<string, string>();
    map.set(holeKey(a), 'value');
    expect(map.get(holeKey(b))).toBe('value');
  });

  it('is different for different coordinates, including digit-boundary cases', () => {
    expect(holeKey({ col: 1, row: 23 })).not.toBe(holeKey({ col: 12, row: 3 }));
    expect(holeKey({ col: 1, row: 2 })).not.toBe(holeKey({ col: 2, row: 1 }));
  });
});

// ---------------------------------------------------------------------------
// holeToMm
// ---------------------------------------------------------------------------

describe('holeToMm', () => {
  it('places hole {0,0} at the origin', () => {
    expect(holeToMm({ col: 0, row: 0 }, board)).toEqual({ x: 0, y: 0 });
  });

  it('grows x with col and y with row, spaced by pitch', () => {
    const p = holeToMm({ col: 2, row: 3 }, board);
    expect(p.x).toBeCloseTo(2 * board.pitch, 10);
    expect(p.y).toBeCloseTo(3 * board.pitch, 10);
  });
});

// ---------------------------------------------------------------------------
// isInsideBoard
// ---------------------------------------------------------------------------

describe('isInsideBoard', () => {
  it('accepts holes within [0,cols) x [0,rows)', () => {
    expect(isInsideBoard({ col: 0, row: 0 }, board)).toBe(true);
    expect(isInsideBoard({ col: board.cols - 1, row: board.rows - 1 }, board)).toBe(true);
  });

  it('rejects holes on or past the far edge, and negative coordinates', () => {
    expect(isInsideBoard({ col: board.cols, row: 0 }, board)).toBe(false);
    expect(isInsideBoard({ col: 0, row: board.rows }, board)).toBe(false);
    expect(isInsideBoard({ col: -1, row: 0 }, board)).toBe(false);
    expect(isInsideBoard({ col: 0, row: -1 }, board)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// neighbors4 / neighbors8
// ---------------------------------------------------------------------------

describe('neighbors4', () => {
  it('returns all 4 neighbours in N, E, S, W order away from any edge', () => {
    expect(neighbors4({ col: 5, row: 5 }, board)).toEqual([
      { col: 5, row: 4 }, // N
      { col: 6, row: 5 }, // E
      { col: 5, row: 6 }, // S
      { col: 4, row: 5 }, // W
    ]);
  });

  it('clips to exactly 2 neighbours at the top-left corner', () => {
    const result = neighbors4({ col: 0, row: 0 }, board);
    expect(result).toHaveLength(2);
    expect(result).toEqual([
      { col: 1, row: 0 }, // E
      { col: 0, row: 1 }, // S
    ]);
  });

  it('clips to exactly 2 neighbours at the bottom-right corner', () => {
    const corner: HoleCoord = { col: board.cols - 1, row: board.rows - 1 };
    const result = neighbors4(corner, board);
    expect(result).toHaveLength(2);
    expect(result).toEqual([
      { col: board.cols - 1, row: board.rows - 2 }, // N
      { col: board.cols - 2, row: board.rows - 1 }, // W
    ]);
  });
});

describe('neighbors8', () => {
  it('returns all 8 neighbours in N,E,S,W,NE,SE,SW,NW order away from any edge', () => {
    expect(neighbors8({ col: 5, row: 5 }, board)).toEqual([
      { col: 5, row: 4 }, // N
      { col: 6, row: 5 }, // E
      { col: 5, row: 6 }, // S
      { col: 4, row: 5 }, // W
      { col: 6, row: 4 }, // NE
      { col: 6, row: 6 }, // SE
      { col: 4, row: 6 }, // SW
      { col: 4, row: 4 }, // NW
    ]);
  });

  it('clips to exactly 3 neighbours at the top-left corner', () => {
    const result = neighbors8({ col: 0, row: 0 }, board);
    expect(result).toHaveLength(3);
    expect(result).toEqual([
      { col: 1, row: 0 }, // E
      { col: 0, row: 1 }, // S
      { col: 1, row: 1 }, // SE
    ]);
  });
});

// ---------------------------------------------------------------------------
// Adjacency / distance predicates
// ---------------------------------------------------------------------------

describe('adjacency and distance', () => {
  it('manhattan and chebyshev distances', () => {
    expect(manhattan({ col: 0, row: 0 }, { col: 3, row: 4 })).toBe(7);
    expect(chebyshev({ col: 0, row: 0 }, { col: 3, row: 4 })).toBe(4);
    expect(manhattan({ col: 1, row: 1 }, { col: 1, row: 1 })).toBe(0);
  });

  it('isAdjacent4 is true only for orthogonal single-step neighbours', () => {
    expect(isAdjacent4({ col: 1, row: 1 }, { col: 1, row: 2 })).toBe(true);
    expect(isAdjacent4({ col: 1, row: 1 }, { col: 2, row: 1 })).toBe(true);
    expect(isAdjacent4({ col: 1, row: 1 }, { col: 2, row: 2 })).toBe(false); // diagonal
    expect(isAdjacent4({ col: 1, row: 1 }, { col: 1, row: 1 })).toBe(false); // same hole
    expect(isAdjacent4({ col: 1, row: 1 }, { col: 3, row: 1 })).toBe(false); // two steps
  });

  it('isAdjacent8 is true for orthogonal and diagonal single-step neighbours', () => {
    expect(isAdjacent8({ col: 1, row: 1 }, { col: 2, row: 2 })).toBe(true);
    expect(isAdjacent8({ col: 1, row: 1 }, { col: 1, row: 2 })).toBe(true);
    expect(isAdjacent8({ col: 1, row: 1 }, { col: 1, row: 1 })).toBe(false);
    expect(isAdjacent8({ col: 1, row: 1 }, { col: 3, row: 3 })).toBe(false);
  });

  it('sameHole', () => {
    expect(sameHole({ col: 4, row: 9 }, { col: 4, row: 9 })).toBe(true);
    expect(sameHole({ col: 4, row: 9 }, { col: 9, row: 4 })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// validateOrthogonalChain
// ---------------------------------------------------------------------------

describe('validateOrthogonalChain', () => {
  it('accepts an L-shaped orthogonal path', () => {
    const path: HoleCoord[] = [
      { col: 0, row: 0 },
      { col: 1, row: 0 },
      { col: 2, row: 0 },
      { col: 2, row: 1 },
      { col: 2, row: 2 },
    ];
    expect(validateOrthogonalChain(path)).toEqual({ ok: true });
  });

  it('rejects a diagonal step and reports its index', () => {
    const path: HoleCoord[] = [
      { col: 0, row: 0 },
      { col: 1, row: 1 }, // diagonal from previous
    ];
    const result = validateOrthogonalChain(path);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.index).toBe(1);
    }
  });

  it('rejects a repeated hole and reports its index', () => {
    const path: HoleCoord[] = [
      { col: 0, row: 0 },
      { col: 1, row: 0 },
      { col: 0, row: 0 }, // backtracks onto the start
    ];
    const result = validateOrthogonalChain(path);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.index).toBe(2);
    }
  });

  it('rejects paths shorter than 2 holes', () => {
    expect(validateOrthogonalChain([]).ok).toBe(false);
    expect(validateOrthogonalChain([{ col: 0, row: 0 }]).ok).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// pathLengthMm
// ---------------------------------------------------------------------------

describe('pathLengthMm', () => {
  it('sums Euclidean segment lengths', () => {
    const path: HoleCoord[] = [
      { col: 0, row: 0 },
      { col: 1, row: 0 },
      { col: 1, row: 1 },
    ];
    expect(pathLengthMm(path, board)).toBeCloseTo(2 * board.pitch, 10);
  });

  it('is 0 for a single-hole path and empty for an empty path', () => {
    expect(pathLengthMm([{ col: 0, row: 0 }], board)).toBe(0);
    expect(pathLengthMm([], board)).toBe(0);
  });

  it('accounts for diagonal segments with Pythagoras', () => {
    const path: HoleCoord[] = [
      { col: 0, row: 0 },
      { col: 3, row: 4 },
    ];
    expect(pathLengthMm(path, board)).toBeCloseTo(5 * board.pitch, 10);
  });
});

// ---------------------------------------------------------------------------
// transformPinOffset
// ---------------------------------------------------------------------------

describe('transformPinOffset', () => {
  it('rotating by 90 four times in a row returns to the identity', () => {
    let x = 3;
    let y = 1;
    for (let i = 0; i < 4; i++) {
      const r = transformPinOffset(x, y, 90, false);
      x = r.dCol;
      y = r.dRow;
    }
    expect({ dCol: x, dRow: y }).toEqual({ dCol: 3, dRow: 1 });
  });

  it('mirroring twice returns to the identity', () => {
    const once = transformPinOffset(3, 1, 0, true);
    const twice = transformPinOffset(once.dCol, once.dRow, 0, true);
    expect(twice).toEqual({ dCol: 3, dRow: 1 });
  });

  it('rotation=0, mirrored=false is the identity', () => {
    expect(transformPinOffset(5, -2, 0, false)).toEqual({ dCol: 5, dRow: -2 });
  });

  it('a single 90-degree clockwise step maps "right" to "down"', () => {
    expect(transformPinOffset(1, 0, 90, false)).toEqual({ dCol: 0, dRow: 1 });
  });

  it('180 degrees maps "right" to "left"', () => {
    expect(transformPinOffset(1, 0, 180, false)).toEqual({ dCol: -1, dRow: 0 });
  });

  it('270 degrees maps "right" to "up"', () => {
    expect(transformPinOffset(1, 0, 270, false)).toEqual({ dCol: 0, dRow: -1 });
  });

  it('mirror is applied about the vertical axis before rotation', () => {
    expect(transformPinOffset(3, 1, 0, true)).toEqual({ dCol: -3, dRow: 1 });
  });

  // Concrete DIP-8 style case, worked out by hand: pin 5 sits at the
  // bottom-right corner of the footprint (dCol: 3, dRow: 3), i.e. the
  // south-east diagonal from the anchor.
  it('DIP-8 pin 5 (SE corner) rotated 90 clockwise moves to the SW corner', () => {
    // A 90-degree clockwise turn sends E -> S and S -> W, so SE -> SW:
    // dCol flips sign (east becomes west), dRow (south) is unchanged.
    expect(transformPinOffset(3, 3, 90, false)).toEqual({ dCol: -3, dRow: 3 });
  });

  it('DIP-8 pin 5 (SE corner) mirrored only moves to the SW corner', () => {
    // Mirroring about the vertical axis flips east<->west, south is unchanged.
    expect(transformPinOffset(3, 3, 0, true)).toEqual({ dCol: -3, dRow: 3 });
  });

  it('DIP-8 pin 5 (SE corner) mirrored then rotated 90 clockwise moves to the NW corner', () => {
    // Mirror first: SE -> SW (dCol: -3, dRow: 3).
    // Then rotate 90 CW: S -> W and W -> N, so SW -> NW (dCol: -3, dRow: -3).
    expect(transformPinOffset(3, 3, 90, true)).toEqual({ dCol: -3, dRow: -3 });
  });
});

// ---------------------------------------------------------------------------
// pinHole / allPinHoles
// ---------------------------------------------------------------------------

describe('pinHole', () => {
  it('places an unrotated, unmirrored pin at anchor + offset', () => {
    const component = makeComponent({ anchor: { col: 2, row: 2 } });
    expect(pinHole(component, dip8Footprint, '1')).toEqual({ col: 2, row: 2 });
    expect(pinHole(component, dip8Footprint, '5')).toEqual({ col: 5, row: 5 });
  });

  it('applies rotation and mirroring around the anchor', () => {
    const component = makeComponent({ anchor: { col: 2, row: 2 }, rotation: 90 });
    // pin 5 offset (3,3) rotated 90 CW -> (-3, 3); anchor (2,2) -> (-1, 5).
    expect(pinHole(component, dip8Footprint, '5')).toEqual({ col: -1, row: 5 });
  });

  it('returns undefined for an unknown pin number', () => {
    const component = makeComponent();
    expect(pinHole(component, dip8Footprint, '99')).toBeUndefined();
  });
});

describe('allPinHoles', () => {
  it('returns one entry per footprint pin, matching pinHole for each', () => {
    const component = makeComponent({ anchor: { col: 4, row: 1 }, rotation: 180, mirrored: true });
    const all = allPinHoles(component, dip8Footprint);
    expect(all).toHaveLength(dip8Footprint.pins.length);
    for (const { pin, hole } of all) {
      expect(hole).toEqual(pinHole(component, dip8Footprint, pin.number));
    }
  });
});

/**
 * These two board measurements are easy to confuse and the consequences are not
 * cosmetic: mirroring the solder-side view about the wrong one shifts the whole hole
 * grid by half a pitch, and someone solders a board backwards. They are pinned here.
 */
describe('boardSizeMm vs holeSpanMm', () => {
  const board60x40: Board = {
    type: 'pad-per-hole',
    cols: 60,
    rows: 40,
    pitch: 2.54,
    thickness: 1.6,
    material: 'FR4',
    padDiameter: 1.9,
    drillDiameter: 1.0,
  };

  it('boardSizeMm is cols*pitch — the physical substrate, half a pitch past the outer holes', () => {
    expect(boardSizeMm(board60x40)).toEqual({ width: 60 * 2.54, height: 40 * 2.54 });
  });

  it('holeSpanMm is (cols-1)*pitch — first hole centre to last hole centre', () => {
    expect(holeSpanMm(board60x40)).toEqual({ width: 59 * 2.54, height: 39 * 2.54 });
  });

  it('they differ by exactly one pitch in each axis', () => {
    const size = boardSizeMm(board60x40);
    const span = holeSpanMm(board60x40);
    expect(size.width - span.width).toBeCloseTo(board60x40.pitch, 10);
    expect(size.height - span.height).toBeCloseTo(board60x40.pitch, 10);
  });

  it('reflecting a hole centre about holeSpanMm lands exactly on another hole centre', () => {
    const span = holeSpanMm(board60x40);
    for (const col of [0, 1, 17, 58, 59]) {
      const x = holeToMm({ col, row: 0 }, board60x40).x;
      const mirroredX = span.width - x;
      const expected = holeToMm({ col: board60x40.cols - 1 - col, row: 0 }, board60x40).x;
      expect(mirroredX).toBeCloseTo(expected, 10);
      // And the mirrored position must be an exact multiple of the pitch — the
      // half-pitch offset that boardSizeMm would introduce is what this catches.
      expect((mirroredX / board60x40.pitch) % 1).toBeCloseTo(0, 10);
    }
  });

  it('boardOutlineMm starts at negative half-pitch and covers every hole', () => {
    const rect = boardOutlineMm(board60x40);
    expect(rect.x).toBeCloseTo(-board60x40.pitch / 2, 10);
    expect(rect.y).toBeCloseTo(-board60x40.pitch / 2, 10);
    const last = holeToMm({ col: 59, row: 39 }, board60x40);
    expect(rect.x + rect.width).toBeGreaterThan(last.x);
    expect(rect.y + rect.height).toBeGreaterThan(last.y);
  });

  it('degenerate 1-column board has zero hole span but one pitch of substrate', () => {
    const tiny: Board = { ...board60x40, cols: 1, rows: 1 };
    expect(holeSpanMm(tiny)).toEqual({ width: 0, height: 0 });
    expect(boardSizeMm(tiny)).toEqual({ width: 2.54, height: 2.54 });
  });
});
