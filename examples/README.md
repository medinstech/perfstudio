# Examples

Four circuits, each shipped twice: as the `.net` a schematic tool exports, and as the
`.perf` that importing, placing and routing it produces.

```sh
perfstudio examples/lm317-supply.perf     # open the finished board
```

...or start from the netlist the way you would with your own circuit: **File → Import
KiCad Netlist**, accept the placement, `Ctrl+Shift+A` to auto-place, `Ctrl+R` to route,
`Ctrl+B` for the build guide.

| | parts | board | what it is there to show |
|---|---|---|---|
| **ne555-astable** | 8 | 32 × 22, FR-4 | The starting point. A 555 flashing an LED — the circuit everybody has built. |
| **lm317-supply** | 11 | 30 × 20, FR-4 | A hot part. The regulator is a TO-220, so the heat-proximity rule has something to measure, and the board carries one `solder-trace-proximity` warning that becomes a checkpoint in the guide. |
| **lpb1-booster** | 12 | 24 × 18, **FR-2** | The material mattering. Phenolic is what a pedal actually gets built on and it is the board whose pads lift, so the guide drops the iron 30 °C (350 → 320) and cuts the dwell from 3 s to 2 s. |
| **arduino-io-shield** | 11 | 28 × 20, FR-4 | Headers. Two of them, 8-pin and 6-pin, which is what a shield mostly is — and the case where lead bends and short traces do nearly all the work. |

All four route to completion, match their schematics under LVS, and carry no DRC error.
`tests/test_examples.py` asserts exactly that on every commit, so an example cannot rot
quietly.

## Regenerating them

```sh
python tools/build_examples.py            # rebuild every .perf from its .net
python tools/build_examples.py --check     # verify only, write nothing
```

The footprints are named in that script rather than guessed from the netlist. A netlist
knows a part is called `U1` and that three of its pins appear in nets, which is all
`guess_footprint_id` has to go on — enough for the app to land parts somewhere obvious
for you to correct, not enough for an example. An LM317 guesses to a DIP-8, and a TO-220
standing in for an 8-pin DIP would put the heat rule and the 3D height check both wrong.

The timestamps in the `.perf` files are fixed rather than current, so rebuilding an
example does not put a date change in the way of whatever the commit was about.

## Adding one

Write the `.net`, add an entry to `CATALOGUE` in `tools/build_examples.py` naming the
board size and each part's real footprint, and run the script. It fails loudly if the
circuit does not route cleanly, which is the point.
