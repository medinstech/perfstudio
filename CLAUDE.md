# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
pip install -e ".[dev,mcp]"      # PySide6 + VTK + pytest/mypy/ruff + the MCP server

pytest                            # the whole suite
pytest tests/test_router.py       # one file
pytest tests/test_drc.py::test_name -x       # one test, stop on first failure
pytest -k "proximity"             # by name fragment

mypy --strict src tests           # the engine must stay strict-clean
ruff check src tests
ruff format src tests

perfstudio                        # launch on a blank board
perfstudio some/board.perf        # ...or open a document
python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf
python -m perfstudio.mcp          # the MCP server (docs/MCP.md)
```

`--headless` renders 2D/3D/PDF to files, runs DRC + LVS and prints timings with no
display. It is how the visual output is exercised in CI, and the fastest way to check
that a rendering change did not crash.

UI tests run under `QT_QPA_PLATFORM=offscreen` (set in `tests/test_ui.py` before PySide6
is imported). Qt's offscreen plugin ships no font database on Windows, so tests that
assert on rendered *text* are skipped there — see the `skipif` guards in `test_ui.py`
before adding one.

The TypeScript side (`packages/`, pnpm + vitest) is the retired reference engine. Only
touch it to regenerate golden fixtures: `pnpm build && node tools/diffcheck/generate.mjs`.

## Architecture

### The document is immutable; every mutation is a command

`model.py` dataclasses are all `frozen=True, slots=True`. Nothing writes to a
`PerfDocument` in place. A mutation is a `CommandDefinition` in `commands.py` dispatched
through `CommandBus` (`command.py`), which builds a new document with
`dataclasses.replace` and records a `HistoryEntry`. The GUI, the MCP server, the headless
CLI and a replayed journal all go through that one bus — which is what makes undo/redo
work across a mixed human/agent session.

Adding a mutation means adding a command, not writing to the document from the caller.

**Commands enforce document integrity; DRC reports design quality.** Ids unique,
references resolve, paths on the board, model invariants hold → hard error, mutation
refused. Overlapping bodies, bridging risk, inadequate copper → reported by `drc.py`,
never refused. When deciding where a new check belongs, ask whether the result is still
a *document* (DRC) or not (command).

### The engine is pure

`src/perfstudio/` outside `ui/` and `mcp/` has no clock, no RNG, no filesystem, and no Qt
or VTK import. `persist.py` turns documents into strings and back; the *host* reads and
writes files. Timestamps (`meta.modified`) are stamped by the host, never the engine.
The placer's simulated annealing is seeded, so same document + same seed = same board.

Breaking this breaks the differential proof below.

### The differential proof

This Python engine is a port of the TypeScript one still in `packages/`. Its output is
frozen in `tools/diffcheck/golden/` and the tests assert against it byte-for-byte —
"produces identical results to the implementation we are replacing" rather than "all
tests pass". Three things depend on it:

- `test_persist.py::test_golden_round_trip_byte_identical` — every `*.perf` fixture must
  re-serialize to the exact bytes on disk. **This is why new optional fields must be
  omitted from the JSON when they hold their default** (see `stripAxis` for the pattern).
  A field emitted unconditionally breaks all 15 fixtures.
- `test_connectivity.py` / `test_occupancy_golden.py` — extracted nets must reproduce the
  `*.expected.json` arrays exactly.
- `test_autoroute.py` — the golden routes reproduce only with the default cost table, so
  changing `DEFAULT_ROUTER_COSTS` is a deliberate act with fixture regeneration attached.

`persist.py` hand-rolls its JSON writer to match `JSON.stringify(x, null, 2)` byte for
byte (whole-number floats print as `1`, not `1.0`), and every object's key order comes
from an explicit `*_KEY_ORDER` tuple, never dict insertion order.

### Two version numbers

`version.py` holds the application version — the single source, read by `pyproject.toml`,
the window title and `--version`. `model.DOCUMENT_FORMAT_VERSION` tracks the `.perf`
format and is bumped **only when an older file needs migrating in order to load**, with
the migration written into `MIGRATIONS` in the same commit. It is at 1 and has never
moved. `tests/test_version.py` fails if `version.py` and `CHANGELOG.md` disagree in
either direction, so a version cannot be bumped without a changelog entry. Ritual in
`docs/RELEASING.md`.

### A perfboard connection is not one thing

The idea the project rests on. `ConductorKind` has six physically distinct members (lead
bend, solder trace, wired solder trace, bare wire, insulated wire, top jumper) with
different costs, limits and failure modes. Two predicates in `model.py` carry most of the
weight:

- `contacts_every_path_hole` — a solder trace is soldered down at *every* pad it crosses;
  a wire touches only its two endpoints and merely lies over the holes between. Getting
  this wrong silently produces a board wired differently from what the screen shows.
- `is_crossing_blocked` — what occupies the copper plane and therefore cannot cross.

This is why `connectivity.py` ("what is electrically joined") and `occupancy.py` ("what
is physically in the way") are separate modules over the same conductors.

`SolderTraceConductor.path` must be an orthogonal chain — solder cannot span a diagonal
gap. `geometry.validate_orthogonal_chain` is the only adjacency check in the codebase;
a hand-edited file that violates it loads with a *warning* and is reported by DRC, rather
than locking the user out of their own project.

### Hole addressing

`HoleCoord(col, row)` is 0-indexed from the top-left, row growing downward.
`HoleRef` ("A1", "AC12", bijective base-26) is the language every user-facing message
speaks — the guide, DRC, the router's explanations, the MCP tools. `geometry.py` owns the
conversion:

- `coord_to_hole_ref` is strict and round-trips with `hole_ref_to_coord`.
- `format_hole` never raises — use it in any message, since off-board coordinates are
  negative by definition and that is exactly what the failing checker needs to print.

**`board_size_mm` vs `hole_span_mm` is a real trap.** The substrate extends half a pitch
past the outermost hole centres — plus `board.border_x_mm` / `border_y_mm` on a board cut
with a printed border — so they differ. Mirroring the board to the solder side reflects
about the *hole span*; reflecting about the substrate size shifts every hole and the user
solders the board backwards without the view ever looking wrong. Rule of thumb: holes and
routing use `hole_span_mm`; substrate, printing and 3D use `board_size_mm` /
`board_outline_mm`, and anything asking "how much bare board is outside the grid" uses
`board_edge_margin_mm(board, axis)`. Nothing recomputes any of them locally —
`ui/view2d._outline_rect` and `ui/view3d._board_size_mm` both delegate.

The border is **per axis** because real boards are not square about it (a 5 × 7 cm board
has ~2.1 mm at the sides, ~4.5 mm top and bottom), and the 1:1 PDF gets taped to the
physical board. `geometry.STANDARD_PRESETS` carries the sizes suppliers stock, keyed on
the advertised centimetres; `board_from_preset` *solves* the border from the size and the
hole count rather than quoting it.

### Three rules only the third dimension can see

`component-too-tall`, `jumper-under-body` and `heat-proximity` are why the 3D view is a
checking tool and not a picture (PLAN.md §8.4). Each of them has a second consumer that
must not be allowed to disagree with it:

- **`heat-proximity` and the placer** read one set of facts —
  `model.HEAT_SOURCE_ARCHETYPES` / `HEAT_SENSITIVE_ARCHETYPES` / `HEAT_CLEARANCE_MM` —
  and both measure between **body-box centres**, never anchors. An anchor is pin 1,
  which on a TO-220 is one end of the tab: rotating the part moves the body and not the
  anchor. Two numbers here would mean the optimiser separating parts to a standard DRC
  declines to confirm. `EDGE_SEEKING_ARCHETYPES` stays in `placer.py` on purpose — it is
  a placement preference, not a fact any rule checks.
- **`jumper-under-body` and the router** both ask `occupancy.body_covers`. The router
  refuses to lay such a jumper at all, so DRC deliberately checks *less*: only holes
  strictly between the jumper's ends, because a body's bounding box covers its own pin
  holes and counting the ends would flag every jumper that lands on a part. DRC must
  never object to copper the router was willing to lay.
- **`jumper-under-body` and the build guide.** A flagged jumper moves from phase 7 to
  phase 1, because by phase 7 the part standing over it is already soldered down. The
  phase and the part-step note both come from `guide.trapped_jumper_ids`, so the order
  and the note cannot drift.

`doc.height_limit_mm` is `None` until someone says otherwise, and `component-too-tall` is
silent until then — with no case chosen there is nothing to be too tall for.

### A pad is not always round, and the board may say where it is

`board.pad_shape` can be `oblong`, which gives a pad **two different neighbour gaps** —
tight along its long axis, comfortable across it. That is not decoration: R5'
(`solder-trace-proximity`, the most valuable rule in `drc.py`) is entirely about that gap,
so it is measured per pair by `geometry.copper_gap_mm`, which also accounts for a pad
widened into an edge-connector finger. Never reintroduce a single board-wide
`pitch - pad_diameter`.

`board.labels` is the `A`..`Z` / `01`..`22` legend printed on the board itself. It is the
same address space `coord_to_hole_ref` produces; `printed_row_label` only adds zero
padding for *rendering*, and `hole_ref_to_coord` still rejects `A07`. Silkscreen is
physical, so it scales with the board (`scenetext.draw_physical_label`) — the exact
opposite of the rulers and reference labels, which hold a screen size (`draw_label`).
Both exist because a millimetre-sized font is a fraction of a point, which some engines
draw as nothing while reporting no error.

`mounting_holes` and `edge_connectors` sit on the *document* (like `cuts`), so each is its
own command and its own undo step. A mounting bore removes copper from pads it was not
drilled on — `geometry.consumed_holes` is the single answer to which — and a pin left on
one is the only DRC *error* about physical impossibility rather than likely failure.

Three things here were got wrong first and corrected against photographs of real boards;
they are easy to get wrong the same way again:

- **Oblong pads on a real board are only the edge strip**, not the whole grid. The
  interior is round. "Oblong pad" and "edge-connector pad" are the same physical thing,
  which is why a finger replaces the grid pad rather than covering it —
  `geometry.holes_without_grid_pad` answers that for both faces.
- **Fingers stop short of the edge** (`inset_mm`); the strip outside them is where the
  row numbers are printed. `legend_strip_mm` asks the *document*, not the board, for that
  reason.
- **Corner mounting holes sit in the border** via `offset_x_mm` / `offset_y_mm`, eating
  no pads. Always go through `mounting_hole_centre_mm`, never `hole_to_mm(mount.at)`.

`board.single_sided` is the cheap phenolic board: copper on the solder side only. Both
renderers still draw the holes on the bare face — a face with neither holes nor pads is a
blank slab.

### Layering

```
model → geometry → connectivity / occupancy → drc, lvs, router, autoroute, placer, ratsnest
                                                        → guide → guide_export
                                                        → ui/, mcp/
```

`ui/view2d.py` works in millimetres (one scene unit = 1 mm), which is what makes the 1:1
PDF export exact without a fudge factor. `ui/view2d.hole_to_screen` / `screen_to_hole` are
the single place a hole becomes a scene position for either board side — every item that
needs a position routes through them rather than flipping signs locally.

Two rendering constraints that were measured, not guessed, and are documented at their
call sites: `PadGridItem` blits one pre-rasterised pad pixmap per hole (painting 6000
holes the obvious way took 124 ms/frame; a single even-odd `QPainterPath` took 5.8 s),
and `ui/scenetext.py` sizes annotation labels in **screen pixels** so they hold their size
as the board zooms — physical silkscreen scales with the board, annotations do not.

`ui/view3d.populate_renderer` refreshes actors in an existing renderer and deliberately
leaves the camera alone; only `apply_default_camera` may move the viewpoint.

### i18n

`ui/i18n.py` is a plain dict whose **keys are the English strings**, wrapped at each call
site with `t()`. `tests/test_i18n.py` scans the UI source and fails if the catalogue names
a string the interface no longer has, if a translation loses its `&` accelerator, or if
two items in one menu claim the same accelerator — so adding a menu item means adding its
Turkish entry and, if it is in one of the grouped menus, extending that test's group list.

Engine-generated text (DRC/LVS messages, rule ids, hole addresses, net and component
names) is never translated: it is compared byte-for-byte by golden fixtures, and the
addresses are the tool's vocabulary in every language.
