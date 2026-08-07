/**
 * PerfStudio domain model.
 *
 * This file is the architectural spine: every other package builds on these types.
 * Design rules encoded here:
 *
 *  1. The document is IMMUTABLE. Mutations happen only by dispatching a Command
 *     (see `command.ts`), never by writing to a document in place.
 *  2. Holes are addressed by 0-indexed {col,row}. The human-facing spreadsheet
 *     form ("A1", "AC12") is a rendering of that, used by the soldering guide.
 *  3. A conductor is not just "a wire". Perfboard has six physically distinct ways
 *     to make a connection, each with its own cost, constraints and failure modes.
 *     Modelling that distinction is the core idea of this project (PLAN.md §4.4).
 */

// ---------------------------------------------------------------------------
// Units
// ---------------------------------------------------------------------------

/** Millimetres. The document is metric throughout; imperial is a display concern. */
export type Mm = number

/** Standard perfboard hole pitch. */
export const STANDARD_PITCH_MM: Mm = 2.54;

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

/**
 * A hole position on the board grid, 0-indexed from the top-left.
 * `col` increases to the right, `row` increases downward — matching both the
 * canvas coordinate system and the way a board is read when held component-side up.
 */
export interface HoleCoord {
  readonly col: number;
  readonly row: number;
}

/**
 * Human-facing hole address: column letters + 1-indexed row, e.g. "A1", "AC12".
 * This is the language the soldering guide speaks, so it is a first-class concept
 * rather than a formatting detail.
 */
export type HoleRef = string;

export interface Point2 {
  readonly x: Mm;
  readonly y: Mm;
}

/** Which physical face of the board something lives on. */
export type BoardSide =
  /** Component side — where bodies sit. */
  | 'top'
  /** Solder side — where solder traces and most wiring live. */
  | 'bottom';

export type Rotation = 0 | 90 | 180 | 270;

// ---------------------------------------------------------------------------
// Board
// ---------------------------------------------------------------------------

export type BoardType =
  /** Each hole has its own isolated copper pad. The v1 target. */
  | 'pad-per-hole'
  /** Continuous copper strips; connections are free along a strip but need cuts. */
  | 'stripboard'
  /** Bare phenolic/glass board, no copper at all. Point-to-point only. */
  | 'plain';

/**
 * Substrate material. This is not cosmetic: FR-2 phenolic paper (cheap "pertinaks")
 * lifts pads under sustained heat far more readily than FR-4, which bounds how long
 * a pure solder trace may be (PLAN.md §5.2 R5'').
 */
export type BoardMaterial = 'FR4' | 'FR2' | 'FR1';

export interface Board {
  readonly type: BoardType;
  readonly cols: number;
  readonly rows: number;
  readonly pitch: Mm;
  readonly thickness: Mm;
  readonly material: BoardMaterial;
  readonly padDiameter: Mm;
  readonly drillDiameter: Mm;
  /** Stripboard only: the axis the copper strips run along. */
  readonly stripAxis?: 'horizontal' | 'vertical';
}

// ---------------------------------------------------------------------------
// Footprints and component bodies
// ---------------------------------------------------------------------------

export interface FootprintPin {
  readonly number: string;
  readonly name?: string;
  /** Hole offset from the component anchor, in grid steps (not mm). */
  readonly dCol: number;
  readonly dRow: number;
}

/**
 * Parameters for procedurally generating the 3D body (PLAN.md D6).
 * We generate ~25 body archetypes rather than shipping a mesh library, which keeps
 * assets at zero, guarantees the 3D body agrees with the footprint, and avoids
 * inheriting a share-alike asset licence.
 */
export interface BodySpec {
  readonly archetype: BodyArchetype;
  /** Archetype-specific dimensions in mm, e.g. { length: 6.3, diameter: 2.4 }. */
  readonly dims: Readonly<Record<string, Mm>>;
  readonly color?: string;
}

export type BodyArchetype =
  | 'axial-cylinder'      // resistors, DO-41 diodes
  | 'radial-electrolytic'
  | 'disc-ceramic'
  | 'box-film'
  | 'dip'
  | 'to92'
  | 'to220'
  | 'led-round'
  | 'pin-header'
  | 'screw-terminal'
  | 'potentiometer'
  | 'tactile-switch'
  | 'crystal-hc49'
  | 'relay-box'
  | 'generic-box';

export interface Footprint {
  readonly id: string;
  readonly name: string;
  readonly pins: readonly FootprintPin[];
  /** Body outline in mm, relative to the anchor hole centre. Used for courtyard DRC. */
  readonly bodyOutline: readonly Point2[];
  /** Height above the board surface, for clearance DRC and 3D. */
  readonly bodyHeight: Mm;
  readonly body: BodySpec;
  readonly leadDiameter: Mm;
  readonly polarized: boolean;
}

// ---------------------------------------------------------------------------
// Component instances
// ---------------------------------------------------------------------------

export type ComponentId = string;

export interface ComponentInstance {
  readonly id: ComponentId;
  /** Designator, e.g. "R1". Matches the schematic netlist. */
  readonly ref: string;
  readonly value: string;
  readonly footprintId: string;
  /** Hole the footprint's origin sits on. */
  readonly anchor: HoleCoord;
  readonly rotation: Rotation;
  readonly mirrored: boolean;
  readonly locked: boolean;
}

// ---------------------------------------------------------------------------
// Conductors — the heart of the model (PLAN.md §4.4, §4.6)
// ---------------------------------------------------------------------------

export type ConductorKind =
  /** A component lead bent over to reach a nearby hole. Effectively free. */
  | 'lead-bend'
  /** TR "lehim yolu": adjacent pads joined with solder alone. */
  | 'solder-trace'
  /** A solder trace reinforced with a tinned-wire or lead-offcut spine. */
  | 'solder-trace-wired'
  /** Bare/tinned wire on the solder side. Cannot cross another bare conductor. */
  | 'bare-wire'
  /** Insulated wire. May cross other conductors, at a preparation cost. */
  | 'insulated-wire'
  /** Insulated jumper routed over the component side. */
  | 'top-jumper'
  /** Stripboard's pre-existing copper strip (v2). */
  | 'strip';

export type ConductorId = string;

/** How much solder has been built up, which sets the effective cross-section. */
export type SolderBuildup = 'light' | 'normal' | 'heavy';

export interface ConductorBase {
  readonly id: ConductorId;
  readonly kind: ConductorKind;
  /**
   * Ordered chain of holes this conductor passes through.
   * INVARIANT for 'solder-trace' and 'solder-trace-wired': consecutive entries must
   * be 4-neighbours (orthogonally adjacent). Solder cannot reliably span a diagonal
   * gap — see PLAN.md §4.6.
   */
  readonly path: readonly HoleCoord[];
  readonly side: BoardSide;
  /** Assigned net, once routing has associated this conductor with one. */
  readonly netId?: NetId;
  /**
   * Physical stacking level on its side, so overlapping conductors can be drawn and
   * collision-checked without ambiguity. 0 = directly on the board surface.
   */
  readonly layerZ: number;
}

export interface SolderTraceConductor extends ConductorBase {
  readonly kind: 'solder-trace' | 'solder-trace-wired';
  readonly side: 'bottom';
  readonly buildup: SolderBuildup;
  /** Present iff kind is 'solder-trace-wired'. */
  readonly spine?: {
    readonly material: 'tinned-copper' | 'lead-offcut';
    readonly gauge: Mm;
  };
}

export interface WireConductor extends ConductorBase {
  readonly kind: 'bare-wire' | 'insulated-wire' | 'top-jumper';
  readonly gaugeAwg?: number;
  /** Insulation colour, used by the cut list and the guide's colour convention. */
  readonly color?: string;
}

export interface LeadBendConductor extends ConductorBase {
  readonly kind: 'lead-bend';
  readonly side: 'bottom';
  /** The component whose lead this is. */
  readonly componentId: ComponentId;
  readonly pinNumber: string;
}

export interface StripConductor extends ConductorBase {
  readonly kind: 'strip';
}

export type Conductor =
  | SolderTraceConductor
  | WireConductor
  | LeadBendConductor
  | StripConductor;

/** Stripboard track cut (v2). */
export interface TrackCut {
  readonly id: string;
  readonly at: HoleCoord;
}

// ---------------------------------------------------------------------------
// Nets
// ---------------------------------------------------------------------------

export type NetId = string;

export type NetClass = 'power' | 'ground' | 'signal';

/** A node in the schematic netlist: one pin of one component. */
export interface NetNode {
  readonly componentRef: string;
  readonly pin: string;
}

/**
 * A net as declared by the schematic. This is the *intent*; what the board actually
 * connects is derived from the conductors by the connectivity engine. Comparing the
 * two is LVS (PLAN.md §5.1).
 */
export interface Net {
  readonly id: NetId;
  readonly name: string;
  readonly nodes: readonly NetNode[];
  readonly class: NetClass;
  /** Expected current, if the user supplied it. Drives current-capacity DRC. */
  readonly currentA?: number;
  /** Nominal voltage, if supplied. Drives creepage DRC. */
  readonly voltageV?: number;
}

// ---------------------------------------------------------------------------
// Document
// ---------------------------------------------------------------------------

/** Bumped whenever a migration is needed. Persisted in the project file. */
export const DOCUMENT_FORMAT_VERSION = 1;

export interface DocumentMeta {
  readonly name: string;
  /** ISO 8601. Set by the host, never by core (core must stay deterministic). */
  readonly created: string;
  readonly modified: string;
}

export interface PerfDocument {
  readonly formatVersion: number;
  readonly meta: DocumentMeta;
  readonly board: Board;
  readonly components: readonly ComponentInstance[];
  readonly conductors: readonly Conductor[];
  readonly cuts: readonly TrackCut[];
  /** Schematic intent, imported from a netlist. Empty until a netlist is loaded. */
  readonly nets: readonly Net[];
}

// ---------------------------------------------------------------------------
// Type guards
// ---------------------------------------------------------------------------

export function isSolderTrace(c: Conductor): c is SolderTraceConductor {
  return c.kind === 'solder-trace' || c.kind === 'solder-trace-wired';
}

export function isWire(c: Conductor): c is WireConductor {
  return c.kind === 'bare-wire' || c.kind === 'insulated-wire' || c.kind === 'top-jumper';
}

export function isLeadBend(c: Conductor): c is LeadBendConductor {
  return c.kind === 'lead-bend';
}

/**
 * Conductors that physically occupy the copper plane and therefore cannot cross
 * one another. Insulated wire and top jumpers are excluded: they may pass over.
 */
export function isCrossingBlocked(c: Conductor): boolean {
  return (
    c.kind === 'solder-trace' ||
    c.kind === 'solder-trace-wired' ||
    c.kind === 'bare-wire' ||
    c.kind === 'lead-bend' ||
    c.kind === 'strip'
  );
}
