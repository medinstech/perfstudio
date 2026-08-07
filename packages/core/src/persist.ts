/**
 * The .perf project file format: turning a PerfDocument into a string, and a string
 * back into a PerfDocument (or a structured error).
 *
 * This module is PURE. It has no file system access and performs no I/O of any kind.
 * Reading and writing the .perf file from disk is the host's job — the desktop app,
 * the CLI, or the MCP server — never this module's.
 *
 * The format exists to be read by both humans and agents (PLAN.md §9.3), and that
 * drives every decision here:
 *
 *  - GIT-DIFFABLE. Keys are emitted in a fixed, hand-declared order per object type
 *    (never JS object insertion order), with 2-space indentation and a trailing
 *    newline. `components`, `conductors` and `nets` are sorted by `id`; `cuts` are
 *    sorted by hole then `id`. That sorting exists ONLY for diff stability — it
 *    carries no semantic meaning, unlike a conductor's `path`, whose order IS
 *    meaningful (it is the physical chain of holes) and is therefore never reordered.
 *    Moving one component should produce a tiny, readable diff.
 *  - HAND-EDITABLE. Deserialization is forgiving where it safely can be — older
 *    format versions upgrade through an explicit migration chain, and a solder-trace
 *    path with a diagonal step loads with a warning instead of locking the user out
 *    of their own file — and precise where it must not be: every structural error
 *    carries a `path` (e.g. "components[3].anchor.col") pointing at the exact
 *    offending value.
 */

import type {
  Board,
  BoardMaterial,
  BoardSide,
  BoardType,
  Conductor,
  ConductorKind,
  ComponentInstance,
  DocumentMeta,
  HoleCoord,
  LeadBendConductor,
  Net,
  NetClass,
  NetNode,
  PerfDocument,
  Rotation,
  SolderBuildup,
  SolderTraceConductor,
  StripConductor,
  TrackCut,
  WireConductor,
} from './model.js';
import { DOCUMENT_FORMAT_VERSION } from './model.js';
import { validateOrthogonalChain } from './geometry.js';

/** Re-exported so callers of persist.ts don't also need to import model.ts. */
export const CURRENT_FORMAT_VERSION: number = DOCUMENT_FORMAT_VERSION;

// ---------------------------------------------------------------------------
// Public result types
// ---------------------------------------------------------------------------

export interface DeserializeOk {
  readonly ok: true;
  readonly document: PerfDocument;
  readonly warnings: readonly string[];
}

export interface DeserializeErr {
  readonly ok: false;
  readonly code: string;
  readonly message: string;
  readonly path?: string;
}

// ---------------------------------------------------------------------------
// JSON value plumbing
// ---------------------------------------------------------------------------

type JsonPrimitive = string | number | boolean;
type JsonValue = JsonPrimitive | readonly JsonValue[] | JsonObj;
type JsonObj = { [key: string]: JsonValue };

/**
 * Builds a plain object with keys inserted in exactly `order`, skipping any key
 * whose value is `undefined`. This is the mechanism behind every stable-key-order
 * guarantee in this file: callers never rely on the shape or insertion order of the
 * input, only on this explicit, hand-written array.
 */
function buildOrdered<K extends string>(order: readonly K[], values: Partial<Record<K, JsonValue | undefined>>): JsonObj {
  const obj: JsonObj = {};
  for (const key of order) {
    const v = values[key];
    if (v !== undefined) {
      obj[key] = v;
    }
  }
  return obj;
}

/**
 * Validates and normalizes a number for serialization.
 *
 * JSON.stringify silently turns NaN and Infinity into the text `null` — a real trap,
 * since the resulting file parses fine but has quietly lost data. This throws instead,
 * with a path pointing at the offending field. It also normalizes -0 to 0: they are
 * numerically identical everywhere, but -0 is a nuisance under strict/deep-equality
 * checks and has no business surviving a round trip through a hand-edited file.
 */
function num(path: string, n: number): number {
  if (!Number.isFinite(n)) {
    throw new Error(
      `Cannot serialize non-finite number at ${path}: ${n}. ` +
        `PerfStudio documents must contain only finite numbers (JSON.stringify would ` +
        `otherwise silently write "null" here).`,
    );
  }
  return Object.is(n, -0) ? 0 : n;
}

// ---------------------------------------------------------------------------
// Path helpers, shared by serialize (error paths) and deserialize (error paths)
// ---------------------------------------------------------------------------

function fieldPath(parent: string, key: string): string {
  return parent === '' ? key : `${parent}.${key}`;
}

function indexPath(parent: string, i: number): string {
  return `${parent}[${i}]`;
}

// ---------------------------------------------------------------------------
// Sorting — for diff stability only, never semantic
// ---------------------------------------------------------------------------

/** ASCII-safe string compare — avoids locale-dependent ordering across platforms. */
function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function byId<T extends { readonly id: string }>(a: T, b: T): number {
  return compareStrings(a.id, b.id);
}

/**
 * Cuts have no other natural ordering, so they sort by hole position (row-major: row
 * then col, matching the reading order documented on HoleCoord in model.ts) and fall
 * back to `id` only to break a tie between two cuts on the same hole. Purely for
 * predictable diffs — cuts are independent of one another.
 */
function byHoleThenId(a: TrackCut, b: TrackCut): number {
  if (a.at.row !== b.at.row) return a.at.row - b.at.row;
  if (a.at.col !== b.at.col) return a.at.col - b.at.col;
  return compareStrings(a.id, b.id);
}

// ---------------------------------------------------------------------------
// Key order declarations — the single source of truth for field order
// ---------------------------------------------------------------------------

const DOCUMENT_KEY_ORDER = ['formatVersion', 'meta', 'board', 'components', 'conductors', 'cuts', 'nets'] as const;
const META_KEY_ORDER = ['name', 'created', 'modified'] as const;
const BOARD_KEY_ORDER = [
  'type',
  'cols',
  'rows',
  'pitch',
  'thickness',
  'material',
  'padDiameter',
  'drillDiameter',
  'stripAxis',
] as const;
const HOLE_KEY_ORDER = ['col', 'row'] as const;
const COMPONENT_KEY_ORDER = ['id', 'ref', 'value', 'footprintId', 'anchor', 'rotation', 'mirrored', 'locked'] as const;
const CONDUCTOR_KEY_ORDER = [
  'id',
  'kind',
  'path',
  'side',
  'netId',
  'layerZ',
  'buildup',
  'spine',
  'gaugeAwg',
  'color',
  'componentId',
  'pinNumber',
] as const;
const SPINE_KEY_ORDER = ['material', 'gauge'] as const;
const CUT_KEY_ORDER = ['id', 'at'] as const;
const NET_KEY_ORDER = ['id', 'name', 'nodes', 'class', 'currentA', 'voltageV'] as const;
const NET_NODE_KEY_ORDER = ['componentRef', 'pin'] as const;

type ConductorFieldKey = (typeof CONDUCTOR_KEY_ORDER)[number];
type NetFieldKey = (typeof NET_KEY_ORDER)[number];

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

function orderedHole(h: HoleCoord, path: string): JsonObj {
  return buildOrdered(HOLE_KEY_ORDER, {
    col: num(fieldPath(path, 'col'), h.col),
    row: num(fieldPath(path, 'row'), h.row),
  });
}

function orderedBoard(b: Board): JsonObj {
  const path = 'board';
  return buildOrdered(BOARD_KEY_ORDER, {
    type: b.type,
    cols: num(fieldPath(path, 'cols'), b.cols),
    rows: num(fieldPath(path, 'rows'), b.rows),
    pitch: num(fieldPath(path, 'pitch'), b.pitch),
    thickness: num(fieldPath(path, 'thickness'), b.thickness),
    material: b.material,
    padDiameter: num(fieldPath(path, 'padDiameter'), b.padDiameter),
    drillDiameter: num(fieldPath(path, 'drillDiameter'), b.drillDiameter),
    stripAxis: b.stripAxis,
  });
}

function orderedComponent(c: ComponentInstance, index: number): JsonObj {
  const path = indexPath('components', index);
  return buildOrdered(COMPONENT_KEY_ORDER, {
    id: c.id,
    ref: c.ref,
    value: c.value,
    footprintId: c.footprintId,
    anchor: orderedHole(c.anchor, fieldPath(path, 'anchor')),
    rotation: num(fieldPath(path, 'rotation'), c.rotation),
    mirrored: c.mirrored,
    locked: c.locked,
  });
}

function orderedConductor(c: Conductor, index: number): JsonObj {
  const path = indexPath('conductors', index);
  const pathFieldPath = fieldPath(path, 'path');

  const values: Partial<Record<ConductorFieldKey, JsonValue | undefined>> = {
    id: c.id,
    kind: c.kind,
    path: c.path.map((h, i) => orderedHole(h, indexPath(pathFieldPath, i))),
    side: c.side,
    netId: c.netId,
    layerZ: num(fieldPath(path, 'layerZ'), c.layerZ),
  };

  if (c.kind === 'solder-trace' || c.kind === 'solder-trace-wired') {
    values.buildup = c.buildup;
    if (c.spine !== undefined) {
      const spinePath = fieldPath(path, 'spine');
      values.spine = buildOrdered(SPINE_KEY_ORDER, {
        material: c.spine.material,
        gauge: num(fieldPath(spinePath, 'gauge'), c.spine.gauge),
      });
    }
  } else if (c.kind === 'bare-wire' || c.kind === 'insulated-wire' || c.kind === 'top-jumper') {
    values.gaugeAwg = c.gaugeAwg;
    values.color = c.color;
  } else if (c.kind === 'lead-bend') {
    values.componentId = c.componentId;
    values.pinNumber = c.pinNumber;
  }
  // 'strip' has no fields beyond the base.

  return buildOrdered(CONDUCTOR_KEY_ORDER, values);
}

function orderedCut(c: TrackCut, index: number): JsonObj {
  const path = indexPath('cuts', index);
  return buildOrdered(CUT_KEY_ORDER, {
    id: c.id,
    at: orderedHole(c.at, fieldPath(path, 'at')),
  });
}

function orderedNet(n: Net, index: number): JsonObj {
  const path = indexPath('nets', index);
  const values: Partial<Record<NetFieldKey, JsonValue | undefined>> = {
    id: n.id,
    name: n.name,
    nodes: n.nodes.map((node) =>
      buildOrdered(NET_NODE_KEY_ORDER, { componentRef: node.componentRef, pin: node.pin }),
    ),
    class: n.class,
    currentA: n.currentA === undefined ? undefined : num(fieldPath(path, 'currentA'), n.currentA),
    voltageV: n.voltageV === undefined ? undefined : num(fieldPath(path, 'voltageV'), n.voltageV),
  };
  return buildOrdered(NET_KEY_ORDER, values);
}

/**
 * Serializes a document to its .perf text form: pretty-printed JSON, fixed key order,
 * diff-stable array sorting, trailing newline. Throws if the document contains a
 * non-finite number (see {@link num}) rather than silently writing `null`.
 */
export function serializeDocument(doc: PerfDocument): string {
  const components = [...doc.components].sort(byId);
  const conductors = [...doc.conductors].sort(byId);
  const cuts = [...doc.cuts].sort(byHoleThenId);
  const nets = [...doc.nets].sort(byId);

  const root = buildOrdered(DOCUMENT_KEY_ORDER, {
    formatVersion: num('formatVersion', doc.formatVersion),
    meta: buildOrdered(META_KEY_ORDER, {
      name: doc.meta.name,
      created: doc.meta.created,
      modified: doc.meta.modified,
    }),
    board: orderedBoard(doc.board),
    components: components.map((c, i) => orderedComponent(c, i)),
    conductors: conductors.map((c, i) => orderedConductor(c, i)),
    cuts: cuts.map((c, i) => orderedCut(c, i)),
    nets: nets.map((n, i) => orderedNet(n, i)),
  });

  return `${JSON.stringify(root, null, 2)}\n`;
}

// ---------------------------------------------------------------------------
// Deserialization: structural validation
// ---------------------------------------------------------------------------

/** Internal-only: carries a machine-readable code and a precise path to the failure. */
class ValidationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly path: string,
  ) {
    super(message);
    this.name = 'ValidationError';
  }
}

function describeType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function expectObject(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new ValidationError('not-an-object', `Expected an object at "${path}", got ${describeType(value)}.`, path);
  }
  return value as Record<string, unknown>;
}

function expectArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new ValidationError('not-an-array', `Expected an array at "${path}", got ${describeType(value)}.`, path);
  }
  return value;
}

function expectString(value: unknown, path: string): string {
  if (typeof value !== 'string') {
    throw new ValidationError('invalid-type', `Expected a string at "${path}", got ${describeType(value)}.`, path);
  }
  return value;
}

function expectBoolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ValidationError('invalid-type', `Expected a boolean at "${path}", got ${describeType(value)}.`, path);
  }
  return value;
}

function expectNumber(value: unknown, path: string): number {
  if (typeof value !== 'number') {
    throw new ValidationError('invalid-type', `Expected a number at "${path}", got ${describeType(value)}.`, path);
  }
  if (!Number.isFinite(value)) {
    throw new ValidationError('invalid-value', `Expected a finite number at "${path}", got ${value}.`, path);
  }
  return value;
}

function expectInteger(value: unknown, path: string): number {
  const n = expectNumber(value, path);
  if (!Number.isInteger(n)) {
    throw new ValidationError('invalid-value', `Expected an integer at "${path}", got ${n}.`, path);
  }
  return n;
}

function expectEnum<T extends string>(value: unknown, path: string, allowed: readonly T[]): T {
  const s = expectString(value, path);
  if (!(allowed as readonly string[]).includes(s)) {
    const options = allowed.map((a) => `"${a}"`).join(', ');
    throw new ValidationError('invalid-value', `Expected one of ${options} at "${path}", got ${JSON.stringify(s)}.`, path);
  }
  return s as T;
}

function requireField(obj: Record<string, unknown>, key: string, parentPath: string): unknown {
  if (!(key in obj) || obj[key] === undefined) {
    throw new ValidationError('missing-field', `Missing required field "${key}".`, fieldPath(parentPath, key));
  }
  return obj[key];
}

/** Emits a warning (not an error) naming any property not part of the schema at `path`. */
function checkUnknownKeys(obj: Record<string, unknown>, known: readonly string[], path: string, warnings: string[]): void {
  for (const key of Object.keys(obj)) {
    if (!known.includes(key)) {
      warnings.push(`Unknown property "${fieldPath(path, key)}" was ignored.`);
    }
  }
}

const BOARD_TYPES: readonly BoardType[] = ['pad-per-hole', 'stripboard', 'plain'];
const BOARD_MATERIALS: readonly BoardMaterial[] = ['FR4', 'FR2', 'FR1'];
const STRIP_AXES = ['horizontal', 'vertical'] as const;
const ROTATIONS: readonly Rotation[] = [0, 90, 180, 270];
const BOARD_SIDES: readonly BoardSide[] = ['top', 'bottom'];
const SOLDER_BUILDUPS: readonly SolderBuildup[] = ['light', 'normal', 'heavy'];
const SPINE_MATERIALS = ['tinned-copper', 'lead-offcut'] as const;
const CONDUCTOR_KINDS: readonly ConductorKind[] = [
  'lead-bend',
  'solder-trace',
  'solder-trace-wired',
  'bare-wire',
  'insulated-wire',
  'top-jumper',
  'strip',
];
const NET_CLASSES: readonly NetClass[] = ['power', 'ground', 'signal'];

function parseHole(raw: unknown, path: string, warnings: string[]): HoleCoord {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, HOLE_KEY_ORDER, path, warnings);
  return {
    col: expectInteger(requireField(obj, 'col', path), fieldPath(path, 'col')),
    row: expectInteger(requireField(obj, 'row', path), fieldPath(path, 'row')),
  };
}

function parseRotation(value: unknown, path: string): Rotation {
  const n = expectInteger(value, path);
  const match = ROTATIONS.find((r) => r === n);
  if (match === undefined) {
    throw new ValidationError('invalid-value', `Expected rotation to be one of 0, 90, 180, 270 at "${path}", got ${n}.`, path);
  }
  return match;
}

function parseMeta(raw: unknown, warnings: string[]): DocumentMeta {
  const path = 'meta';
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, META_KEY_ORDER, path, warnings);
  return {
    name: expectString(requireField(obj, 'name', path), fieldPath(path, 'name')),
    created: expectString(requireField(obj, 'created', path), fieldPath(path, 'created')),
    modified: expectString(requireField(obj, 'modified', path), fieldPath(path, 'modified')),
  };
}

function parseBoard(raw: unknown, warnings: string[]): Board {
  const path = 'board';
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, BOARD_KEY_ORDER, path, warnings);

  const stripAxisRaw = obj['stripAxis'];
  const stripAxis = stripAxisRaw === undefined ? undefined : expectEnum(stripAxisRaw, fieldPath(path, 'stripAxis'), STRIP_AXES);

  return {
    type: expectEnum(requireField(obj, 'type', path), fieldPath(path, 'type'), BOARD_TYPES),
    cols: expectInteger(requireField(obj, 'cols', path), fieldPath(path, 'cols')),
    rows: expectInteger(requireField(obj, 'rows', path), fieldPath(path, 'rows')),
    pitch: expectNumber(requireField(obj, 'pitch', path), fieldPath(path, 'pitch')),
    thickness: expectNumber(requireField(obj, 'thickness', path), fieldPath(path, 'thickness')),
    material: expectEnum(requireField(obj, 'material', path), fieldPath(path, 'material'), BOARD_MATERIALS),
    padDiameter: expectNumber(requireField(obj, 'padDiameter', path), fieldPath(path, 'padDiameter')),
    drillDiameter: expectNumber(requireField(obj, 'drillDiameter', path), fieldPath(path, 'drillDiameter')),
    ...(stripAxis !== undefined ? { stripAxis } : {}),
  };
}

function parseComponent(raw: unknown, path: string, warnings: string[]): ComponentInstance {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, COMPONENT_KEY_ORDER, path, warnings);
  return {
    id: expectString(requireField(obj, 'id', path), fieldPath(path, 'id')),
    ref: expectString(requireField(obj, 'ref', path), fieldPath(path, 'ref')),
    value: expectString(requireField(obj, 'value', path), fieldPath(path, 'value')),
    footprintId: expectString(requireField(obj, 'footprintId', path), fieldPath(path, 'footprintId')),
    anchor: parseHole(requireField(obj, 'anchor', path), fieldPath(path, 'anchor'), warnings),
    rotation: parseRotation(requireField(obj, 'rotation', path), fieldPath(path, 'rotation')),
    mirrored: expectBoolean(requireField(obj, 'mirrored', path), fieldPath(path, 'mirrored')),
    locked: expectBoolean(requireField(obj, 'locked', path), fieldPath(path, 'locked')),
  };
}

/**
 * Checks a solder-trace path against the orthogonal-chain invariant — solder cannot
 * reliably span a diagonal gap (PLAN.md §4.6, and the ConductorBase.path doc comment
 * in model.ts).
 *
 * Deliberately a warning rather than an error: a hand-edited file must still load, so
 * the user sees the problem in DRC instead of being locked out of their own project.
 */
function validateSolderTraceChain(c: SolderTraceConductor, path: string, warnings: string[]): void {
  const result = validateOrthogonalChain(c.path);
  if (result.ok) return;
  warnings.push(
    `${indexPath(fieldPath(path, 'path'), result.index)}: ${result.reason} ` +
      `The document still loaded — this will be reported by DRC.`,
  );
}

function parseConductor(raw: unknown, path: string, warnings: string[]): Conductor {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, CONDUCTOR_KEY_ORDER, path, warnings);

  const kind = expectEnum(requireField(obj, 'kind', path), fieldPath(path, 'kind'), CONDUCTOR_KINDS);
  const id = expectString(requireField(obj, 'id', path), fieldPath(path, 'id'));
  const pathFieldPath = fieldPath(path, 'path');
  const pathRaw = expectArray(requireField(obj, 'path', path), pathFieldPath);
  const holePath = pathRaw.map((h, i) => parseHole(h, indexPath(pathFieldPath, i), warnings));
  const side = expectEnum(requireField(obj, 'side', path), fieldPath(path, 'side'), BOARD_SIDES);
  const netIdRaw = obj['netId'];
  const netId = netIdRaw === undefined ? undefined : expectString(netIdRaw, fieldPath(path, 'netId'));
  const layerZ = expectNumber(requireField(obj, 'layerZ', path), fieldPath(path, 'layerZ'));

  switch (kind) {
    case 'solder-trace':
    case 'solder-trace-wired': {
      if (side !== 'bottom') {
        throw new ValidationError(
          'invalid-value',
          `Conductors of kind "${kind}" must have side "bottom", got "${side}".`,
          fieldPath(path, 'side'),
        );
      }
      const buildup = expectEnum(requireField(obj, 'buildup', path), fieldPath(path, 'buildup'), SOLDER_BUILDUPS);
      const spineRaw = obj['spine'];
      let spine: SolderTraceConductor['spine'];
      if (spineRaw !== undefined) {
        const spinePath = fieldPath(path, 'spine');
        const spineObj = expectObject(spineRaw, spinePath);
        checkUnknownKeys(spineObj, SPINE_KEY_ORDER, spinePath, warnings);
        spine = {
          material: expectEnum(requireField(spineObj, 'material', spinePath), fieldPath(spinePath, 'material'), SPINE_MATERIALS),
          gauge: expectNumber(requireField(spineObj, 'gauge', spinePath), fieldPath(spinePath, 'gauge')),
        };
      }
      const conductor: SolderTraceConductor = {
        id,
        kind,
        path: holePath,
        side: 'bottom',
        layerZ,
        buildup,
        ...(netId !== undefined ? { netId } : {}),
        ...(spine !== undefined ? { spine } : {}),
      };
      validateSolderTraceChain(conductor, path, warnings);
      return conductor;
    }
    case 'bare-wire':
    case 'insulated-wire':
    case 'top-jumper': {
      const gaugeAwgRaw = obj['gaugeAwg'];
      const gaugeAwg = gaugeAwgRaw === undefined ? undefined : expectNumber(gaugeAwgRaw, fieldPath(path, 'gaugeAwg'));
      const colorRaw = obj['color'];
      const color = colorRaw === undefined ? undefined : expectString(colorRaw, fieldPath(path, 'color'));
      const conductor: WireConductor = {
        id,
        kind,
        path: holePath,
        side,
        layerZ,
        ...(netId !== undefined ? { netId } : {}),
        ...(gaugeAwg !== undefined ? { gaugeAwg } : {}),
        ...(color !== undefined ? { color } : {}),
      };
      return conductor;
    }
    case 'lead-bend': {
      if (side !== 'bottom') {
        throw new ValidationError(
          'invalid-value',
          `Conductors of kind "lead-bend" must have side "bottom", got "${side}".`,
          fieldPath(path, 'side'),
        );
      }
      const componentId = expectString(requireField(obj, 'componentId', path), fieldPath(path, 'componentId'));
      const pinNumber = expectString(requireField(obj, 'pinNumber', path), fieldPath(path, 'pinNumber'));
      const conductor: LeadBendConductor = {
        id,
        kind,
        path: holePath,
        side: 'bottom',
        layerZ,
        componentId,
        pinNumber,
        ...(netId !== undefined ? { netId } : {}),
      };
      return conductor;
    }
    case 'strip': {
      const conductor: StripConductor = {
        id,
        kind,
        path: holePath,
        side,
        layerZ,
        ...(netId !== undefined ? { netId } : {}),
      };
      return conductor;
    }
    default: {
      throw new ValidationError('invalid-value', 'Unhandled conductor kind.', fieldPath(path, 'kind'));
    }
  }
}

function parseCut(raw: unknown, path: string, warnings: string[]): TrackCut {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, CUT_KEY_ORDER, path, warnings);
  return {
    id: expectString(requireField(obj, 'id', path), fieldPath(path, 'id')),
    at: parseHole(requireField(obj, 'at', path), fieldPath(path, 'at'), warnings),
  };
}

function parseNetNode(raw: unknown, path: string, warnings: string[]): NetNode {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, NET_NODE_KEY_ORDER, path, warnings);
  return {
    componentRef: expectString(requireField(obj, 'componentRef', path), fieldPath(path, 'componentRef')),
    pin: expectString(requireField(obj, 'pin', path), fieldPath(path, 'pin')),
  };
}

function parseNet(raw: unknown, path: string, warnings: string[]): Net {
  const obj = expectObject(raw, path);
  checkUnknownKeys(obj, NET_KEY_ORDER, path, warnings);
  const nodesPath = fieldPath(path, 'nodes');
  const nodesRaw = expectArray(requireField(obj, 'nodes', path), nodesPath);
  const nodes = nodesRaw.map((n, i) => parseNetNode(n, indexPath(nodesPath, i), warnings));
  const currentARaw = obj['currentA'];
  const currentA = currentARaw === undefined ? undefined : expectNumber(currentARaw, fieldPath(path, 'currentA'));
  const voltageVRaw = obj['voltageV'];
  const voltageV = voltageVRaw === undefined ? undefined : expectNumber(voltageVRaw, fieldPath(path, 'voltageV'));
  return {
    id: expectString(requireField(obj, 'id', path), fieldPath(path, 'id')),
    name: expectString(requireField(obj, 'name', path), fieldPath(path, 'name')),
    nodes,
    class: expectEnum(requireField(obj, 'class', path), fieldPath(path, 'class'), NET_CLASSES),
    ...(currentA !== undefined ? { currentA } : {}),
    ...(voltageV !== undefined ? { voltageV } : {}),
  };
}

// ---------------------------------------------------------------------------
// Format migrations
// ---------------------------------------------------------------------------

interface Migration {
  readonly fromVersion: number;
  readonly toVersion: number;
  readonly migrate: (doc: Record<string, unknown>) => Record<string, unknown>;
}

/**
 * Ordered chain of migrations, oldest first. Empty today because format version 1 is
 * the only version that has ever existed. The seam is built now, on purpose, so that
 * the day format 2 exists there is somewhere to put its migration — retrofitting a
 * migration chain after users already have version-1 files on disk is how projects
 * lose data.
 */
const MIGRATIONS: readonly Migration[] = [];

function migrate(doc: Record<string, unknown>, fromVersion: number): Record<string, unknown> {
  let current = doc;
  let version = fromVersion;
  for (const step of MIGRATIONS) {
    if (version === step.fromVersion) {
      current = step.migrate(current);
      version = step.toVersion;
    }
  }
  return current;
}

// ---------------------------------------------------------------------------
// Top-level parse
// ---------------------------------------------------------------------------

function parseDocument(rawInput: unknown): { readonly document: PerfDocument; readonly warnings: string[] } {
  const warnings: string[] = [];
  const root = expectObject(rawInput, '');

  const formatVersion = expectInteger(requireField(root, 'formatVersion', ''), 'formatVersion');
  if (formatVersion > CURRENT_FORMAT_VERSION) {
    throw new ValidationError(
      'format-too-new',
      `This file was saved by a newer version of PerfStudio (file format ${formatVersion}); ` +
        `this build understands up to format ${CURRENT_FORMAT_VERSION}. Please upgrade PerfStudio to open it.`,
      'formatVersion',
    );
  }

  const migrated = migrate(root, formatVersion);
  checkUnknownKeys(migrated, DOCUMENT_KEY_ORDER, '', warnings);

  const meta = parseMeta(requireField(migrated, 'meta', ''), warnings);
  const board = parseBoard(requireField(migrated, 'board', ''), warnings);

  const componentsRaw = expectArray(requireField(migrated, 'components', ''), 'components');
  const components = componentsRaw.map((item, i) => parseComponent(item, indexPath('components', i), warnings));

  const conductorsRaw = expectArray(requireField(migrated, 'conductors', ''), 'conductors');
  const conductors = conductorsRaw.map((item, i) => parseConductor(item, indexPath('conductors', i), warnings));

  const cutsRaw = expectArray(requireField(migrated, 'cuts', ''), 'cuts');
  const cuts = cutsRaw.map((item, i) => parseCut(item, indexPath('cuts', i), warnings));

  const netsRaw = expectArray(requireField(migrated, 'nets', ''), 'nets');
  const nets = netsRaw.map((item, i) => parseNet(item, indexPath('nets', i), warnings));

  const document: PerfDocument = {
    formatVersion: CURRENT_FORMAT_VERSION,
    meta,
    board,
    components,
    conductors,
    cuts,
    nets,
  };
  return { document, warnings };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Parses and validates a .perf file's contents. Never throws: JSON syntax errors and
 * structural problems both come back as a `DeserializeErr` with a machine-readable
 * `code` and, wherever the failure can be localized, a `path` such as
 * `"components[3].anchor.col"`. Non-fatal issues (an unknown property, a solder-trace
 * path with a diagonal step) are reported as `warnings` on a successful result rather
 * than blocking the load — a hand-edited file should still open.
 */
export function deserializeDocument(json: string): DeserializeOk | DeserializeErr {
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch (err) {
    return {
      ok: false,
      code: 'invalid-json',
      message: `Could not parse file as JSON: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  try {
    const { document, warnings } = parseDocument(raw);
    return { ok: true, document, warnings };
  } catch (err) {
    if (err instanceof ValidationError) {
      return { ok: false, code: err.code, message: err.message, path: err.path };
    }
    throw err;
  }
}

/** Throwing variant of {@link deserializeDocument} for callers that want it. */
export function parseDocumentOrThrow(json: string): PerfDocument {
  const result = deserializeDocument(json);
  if (!result.ok) {
    throw new Error(`${result.code}: ${result.message}${result.path !== undefined ? ` (at ${result.path})` : ''}`);
  }
  return result.document;
}
