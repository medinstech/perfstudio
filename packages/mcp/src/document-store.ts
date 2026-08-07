/**
 * Loads and holds a PerfDocument for the MCP server.
 *
 * Resolution order for where the document comes from:
 *   1. argv[2] (a bare positional path), if given and non-empty.
 *   2. the PERFSTUDIO_DOCUMENT env var, if set.
 *   3. neither: start with a small empty default board, entirely in memory.
 *
 * This module never touches stdout (see log.ts) and always fails loudly — via
 * log.error plus a thrown, machine-readable DocumentStoreError — rather than
 * silently limping on with a document that doesn't match what the tool schemas
 * below assume.
 */

import { readFileSync } from 'node:fs';

import { DOCUMENT_FORMAT_VERSION, STANDARD_PITCH_MM } from '@perfstudio/core';
import type { PerfDocument } from '@perfstudio/core';

import { log } from './log.js';

/** Raised for any problem loading or validating a document. Carries a machine-readable code. */
export class DocumentStoreError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'DocumentStoreError';
  }
}

/** Logs the failure to stderr (so it is never silent) and then throws it. */
function fail(code: string, message: string): never {
  log.error(message, { code });
  throw new DocumentStoreError(code, message);
}

export interface DocumentStoreOptions {
  /** Defaults to process.argv. Overridable so tests don't depend on the real CLI invocation. */
  readonly argv?: readonly string[];
  /** Defaults to process.env. */
  readonly env?: Readonly<Record<string, string | undefined>>;
}

type DocumentSource = { readonly kind: 'file'; readonly path: string } | { readonly kind: 'default' };

/** Small default board: pad-per-hole, 60x40 holes, 2.54mm pitch, FR4. */
function createDefaultDocument(): PerfDocument {
  const now = new Date().toISOString();
  return {
    formatVersion: DOCUMENT_FORMAT_VERSION,
    meta: { name: 'Untitled board', created: now, modified: now },
    board: {
      type: 'pad-per-hole',
      cols: 60,
      rows: 40,
      pitch: STANDARD_PITCH_MM,
      thickness: 1.6,
      material: 'FR4',
      padDiameter: 1.9,
      drillDiameter: 1.0,
    },
    components: [],
    conductors: [],
    cuts: [],
    nets: [],
  };
}

/**
 * Minimal structural check, not a full schema validator (no validation library is
 * available to this package without touching package.json). It exists so a
 * malformed or version-mismatched file fails loudly right here, with a clear
 * message, instead of crashing obscurely deep inside a tool handler.
 */
function assertPerfDocumentShape(value: unknown, sourcePath: string): asserts value is PerfDocument {
  if (typeof value !== 'object' || value === null) {
    fail('invalid-document-shape', `${sourcePath}: expected a JSON object at the top level`);
  }
  const doc = value as Record<string, unknown>;

  if (typeof doc['formatVersion'] !== 'number') {
    fail('invalid-document-shape', `${sourcePath}: missing or non-numeric "formatVersion"`);
  }
  if (doc['formatVersion'] !== DOCUMENT_FORMAT_VERSION) {
    fail(
      'format-version-mismatch',
      `${sourcePath}: document formatVersion ${String(doc['formatVersion'])} does not match the version ` +
        `this server understands (${DOCUMENT_FORMAT_VERSION}). Migrate the document or use a matching server version.`,
    );
  }

  const requiredArrayFields = ['components', 'conductors', 'cuts', 'nets'] as const;
  for (const field of requiredArrayFields) {
    if (!Array.isArray(doc[field])) {
      fail('invalid-document-shape', `${sourcePath}: "${field}" must be an array`);
    }
  }
  if (typeof doc['board'] !== 'object' || doc['board'] === null) {
    fail('invalid-document-shape', `${sourcePath}: missing "board" object`);
  }
  if (typeof doc['meta'] !== 'object' || doc['meta'] === null) {
    fail('invalid-document-shape', `${sourcePath}: missing "meta" object`);
  }
}

function loadFromFile(path: string): PerfDocument {
  let raw: string;
  try {
    raw = readFileSync(path, 'utf-8');
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return fail('file-read-failed', `Could not read document file "${path}": ${message}`);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return fail('invalid-json', `Document file "${path}" is not valid JSON: ${message}`);
  }

  assertPerfDocumentShape(parsed, path);
  return parsed;
}

export class DocumentStore {
  #document: PerfDocument;
  #source: DocumentSource;

  private constructor(document: PerfDocument, source: DocumentSource) {
    this.#document = document;
    this.#source = source;
  }

  /** Resolves the document source per the module doc comment and loads it. */
  static load(options: DocumentStoreOptions = {}): DocumentStore {
    const argv = options.argv ?? process.argv;
    const env = options.env ?? process.env;

    const argPath = argv[2];
    const path = argPath !== undefined && argPath.length > 0 ? argPath : env['PERFSTUDIO_DOCUMENT'];

    if (path !== undefined) {
      log.info('Loading document from file', { path });
      const document = loadFromFile(path);
      return new DocumentStore(document, { kind: 'file', path });
    }

    log.info('No document path given (argv[2] / PERFSTUDIO_DOCUMENT); starting with an empty default board');
    return new DocumentStore(createDefaultDocument(), { kind: 'default' });
  }

  /** Wraps an already-in-memory document directly. Useful for embedding hosts and tests. */
  static fromDocument(document: PerfDocument): DocumentStore {
    return new DocumentStore(document, { kind: 'default' });
  }

  get document(): PerfDocument {
    return this.#document;
  }

  /** Re-reads the document from its backing file. A no-op (with a warning) for the default document. */
  reload(): void {
    if (this.#source.kind === 'file') {
      log.info('Reloading document from file', { path: this.#source.path });
      this.#document = loadFromFile(this.#source.path);
      return;
    }
    log.warn('reload() called on a store with no backing file; document left unchanged');
  }
}
