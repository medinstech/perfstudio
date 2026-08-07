import { describe, expect, it } from 'vitest';

import { CommandBus, createIdGenerator, replay } from './command.js';
import type { CommandRecord } from './command.js';
import {
  DEFAULT_BOARD,
  createEmptyDocument,
  createStandardRegistry,
  STANDARD_COMMANDS,
} from './commands.js';
import type { DocumentMeta, PerfDocument, SolderTraceConductor } from './model.js';

const META: DocumentMeta = {
  name: 'test',
  created: '2026-01-01T00:00:00.000Z',
  modified: '2026-01-01T00:00:00.000Z',
};

function newBus(doc?: PerfDocument): CommandBus {
  return new CommandBus(
    doc ?? createEmptyDocument(META),
    createStandardRegistry(),
    { nextId: createIdGenerator() },
  );
}

function placeR1(bus: CommandBus, col = 2, row = 2) {
  return bus.dispatch('component.place', {
    ref: 'R1',
    value: '10k',
    footprintId: 'r-axial-5',
    anchor: { col, row },
  });
}

// ---------------------------------------------------------------------------

describe('registry', () => {
  it('registers every standard command exactly once', () => {
    const registry = createStandardRegistry();
    expect(registry.types()).toHaveLength(STANDARD_COMMANDS.length);
    const unique = new Set(STANDARD_COMMANDS.map((c) => c.type));
    expect(unique.size).toBe(STANDARD_COMMANDS.length);
  });

  it('rejects an unknown command type without throwing', () => {
    const bus = newBus();
    const result = bus.dispatch('component.teleport', {});
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('unknown-command');
  });
});

describe('component.place', () => {
  it('places a component and generates a deterministic id', () => {
    const bus = newBus();
    const result = placeR1(bus);
    expect(result.ok).toBe(true);
    expect(bus.document.components).toHaveLength(1);
    expect(bus.document.components[0]?.id).toBe('cmp-1');
    expect(bus.document.components[0]?.rotation).toBe(0);
    if (result.ok) expect(result.description).toBe('Place R1 at C3');
  });

  it('refuses a duplicate ref', () => {
    const bus = newBus();
    placeR1(bus);
    const again = bus.dispatch('component.place', {
      ref: 'R1',
      value: '4k7',
      footprintId: 'r-axial-5',
      anchor: { col: 9, row: 9 },
    });
    expect(again.ok).toBe(false);
    if (!again.ok) expect(again.code).toBe('duplicate-ref');
    expect(bus.document.components).toHaveLength(1);
  });

  it('refuses an anchor off the board', () => {
    const bus = newBus();
    const result = bus.dispatch('component.place', {
      ref: 'R9',
      value: '1k',
      footprintId: 'r-axial-5',
      anchor: { col: DEFAULT_BOARD.cols, row: 0 },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('off-board');
  });

  it('refuses an invalid rotation', () => {
    const bus = newBus();
    const result = bus.dispatch('component.place', {
      ref: 'R2',
      value: '1k',
      footprintId: 'r-axial-5',
      anchor: { col: 1, row: 1 },
      rotation: 45 as 90,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('invalid-rotation');
  });
});

describe('component mutation and locking', () => {
  it('moves, rotates and mirrors', () => {
    const bus = newBus();
    placeR1(bus);
    bus.dispatch('component.move', { id: 'cmp-1', anchor: { col: 5, row: 6 } });
    bus.dispatch('component.rotate', { id: 'cmp-1', rotation: 90 });
    bus.dispatch('component.mirror', { id: 'cmp-1', mirrored: true });
    const c = bus.document.components[0];
    expect(c?.anchor).toEqual({ col: 5, row: 6 });
    expect(c?.rotation).toBe(90);
    expect(c?.mirrored).toBe(true);
  });

  it('refuses to move a locked component', () => {
    const bus = newBus();
    placeR1(bus);
    bus.dispatch('component.update', { id: 'cmp-1', locked: true });
    const result = bus.dispatch('component.move', { id: 'cmp-1', anchor: { col: 8, row: 8 } });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('component-locked');
  });

  it('deleting a component also removes its lead bends but keeps other routing', () => {
    const bus = newBus();
    placeR1(bus);
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'lead-bend',
        path: [{ col: 2, row: 2 }, { col: 4, row: 2 }],
        side: 'bottom',
        layerZ: 0,
        componentId: 'cmp-1',
        pinNumber: '1',
      },
    });
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'bare-wire',
        path: [{ col: 10, row: 10 }, { col: 14, row: 10 }],
        side: 'bottom',
        layerZ: 0,
      },
    });
    expect(bus.document.conductors).toHaveLength(2);

    bus.dispatch('component.delete', { id: 'cmp-1' });
    expect(bus.document.components).toHaveLength(0);
    expect(bus.document.conductors).toHaveLength(1);
    expect(bus.document.conductors[0]?.kind).toBe('bare-wire');
  });
});

describe('conductor.add', () => {
  it('accepts an orthogonal solder trace', () => {
    const bus = newBus();
    const result = bus.dispatch('conductor.add', {
      conductor: {
        kind: 'solder-trace',
        path: [
          { col: 1, row: 1 },
          { col: 2, row: 1 },
          { col: 3, row: 1 },
        ],
        side: 'bottom',
        layerZ: 0,
        buildup: 'normal',
      },
    });
    expect(result.ok).toBe(true);
    const c = bus.document.conductors[0] as SolderTraceConductor | undefined;
    expect(c?.kind).toBe('solder-trace');
    expect(c?.buildup).toBe('normal');
  });

  it('REFUSES a solder trace with a diagonal step', () => {
    const bus = newBus();
    const result = bus.dispatch('conductor.add', {
      conductor: {
        kind: 'solder-trace',
        path: [
          { col: 1, row: 1 },
          { col: 2, row: 2 },
        ],
        side: 'bottom',
        layerZ: 0,
        buildup: 'normal',
      },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('non-orthogonal-path');
  });

  it('allows a diagonal insulated wire, which is physically fine', () => {
    const bus = newBus();
    const result = bus.dispatch('conductor.add', {
      conductor: {
        kind: 'insulated-wire',
        path: [
          { col: 1, row: 1 },
          { col: 9, row: 7 },
        ],
        side: 'bottom',
        layerZ: 1,
      },
    });
    expect(result.ok).toBe(true);
  });

  it('refuses a lead-bend referencing a component that does not exist', () => {
    const bus = newBus();
    const result = bus.dispatch('conductor.add', {
      conductor: {
        kind: 'lead-bend',
        path: [{ col: 1, row: 1 }, { col: 2, row: 1 }],
        side: 'bottom',
        layerZ: 0,
        componentId: 'nope',
        pinNumber: '1',
      },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('component-not-found');
  });

  it('refuses a path that leaves the board', () => {
    const bus = newBus();
    const result = bus.dispatch('conductor.add', {
      conductor: {
        kind: 'bare-wire',
        path: [{ col: 0, row: 0 }, { col: DEFAULT_BOARD.cols + 5, row: 0 }],
        side: 'bottom',
        layerZ: 0,
      },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('off-board');
  });
});

describe('board.set', () => {
  it('refuses a shrink that would strand a placed component', () => {
    const bus = newBus();
    bus.dispatch('component.place', {
      ref: 'U1',
      value: 'NE555',
      footprintId: 'dip-8',
      anchor: { col: 50, row: 30 },
    });
    const result = bus.dispatch('board.set', { board: { ...DEFAULT_BOARD, cols: 20, rows: 20 } });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe('would-strand-component');
      expect(result.message).toContain('U1');
    }
  });

  it('refuses a drill diameter that is not smaller than the pad', () => {
    const bus = newBus();
    const result = bus.dispatch('board.set', {
      board: { ...DEFAULT_BOARD, padDiameter: 1.0, drillDiameter: 1.0 },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('invalid-board');
  });
});

describe('cut.add', () => {
  it('is refused on a pad-per-hole board', () => {
    const bus = newBus();
    const result = bus.dispatch('cut.add', { at: { col: 3, row: 3 } });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('not-stripboard');
  });

  it('is allowed on stripboard', () => {
    const bus = newBus(
      createEmptyDocument(META, { ...DEFAULT_BOARD, type: 'stripboard', stripAxis: 'horizontal' }),
    );
    const result = bus.dispatch('cut.add', { at: { col: 3, row: 3 } });
    expect(result.ok).toBe(true);
    expect(bus.document.cuts).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// The guarantees the command bus exists to provide
// ---------------------------------------------------------------------------

describe('undo/redo', () => {
  it('restores the exact previous document, by reference', () => {
    const bus = newBus();
    const before = bus.document;
    placeR1(bus);
    const after = bus.document;
    expect(after).not.toBe(before);

    bus.undo();
    expect(bus.document).toBe(before); // identity, not just deep equality

    bus.redo();
    expect(bus.document).toBe(after);
  });

  it('a failed command leaves history untouched', () => {
    const bus = newBus();
    placeR1(bus);
    const historyBefore = bus.history().length;
    bus.dispatch('component.move', { id: 'does-not-exist', anchor: { col: 1, row: 1 } });
    expect(bus.history()).toHaveLength(historyBefore);
    expect(bus.canRedo()).toBe(false);
  });

  it('dispatching after an undo clears the redo stack', () => {
    const bus = newBus();
    placeR1(bus);
    bus.undo();
    expect(bus.canRedo()).toBe(true);
    bus.dispatch('component.place', {
      ref: 'R2',
      value: '1k',
      footprintId: 'r-axial-5',
      anchor: { col: 7, row: 7 },
    });
    expect(bus.canRedo()).toBe(false);
  });
});

describe('deterministic replay', () => {
  it('replaying a journal onto a fresh document reproduces it exactly', () => {
    const bus = newBus();
    placeR1(bus);
    bus.dispatch('component.place', {
      ref: 'C1',
      value: '100nF',
      footprintId: 'c-disc-1',
      anchor: { col: 6, row: 2 },
    });
    bus.dispatch('component.move', { id: 'cmp-1', anchor: { col: 3, row: 4 } });
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'solder-trace-wired',
        path: [
          { col: 3, row: 8 },
          { col: 4, row: 8 },
          { col: 5, row: 8 },
        ],
        side: 'bottom',
        layerZ: 0,
        buildup: 'heavy',
        spine: { material: 'tinned-copper', gauge: 0.6 },
      },
    });

    const journal: readonly CommandRecord[] = bus.journal();
    expect(journal).toHaveLength(4);

    // Fresh id generator: ids must come out the same, which is the whole point.
    const replayed = replay(
      createEmptyDocument(META),
      journal,
      createStandardRegistry(),
      { nextId: createIdGenerator() },
    );

    expect(replayed.ok).toBe(true);
    if (replayed.ok) expect(replayed.document).toEqual(bus.document);
  });

  it('replay surfaces the first failing command rather than continuing', () => {
    const registry = createStandardRegistry();
    const journal: CommandRecord[] = [
      { type: 'component.place', payload: { ref: 'R1', value: '1k', footprintId: 'f', anchor: { col: 1, row: 1 } } },
      { type: 'component.move', payload: { id: 'nope', anchor: { col: 2, row: 2 } } },
    ];
    const result = replay(createEmptyDocument(META), journal, registry, {
      nextId: createIdGenerator(),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.code).toBe('component-not-found');
  });
});

describe('immutability', () => {
  it('never mutates the previous document', () => {
    const bus = newBus();
    const before = bus.document;
    const snapshot = JSON.stringify(before);
    placeR1(bus);
    bus.dispatch('conductor.add', {
      conductor: {
        kind: 'bare-wire',
        path: [{ col: 1, row: 1 }, { col: 5, row: 1 }],
        side: 'bottom',
        layerZ: 0,
      },
    });
    expect(JSON.stringify(before)).toBe(snapshot);
  });
});
