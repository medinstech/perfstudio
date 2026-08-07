/**
 * Generates golden fixtures from the TypeScript engine, so the Python port can be
 * proved equivalent rather than merely tested.
 *
 * The TS engine is deterministic by construction — no clock, no RNG, injected ids — so
 * its output for a given document is a stable specification. We freeze that here and
 * the Python tests assert against it. "All tests pass" is a much weaker claim than
 * "produces byte-identical results to the implementation we are replacing".
 *
 *   node tools/diffcheck/generate.mjs
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const core = await import('../../packages/core/dist/index.js');
const { parseKicadNetlist } = await import('../../packages/parsers/dist/index.js');
const { NE555_ASTABLE_NETLIST } = await import(
  '../../packages/parsers/dist/__fixtures__/ne555-astable.js'
);

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, 'golden');
mkdirSync(OUT, { recursive: true });

const META = {
  name: 'diffcheck',
  created: '2026-01-01T00:00:00.000Z',
  modified: '2026-01-01T00:00:00.000Z',
};
const lookup = core.footprintLookup();

/** Deterministic PRNG: the fixtures must be reproducible from this file alone. */
function mulberry32(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const FOOTPRINT_POOL = ['dip-8', 'r-axial-4', 'r-axial-5', 'c-elec-d5-p2', 'c-disc-1', 'led-5mm', 'to92', 'hdr-1x4'];

/** A pseudo-random but valid board: parts placed, some routed, a netlist attached. */
function makeCase(seed, { cols = 30, rows = 20, parts = 8, traces = 4, wires = 3 } = {}) {
  const rnd = mulberry32(seed);
  const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
  const board = { ...core.DEFAULT_BOARD, cols, rows, material: pick(['FR4', 'FR2']) };
  const bus = new core.CommandBus(core.createEmptyDocument(META, board), core.createStandardRegistry(), {
    nextId: core.createIdGenerator(),
  });

  for (let i = 0; i < parts; i++) {
    bus.dispatch('component.place', {
      ref: `X${i + 1}`,
      value: `v${i}`,
      footprintId: pick(FOOTPRINT_POOL),
      anchor: { col: 1 + Math.floor(rnd() * (cols - 6)), row: 1 + Math.floor(rnd() * (rows - 6)) },
      rotation: pick([0, 90, 180, 270]),
      mirrored: rnd() < 0.25,
    });
  }

  for (let i = 0; i < traces; i++) {
    const col = 1 + Math.floor(rnd() * (cols - 8));
    const row = 1 + Math.floor(rnd() * (rows - 2));
    const len = 2 + Math.floor(rnd() * 6);
    const path = Array.from({ length: len }, (_, k) => ({ col: col + k, row }));
    const wired = rnd() < 0.4;
    bus.dispatch('conductor.add', {
      conductor: wired
        ? {
            kind: 'solder-trace-wired',
            path,
            side: 'bottom',
            layerZ: 0,
            buildup: pick(['light', 'normal', 'heavy']),
            spine: { material: 'tinned-copper', gauge: 0.6 },
          }
        : { kind: 'solder-trace', path, side: 'bottom', layerZ: 0, buildup: pick(['light', 'normal', 'heavy']) },
    });
  }

  for (let i = 0; i < wires; i++) {
    const a = { col: Math.floor(rnd() * cols), row: Math.floor(rnd() * rows) };
    const b = { col: Math.floor(rnd() * cols), row: Math.floor(rnd() * rows) };
    if (a.col === b.col && a.row === b.row) continue;
    bus.dispatch('conductor.add', {
      conductor: {
        kind: pick(['bare-wire', 'insulated-wire', 'top-jumper']),
        path: [a, b],
        side: rnd() < 0.85 ? 'bottom' : 'top',
        layerZ: rnd() < 0.5 ? 0 : 1,
        gaugeAwg: pick([22, 24, 26]),
      },
    });
  }

  // A netlist over the placed refs, so LVS has intent to compare against.
  const refs = bus.document.components.map((c) => c.ref);
  const nets = [];
  for (let n = 0; n < 3 && refs.length >= 2; n++) {
    const nodes = [];
    for (let k = 0; k < 2 + Math.floor(rnd() * 2); k++) {
      nodes.push({ componentRef: pick(refs), pin: '1' });
    }
    nets.push({
      id: `net-${n + 1}`,
      name: ['GND', 'VCC', 'SIG'][n],
      class: ['ground', 'power', 'signal'][n],
      nodes,
    });
  }
  bus.dispatch('netlist.import', { nets });
  return bus.document;
}

/** Everything the Python port has to reproduce for a given document. */
function describe(doc) {
  const physical = core.extractPhysicalNets(doc, lookup);
  const drc = core.runDrc(doc, lookup);
  const lvs = core.runLvs(doc, lookup);

  // A handful of routing requests, chosen deterministically from the board itself.
  const routes = [];
  const pts = [
    [{ col: 1, row: 1 }, { col: 5, row: 1 }],
    [{ col: 2, row: 4 }, { col: 9, row: 8 }],
    [{ col: 0, row: 0 }, { col: doc.board.cols - 1, row: doc.board.rows - 1 }],
  ];
  for (const [from, to] of pts) {
    const r = core.routeConnection(doc, lookup, { from, to });
    routes.push({
      from,
      to,
      ok: r.ok,
      strategy: r.best?.strategy ?? null,
      cost: r.best ? Number(r.best.cost.toFixed(6)) : null,
      path: r.best?.conductors[0]?.path ?? null,
      riskHoles: r.best?.riskHoles ?? [],
      alternatives: r.alternatives.map((a) => a.strategy),
    });
  }

  // Occupancy is the counterpart to connectivity — what is physically in the way rather
  // than what is electrically joined — and the router depends on it. Without a fixture
  // its port would rest on hand-written tests alone, and once the TypeScript source is
  // deleted that proof can never be produced again.
  const occ = core.buildOccupancy(doc, lookup);
  const occupancy = occ.occupiedHoles().map((hole) => {
    const pin = occ.pinAt(hole);
    return {
      hole,
      pin: pin ? { componentRef: pin.componentRef, pin: pin.pin } : null,
      bottom: occ.conductorsAt(hole, 'bottom'),
      top: occ.conductorsAt(hole, 'top'),
      blockedBottom: occ.isCopperBlocked(hole, 'bottom'),
      blockedTop: occ.isCopperBlocked(hole, 'top'),
      bodyCovers: occ.bodyCovers(hole) ?? null,
    };
  });

  return {
    occupancy,
    physicalNets: physical.map((n) => ({
      id: n.id,
      nodes: n.nodes,
      pins: n.pins,
      conductorIds: n.conductorIds,
    })),
    drc: drc.map((v) => ({
      rule: v.rule,
      severity: v.severity,
      holes: v.holes,
      componentIds: v.componentIds ?? [],
      conductorIds: v.conductorIds ?? [],
    })),
    lvs: {
      ok: lvs.ok,
      summary: lvs.summary,
      issues: lvs.issues.map((i) => ({
        kind: i.kind,
        netNames: i.netNames,
        pins: i.pins,
        physicalNetIds: i.physicalNetIds,
      })),
    },
    continuity: core.continuityChecks(doc),
    isolation: core.isolationChecks(doc),
    routes,
  };
}

// --- Cases ---------------------------------------------------------------------

const cases = [];
for (let seed = 1; seed <= 12; seed++) {
  cases.push({ name: `random-${String(seed).padStart(2, '0')}`, doc: makeCase(seed) });
}
cases.push({ name: 'dense', doc: makeCase(99, { cols: 40, rows: 28, parts: 16, traces: 10, wires: 8 }) });
cases.push({ name: 'sparse', doc: makeCase(7, { cols: 16, rows: 12, parts: 3, traces: 1, wires: 1 }) });

// A real circuit, imported through the real parser.
{
  const imported = parseKicadNetlist(NE555_ASTABLE_NETLIST);
  const bus = new core.CommandBus(
    core.createEmptyDocument(META, { ...core.DEFAULT_BOARD, cols: 32, rows: 22 }),
    core.createStandardRegistry(),
    { nextId: core.createIdGenerator() },
  );
  bus.dispatch('netlist.import', { nets: imported.nets });
  imported.components.forEach((c, i) => {
    const fpid = { U: 'dip-8', R: 'r-axial-4', C: 'c-elec-d5-p2', D: 'led-5mm', J: 'hdr-1x4' }[c.ref[0]] ?? 'r-axial-4';
    bus.dispatch('component.place', {
      ref: c.ref,
      value: c.value,
      footprintId: fpid,
      anchor: { col: 1 + (i % 4) * 7, row: 2 + Math.floor(i / 4) * 8 },
    });
  });
  cases.push({ name: 'ne555', doc: bus.document });
}

/**
 * Describe the document AS IT COMES BACK OFF DISK, not as it sits in memory.
 *
 * serializeDocument sorts components, conductors and nets by id for diff stability, so
 * a freshly built document and its reloaded self can differ in array ORDER. Most of the
 * engine does not care, but DRC's component-body-overlap reports the pair it found in
 * iteration order, so the two orderings give different — both correct — output.
 *
 * Describing the in-memory document would therefore have paired every .perf file with
 * expected output computed from an ordering nobody ever actually has: real documents
 * arrive by being loaded. The fixture would be self-inconsistent, and the port would be
 * verified against a situation that cannot occur.
 */
function roundTrip(doc) {
  const json = core.serializeDocument(doc);
  const reloaded = core.deserializeDocument(json);
  if (!reloaded.ok) {
    throw new Error(`fixture failed to round-trip: ${reloaded.code} ${reloaded.message}`);
  }
  const again = core.serializeDocument(reloaded.document);
  if (again !== json) {
    throw new Error('fixture round-trip is not byte-stable; the fixture would be a moving target');
  }
  return { doc: reloaded.document, json };
}

for (const entry of cases) {
  const { doc, json } = roundTrip(entry.doc);
  entry.doc = doc; // so the summary below reports on what was actually described
  writeFileSync(join(OUT, `${entry.name}.perf`), json, 'utf8');
  writeFileSync(
    join(OUT, `${entry.name}.expected.json`),
    JSON.stringify(describe(doc), null, 2) + '\n',
    'utf8',
  );
}

// The whole footprint registry, so the Python generators can be proved identical.
const registry = core.standardFootprints();
const footprints = {};
for (const [id, fp] of registry) footprints[id] = fp;
writeFileSync(join(OUT, 'footprints.expected.json'), JSON.stringify(footprints, null, 2) + '\n', 'utf8');

// Geometry edge cases, cheap to get subtly wrong.
const geometry = {
  holeRefs: Object.fromEntries(
    [0, 1, 25, 26, 51, 52, 701, 702, 703, 1000].map((col) => [col, core.coordToHoleRef({ col, row: 11 })]),
  ),
  transforms: [],
};
for (const rotation of [0, 90, 180, 270]) {
  for (const mirrored of [false, true]) {
    for (const [x, y] of [[3, 0], [0, 3], [3, 3], [-2, 5], [1, 0]]) {
      const t = core.transformOffset(x, y, rotation, mirrored);
      geometry.transforms.push({ x, y, rotation, mirrored, out: [t.x, t.y] });
    }
  }
}
writeFileSync(join(OUT, 'geometry.expected.json'), JSON.stringify(geometry, null, 2) + '\n', 'utf8');

console.log(`wrote ${cases.length} cases + footprints + geometry to ${OUT}`);
for (const { name, doc } of cases) {
  const d = describe(doc);
  console.log(
    `  ${name.padEnd(12)} ${String(doc.components.length).padStart(2)} parts  ` +
      `${String(doc.conductors.length).padStart(2)} cond  ` +
      `${String(d.physicalNets.length).padStart(3)} nets  ` +
      `${String(d.drc.length).padStart(3)} drc  ` +
      `${String(d.lvs.issues.length).padStart(2)} lvs`,
  );
}
console.log(`  footprints   ${registry.size}`);
