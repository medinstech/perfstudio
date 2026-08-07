import { describe, expect, it } from 'vitest';
import type { PerfDocument } from './model.js';
import { CURRENT_FORMAT_VERSION, deserializeDocument, parseDocumentOrThrow, serializeDocument } from './persist.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/**
 * A rich document exercising: several components, all six physical conductor kinds
 * (plus 'strip', the v2 stripboard kind, for full union coverage), nets with and
 * without currentA/voltageV, cuts, and a mix of optional fields present vs. omitted.
 *
 * Top-level arrays are already declared in their canonical sorted order (by id, or by
 * hole-then-id for cuts) so that a straight `toEqual` against the round-tripped result
 * holds without re-sorting the fixture by hand.
 */
const richDoc: PerfDocument = {
  formatVersion: CURRENT_FORMAT_VERSION,
  meta: {
    name: 'Test Board',
    created: '2026-01-01T00:00:00.000Z',
    modified: '2026-01-02T00:00:00.000Z',
  },
  board: {
    type: 'pad-per-hole',
    cols: 40,
    rows: 30,
    pitch: 2.54,
    thickness: 1.6,
    material: 'FR4',
    padDiameter: 1.8,
    drillDiameter: 0.9,
    // stripAxis intentionally omitted: only meaningful for stripboard.
  },
  components: [
    { id: 'c1', ref: 'R1', value: '10k', footprintId: 'axial-r', anchor: { col: 2, row: 2 }, rotation: 0, mirrored: false, locked: false },
    { id: 'c2', ref: 'R2', value: '4k7', footprintId: 'axial-r', anchor: { col: 6, row: 2 }, rotation: 90, mirrored: false, locked: true },
    { id: 'c3', ref: 'U1', value: 'NE555', footprintId: 'dip8', anchor: { col: 10, row: 4 }, rotation: 180, mirrored: true, locked: false },
  ],
  conductors: [
    {
      id: 'cond-1',
      kind: 'lead-bend',
      path: [{ col: 2, row: 2 }, { col: 2, row: 3 }],
      side: 'bottom',
      layerZ: 0,
      componentId: 'c1',
      pinNumber: '1',
      // netId intentionally omitted.
    },
    {
      id: 'cond-2',
      kind: 'solder-trace',
      path: [{ col: 2, row: 3 }, { col: 3, row: 3 }, { col: 4, row: 3 }],
      side: 'bottom',
      layerZ: 0,
      netId: 'net-1',
      buildup: 'normal',
      // spine intentionally omitted (only conventional for solder-trace-wired).
    },
    {
      id: 'cond-3',
      kind: 'solder-trace-wired',
      path: [{ col: 4, row: 3 }, { col: 4, row: 4 }],
      side: 'bottom',
      layerZ: 1,
      netId: 'net-1',
      buildup: 'heavy',
      spine: { material: 'tinned-copper', gauge: 0.5 },
    },
    {
      id: 'cond-4',
      kind: 'bare-wire',
      path: [{ col: 6, row: 2 }, { col: 6, row: 8 }],
      side: 'bottom',
      layerZ: 2,
      gaugeAwg: 24,
      color: 'red',
      // netId intentionally omitted.
    },
    {
      id: 'cond-5',
      kind: 'insulated-wire',
      path: [{ col: 8, row: 1 }, { col: 8, row: 9 }],
      side: 'bottom',
      layerZ: 0,
      netId: 'net-2',
      // gaugeAwg and color intentionally omitted.
    },
    {
      id: 'cond-6',
      kind: 'top-jumper',
      path: [{ col: 10, row: 4 }, { col: 13, row: 4 }],
      side: 'top',
      layerZ: 0,
      netId: 'net-3',
      gaugeAwg: 26,
      color: 'blue',
    },
    {
      id: 'cond-7',
      kind: 'strip',
      path: [{ col: 0, row: 0 }, { col: 1, row: 0 }, { col: 2, row: 0 }],
      side: 'bottom',
      layerZ: 0,
      // netId intentionally omitted.
    },
  ],
  cuts: [
    { id: 'cut-1', at: { col: 2, row: 1 } },
    { id: 'cut-2', at: { col: 5, row: 5 } },
  ],
  nets: [
    { id: 'net-1', name: 'GND', nodes: [{ componentRef: 'R1', pin: '1' }, { componentRef: 'U1', pin: '1' }], class: 'ground', currentA: 1.5, voltageV: 5 },
    { id: 'net-2', name: 'SIG_A', nodes: [{ componentRef: 'U1', pin: '2' }], class: 'signal' },
    { id: 'net-3', name: 'V_PLUS', nodes: [{ componentRef: 'R2', pin: '2' }], class: 'power', currentA: 0.2 },
  ],
};

// ---------------------------------------------------------------------------
// Round-trip
// ---------------------------------------------------------------------------

describe('round-trip', () => {
  it('serializes and deserializes a rich document to a deeply equal result, with no warnings', () => {
    const json = serializeDocument(richDoc);
    const result = deserializeDocument(json);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.document).toEqual(richDoc);
      expect(result.warnings).toEqual([]);
    }
  });

  it('parseDocumentOrThrow returns the same document without throwing', () => {
    const json = serializeDocument(richDoc);
    expect(parseDocumentOrThrow(json)).toEqual(richDoc);
  });
});

// ---------------------------------------------------------------------------
// Determinism — the git-diffability guarantee
// ---------------------------------------------------------------------------

describe('determinism', () => {
  it('serializing the same document twice gives byte-identical output', () => {
    expect(serializeDocument(richDoc)).toBe(serializeDocument(richDoc));
  });

  it('serializing documents that differ only in array insertion order gives identical output', () => {
    const reordered: PerfDocument = {
      ...richDoc,
      components: [...richDoc.components].reverse(),
      conductors: [...richDoc.conductors].reverse(),
      cuts: [...richDoc.cuts].reverse(),
      nets: [...richDoc.nets].reverse(),
    };
    expect(serializeDocument(reordered)).toBe(serializeDocument(richDoc));
  });

  it('produces 2-space indentation and a trailing newline', () => {
    const json = serializeDocument(richDoc);
    expect(json.endsWith('\n')).toBe(true);
    expect(json).toContain('\n  "formatVersion"');
  });

  it('emits keys in fixed declared order, not alphabetical', () => {
    const json = serializeDocument(richDoc);
    // "type" is declared before "cols" in Board, which is NOT alphabetical order.
    expect(json.indexOf('"type"')).toBeLessThan(json.indexOf('"cols"'));
    // "id" is declared before "kind" in Conductor.
    const firstConductorIdIdx = json.indexOf('"id": "cond-1"');
    const firstConductorKindIdx = json.indexOf('"kind": "lead-bend"');
    expect(firstConductorIdIdx).toBeLessThan(firstConductorKindIdx);
  });
});

// ---------------------------------------------------------------------------
// Diff stability
// ---------------------------------------------------------------------------

/** Simple set-difference line diff — sufficient to bound the blast radius of an edit. */
function diffLineCount(a: string, b: string): number {
  const setA = new Set(a.split('\n'));
  const setB = new Set(b.split('\n'));
  let diff = 0;
  for (const line of setA) if (!setB.has(line)) diff++;
  for (const line of setB) if (!setA.has(line)) diff++;
  return diff;
}

describe('diff stability', () => {
  it('moving one component by one hole changes only a handful of lines', () => {
    const before = serializeDocument(richDoc);

    const moved: PerfDocument = {
      ...richDoc,
      components: richDoc.components.map((c) =>
        c.id === 'c1' ? { ...c, anchor: { col: c.anchor.col + 1, row: c.anchor.row } } : c,
      ),
    };
    const after = serializeDocument(moved);

    expect(before).not.toBe(after);
    expect(diffLineCount(before, after)).toBeLessThan(6);
  });
});

// ---------------------------------------------------------------------------
// Format version
// ---------------------------------------------------------------------------

describe('format version', () => {
  it('rejects a formatVersion newer than the current format', () => {
    const raw = JSON.parse(serializeDocument(richDoc)) as Record<string, unknown>;
    raw['formatVersion'] = CURRENT_FORMAT_VERSION + 1;
    const result = deserializeDocument(JSON.stringify(raw));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('format-too-new');
      expect(result.message).toMatch(/upgrade/i);
    }
  });
});

// ---------------------------------------------------------------------------
// Malformed input
// ---------------------------------------------------------------------------

describe('malformed input', () => {
  it('rejects text that is not JSON', () => {
    const result = deserializeDocument('{ this is not valid json');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('invalid-json');
    }
  });

  it('rejects JSON whose root is not an object', () => {
    const result = deserializeDocument('[1, 2, 3]');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('not-an-object');
    }
  });

  it('rejects a document missing "board", with a path pointing at it', () => {
    const raw = JSON.parse(serializeDocument(richDoc)) as Record<string, unknown>;
    delete raw['board'];
    const result = deserializeDocument(JSON.stringify(raw));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('missing-field');
      expect(result.path).toBe('board');
    }
  });

  it('rejects a component with a non-integer anchor, with a path to the exact field', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const raw = JSON.parse(serializeDocument(richDoc)) as any;
    raw.components[0].anchor.col = 1.5;
    const result = deserializeDocument(JSON.stringify(raw));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('invalid-value');
      expect(result.path).toBe('components[0].anchor.col');
    }
  });

  it('uses distinct codes for each of the four malformed-input cases above', () => {
    const notJson = deserializeDocument('not json at all');
    const notObject = deserializeDocument('"just a string"');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rawMissingBoard = JSON.parse(serializeDocument(richDoc)) as any;
    delete rawMissingBoard.board;
    const missingBoard = deserializeDocument(JSON.stringify(rawMissingBoard));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rawBadAnchor = JSON.parse(serializeDocument(richDoc)) as any;
    rawBadAnchor.components[0].anchor.row = 2.2;
    const badAnchor = deserializeDocument(JSON.stringify(rawBadAnchor));

    const codes = [notJson, notObject, missingBoard, badAnchor].map((r) => (r.ok ? undefined : r.code));
    expect(codes).toEqual(['invalid-json', 'not-an-object', 'missing-field', 'invalid-value']);
    expect(new Set(codes).size).toBe(4);
  });
});

// ---------------------------------------------------------------------------
// Non-finite numbers
// ---------------------------------------------------------------------------

describe('non-finite numbers', () => {
  it('rejects a NaN coordinate on serialize rather than silently emitting null', () => {
    const bad: PerfDocument = {
      ...richDoc,
      components: richDoc.components.map((c) =>
        c.id === 'c1' ? { ...c, anchor: { col: Number.NaN, row: 0 } } : c,
      ),
    };
    expect(() => serializeDocument(bad)).toThrow(/non-finite/);
  });

  it('rejects an Infinity value on serialize rather than silently emitting null', () => {
    const bad: PerfDocument = { ...richDoc, board: { ...richDoc.board, pitch: Number.POSITIVE_INFINITY } };
    expect(() => serializeDocument(bad)).toThrow(/non-finite/);
  });

  it('normalizes -0 to 0 rather than leaking a signed zero into the file', () => {
    const withNegativeZero: PerfDocument = {
      ...richDoc,
      components: richDoc.components.map((c) => (c.id === 'c1' ? { ...c, anchor: { col: -0, row: 0 } } : c)),
    };
    const json = serializeDocument(withNegativeZero);
    // A naive substring check for "-0" is unsound here: ISO dates like "2026-01-02"
    // legitimately contain that substring. Check the actual serialized field instead.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const parsed = JSON.parse(json) as any;
    const anchor = parsed.components.find((c: { id: string }) => c.id === 'c1').anchor;
    expect(Object.is(anchor.col, -0)).toBe(false);
    expect(Object.is(anchor.col, 0)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Solder-trace invariant: warning, not a hard error
// ---------------------------------------------------------------------------

describe('solder-trace adjacency', () => {
  it('loads a solder-trace with a diagonal step with a warning, and keeps the bad path', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const raw = JSON.parse(serializeDocument(richDoc)) as any;
    const trace = raw.conductors.find((c: { id: string }) => c.id === 'cond-2');
    trace.path = [{ col: 0, row: 0 }, { col: 1, row: 1 }]; // diagonal step

    const result = deserializeDocument(JSON.stringify(raw));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.warnings.some((w) => w.toLowerCase().includes('adjacent'))).toBe(true);
      const loaded = result.document.conductors.find((c) => c.id === 'cond-2');
      expect(loaded?.path).toEqual([{ col: 0, row: 0 }, { col: 1, row: 1 }]);
    }
  });
});

// ---------------------------------------------------------------------------
// Optional fields
// ---------------------------------------------------------------------------

describe('optional fields', () => {
  it('omits undefined optional fields from the output instead of emitting null', () => {
    const json = serializeDocument(richDoc);
    expect(json).not.toMatch(/:\s*null/);
    // board.stripAxis, cond-1.netId, cond-2.spine, cond-5.gaugeAwg/color, net-2.currentA/voltageV
    // are all undefined in richDoc and must not appear as keys at all.
    expect(json).not.toContain('"stripAxis"');
  });

  it('unknown properties in a hand-edited file are dropped with a warning, not a hard error', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const raw = JSON.parse(serializeDocument(richDoc)) as any;
    raw.board.totallyMadeUpField = 'agent typo';
    const result = deserializeDocument(JSON.stringify(raw));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.warnings.some((w) => w.includes('totallyMadeUpField'))).toBe(true);
      expect(result.document.board).toEqual(richDoc.board);
    }
  });
});
