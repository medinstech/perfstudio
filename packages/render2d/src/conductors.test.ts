import type { Conductor, HoleCoord } from '@perfstudio/core';
import { describe, expect, it } from 'vitest';

import { drawConductor } from './conductors.js';
import { createRecordingContext } from './recording-context.js';
import { makeBoard } from './test-fixtures.js';
import { DEFAULT_THEME } from './theme.js';
import type { Viewport } from './transform.js';

const board = makeBoard();
const view: Viewport = { panXMm: 0, panYMm: 0, scale: 10, widthPx: 500, heightPx: 500, dpr: 1, side: 'top' };
const straightPath: HoleCoord[] = [
  { col: 0, row: 0 },
  { col: 1, row: 0 },
  { col: 2, row: 0 },
];
const bentPath: HoleCoord[] = [
  { col: 0, row: 0 },
  { col: 1, row: 0 },
  { col: 2, row: 0 },
  { col: 2, row: 1 },
];

function render(conductor: Conductor) {
  const { ctx, recorder } = createRecordingContext();
  drawConductor(ctx, conductor, board, view, DEFAULT_THEME);
  return recorder;
}

describe('conductor kind visual signatures', () => {
  it('solder-trace: one arc (bead) per path hole, no line dash', () => {
    const rec = render({
      id: 'c1',
      kind: 'solder-trace',
      path: straightPath,
      side: 'bottom',
      layerZ: 0,
      buildup: 'normal',
    });
    expect(rec.callsOf('arc').length).toBe(straightPath.length);
    expect(rec.callsOf('setLineDash').length).toBe(0);
  });

  it('solder-trace-wired: same bead count as solder-trace, plus an extra spine stroke', () => {
    const plain = render({
      id: 'c1',
      kind: 'solder-trace',
      path: straightPath,
      side: 'bottom',
      layerZ: 0,
      buildup: 'normal',
    });
    const wired = render({
      id: 'c2',
      kind: 'solder-trace-wired',
      path: straightPath,
      side: 'bottom',
      layerZ: 0,
      buildup: 'normal',
      spine: { material: 'tinned-copper', gauge: 0.6 },
    });

    expect(wired.callsOf('arc').length).toBe(plain.callsOf('arc').length);
    expect(wired.callsOf('stroke').length).toBeGreaterThan(plain.callsOf('stroke').length);
  });

  it('bare-wire: dots only at the two endpoints, none at intermediate bends', () => {
    const rec = render({ id: 'c3', kind: 'bare-wire', path: bentPath, side: 'bottom', layerZ: 0 });
    // 4-hole path but only start and end are electrical contacts.
    expect(rec.callsOf('arc').length).toBe(2);
    expect(rec.callsOf('lineTo').length).toBe(bentPath.length - 1);
  });

  it('insulated-wire: two strokes (outline + main), no beads, no dash', () => {
    const rec = render({
      id: 'c4',
      kind: 'insulated-wire',
      path: straightPath,
      side: 'bottom',
      layerZ: 0,
      color: '#00ff00',
    });
    expect(rec.callsOf('arc').length).toBe(0);
    expect(rec.callsOf('setLineDash').length).toBe(0);
    expect(rec.callsOf('stroke').length).toBe(2);
  });

  it('top-jumper: sets a non-empty line dash, then resets it', () => {
    const rec = render({ id: 'c5', kind: 'top-jumper', path: straightPath, side: 'top', layerZ: 0 });
    const dashCalls = rec.callsOf('setLineDash');
    expect(dashCalls.length).toBe(2);
    const firstArgs = dashCalls[0]?.args[0] as number[] | undefined;
    const secondArgs = dashCalls[1]?.args[0] as number[] | undefined;
    expect(firstArgs && firstArgs.length).toBeGreaterThan(0);
    expect(secondArgs && secondArgs.length).toBe(0);
  });

  it('lead-bend: a plain thin stroke, no beads and no dash', () => {
    const rec = render({
      id: 'c6',
      kind: 'lead-bend',
      path: straightPath,
      side: 'bottom',
      layerZ: 0,
      componentId: 'comp-1',
      pinNumber: '1',
    });
    expect(rec.callsOf('arc').length).toBe(0);
    expect(rec.callsOf('setLineDash').length).toBe(0);
    expect(rec.callsOf('stroke').length).toBe(1);
  });

  it('every kind produces a distinguishable (method, count) signature', () => {
    const signatures = new Map<string, string>();
    const conductors: Conductor[] = [
      { id: 'a', kind: 'solder-trace', path: straightPath, side: 'bottom', layerZ: 0, buildup: 'normal' },
      {
        id: 'b',
        kind: 'solder-trace-wired',
        path: straightPath,
        side: 'bottom',
        layerZ: 0,
        buildup: 'normal',
      },
      { id: 'c', kind: 'bare-wire', path: straightPath, side: 'bottom', layerZ: 0 },
      { id: 'd', kind: 'insulated-wire', path: straightPath, side: 'bottom', layerZ: 0 },
      { id: 'e', kind: 'top-jumper', path: straightPath, side: 'top', layerZ: 0 },
      { id: 'f', kind: 'lead-bend', path: straightPath, side: 'bottom', layerZ: 0, componentId: 'x', pinNumber: '1' },
    ];
    for (const c of conductors) {
      const rec = render(c);
      const sig = ['arc', 'stroke', 'setLineDash', 'fill'].map((m) => `${m}:${rec.callsOf(m).length}`).join(',');
      signatures.set(c.kind, sig);
    }
    // All six signatures must be pairwise distinct.
    expect(new Set(signatures.values()).size).toBe(signatures.size);
  });
});
