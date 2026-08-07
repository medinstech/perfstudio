/**
 * Hand-written NE555 astable oscillator netlist, in the KiCad 6+ "export"
 * S-expression format, used as a realistic fixture for the KiCad netlist
 * importer tests.
 *
 * Circuit: classic 555 astable oscillator.
 *   - U1: NE555 timer
 *   - R1, R2: timing resistors (charge / discharge path)
 *   - C1: timing capacitor
 *   - C2: control-voltage pin (5) bypass capacitor
 *   - R3: current-limiting resistor for the output LED
 *   - LED1: output indicator LED
 *   - J1: 2-pin power input connector
 *
 * Two things are deliberate, to give the importer something to report on:
 *   - Net codes are out of numeric order in the source text (net "GND" is code
 *     "3" and appears first), to exercise "sort nets by numeric code".
 *   - U1 pin 4 (RESET) is wired to its own "unconnected-(U1-Pad4)" pseudo-net,
 *     the way KiCad emits an unwired pin, purely to exercise the importer's
 *     unconnected-net handling. (A real board would tie RESET to VCC.)
 *
 * Ref quoting is deliberately mixed between `(ref U1)` and `(ref "R1")` forms,
 * matching the inconsistency between KiCad versions that the importer must
 * tolerate.
 */
export const NE555_ASTABLE_NETLIST = `
(export (version "E")
  (design
    (source "/home/user/ne555_astable/ne555_astable.kicad_sch")
    (date "2026-01-01T00:00:00")
    (tool "Eeschema 8.0.0")
    (sheet (number "1") (name "/") (tstamps "/")
      (title_block
        (title "NE555 Astable")
        (company "")
        (rev "")
        (date "")
        (source "ne555_astable.kicad_sch"))))
  (components
    (comp (ref U1)
      (value "NE555")
      (footprint "Package_DIP:DIP-8_W7.62mm")
      (libsource (lib "Timer") (part "NE555") (description "Single 555 timer"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000001"))
    (comp (ref "R1")
      (value "10k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000002"))
    (comp (ref "R2")
      (value "100k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000003"))
    (comp (ref C1)
      (value "10uF")
      (footprint "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm")
      (libsource (lib "Device") (part "C_Polarized") (description "Polarized capacitor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000004"))
    (comp (ref "C2")
      (value "0.01uF")
      (footprint "Capacitor_THT:C_Disc_D3.0mm_W2.0mm_P2.50mm")
      (libsource (lib "Device") (part "C") (description "Unpolarized capacitor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000005"))
    (comp (ref "R3")
      (value "330")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000006"))
    (comp (ref LED1)
      (value "LED")
      (footprint "LED_THT:LED_D5.0mm")
      (libsource (lib "Device") (part "LED") (description "Light emitting diode"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000007"))
    (comp (ref "J1")
      (value "Conn_01x02")
      (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
      (libsource (lib "Connector_Generic") (part "Conn_01x02") (description "Generic connector, 2 pins"))
      (property (name "Sheetfile") (value "ne555_astable.kicad_sch"))
      (sheetpath (names "/") (tstamps "/"))
      (tstamps "00000000-0000-0000-0000-000000000008")))
  (nets
    (net (code "3") (name "GND")
      (node (ref U1) (pin "1") (pinfunction "GND") (pintype "power_in"))
      (node (ref C1) (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref "C2") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref LED1) (pin "2") (pinfunction "K") (pintype "passive"))
      (node (ref "J1") (pin "2") (pinfunction "Pin_2") (pintype "passive")))
    (net (code "1") (name "VCC")
      (node (ref U1) (pin "8") (pinfunction "VCC") (pintype "power_in"))
      (node (ref "R1") (pin "1") (pinfunction "~") (pintype "passive"))
      (node (ref "J1") (pin "1") (pinfunction "Pin_1") (pintype "passive")))
    (net (code "5") (name "THRESH")
      (node (ref U1) (pin "2") (pinfunction "TRIG") (pintype "input"))
      (node (ref U1) (pin "6") (pinfunction "THR") (pintype "input"))
      (node (ref "R2") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref C1) (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "8") (name "unconnected-(U1-Pad4)")
      (node (ref U1) (pin "4") (pinfunction "RESET") (pintype "input")))
    (net (code "2") (name "OUT")
      (node (ref U1) (pin "3") (pinfunction "OUT") (pintype "output"))
      (node (ref "R3") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "7") (name "CTRL")
      (node (ref U1) (pin "5") (pinfunction "CV") (pintype "passive"))
      (node (ref "C2") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "6") (name "DISCH")
      (node (ref U1) (pin "7") (pinfunction "DISCH") (pintype "output"))
      (node (ref "R1") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref "R2") (pin "1") (pinfunction "~") (pintype "passive")))
    (net (code "4") (name "LED_A")
      (node (ref "R3") (pin "2") (pinfunction "~") (pintype "passive"))
      (node (ref LED1) (pin "1") (pinfunction "A") (pintype "passive")))))
`;
