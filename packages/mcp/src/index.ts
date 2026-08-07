/**
 * Library exports for @perfstudio/mcp.
 *
 * The executable entry point (bin: perfstudio-mcp) lives in stdio.ts and is not
 * re-exported here — importing this module must never have the side effect of
 * connecting a transport or touching stdin/stdout.
 */

export { createServer, dispatchToolCall, TOOLS } from './server.js';
export { DocumentStore, DocumentStoreError } from './document-store.js';
export type { DocumentStoreOptions } from './document-store.js';
export { log } from './log.js';
export type { Logger, LogLevel } from './log.js';
