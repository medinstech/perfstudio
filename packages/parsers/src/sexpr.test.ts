import { describe, expect, it } from 'vitest';

import { parseSExpr, SExprSyntaxError } from './sexpr.js';

describe('parseSExpr', () => {
  it('parses a flat list of bare atoms', () => {
    expect(parseSExpr('(a b c)')).toEqual([['a', 'b', 'c']]);
  });

  it('parses nested lists', () => {
    expect(parseSExpr('(a (b c) (d (e f) g))')).toEqual([['a', ['b', 'c'], ['d', ['e', 'f'], 'g']]]);
  });

  it('parses multiple top-level forms', () => {
    expect(parseSExpr('(a) (b)')).toEqual([['a'], ['b']]);
  });

  it('returns an empty array for empty or whitespace-only input', () => {
    expect(parseSExpr('')).toEqual([]);
    expect(parseSExpr('   \n\t  ')).toEqual([]);
  });

  it('treats whitespace (spaces, tabs, newlines, CRLF) as separators', () => {
    expect(parseSExpr('(a\tb\n c\r\nd)')).toEqual([['a', 'b', 'c', 'd']]);
  });

  it('parses quoted strings containing spaces and parentheses as a single atom', () => {
    expect(parseSExpr('(name "hello world (nested)")')).toEqual([['name', 'hello world (nested)']]);
  });

  it('parses an empty quoted string', () => {
    expect(parseSExpr('(value "")')).toEqual([['value', '']]);
  });

  it('decodes backslash escapes: \\" \\\\ and \\n', () => {
    expect(parseSExpr('(s "a\\"b\\\\c\\nd")')).toEqual([['s', 'a"b\\c\nd']]);
  });

  it('keeps an unrecognized escape sequence as the literal escaped character', () => {
    expect(parseSExpr('(s "a\\tb")')).toEqual([['s', 'atb']]);
  });

  it('parses quoted and unquoted atom forms to the same result', () => {
    expect(parseSExpr('(ref R1)')).toEqual(parseSExpr('(ref "R1")'));
    expect(parseSExpr('(ref R1)')).toEqual([['ref', 'R1']]);
  });

  it('treats adjacent bare atoms and quoted strings as distinct tokens', () => {
    expect(parseSExpr('(a"b"c)')).toEqual([['a', 'b', 'c']]);
  });

  it('throws SExprSyntaxError with an offset on a missing closing paren', () => {
    expect(() => parseSExpr('(a (b c)')).toThrow(SExprSyntaxError);
    try {
      parseSExpr('(a (b c)');
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(SExprSyntaxError);
      expect((err as SExprSyntaxError).offset).toBe(0);
    }
  });

  it('throws SExprSyntaxError with an offset on an unexpected closing paren', () => {
    try {
      parseSExpr('(a b))');
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(SExprSyntaxError);
      expect((err as SExprSyntaxError).offset).toBe(5);
    }
  });

  it('throws SExprSyntaxError with an offset on an unterminated string', () => {
    const input = '(name "unterminated)';
    try {
      parseSExpr(input);
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(SExprSyntaxError);
      expect((err as SExprSyntaxError).offset).toBe(input.indexOf('"'));
    }
  });

  it('throws SExprSyntaxError on a string with a dangling escape at end of input', () => {
    const input = '(name "abc\\';
    try {
      parseSExpr(input);
      expect.unreachable();
    } catch (err) {
      expect(err).toBeInstanceOf(SExprSyntaxError);
      expect((err as SExprSyntaxError).offset).toBe(input.length - 1);
    }
  });

  it('includes the offset in the error message text', () => {
    try {
      parseSExpr(')');
      expect.unreachable();
    } catch (err) {
      expect((err as Error).message).toContain('offset 0');
    }
  });
});
