import { describe, expect, it } from 'vitest';

import { drawHoleGrid } from './holes.js';
import { createRecordingContext } from './recording-context.js';
import { makeBoard } from './test-fixtures.js';
import { DEFAULT_THEME } from './theme.js';
import type { Viewport } from './transform.js';
import { boardSpanMm } from './transform.js';

describe('drawHoleGrid culling', () => {
  it('only draws a small fraction of a 100x60 board when the viewport shows a small corner', () => {
    const board = makeBoard({ cols: 100, rows: 60, pitch: 2.54 });
    const totalHoles = board.cols * board.rows; // 6000

    // A small window near the top-left corner, zoomed in.
    const view: Viewport = {
      panXMm: 0,
      panYMm: 0,
      scale: 20,
      widthPx: 200,
      heightPx: 150,
      dpr: 1,
      side: 'top',
    };

    const { ctx } = createRecordingContext();
    const stats = drawHoleGrid(ctx, board, view, DEFAULT_THEME);

    expect(stats.holesDrawn + stats.holesCulled).toBe(totalHoles);
    expect(stats.holesDrawn).toBeGreaterThan(0);
    // "Small fraction": well under 10% of the board should be visible in this window.
    expect(stats.holesDrawn).toBeLessThan(totalHoles * 0.1);
  });

  it('draws every hole when the viewport covers the whole board', () => {
    const board = makeBoard({ cols: 20, rows: 10, pitch: 2.54 });
    const totalHoles = board.cols * board.rows;
    // Use the shared span helper rather than re-deriving the formula here: a test
    // that recomputes what it is meant to validate would keep passing if the real
    // one drifted.
    const { widthMm, heightMm } = boardSpanMm(board);

    const view: Viewport = {
      panXMm: -5,
      panYMm: -5,
      scale: 10,
      widthPx: (widthMm + 10) * 10,
      heightPx: (heightMm + 10) * 10,
      dpr: 1,
      side: 'top',
    };

    const { ctx } = createRecordingContext();
    const stats = drawHoleGrid(ctx, board, view, DEFAULT_THEME);

    expect(stats.holesDrawn).toBe(totalHoles);
    expect(stats.holesCulled).toBe(0);
  });

  it('draws nothing (fully culled) when the viewport is entirely off-board', () => {
    const board = makeBoard({ cols: 100, rows: 60, pitch: 2.54 });
    const totalHoles = board.cols * board.rows;

    const view: Viewport = {
      panXMm: 100000,
      panYMm: 100000,
      scale: 10,
      widthPx: 200,
      heightPx: 200,
      dpr: 1,
      side: 'top',
    };

    const { ctx } = createRecordingContext();
    const stats = drawHoleGrid(ctx, board, view, DEFAULT_THEME);

    expect(stats.holesDrawn).toBe(0);
    expect(stats.holesCulled).toBe(totalHoles);
  });

  it('does not draw pad annuli for a "plain" board, only drilled holes', () => {
    const board = makeBoard({ cols: 5, rows: 5, type: 'plain' });
    const view: Viewport = { panXMm: -2, panYMm: -2, scale: 10, widthPx: 200, heightPx: 200, dpr: 1, side: 'top' };

    const { ctx, recorder } = createRecordingContext();
    const stats = drawHoleGrid(ctx, board, view, DEFAULT_THEME);

    // One arc (the drill) per drawn hole, not two (pad + drill).
    expect(recorder.callsOf('arc').length).toBe(stats.holesDrawn);
  });
});
