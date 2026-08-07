/**
 * Coordinate transforms between board millimetres, screen pixels and hole indices.
 *
 * Geometry comes from `@perfstudio/core`; this module owns only the screen/viewport
 * layer on top of it.
 *
 * Note which board measurement the mirror uses: `holeSpanMm`, the first-to-last hole
 * centre distance, NOT `boardSizeMm`, the physical substrate. The reflection has to
 * land hole 0 exactly on hole (cols-1); reflecting about the substrate would shift the
 * entire grid by half a pitch and the solder-side view would be quietly wrong.
 *
 * Screen space is CSS pixels — the same units as `Viewport.widthPx/heightPx` and
 * whatever a host reports for pointer events. `Viewport.dpr` is applied once, inside
 * `renderBoard`, via `ctx.scale(dpr, dpr)`, purely so the canvas backing store can be
 * higher resolution; it never appears in these coordinate formulas. That keeps
 * `screenToHole`/`holeToScreen` usable directly against pointer coordinates without
 * every caller having to remember to divide out the device pixel ratio.
 */

import type { Board, BoardSide, HoleCoord, Point2 } from '@perfstudio/core';
import { holeSpanMm, holeToMm } from '@perfstudio/core';

export { holeToMm };

export interface Viewport {
  readonly panXMm: number;
  readonly panYMm: number;
  /** Pixels per millimetre. */
  readonly scale: number;
  readonly widthPx: number;
  readonly heightPx: number;
  readonly dpr: number;
  /** 'bottom' renders MIRRORED horizontally (solder-side view). */
  readonly side: BoardSide;
}

/**
 * Board span in mm, centre-to-centre from the first to the last hole. Thin adapter
 * over core's `holeSpanMm`, kept because this package's callers use the `widthMm`
 * / `heightMm` naming throughout.
 */
export function boardSpanMm(board: Board): { readonly widthMm: number; readonly heightMm: number } {
  const span = holeSpanMm(board);
  return { widthMm: span.width, heightMm: span.height };
}

/**
 * Board-space mm -> screen-space px.
 *
 * Board space is unmirrored: x grows right with col, y grows down with row, exactly
 * as HoleCoord documents it, regardless of which side is being viewed. The 'bottom'
 * side then mirrors horizontally about the board's own centreline — as if the board
 * were physically flipped left-to-right like a page, which is what a builder does to
 * look at the solder side. Doing the mirror here means every other module (holes,
 * conductors, components) can work entirely in unmirrored board space and never think
 * about which side is being viewed.
 */
export function boardMmToScreenPx(
  p: Point2,
  board: Board,
  view: Viewport,
): { readonly x: number; readonly y: number } {
  const { widthMm } = boardSpanMm(board);
  const mirroredX = view.side === 'bottom' ? widthMm - p.x : p.x;
  return {
    x: (mirroredX - view.panXMm) * view.scale,
    y: (p.y - view.panYMm) * view.scale,
  };
}

/** Inverse of {@link boardMmToScreenPx}. */
export function screenPxToBoardMm(x: number, y: number, board: Board, view: Viewport): Point2 {
  const { widthMm } = boardSpanMm(board);
  const mirroredX = x / view.scale + view.panXMm;
  const boardY = y / view.scale + view.panYMm;
  const boardX = view.side === 'bottom' ? widthMm - mirroredX : mirroredX;
  return { x: boardX, y: boardY };
}

/** Screen-space position of a hole's centre. Handles side-mirroring internally. */
export function holeToScreen(
  c: HoleCoord,
  board: Board,
  view: Viewport,
): { readonly x: number; readonly y: number } {
  return boardMmToScreenPx(holeToMm(c, board), board, view);
}

/**
 * Nearest hole to a screen-space point, or undefined if that point falls outside the
 * board's col/row extent. Handles side-mirroring internally, so callers never need to
 * think about it — this is exactly the class of bug (mirroring) that makes someone
 * solder a board backwards.
 */
export function screenToHole(px: number, py: number, board: Board, view: Viewport): HoleCoord | undefined {
  const mm = screenPxToBoardMm(px, py, board, view);
  const col = Math.round(mm.x / board.pitch);
  const row = Math.round(mm.y / board.pitch);
  if (col < 0 || col >= board.cols || row < 0 || row >= board.rows) {
    return undefined;
  }
  return { col, row };
}

export interface HoleRange {
  readonly colMin: number;
  readonly colMax: number;
  readonly rowMin: number;
  readonly rowMax: number;
}

/**
 * Hole index range that might intersect the visible screen rect, expanded by one pad
 * radius so pads that are only partially on-screen at the viewport edge still get
 * drawn. `colMax < colMin` (or `rowMax < rowMin`) signals an empty range: nothing on
 * the board is visible.
 */
export function visibleHoleRange(board: Board, view: Viewport): HoleRange {
  const corners = [
    screenPxToBoardMm(0, 0, board, view),
    screenPxToBoardMm(view.widthPx, view.heightPx, board, view),
  ] as const;
  const minX = Math.min(corners[0].x, corners[1].x);
  const maxX = Math.max(corners[0].x, corners[1].x);
  const minY = Math.min(corners[0].y, corners[1].y);
  const maxY = Math.max(corners[0].y, corners[1].y);

  const padRadius = board.padDiameter / 2;

  const colMin = Math.max(0, Math.floor((minX - padRadius) / board.pitch));
  const colMax = Math.min(board.cols - 1, Math.ceil((maxX + padRadius) / board.pitch));
  const rowMin = Math.max(0, Math.floor((minY - padRadius) / board.pitch));
  const rowMax = Math.min(board.rows - 1, Math.ceil((maxY + padRadius) / board.pitch));

  return { colMin, colMax, rowMin, rowMax };
}
