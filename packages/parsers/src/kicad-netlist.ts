/**
 * KiCad netlist importer.
 *
 * Maps a KiCad 6+ "export" netlist (the S-expression format KiCad writes via
 * File > Export > Netlist, or the equivalent produced from a schematic) onto
 * PerfStudio's `Net` / `NetNode` model, so the router and DRC can consume the
 * schematic's intent (PLAN.md §5.1, LVS).
 *
 * This module is pure: it takes a string and returns data. Reading the .net file
 * from disk is the caller's job.
 */

import type { Net, NetClass, NetNode } from '@perfstudio/core';

import { parseSExpr, type SExpr } from './sexpr.js';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface ImportedComponent {
  readonly ref: string;
  readonly value: string;
  readonly footprint?: string;
  readonly libPart?: string;
}

export interface KicadNetlistImport {
  readonly components: readonly ImportedComponent[];
  readonly nets: readonly Net[];
  readonly warnings: readonly string[];
}

// ---------------------------------------------------------------------------
// Net class inference
// ---------------------------------------------------------------------------

const GROUND_NET_NAMES = new Set(['GND', 'GNDA', 'GNDD', 'AGND', 'DGND', 'VSS', '0']);

const POWER_NET_NAMES = new Set(['VCC', 'VDD', 'VEE', 'VBUS', 'V+', 'V-', '+5V', '+3V3', '+12V', '-12V']);

/** Matches names like "+5V", "-12V", "3V3", "+3V3": an optional sign, digits, "V", more digits. */
const POWER_VOLTAGE_PATTERN = /^[+-]?\d+V\d*$/;

const POWER_NET_PREFIXES = ['VCC', 'VDD', 'VBAT'];

/**
 * Infer a net's electrical class from its schematic name. Case-insensitive.
 * Kept separate from `parseKicadNetlist` so the classification rules can be
 * unit-tested in isolation.
 */
export function inferNetClass(name: string): NetClass {
  const upper = name.trim().toUpperCase();
  if (GROUND_NET_NAMES.has(upper)) return 'ground';
  if (POWER_NET_NAMES.has(upper)) return 'power';
  if (POWER_VOLTAGE_PATTERN.test(upper)) return 'power';
  if (POWER_NET_PREFIXES.some((prefix) => upper.startsWith(prefix))) return 'power';
  return 'signal';
}

// ---------------------------------------------------------------------------
// S-expression tree helpers
// ---------------------------------------------------------------------------

/** True if `node` is a list whose first element is the atom `tag` (e.g. `(ref ...)`). */
function isTaggedList(node: SExpr, tag: string): node is SExpr[] {
  return Array.isArray(node) && node[0] === tag;
}

/** First child of `list` that is itself a list tagged `tag`, e.g. `(ref ...)` inside `(comp ...)`. */
function findChild(list: readonly SExpr[], tag: string): SExpr[] | undefined {
  return list.find((item): item is SExpr[] => isTaggedList(item, tag));
}

/** All children of `list` that are lists tagged `tag`, e.g. every `(node ...)` inside a `(net ...)`. */
function findChildren(list: readonly SExpr[], tag: string): SExpr[][] {
  return list.filter((item): item is SExpr[] => isTaggedList(item, tag));
}

/**
 * Extract the string value of a single-value tagged field, e.g. `(ref "R1")` or the
 * unquoted `(ref R1)` — both parse to the same atom via `parseSExpr`, so this handles
 * KiCad's inconsistent quoting for free.
 */
function fieldValue(list: readonly SExpr[], tag: string): string | undefined {
  const field = findChild(list, tag);
  if (!field) return undefined;
  const value = field[1];
  return typeof value === 'string' ? value : undefined;
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

const UNCONNECTED_NET_NAME = /^unconnected-/i;

function pluralize(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

function parseComponents(componentsList: SExpr[] | undefined, warnings: string[]): ImportedComponent[] {
  if (!componentsList) return [];

  const components: ImportedComponent[] = [];
  for (const comp of findChildren(componentsList, 'comp')) {
    const ref = fieldValue(comp, 'ref');
    if (ref === undefined) {
      warnings.push('Skipped a component with no "ref" field');
      continue;
    }

    const value = fieldValue(comp, 'value') ?? '';
    const footprint = fieldValue(comp, 'footprint');
    const libsource = findChild(comp, 'libsource');
    const libPart = libsource ? fieldValue(libsource, 'part') : undefined;

    components.push({
      ref,
      value,
      ...(footprint !== undefined ? { footprint } : {}),
      ...(libPart !== undefined ? { libPart } : {}),
    });
  }
  return components;
}

interface SortableNet {
  readonly net: Net;
  /** Numeric KiCad net code, for ordering; NaN if the code was missing or non-numeric. */
  readonly codeKey: number;
}

function parseNets(netsList: SExpr[] | undefined, warnings: string[]): Net[] {
  if (!netsList) return [];

  let unconnectedCount = 0;
  const collected: SortableNet[] = [];

  for (const netForm of findChildren(netsList, 'net')) {
    const code = fieldValue(netForm, 'code');
    const name = fieldValue(netForm, 'name') ?? '';

    if (UNCONNECTED_NET_NAME.test(name)) {
      unconnectedCount += 1;
      continue;
    }

    const nodes: NetNode[] = [];
    for (const nodeForm of findChildren(netForm, 'node')) {
      const nodeRef = fieldValue(nodeForm, 'ref');
      const pin = fieldValue(nodeForm, 'pin');
      if (nodeRef === undefined || pin === undefined) {
        warnings.push(`Skipped a node with missing "ref"/"pin" in net "${name}"`);
        continue;
      }
      nodes.push({ componentRef: nodeRef, pin });
    }

    if (nodes.length < 2) {
      warnings.push(`net "${name}" has only ${pluralize(nodes.length, 'node')}`);
    }

    let id: string;
    let codeKey: number;
    if (code !== undefined) {
      id = `net-${code}`;
      codeKey = Number.parseInt(code, 10);
    } else {
      warnings.push(`Net "${name}" has no "code" field; using a positional id`);
      id = `net-${collected.length}`;
      codeKey = Number.NaN;
    }

    collected.push({
      net: { id, name, nodes, class: inferNetClass(name) },
      codeKey,
    });
  }

  if (unconnectedCount > 0) {
    warnings.push(`Skipped ${pluralize(unconnectedCount, 'unconnected net')}`);
  }

  // Deterministic order: ascending numeric code; nets with a missing/non-numeric code
  // (NaN) sort last, tie-broken by id so the result never depends on Array#sort stability.
  collected.sort((a, b) => {
    const aNaN = Number.isNaN(a.codeKey);
    const bNaN = Number.isNaN(b.codeKey);
    if (aNaN !== bNaN) return aNaN ? 1 : -1;
    if (!aNaN && a.codeKey !== b.codeKey) return a.codeKey - b.codeKey;
    return a.net.id.localeCompare(b.net.id);
  });

  return collected.map((c) => c.net);
}

/**
 * Parse a KiCad netlist (the `(export (version ...) (components ...) (nets ...))`
 * S-expression format) into PerfStudio's component and net model.
 *
 * Throws only on malformed S-expression syntax (see `parseSExpr`) or if the input
 * has no top-level `export` form. Anything else recoverable — a component missing
 * its ref, an undersized net, KiCad's `unconnected-*` pseudo-nets — is reported via
 * `warnings` rather than thrown, so a partially-off netlist can still be imported.
 */
export function parseKicadNetlist(source: string): KicadNetlistImport {
  const forms = parseSExpr(source);
  const root = forms.find((f): f is SExpr[] => isTaggedList(f, 'export'));
  if (!root) {
    throw new Error('Not a KiCad netlist: no top-level "export" form found');
  }

  const warnings: string[] = [];
  const components = parseComponents(findChild(root, 'components'), warnings);
  const nets = parseNets(findChild(root, 'nets'), warnings);

  return { components, nets, warnings };
}
