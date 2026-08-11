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
