/**
 * Command bus.
 *
 * The single rule of this architecture (PLAN.md §8.1): the UI never mutates the
 * document directly. Every action — from the GUI, the CLI, the MCP server or a
 * replayed journal — is a command that goes through this bus.
 *
 * Four things fall out of that for free:
 *   - undo/redo
 *   - macro recording
 *   - deterministic replay tests
 *   - an agent and a user driving the same document concurrently
 *
 * Core stays deterministic: no Date.now(), no Math.random(), no I/O in here. Ids and
 * timestamps are injected through CommandContext so replays reproduce exactly.
 */

import type { PerfDocument } from './model.js';

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

/**
 * Host-supplied capabilities a command may need. Injected rather than imported so
 * that replaying a journal produces byte-identical documents.
 */
export interface CommandContext {
  /** Deterministic id source, e.g. nextId('cond') -> 'cond-7'. */
  readonly nextId: (prefix: string) => string;
}

/** Deterministic, monotonically increasing id source. */
export function createIdGenerator(start = 0): CommandContext['nextId'] {
  const counters = new Map<string, number>();
  return (prefix: string): string => {
    const n = (counters.get(prefix) ?? start) + 1;
    counters.set(prefix, n);
    return `${prefix}-${n}`;
  };
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

/** A command as it travels over the wire and gets written to the journal. */
export interface CommandRecord<TPayload = unknown> {
  readonly type: string;
  readonly payload: TPayload;
}

/**
 * Raised by a command handler when the requested change is not valid. Carries a
 * machine-readable code so the MCP server and CLI can report it structurally
 * instead of parsing prose.
 */
export class CommandError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'CommandError';
  }
}

export interface CommandDefinition<TPayload> {
  readonly type: string;
  /**
   * Pure. Returns a new document; must not mutate `doc`. Throws CommandError if the
   * payload is invalid against the current document.
   */
  apply(doc: PerfDocument, payload: TPayload, ctx: CommandContext): PerfDocument;
  /**
   * Human-readable one-liner. Doubles as the undo-stack label and as source material
   * for the soldering guide, so write it in the imperative: "Place R1 at C7".
   */
  describe(payload: TPayload, doc: PerfDocument): string;
  /** Optional payload validation, run before apply. */
  validate?(payload: unknown): payload is TPayload;
}

export type DispatchResult =
  | { readonly ok: true; readonly document: PerfDocument; readonly description: string }
  | { readonly ok: false; readonly code: string; readonly message: string };

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export class CommandRegistry {
  readonly #defs = new Map<string, CommandDefinition<never>>();

  register<TPayload>(def: CommandDefinition<TPayload>): this {
    if (this.#defs.has(def.type)) {
      throw new Error(`Duplicate command type: ${def.type}`);
    }
    this.#defs.set(def.type, def as CommandDefinition<never>);
    return this;
  }

  get(type: string): CommandDefinition<never> | undefined {
    return this.#defs.get(type);
  }

  types(): readonly string[] {
    return [...this.#defs.keys()].sort();
  }
}

// ---------------------------------------------------------------------------
// Bus
// ---------------------------------------------------------------------------

interface HistoryEntry {
  readonly record: CommandRecord;
  /**
   * Document snapshots. Cheap because documents are immutable and commands replace
   * only the arrays they touch, so unchanged subtrees are shared by reference.
   */
  readonly before: PerfDocument;
  readonly after: PerfDocument;
  readonly description: string;
}

export type BusListener = (doc: PerfDocument, entry: HistoryEntry | null) => void;

export class CommandBus {
  #document: PerfDocument;
  readonly #registry: CommandRegistry;
  readonly #ctx: CommandContext;
  readonly #undoStack: HistoryEntry[] = [];
  readonly #redoStack: HistoryEntry[] = [];
  readonly #listeners = new Set<BusListener>();

  constructor(initial: PerfDocument, registry: CommandRegistry, ctx: CommandContext) {
    this.#document = initial;
    this.#registry = registry;
    this.#ctx = ctx;
  }

  get document(): PerfDocument {
    return this.#document;
  }

  dispatch<TPayload>(type: string, payload: TPayload): DispatchResult {
    const def = this.#registry.get(type) as CommandDefinition<TPayload> | undefined;
    if (!def) {
      return { ok: false, code: 'unknown-command', message: `Unknown command: ${type}` };
    }
    if (def.validate && !def.validate(payload)) {
      return { ok: false, code: 'invalid-payload', message: `Invalid payload for ${type}` };
    }

    const before = this.#document;
    let after: PerfDocument;
    try {
      after = def.apply(before, payload, this.#ctx);
    } catch (err) {
      if (err instanceof CommandError) {
        return { ok: false, code: err.code, message: err.message };
      }
      throw err;
    }

    const description = def.describe(payload, before);
    const entry: HistoryEntry = { record: { type, payload }, before, after, description };

    this.#document = after;
    this.#undoStack.push(entry);
    this.#redoStack.length = 0;
    this.#emit(entry);

    return { ok: true, document: after, description };
  }

  canUndo(): boolean {
    return this.#undoStack.length > 0;
  }

  canRedo(): boolean {
    return this.#redoStack.length > 0;
  }

  undo(): PerfDocument {
    const entry = this.#undoStack.pop();
    if (!entry) return this.#document;
    this.#redoStack.push(entry);
    this.#document = entry.before;
    this.#emit(null);
    return this.#document;
  }

  redo(): PerfDocument {
    const entry = this.#redoStack.pop();
    if (!entry) return this.#document;
    this.#undoStack.push(entry);
    this.#document = entry.after;
    this.#emit(entry);
    return this.#document;
  }

  /** The command journal, for macro export and deterministic replay tests. */
  journal(): readonly CommandRecord[] {
    return this.#undoStack.map((e) => e.record);
  }

  /** Undo-stack labels, newest last. Used by the UI and by guide generation. */
  history(): readonly string[] {
    return this.#undoStack.map((e) => e.description);
  }

  subscribe(listener: BusListener): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #emit(entry: HistoryEntry | null): void {
    for (const l of this.#listeners) l(this.#document, entry);
  }
}

/**
 * Replay a journal onto a fresh document. Given the same initial document, the same
 * registry and a fresh id generator, this must reproduce the original document
 * exactly — the property the replay tests assert.
 */
export function replay(
  initial: PerfDocument,
  records: readonly CommandRecord[],
  registry: CommandRegistry,
  ctx: CommandContext,
): DispatchResult {
  const bus = new CommandBus(initial, registry, ctx);
  let last: DispatchResult = { ok: true, document: initial, description: 'initial' };
  for (const r of records) {
    last = bus.dispatch(r.type, r.payload);
    if (!last.ok) return last;
  }
  return last;
}
