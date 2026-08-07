/**
 * Conductor drawing — the point of the whole model (model.ts, PLAN.md §4.4/§4.6).
 * Perfboard has six physically distinct ways to make a connection; each must be
 * visually distinct enough that a reviewer can tell them apart at a glance:
 *
 *  - solder-trace:        beaded chain (filled circle per hole + tapered links).
 *  - solder-trace-wired:  same beaded chain PLUS a solid darker spine on top.
 *  - bare-wire:           thin solid line; solder-fillet dot at the two ENDPOINTS
 *                          only (only the endpoints are electrical — see core's
 *                          connectivity.ts, same rule as here).
 *  - insulated-wire:      thicker line in the conductor's colour, with a subtle
 *                          outline underneath so it reads as jacketed.
 *  - top-jumper:          dashed line in the conductor's colour.
 *  - lead-bend:           thin line in a lead-metal tone.
 */

import type {
  Board,
  Conductor,
  HoleCoord,
  LeadBendConductor,
  SolderTraceConductor,
  StripConductor,
  WireConductor,
} from '@perfstudio/core';

import type { Theme } from './theme.js';
import type { Viewport } from './transform.js';
import { holeToScreen } from './transform.js';

interface ScreenPoint {
  readonly x: number;
  readonly y: number;
}

function pathToScreen(path: readonly HoleCoord[], board: Board, view: Viewport): ScreenPoint[] {
  return path.map((h) => holeToScreen(h, board, view));
}

function strokePolyline(ctx: CanvasRenderingContext2D, pts: readonly ScreenPoint[]): void {
  if (pts.length === 0) {
    return;
  }
  const first = pts[0];
  if (first === undefined) {
    return;
  }
  ctx.beginPath();
  ctx.moveTo(first.x, first.y);
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i];
    if (p === undefined) {
      continue;
    }
    ctx.lineTo(p.x, p.y);
  }
  ctx.stroke();
}

function fillDot(ctx: CanvasRenderingContext2D, p: ScreenPoint, radiusPx: number, color: string): void {
  ctx.beginPath();
  ctx.arc(p.x, p.y, radiusPx, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

/**
 * Beads (filled circle per path hole) joined by links thinner than the beads, which
 * gives the "bulges at pads, thins in between" look PLAN.md §8.3 asks for.
 */
function drawBeadedChain(
  ctx: CanvasRenderingContext2D,
  pts: readonly ScreenPoint[],
  padRadiusPx: number,
  color: string,
): void {
  if (pts.length > 1) {
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, padRadiusPx * 0.9);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    strokePolyline(ctx, pts);
  }
  const beadRadiusPx = Math.max(1, padRadiusPx * 0.85);
  for (const p of pts) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, beadRadiusPx, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }
}

function drawSolderTrace(
  ctx: CanvasRenderingContext2D,
  conductor: SolderTraceConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  const padRadiusPx = (board.padDiameter / 2) * view.scale;
  drawBeadedChain(ctx, pts, padRadiusPx, theme.conductor['solder-trace']);

  if (conductor.kind === 'solder-trace-wired') {
    // Solid darker spine running the length of the trace, on top of the beads.
    ctx.strokeStyle = theme.conductorSpine;
    ctx.lineWidth = Math.max(1, padRadiusPx * 0.3);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    strokePolyline(ctx, pts);
  }
}

function drawBareWire(
  ctx: CanvasRenderingContext2D,
  conductor: WireConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  ctx.strokeStyle = theme.conductor['bare-wire'];
  ctx.lineWidth = Math.max(1, view.scale * 0.15);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  strokePolyline(ctx, pts);

  // Only the endpoints are electrical (core connectivity.ts): dots go there only.
  // Intermediate points are a plain bend in the wire, drawn above with no marker.
  const filletRadiusPx = Math.max(1, view.scale * 0.35);
  const first = pts[0];
  const last = pts[pts.length - 1];
  if (first !== undefined) {
    fillDot(ctx, first, filletRadiusPx, theme.solderFillet);
  }
  if (last !== undefined && pts.length > 1) {
    fillDot(ctx, last, filletRadiusPx, theme.solderFillet);
  }
}

function drawInsulatedWire(
  ctx: CanvasRenderingContext2D,
  conductor: WireConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  const color = conductor.color ?? theme.conductor['insulated-wire'];
  const widthPx = Math.max(1, view.scale * 0.45);

  // Subtle outline first, main colour on top: reads as an insulated jacket.
  ctx.strokeStyle = theme.insulatedOutline;
  ctx.lineWidth = widthPx + 2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  strokePolyline(ctx, pts);

  ctx.strokeStyle = color;
  ctx.lineWidth = widthPx;
  strokePolyline(ctx, pts);
}

function drawTopJumper(
  ctx: CanvasRenderingContext2D,
  conductor: WireConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  const color = conductor.color ?? theme.conductor['top-jumper'];
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(1, view.scale * 0.4);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.setLineDash([Math.max(2, view.scale * 0.6), Math.max(2, view.scale * 0.4)]);
  strokePolyline(ctx, pts);
  ctx.setLineDash([]); // reset so subsequent draws are not dashed
}

function drawLeadBend(
  ctx: CanvasRenderingContext2D,
  conductor: LeadBendConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  ctx.strokeStyle = theme.conductor['lead-bend'];
  ctx.lineWidth = Math.max(1, view.scale * 0.12);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  strokePolyline(ctx, pts);
}

function drawStrip(
  ctx: CanvasRenderingContext2D,
  conductor: StripConductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  const pts = pathToScreen(conductor.path, board, view);
  ctx.strokeStyle = theme.conductor.strip;
  ctx.lineWidth = Math.max(1, (board.padDiameter / 2) * view.scale * 1.6);
  ctx.lineCap = 'butt';
  ctx.lineJoin = 'round';
  strokePolyline(ctx, pts);
}

export function drawConductor(
  ctx: CanvasRenderingContext2D,
  conductor: Conductor,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  switch (conductor.kind) {
    case 'solder-trace':
    case 'solder-trace-wired':
      drawSolderTrace(ctx, conductor, board, view, theme);
      return;
    case 'bare-wire':
      drawBareWire(ctx, conductor, board, view, theme);
      return;
    case 'insulated-wire':
      drawInsulatedWire(ctx, conductor, board, view, theme);
      return;
    case 'top-jumper':
      drawTopJumper(ctx, conductor, board, view, theme);
      return;
    case 'lead-bend':
      drawLeadBend(ctx, conductor, board, view, theme);
      return;
    case 'strip':
      drawStrip(ctx, conductor, board, view, theme);
      return;
  }
}
