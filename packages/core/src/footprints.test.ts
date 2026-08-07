import { describe, expect, it } from 'vitest';

import { axialFootprint, dipFootprint, getFootprint, footprintLookup, standardFootprints } from './footprints.js';
import { STANDARD_PITCH_MM } from './model.js';
import type { Footprint, Point2 } from './model.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function pinMm(dCol: number, dRow: number): Point2 {
  return { x: dCol * STANDARD_PITCH_MM, y: dRow * STANDARD_PITCH_MM };
}

function outlineBounds(outline: readonly Point2[]): { minX: number; maxX: number; minY: number; maxY: number } {
  const first = outline[0];
  if (first === undefined) {
    throw new Error('Outline has no points.');
  }
  let minX = first.x;
  let maxX = first.x;
  let minY = first.y;
  let maxY = first.y;
  for (const p of outline) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, maxX, minY, maxY };
}

// ---------------------------------------------------------------------------
// Whole-registry invariants
// ---------------------------------------------------------------------------

describe('standardFootprints() - registry-wide invariants', () => {
  const registry = standardFootprints();

  it('is non-empty', () => {
    expect(registry.size).toBeGreaterThan(0);
  });

  it('every footprint satisfies the structural invariants required for DRC', () => {
    for (const [id, fp] of registry) {
      // Ids match and pins are present.
      expect(fp.id, `registry key "${id}" should equal footprint.id`).toBe(id);
      expect(fp.pins.length, `${id}: pins must be non-empty`).toBeGreaterThan(0);

      // Pin numbers are unique.
      const numbers = fp.pins.map((p) => p.number);
      expect(new Set(numbers).size, `${id}: pin numbers must be unique`).toBe(numbers.length);

      // Pin 1 exists (the anchor convention).
      expect(numbers, `${id}: must have a pin numbered "1"`).toContain('1');

      // Pin offsets are integer grid steps.
      for (const p of fp.pins) {
        expect(Number.isInteger(p.dCol), `${id}: pin ${p.number} dCol must be an integer`).toBe(true);
        expect(Number.isInteger(p.dRow), `${id}: pin ${p.number} dRow must be an integer`).toBe(true);
      }

      // bodyHeight is positive.
      expect(fp.bodyHeight, `${id}: bodyHeight must be positive`).toBeGreaterThan(0);

      // Outline is a valid polygon with at least 3 points.
      expect(fp.bodyOutline.length, `${id}: bodyOutline must have >= 3 points`).toBeGreaterThanOrEqual(3);

      // The outline's bounding box must contain every pin position (in mm).
      const bounds = outlineBounds(fp.bodyOutline);
      for (const p of fp.pins) {
        const mm = pinMm(p.dCol, p.dRow);
        expect(mm.x, `${id}: pin ${p.number} x=${mm.x} must be within outline bounds`).toBeGreaterThanOrEqual(
          bounds.minX,
        );
        expect(mm.x).toBeLessThanOrEqual(bounds.maxX);
        expect(mm.y, `${id}: pin ${p.number} y=${mm.y} must be within outline bounds`).toBeGreaterThanOrEqual(
          bounds.minY,
        );
        expect(mm.y).toBeLessThanOrEqual(bounds.maxY);
      }
    }
  });

  it('ids in the registry are unique and match their footprint id field', () => {
    const seen = new Set<string>();
    for (const [id, fp] of registry) {
      expect(seen.has(id), `duplicate id: ${id}`).toBe(false);
      seen.add(id);
      expect(fp.id).toBe(id);
    }
    expect(seen.size).toBe(registry.size);
  });
});

// ---------------------------------------------------------------------------
// DIP-8: pin 1 at (0,0), rows 3 holes apart, counter-clockwise numbering.
// ---------------------------------------------------------------------------

describe('dipFootprint - DIP-8', () => {
  const dip8 = dipFootprint({ pinCount: 8, id: 'dip-8', name: 'DIP-8' });

  it('has exactly 8 pins', () => {
    expect(dip8.pins.length).toBe(8);
  });

  it('places pin 1 at the anchor (0,0)', () => {
    const pin1 = dip8.pins.find((p) => p.number === '1');
    expect(pin1).toBeDefined();
    expect(pin1?.dCol).toBe(0);
    expect(pin1?.dRow).toBe(0);
  });

  it('spaces the two rows exactly 3 holes apart (0.3")', () => {
    const cols = new Set(dip8.pins.map((p) => p.dCol));
    expect(cols).toEqual(new Set([0, 3]));
  });

  it('runs pins 1-4 down the left column and 5-8 back up the right column, counter-clockwise', () => {
    const byNumber = new Map(dip8.pins.map((p) => [p.number, p]));
    // Left column: 1,2,3,4 at dCol 0, dRow increasing 0..3.
    for (let i = 0; i < 4; i++) {
      const p = byNumber.get(String(i + 1));
      expect(p, `pin ${i + 1} should exist`).toBeDefined();
      expect(p?.dCol).toBe(0);
      expect(p?.dRow).toBe(i);
    }
    // Right column: 5,6,7,8 at dCol 3, dRow decreasing 3..0 (back up to the top,
    // beside pin 1 - this is the counter-clockwise traversal).
    for (let i = 0; i < 4; i++) {
      const number = String(5 + i);
      const p = byNumber.get(number);
      expect(p, `pin ${number} should exist`).toBeDefined();
      expect(p?.dCol).toBe(3);
      expect(p?.dRow).toBe(3 - i);
    }
  });

  it('ends numbering (pin 8) on the opposite side of the package from pin 1, at the same end', () => {
    const pin1 = dip8.pins.find((p) => p.number === '1');
    const pin8 = dip8.pins.find((p) => p.number === '8');
    expect(pin1?.dRow).toBe(pin8?.dRow); // same row (same end of the package, next to the notch)
    expect(pin1?.dCol).not.toBe(pin8?.dCol); // opposite column (opposite row of the DIP)
  });
});

// ---------------------------------------------------------------------------
// Axial span
// ---------------------------------------------------------------------------

describe('axialFootprint', () => {
  it('places the two pins exactly spanHoles holes apart for spanHoles=5', () => {
    const fp = axialFootprint({ spanHoles: 5, bodyLengthMm: 6.3, bodyDiameterMm: 2.5 });
    expect(fp.pins.length).toBe(2);
    const pin1 = fp.pins.find((p) => p.number === '1');
    const pin2 = fp.pins.find((p) => p.number === '2');
    expect(pin1?.dCol).toBe(0);
    expect(pin1?.dRow).toBe(0);
    expect(pin2?.dRow).toBe(0);
    expect(pin2?.dCol).toBe(5);
  });

  it('defaults to non-polarized and lets callers opt into polarized (diode) behaviour', () => {
    const resistor = axialFootprint({ spanHoles: 4, bodyLengthMm: 6.3, bodyDiameterMm: 2.3 });
    expect(resistor.polarized).toBe(false);
    const diode = axialFootprint({ spanHoles: 4, bodyLengthMm: 5.2, bodyDiameterMm: 2.7, polarized: true });
    expect(diode.polarized).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Polarization spot checks
// ---------------------------------------------------------------------------

describe('polarized flag', () => {
  it('is true for LEDs and electrolytic capacitors', () => {
    expect(getFootprint('led-5mm')?.polarized).toBe(true);
    expect(getFootprint('c-elec-d5-p2')?.polarized).toBe(true);
  });

  it('is false for resistors and ceramic capacitors', () => {
    expect(getFootprint('r-axial-4')?.polarized).toBe(false);
    expect(getFootprint('c-disc-p2')?.polarized).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// getFootprint / footprintLookup
// ---------------------------------------------------------------------------

describe('getFootprint / footprintLookup', () => {
  it('getFootprint returns undefined for an unknown id', () => {
    expect(getFootprint('does-not-exist')).toBeUndefined();
  });

  it('getFootprint returns the same footprint that is in the registry', () => {
    const fp = getFootprint('dip-8');
    expect(fp).toBeDefined();
    expect(standardFootprints().get('dip-8')).toBe(fp);
  });

  it('footprintLookup() returns a function usable as a FootprintLookup', () => {
    const lookup = footprintLookup();
    const fp: Footprint | undefined = lookup('to220');
    expect(fp?.id).toBe('to220');
    expect(lookup('does-not-exist')).toBeUndefined();
  });
});
