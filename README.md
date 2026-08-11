# PerfStudio

Design circuits on perfboard the way you would on a PCB — then get a soldering guide
you can actually build from.

> **Status: pre-alpha, and now end to end.** A netlist can go in and a soldering guide
> can come out: 2D editor, 3D view, placement optimiser, autorouter, DRC, LVS, the build
> guide, an exact 1:1 PDF export and an MCP server. What is missing is the dogfood test
> — nobody has yet built a real board by following a generated guide, and PLAN.md §11
> says M5 does not close until somebody has. See [PLAN.md](./PLAN.md) and
> [CHANGELOG.md](./CHANGELOG.md).

---

## What it does

Take a schematic netlist, lay it out on pad-per-hole perfboard, let the router work out
the connections, prove the result matches the schematic, and export a step-by-step
build guide with measurement checkpoints.

**Three things make it different from the tools that already exist:**

1. **A soldering guide with verification steps.** Not just "solder R1 here", but
   *"block 2 complete → U1 pin 4 to C3(−) must show continuity"* and
   *"before power-on: GND to V+ must read above 10 kΩ"*. Derived from the netlist,
   so it is exact rather than generic advice.
2. **Perfboard LVS.** The board's real connectivity is extracted and compared against
   the schematic. Opens, shorts and floating conductors are reported before you pick
   up the iron.
3. **Agent-native.** An MCP server, a headless CLI and a git-diffable project file,
   all driving the same command bus as the GUI.

## Connections are not all the same thing

Most tools model a perfboard connection as "a wire". Perfboard has six physically
distinct ways to join two points, each with its own cost, limits and failure modes —
and modelling that difference is what lets the router produce a layout that is
pleasant to actually solder:

| | what it is | notes |
|---|---|---|
| lead bend | a component leg bent to a nearby hole | effectively free, 3–4 holes |
| **solder trace** | adjacent pads joined with solder alone | orthogonal only; ~0.6 mm to the next pad |
| **solder trace, wired** | the same, over a tinned-wire spine | ~10× lower resistance, no length limit |
| bare wire | tinned wire on the solder side | cannot cross another bare conductor |
| insulated wire | may cross freely | costs preparation time |
| top jumper | insulated jumper over the component side | visible, occupies body space |

The 0.6 mm gap to the neighbouring pad is why solder traces are both so useful and so
easy to get wrong. PerfStudio scores that risk into the router's cost function, and
turns every flagged spot into a measurement step in the build guide.

## Running it

Requires Python 3.12+. The desktop app is PySide6 (Qt 6) with a VTK viewport.

```sh
pip install -e ".[dev,mcp]"
pytest                       # the test suite
perfstudio                   # launch the app on a blank board
perfstudio some/board.perf   # ...or open a document
perfstudio --version
```

A board from nothing, in the app: **File → Import KiCad Netlist** on
`examples/ne555-astable.net`, accept the offered placement, **Place → Auto-place Board**
(Ctrl+Shift+A), **Ctrl+R** to route, then **File → Export Build Guide** (Ctrl+B).

The same thing from an agent — the MCP server drives the identical command bus, so undo
works across both:

```sh
pip install -e ".[mcp]"
claude mcp add perfstudio -- python -m perfstudio.mcp
```

See [docs/MCP.md](./docs/MCP.md) for the tool list and the rest of the setup.

There is also a headless mode, which renders 2D/3D/PDF to files, runs DRC and LVS and
prints timings — it is how the visual output is exercised in CI, with no display:

```sh
python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf
```

## Repository layout

```
src/perfstudio/            the engine: document model, command bus, connectivity,
                           router, autorouter, placer, DRC, LVS, persistence
src/perfstudio/guide.py    the soldering guide, and guide_export.py for HTML/CSV/JSON
src/perfstudio/parsers/    KiCad netlist importer
src/perfstudio/ui/         Qt application: 2D editor, VTK 3D view, 1:1 PDF export
src/perfstudio/mcp/        the MCP server (docs/MCP.md)
examples/                  a netlist to import
tests/                     850+ tests; the engine is mypy --strict clean
packages/                  the original TypeScript engine, kept as the reference the
                           Python port is proved against (golden fixtures in tools/)
```

Still to come, in the order PLAN.md puts them: assembly animation and per-step rendered
images for the guide (§7.2, M4), i18n and packaging (M7), and the dogfood build that
closes M5.

## Licence

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

PerfStudio is written clean-room with respect to the existing GPL-licensed tools in
this space; see the NOTICE file.
