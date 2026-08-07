/**
 * Cross-package integration test: netlist -> place -> route -> DRC -> LVS -> save.
 *
 * Lives in @perfstudio/parsers because this is the only package that depends on both
 * the parser and the engine, so it is the only place the whole pipeline can be driven.
 *
 * Every other test in this repo checks one module against its own spec. This one checks
 * that the modules agree with each other, which is where the expensive bugs live: a
 * footprint whose pins land where connectivity does not expect them, a serializer that
 * quietly drops a field, a DRC rule that never fires on a real board.
 */

import { describe, expect, it } from 'vitest';

import {
  CommandBus,
  coordToHoleRef,
  createEmptyDocument,
  createIdGenerator,
  createStandardRegistry,
  continuityChecks,
  deserializeDocument,
  footprintLookup,
  isolationChecks,
  pinHole,
  runDrc,
  runLvs,
  serializeDocument,
} from '@perfstudio/core';
import type { DocumentMeta, HoleCoord } from '@perfstudio/core';

import { parseKicadNetlist } from './kicad-netlist.js';
import { NE555_ASTABLE_NETLIST } from './__fixtures__/ne555-astable.js';

const META: DocumentMeta = {
  name: 'ne555-astable',
  created: '2026-01-01T00:00:00.000Z',
  modified: '2026-01-01T00:00:00.000Z',
};

const FOOTPRINT_FOR_PREFIX: Record<string, string> = {
  U: 'dip-8',
  R: 'r-axial-4',
  C: 'c-elec-d5-p2',
  D: 'led-5mm',
  J: 'screw-terminal-2',
};

function buildBoard() {
  const lookup = footprintLookup();
  const bus = new CommandBus(createEmptyDocument(META), createStandardRegistry(), {
    nextId: createIdGenerator(),
  });

  const imported = parseKicadNetlist(NE555_ASTABLE_NETLIST);
  bus.dispatch('netlist.import', { nets: imported.nets });

  // Lay the parts out on a coarse grid, four per row. Spacing is generous so that
  // placement itself is not what this test is probing — but it still has to fit the
  // default 60x40 board, because component.place rightly refuses an off-board anchor.
  imported.components.forEach((c, i) => {
    const footprintId = FOOTPRINT_FOR_PREFIX[c.ref[0] ?? ''] ?? 'r-axial-4';
    const result = bus.dispatch('component.place', {
      ref: c.ref,
      value: c.value,
      footprintId,
      anchor: { col: 1 + (i % 4) * 13, row: 4 + Math.floor(i / 4) * 12 },
    });
    if (!result.ok) {
      throw new Error(`fixture placement failed for ${c.ref}: [${result.code}] ${result.message}`);
    }
  });

  return { bus, lookup, imported };
}

describe('netlist to board pipeline', () => {
  it('imports the NE555 netlist and places every component', () => {
    const { bus, imported } = buildBoard();
    expect(imported.components.length).toBeGreaterThan(5);
    expect(bus.document.components).toHaveLength(imported.components.length);
    expect(bus.document.nets).toHaveLength(imported.nets.length);
    // KiCad's unconnected-* pseudo-nets must be dropped, and said so.
    expect(imported.warnings.join(' ')).toMatch(/unconnected/i);
  });

  it('footprint pin positions agree with what the engine computes', () => {
    const { bus, lookup } = buildBoard();
    const u1 = bus.document.components.find((c) => c.ref === 'U1');
    expect(u1).toBeDefined();
    const dip = lookup(u1!.footprintId);
    expect(dip).toBeDefined();

    // A DIP-8's pin 1 and pin 8 face each other across the package: same row,
    // three columns apart. If the footprint library and geometry ever disagree about
    // rotation or anchoring, this is where it shows up.
    const p1 = pinHole(u1!, dip!, '1');
    const p8 = pinHole(u1!, dip!, '8');
    const p5 = pinHole(u1!, dip!, '5');
    expect(p1).toBeDefined();
    expect(p8?.row).toBe(p1?.row);
    expect((p8?.col ?? 0) - (p1?.col ?? 0)).toBe(3);
    expect(p5?.row).toBe((p1?.row ?? 0) + 3);
  });

  it('LVS catches a solder trace that shorts several pins of a DIP together', () => {
    const { bus, lookup } = buildBoard();
    const u1 = bus.document.components.find((c) => c.ref === 'U1')!;
    const dip = lookup(u1.footprintId)!;

    // Run a trace straight down the DIP's left-hand pins — the classic beginner error.
    const path = ['1', '2', '3', '4']
      .map((n) => pinHole(u1, dip, n))
      .filter((h): h is HoleCoord => h !== undefined);
    const added = bus.dispatch('conductor.add', {
      conductor: { kind: 'solder-trace', path, side: 'bottom', layerZ: 0, buildup: 'normal' },
    });
    expect(added.ok).toBe(true);

    const lvs = runLvs(bus.document, lookup);
    expect(lvs.ok).toBe(false);
    const shorts = lvs.issues.filter((i) => i.kind === 'short');
    expect(shorts).toHaveLength(1);
    // The message has to name the nets: this is what a user acts on.
    expect(shorts[0]?.netNames.length).toBeGreaterThanOrEqual(2);
    expect(shorts[0]?.message).toContain(coordToHoleRef(path[0]!));
    expect(lvs.summary.shorts).toBe(1);
  });

  it('an unrouted import reports every net as under-connected, and nothing as matched', () => {
    const { bus, lookup } = buildBoard();
    const lvs = runLvs(bus.document, lookup);
    expect(lvs.summary.matchedNets).toBe(0);
    // Single-pin nets are excluded on purpose: two pins have to exist before "not
    // connected to each other" means anything, so reporting them would be noise.
    const connectableNets = bus.document.nets.filter((n) => n.nodes.length >= 2).length;
    expect(connectableNets).toBeGreaterThan(0);
    expect(lvs.summary.opens).toBe(connectableNets);
    expect(lvs.summary.shorts).toBe(0);
  });

  it('derives a measurement checklist from the schematic, power/ground pair first', () => {
    const { bus } = buildBoard();
    const continuity = continuityChecks(bus.document);
    const isolation = isolationChecks(bus.document);

    // A spanning chain, not the full cross product: n-1 measurements per net.
    const expectedSpan = bus.document.nets
      .filter((n) => n.nodes.length >= 2)
      .reduce((sum, n) => sum + n.nodes.length - 1, 0);
    expect(continuity).toHaveLength(expectedSpan);

    expect(isolation.length).toBeGreaterThan(0);
    const first = isolation[0]!;
    const classOf = (name: string) => bus.document.nets.find((n) => n.name === name)?.class;
    expect(new Set([classOf(first.netA), classOf(first.netB)])).toEqual(
      new Set(['power', 'ground']),
    );
  });

  it('DRC runs over a real board without crashing and reports unrouted pins', () => {
    const { bus, lookup } = buildBoard();
    const violations = runDrc(bus.document, lookup);
    expect(violations.some((v) => v.rule === 'pin-not-connected')).toBe(true);
    // Deterministic across runs — the same board must never produce a different report.
    expect(runDrc(bus.document, lookup)).toEqual(violations);
  });

  it('round-trips a fully populated document through the project file format', () => {
    const { bus, lookup } = buildBoard();
    const u1 = bus.document.components.find((c) => c.ref === 'U1')!;
    const dip = lookup(u1.footprintId)!;
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'solder-trace-wired',
        path: ['1', '2'].map((n) => pinHole(u1, dip, n)!),
        side: 'bottom',
        layerZ: 0,
        buildup: 'heavy',
        spine: { material: 'tinned-copper', gauge: 0.6 },
      },
    });
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'insulated-wire',
        path: [
          { col: 20, row: 20 },
          { col: 30, row: 25 },
        ],
        side: 'bottom',
        layerZ: 1,
        gaugeAwg: 24,
        color: '#ff0000',
      },
    });

    const json = serializeDocument(bus.document);
    const back = deserializeDocument(json);
    expect(back.ok).toBe(true);
    if (!back.ok) return;

    expect(back.warnings).toEqual([]);
    // Re-serializing must be byte-identical, which is the git-diffability guarantee.
    expect(serializeDocument(back.document)).toBe(json);
    // And nothing may be lost. Compared key-order-insensitively, because reordering
    // keys is precisely what the serializer is for.
    expect(normalise(back.document)).toEqual(normalise(bus.document));
  });

  it('undoing every command returns the document to empty', () => {
    const { bus } = buildBoard();
    while (bus.canUndo()) bus.undo();
    expect(bus.document.components).toHaveLength(0);
    expect(bus.document.conductors).toHaveLength(0);
    expect(bus.document.nets).toHaveLength(0);
  });
});

/** Sorts object keys recursively so comparison ignores key order. */
function normalise(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalise);
  if (value !== null && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(obj)
        .sort()
        .map((k) => [k, normalise(obj[k])]),
    );
  }
  return value;
}
