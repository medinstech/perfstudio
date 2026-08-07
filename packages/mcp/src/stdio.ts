#!/usr/bin/env node
/**
 * Executable entry point: wires the server to the stdio transport.
 *
 * Reminder (see log.ts for the full story): nothing in this process may write to
 * stdout except the transport itself. All diagnostics here go through `log`.
 */

import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { DocumentStore } from './document-store.js';
import { log } from './log.js';
import { createServer } from './server.js';

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function main(): Promise<void> {
  const store = DocumentStore.load();
  const server = createServer(store);
  const transport = new StdioServerTransport();

  let shuttingDown = false;
  const shutdown = (signal: NodeJS.Signals): void => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    log.info(`Received ${signal}, shutting down`);
    server
      .close()
      .catch((err: unknown) => {
        log.error('Error while closing the server', { error: describeError(err) });
      })
      .finally(() => {
        process.exit(0);
      });
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  await server.connect(transport);
  log.info('PerfStudio MCP server connected over stdio');
}

main().catch((err: unknown) => {
  log.error('Fatal error starting the PerfStudio MCP server', { error: describeError(err) });
  process.exit(1);
});
