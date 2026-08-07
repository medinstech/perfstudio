import { describe, expect, it } from 'vitest';

import { makeBoard } from './test-fixtures.js';
import type { Viewport } from './transform.js';
import { boardSpanMm, holeToScreen, screenToHole } from './transform.js';

const board = makeBoard({ cols: 10, rows: 6, pitch: 2.54 });

function makeView(overrides: Partial<Viewport> = {}): Viewport {
  return {
    panXMm: 0,
    panYMm: 0,
    scale: 10,
    widthPx: 1000,
    heightPx: 1000,
    dpr: 1,
    side: 'top',
    ...overrides,
  };
}

describe('screenToHole(holeToScreen(c)) round-trip', () => {
  it('round-trips every hole on the board for side="top"', () => {
    const view = makeView({ side: 'top' });
    for (let row = 0; row < board.rows; row++) {
      for (let col = 0; col < board.cols; col++) {
        const c = { col, row };
        const p = holeToScreen(c, board, view);
        expect(screenToHole(p.x, p.y, board, view)).toEqual(c);
      }
    }
  });

  it('round-trips every hole on the board for side="bottom" (mirrored)', () => {
    const view = makeView({ side: 'bottom' });
    for (let row = 0; row < board.rows; row++) {
      for (let col = 0; col < board.cols; col++) {
        const c = { col, row };
        const p = holeToScreen(c, board, view);
        expect(screenToHole(p.x, p.y, board, view)).toEqual(c);
      }
    }
  });

  it('round-trips with non-trivial pan and scale, both sides', () => {
    for (const side of ['top', 'bottom'] as const) {
      const view = makeView({ side, scale: 6.5, panXMm: -3.2, panYMm: 4.1 });
      for (let row = 0; row < board.rows; row++) {
        for (let col = 0; col < board.cols; col++) {
          const c = { col, row };
          const p = holeToScreen(c, board, view);
          expect(screenToHole(p.x, p.y, board, view)).toEqual(c);
        }
      }
    }
  });
});

describe('bottom-side mirroring', () => {
  it('actually mirrors: top and bottom screen positions differ for an off-centre hole', () => {
    const topView = makeView({ side: 'top' });
    const bottomView = makeView({ side: 'bottom' });
    const c = { col: 0, row: 0 };
    const topP = holeToScreen(c, board, topView);
    const bottomP = holeToScreen(c, board, bottomView);
    expect(bottomP.x).not.toBeCloseTo(topP.x, 5);
    expect(bottomP.y).toBeCloseTo(topP.y, 5); // mirror is horizontal only
  });

  it('mirrors about the board centreline: sum of top+bottom x equals the board width', () => {
    const { widthMm } = boardSpanMm(board);
    const topView = makeView({ side: 'top' });
    const bottomView = makeView({ side: 'bottom' });
    const c = { col: 2, row: 3 };
    const topP = holeToScreen(c, board, topView);
    const bottomP = holeToScreen(c, board, bottomView);
    expect(topP.x + bottomP.x).toBeCloseTo(widthMm * topView.scale, 5);
  });
});

describe('screenToHole out-of-bounds handling', () => {
  it('returns undefined for a screen point far outside the board', () => {
    const view = makeView();
    expect(screenToHole(-500, -500, board, view)).toBeUndefined();
    expect(screenToHole(100000, 100000, board, view)).toBeUndefined();
  });
});
