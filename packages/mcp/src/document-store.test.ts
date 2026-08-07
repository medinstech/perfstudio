import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { DOCUMENT_FORMAT_VERSION } from '@perfstudio/core';
import type { PerfDocument } from '@perfstudio/core';

import { DocumentStore, DocumentStoreError } from './document-store.js';

function validDocument(overrides: Partial<PerfDocument> = {}): PerfDocument {
  return {
    formatVersion: DOCUMENT_FORMAT_VERSION,
    meta: { name: 'Test board', created: '2026-01-01T00:00:00.000Z', modified: '2026-01-01T00:00:00.000Z' },
    board: {
      type: 'pad-per-hole',
      cols: 10,
      rows: 8,
      pitch: 2.54,
      thickness: 1.6,
      material: 'FR4',
      padDiameter: 1.9,
      drillDiameter: 1.0,
    },
    components: [],
    conductors: [],
    cuts: [],
    nets: [],
    ...overrides,
  };
}

describe('DocumentStore', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'perfstudio-mcp-test-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('starts with a small default board when no path is given', () => {
    const store = DocumentStore.load({ argv: ['node', 'stdio.js'], env: {} });
    const doc = store.document;

    expect(doc.formatVersion).toBe(DOCUMENT_FORMAT_VERSION);
    expect(doc.board).toMatchObject({
      type: 'pad-per-hole',
      cols: 60,
      rows: 40,
      pitch: 2.54,
      material: 'FR4',
      padDiameter: 1.9,
      drillDiameter: 1.0,
      thickness: 1.6,
    });
    expect(doc.components).toEqual([]);
    expect(doc.conductors).toEqual([]);
    expect(doc.cuts).toEqual([]);
    expect(doc.nets).toEqual([]);
  });

  it('loads a document from an argv[2] path', () => {
    const path = join(dir, 'board.perf');
    writeFileSync(path, JSON.stringify(validDocument({ meta: { name: 'From file', created: 'x', modified: 'x' } })));

    const store = DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });

    expect(store.document.meta.name).toBe('From file');
  });

  it('falls back to the PERFSTUDIO_DOCUMENT env var when argv[2] is absent', () => {
    const path = join(dir, 'board.perf');
    writeFileSync(path, JSON.stringify(validDocument({ meta: { name: 'From env', created: 'x', modified: 'x' } })));

    const store = DocumentStore.load({ argv: ['node', 'stdio.js'], env: { PERFSTUDIO_DOCUMENT: path } });

    expect(store.document.meta.name).toBe('From env');
  });

  it('prefers argv[2] over the env var when both are given', () => {
    const argvPath = join(dir, 'from-argv.perf');
    const envPath = join(dir, 'from-env.perf');
    writeFileSync(argvPath, JSON.stringify(validDocument({ meta: { name: 'argv wins', created: 'x', modified: 'x' } })));
    writeFileSync(envPath, JSON.stringify(validDocument({ meta: { name: 'env loses', created: 'x', modified: 'x' } })));

    const store = DocumentStore.load({ argv: ['node', 'stdio.js', argvPath], env: { PERFSTUDIO_DOCUMENT: envPath } });

    expect(store.document.meta.name).toBe('argv wins');
  });

  it('reload() re-reads the backing file', () => {
    const path = join(dir, 'board.perf');
    writeFileSync(path, JSON.stringify(validDocument({ meta: { name: 'v1', created: 'x', modified: 'x' } })));
    const store = DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });
    expect(store.document.meta.name).toBe('v1');

    writeFileSync(path, JSON.stringify(validDocument({ meta: { name: 'v2', created: 'x', modified: 'x' } })));
    store.reload();

    expect(store.document.meta.name).toBe('v2');
  });

  it('reload() on the default in-memory document is a harmless no-op', () => {
    const store = DocumentStore.load({ argv: ['node', 'stdio.js'], env: {} });
    const before = store.document;
    store.reload();
    expect(store.document).toBe(before);
  });

  it('fromDocument() wraps an in-memory document directly', () => {
    const doc = validDocument({ meta: { name: 'Direct', created: 'x', modified: 'x' } });
    const store = DocumentStore.fromDocument(doc);
    expect(store.document).toBe(doc);
  });

  it('fails loudly on a formatVersion mismatch', () => {
    const path = join(dir, 'old.perf');
    writeFileSync(path, JSON.stringify(validDocument({ formatVersion: DOCUMENT_FORMAT_VERSION + 1 })));

    expect(() => DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} })).toThrow(DocumentStoreError);
    try {
      DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(DocumentStoreError);
      expect((err as DocumentStoreError).code).toBe('format-version-mismatch');
    }
  });

  it('fails loudly when the file does not exist', () => {
    const path = join(dir, 'missing.perf');
    try {
      DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(DocumentStoreError);
      expect((err as DocumentStoreError).code).toBe('file-read-failed');
    }
  });

  it('fails loudly on invalid JSON', () => {
    const path = join(dir, 'broken.perf');
    writeFileSync(path, '{ not json');
    try {
      DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(DocumentStoreError);
      expect((err as DocumentStoreError).code).toBe('invalid-json');
    }
  });

  it('fails loudly when required fields are missing', () => {
    const path = join(dir, 'shapeless.perf');
    writeFileSync(path, JSON.stringify({ formatVersion: DOCUMENT_FORMAT_VERSION }));
    try {
      DocumentStore.load({ argv: ['node', 'stdio.js', path], env: {} });
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(DocumentStoreError);
      expect((err as DocumentStoreError).code).toBe('invalid-document-shape');
    }
  });
});
