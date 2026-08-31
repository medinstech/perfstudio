# PerfStudio

[![CI](https://github.com/medinstech/perfstudio/actions/workflows/ci.yml/badge.svg)](https://github.com/medinstech/perfstudio/actions/workflows/ci.yml)
[![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](https://github.com/medinstech/perfstudio/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

**English** · [Türkçe](https://github.com/medinstech/perfstudio/blob/main/README.tr.md)

Design circuits on perfboard the way you would on a PCB — then get a soldering guide
you can actually build from.

![The 2D editor with an NE555 astable placed and routed](https://raw.githubusercontent.com/medinstech/perfstudio/main/docs/images/editor-component-side.png)

> **Status: pre-alpha, and end to end.** A netlist goes in and a soldering guide comes
> out: 2D editor, 3D view, placement optimiser, autorouter, DRC, LVS, the build guide,
> an exact 1:1 PDF export and an MCP server. What is missing is the dogfood test —
> nobody has yet built a real board by following a generated guide, and
> [PLAN.md](https://github.com/medinstech/perfstudio/blob/main/PLAN.md) §11 says M5 does not close until somebody has. Everything else
> runs: **v0.4.0** ships an installer for each of the three desktop platforms, none of
> them code-signed.

---

## What it does

Take a schematic netlist, lay it out on pad-per-hole perfboard, let the router work out
the connections, prove the result matches the schematic, and export a step-by-step build
guide with measurement checkpoints.

**Three things make it different from the tools that already exist:**

1. **A soldering guide with verification steps.** Not just "solder R1 here", but
   *"block 2 complete → U1 pin 4 to C3(−) must show continuity"* and
   *"before power-on: GND to V+ must read above 10 kΩ"*. Derived from the netlist, so it
   is exact rather than generic advice.
2. **Perfboard LVS.** The board's real connectivity is extracted and compared against the
   schematic. Opens, shorts and floating conductors are reported before you pick up the
   iron.
3. **Agent-native.** An MCP server, a headless CLI and a git-diffable project file, all
   driving the same command bus as the GUI — so undo works across a session where a human
   and a model both edit the board.

## Connections are not all the same thing

Most tools model a perfboard connection as "a wire". Perfboard has six physically
distinct ways to join two points, each with its own cost, limits and failure modes — and
modelling that difference is what lets the router produce a layout that is pleasant to
actually solder:

| | what it is | notes |
|---|---|---|
| lead bend | a component leg bent to a nearby hole | effectively free, 3–4 holes |
| **solder trace** | adjacent pads joined with solder alone | orthogonal only; ~0.6 mm to the next pad |
| **solder trace, wired** | the same, over a tinned-wire spine | ~10× lower resistance, no length limit |
| bare wire | tinned wire on the solder side | cannot cross another bare conductor |
| insulated wire | may cross freely | costs preparation time |
| top jumper | insulated jumper over the component side | visible, occupies body space |

The 0.6 mm gap to the neighbouring pad is why solder traces are both so useful and so
easy to get wrong. PerfStudio scores that risk into the router's cost function, and turns
every flagged spot into a measurement step in the build guide.

## Both faces, and the third dimension

The solder side is where the copper is, so it is a first-class view rather than a mirror
mode — and copper on the face you are *not* looking at is hatched, because a board is
opaque and a trace drawn solid says *this is in front of you*.

![The solder side, with far-side copper hatched](https://raw.githubusercontent.com/medinstech/perfstudio/main/docs/images/editor-solder-side.png)

The 3D view is a checking tool, not a picture. Three rules exist that a top-down view
cannot see at all: a part too tall for the case, a jumper trapped under a body that will
be soldered down on top of it, and a heat-sensitive part sitting too close to a hot one.

![The same board in 3D](https://raw.githubusercontent.com/medinstech/perfstudio/main/docs/images/board-3d.png)

## The guide has an order, and you can watch it

![The NE555 board assembling itself: parts first, then the board turns over and the copper goes on](https://raw.githubusercontent.com/medinstech/perfstudio/main/docs/images/assembly.gif)

Parts go in **shortest first** — a tall part fitted early stops the board lying flat on
the bench while the short ones are soldered. Then the board is turned over and the copper
goes on, and ICs are last for heat and ESD. A jumper that would end up trapped under a
part body is moved to the *first* phase, because by the time that part is down it is too
late.

The animation turns the board over halfway through for the same reason you would: a
perfboard is opaque, and fourteen of this build's twenty-two steps happen on the face you
cannot see from above. It is generated by playing the guide back through the same function
the 3D panel's assembly slider calls, so it cannot show an order the guide does not
actually give you.

## Running it

Requires **Python 3.12+**. The desktop app is PySide6 (Qt 6) with a VTK viewport.

```sh
pip install perfstudio

perfstudio                   # launch on a blank board
perfstudio some/board.perf   # ...or open a document
perfstudio --version
```

Qt and VTK are most of a 400 MB download the first time; there is no smaller build,
because the 3D view is a checking tool the application depends on rather than an optional
extra. From a clone, `pip install -e .` instead.

Or install it as an application: **[the releases page](https://github.com/medinstech/perfstudio/releases)**
carries a Windows installer, a Linux AppImage and a macOS disk image, each built and
smoke-tested by the tag itself. **None of them is code-signed**, so each warns on first
run and the release notes say how to get past it — a Windows EV certificate is ~$300/year
and Apple notarization $99/year. Running from source avoids the warning entirely. See
[docs/RELEASING.md](https://github.com/medinstech/perfstudio/blob/main/docs/RELEASING.md).

The interface speaks **English and Turkish** (`--lang tr`, or follow the system locale).

### A board from nothing

In the app: **File → Import KiCad Netlist** on `examples/ne555-astable.net`, accept the
offered placement, **Place → Auto-place Board** (`Ctrl+Shift+A`), **`Ctrl+R`** to route,
then **File → Export Build Guide** (`Ctrl+B`). That is the exact sequence the screenshots
above come out of — see [`tools/screenshots.py`](https://github.com/medinstech/perfstudio/blob/main/tools/screenshots.py).

You do not need KiCad: nets can be built by hand in the app or over MCP.

### Or open one that is already built

[Four examples](https://github.com/medinstech/perfstudio/blob/main/examples/README.md) ship as both the netlist and the finished board:

```sh
perfstudio examples/lm317-supply.perf
```

| | what it is there to show |
|---|---|
| `ne555-astable` | the starting point — a 555 flashing an LED |
| `lm317-supply` | a TO-220 regulator, so the heat rule has something to measure |
| `lpb1-booster` | built on **FR-2**, the phenolic board whose pads lift |
| `arduino-io-shield` | two headers, which is what a shield mostly is |

All four route to completion, match their schematics under LVS and carry no DRC error —
`tests/test_examples.py` asserts it on every commit.

### From an agent

The MCP server drives the identical command bus, so undo works across both:

```sh
pip install -e ".[mcp]"
claude mcp add perfstudio -- python -m perfstudio.mcp
```

Forty-four tools, every hole addressed the way people talk about perfboard (`A1`, `C7`,
`AC12`) and never as raw coordinates. See [docs/MCP.md](https://github.com/medinstech/perfstudio/blob/main/docs/MCP.md) for the tool list,
the JSON config other clients want, and the rest of the setup.

### Headless

Renders 2D/3D/PDF to files, runs DRC and LVS and prints timings, with no display. It is
how the visual output is exercised in CI, and the fastest way to check that a rendering
change did not crash:

```sh
python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf
```

## How it is built

The document is **immutable** and every mutation is a command dispatched through one
bus — which is what makes undo work across a mixed human/agent session. The engine is
**pure**: no clock, no RNG, no filesystem, no Qt or VTK below `ui/`. The placer's
annealing is seeded, so the same document and the same seed give the same board.

That purity is load-bearing rather than decorative. This Python engine is a port of the
TypeScript one still in `packages/`, and its acceptance criterion was never "the tests
pass" but "it produces byte-identical results to the implementation it replaces" —
golden fixtures in `tools/diffcheck/`, down to the last IEEE-754 double.

```
src/perfstudio/            the engine: document model, command bus, connectivity,
                           router, autorouter, placer, DRC, LVS, persistence
src/perfstudio/guide.py    the soldering guide, and guide_export.py for HTML/CSV/JSON
src/perfstudio/stripboard.py  the board whose copper arrives joined, and striproute.py
                           for the cuts-and-links planner that designs on one
src/perfstudio/parsers/    KiCad netlist importer
src/perfstudio/ui/         Qt application: 2D editor, VTK 3D view, 1:1 PDF export,
                           and headless.py, the no-display run CI checks the output with
src/perfstudio/mcp/        the MCP server (docs/MCP.md)
examples/                  a netlist to import
tests/                     1363 tests; the engine is mypy --strict clean
packages/                  the original TypeScript engine, kept as the reference the
                           Python port is proved against
```

The 61 THT footprints are **generated from numeric parameters**, not shipped as assets —
no mesh library, no share-alike licence to inherit. The same spec that draws a part in 2D
extrudes its body in 3D, so the two cannot disagree.

## Where it is going

Done: the editor, the library, connectivity and LVS, DRC, the router and the placement
optimiser, the build guide with rendered step images and assembly playback, the 1:1 PDF
export, the MCP server, TR/EN localisation, the three-platform packaging that a `v*` tag
runs, and the update check that tells you a release exists and fetches it (**Help ▸ Check
for Updates**; it verifies the download against the release's `SHA256SUMS` and then hands
it to you — running it stays your click).

Next, in the order [PLAN.md](https://github.com/medinstech/perfstudio/blob/main/PLAN.md) §11 puts them:

- **The dogfood build (M5).** Somebody has to solder a real board from a generated guide.
  Until that has happened, every claim on this page is a claim about software rather than
  about a working circuit. It is also the one thing on this list that a stranger can do
  for the project — there is [an issue template for it](https://github.com/medinstech/perfstudio/blob/main/.github/ISSUE_TEMPLATE/board_i_could_not_build.yml).
- **Code signing.** A Windows EV certificate is ~$300/year and Apple notarization
  $99/year, so until then the installers warn on first run and the release notes say how
  to get past it.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](https://github.com/medinstech/perfstudio/blob/main/CONTRIBUTING.md)
first — it covers how to run the suite, which checks are gates and which are not, and one
licence boundary that matters more here than in most projects: **do not read or port code
from the GPL-licensed tools in this space.** PerfStudio is clean-room with respect to
them, and that has to stay true. The record is in [docs/prior-art.md](https://github.com/medinstech/perfstudio/blob/main/docs/prior-art.md).

## Licence

Apache-2.0. See [LICENSE](https://github.com/medinstech/perfstudio/blob/main/LICENSE) and [NOTICE](https://github.com/medinstech/perfstudio/blob/main/NOTICE).
