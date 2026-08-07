/**
 * Component drawing: body outline polygon, pin markers, ref label.
 *
 * The mirror-then-rotate placement transform comes from core (`transformOffset`), so
 * a part is drawn exactly where the connectivity engine believes its pins are. If those
 * two ever disagreed, components would be drawn in one place and wired in another, and
 * nothing would flag it until a physical board came back wrong.
 */

import type { Board, ComponentInstance, Footprint, HoleCoord, Point2 } from '@perfstudio/core';
import { transformOffset } from '@perfstudio/core';

import type { Theme } from './theme.js';
import type { Viewport } from './transform.js';
import { boardMmToScreenPx, holeToMm, holeToScreen } from './transform.js';

/** Below this pixels-per-mm scale, ref labels are too small to read and are skipped. */
const LABEL_MIN_SCALE_PX_PER_MM = 3.5;

function pinAbsoluteHole(component: ComponentInstance, dCol: number, dRow: number): HoleCoord {
  const off = transformOffset(dCol, dRow, component.rotation, component.mirrored);
  return { col: component.anchor.col + off.x, row: component.anchor.row + off.y };
}

/** Body outline point (mm, relative to anchor) transformed into absolute board-space mm. */
function bodyPointToBoardMm(pt: Point2, component: ComponentInstance, board: Board): Point2 {
  const off = transformOffset(pt.x, pt.y, component.rotation, component.mirrored);
  const anchorMm = holeToMm(component.anchor, board);
  return { x: anchorMm.x + off.x, y: anchorMm.y + off.y };
}

export function drawComponent(
  ctx: CanvasRenderingContext2D,
  component: ComponentInstance,
  footprint: Footprint,
  board: Board,
  view: Viewport,
  theme: Theme,
): void {
  // Body outline.
  if (footprint.bodyOutline.length > 0) {
    ctx.beginPath();
    footprint.bodyOutline.forEach((pt, i) => {
      const mm = bodyPointToBoardMm(pt, component, board);
      const p = boardMmToScreenPx(mm, board, view);
      if (i === 0) {
        ctx.moveTo(p.x, p.y);
      } else {
        ctx.lineTo(p.x, p.y);
      }
    });
    ctx.closePath();
    ctx.fillStyle = theme.componentBody;
    ctx.fill();
    ctx.strokeStyle = theme.componentBodyStroke;
    ctx.lineWidth = Math.max(1, view.scale * 0.08);
    ctx.stroke();
  }

  // Pin markers.
  const pinRadiusPx = Math.max(1, view.scale * 0.3);
  for (const pin of footprint.pins) {
    const hole = pinAbsoluteHole(component, pin.dCol, pin.dRow);
    const p = holeToScreen(hole, board, view);
    ctx.beginPath();
    ctx.arc(p.x, p.y, pinRadiusPx, 0, Math.PI * 2);
    ctx.fillStyle = theme.pinMarker;
    ctx.fill();
  }

  // Ref label — skipped below the legibility threshold.
  if (view.scale >= LABEL_MIN_SCALE_PX_PER_MM) {
    const anchorPx = holeToScreen(component.anchor, board, view);
    ctx.fillStyle = theme.label;
    ctx.font = `${Math.max(8, view.scale * 1.6)}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(component.ref, anchorPx.x, anchorPx.y - pinRadiusPx - 2);
  }
}
