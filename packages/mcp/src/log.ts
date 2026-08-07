/**
 * Stderr-only logger.
 *
 * WHY THIS FILE EXISTS: over the stdio transport, stdout IS the JSON-RPC protocol
 * channel. Any stray write to stdout — a forgotten `console.log`, a library that
 * logs by default, a debug print left in during development — interleaves garbage
 * bytes into the message stream. The client then either fails to parse a message
 * or, worse, silently hangs waiting for a well-formed response that never arrives.
 * This is reportedly the single most common way MCP servers break in production.
 *
 * The fix is structural, not disciplinary: every diagnostic in this package MUST
 * go through `log` below, which writes exclusively to `process.stderr` and never
 * touches `process.stdout` or the `console` global (whose methods route several
 * levels to stdout depending on the runtime).
 */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LEVEL_ORDER: readonly LogLevel[] = ['debug', 'info', 'warn', 'error'];

function isLogLevel(value: string): value is LogLevel {
  return (LEVEL_ORDER as readonly string[]).includes(value);
}

function levelRank(level: LogLevel): number {
  return LEVEL_ORDER.indexOf(level);
}

/** Reads the configured minimum level fresh on every call so tests can flip it via env. */
function configuredLevel(): LogLevel {
  const raw = process.env['PERFSTUDIO_LOG'];
  const normalized = raw?.toLowerCase();
  if (normalized !== undefined && isLogLevel(normalized)) {
    return normalized;
  }
  return 'info';
}

export interface Logger {
  debug(message: string, data?: Record<string, unknown>): void;
  info(message: string, data?: Record<string, unknown>): void;
  warn(message: string, data?: Record<string, unknown>): void;
  error(message: string, data?: Record<string, unknown>): void;
}

function emit(level: LogLevel, message: string, data?: Record<string, unknown> | undefined): void {
  if (levelRank(level) < levelRank(configuredLevel())) {
    return;
  }
  const record = {
    ts: new Date().toISOString(),
    level,
    message,
    ...(data !== undefined ? { data } : {}),
  };
  // process.stderr.write, deliberately not console.error/console.log: this is the
  // one line in the codebase that is allowed to touch a stream, and it must never
  // drift onto stdout. See the file header for why that matters.
  process.stderr.write(`${JSON.stringify(record)}\n`);
}

export const log: Logger = {
  debug: (message, data) => emit('debug', message, data),
  info: (message, data) => emit('info', message, data),
  warn: (message, data) => emit('warn', message, data),
  error: (message, data) => emit('error', message, data),
};
