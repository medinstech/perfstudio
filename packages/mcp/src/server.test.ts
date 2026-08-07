import { describe, expect, it } from 'vitest';

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { McpError } from '@modelcontextprotocol/sdk/types.js';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';

import { DOCUMENT_FORMAT_VERSION } from '@perfstudio/core';
import type { PerfDocument } from '@perfstudio/core';

import { DocumentStore } from './document-store.js';
import { createServer, dispatchToolCall, TOOLS } from './server.js';

function buildDocument(): PerfDocument {
  return {
    formatVersion: DOCUMENT_FORMAT_VERSION,
    meta: { name: 'Fixture board', created: '2026-01-01T00:00:00.000Z', modified: '2026-01-01T00:00:00.000Z' },
    board: {
      type: 'pad-per-hole',
      cols: 20,
      rows: 15,
      pitch: 2.54,
      thickness: 1.6,
      material: 'FR4',
      padDiameter: 1.9,
      drillDiameter: 1.0,
    },
    components: [
      {
        id: 'comp-1',
        ref: 'R1',
        value: '10k',
        footprintId: 'axial-th',
        anchor: { col: 0, row: 0 },
        rotation: 0,
        mirrored: false,
        locked: false,
      },
      {
        id: 'comp-2',
        ref: 'R2',
        value: '4k7',
        footprintId: 'axial-th',
        anchor: { col: 5, row: 3 },
        rotation: 90,
        mirrored: false,
        locked: false,
      },
      {
        id: 'comp-3',
        ref: 'C1',
        value: '100nF',
        footprintId: 'disc-ceramic',
        anchor: { col: 2, row: 2 },
        rotation: 0,
        mirrored: false,
        locked: true,
      },
    ],
    conductors: [
      {
        id: 'cond-1',
        kind: 'lead-bend',
        side: 'bottom',
        componentId: 'comp-1',
        pinNumber: '1',
        path: [
          { col: 0, row: 0 },
          { col: 0, row: 1 },
        ],
        layerZ: 0,
      },
      {
        id: 'cond-2',
        kind: 'lead-bend',
        side: 'bottom',
        componentId: 'comp-2',
        pinNumber: '2',
        path: [
          { col: 5, row: 3 },
          { col: 6, row: 3 },
        ],
        layerZ: 0,
      },
      {
        id: 'cond-3',
        kind: 'solder-trace',
        side: 'bottom',
        buildup: 'normal',
        path: [
          { col: 0, row: 1 },
          { col: 1, row: 1 },
          { col: 2, row: 1 },
        ],
        layerZ: 0,
      },
      {
        id: 'cond-4',
        kind: 'bare-wire',
        side: 'bottom',
        path: [
          { col: 2, row: 1 },
          { col: 2, row: 2 },
        ],
        layerZ: 1,
      },
    ],
    cuts: [{ id: 'cut-1', at: { col: 3, row: 3 } }],
    nets: [
      {
        id: 'net-gnd',
        name: 'GND',
        class: 'ground',
        nodes: [
          { componentRef: 'R1', pin: '1' },
          { componentRef: 'R2', pin: '2' },
          { componentRef: 'C1', pin: '-' },
        ],
      },
      {
        id: 'net-vcc',
        name: 'VCC',
        class: 'power',
        nodes: [
          { componentRef: 'R1', pin: '2' },
          { componentRef: 'C1', pin: '+' },
        ],
        currentA: 0.5,
        voltageV: 5,
      },
    ],
  };
}

function parseData(result: CallToolResult): unknown {
  const first = result.content[0];
  if (!first || first.type !== 'text') {
    throw new Error('Expected a text content block');
  }
  return JSON.parse(first.text);
}

function parseError(result: CallToolResult): { code: string; message: string } {
  const parsed = parseData(result) as { error: { code: string; message: string } };
  return parsed.error;
}

describe('TOOLS', () => {
  it('registers exactly the five read-only tools', () => {
    expect(TOOLS.map((t) => t.name).sort()).toEqual(
      ['get_board_info', 'get_component', 'get_document_stats', 'get_nets', 'list_components'].sort(),
    );
  });

  it('gives every tool an object-typed JSON Schema and a non-empty description', () => {
    for (const tool of TOOLS) {
      expect(tool.inputSchema.type).toBe('object');
      expect(tool.description.length).toBeGreaterThan(0);
    }
  });
});

describe('createServer', () => {
  it('returns a proper Server instance without needing a transport', () => {
    const store = DocumentStore.fromDocument(buildDocument());
    const server = createServer(store);
    expect(server).toBeInstanceOf(Server);
  });
});

describe('dispatchToolCall', () => {
  const store = DocumentStore.fromDocument(buildDocument());

  it('get_board_info reports dimensions, pitch, material and hole count', () => {
    const result = dispatchToolCall(store, 'get_board_info', {});
    expect(result.isError).toBeUndefined();
    const data = parseData(result) as {
      type: string;
      holes: { cols: number; rows: number; count: number };
      dimensionsMm: { width: number; height: number };
      pitchMm: number;
      material: string;
      padDiameterMm: number;
      drillDiameterMm: number;
    };
    expect(data.type).toBe('pad-per-hole');
    expect(data.holes).toEqual({ cols: 20, rows: 15, count: 300 });
    expect(data.dimensionsMm).toEqual({ width: 20 * 2.54, height: 15 * 2.54 });
    expect(data.pitchMm).toBe(2.54);
    expect(data.material).toBe('FR4');
    expect(data.padDiameterMm).toBe(1.9);
    expect(data.drillDiameterMm).toBe(1.0);
  });

  it('list_components returns all components with both anchor forms', () => {
    const result = dispatchToolCall(store, 'list_components', {});
    const data = parseData(result) as Array<{ ref: string; anchor: { col: number; row: number }; anchorRef: string }>;
    expect(data).toHaveLength(3);
    const r1 = data.find((c) => c.ref === 'R1');
    expect(r1?.anchor).toEqual({ col: 0, row: 0 });
    expect(r1?.anchorRef).toBe('A1');
  });

  it('list_components filters by a case-insensitive ref/value substring', () => {
    const result = dispatchToolCall(store, 'list_components', { filter: 'r' });
    const data = parseData(result) as Array<{ ref: string }>;
    // R1, R2 match on ref; C1 matches nothing ("c1"/"100nf" have no "r"... wait "100nF" has no r either)
    expect(data.map((c) => c.ref).sort()).toEqual(['R1', 'R2']);
  });

  it('list_components rejects a non-string filter as an error result, not a throw', () => {
    const result = dispatchToolCall(store, 'list_components', { filter: 123 });
    expect(result.isError).toBe(true);
    expect(parseError(result).code).toBe('invalid-params');
  });

  it('get_component finds a component by ref', () => {
    const result = dispatchToolCall(store, 'get_component', { ref: 'C1' });
    expect(result.isError).toBeUndefined();
    const data = parseData(result) as { id: string; ref: string; value: string; locked: boolean };
    expect(data.id).toBe('comp-3');
    expect(data.value).toBe('100nF');
    expect(data.locked).toBe(true);
  });

  it('get_component finds a component by id', () => {
    const result = dispatchToolCall(store, 'get_component', { id: 'comp-2' });
    const data = parseData(result) as { ref: string; rotation: number };
    expect(data.ref).toBe('R2');
    expect(data.rotation).toBe(90);
  });

  it('get_component with an unknown ref returns an error result rather than throwing', () => {
    expect(() => dispatchToolCall(store, 'get_component', { ref: 'R99' })).not.toThrow();
    const result = dispatchToolCall(store, 'get_component', { ref: 'R99' });
    expect(result.isError).toBe(true);
    const error = parseError(result);
    expect(error.code).toBe('not-found');
    expect(error.message).toMatch(/R99/);
  });

  it('get_component with neither ref nor id returns an invalid-params error result', () => {
    const result = dispatchToolCall(store, 'get_component', {});
    expect(result.isError).toBe(true);
    expect(parseError(result).code).toBe('invalid-params');
  });

  it('get_nets reports node counts and full node lists', () => {
    const result = dispatchToolCall(store, 'get_nets', {});
    const data = parseData(result) as Array<{ name: string; nodeCount: number; nodes: unknown[]; currentA?: number }>;
    expect(data).toHaveLength(2);
    const gnd = data.find((n) => n.name === 'GND');
    expect(gnd?.nodeCount).toBe(3);
    expect(gnd?.nodes).toHaveLength(3);
    const vcc = data.find((n) => n.name === 'VCC');
    expect(vcc?.currentA).toBe(0.5);
  });

  it('get_nets filters by a case-insensitive name substring', () => {
    const result = dispatchToolCall(store, 'get_nets', { nameFilter: 'gn' });
    const data = parseData(result) as Array<{ name: string }>;
    expect(data.map((n) => n.name)).toEqual(['GND']);
  });

  it('get_document_stats counts components, nets, cuts and conductors by kind', () => {
    const result = dispatchToolCall(store, 'get_document_stats', {});
    const data = parseData(result) as {
      formatVersion: number;
      documentName: string;
      componentCount: number;
      netCount: number;
      cutCount: number;
      conductorCount: number;
      conductorsByKind: Record<string, number>;
    };
    expect(data.formatVersion).toBe(DOCUMENT_FORMAT_VERSION);
    expect(data.documentName).toBe('Fixture board');
    expect(data.componentCount).toBe(3);
    expect(data.netCount).toBe(2);
    expect(data.cutCount).toBe(1);
    expect(data.conductorCount).toBe(4);
    expect(data.conductorsByKind).toEqual({ 'lead-bend': 2, 'solder-trace': 1, 'bare-wire': 1 });
  });

  it('throws an McpError for an unknown tool name (a protocol-level problem, not a domain one)', () => {
    expect(() => dispatchToolCall(store, 'delete_everything', {})).toThrow(McpError);
  });
});
