import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { log } from './log.js';

describe('log', () => {
  const originalLevel = process.env['PERFSTUDIO_LOG'];

  beforeEach(() => {
    delete process.env['PERFSTUDIO_LOG'];
  });

  afterEach(() => {
    vi.restoreAllMocks();
    if (originalLevel === undefined) {
      delete process.env['PERFSTUDIO_LOG'];
    } else {
      process.env['PERFSTUDIO_LOG'] = originalLevel;
    }
  });

  it('never writes to stdout, only to stderr', () => {
    const stdoutSpy = vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);

    log.debug('debug message');
    log.info('info message');
    log.warn('warn message');
    log.error('error message', { detail: 'x' });

    expect(stdoutSpy).not.toHaveBeenCalled();
    // debug is below the default 'info' level, so only info/warn/error should land.
    expect(stderrSpy).toHaveBeenCalledTimes(3);
  });

  it('includes the message and level in the written line as JSON', () => {
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);

    log.warn('something happened', { code: 'x' });

    expect(stderrSpy).toHaveBeenCalledTimes(1);
    const written = stderrSpy.mock.calls[0]?.[0];
    expect(typeof written).toBe('string');
    const parsed = JSON.parse(written as string) as { level: string; message: string; data?: unknown };
    expect(parsed.level).toBe('warn');
    expect(parsed.message).toBe('something happened');
    expect(parsed.data).toEqual({ code: 'x' });
  });

  it('respects PERFSTUDIO_LOG to raise the minimum level', () => {
    process.env['PERFSTUDIO_LOG'] = 'error';
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);

    log.info('should be suppressed');
    log.warn('should also be suppressed');
    log.error('should be written');

    expect(stderrSpy).toHaveBeenCalledTimes(1);
  });

  it('respects PERFSTUDIO_LOG=debug to allow debug messages through', () => {
    process.env['PERFSTUDIO_LOG'] = 'debug';
    const stderrSpy = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);

    log.debug('now visible');

    expect(stderrSpy).toHaveBeenCalledTimes(1);
  });
});
