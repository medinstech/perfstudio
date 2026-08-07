/**
 * Shared minimal fixtures for this package's own test suite. Not used by any
 * runtime rendering code.
 */

import type { Board, ComponentInstance, Footprint, FootprintPin, PerfDocument } from '@perfstudio/core';

export function makeBoard(overrides: Partial<Board> = {}): Board {
  return {
    type: 'pad-per-hole',
    cols: 10,
    rows: 6,
    pitch: 2.54,
    thickness: 1.6,
    material: 'FR4',
    padDiameter: 1.9,
    drillDiameter: 0.9,
    ...overrides,
  };
}

export function makeDocument(overrides: Partial<PerfDocument> = {}): PerfDocument {
  return {
    formatVersion: 1,
    meta: { name: 'test', created: '2026-01-01T00:00:00Z', modified: '2026-01-01T00:00:00Z' },
    board: makeBoard(),
    components: [],
    conductors: [],
    cuts: [],
    nets: [],
    ...overrides,
  };
}

export function makeFootprint(overrides: Partial<Footprint> = {}): Footprint {
  const defaultPins: readonly FootprintPin[] = [
    { number: '1', dCol: 0, dRow: 0 },
    { number: '2', dCol: 1, dRow: 0 },
  ];
  return {
    id: 'fp-test',
    name: 'Test Footprint',
    pins: defaultPins,
    bodyOutline: [
      { x: -1, y: -1 },
      { x: 3.5, y: -1 },
      { x: 3.5, y: 1 },
      { x: -1, y: 1 },
    ],
    bodyHeight: 3,
    body: { archetype: 'generic-box', dims: {} },
    leadDiameter: 0.5,
    polarized: false,
    ...overrides,
  };
}

export function makeComponent(overrides: Partial<ComponentInstance> = {}): ComponentInstance {
  return {
    id: 'comp-1',
    ref: 'R1',
    value: '1k',
    footprintId: 'fp-test',
    anchor: { col: 0, row: 0 },
    rotation: 0,
    mirrored: false,
    locked: false,
    ...overrides,
  };
}
