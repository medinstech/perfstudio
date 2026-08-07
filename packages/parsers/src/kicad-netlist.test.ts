import { describe, expect, it } from 'vitest';

import { NE555_ASTABLE_NETLIST } from './__fixtures__/ne555-astable.js';
import { inferNetClass, parseKicadNetlist } from './kicad-netlist.js';

// A minimal netlist crafted to exercise the two warning paths the NE555 fixture
// doesn't reach on its own: a surviving net with only one node, and an
// unconnected-* pseudo-net that must be dropped.
const MINIMAL_NETLIST = `
(export (version "E")
  (components
    (comp (ref "R1") (value "1k")))
  (nets
    (net (code "1") (name "LONELY")
      (node (ref "R1") (pin "1")))
    (net (code "2") (name "unconnected-(R1-Pad2)")
      (node (ref "R1") (pin "2")))))
`;

describe('inferNetClass', () => {
  it.each(['GND', 'GNDA', 'GNDD', 'AGND', 'DGND', 'VSS', '0', 'gnd'])('classifies "%s" as ground', (name) => {
    expect(inferNetClass(name)).toBe('ground');
  });

  it.each(['VCC', 'VDD', 'VEE', 'VBUS', 'V+', 'V-', '+5V', '+3V3', '+12V', '-12V', 'vcc', 'VCC1', 'VDD_3V3', 'VBAT'])(
    'classifies "%s" as power',
    (name) => {
      expect(inferNetClass(name)).toBe('power');
    },
  );

  it.each(['OUT', 'THRESH', 'SDA', 'SCL', 'LED_A', 'NET1', 'RESET'])('classifies "%s" as signal', (name) => {
    expect(inferNetClass(name)).toBe('signal');
  });
});

describe('parseKicadNetlist — NE555 astable fixture', () => {
  const result = parseKicadNetlist(NE555_ASTABLE_NETLIST);

  it('parses all 8 components, in file order', () => {
    expect(result.components).toHaveLength(8);
    expect(result.components.map((c) => c.ref)).toEqual(['U1', 'R1', 'R2', 'C1', 'C2', 'R3', 'LED1', 'J1']);
  });

  it('parses ref, value, footprint and libPart regardless of quoting style', () => {
    const u1 = result.components.find((c) => c.ref === 'U1'); // unquoted (ref U1) in the fixture
    const r1 = result.components.find((c) => c.ref === 'R1'); // quoted (ref "R1") in the fixture

    expect(u1).toEqual({
      ref: 'U1',
      value: 'NE555',
      footprint: 'Package_DIP:DIP-8_W7.62mm',
      libPart: 'NE555',
    });
    expect(r1).toEqual({
      ref: 'R1',
      value: '10k',
      footprint: 'Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal',
      libPart: 'R',
    });
  });

  it('drops the unconnected-(U1-Pad4) pseudo-net, leaving 7 real nets', () => {
    expect(result.nets).toHaveLength(7);
    expect(result.nets.some((n) => /^unconnected-/i.test(n.name))).toBe(false);
  });

  it('records exactly one skipped-unconnected-net warning', () => {
    const matches = result.warnings.filter((w) => /1 unconnected net/i.test(w));
    expect(matches).toHaveLength(1);
  });

  it('sorts nets by ascending numeric KiCad net code, independent of file order', () => {
    // File order is GND(3), VCC(1), THRESH(5), unconnected(8, dropped), OUT(2), CTRL(7), DISCH(6), LED_A(4).
    expect(result.nets.map((n) => n.id)).toEqual([
      'net-1',
      'net-2',
      'net-3',
      'net-4',
      'net-5',
      'net-6',
      'net-7',
    ]);
    expect(result.nets.map((n) => n.name)).toEqual(['VCC', 'OUT', 'GND', 'LED_A', 'THRESH', 'DISCH', 'CTRL']);
  });

  it('the GND net contains exactly the expected component pins', () => {
    const gnd = result.nets.find((n) => n.name === 'GND');
    expect(gnd?.nodes).toEqual([
      { componentRef: 'U1', pin: '1' },
      { componentRef: 'C1', pin: '2' },
      { componentRef: 'C2', pin: '2' },
      { componentRef: 'LED1', pin: '2' },
      { componentRef: 'J1', pin: '2' },
    ]);
  });

  it('classifies GND, VCC, and OUT nets correctly via the imported net class', () => {
    const byName = (name: string) => result.nets.find((n) => n.name === name);
    expect(byName('GND')?.class).toBe('ground');
    expect(byName('VCC')?.class).toBe('power');
    expect(byName('OUT')?.class).toBe('signal');
  });
});

describe('parseKicadNetlist — edge cases', () => {
  it('warns about a net with fewer than two nodes, but still returns it', () => {
    const result = parseKicadNetlist(MINIMAL_NETLIST);
    const lonely = result.nets.find((n) => n.name === 'LONELY');
    expect(lonely).toBeDefined();
    expect(lonely?.nodes).toEqual([{ componentRef: 'R1', pin: '1' }]);
    expect(result.warnings.some((w) => w.includes('LONELY') && w.includes('1 node'))).toBe(true);
  });

  it('drops unconnected-* nets here too and counts them in warnings', () => {
    const result = parseKicadNetlist(MINIMAL_NETLIST);
    expect(result.nets).toHaveLength(1);
    expect(result.warnings.some((w) => /1 unconnected net/i.test(w))).toBe(true);
  });

  it('throws a descriptive error when there is no top-level "export" form', () => {
    expect(() => parseKicadNetlist('(not-a-netlist)')).toThrow(/export/i);
  });

  it('returns empty components/nets for an export with no components or nets sections', () => {
    const result = parseKicadNetlist('(export (version "E"))');
    expect(result.components).toEqual([]);
    expect(result.nets).toEqual([]);
    expect(result.warnings).toEqual([]);
  });
});
