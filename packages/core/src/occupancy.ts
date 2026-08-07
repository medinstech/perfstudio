/**
 * Occupancy index: what physically sits at each (hole, side).
 *
 * This is the counterpart to connectivity.ts, and the split between them is deliberate.
 * Connectivity answers "what is electrically joined" — a wire contacts only its two
 * endpoints. Occupancy answers "what is physically in the way" — that same wire runs
 * across every hole on its path, and the router may not lay a bare trace through them.
 *
 * Keeping the two apart is what lets connectivity stay clean (no meaningless one-node
 * nets for holes a wire merely crosses) while the router still knows the board is full
 * there.
 */

import type {
  ComponentId,
  Conductor,
  ConductorId,
  Footprint,
  HoleCoord,
  BoardSide,
  PerfDocument,
} from './model.js';
import { isCrossingBlocked } from './model.js';
import { allPinHoles, holeKey, holeToMm, transformOffset } from './geometry.js';
import type { FootprintLookup } from './connectivity.js';

export interface OccupyingPin {
  readonly componentId: ComponentId;
  readonly componentRef: string;
  readonly pin: string;
}

export interface OccupancyIndex {
  /** Conductors whose path runs across this hole on this side, contact or not. */
  conductorsAt(hole: HoleCoord, side: BoardSide): readonly ConductorId[];
  /** The component pin in this hole, if any. A pin occupies both sides. */
  pinAt(hole: HoleCoord): OccupyingPin | undefined;
  /**
   * True when a conductor that cannot be crossed already occupies this hole+side.
   * A router laying a solder trace or bare wire must treat these as walls; insulated
   * wire and top jumpers may pass over them.
   */
  isCopperBlocked(hole: HoleCoord, side: BoardSide): boolean;
  /** Component whose body covers this hole on the component side, if any. */
  bodyCovers(hole: HoleCoord): ComponentId | undefined;
  /** Every hole that has anything at all on it, for quick iteration. */
  occupiedHoles(): readonly HoleCoord[];
}

export function buildOccupancy(doc: PerfDocument, lookup: FootprintLookup): OccupancyIndex {
  const conductorsByNode = new Map<string, ConductorId[]>();
  const blockedNodes = new Set<string>();
  const pinsByHole = new Map<string, OccupyingPin>();
  const bodyByHole = new Map<string, ComponentId>();
  const holes = new Map<string, HoleCoord>();

  const node = (hole: HoleCoord, side: BoardSide): string => `${holeKey(hole)}@${side}`;
  const remember = (hole: HoleCoord): void => {
    const k = holeKey(hole);
    if (!holes.has(k)) holes.set(k, hole);
  };

  // --- Component pins and bodies. ---
  for (const component of doc.components) {
    const footprint = lookup(component.footprintId);
    if (!footprint) continue;

    for (const { pin, hole } of allPinHoles(component, footprint)) {
      remember(hole);
      pinsByHole.set(holeKey(hole), {
        componentId: component.id,
        componentRef: component.ref,
        pin: pin.number,
      });
    }

    for (const hole of bodyFootprintHoles(component, footprint, doc)) {
      remember(hole);
      bodyByHole.set(holeKey(hole), component.id);
    }
  }

  // --- Conductors: EVERY hole on the path is physically occupied, contact or not. ---
  for (const conductor of doc.conductors) {
    const blocks = isCrossingBlocked(conductor);
    for (const hole of conductor.path) {
      remember(hole);
      const k = node(hole, conductor.side);
      const list = conductorsByNode.get(k);
      if (list) list.push(conductor.id);
      else conductorsByNode.set(k, [conductor.id]);
      if (blocks) blockedNodes.add(k);
    }
  }

  const sortedHoles = [...holes.values()].sort((a, b) => a.col - b.col || a.row - b.row);

  return {
    conductorsAt: (hole, side) => conductorsByNode.get(node(hole, side)) ?? [],
    pinAt: (hole) => pinsByHole.get(holeKey(hole)),
    isCopperBlocked: (hole, side) => blockedNodes.has(node(hole, side)),
    bodyCovers: (hole) => bodyByHole.get(holeKey(hole)),
    occupiedHoles: () => sortedHoles,
  };
}

/**
 * Holes covered by a component's body outline on the component side. Uses the outline's
 * axis-aligned bounding box, which is what DRC's overlap check uses too — good enough
 * for deciding whether a top-side jumper would have to run underneath a part.
 */
function bodyFootprintHoles(
  component: { readonly anchor: HoleCoord; readonly rotation: 0 | 90 | 180 | 270; readonly mirrored: boolean },
  footprint: Footprint,
  doc: PerfDocument,
): HoleCoord[] {
  if (footprint.bodyOutline.length === 0) return [];

  const anchorMm = holeToMm(component.anchor, doc.board);
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const pt of footprint.bodyOutline) {
    const t = transformOffset(pt.x, pt.y, component.rotation, component.mirrored);
    const x = anchorMm.x + t.x;
    const y = anchorMm.y + t.y;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }

  const pitch = doc.board.pitch;
  const result: HoleCoord[] = [];
  for (let col = Math.ceil(minX / pitch); col <= Math.floor(maxX / pitch); col++) {
    for (let row = Math.ceil(minY / pitch); row <= Math.floor(maxY / pitch); row++) {
      if (col >= 0 && row >= 0 && col < doc.board.cols && row < doc.board.rows) {
        result.push({ col, row });
      }
    }
  }
  return result;
}

/** Conductor kinds a router may lay over occupied copper. */
export function canCrossCopper(kind: Conductor['kind']): boolean {
  return kind === 'insulated-wire' || kind === 'top-jumper';
}
