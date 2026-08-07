import { describe, expect, it } from 'vitest';

import { drawComponent } from './components.js';
import { createRecordingContext } from './recording-context.js';
import { makeBoard, makeComponent, makeFootprint } from './test-fixtures.js';
import { DEFAULT_THEME } from './theme.js';
import type { Viewport } from './transform.js';
import { holeToScreen } from './transform.js';

const board = makeBoard();

describe('drawComponent label legibility threshold', () => {
  it('skips the ref label below the legibility scale threshold', () => {
    const view: Viewport = { panXMm: 0, panYMm: 0, scale: 1, widthPx: 500, heightPx: 500, dpr: 1, side: 'top' };
    const { ctx, recorder } = createRecordingContext();
    drawComponent(ctx, makeComponent(), makeFootprint(), board, view, DEFAULT_THEME);
    expect(recorder.callsOf('fillText').length).toBe(0);
  });

  it('draws the ref label above the legibility scale threshold', () => {
    const view: Viewport = { panXMm: 0, panYMm: 0, scale: 15, widthPx: 500, heightPx: 500, dpr: 1, side: 'top' };
    const { ctx, recorder } = createRecordingContext();
    drawComponent(ctx, makeComponent(), makeFootprint(), board, view, DEFAULT_THEME);
    const calls = recorder.callsOf('fillText');
    expect(calls.length).toBe(1);
    expect(calls[0]?.args[0]).toBe('R1');
  });
});

describe('drawComponent pin placement transform', () => {
  it('places an unrotated, unmirrored pin at anchor + (dCol, dRow)', () => {
    const view: Viewport = { panXMm: 0, panYMm: 0, scale: 10, widthPx: 500, heightPx: 500, dpr: 1, side: 'top' };
    const footprint = makeFootprint({ pins: [{ number: '1', dCol: 2, dRow: 0 }] });
    const component = makeComponent({ anchor: { col: 1, row: 1 }, rotation: 0, mirrored: false });
    const { ctx, recorder } = createRecordingContext();
    drawComponent(ctx, component, footprint, board, view, DEFAULT_THEME);

    const expected = holeToScreen({ col: 3, row: 1 }, board, view); // anchor.col + dCol
    const pinArc = recorder.callsOf('arc')[0];
    expect(pinArc?.args[0]).toBeCloseTo(expected.x, 5);
    expect(pinArc?.args[1]).toBeCloseTo(expected.y, 5);
  });

  it('a 90-degree clockwise rotation maps a pin to the right onto a pin below the anchor', () => {
    const view: Viewport = { panXMm: 0, panYMm: 0, scale: 10, widthPx: 500, heightPx: 500, dpr: 1, side: 'top' };
    const footprint = makeFootprint({ pins: [{ number: '1', dCol: 1, dRow: 0 }] });
    const component = makeComponent({ anchor: { col: 2, row: 2 }, rotation: 90, mirrored: false });
    const { ctx, recorder } = createRecordingContext();
    drawComponent(ctx, component, footprint, board, view, DEFAULT_THEME);

    // (1, 0) rotated 90 degrees clockwise in a y-down system -> (0, 1): straight down.
    const expected = holeToScreen({ col: 2, row: 3 }, board, view);
    const pinArc = recorder.callsOf('arc')[0];
    expect(pinArc?.args[0]).toBeCloseTo(expected.x, 5);
    expect(pinArc?.args[1]).toBeCloseTo(expected.y, 5);
  });
});
