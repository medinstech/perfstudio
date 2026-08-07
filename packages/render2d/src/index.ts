/**
 * Host-agnostic 2D board renderer.
 *
 * This package never references `window`, `document`, `requestAnimationFrame` or any
 * other global. It draws into whatever `CanvasRenderingContext2D`-shaped object it is
 * given, at an explicit width/height/devicePixelRatio — so the same renderer runs in
 * the Tauri webview, in a headless Node canvas (soldering-guide step images), and in
 * tests against a recording stub (see PLAN.md §8.3).
 */

import type { Board, Footprint, HoleCoord, PerfDocument } from '@perfstudio/core';

import { drawComponent } from './components.js';
import { drawConductor } from './conductors.js';
import { drawHoleGrid } from './holes.js';
import type { Theme } from './theme.js';
import { boardMmToScreenPx, boardSpanMm, holeToScreen, screenToHole } from './transform.js';
import type { Viewport } from './transform.js';

export type { Viewport } from './transform.js';
export { holeToScreen, screenToHole } from './transform.js';
export type { Theme } from './theme.js';
export { DEFAULT_THEME, DARK_THEME } from './theme.js';

export interface RenderInput {
  readonly doc: PerfDocument;
  readonly footprints: (id: string) => Footprint | undefined;
  /** Component or conductor ids. */
  readonly selection?: ReadonlySet<string>;
  /** DRC R5' solder-trace proximity risks, drawn as red rings. */
  readonly riskHoles?: readonly HoleCoord[];
}

export interface RenderStats {
  readonly holesDrawn: number;
  readonly holesCulled: number;
  readonly conductorsDrawn: number;
  readonly componentsDrawn: number;
}

function drawSubstrate(ctx: CanvasRenderingContext2D, board: Board, view: Viewport, theme: Theme): void {
  const { widthMm, heightMm } = boardSpanMm(board);
  // Extend past the outermost hole centres so the outer ring of pads/drills isn't
  // drawn hanging off the edge of the substrate.
  const marginMm = Math.max(board.pitch / 2, board.padDiameter / 2);
  const a = boardMmToScreenPx({ x: -marginMm, y: -marginMm }, board, view);
  const b = boardMmToScreenPx({ x: widthMm + marginMm, y: heightMm + marginMm }, board, view);

  ctx.fillStyle = theme.substrate[board.material];
  ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
}

function drawSelectionHighlight(
  ctx: CanvasRenderingContext2D,
  doc: PerfDocument,
  selection: ReadonlySet<string>,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const prevAlpha = ctx.globalAlpha;
  ctx.globalAlpha = 0.9;
  ctx.strokeStyle = theme.selection;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  for (const conductor of doc.conductors) {
    if (!selection.has(conductor.id) || conductor.path.length === 0) {
      continue;
    }
    ctx.lineWidth = Math.max(2, view.scale * 0.6);
    ctx.beginPath();
    conductor.path.forEach((hole, i) => {
      const p = holeToScreen(hole, board, view);
      if (i === 0) {
        ctx.moveTo(p.x, p.y);
      } else {
        ctx.lineTo(p.x, p.y);
      }
    });
    ctx.stroke();
  }

  for (const component of doc.components) {
    if (!selection.has(component.id)) {
      continue;
    }
    const p = holeToScreen(component.anchor, board, view);
    const r = Math.max(4, view.scale * 1.2);
    ctx.lineWidth = Math.max(2, view.scale * 0.3);
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.globalAlpha = prevAlpha;
}

function drawRiskRings(
  ctx: CanvasRenderingContext2D,
  riskHoles: readonly HoleCoord[],
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const r = Math.max(3, (board.padDiameter / 2) * view.scale * 1.4);
  ctx.strokeStyle = theme.riskMarker;
  ctx.lineWidth = Math.max(1.5, view.scale * 0.25);
  for (const hole of riskHoles) {
    const p = holeToScreen(hole, board, view);
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.stroke();
  }
}

/**
 * Renders one frame of the board into `ctx`. Draw order (back to front):
 * substrate -> hole grid (culled) -> conductors -> components -> selection highlight
 * -> risk rings.
 */
export function renderBoard(
  ctx: CanvasRenderingContext2D,
  input: RenderInput,
  view: Viewport,
  theme: Theme,
): RenderStats {
  const board = input.doc.board;

  ctx.save();
  // The backing store may be higher resolution than widthPx/heightPx (device pixel
  // ratio); this is the one place dpr is applied, so every other coordinate in this
  // module — and everything screenToHole/holeToScreen callers do — stays in plain
  // CSS-pixel space.
  if (view.dpr !== 1) {
    ctx.scale(view.dpr, view.dpr);
  }
  ctx.clearRect(0, 0, view.widthPx, view.heightPx);

  drawSubstrate(ctx, board, view, theme);

  const holeStats = drawHoleGrid(ctx, board, view, theme);

  for (const conductor of input.doc.conductors) {
    drawConductor(ctx, conductor, board, view, theme);
  }

  let componentsDrawn = 0;
  for (const component of input.doc.components) {
    const footprint = input.footprints(component.footprintId);
    if (footprint === undefined) {
      // Footprint not resolvable (e.g. missing from the library): skip, don't throw.
      continue;
    }
    drawComponent(ctx, component, footprint, board, view, theme);
    componentsDrawn++;
  }

  if (input.selection !== undefined && input.selection.size > 0) {
    drawSelectionHighlight(ctx, input.doc, input.selection, board, view, theme);
  }
  if (input.riskHoles !== undefined && input.riskHoles.length > 0) {
    drawRiskRings(ctx, input.riskHoles, board, view, theme);
  }

  ctx.restore();

  return {
    holesDrawn: holeStats.holesDrawn,
    holesCulled: holeStats.holesCulled,
    conductorsDrawn: input.doc.conductors.length,
    componentsDrawn,
  };
}
