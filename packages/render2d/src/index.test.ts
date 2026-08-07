import type { Conductor, Footprint, HoleCoord } from '@perfstudio/core';
import { describe, expect, it } from 'vitest';

import { DEFAULT_THEME, renderBoard } from './index.js';
import { createRecordingContext } from './recording-context.js';
import { makeBoard, makeComponent, makeDocument, makeFootprint } from './test-fixtures.js';
import type { Viewport } from './transform.js';

const view: Viewport = { panXMm: -2, panYMm: -2, scale: 10, widthPx: 400, heightPx: 300, dpr: 1, side: 'top' };

describe('renderBoard', () => {
  it('does not throw on an empty document', () => {
    const doc = makeDocument({ board: makeBoard({ cols: 10, rows: 6 }) });
    const { ctx } = createRecordingContext();
    expect(() => renderBoard(ctx, { doc, footprints: () => undefined }, view, DEFAULT_THEME)).not.toThrow();
  });

  it('returns stats consistent with an empty document', () => {
    const board = makeBoard({ cols: 10, rows: 6 });
    const doc = makeDocument({ board });
    const { ctx } = createRecordingContext();
    const stats = renderBoard(ctx, { doc, footprints: () => undefined }, view, DEFAULT_THEME);

    expect(stats.holesDrawn + stats.holesCulled).toBe(board.cols * board.rows);
    expect(stats.conductorsDrawn).toBe(0);
    expect(stats.componentsDrawn).toBe(0);
  });

  it('counts conductors and skips components whose footprint cannot be resolved', () => {
    const board = makeBoard({ cols: 10, rows: 6 });
    const conductor: Conductor = {
      id: 'c1',
      kind: 'bare-wire',
      path: [
        { col: 0, row: 0 },
        { col: 1, row: 0 },
      ],
      side: 'bottom',
      layerZ: 0,
    };
    const doc = makeDocument({ board, conductors: [conductor], components: [makeComponent()] });
    const { ctx } = createRecordingContext();
    // No footprint resolver match: the component must be skipped, not throw.
    const stats = renderBoard(ctx, { doc, footprints: () => undefined }, view, DEFAULT_THEME);

    expect(stats.conductorsDrawn).toBe(1);
    expect(stats.componentsDrawn).toBe(0);
  });

  it('draws a resolvable component and reports it', () => {
    const board = makeBoard({ cols: 10, rows: 6 });
    const component = makeComponent();
    const footprint = makeFootprint();
    const doc = makeDocument({ board, components: [component] });
    const lookup = (id: string): Footprint | undefined => (id === footprint.id ? footprint : undefined);
    const { ctx } = createRecordingContext();
    const stats = renderBoard(ctx, { doc, footprints: lookup }, view, DEFAULT_THEME);

    expect(stats.componentsDrawn).toBe(1);
  });

  it('does not throw with a selection and risk holes set', () => {
    const board = makeBoard({ cols: 10, rows: 6 });
    const conductor: Conductor = {
      id: 'c1',
      kind: 'bare-wire',
      path: [
        { col: 0, row: 0 },
        { col: 1, row: 0 },
      ],
      side: 'bottom',
      layerZ: 0,
    };
    const component = makeComponent();
    const doc = makeDocument({ board, conductors: [conductor], components: [component] });
    const footprint = makeFootprint();
    const lookup = (id: string): Footprint | undefined => (id === footprint.id ? footprint : undefined);
    const riskHoles: HoleCoord[] = [{ col: 1, row: 1 }];
    const { ctx, recorder } = createRecordingContext();

    expect(() =>
      renderBoard(
        ctx,
        { doc, footprints: lookup, selection: new Set([conductor.id, component.id]), riskHoles },
        view,
        DEFAULT_THEME,
      ),
    ).not.toThrow();

    // Risk ring is a stroked arc drawn with the theme's risk marker colour at some point.
    expect(recorder.callsOf('arc').length).toBeGreaterThan(0);
  });

  it('applies devicePixelRatio via a single ctx.scale call', () => {
    const board = makeBoard({ cols: 5, rows: 5 });
    const doc = makeDocument({ board });
    const { ctx, recorder } = createRecordingContext();
    renderBoard(ctx, { doc, footprints: () => undefined }, { ...view, dpr: 2 }, DEFAULT_THEME);
    const scaleCalls = recorder.callsOf('scale');
    expect(scaleCalls.length).toBe(1);
    expect(scaleCalls[0]?.args).toEqual([2, 2]);
  });
});
