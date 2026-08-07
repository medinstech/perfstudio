/**
 * Minimal S-expression tokenizer and parser.
 *
 * KiCad's netlist, schematic and PCB file formats are all S-expressions:
 * parenthesized lists of atoms and double-quoted strings. This module knows
 * nothing about KiCad semantics — it only turns text into a tree — so it can be
 * reused for any other S-expression-based format later.
 */

/** A parsed S-expression node: either an atom (bare or from a quoted string) or a list. */
export type SExpr = string | SExpr[];

/**
 * Raised when the input is not well-formed S-expression syntax. `offset` is the
 * 0-indexed character position in the original input where the problem was found,
 * so callers can point a user at the exact spot.
 */
export class SExprSyntaxError extends Error {
  constructor(
    message: string,
    readonly offset: number,
  ) {
    super(`${message} (at offset ${offset})`);
    this.name = 'SExprSyntaxError';
  }
}

const OPEN = '(';
const CLOSE = ')';
const QUOTE = '"';

function isWhitespace(ch: string): boolean {
  return ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === '\f' || ch === '\v';
}

/** True at end-of-input (charAt returns '') or at any character that ends a bare atom. */
function isAtomBoundary(ch: string): boolean {
  return ch === '' || ch === OPEN || ch === CLOSE || ch === QUOTE || isWhitespace(ch);
}

function skipWhitespace(input: string, pos: number): number {
  let i = pos;
  while (isWhitespace(input.charAt(i))) i += 1;
  return i;
}

/**
 * Parse a double-quoted string starting at `input[start]` (which must be `"`).
 * Supports the escapes KiCad emits: `\"`, `\\`, `\n`. Any other backslash escape is
 * taken literally (the escaped character is kept as-is).
 * Returns the decoded string and the index just past the closing quote.
 */
function parseQuotedString(input: string, start: number): { value: string; next: number } {
  let i = start + 1;
  let out = '';
  for (;;) {
    const ch = input.charAt(i);
    if (ch === '') {
      throw new SExprSyntaxError('Unterminated string', start);
    }
    if (ch === '\\') {
      const escaped = input.charAt(i + 1);
      if (escaped === '') {
        throw new SExprSyntaxError('Unterminated string: dangling escape', i);
      }
      switch (escaped) {
        case '"':
          out += '"';
          break;
        case '\\':
          out += '\\';
          break;
        case 'n':
          out += '\n';
          break;
        default:
          out += escaped;
          break;
      }
      i += 2;
      continue;
    }
    if (ch === QUOTE) {
      return { value: out, next: i + 1 };
    }
    out += ch;
    i += 1;
  }
}

/**
 * Parse one form (atom or list) starting at `input[pos]`. Caller must have already
 * skipped leading whitespace and verified `pos` is not past the end of input.
 * Returns the parsed node and the index just past it.
 */
function parseForm(input: string, pos: number): { node: SExpr; next: number } {
  const ch = input.charAt(pos);

  if (ch === OPEN) {
    const items: SExpr[] = [];
    let i = pos + 1;
    for (;;) {
      i = skipWhitespace(input, i);
      const peek = input.charAt(i);
      if (peek === '') {
        throw new SExprSyntaxError('Unbalanced parentheses: missing closing ")"', pos);
      }
      if (peek === CLOSE) {
        return { node: items, next: i + 1 };
      }
      const parsed = parseForm(input, i);
      items.push(parsed.node);
      i = parsed.next;
    }
  }

  if (ch === CLOSE) {
    throw new SExprSyntaxError('Unbalanced parentheses: unexpected ")"', pos);
  }

  if (ch === QUOTE) {
    const { value, next } = parseQuotedString(input, pos);
    return { node: value, next };
  }

  // Bare atom: run until the next boundary character.
  let i = pos;
  while (!isAtomBoundary(input.charAt(i))) i += 1;
  return { node: input.slice(pos, i), next: i };
}

/**
 * Parse zero or more top-level S-expression forms from `input`.
 * Throws SExprSyntaxError on unbalanced parentheses or an unterminated string,
 * with the character offset of the problem.
 */
export function parseSExpr(input: string): SExpr[] {
  const forms: SExpr[] = [];
  let i = skipWhitespace(input, 0);
  while (i < input.length) {
    if (input.charAt(i) === CLOSE) {
      throw new SExprSyntaxError('Unbalanced parentheses: unexpected ")"', i);
    }
    const { node, next } = parseForm(input, i);
    forms.push(node);
    i = skipWhitespace(input, next);
  }
  return forms;
}
