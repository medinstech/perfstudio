/**
 * Parametric through-hole footprint library.
 *
 * PerfStudio deliberately generates footprints (and, from the same BodySpec, the
 * matching 3D body) from a handful of numeric parameters rather than shipping a
 * mesh/footprint library: zero assets, guaranteed agreement between the 2D
 * footprint and the 3D body, and no share-alike asset licence to inherit
 * (PLAN.md D6).
 *
 * Conventions used throughout this file:
 *  - Every pin offset (dCol/dRow) is an integer grid step on the standard 2.54 mm
 *    perfboard pitch (model.ts STANDARD_PITCH_MM), regardless of the pitch of
 *    whatever board a component eventually lands on. bodyOutline and bodyHeight
 *    are always millimetres.
 *  - ANCHOR CONVENTION: pin "1" always sits at grid offset (0,0) - for two-lead
 *    parts, inline parts (TO-92, TO-220, headers, ...) and DIP packages alike.
 *    This keeps the anchor a real, physical pin in every case rather than an
 *    arbitrary geometric centre.
 *  - bodyOutline is a closed polygon (vertices only, no repeated closing point)
 *    in mm relative to the anchor. It doubles as the courtyard for overlap DRC,
 *    so every generator guarantees its bounding box contains every pin position
 *    plus a clearance margin (COURTYARD_MARGIN_MM, half a grid step) - even when
 *    the physical package is narrower than the pins fanned out to reach the grid
 *    (see to92Footprint).
 *  - Physical dimensions (body length/diameter/height, DIP row spacing, etc.) are
 *    realistic, commonly-seen values for each part family, not a transcription of
 *    any single manufacturer's datasheet: this library trades datasheet-exact
 *    dimensions for parts that always land cleanly on the 2.54 mm grid.
 *  - `polarized` means "swapping the leads changes the circuit's behaviour": true
 *    for diodes, electrolytics and LEDs; false for everything else, including
 *    parts (DIPs, transistors, pots) whose pins are non-interchangeable for other
 *    reasons (a fixed pinout, not electrical polarity).
 *  - Pure and deterministic: no I/O, no Date.now(), no Math.random(). Every
 *    function here computes its result solely from its arguments.
 */

import { STANDARD_PITCH_MM } from './model.js';
import type { Footprint, FootprintPin, Mm, Point2 } from './model.js';

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/** Default courtyard clearance beyond a footprint's pins/body: half a grid step. */
const COURTYARD_MARGIN_MM: Mm = STANDARD_PITCH_MM / 2;

/** Builds a FootprintPin, omitting `name` entirely when not supplied. */
function makePin(number: string, dCol: number, dRow: number, name?: string): FootprintPin {
  return name === undefined ? { number, dCol, dRow } : { number, dCol, dRow, name };
}

/** Millimetre position of a grid offset, on the standard (not board-specific) pitch. */
function pinMm(dCol: number, dRow: number): Point2 {
  return { x: dCol * STANDARD_PITCH_MM, y: dRow * STANDARD_PITCH_MM };
}

/** Millimetre positions of every pin, in the same order. */
function toMm(pins: readonly FootprintPin[]): Point2[] {
  return pins.map((p) => pinMm(p.dCol, p.dRow));
}

/** Axis-aligned bounding box of a set of mm points. Empty input yields a degenerate box at the origin. */
function pinsBoundingBox(pinsMm: readonly Point2[]): { minX: Mm; maxX: Mm; minY: Mm; maxY: Mm } {
  const first = pinsMm[0];
  if (first === undefined) {
    return { minX: 0, maxX: 0, minY: 0, maxY: 0 };
  }
  let minX = first.x;
  let maxX = first.x;
  let minY = first.y;
  let maxY = first.y;
  for (const p of pinsMm) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, maxX, minY, maxY };
}

/**
 * Rectangular courtyard outline (4 vertices, closed implicitly), centred on the
 * pins' bounding box. Never smaller than `minWidthMm` x `minHeightMm`, so it
 * always covers both the physical body and the full pin span (relevant when
 * leads are fanned out wider than the body to reach the grid), then adds
 * `marginMm` clearance on every side.
 */
function rectOutline(
  pinsMm: readonly Point2[],
  minWidthMm: Mm,
  minHeightMm: Mm,
  marginMm: Mm,
): Point2[] {
  const { minX, maxX, minY, maxY } = pinsBoundingBox(pinsMm);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const halfW = Math.max(maxX - minX, minWidthMm) / 2 + marginMm;
  const halfH = Math.max(maxY - minY, minHeightMm) / 2 + marginMm;
  return [
    { x: cx - halfW, y: cy - halfH },
    { x: cx + halfW, y: cy - halfH },
    { x: cx + halfW, y: cy + halfH },
    { x: cx - halfW, y: cy + halfH },
  ];
}

/**
 * Circular courtyard outline approximated with 24 vertices (divisible by 4, so
 * the cardinal points land exactly on the bounding box edges). Centred on the
 * pins' bounding box, radius large enough to cover both `minDiameterMm` and the
 * farthest pin, plus `marginMm` clearance.
 */
function circleOutline(
  pinsMm: readonly Point2[],
  minDiameterMm: Mm,
  marginMm: Mm,
  sides = 24,
): Point2[] {
  const { minX, maxX, minY, maxY } = pinsBoundingBox(pinsMm);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  let maxDist = minDiameterMm / 2;
  for (const p of pinsMm) {
    const d = Math.hypot(p.x - cx, p.y - cy);
    if (d > maxDist) maxDist = d;
  }
  const r = maxDist + marginMm;
  const pts: Point2[] = [];
  for (let i = 0; i < sides; i++) {
    const theta = (2 * Math.PI * i) / sides;
    pts.push({ x: cx + r * Math.cos(theta), y: cy + r * Math.sin(theta) });
  }
  return pts;
}

/** Compact, deterministic token for a value in an auto-generated id/name: "5", "6.3". */
function formatMmToken(value: Mm): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

// ---------------------------------------------------------------------------
// Axial two-lead parts: resistors, DO-41 / DO-35 diodes
// ---------------------------------------------------------------------------

export interface AxialFootprintParams {
  /** Distance between the two leads, in grid holes (typically 3, 4, 5 or 6). */
  readonly spanHoles: number;
  readonly bodyLengthMm: Mm;
  readonly bodyDiameterMm: Mm;
  readonly leadDiameterMm?: Mm;
  /** True for diodes (direction matters); false for resistors. Defaults to false. */
  readonly polarized?: boolean;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Axial two-lead part lying flat on the board: resistors, DO-41/DO-35 diodes.
 * Pin 1 is the anchor at (0,0); pin 2 sits `spanHoles` grid steps to the right on
 * the same row. The body is centred between the leads and the courtyard covers
 * the full lead span even when it is longer than the body itself.
 */
export function axialFootprint(params: AxialFootprintParams): Footprint {
  const { spanHoles, bodyLengthMm, bodyDiameterMm } = params;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const polarized = params.polarized ?? false;
  const pins = [makePin('1', 0, 0), makePin('2', spanHoles, 0)];
  const outline = rectOutline(toMm(pins), bodyLengthMm, bodyDiameterMm, COURTYARD_MARGIN_MM);
  const id = params.id ?? `axial-${spanHoles}h-${formatMmToken(bodyLengthMm)}x${formatMmToken(bodyDiameterMm)}`;
  const name = params.name ?? `Axial (${spanHoles}-hole span, ${bodyLengthMm}x${bodyDiameterMm} mm body)`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: bodyDiameterMm,
    body: { archetype: 'axial-cylinder', dims: { length: bodyLengthMm, diameter: bodyDiameterMm } },
    leadDiameter,
    polarized,
  };
}

// ---------------------------------------------------------------------------
// Radial electrolytic capacitors
// ---------------------------------------------------------------------------

export interface RadialElectrolyticParams {
  /** Lead pitch, in grid holes: 2 (5.08 mm) or 3 (7.62 mm). */
  readonly pitchHoles: 2 | 3;
  readonly canDiameterMm: Mm;
  readonly canHeightMm: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Radial electrolytic capacitor: a round can standing upright on two leads.
 * Pin 1 (the anchor, "+") sits at (0,0); pin 2 ("-") sits `pitchHoles` grid
 * steps to the right.
 */
export function radialElectrolyticFootprint(params: RadialElectrolyticParams): Footprint {
  const { pitchHoles, canDiameterMm, canHeightMm } = params;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const pins = [makePin('1', 0, 0, '+'), makePin('2', pitchHoles, 0, '-')];
  const outline = circleOutline(toMm(pins), canDiameterMm, COURTYARD_MARGIN_MM);
  const id = params.id ?? `c-elec-d${formatMmToken(canDiameterMm)}-p${pitchHoles}`;
  const name = params.name ?? `Electrolytic capacitor, ${canDiameterMm} mm dia, ${pitchHoles}-hole pitch`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: canHeightMm,
    body: { archetype: 'radial-electrolytic', dims: { diameter: canDiameterMm, height: canHeightMm } },
    leadDiameter,
    polarized: true,
  };
}

// ---------------------------------------------------------------------------
// Disc ceramic capacitors
// ---------------------------------------------------------------------------

export interface DiscCeramicParams {
  readonly pitchHoles: number;
  readonly bodyDiameterMm: Mm;
  /** Disc thickness (the "thin" dimension, perpendicular to the lead pitch). */
  readonly bodyThicknessMm?: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Disc ceramic capacitor: a flat round disc standing on edge, two leads down.
 * Pin 1 is the anchor at (0,0); pin 2 sits `pitchHoles` grid steps to the right.
 */
export function discCeramicFootprint(params: DiscCeramicParams): Footprint {
  const { pitchHoles, bodyDiameterMm } = params;
  const bodyThickness = params.bodyThicknessMm ?? 2.5;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const pins = [makePin('1', 0, 0), makePin('2', pitchHoles, 0)];
  const outline = rectOutline(toMm(pins), bodyDiameterMm, bodyThickness, COURTYARD_MARGIN_MM);
  const id = params.id ?? `c-disc-d${formatMmToken(bodyDiameterMm)}-p${pitchHoles}`;
  const name = params.name ?? `Disc ceramic capacitor, ${bodyDiameterMm} mm dia, ${pitchHoles}-hole pitch`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: bodyDiameterMm,
    body: { archetype: 'disc-ceramic', dims: { diameter: bodyDiameterMm, thickness: bodyThickness } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Boxed film capacitors
// ---------------------------------------------------------------------------

export interface BoxFilmParams {
  readonly pitchHoles: number;
  readonly bodyLengthMm: Mm;
  readonly bodyWidthMm: Mm;
  readonly bodyHeightMm: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Boxed film capacitor: a rectangular block, two leads down from the bottom.
 * Pin 1 is the anchor at (0,0); pin 2 sits `pitchHoles` grid steps to the right.
 */
export function boxFilmCapacitorFootprint(params: BoxFilmParams): Footprint {
  const { pitchHoles, bodyLengthMm, bodyWidthMm, bodyHeightMm } = params;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const pins = [makePin('1', 0, 0), makePin('2', pitchHoles, 0)];
  const outline = rectOutline(toMm(pins), bodyLengthMm, bodyWidthMm, COURTYARD_MARGIN_MM);
  const id =
    params.id ?? `c-film-${formatMmToken(bodyLengthMm)}x${formatMmToken(bodyWidthMm)}-p${pitchHoles}`;
  const name = params.name ?? `Film capacitor, ${bodyLengthMm}x${bodyWidthMm} mm, ${pitchHoles}-hole pitch`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: bodyHeightMm,
    body: { archetype: 'box-film', dims: { length: bodyLengthMm, width: bodyWidthMm, height: bodyHeightMm } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// DIP packages
// ---------------------------------------------------------------------------

export interface DipParams {
  /** Total pin count; must be even and >= 4 (8, 14, 16, 18, 20, 28, 40, ...). */
  readonly pinCount: number;
  /** 0.6" (6-hole) wide body instead of the standard 0.3" (3-hole) narrow body. */
  readonly wide?: boolean;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * DIP package: two rows of pins, `pinCount / 2` per side, rows 3 grid holes
 * apart (0.3" narrow, the standard) or 6 holes apart (0.6" wide, `wide: true`).
 *
 * Pin 1 is the anchor at (0,0), at the top of the left column. Numbering runs
 * counter-clockwise as on a real DIP viewed from above with pin 1 at the top
 * left: 1..pinCount/2 go DOWN the left column (dCol 0), then pinCount/2+1..
 * pinCount go back UP the right column (dCol = row spacing), so the highest
 * pin number ends up beside pin 1 at the top of the package, same as the real
 * part's pin-1 notch marks both ends of that row.
 */
export function dipFootprint(params: DipParams): Footprint {
  const { pinCount } = params;
  if (!Number.isInteger(pinCount) || pinCount < 4 || pinCount % 2 !== 0) {
    throw new Error(`dipFootprint: pinCount must be an even integer >= 4 (got ${pinCount}).`);
  }
  const perSide = pinCount / 2;
  const wide = params.wide ?? false;
  const rowSpacingHoles = wide ? 6 : 3;
  const leadDiameter = params.leadDiameterMm ?? 0.46;

  const pins: FootprintPin[] = [];
  for (let i = 0; i < perSide; i++) {
    pins.push(makePin(String(i + 1), 0, i));
  }
  for (let i = 0; i < perSide; i++) {
    const number = String(perSide + i + 1);
    const dRow = perSide - 1 - i;
    pins.push(makePin(number, rowSpacingHoles, dRow));
  }

  const outline = rectOutline(toMm(pins), 0, 0, COURTYARD_MARGIN_MM);
  const rowSpacingMm = rowSpacingHoles * STANDARD_PITCH_MM;
  // Approximate realistic package dims: body is a little narrower than the row
  // spacing and a little longer than the pin column span (the plastic overhangs
  // the outermost pins at each end). Not tied to a specific datasheet.
  const bodyLengthMm = (perSide - 1) * STANDARD_PITCH_MM + 2.2;
  const bodyWidthMm = rowSpacingMm - 0.2;

  const id = params.id ?? `dip-${pinCount}${wide ? '-wide' : ''}`;
  const name = params.name ?? `DIP-${pinCount}${wide ? ' (0.6" wide)' : ''}`;

  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 5,
    body: { archetype: 'dip', dims: { length: bodyLengthMm, width: bodyWidthMm, rowSpacing: rowSpacingMm } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// TO-92
// ---------------------------------------------------------------------------

export interface To92Params {
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * TO-92 transistor package, inline-on-grid variant: 3 pins in a row, one hole
 * apart. Real TO-92 leads are much closer together at the body and fan out to
 * reach this pitch; the courtyard therefore covers the full fanned-out pin span,
 * not just the (narrower) physical body, since that is what actually needs
 * clearance on the board.
 */
export function to92Footprint(params: To92Params = {}): Footprint {
  const leadDiameter = params.leadDiameterMm ?? 0.45;
  const pins = [makePin('1', 0, 0), makePin('2', 1, 0), makePin('3', 2, 0)];
  const outline = rectOutline(toMm(pins), 4.5, 3.7, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'to92';
  const name = params.name ?? 'TO-92 (inline, on-grid)';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 5.2,
    body: { archetype: 'to92', dims: { width: 4.5, depth: 3.7 } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// TO-220
// ---------------------------------------------------------------------------

export interface To220Params {
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * TO-220 power package: 3 pins in a row at 2.54 mm pitch, tall body, plus the
 * metal mounting tab above it (tabHeight / tabHoleDiameter in `body.dims`).
 */
export function to220Footprint(params: To220Params = {}): Footprint {
  const leadDiameter = params.leadDiameterMm ?? 0.7;
  const pins = [makePin('1', 0, 0), makePin('2', 1, 0), makePin('3', 2, 0)];
  const outline = rectOutline(toMm(pins), 10.0, 4.6, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'to220';
  const name = params.name ?? 'TO-220';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 20,
    body: {
      archetype: 'to220',
      dims: { width: 10.0, depth: 4.6, tabHeight: 3.5, tabHoleDiameter: 3.4 },
    },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// LEDs
// ---------------------------------------------------------------------------

export type LedDiameterMm = 3 | 5 | 10;

/** Typical total height above the board for each standard round LED size. */
const LED_HEIGHT_BY_DIAMETER_MM: Readonly<Record<LedDiameterMm, Mm>> = { 3: 4.8, 5: 8.6, 10: 13.0 };

export interface LedParams {
  readonly diameterMm: LedDiameterMm;
  readonly bodyHeightMm?: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Round LED: 3, 5 or 10 mm. Pin 1 (the anchor, anode "A") sits at (0,0); pin 2
 * (cathode "K", the flat-side/shorter lead) sits one hole to the right.
 */
export function ledFootprint(params: LedParams): Footprint {
  const { diameterMm } = params;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const bodyHeight = params.bodyHeightMm ?? LED_HEIGHT_BY_DIAMETER_MM[diameterMm];
  const pins = [makePin('1', 0, 0, 'A'), makePin('2', 1, 0, 'K')];
  const outline = circleOutline(toMm(pins), diameterMm, COURTYARD_MARGIN_MM);
  const id = params.id ?? `led-${diameterMm}mm`;
  const name = params.name ?? `LED, ${diameterMm} mm round`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight,
    body: { archetype: 'led-round', dims: { diameter: diameterMm } },
    leadDiameter,
    polarized: true,
  };
}

// ---------------------------------------------------------------------------
// Pin headers
// ---------------------------------------------------------------------------

export interface PinHeaderParams {
  readonly rows: 1 | 2;
  readonly cols: number;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Pin header, single or dual row, 2.54 mm pitch. Pin 1 is the anchor at (0,0).
 *
 * 1xN: pins run left to right, 1, 2, 3, ... N.
 * 2xN: the standard zig-zag numbering used by IDC/box headers (e.g. Raspberry
 * Pi GPIO): column-major, so pin 1 and pin 2 are the top/bottom pair in the
 * first column, pin 3 and pin 4 the next column, and so on.
 */
export function pinHeaderFootprint(params: PinHeaderParams): Footprint {
  const { rows, cols } = params;
  if (!Number.isInteger(cols) || cols < 1) {
    throw new Error(`pinHeaderFootprint: cols must be a positive integer (got ${cols}).`);
  }
  const leadDiameter = params.leadDiameterMm ?? 0.64;
  const pins: FootprintPin[] = [];
  if (rows === 1) {
    for (let i = 0; i < cols; i++) {
      pins.push(makePin(String(i + 1), i, 0));
    }
  } else {
    for (let i = 0; i < cols; i++) {
      pins.push(makePin(String(2 * i + 1), i, 0));
      pins.push(makePin(String(2 * i + 2), i, 1));
    }
  }
  const outline = rectOutline(toMm(pins), 0, 0, COURTYARD_MARGIN_MM);
  const id = params.id ?? `hdr-${rows}x${cols}`;
  const name = params.name ?? `Pin header, ${rows}x${cols}`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 8.5,
    body: {
      archetype: 'pin-header',
      dims: {
        length: (cols - 1) * STANDARD_PITCH_MM,
        width: (rows - 1) * STANDARD_PITCH_MM,
        height: 8.5,
      },
    },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Screw terminals
// ---------------------------------------------------------------------------

export interface ScrewTerminalParams {
  /** Number of ways (poles); must be an integer >= 2. */
  readonly ways: number;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Screw terminal block, 5.08 mm pitch (2 grid holes per way). Pin 1 is the
 * anchor at (0,0).
 */
export function screwTerminalFootprint(params: ScrewTerminalParams): Footprint {
  const { ways } = params;
  if (!Number.isInteger(ways) || ways < 2) {
    throw new Error(`screwTerminalFootprint: ways must be an integer >= 2 (got ${ways}).`);
  }
  const leadDiameter = params.leadDiameterMm ?? 0.8;
  const pins: FootprintPin[] = [];
  for (let i = 0; i < ways; i++) {
    pins.push(makePin(String(i + 1), i * 2, 0));
  }
  const bodyLengthMm = ways * 5.08 + 1.5;
  const bodyWidthMm = 8.0;
  const outline = rectOutline(toMm(pins), bodyLengthMm, bodyWidthMm, COURTYARD_MARGIN_MM);
  const id = params.id ?? `screw-terminal-${ways}`;
  const name = params.name ?? `Screw terminal, ${ways}-way, 5.08 mm pitch`;
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 10,
    body: { archetype: 'screw-terminal', dims: { length: bodyLengthMm, width: bodyWidthMm, height: 10 } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Potentiometer
// ---------------------------------------------------------------------------

export interface PotentiometerParams {
  readonly bodyDiameterMm?: Mm;
  readonly bodyHeightMm?: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Potentiometer, perfboard-friendly inline variant: 3 pins in a row, one hole
 * apart, with the round body centred over them (real panel pots have pins that
 * don't sit on a clean 2.54 mm grid; this is the on-grid approximation, same
 * philosophy as to92Footprint).
 */
export function potentiometerFootprint(params: PotentiometerParams = {}): Footprint {
  const bodyDiameter = params.bodyDiameterMm ?? 16;
  const bodyHeight = params.bodyHeightMm ?? 10;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const pins = [makePin('1', 0, 0), makePin('2', 1, 0), makePin('3', 2, 0)];
  const outline = circleOutline(toMm(pins), bodyDiameter, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'pot-3';
  const name = params.name ?? 'Potentiometer, 3-pin inline';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight,
    body: { archetype: 'potentiometer', dims: { diameter: bodyDiameter, height: bodyHeight } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Tactile switch
// ---------------------------------------------------------------------------

export interface TactileSwitchParams {
  readonly bodySizeMm?: Mm;
  readonly bodyHeightMm?: Mm;
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Tactile switch, perfboard-friendly 4-pin variant: pins at the corners of a
 * 2-hole x 1-hole rectangle (pins 1/2 on the left, both the same node; pins
 * 3/4 on the right, both the same node) - the on-grid approximation of the
 * common 4-leg tactile switch.
 */
export function tactileSwitchFootprint(params: TactileSwitchParams = {}): Footprint {
  const bodySize = params.bodySizeMm ?? 6.0;
  const bodyHeight = params.bodyHeightMm ?? 4.3;
  const leadDiameter = params.leadDiameterMm ?? 0.5;
  const pins = [makePin('1', 0, 0), makePin('2', 0, 1), makePin('3', 2, 0), makePin('4', 2, 1)];
  const outline = rectOutline(toMm(pins), bodySize, bodySize, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'sw-tactile';
  const name = params.name ?? 'Tactile switch, 4-pin';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight,
    body: { archetype: 'tactile-switch', dims: { width: bodySize, depth: bodySize } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// HC-49 crystal
// ---------------------------------------------------------------------------

export interface CrystalHc49Params {
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * HC-49/U crystal, mounted standing upright on 2 leads, 2 holes apart (5.08 mm;
 * the real lead pitch is 4.88 mm, close enough that this is the standard
 * perfboard approximation).
 */
export function crystalHc49Footprint(params: CrystalHc49Params = {}): Footprint {
  const leadDiameter = params.leadDiameterMm ?? 0.45;
  const pins = [makePin('1', 0, 0), makePin('2', 2, 0)];
  const outline = rectOutline(toMm(pins), 4.65, 3.5, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'xtal-hc49';
  const name = params.name ?? 'Crystal, HC-49/U';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 13.46,
    body: { archetype: 'crystal-hc49', dims: { width: 4.65, depth: 3.5 } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Small relay
// ---------------------------------------------------------------------------

export interface RelayParams {
  readonly leadDiameterMm?: Mm;
  readonly id?: string;
  readonly name?: string;
}

/**
 * Small SPDT relay (e.g. Songle SRD-05VDC-SL-C style), on-grid approximation:
 * 2 coil pins in one column (2 holes apart), 3 switch pins (NO/COM/NC) in a
 * second column 5 holes over (1 hole apart from each other). Real relay
 * pinouts are rarely a clean 2.54 mm grid; verify against your specific part's
 * datasheet before relying on this for anything but layout planning.
 */
export function relayFootprint(params: RelayParams = {}): Footprint {
  const leadDiameter = params.leadDiameterMm ?? 0.6;
  const pins = [
    makePin('1', 0, 0), // coil
    makePin('2', 0, 2), // coil
    makePin('3', 5, 0), // NO
    makePin('4', 5, 1), // COM
    makePin('5', 5, 2), // NC
  ];
  const outline = rectOutline(toMm(pins), 19, 15, COURTYARD_MARGIN_MM);
  const id = params.id ?? 'relay-spdt';
  const name = params.name ?? 'Small SPDT relay';
  return {
    id,
    name,
    pins,
    bodyOutline: outline,
    bodyHeight: 15,
    body: { archetype: 'relay-box', dims: { length: 19, width: 15 } },
    leadDiameter,
    polarized: false,
  };
}

// ---------------------------------------------------------------------------
// Standard registry
// ---------------------------------------------------------------------------

function buildStandardFootprints(): ReadonlyMap<string, Footprint> {
  const list: Footprint[] = [];

  // Resistors: axial, 4 standard lead spans.
  const resistorSpans: ReadonlyArray<{ span: number; length: Mm; diameter: Mm }> = [
    { span: 3, length: 5.0, diameter: 2.0 },
    { span: 4, length: 6.3, diameter: 2.3 },
    { span: 5, length: 6.3, diameter: 2.5 },
    { span: 6, length: 9.0, diameter: 3.6 },
  ];
  for (const r of resistorSpans) {
    list.push(
      axialFootprint({
        spanHoles: r.span,
        bodyLengthMm: r.length,
        bodyDiameterMm: r.diameter,
        id: `r-axial-${r.span}`,
        name: `Resistor (axial, ${r.span}-hole span)`,
      }),
    );
  }

  // Diodes: DO-41 / DO-35, axial and polarized.
  list.push(
    axialFootprint({
      spanHoles: 4,
      bodyLengthMm: 5.2,
      bodyDiameterMm: 2.7,
      polarized: true,
      id: 'd-do41',
      name: 'Diode, DO-41',
    }),
    axialFootprint({
      spanHoles: 3,
      bodyLengthMm: 3.5,
      bodyDiameterMm: 2.0,
      polarized: true,
      id: 'd-do35',
      name: 'Diode, DO-35',
    }),
  );

  // Radial electrolytic capacitors.
  const electrolytics: ReadonlyArray<{ dia: Mm; height: Mm; pitch: 2 | 3 }> = [
    { dia: 5, height: 7, pitch: 2 },
    { dia: 6.3, height: 11, pitch: 2 },
    { dia: 8, height: 11.5, pitch: 3 },
    { dia: 10, height: 12.5, pitch: 3 },
  ];
  for (const e of electrolytics) {
    list.push(
      radialElectrolyticFootprint({
        pitchHoles: e.pitch,
        canDiameterMm: e.dia,
        canHeightMm: e.height,
        id: `c-elec-d${formatMmToken(e.dia)}-p${e.pitch}`,
        name: `Electrolytic capacitor, ${e.dia} mm dia, ${e.pitch}-hole pitch`,
      }),
    );
  }

  // Disc ceramic capacitors.
  list.push(
    discCeramicFootprint({
      pitchHoles: 2,
      bodyDiameterMm: 5,
      id: 'c-disc-p2',
      name: 'Disc ceramic capacitor, 2-hole pitch',
    }),
    discCeramicFootprint({
      pitchHoles: 3,
      bodyDiameterMm: 7.5,
      id: 'c-disc-p3',
      name: 'Disc ceramic capacitor, 3-hole pitch',
    }),
  );

  // Boxed film capacitors.
  list.push(
    boxFilmCapacitorFootprint({
      pitchHoles: 2,
      bodyLengthMm: 7,
      bodyWidthMm: 4,
      bodyHeightMm: 6,
      id: 'c-film-p2',
      name: 'Film capacitor, 2-hole pitch',
    }),
    boxFilmCapacitorFootprint({
      pitchHoles: 3,
      bodyLengthMm: 10,
      bodyWidthMm: 5,
      bodyHeightMm: 8,
      id: 'c-film-p3',
      name: 'Film capacitor, 3-hole pitch',
    }),
  );

  // DIP packages, standard 0.3" narrow row spacing.
  const dipCounts: readonly number[] = [8, 14, 16, 18, 20, 28, 40];
  for (const n of dipCounts) {
    list.push(dipFootprint({ pinCount: n, id: `dip-${n}`, name: `DIP-${n}` }));
  }
  // DIP-40 also commonly comes in a 0.6" wide body.
  list.push(dipFootprint({ pinCount: 40, wide: true, id: 'dip-40-wide', name: 'DIP-40 (0.6" wide)' }));

  // TO-92, TO-220.
  list.push(to92Footprint({ id: 'to92', name: 'TO-92' }));
  list.push(to220Footprint({ id: 'to220', name: 'TO-220' }));

  // LEDs.
  for (const dia of [3, 5, 10] as const) {
    list.push(ledFootprint({ diameterMm: dia, id: `led-${dia}mm`, name: `LED, ${dia} mm round` }));
  }

  // Pin headers, 1xN and 2xN, over a sensible range of common sizes.
  const headerCounts: readonly number[] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20];
  for (const n of headerCounts) {
    list.push(pinHeaderFootprint({ rows: 1, cols: n, id: `hdr-1x${n}`, name: `Pin header, 1x${n}` }));
    list.push(pinHeaderFootprint({ rows: 2, cols: n, id: `hdr-2x${n}`, name: `Pin header, 2x${n}` }));
  }

  // Screw terminals, 5.08 mm pitch.
  list.push(
    screwTerminalFootprint({ ways: 2, id: 'screw-terminal-2', name: 'Screw terminal, 2-way' }),
    screwTerminalFootprint({ ways: 3, id: 'screw-terminal-3', name: 'Screw terminal, 3-way' }),
  );

  // Potentiometer, tactile switch, crystal, relay.
  list.push(potentiometerFootprint({ id: 'pot-3', name: 'Potentiometer, 3-pin inline' }));
  list.push(tactileSwitchFootprint({ id: 'sw-tactile', name: 'Tactile switch, 4-pin' }));
  list.push(crystalHc49Footprint({ id: 'xtal-hc49', name: 'Crystal, HC-49/U' }));
  list.push(relayFootprint({ id: 'relay-spdt', name: 'Small SPDT relay' }));

  const map = new Map<string, Footprint>();
  for (const fp of list) {
    if (map.has(fp.id)) {
      throw new Error(`Duplicate footprint id in standard registry: ${fp.id}`);
    }
    map.set(fp.id, fp);
  }
  return map;
}

let cachedRegistry: ReadonlyMap<string, Footprint> | undefined;

/**
 * The full standard footprint library, keyed by id. Built once (lazily) and
 * cached: construction is pure and deterministic, so sharing the same Map
 * instance across calls is safe and avoids rebuilding ~60 footprints per call.
 */
export function standardFootprints(): ReadonlyMap<string, Footprint> {
  cachedRegistry ??= buildStandardFootprints();
  return cachedRegistry;
}

/** Looks up a single standard footprint by id. */
export function getFootprint(id: string): Footprint | undefined {
  return standardFootprints().get(id);
}

/**
 * A lookup function over the standard registry, in the shape connectivity.ts
 * and the DRC/LVS modules expect (`(footprintId) => Footprint | undefined`).
 */
export function footprintLookup(): (id: string) => Footprint | undefined {
  return (id: string) => getFootprint(id);
}
