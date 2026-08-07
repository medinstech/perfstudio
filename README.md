# PerfStudio

Design circuits on perfboard the way you would on a PCB — then get a soldering guide
you can actually build from.

> **Status: pre-alpha.** The architecture is settled and the engine is being built.
> Nothing here is usable yet. See [PLAN.md](./PLAN.md) for the full design.

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

## Building

Requires Node 22+ and pnpm.

```sh
pnpm install
pnpm build
pnpm test
```

The desktop shell (Tauri v2) additionally needs a Rust toolchain; it is not wired up yet.

## Repository layout

```
packages/core        document model, command bus, connectivity, DRC, LVS, router
packages/parsers     KiCad netlist and footprint importers
packages/render2d    Canvas2D board renderer (host-agnostic, runs headless too)
packages/mcp         MCP server for coding agents
tools/bench-3d       three.js performance harness for the M0 webview decision
```

## Licence

Apache-2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

PerfStudio is written clean-room with respect to the existing GPL-licensed tools in
this space; see the NOTICE file.
