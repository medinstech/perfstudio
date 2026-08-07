/**
 * The PerfStudio MCP server: tool registration and dispatch.
 *
 * This first pass is READ-ONLY — every tool here only reads the current document
 * held by the DocumentStore. No tool mutates it.
 *
 * Built on the SDK's low-level `Server` (not the zod-based `McpServer` helper):
 * `McpServer.registerTool` requires constructing zod schemas, and the installed
 * SDK (1.30.0) resolves `zod` only inside its own nested node_modules — it is not
 * hoisted to this package and cannot be added without touching package.json,
 * which is off-limits here. The low-level `Server` sidesteps this entirely: tool
 * input schemas are plain JSON Schema objects (see `Tool['inputSchema']` in the
 * SDK's types.d.ts), so this file never needs to import `zod` at all.
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { CallToolRequestSchema, ListToolsRequestSchema, ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js';
import type { CallToolResult, Tool } from '@modelcontextprotocol/sdk/types.js';

import type {
  BoardMaterial,
  BoardType,
  ComponentInstance,
  Net,
  NetClass,
  PerfDocument,
  Rotation,
} from '@perfstudio/core';

import type { DocumentStore } from './document-store.js';
import { boardSizeMm, coordToHoleRef } from '@perfstudio/core';
import { log } from './log.js';

// ---------------------------------------------------------------------------
// Tool-level errors
// ---------------------------------------------------------------------------

/**
 * Raised by a tool handler when the request is well-formed JSON-RPC but invalid
 * at the domain level (bad argument, target not found, ...). Carries a
 * machine-readable code, mirroring core's CommandError. Caught by dispatch and
 * turned into a structured `isError` result rather than a raw thrown exception —
 * so the client sees a clear reason, not a stack trace.
 */
class ToolError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'ToolError';
  }
}

// ---------------------------------------------------------------------------
// Small argument helpers
// ---------------------------------------------------------------------------

function optionalStringArg(args: Record<string, unknown>, key: string): string | undefined {
  const value = args[key];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== 'string') {
    throw new ToolError('invalid-params', `Expected "${key}" to be a string, got ${typeof value}`);
  }
  return value;
}

function jsonSchemaObject(properties: Record<string, Record<string, unknown>>): Tool['inputSchema'] {
  return { type: 'object', properties, additionalProperties: false };
}

// ---------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------

interface ComponentDetail {
  readonly id: string;
  readonly ref: string;
  readonly value: string;
  readonly footprintId: string;
  readonly anchor: { readonly col: number; readonly row: number };
  readonly anchorRef: string;
  readonly rotation: Rotation;
  readonly mirrored: boolean;
  readonly locked: boolean;
}

function toComponentDetail(c: ComponentInstance): ComponentDetail {
  return {
    id: c.id,
    ref: c.ref,
    value: c.value,
    footprintId: c.footprintId,
    anchor: { col: c.anchor.col, row: c.anchor.row },
    anchorRef: coordToHoleRef(c.anchor),
    rotation: c.rotation,
    mirrored: c.mirrored,
    locked: c.locked,
  };
}

interface NetSummary {
  readonly id: string;
  readonly name: string;
  readonly class: NetClass;
  readonly nodeCount: number;
  readonly nodes: readonly { readonly componentRef: string; readonly pin: string }[];
  readonly currentA?: number;
  readonly voltageV?: number;
}

function toNetSummary(n: Net): NetSummary {
  return {
    id: n.id,
    name: n.name,
    class: n.class,
    nodeCount: n.nodes.length,
    nodes: n.nodes.map((node) => ({ componentRef: node.componentRef, pin: node.pin })),
    ...(n.currentA !== undefined ? { currentA: n.currentA } : {}),
    ...(n.voltageV !== undefined ? { voltageV: n.voltageV } : {}),
  };
}

// ---------------------------------------------------------------------------
// Tool 1: get_board_info
// ---------------------------------------------------------------------------

interface BoardInfoResult {
  readonly type: BoardType;
  readonly holes: { readonly cols: number; readonly rows: number; readonly count: number };
  /**
   * Physical board size, from core's canonical `boardSizeMm`: the substrate extends
   * half a pitch beyond the outermost hole centres, so this is cols*pitch, while the
   * hole centres themselves span only (cols-1)*pitch.
   */
  readonly dimensionsMm: { readonly width: number; readonly height: number };
  readonly pitchMm: number;
  readonly material: BoardMaterial;
  readonly padDiameterMm: number;
  readonly drillDiameterMm: number;
  readonly thicknessMm: number;
  readonly stripAxis?: 'horizontal' | 'vertical';
}

function getBoardInfo(doc: PerfDocument): BoardInfoResult {
  const { board } = doc;
  return {
    type: board.type,
    holes: { cols: board.cols, rows: board.rows, count: board.cols * board.rows },
    dimensionsMm: boardSizeMm(board),
    pitchMm: board.pitch,
    material: board.material,
    padDiameterMm: board.padDiameter,
    drillDiameterMm: board.drillDiameter,
    thicknessMm: board.thickness,
    ...(board.stripAxis !== undefined ? { stripAxis: board.stripAxis } : {}),
  };
}

// ---------------------------------------------------------------------------
// Tool 2: list_components
// ---------------------------------------------------------------------------

function listComponents(doc: PerfDocument, filter: string | undefined): ComponentDetail[] {
  const needle = filter?.trim().toLowerCase();
  const matches =
    needle !== undefined && needle.length > 0
      ? doc.components.filter(
          (c) => c.ref.toLowerCase().includes(needle) || c.value.toLowerCase().includes(needle),
        )
      : doc.components;
  return matches.map(toComponentDetail);
}

// ---------------------------------------------------------------------------
// Tool 3: get_component
// ---------------------------------------------------------------------------

function getComponent(doc: PerfDocument, ref: string | undefined, id: string | undefined): ComponentDetail {
  if (id === undefined && ref === undefined) {
    throw new ToolError('invalid-params', 'get_component requires either "ref" or "id"');
  }
  const found = id !== undefined ? doc.components.find((c) => c.id === id) : doc.components.find((c) => c.ref === ref);
  if (!found) {
    const which = id !== undefined ? `id "${id}"` : `ref "${ref}"`;
    throw new ToolError('not-found', `No component with ${which}`);
  }
  return toComponentDetail(found);
}

// ---------------------------------------------------------------------------
// Tool 4: get_nets
// ---------------------------------------------------------------------------

function getNets(doc: PerfDocument, nameFilter: string | undefined): NetSummary[] {
  const needle = nameFilter?.trim().toLowerCase();
  const matches =
    needle !== undefined && needle.length > 0 ? doc.nets.filter((n) => n.name.toLowerCase().includes(needle)) : doc.nets;
  return matches.map(toNetSummary);
}

// ---------------------------------------------------------------------------
// Tool 5: get_document_stats
// ---------------------------------------------------------------------------

interface DocumentStatsResult {
  readonly formatVersion: number;
  readonly documentName: string;
  readonly componentCount: number;
  readonly netCount: number;
  readonly cutCount: number;
  readonly conductorCount: number;
  readonly conductorsByKind: Readonly<Record<string, number>>;
}

function getDocumentStats(doc: PerfDocument): DocumentStatsResult {
  const conductorsByKind: Record<string, number> = {};
  for (const c of doc.conductors) {
    conductorsByKind[c.kind] = (conductorsByKind[c.kind] ?? 0) + 1;
  }
  return {
    formatVersion: doc.formatVersion,
    documentName: doc.meta.name,
    componentCount: doc.components.length,
    netCount: doc.nets.length,
    cutCount: doc.cuts.length,
    conductorCount: doc.conductors.length,
    conductorsByKind,
  };
}

// ---------------------------------------------------------------------------
// Tool table
// ---------------------------------------------------------------------------

interface ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: Tool['inputSchema'];
  readonly run: (doc: PerfDocument, args: Record<string, unknown>) => unknown;
}

const EMPTY_SCHEMA = jsonSchemaObject({});

export const TOOLS: readonly ToolDefinition[] = [
  {
    name: 'get_board_info',
    description:
      'Board type, dimensions (in holes and mm), pitch, material, pad/drill diameter and hole count for the current document.',
    inputSchema: EMPTY_SCHEMA,
    run: (doc) => getBoardInfo(doc),
  },
  {
    name: 'list_components',
    description:
      'List the components placed on the board: id, ref, value, footprint, anchor position (grid + "A1"-style) and orientation. Optionally filter by a case-insensitive substring match against ref or value.',
    inputSchema: jsonSchemaObject({
      filter: { type: 'string', description: 'Case-insensitive substring to match against ref or value.' },
    }),
    run: (doc, args) => listComponents(doc, optionalStringArg(args, 'filter')),
  },
  {
    name: 'get_component',
    description:
      'Look up a single component by ref (e.g. "R1") or id, with its full detail. Returns an error result, not an exception, if no matching component exists.',
    inputSchema: jsonSchemaObject({
      ref: { type: 'string', description: 'Component designator, e.g. "R1".' },
      id: { type: 'string', description: 'Component id.' },
    }),
    run: (doc, args) => getComponent(doc, optionalStringArg(args, 'ref'), optionalStringArg(args, 'id')),
  },
  {
    name: 'get_nets',
    description:
      'List the schematic nets: id, name, class, node count and nodes. Optionally filter by a case-insensitive substring match against the net name.',
    inputSchema: jsonSchemaObject({
      nameFilter: { type: 'string', description: 'Case-insensitive substring to match against the net name.' },
    }),
    run: (doc, args) => getNets(doc, optionalStringArg(args, 'nameFilter')),
  },
  {
    name: 'get_document_stats',
    description:
      'Counts of components, conductors (broken down by kind), nets and cuts in the current document, plus its formatVersion and name.',
    inputSchema: EMPTY_SCHEMA,
    run: (doc) => getDocumentStats(doc),
  },
];

const TOOLS_BY_NAME = new Map(TOOLS.map((t) => [t.name, t]));

// ---------------------------------------------------------------------------
// Dispatch — the logic shared by the real Server wiring and by tests
// ---------------------------------------------------------------------------

function textResult(data: unknown): CallToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(data, null, 2) }] };
}

function errorResult(code: string, message: string): CallToolResult {
  return {
    isError: true,
    content: [{ type: 'text', text: JSON.stringify({ error: { code, message } }, null, 2) }],
  };
}

/**
 * Runs a single tool call against `store`'s current document and returns a
 * CallToolResult. Exported directly so tests can drive tool behaviour without
 * going through a transport (per-task requirement).
 *
 * An unknown tool name is a protocol-level problem (the client asked for
 * something that isn't in the tools list) and is signalled by throwing McpError,
 * same as the SDK's own request routing would. Everything else — bad arguments,
 * a ref/id that doesn't resolve, an unexpected internal error — is a domain-level
 * problem and comes back as a normal (non-throwing) `isError` result instead.
 */
export function dispatchToolCall(
  store: DocumentStore,
  name: string,
  rawArgs: Record<string, unknown> | undefined,
): CallToolResult {
  const tool = TOOLS_BY_NAME.get(name);
  if (!tool) {
    throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: "${name}"`);
  }
  try {
    const data = tool.run(store.document, rawArgs ?? {});
    return textResult(data);
  } catch (err) {
    if (err instanceof ToolError) {
      log.warn(`Tool "${name}" rejected its arguments or target`, { code: err.code, message: err.message });
      return errorResult(err.code, err.message);
    }
    log.error(`Tool "${name}" threw an unexpected error`, {
      error: err instanceof Error ? err.message : String(err),
    });
    return errorResult('internal-error', `Unexpected error while running tool "${name}"`);
  }
}

// ---------------------------------------------------------------------------
// Server factory
// ---------------------------------------------------------------------------

/** Builds the MCP server and registers all read-only tools against `store`. */
export function createServer(store: DocumentStore): Server {
  const server = new Server(
    { name: 'perfstudio-mcp', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, () => ({
    tools: TOOLS.map(
      (t): Tool => ({
        name: t.name,
        description: t.description,
        inputSchema: t.inputSchema,
        annotations: { readOnlyHint: true, idempotentHint: true, openWorldHint: false },
      }),
    ),
  }));

  server.setRequestHandler(CallToolRequestSchema, (request) =>
    dispatchToolCall(store, request.params.name, request.params.arguments),
  );

  return server;
}
