/**
 * Produces a realistic .perf board for the Qt prototype to display.
 *
 * The prototype does NOT port the engine — it reads this file as fixed data. The point
 * is to evaluate Qt and VTK as a UI stack, not to rebuild what already works.
 *
 * Run:  node prototypes/qt/make_fixture.mjs
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const core = await import('../../packages/core/dist/index.js');

const here = dirname(fileURLToPath(import.meta.url));
const META = {
  name: 'ne555-blinker',
  created: '2026-01-01T00:00:00.000Z',
  modified: '2026-01-01T00:00:00.000Z',
};

const board = { ...core.DEFAULT_BOARD, cols: 32, rows: 22, material: 'FR2' };
const bus = new core.CommandBus(core.createEmptyDocument(META, board), core.createStandardRegistry(), {
  nextId: core.createIdGenerator(),
});
const fp = core.footprintLookup();
const ok = (r, what) => {
  if (!r.ok) console.log(`  ! ${what}: [${r.code}] ${r.message}`);
  return r.ok;
};

// --- Parts, hand-placed so the layout reads like something a person would build. ---
const parts = [
  ['U1', 'NE555', 'dip-8', 6, 8],
  ['R1', '10k', 'r-axial-4', 3, 4],
  ['R2', '47k', 'r-axial-4', 3, 14],
  ['R3', '470R', 'r-axial-4', 14, 4],
  ['C1', '10uF', 'c-elec-d5-p2', 12, 14],
  ['C2', '100nF', 'c-disc-1', 12, 17],
  ['D1', 'LED', 'led-5mm', 20, 6],
  ['J1', 'PWR', 'screw-terminal-2', 26, 2],
  ['J2', 'OUT', 'hdr-1x3', 26, 16],
];
for (const [ref, value, footprintId, col, row] of parts) {
  ok(bus.dispatch('component.place', { ref, value, footprintId, anchor: { col, row } }), `place ${ref}`);
}

// --- Conductors: one of every kind, so the prototype has to draw them all. ---
const h = (col, row) => ({ col, row });
const conductors = [
  // A GND rail: long, so it gets a spine. This is the §6.2 bus pattern by hand.
  {
    kind: 'solder-trace-wired',
    path: Array.from({ length: 10 }, (_, i) => h(4 + i, 20)),
    side: 'bottom',
    layerZ: 0,
    buildup: 'heavy',
    spine: { material: 'tinned-copper', gauge: 0.6 },
  },
  // Short pure solder traces — the everyday primitive.
  { kind: 'solder-trace', path: [h(6, 8), h(7, 8), h(8, 8)], side: 'bottom', layerZ: 0, buildup: 'normal' },
  { kind: 'solder-trace', path: [h(6, 11), h(6, 12), h(6, 13)], side: 'bottom', layerZ: 0, buildup: 'normal' },
  { kind: 'solder-trace', path: [h(12, 14), h(13, 14), h(14, 14)], side: 'bottom', layerZ: 0, buildup: 'light' },
  // A bare wire: soldered at the ends only, passing over everything between.
  { kind: 'bare-wire', path: [h(9, 8), h(20, 6)], side: 'bottom', layerZ: 0, gaugeAwg: 22 },
  // Insulated wires, which may cross other conductors.
  { kind: 'insulated-wire', path: [h(26, 2), h(6, 8)], side: 'bottom', layerZ: 1, gaugeAwg: 24, color: '#d32f2f' },
  { kind: 'insulated-wire', path: [h(27, 2), h(9, 20)], side: 'bottom', layerZ: 1, gaugeAwg: 24, color: '#212121' },
  { kind: 'insulated-wire', path: [h(9, 11), h(26, 16)], side: 'bottom', layerZ: 1, gaugeAwg: 26, color: '#1976d2' },
  // A top-side jumper.
  { kind: 'top-jumper', path: [h(3, 4), h(3, 14)], side: 'top', layerZ: 0, gaugeAwg: 24, color: '#388e3c' },
];
for (const conductor of conductors) {
  ok(bus.dispatch('conductor.add', { conductor }), `add ${conductor.kind}`);
}

// --- Schematic intent, so DRC/LVS have something to compare against. ---
const net = (id, name, cls, nodes) => ({
  id,
  name,
  class: cls,
  nodes: nodes.map(([componentRef, pin]) => ({ componentRef, pin })),
});
ok(
  bus.dispatch('netlist.import', {
    nets: [
      net('n1', 'GND', 'ground', [['U1', '1'], ['C1', '2'], ['C2', '2'], ['J1', '2'], ['J2', '3']]),
      net('n2', 'VCC', 'power', [['U1', '8'], ['U1', '4'], ['R1', '1'], ['J1', '1']]),
      net('n3', 'OUT', 'signal', [['U1', '3'], ['R3', '1'], ['J2', '1']]),
      net('n4', 'THRESH', 'signal', [['U1', '6'], ['U1', '2'], ['R2', '2'], ['C1', '1']]),
      net('n5', 'DISCH', 'signal', [['U1', '7'], ['R1', '2'], ['R2', '1']]),
      net('n6', 'LEDK', 'signal', [['R3', '2'], ['D1', '1']]),
    ],
  }),
  'netlist.import',
);

const doc = bus.document;
const json = core.serializeDocument(doc);
const out = join(here, 'sample.perf');
writeFileSync(out, json, 'utf8');

const drc = core.runDrc(doc, fp);
const lvs = core.runLvs(doc, fp);
console.log(`wrote ${out}`);
console.log(`  board       ${doc.board.cols}x${doc.board.rows} ${doc.board.material}`);
console.log(`  components  ${doc.components.length}`);
console.log(`  conductors  ${doc.conductors.length} (${[...new Set(doc.conductors.map((c) => c.kind))].join(', ')})`);
console.log(`  nets        ${doc.nets.length}`);
console.log(`  DRC         ${drc.filter((v) => v.severity === 'error').length} errors, ${drc.filter((v) => v.severity === 'warning').length} warnings`);
console.log(`  LVS         ${lvs.summary.opens} open, ${lvs.summary.shorts} short, ${lvs.summary.matchedNets} matched`);

// Footprint geometry the prototype needs, dumped alongside so it needs no engine.
const used = [...new Set(doc.components.map((c) => c.footprintId))];
const footprints = Object.fromEntries(used.map((id) => [id, fp(id)]));
writeFileSync(join(here, 'footprints.json'), JSON.stringify(footprints, null, 2), 'utf8');
console.log(`  footprints  ${used.length} dumped to footprints.json`);
