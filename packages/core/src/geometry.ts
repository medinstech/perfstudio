/**
 * Hole coordinate system and grid geometry.
 *
 * Everything here is pure and deterministic: no Date.now(), no Math.random(), no I/O.
 * This module is the bridge between the abstract {col,row} addressing used by the
 * document model (model.ts) and two concrete things a human or a renderer needs:
 *   - the spreadsheet-style HoleRef ("A1", "AC12") used by the soldering guide,
 *   - millimetre coordinates on the physical board.
 * It also carries the orthogonal-adjacency invariant that solder traces depend on
 * (model.ts ConductorBase.path doc comment, PLAN.md §4.6), and the pin-placement
 * math that turns a footprint + component transform into absolute hole positions.
 */

import type {
  Board,
  ComponentInstance,
  Footprint,
  FootprintPin,
  HoleCoord,
  HoleRef,
  Mm,
  Point2,
  Rotation,
} from './model.js';

// ---------------------------------------------------------------------------
// Hole reference <-> coordinate
// ---------------------------------------------------------------------------

/**
 * Matches a well-formed HoleRef: one or more uppercase letters (the column, in
 * spreadsheet/bijective-base-26 notation) followed by a 1-indexed row number with
 * no leading zero. Anchored on both ends so trailing/leading garbage is rejected.
 */
const HOLE_REF_PATTERN = /^([A-Z]+)([1-9][0-9]*)$/;

/**
 * Encodes a 0-indexed column as spreadsheet-style letters using bijective base-26:
 * 0 -> "A", 25 -> "Z", 26 -> "AA", 51 -> "AZ", 52 -> "BA", 701 -> "ZZ", 702 -> "AAA".
 *
 * This is NOT plain base-26 (there is no digit for "zero" in a letter system —
 * "AA" is not "0,0", it is the number after "Z"). The bijective form is: shift to
 * 1-indexed, then repeatedly take ((n-1) mod 26) as the next letter and continue
 * with floor((n-1)/26) until it reaches zero.
 */
function columnLettersFromIndex(col: number): string {
  if (!Number.isInteger(col) || col < 0) {
    throw new Error(`Invalid hole column index: ${col} (must be a non-negative integer).`);
  }
  let n = col + 1;
  let letters = '';
  while (n > 0) {
    n -= 1;
    const rem = n % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor(n / 26);
  }
  return letters;
}

/**
 * Inverse of {@link columnLettersFromIndex}. Assumes `letters` already matches
 * `[A-Z]+` (the caller validates via HOLE_REF_PATTERN before calling this).
 */
function columnIndexFromLetters(letters: string): number {
  let n = 0;
  for (const ch of letters) {
    n = n * 26 + (ch.charCodeAt(0) - 64);
  }
  return n - 1;
}

/** Converts a 0-indexed {col,row} hole coordinate to its human-facing HoleRef. */
export function coordToHoleRef(c: HoleCoord): HoleRef {
  if (!Number.isInteger(c.row) || c.row < 0) {
    throw new Error(`Invalid hole row index: ${c.row} (must be a non-negative integer).`);
  }
  return `${columnLettersFromIndex(c.col)}${c.row + 1}`;
}

/**
 * Parses a human-facing HoleRef ("A1", "AC12") back into a 0-indexed HoleCoord.
 * Throws a descriptive Error on anything that isn't exactly [uppercase letters]
 * followed by [a row number >= 1 with no leading zero] — e.g. "1A", "", "A0",
 * "A-1", "A1.5" are all rejected. Lowercase letters are also rejected: HoleRef is
 * a canonical, uppercase-only rendering, and this is its strict parser.
 */
export function holeRefToCoord(ref: HoleRef): HoleCoord {
  const match = HOLE_REF_PATTERN.exec(ref);
  if (!match) {
    throw new Error(
      `Malformed hole reference: ${JSON.stringify(ref)}. Expected uppercase column letters ` +
        `followed by a 1-indexed row number, e.g. "A1" or "AC12".`,
    );
  }
  // Guaranteed present: the pattern has exactly two capturing groups and matched.
  const letters = match[1];
  const digits = match[2];
  if (letters === undefined || digits === undefined) {
    throw new Error(`Malformed hole reference: ${JSON.stringify(ref)}.`);
  }
  const row = Number(digits) - 1;
  return { col: columnIndexFromLetters(letters), row };
}

// ---------------------------------------------------------------------------
// Keys
// ---------------------------------------------------------------------------

/**
 * Stable string key for a HoleCoord, suitable for Map/Set use. The comma
 * separator makes the encoding unambiguous for any pair of integers (positive,
 * negative or zero), since a plain integer's decimal form never contains one.
 */
export function holeKey(c: HoleCoord): string {
  return `${c.col},${c.row}`;
}

// ---------------------------------------------------------------------------
// Millimetre geometry
// ---------------------------------------------------------------------------

/**
 * Physical centre of a hole in board-space millimetres. Hole {col:0,row:0} sits at
 * the origin; x grows with col (rightward), y grows with row (downward), spaced by
 * board.pitch — matching the screen-like convention documented on HoleCoord.
 */
export function holeToMm(c: HoleCoord, board: Board): Point2 {
  return { x: c.col * board.pitch, y: c.row * board.pitch };
}

/** Whether a hole coordinate falls within the board's col/row extent. */
export function isInsideBoard(c: HoleCoord, board: Board): boolean {
  return c.col >= 0 && c.col < board.cols && c.row >= 0 && c.row < board.rows;
}

export interface RectMm {
  readonly x: Mm;
  readonly y: Mm;
  readonly width: Mm;
  readonly height: Mm;
}

/**
 * Physical size of the board in mm.
 *
 * THE CONVENTION, defined here once and nowhere else: the substrate extends half a
 * pitch beyond the outermost hole centres on every side. So the hole centres span
 * (cols - 1) * pitch, and the board measures cols * pitch. A 60-column board at
 * 2.54 mm pitch is 152.4 mm wide, which is how perfboard is actually sold and cut.
 *
 * This must not be recomputed anywhere else. The 1:1-scale printable PDF, the 3D
 * substrate mesh and the 2D renderer all have to agree to the last tenth of a
 * millimetre, because the user tapes the printout onto the physical board.
 */
export function boardSizeMm(board: Board): { readonly width: Mm; readonly height: Mm } {
  return { width: board.cols * board.pitch, height: board.rows * board.pitch };
}

/**
 * Board outline as a rect in the same mm space as {@link holeToMm}, where hole
 * {col:0,row:0} sits at the origin. The rect therefore starts at negative half-pitch.
 */
export function boardOutlineMm(board: Board): RectMm {
  const { width, height } = boardSizeMm(board);
  const half = board.pitch / 2;
  return { x: -half, y: -half, width, height };
}

/**
 * Distance from the FIRST hole centre to the LAST hole centre: (cols - 1) * pitch.
 *
 * This is NOT {@link boardSizeMm}, and confusing the two is a real hazard. Use this
 * one wherever holes must map onto holes — above all when mirroring the board to show
 * the solder side, where the reflection `x -> holeSpanMm - x` has to land hole 0
 * exactly on hole (cols-1). Reflecting about the physical board size instead would
 * shift the whole grid by half a pitch, and the user would solder the board backwards
 * without the view ever looking obviously wrong.
 *
 * Rule of thumb: holes and routing use holeSpanMm; substrate, printing and 3D use
 * boardSizeMm.
 */
export function holeSpanMm(board: Board): { readonly width: Mm; readonly height: Mm } {
  return {
    width: Math.max(0, board.cols - 1) * board.pitch,
    height: Math.max(0, board.rows - 1) * board.pitch,
  };
}

// ---------------------------------------------------------------------------
// Neighbours
// ---------------------------------------------------------------------------

/**
 * Orthogonal (4-connected) neighbours in deterministic compass order N, E, S, W,
 * clipped to the board so a hole on an edge or corner simply yields fewer results.
 */
export function neighbors4(c: HoleCoord, board: Board): HoleCoord[] {
  const candidates: readonly HoleCoord[] = [
    { col: c.col, row: c.row - 1 }, // N
    { col: c.col + 1, row: c.row }, // E
    { col: c.col, row: c.row + 1 }, // S
    { col: c.col - 1, row: c.row }, // W
  ];
  return candidates.filter((n) => isInsideBoard(n, board));
}

/**
 * All 8-connected neighbours in deterministic order: the 4 orthogonal directions
 * (N, E, S, W) first, then the 4 diagonals (NE, SE, SW, NW), clipped to the board.
 */
export function neighbors8(c: HoleCoord, board: Board): HoleCoord[] {
  const candidates: readonly HoleCoord[] = [
    { col: c.col, row: c.row - 1 }, // N
    { col: c.col + 1, row: c.row }, // E
    { col: c.col, row: c.row + 1 }, // S
    { col: c.col - 1, row: c.row }, // W
    { col: c.col + 1, row: c.row - 1 }, // NE
    { col: c.col + 1, row: c.row + 1 }, // SE
    { col: c.col - 1, row: c.row + 1 }, // SW
    { col: c.col - 1, row: c.row - 1 }, // NW
  ];
  return candidates.filter((n) => isInsideBoard(n, board));
}

// ---------------------------------------------------------------------------
// Distances and adjacency predicates
// ---------------------------------------------------------------------------

/** Grid (L1/taxicab) distance between two holes. */
export function manhattan(a: HoleCoord, b: HoleCoord): number {
  return Math.abs(a.col - b.col) + Math.abs(a.row - b.row);
}

/** Chebyshev (L-infinity) distance between two holes. */
export function chebyshev(a: HoleCoord, b: HoleCoord): number {
  return Math.max(Math.abs(a.col - b.col), Math.abs(a.row - b.row));
}

/** True iff `a` and `b` address the same hole. */
export function sameHole(a: HoleCoord, b: HoleCoord): boolean {
  return a.col === b.col && a.row === b.row;
}

/** True iff `a` and `b` are orthogonal (4-connected) neighbours. */
export function isAdjacent4(a: HoleCoord, b: HoleCoord): boolean {
  return manhattan(a, b) === 1;
}

/** True iff `a` and `b` are 8-connected (orthogonal or diagonal) neighbours. */
export function isAdjacent8(a: HoleCoord, b: HoleCoord): boolean {
  return chebyshev(a, b) === 1;
}

// ---------------------------------------------------------------------------
// Path validation and measurement
// ---------------------------------------------------------------------------

export type OrthogonalChainResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly index: number; readonly reason: string };

/**
 * Validates the solder-trace path invariant from model.ts: consecutive holes must
 * be 4-neighbours (solder cannot reliably span a diagonal gap — PLAN.md §4.6).
 * Also rejects paths shorter than 2 holes and paths that revisit a hole (a trace
 * must not loop back on itself). `index` points at the offending element: for a
 * non-adjacent step or a revisit, that's the second hole of the bad pair; for an
 * undersized path, it's 0.
 */
export function validateOrthogonalChain(path: readonly HoleCoord[]): OrthogonalChainResult {
  if (path.length < 2) {
    return {
      ok: false,
      index: 0,
      reason: `A trace path must contain at least 2 holes (got ${path.length}).`,
    };
  }

  const first = path[0];
  if (first === undefined) {
    // Unreachable given the length check above; keeps noUncheckedIndexedAccess happy.
    return { ok: false, index: 0, reason: 'Path is empty.' };
  }

  const seen = new Set<string>([holeKey(first)]);

  for (let i = 1; i < path.length; i++) {
    const prev = path[i - 1];
    const cur = path[i];
    if (prev === undefined || cur === undefined) {
      // Unreachable: i-1 and i are both within [0, path.length) by the loop bound.
      return { ok: false, index: i, reason: 'Path element is missing.' };
    }

    if (!isAdjacent4(prev, cur)) {
      return {
        ok: false,
        index: i,
        reason:
          `Hole at index ${i} (${holeKey(cur)}) is not 4-adjacent (orthogonal) to the ` +
          `previous hole (${holeKey(prev)}); solder traces cannot span a diagonal gap.`,
      };
    }

    const key = holeKey(cur);
    if (seen.has(key)) {
      return {
        ok: false,
        index: i,
        reason: `Hole at index ${i} (${key}) revisits a hole already used earlier in the path.`,
      };
    }
    seen.add(key);
  }

  return { ok: true };
}

/** Sum of Euclidean segment lengths along a path, in mm. */
export function pathLengthMm(path: readonly HoleCoord[], board: Board): Mm {
  let total = 0;
  for (let i = 1; i < path.length; i++) {
    const prev = path[i - 1];
    const cur = path[i];
    if (prev === undefined || cur === undefined) {
      // Unreachable: i-1 and i are both within [0, path.length) by the loop bound.
      continue;
    }
    const p1 = holeToMm(prev, board);
    const p2 = holeToMm(cur, board);
    total += Math.hypot(p2.x - p1.x, p2.y - p1.y);
  }
  return total;
}

// ---------------------------------------------------------------------------
// Component/footprint placement
// ---------------------------------------------------------------------------

/**
 * Applies a component's placement transform to a footprint pin's grid offset.
 * Coordinate system is screen-like: col -> +x (right), row -> +y (down).
 *
 * Order matters and matches how a physical part is placed: it is first flipped
 * over (mirrored about the vertical axis through the anchor), THEN rotated.
 *   - Mirror: (x, y) -> (-x, y).
 *   - Rotation is clockwise (as drawn on screen, y-down): each 90-degree step maps
 *     (x, y) -> (-y, x). Check: (1, 0) "right" -> (0, 1) "down", as expected for a
 *     clockwise turn in a y-down system.
 */
export function transformPinOffset(
  dCol: number,
  dRow: number,
  rotation: Rotation,
  mirrored: boolean,
): { dCol: number; dRow: number } {
  const p = transformOffset(dCol, dRow, rotation, mirrored);
  return { dCol: p.x, dRow: p.y };
}

/**
 * The same placement transform as {@link transformPinOffset}, but unit-agnostic.
 *
 * A component's placement rotates its pin offsets (grid steps) and its body outline
 * (millimetres) by exactly the same rule, so both go through this one function. Keeping
 * a single implementation is the point: if the renderer's idea of "rotated" ever drifts
 * from the connectivity engine's, parts would be drawn in one place and wired in
 * another — a discrepancy nothing would flag until a board came back wrong.
 */
export function transformOffset(
  x0: number,
  y0: number,
  rotation: Rotation,
  mirrored: boolean,
): { x: number; y: number } {
  let x = mirrored ? -x0 : x0;
  let y = y0;

  const steps = (rotation / 90) % 4;
  for (let i = 0; i < steps; i++) {
    const nx = -y;
    const ny = x;
    x = nx;
    y = ny;
  }

  // Negating 0 produces -0, which is numerically harmless (it behaves like 0 in every
  // arithmetic and string context) but trips up strict/deep-equality assertions.
  // Normalize it away so callers never observe it.
  return { x: x + 0, y: y + 0 };
}

/**
 * Absolute hole a given footprint pin lands on, once the component's anchor,
 * rotation and mirroring are applied. Returns undefined if the footprint has no
 * pin with that number.
 */
export function pinHole(
  component: ComponentInstance,
  footprint: Footprint,
  pinNumber: string,
): HoleCoord | undefined {
  const pin = footprint.pins.find((p) => p.number === pinNumber);
  if (pin === undefined) {
    return undefined;
  }
  const off = transformPinOffset(pin.dCol, pin.dRow, component.rotation, component.mirrored);
  return { col: component.anchor.col + off.dCol, row: component.anchor.row + off.dRow };
}

/** Absolute holes for every pin of a footprint, placed by a component instance. */
export function allPinHoles(
  component: ComponentInstance,
  footprint: Footprint,
): Array<{ pin: FootprintPin; hole: HoleCoord }> {
  return footprint.pins.map((pin) => {
    const off = transformPinOffset(pin.dCol, pin.dRow, component.rotation, component.mirrored);
    const hole: HoleCoord = {
      col: component.anchor.col + off.dCol,
      row: component.anchor.row + off.dRow,
    };
    return { pin, hole };
  });
}
