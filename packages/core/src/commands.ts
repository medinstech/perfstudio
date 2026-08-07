/**
 * The standard command set.
 *
 * Every mutation of a PerfDocument is one of these. The GUI, the CLI, the MCP server
 * and a replayed journal all go through the same list, which is what keeps undo/redo,
 * macro recording and agent-driven editing consistent with each other (PLAN.md §8.1).
 *
 * DIVISION OF RESPONSIBILITY — worth being precise about, because the temptation is to
 * validate everything here:
 *
 *   Commands enforce DOCUMENT INTEGRITY. Ids are unique, references resolve, paths lie
 *   on the board, and the invariants declared in model.ts hold. A document that fails
 *   any of these is not a document, so these are hard errors and the mutation is
 *   refused.
 *
 *   DRC reports DESIGN QUALITY. Overlapping bodies, solder-trace proximity risk,
 *   inadequate current capacity. These are all legal documents that describe a board
 *   you probably do not want to build, so they are reported, not refused.
 *
 * That split is why commands need geometry but not the footprint library: whether a
 * part's pins land somewhere sensible is a design question, and DRC owns it.
 *
 * Nothing here touches `meta.modified`. Core is deterministic and has no clock; the
 * host stamps timestamps when it saves.
 */

import { CommandError, CommandRegistry } from './command.js';
import type { CommandDefinition } from './command.js';
import { coordToHoleRef, isInsideBoard, validateOrthogonalChain } from './geometry.js';
import type {
  Board,
  ComponentId,
  ComponentInstance,
  Conductor,
  ConductorId,
  DocumentMeta,
  HoleCoord,
  Net,
  PerfDocument,
  Rotation,
  TrackCut,
} from './model.js';
import { DOCUMENT_FORMAT_VERSION, isSolderTrace } from './model.js';

// ---------------------------------------------------------------------------
// Document construction
// ---------------------------------------------------------------------------

export const DEFAULT_BOARD: Board = {
  type: 'pad-per-hole',
  cols: 60,
  rows: 40,
  pitch: 2.54,
  thickness: 1.6,
  material: 'FR4',
  padDiameter: 1.9,
  drillDiameter: 1.0,
};

/**
 * A blank document. `meta.created`/`meta.modified` are supplied by the caller because
 * core must not read a clock — see the module note above.
 */
export function createEmptyDocument(meta: DocumentMeta, board: Board = DEFAULT_BOARD): PerfDocument {
  return {
    formatVersion: DOCUMENT_FORMAT_VERSION,
    meta,
    board,
    components: [],
    conductors: [],
    cuts: [],
    nets: [],
  };
}

// ---------------------------------------------------------------------------
// Shared validation helpers
// ---------------------------------------------------------------------------

const VALID_ROTATIONS: readonly Rotation[] = [0, 90, 180, 270];

function requireComponent(doc: PerfDocument, id: ComponentId): ComponentInstance {
  const found = doc.components.find((c) => c.id === id);
  if (!found) {
    throw new CommandError('component-not-found', `No component with id "${id}".`);
  }
  return found;
}

function requireConductor(doc: PerfDocument, id: ConductorId): Conductor {
  const found = doc.conductors.find((c) => c.id === id);
  if (!found) {
    throw new CommandError('conductor-not-found', `No conductor with id "${id}".`);
  }
  return found;
}

function assertRotation(rotation: number): asserts rotation is Rotation {
  if (!VALID_ROTATIONS.includes(rotation as Rotation)) {
    throw new CommandError(
      'invalid-rotation',
      `Rotation must be 0, 90, 180 or 270; got ${rotation}.`,
    );
  }
}

function assertHoleOnBoard(hole: HoleCoord, board: Board, what: string): void {
  if (!Number.isInteger(hole.col) || !Number.isInteger(hole.row)) {
    throw new CommandError('invalid-hole', `${what} must have integer col/row.`);
  }
  if (!isInsideBoard(hole, board)) {
    throw new CommandError(
      'off-board',
      `${what} ${coordToHoleRef(hole)} is outside the ${board.cols}x${board.rows} board.`,
    );
  }
}

/**
 * Path checks common to every conductor: on the board, non-empty, and — for solder
 * traces — an unbroken chain of orthogonal neighbours, since solder cannot reliably
 * span a diagonal gap (model.ts ConductorBase.path, PLAN.md §4.6).
 */
function assertValidPath(path: readonly HoleCoord[], kind: Conductor['kind'], board: Board): void {
  if (path.length < 2) {
    throw new CommandError('path-too-short', `A conductor path needs at least 2 holes.`);
  }
  for (const hole of path) {
    assertHoleOnBoard(hole, board, 'Conductor path hole');
  }
  if (kind === 'solder-trace' || kind === 'solder-trace-wired' || kind === 'strip') {
    const check = validateOrthogonalChain(path);
    if (!check.ok) {
      throw new CommandError('non-orthogonal-path', check.reason);
    }
  }
}

// ---------------------------------------------------------------------------
// Payloads
// ---------------------------------------------------------------------------

/** Distributes over the Conductor union so each variant keeps its own fields. */
type WithoutId<T> = T extends { id: string } ? Omit<T, 'id'> : never;
export type NewConductor = WithoutId<Conductor>;

export interface PlaceComponentPayload {
  readonly ref: string;
  readonly value: string;
  readonly footprintId: string;
  readonly anchor: HoleCoord;
  readonly rotation?: Rotation;
  readonly mirrored?: boolean;
  /** Supply to make placement reproducible (e.g. netlist import); otherwise generated. */
  readonly id?: ComponentId;
}

export interface MoveComponentPayload {
  readonly id: ComponentId;
  readonly anchor: HoleCoord;
}

export interface RotateComponentPayload {
  readonly id: ComponentId;
  readonly rotation: Rotation;
}

export interface MirrorComponentPayload {
  readonly id: ComponentId;
  readonly mirrored: boolean;
}

export interface UpdateComponentPayload {
  readonly id: ComponentId;
  readonly ref?: string;
  readonly value?: string;
  readonly locked?: boolean;
}

export interface DeleteComponentPayload {
  readonly id: ComponentId;
}

export interface AddConductorPayload {
  readonly conductor: NewConductor;
  readonly id?: ConductorId;
}

export interface SetConductorPathPayload {
  readonly id: ConductorId;
  readonly path: readonly HoleCoord[];
}

export interface DeleteConductorPayload {
  readonly id: ConductorId;
}

export interface SetBoardPayload {
  readonly board: Board;
}

export interface ImportNetlistPayload {
  readonly nets: readonly Net[];
}

export interface AddCutPayload {
  readonly at: HoleCoord;
  readonly id?: string;
}

export interface DeleteCutPayload {
  readonly id: string;
}

// ---------------------------------------------------------------------------
// Component commands
// ---------------------------------------------------------------------------

export const placeComponent: CommandDefinition<PlaceComponentPayload> = {
  type: 'component.place',
  apply(doc, p, ctx) {
    assertHoleOnBoard(p.anchor, doc.board, `Anchor for ${p.ref}`);
    const rotation = p.rotation ?? 0;
    assertRotation(rotation);

    if (doc.components.some((c) => c.ref === p.ref)) {
      throw new CommandError('duplicate-ref', `A component with ref "${p.ref}" already exists.`);
    }
    const id = p.id ?? ctx.nextId('cmp');
    if (doc.components.some((c) => c.id === id)) {
      throw new CommandError('duplicate-id', `A component with id "${id}" already exists.`);
    }

    const component: ComponentInstance = {
      id,
      ref: p.ref,
      value: p.value,
      footprintId: p.footprintId,
      anchor: p.anchor,
      rotation,
      mirrored: p.mirrored ?? false,
      locked: false,
    };
    return { ...doc, components: [...doc.components, component] };
  },
  describe: (p) => `Place ${p.ref} at ${coordToHoleRef(p.anchor)}`,
};

export const moveComponent: CommandDefinition<MoveComponentPayload> = {
  type: 'component.move',
  apply(doc, p) {
    const existing = requireComponent(doc, p.id);
    if (existing.locked) {
      throw new CommandError('component-locked', `${existing.ref} is locked.`);
    }
    assertHoleOnBoard(p.anchor, doc.board, `Anchor for ${existing.ref}`);
    return {
      ...doc,
      components: doc.components.map((c) => (c.id === p.id ? { ...c, anchor: p.anchor } : c)),
    };
  },
  describe(p, doc) {
    const c = doc.components.find((x) => x.id === p.id);
    return `Move ${c?.ref ?? p.id} to ${coordToHoleRef(p.anchor)}`;
  },
};

export const rotateComponent: CommandDefinition<RotateComponentPayload> = {
  type: 'component.rotate',
  apply(doc, p) {
    const existing = requireComponent(doc, p.id);
    if (existing.locked) {
      throw new CommandError('component-locked', `${existing.ref} is locked.`);
    }
    assertRotation(p.rotation);
    return {
      ...doc,
      components: doc.components.map((c) => (c.id === p.id ? { ...c, rotation: p.rotation } : c)),
    };
  },
  describe(p, doc) {
    const c = doc.components.find((x) => x.id === p.id);
    return `Rotate ${c?.ref ?? p.id} to ${p.rotation} degrees`;
  },
};

export const mirrorComponent: CommandDefinition<MirrorComponentPayload> = {
  type: 'component.mirror',
  apply(doc, p) {
    const existing = requireComponent(doc, p.id);
    if (existing.locked) {
      throw new CommandError('component-locked', `${existing.ref} is locked.`);
    }
    return {
      ...doc,
      components: doc.components.map((c) => (c.id === p.id ? { ...c, mirrored: p.mirrored } : c)),
    };
  },
  describe(p, doc) {
    const c = doc.components.find((x) => x.id === p.id);
    return `${p.mirrored ? 'Mirror' : 'Unmirror'} ${c?.ref ?? p.id}`;
  },
};

export const updateComponent: CommandDefinition<UpdateComponentPayload> = {
  type: 'component.update',
  apply(doc, p) {
    const existing = requireComponent(doc, p.id);
    if (p.ref !== undefined && p.ref !== existing.ref) {
      if (doc.components.some((c) => c.ref === p.ref)) {
        throw new CommandError('duplicate-ref', `A component with ref "${p.ref}" already exists.`);
      }
    }
    return {
      ...doc,
      components: doc.components.map((c) =>
        c.id === p.id
          ? {
              ...c,
              ref: p.ref ?? c.ref,
              value: p.value ?? c.value,
              locked: p.locked ?? c.locked,
            }
          : c,
      ),
    };
  },
  describe(p, doc) {
    const c = doc.components.find((x) => x.id === p.id);
    return `Update ${c?.ref ?? p.id}`;
  },
};

export const deleteComponent: CommandDefinition<DeleteComponentPayload> = {
  type: 'component.delete',
  apply(doc, p) {
    const existing = requireComponent(doc, p.id);
    if (existing.locked) {
      throw new CommandError('component-locked', `${existing.ref} is locked.`);
    }
    // Lead bends belong to the component; nothing else can own them, so they go too.
    // Wires and traces are deliberately left in place: they may still be wanted, and
    // silently deleting a user's routing is worse than leaving something dangling for
    // DRC and LVS to point at.
    return {
      ...doc,
      components: doc.components.filter((c) => c.id !== p.id),
      conductors: doc.conductors.filter(
        (c) => !(c.kind === 'lead-bend' && c.componentId === p.id),
      ),
    };
  },
  describe(p, doc) {
    const c = doc.components.find((x) => x.id === p.id);
    return `Delete ${c?.ref ?? p.id}`;
  },
};

// ---------------------------------------------------------------------------
// Conductor commands
// ---------------------------------------------------------------------------

export const addConductor: CommandDefinition<AddConductorPayload> = {
  type: 'conductor.add',
  apply(doc, p, ctx) {
    const spec = p.conductor;
    assertValidPath(spec.path, spec.kind, doc.board);

    if (spec.kind === 'lead-bend') {
      requireComponent(doc, spec.componentId);
    }
    if (isSolderTrace(spec as Conductor) && spec.side !== 'bottom') {
      throw new CommandError('invalid-side', 'Solder traces exist on the solder side only.');
    }

    const id = p.id ?? ctx.nextId('cond');
    if (doc.conductors.some((c) => c.id === id)) {
      throw new CommandError('duplicate-id', `A conductor with id "${id}" already exists.`);
    }

    const conductor = { ...spec, id } as Conductor;
    return { ...doc, conductors: [...doc.conductors, conductor] };
  },
  describe(p) {
    const path = p.conductor.path;
    const from = path[0];
    const to = path[path.length - 1];
    const span =
      from && to ? ` ${coordToHoleRef(from)} to ${coordToHoleRef(to)}` : '';
    return `Add ${p.conductor.kind}${span}`;
  },
};

export const setConductorPath: CommandDefinition<SetConductorPathPayload> = {
  type: 'conductor.setPath',
  apply(doc, p) {
    const existing = requireConductor(doc, p.id);
    assertValidPath(p.path, existing.kind, doc.board);
    return {
      ...doc,
      conductors: doc.conductors.map((c) =>
        c.id === p.id ? ({ ...c, path: p.path } as Conductor) : c,
      ),
    };
  },
  describe: (p) => `Reroute conductor ${p.id}`,
};

export const deleteConductor: CommandDefinition<DeleteConductorPayload> = {
  type: 'conductor.delete',
  apply(doc, p) {
    requireConductor(doc, p.id); // Throws if it does not exist.
    return { ...doc, conductors: doc.conductors.filter((c) => c.id !== p.id) };
  },
  describe(p, doc) {
    const c = doc.conductors.find((x) => x.id === p.id);
    return `Delete ${c?.kind ?? 'conductor'} ${p.id}`;
  },
};

// ---------------------------------------------------------------------------
// Board, netlist and cuts
// ---------------------------------------------------------------------------

export const setBoard: CommandDefinition<SetBoardPayload> = {
  type: 'board.set',
  apply(doc, p) {
    const b = p.board;
    if (!Number.isInteger(b.cols) || !Number.isInteger(b.rows) || b.cols < 1 || b.rows < 1) {
      throw new CommandError('invalid-board', 'Board must have at least 1 column and 1 row.');
    }
    if (!(b.pitch > 0) || !(b.padDiameter > 0) || !(b.drillDiameter > 0)) {
      throw new CommandError('invalid-board', 'Pitch and pad/drill diameters must be positive.');
    }
    if (b.drillDiameter >= b.padDiameter) {
      throw new CommandError('invalid-board', 'Drill diameter must be smaller than the pad.');
    }

    // Shrinking the board could strand parts outside it. Refuse rather than silently
    // dropping the user's work, and name the first offender so the message is useful.
    const strandedComponent = doc.components.find((c) => !isInsideBoard(c.anchor, b));
    if (strandedComponent) {
      throw new CommandError(
        'would-strand-component',
        `${strandedComponent.ref} at ${coordToHoleRef(strandedComponent.anchor)} would fall outside a ${b.cols}x${b.rows} board.`,
      );
    }
    for (const cond of doc.conductors) {
      const stranded = cond.path.find((h) => !isInsideBoard(h, b));
      if (stranded) {
        throw new CommandError(
          'would-strand-conductor',
          `Conductor ${cond.id} passes through ${coordToHoleRef(stranded)}, outside a ${b.cols}x${b.rows} board.`,
        );
      }
    }

    return { ...doc, board: b };
  },
  describe: (p) => `Set board to ${p.board.cols}x${p.board.rows} ${p.board.material}`,
};

export const importNetlist: CommandDefinition<ImportNetlistPayload> = {
  type: 'netlist.import',
  apply(doc, p) {
    const seen = new Set<string>();
    for (const net of p.nets) {
      if (seen.has(net.id)) {
        throw new CommandError('duplicate-net-id', `Duplicate net id "${net.id}".`);
      }
      seen.add(net.id);
    }
    // Replaces the schematic intent wholesale. Placement and routing are untouched:
    // re-importing after a schematic edit must not throw away the board.
    return { ...doc, nets: [...p.nets] };
  },
  describe: (p) => `Import netlist (${p.nets.length} nets)`,
};

export const addCut: CommandDefinition<AddCutPayload> = {
  type: 'cut.add',
  apply(doc, p, ctx) {
    if (doc.board.type !== 'stripboard') {
      throw new CommandError('not-stripboard', 'Track cuts only apply to stripboard.');
    }
    assertHoleOnBoard(p.at, doc.board, 'Cut');
    const id = p.id ?? ctx.nextId('cut');
    if (doc.cuts.some((c) => c.id === id)) {
      throw new CommandError('duplicate-id', `A cut with id "${id}" already exists.`);
    }
    const cut: TrackCut = { id, at: p.at };
    return { ...doc, cuts: [...doc.cuts, cut] };
  },
  describe: (p) => `Cut track at ${coordToHoleRef(p.at)}`,
};

export const deleteCut: CommandDefinition<DeleteCutPayload> = {
  type: 'cut.delete',
  apply(doc, p) {
    if (!doc.cuts.some((c) => c.id === p.id)) {
      throw new CommandError('cut-not-found', `No cut with id "${p.id}".`);
    }
    return { ...doc, cuts: doc.cuts.filter((c) => c.id !== p.id) };
  },
  describe: (p) => `Remove cut ${p.id}`,
};

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export const STANDARD_COMMANDS = [
  placeComponent,
  moveComponent,
  rotateComponent,
  mirrorComponent,
  updateComponent,
  deleteComponent,
  addConductor,
  setConductorPath,
  deleteConductor,
  setBoard,
  importNetlist,
  addCut,
  deleteCut,
] as const;

/** A registry with every standard command registered. */
export function createStandardRegistry(): CommandRegistry {
  const registry = new CommandRegistry();
  for (const def of STANDARD_COMMANDS) {
    registry.register(def as CommandDefinition<never>);
  }
  return registry;
}
