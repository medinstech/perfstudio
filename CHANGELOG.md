# Changelog

Everything notable that changes in PerfStudio is written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two version numbers exist in this project and they move independently:

- **The application version**, the one below, single-sourced from
  [`src/perfstudio/version.py`](./src/perfstudio/version.py). While the major version is
  0, a **minor** bump is where breaking changes land.
- **The document format version** (`DOCUMENT_FORMAT_VERSION` in `model.py`), bumped only
  when a `.perf` file written by an older build needs migrating in order to load. It is
  at **1** and has never moved.

The release ritual is in [docs/RELEASING.md](./docs/RELEASING.md), and
`tests/test_version.py` fails if this file and `version.py` disagree in either
direction — so the version cannot be bumped without a changelog entry, or a section
closed without a bump.

## [Unreleased]

### Added

- **The placement optimiser** (PLAN.md §6.3, `placer.py`): seeded simulated annealing
  over translate/rotate/swap, with a cost of HPWL + rail alignability + courtyard
  overlap + pin collisions + off-board pins + edge-seeking connectors + heat proximity.
  Deterministic — same document and seed, same board. Reachable from **Place →
  Auto-place Board** (Ctrl+Shift+A), which shows what it found and what it bought before
  moving anything, and **Try Another Arrangement**, which advances the seed.
  - Candidates are chosen by **routing** each one rather than by trusting HPWL. Measuring
    is what settled it: on NE555 one candidate with 152 mm of HPWL routes for 191 while
    another with 145 mm routes for 209, because half-perimeter cannot see that a shorter
    net crosses three others. The cheap heuristic searches, the expensive truth decides.
  - On the fixtures: NE555 5 → 3 insulated wires (routing cost 202 → 151); the same
    circuit from a grid import 7 → 3 with 2 unroutable connections becoming 0; `dense`
    3 → 0 and its 6 courtyard overlaps cleared; `sparse` 2 → 0.
- `component.moveMany`, so a whole placement is one undo step — the counterpart of
  `conductor.addMany`. All-or-nothing, and it refuses a locked part, an off-board anchor
  or the same component twice.
- Headless mode reports a dry-run placement, so CI has a number that moves when either
  the placer or the router changes.
- **The soldering guide** (PLAN.md §7, milestone M5, `guide.py` + `guide_export.py`) —
  the thing the project exists to produce. Nine phases in build order, a step per part
  with its hole addresses, lead-bend pitch and orientation, a step per connection with
  its path, length and estimated resistance, a wire cut list, a spine list and a BOM.
  **File → Export Build Guide** (Ctrl+B) writes four files; headless mode writes them too.
  - **Verification checkpoints**, which is the part no competing tool has. Continuity
    comes from the schematic's own nets and lands in the phase that finishes each net.
    Isolation comes from DRC: every R5′ proximity warning — a solder trace running
    0.6 mm from another net's pad — becomes a specific probe, so the risk the tool
    predicted and the measurement the user performs come off one list. Long runs get an
    end-to-end resistance expectation computed from the same model DRC prints.
  - Polarity is read from the registry's **pin names**, not from a convention about
    pin 1, because no one convention covers an electrolytic (pin 1 is `+`), an LED
    (pin 1 is the anode) and a diode (pin 1 is the cathode) at once.
  - The HTML is one self-contained offline file with tickable steps and progress in
    `localStorage` — no CDN, no fonts, no network, so it still opens from a USB stick on
    a phone in five years.
  - Anything the guide cannot cover — no netlist, an unknown footprint, an open net, a
    DRC error — is reported as a warning rather than producing a quietly shorter guide.
- `drc.trace_electrical` is public, so the guide and DRC rule 9 quote one resistance
  model rather than two.
- **The MCP server** (PLAN.md §9, milestone M6, `perfstudio.mcp`): 31 tools over stdio or
  streamable HTTP, driving the same command bus the GUI does, so an agent's edits undo
  the same way and land in the same journal. `python -m perfstudio.mcp`, or
  `perfstudio-mcp`. Setup and the full tool list are in [docs/MCP.md](./docs/MCP.md).
  - Holes are addressed as `C7` everywhere — there are no raw coordinates in the API,
    and a test enforces it.
  - A refused command comes back as data with a code, not as an exception; only
    malformed input raises, and the message names what would have worked.
  - `BoardSession` holds every operation and imports no MCP at all, so the tools are
    tested by calling them — a test that stands up a stdio server tests the transport.
  - `examples/ne555-astable.net` ships as something to import, and the end-to-end test
    takes a blank board through import → place → optimise → route → verify → guide with
    7/7 nets matched, 0 opens, 0 shorts, 0 DRC errors and no guide warnings.

- **Rip-up and re-route** (`autoroute.plan_reroute`, `conductor.replace`,
  **Route → Re-route Everything** / **Re-route Nets of Selection** (Ctrl+Alt+R), and the
  `reroute` MCP tool). Autoroute only *adds*, which is right for finishing a board and
  wrong after a part has moved: the copper laid for the old position still joins the
  right pins, so it is neither stale nor floating nor redundant, and routing again puts
  more copper beside it. Measured on the NE555 fixture — 14 conductors routed fresh, 16
  after moving one resistor and autorouting again, none of them removable without
  disconnecting something, and 14 again after a re-route. Ctrl+R now notices when a net's
  parts have moved since it was routed and offers to re-route it instead.

### Changed

- **Conductors are drawn at physical widths.** They were set for legibility alone, which
  made every solder trace wider than the pads it joins and turned a routed board into a
  diagram of coloured bars with a board somewhere underneath. Solder beads now sit inside
  the pad, a wired trace shows its copper spine as a core, and bare wire is half a
  millimetre.
- **Red is no longer a conductor colour.** It is the error and R5′ risk colour, and it
  was also every insulated wire, so a completely correct board looked alarming and a real
  risk had nothing to stand out against. Insulated wire takes its **net's** colour
  instead — the same convention the build guide's cut list prints, so the screen and the
  list someone works from cannot disagree about which wire is which.
- The solder side hatches each part's footprint, so it is clear something is on the other
  side without drawing a body as seen from above — which is how a board gets soldered
  backwards. The hatch deliberately carries no cathode band, pin-1 notch or tab: those
  are moulded into the top of a part and cannot be seen from below.

### Fixed

- `bodies.polarity_pin_offset`'s docstring claimed pin 1 is "the cathode of a diode or
  LED", which the registry contradicts for the LED (its pin 1 is named `A`). The drawing
  was right; the sentence a reader would have believed was not.

- Versioning. `perfstudio.__version__` is single-sourced from `version.py`, the wheel's
  version is derived from it rather than repeated in `pyproject.toml`, and this file
  exists. `perfstudio --version` prints the app version, the document format version and
  the Python/PySide6 it is running on, which is what a bug report should quote.
- The version is in the window title and in **Help → About**, so a screenshot says which
  build produced it.

## [0.3.0] - 2026-08-11

The release where the application became usable by someone who did not write it: parts
can be added, netlists imported, and the router stopped producing boards that cannot
physically exist.

### Added

- **Parts dock** over the 61-footprint registry, with a ghost under the cursor showing
  the real body at real size and the holes it will occupy. Five engine capabilities —
  the footprint registry, `component.place`, the netlist parser, `netlist.import` and
  `component.rotate` — had been reachable only from Python until now.
- **File → Import KiCad Netlist**, which is how connections get defined at all
  (PLAN.md D3: netlist import and visual editing, deliberately not a schematic editor).
  The import offers a first-pass grid placement, inferring each footprint from the
  reference letter and the pin count the netlist itself reveals.
- Rotate, mirror, lock, delete and arrow-key nudge, all dispatched through the command
  bus, so one Ctrl+Z undoes each and an agent driving the same board sees them
  identically. References count up from the board rather than from a counter, so undo,
  delete and reload cannot desynchronise them.
- **Ratsnest** computed over physical groups rather than schematic pins, so it shrinks
  as work is done instead of re-proposing connections that are already routed.
- **Autoroute** (`autoroute.py`): criticality ordering, the rail/bus strategy of
  PLAN.md §6.2 for high-fan-out nets, and rip-up & reroute. Two strategies compete per
  net and the cheaper one wins.
- `RouterOptions.crossing_policy`: when a crossing is unavoidable, `hop` runs a solder
  trace up to the obstacle with one short insulated jumper over it (the default, and
  what a person actually does), `wire` runs one insulated wire end to end, and `refuse`
  reports what solder cannot reach as unrouted rather than inventing a wire.
- DRC rule `conductor-crossing`, and detection of **stale conductors** — copper left
  behind by a moved part — cleared before routing under its own undo entry.
- `conductor.addMany` / `conductor.deleteMany`, so a whole routing plan is one undo step.
- 15 parametric body archetypes, generated from the registry's real dimensions and
  shared between the 2D and 3D views from one table (PLAN.md D6: parametric, no mesh
  library, no share-alike licence in an Apache-2.0 project). Diode cathode bands,
  electrolytic polarity stripes, DIP pin-1 notch, LED cathode flat, TO-220 metal tab.
- A dark theme, a toolbar, a Nets dock and hole-address rulers — the address language
  every DRC message and the build guide will speak.

### Changed

- Bodies are drawn from the registry's dimensions instead of the **courtyard**, which is
  a padded DRC boundary and not the part: `r-axial-3`'s courtyard is 10.16 mm around a
  5 mm resistor.
- The 2D solder side shows pads, cut lead ends and solder-side conductors rather than
  mirrored component bodies. Turn a board over and the parts are on the far face; that
  is the side where a misreading gets soldered in.
- Ruler and reference labels are sized in screen pixels, holding their size while the
  board zooms.
- DRC rule 4 compares conductor **geometry** (`geometry.segments_touch`) rather than hole
  lists. Two wires at an angle cross in the middle of a cell and share no hole, which is
  the ordinary case for point-to-point wiring, so the commonest defect had been invisible.

### Fixed

- The 3D camera reset on every command, so any rotation vanished the moment you did
  anything; and "Reset Camera" did not reset, because `ResetCamera` preserves view
  direction and the tilt is relative.
- Moving a part left its old copper behind and autoroute added more beside it.
- Component references were drawn near-black on dark green FR4, so no part was labelled.
- The bus id generator restarted at zero, so the first edit to a loaded document was
  refused as a duplicate id.
- LVS crashed formatting a message about an off-board pin, using the strict hole encoder
  in three places — it crashed on exactly the defect it exists to report.
- Two Qt "internal C++ object already deleted" crashes: `scene.clear()` emits
  `selectionChanged`, and a loop that dispatches rebuilds the scene under itself.
- Selection was lost on every scene rebuild, so pressing rotate twice rotated once.
- Qt's offscreen platform ships no font database on Windows, so every headless label
  rendered as a missing-glyph box while looking perfect in the GUI.

### Notes

- The crossing fix diverges from the reference TypeScript engine on 3 of the 45 golden
  routes, because that engine has the same blind spot. Each divergence is recorded in
  `INTENTIONAL_ROUTE_DIVERGENCES` with its reason and asserted individually, so the
  differential proof stays strict everywhere else and a divergence appearing anywhere
  new fails the suite. Same for DRC's `PYTHON_ONLY_RULES`.
- 715 tests, `mypy --strict` clean across 25 modules.

## [0.2.0] - 2026-08-07

### Added

- **The desktop application.** The Qt prototype promoted into `src/perfstudio/ui/` and
  wired to the real engine: the scene is built from the real model and persistence
  layer, and the prototype's parallel `board_model` is gone rather than kept alongside.
- Dragging a part mutates nothing. It computes a snapped, uncommitted anchor, and on
  release one call dispatches `component.move` through the real `CommandBus` — which is
  what makes Ctrl+Z work without the UI implementing undo at all.
- DRC and LVS run after every successful command, with counts in the status bar, a dock
  listing violations by rule, and red rings on the holes a solder trace runs too close
  to another net. DRC measures 1.0 ms on the sixteen-component fixture.
- VTK 3D viewport, and an exact **1:1 PDF export**: a ten-hole span at 300 dpi lands on
  300.000 px against 300.000 expected, on both the component-side and the mirrored
  solder-side sheet.
- Headless mode (`--headless`), which renders 2D/3D/PDF to files, runs DRC and LVS and
  prints timings with no display — how the visual output is exercised in CI.

### Fixed

- `commands.py` formatted its off-board refusal with the strict `coord_to_hole_ref`,
  which rejects negative columns by design, so dragging a part off the *left* edge
  raised out of `dispatch` instead of returning `ok=False`. `dispatch()`'s contract is
  that it never raises for bad input, and the CLI and MCP server depend on that.

## [0.1.0] - 2026-08-07

The engine, in Python, proved rather than merely tested.

### Added

- Document model, command bus with undo/redo, geometry, connectivity (union-find),
  footprint registry, occupancy index, router, DRC, LVS, `.perf` persistence and the
  KiCad netlist parser.
- **The differential proof.** `tools/diffcheck/generate.mjs` runs the reference
  TypeScript engine over fifteen boards and freezes its full output: physical nets,
  every DRC violation, the LVS result, the continuity and isolation checklists and the
  routes it chose. The Python port reproduces it — connectivity on all fifteen, all 45
  golden routes to six decimal places, all fifteen DRC and LVS cases, and `.perf`
  round-trips byte-identically.
- `mypy --strict` clean across the package.

### Fixed

- Three cross-runtime traps that a passing test suite cannot catch, each found by
  comparing against the original implementation rather than by trusting a green suite:
  `JSON.stringify(1.0)` is `"1"` while `json.dumps(1.0)` is `"1.0"`, so `persist.py`
  reimplements the ECMA-262 `Number::toString` algorithm; V8 and CPython disagree by one
  ULP on `sin`/`cos` at π/4; and JavaScript's `Math.round` sends a half toward positive
  infinity while Python's `round()` uses banker's rounding, which matters because the
  router samples holes along a wire run and those samples land exactly on `.5` routinely.
- A defect in the fixture generator itself: it ran the engine over the in-memory
  document, whose components sit in id-creation order, but wrote the `.perf` sorted by
  id. Below ten components those orders coincide, which is why fourteen cases looked
  fine. Every proof built on it was being verified against an ordering that cannot occur.
- The S-expression tokenizer tested whitespace with `ch in " \t\n"`, and in Python an
  empty string is a substring of everything, so end-of-input looked like whitespace and
  the scanner spun forever.

## Before 0.1.0

Version numbers start at 0.1.0, the point where the Python engine first reproduced the
reference output. Before it: the original TypeScript engine under `packages/`, kept as
the reference the port is proved against (`19999da`, `b31764a`, `3d83fde`), and a
throwaway Qt/VTK prototype that settled the desktop stack question (`2c7daa6`).

The 0.1.0–0.3.0 entries above were reconstructed from the commit history when versioning
was introduced during 0.4.0 development, so they are accurate but were not written at
release time. Their compare links point at commits rather than tags for the same reason;
from v0.4.0 onwards every release is tagged.

[Unreleased]: https://github.com/medinstech/perfstudio/compare/e36ac8c...HEAD
[0.3.0]: https://github.com/medinstech/perfstudio/compare/e66e3f8...e36ac8c
[0.2.0]: https://github.com/medinstech/perfstudio/compare/11cb8af...e66e3f8
[0.1.0]: https://github.com/medinstech/perfstudio/compare/2c7daa6...11cb8af
