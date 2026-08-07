/**
 * Hole grid drawing: pad annulus + drilled hole, with mandatory viewport culling.
 *
 * At 100x60 a board has 6000 holes; drawing all of them every frame regardless of
 * what is on screen does not hold 60fps. `visibleHoleRange` (transform.ts) narrows
 * the loop to the holes that could possibly intersect the visible rect, and this
 * module reports exactly how many were drawn vs. culled so callers/tests can verify
 * the culling actually happened.
 */

import type { Board } from '@perfstudio/core';

import type { Theme } from './theme.js';
import type { Viewport } from './transform.js';
import { holeToScreen, visibleHoleRange } from './transform.js';

export interface HoleGridStats {
  readonly holesDrawn: number;
  readonly holesCulled: number;
}

export function drawHoleGrid(
  ctx: CanvasRenderingContext2D,
  board: Board,
  view: Viewport,
  theme: Theme,
): HoleGridStats {
  const total = board.cols * board.rows;
  const range = visibleHoleRange(board, view);

  if (range.colMax < range.colMin || range.rowMax < range.rowMin) {
    return { holesDrawn: 0, holesCulled: total };
  }

  // 'plain' boards have no copper: only the drilled hole is meaningful. Stripboard
  // (v2) still gets isolated pads drawn here as a placeholder — its continuous-strip
  // copper rendering is out of scope for this pass (PLAN.md marks 'strip' as v2).
  const drawPad = board.type !== 'plain';
  const padRadiusPx = (board.padDiameter / 2) * view.scale;
  const drillRadiusPx = (board.drillDiameter / 2) * view.scale;

  let drawn = 0;
  for (let row = range.rowMin; row <= range.rowMax; row++) {
    for (let col = range.colMin; col <= range.colMax; col++) {
      const p = holeToScreen({ col, row }, board, view);

      if (drawPad) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, padRadiusPx, 0, Math.PI * 2);
        ctx.fillStyle = theme.pad;
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(p.x, p.y, drillRadiusPx, 0, Math.PI * 2);
      ctx.fillStyle = theme.hole;
      ctx.fill();

      drawn++;
    }
  }

  return { holesDrawn: drawn, holesCulled: total - drawn };
}
