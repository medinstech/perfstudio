# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
pip install -e ".[dev,mcp]"      # PySide6 + VTK + pytest/mypy/ruff + the MCP server

pytest                            # the whole suite
pytest tests/test_router.py       # one file
pytest tests/test_drc.py::test_name -x       # one test, stop on first failure
pytest -k "proximity"             # by name fragment

mypy --strict src                 # `src` ONLY — see below
ruff check src tests --statistics # reports; NOT a gate — see below

perfstudio                        # launch on a blank board
perfstudio some/board.perf        # ...or open a document
python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf
python -m perfstudio.mcp          # the MCP server (docs/MCP.md)
```

The suite is ~1360 tests in about 40 seconds, so run all of it; there is no reason to
narrow to one file except while iterating.

**`mypy --strict src`, never `src tests`.** The engine is strict-clean and must stay
that way. The tests are not and never have been — `--strict` over `tests` reports ~267
errors, nearly all `no-untyped-def` on UI test helpers. CI gates on `src` alone, and
deliberately: gating on something already broken teaches everyone to ignore the red tick.

**Ruff is a gate; `ruff format` still must not be run casually.** `ruff check src tests`
is clean and CI fails on a finding. Two rules are switched off in `pyproject.toml` with
the reason at the switch: `E501`, because its 235 hits were prose a formatter could not
split either, and `RUF001`/`2`/`3`, because 137 of its 154 were the Turkish catalogue's
dotless ı — a rule that flags correct Turkish is a rule people learn to ignore.

Five suppressions in the source are load-bearing, not noise: `model.py`'s
`BoardMaterial`, `BoardType`, `BoardEdge`, `BodyArchetype` and `ConductorKind`, and
`router.py`'s `RoutingStyle`, keep the old `TypeAlias` spelling with `# noqa: UP040`
because they are read at run time by `get_args`, which returns an empty tuple for a PEP
695 `type` alias. Converting them made three completeness tests assert that an empty set
equals an empty set — and `BoardType` was found the same way later, refusing every board
type an agent asked for from a check that raises nothing.

`ruff format` would still rewrite 40 of the 57 files and point every line of blame in the
repository at a reformat. That is its own decision, not a side effect of another change.

`--headless` (`ui/headless.py`) renders 2D/3D/PDF into `headless_out/`, runs DRC + LVS
and prints timings with no display. It is the only step that exercises 2D, 3D and the PDF
export against a real board end to end, and the fastest way to check that a rendering
change did not crash. It inspects a document and never edits one. What the render LOOKS
like is a separate question, answered by `tests/test_render_golden.py` below.

CI runs the full three-OS matrix on every push. It was Linux-only off `main` while the
repository was private and minutes were metered; standard runners are free on public
repositories, so that restriction is gone. It earned its keep immediately — the first
full matrix found a VTK abort on Windows and two footprint goldens off by one ULP on
macOS arm64, neither of which Linux can see.

**A test that renders through VTK must carry `@requires_offscreen_gl`** (`tests/test_gl.py`).
Without a GL context VTK does not raise, it aborts the process, so an unmarked test does
not fail — it ends the run partway through with no summary. `test_every_vtk_touching_test_is_marked`
finds them by reading the sources, because marking them by hand missed two.

**What the render LOOKS like is checked by `tests/test_render_golden.py`**, not by the
headless PNGs, which nobody opens. It compares the mean colour of each cell of a 6 × 6
grid against `tests/render_signatures.json` — stable across renderers to a fraction of a
level, and 20+ levels away from a board that lost its parts. Re-bless with
`PERFSTUDIO_BLESS_RENDER=1` after looking at the render. The first attempt measured ink
coverage instead and was nearly useless (a perfboard is mostly board); that is written
down in the file so nobody tries it again.

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

### Footprints are generated, not shipped

`footprints.py` computes all 61 footprints from a handful of numeric parameters — zero
assets, no mesh library, no share-alike licence to inherit (PLAN.md D6). The same
`BodySpec` that a footprint carries is what `ui/bodies.py` extrudes in 3D, so the 2D
footprint and the 3D body cannot disagree.

Three conventions here are load-bearing:

- **The anchor is pin 1, at grid offset `(0, 0)`** — for two-lead, inline (TO-92,
  TO-220, headers) and DIP packages alike. It is a real physical pin in every case, never
  a geometric centre. This is the convention the heat-proximity rule above works around.
- **`body_outline` is the COURTYARD, not the body.** It is padded by
  `COURTYARD_MARGIN_MM` (half a grid step) so its bounding box contains every pin plus
  clearance, which is what overlap DRC needs. For `r-axial-3` it spans 10.16 mm while the
  resistor body is 5 mm long. Drawing the outline as the part draws a box half again too
  big — the specific mistake that made both renderers look wrong. The real body comes
  from `BodySpec.dims` via `bodies.placement_for`.
- **Pin offsets are integer steps on `STANDARD_PITCH_MM`** regardless of the board's own
  pitch; `body_outline` and `body_height` are always millimetres.

The file is a line-for-line port whose acceptance criterion is bit-for-bit reproduction
of `tools/diffcheck/golden/footprints.expected.json`, down to the last IEEE-754 double.
Preserve the original's arithmetic and generation order, not merely its intent.
`BodySpec.dims` keys stay camelCase (`"rowSpacing"`, `"tabHeight"`) because `dims` is a
free-form dict, not a model field — there is nothing there for `persist.py` to rename.

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

### The guide's order is physical, and its checks are derived

`guide.py` has nine phases (`PHASE_TITLES`, 0–8) and the order is not editorial: parts go
in **shortest first**, because a tall part fitted early stops the board lying flat on the
bench while the short ones are soldered. `PHASE_BY_ARCHETYPE` is that height ordering
written down; `PHASE_BY_CONDUCTOR` puts the solder side in phase 6 and long insulated
wire in 7. ICs go last (phase 8) — heat and ESD.

Two derivations must not be replaced with conventions:

- **Polarity comes from the registry's pin NAMES** (`'+'`, `'K'`, `'A'`), never from a
  convention about pin 1 — an electrolytic's pin 1 is its positive lead, an LED's is its
  anode, a diode's is its cathode. A rule keyed on pin 1 produces a dead board.
- **Checkpoints come from the same lists that predicted the risk.** Continuity is read
  off the schematic's nets; isolation probes are generated from DRC's `R5'`
  (`solder-trace-proximity`) hits. The risk the tool predicted and the measurement the
  user performs are one list, which is the whole point.

### The MCP server is behaviour in one file, protocol in the other

`mcp/session.py` holds every tool's actual behaviour; `mcp/server.py` only binds names,
docstrings and transport. So the tools are tested by calling them — no client, no stdio,
no event loop. Put logic in `session.py`; a test that needs a live session is testing the
transport, and the interesting failures are never there.

**The stdout trap (PLAN.md §9.1): on stdio, stdout IS the protocol.** One stray `print`
corrupts the stream and the client reports something baffling and unrelated. Nothing
under `perfstudio.mcp` may print; logging is configured to stderr before anything else,
and the render tools import Qt and VTK *lazily inside the tool* rather than at module
scope — those imports are the real risk, since the engine itself has no prints.

Every result crossing this boundary is plain JSON-able data, and every hole is given as
its **address** (`"C7"`), the same language DRC and the guide speak. A refused command
returns `{"ok": false, "code": ..., "message": ...}` rather than raising, matching
`CommandBus.dispatch`'s contract — an agent must be able to try something and be told no.

### Stripboard is the board where the copper is subtracted

`board.type` is `pad-per-hole` or `stripboard`, and it is not a display setting. On
stripboard whole rows arrive already joined, so **`connectivity.py` has a fourth rule**
beside its three: on that board type the BOARD joins the holes along an uncut run, and
nobody soldered those connections. Only holes something is soldered into take part — a
strip physically joins all thirty holes in its row, and registering the rest would put
every empty pad into a net, which is what the module's own docstring says not to do.

`stripboard.py` owns the geometry, and one decision governs it: **a cut destroys the
copper AT a hole** (`TrackCut.at`), because that is how a track is broken — a drill turned
by hand, which takes the pad with it. A pin left in a cut hole is soldered to nothing,
which is DRC's `cut-track-conflict`, the twin of `mounting-hole-conflict`.

`striproute.py` is that board's router and it subtracts before it adds: cuts first, then
links over the COMPONENT side — the solder side is one sheet of parallel copper, and a
wire laid across it there shorts every strip it crosses. Both halves commit as one
command (`stripboard.apply`), because separately one Ctrl+Z leaves the board cut apart
with nothing linking it, or linked with nothing cut, which is a short. Pairs it cannot
separate (adjacent pins, no hole between them to drill) are reported, never routed around:
the fix is to move a part, and `placer.py` is what moves parts. It prices exactly those
pairs (`PlacementWeights.strip_conflict`) by exactly this rule — both modules read
`stripboard.MIN_SEPARABLE_GAP`, the same one-fact-two-consumers shape as `heat-proximity`
and the placer. Two more things follow from the board type there and were wrong before:
the alignment term counts **only the strip axis** (a shared column joins nothing on a
horizontal-strip board), and a candidate placement is judged by `plan_stripboard`, never
by `autoroute.py` — ranking a stripboard with the pad-per-hole router scores it on a build
nobody is going to follow.

### Layering

```
model → geometry → stripboard → connectivity / occupancy
                                    → drc, lvs, router, autoroute, placer, ratsnest, striproute
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

**Appearance is a view setting, not a document field.** Solder-mask colour changes
nothing about the circuit, and adding it to `Board` would reopen the byte-for-byte `.perf`
format for a cosmetic preference — so `ui/boardcolors.py` owns it, it is chosen from the
View menu, and it is not saved. Each scheme carries the 2D and 3D colours *together*, so
a board cannot be green in the editor and blue in the 3D view. Reach for this test on any
new visual option before adding a model field.

Three colour tables exist and answer different questions: `ui/theme.py` colours the
**application** (deliberately dim, so the eye lands on the board), `view2d`'s theme block
colours the **physical object** (FR4 green, tinned copper, solder grey), and
`ui/bodies.py` colours **parts** — in one table, so a resistor is the same beige in the
editor, the 3D view and the guide's step images.

`parsers/` is pure string-to-data: `sexpr.py` knows no KiCad semantics (just text to
tree), `kicad.py` maps a KiCad 6+ export netlist onto `Net` / `NetNode`. Reading the file
from disk is the caller's job, as everywhere else in the engine.

`ui/headless.py` is the `--headless` run, its own module rather than the bottom of
`main.py`: it is a program in its own right and being importable alone is what lets a
test call it. `ui/clipboard.py` is copy/paste — text in, `block.place` payload out, no
widgets — so the interesting half is tested by calling it.

**The window watches the open file** (`QFileSystemWatcher`, PLAN.md §9.3). With nothing
unsaved it reloads itself when the file changes; with unsaved edits it refuses and says
so, because losing somebody's work to a background event is the one outcome that must not
happen. A save does not trigger a reload — `_disk_text` is what tells our own write from
somebody else's.

**`router.py` keys its own sets on `(col, row)` tuples, not `geometry.hole_key`**, and
memoises the R5' proximity answer per hole. Both are measured (33% off a 100 × 60 board;
the numbers are in the module docstring) and neither may change a route: `hole_key` stays
the one encoding for everything that crosses a module boundary — occupancy, connectivity,
DRC — all of which have golden output. `tests/test_router.py` pins both properties.

### i18n

`ui/i18n.py` is a plain dict whose **keys are the English strings**, wrapped at each call
site with `t()`. `tests/test_i18n.py` scans the UI source and fails if the catalogue names
a string the interface no longer has, if a translation loses its `&` accelerator, or if
two items in one menu claim the same accelerator — so adding a menu item means adding its
Turkish entry and, if it is in one of the grouped menus, extending that test's group list.

It also fails the **other** direction, which is the one that let every tooltip stay
English while the menus were translated: `test_no_tooltip_or_placeholder_is_left_out_of_the_catalogue`
flags a `setToolTip` / `setPlaceholderText` / `setHeaderLabels` argument that is a bare
literal. A string never wrapped in `t()` is not a missing translation — it is not in the
system at all, so it moves no coverage number and nothing else would ever mention it. The
exceptions are derived, not listed: prose has a four-letter word in it, and the strings
deliberately left alone (`"GND, +5V, OUT…"`, `"R1, C3, U2…"`, `"10k, 100nF, NE555…"`) are
the tool's own vocabulary, whose longest alphabetic run is two.

The scanner treats **adjacent string literals as one key** (`ast.literal_eval` over the
lot), because a tooltip lives in the source as three quoted fragments on three lines.
`setText` is deliberately outside all of this: it is what every status field and every
f-string of engine output goes through.

Engine-generated text (DRC/LVS messages, rule ids, hole addresses, net and component
names) is never translated: it is compared byte-for-byte by golden fixtures, and the
addresses are the tool's vocabulary in every language.

The language is chosen `--lang` → `PERFSTUDIO_LANG` → the View menu's stored choice → the
system locale (`main._preferred_language`), and applies at the **next start**: every label
is translated once, as the window is built, so a live re-translation would leave whatever
a rebuild missed in English.

### What the window remembers

`main.app_settings()` is the one `QSettings` store — recent files and the session
(geometry, dock state, board colour, the view toggles, routing style, language). A test
must point it somewhere temporary; `tests/test_ui.py`'s autouse `_settings_in_a_temp_file`
does, and has to, because every test there closes a window and closing saves.

Two rules it is easy to break: `restoreState` matches docks and toolbars **by
`objectName`** and silently drops the ones without one, and the **3D panel is forced shut
after a restore** — reopening it would build VTK's pipeline during startup for a board
nobody has looked at yet. Its size still comes back with the rest of the layout. The
build-guide dock (`Ctrl+4`) is the same shape of thing: it fills itself only while open,
because `build_guide` runs DRC and LVS, and `MainWindow.current_guide()` is the single
cache both it and the 3D assembly slider read — two views of one list that must not
disagree about how many steps there are.
