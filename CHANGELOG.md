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

- **The last two DRC rules, which are the two a top-down view cannot see.** PLAN.md
  §5.2's table had eleven rows and nine of them were implemented; the missing pair
  (rule 8, height and envelope, and rule 9, heat proximity) were both missing for the
  same reason, which is that from directly above — every view a 2D editor can offer — a
  20 mm TO-220 and a 2.3 mm resistor look identical. This is PLAN.md §8.4's first
  functional justification for having a third dimension at all, and it is now a rule
  rather than a picture.
  - **`heat-proximity`**: a TO-220 or a relay sitting within 12 mm of an electrolytic,
    which loses roughly half its rated life for every 10 °C it runs hotter. The
    placement optimiser has priced this since it was written and DRC said nothing, so
    auto-place moved parts apart for a reason the user was never told, and a board
    placed by hand got no warning at all. Which parts run hot, which mind, and how close
    is too close now live in `model.py` and both modules read them.
  - **Measured between bodies, not anchors** — and the placer was changed to match. An
    anchor is pin 1, which on a TO-220 is one end of a 10 mm tab and on a DIP is a
    corner. Rotating a TO-220 180° swings its body to the other side of an anchor that
    has not moved, and the old measure reported the same distance for two placements
    that differ by a centimetre in the only way that matters. There is now one number
    with two consumers, and a test that fails if they drift apart.
  - **`component-too-tall`**, against a `heightLimitMm` the document now carries — the
    clear height inside the case, set from **File → Board Features…**, from the
    `height-limit.set` command, or from MCP. Silent until one is declared, which is the
    honest default: with no case chosen there is nothing to be too tall for. The build
    guide's own note stops guessing when the real number exists — it used to say "10 mm
    or over, check it clears anything meant to go over the board", and now says what
    will not fit.
  - **`jumper-under-body`**, a top-side jumper that has to run beneath a part. The
    router has always refused to lay one and asks `occupancy.body_covers` to decide;
    DRC did not know the rule existed, so **moving a part on top of an existing jumper
    was completely silent** — the copper was legal when it was laid and nothing looked
    at it again. A warning rather than an error, because it is buildable: a wire
    threaded under a DIP socket is ordinary practice. What it is not is buildable in any
    order, which turned out to matter — see below.
  - Only holes strictly *between* a jumper's ends count. For a DIP, an electrolytic or a
    TO-92 the body's bounding box covers its own pin holes, so counting the ends would
    flag every jumper that lands on a part. That makes the rule a strict subset of the
    router's guard, which is the right direction: DRC never objects to copper the router
    was willing to lay.
- **`check_heights`** and **`set_height_limit`** on the MCP server, taking it to 33
  tools. `check_heights` is named in PLAN.md §9.2 and answers what neither render tool
  can: how tall the build stands, tallest part first, whether or not a limit is set —
  because "what decides the enclosure height" is a question worth asking before there is
  an enclosure. Parts whose footprint is unknown are named rather than skipped, so an
  empty `over_limit` cannot be read as "everything was measured".

- **The board can now be described as the ones people actually buy** — three features
  that a bare grid of round pads cannot express, added together because each of them
  changes what the other layers say.
  - **Oblong pads** (`board.padShape` / `padLength` / `padAxis`). Not cosmetic: the R5'
    bridging risk this whole tool is organised around is a function of the gap between
    one pad's edge and the next, and an oblong pad has **two** such gaps. At 2.54 mm
    pitch a 2.25 × 1.9 mm pad leaves 0.29 mm down a column and 0.64 mm along a row — so
    a solder trace one way is easy to make *and easy to make by accident*, and the other
    way is neither. `geometry.copper_gap_mm` measures it per pair, DRC's proximity
    message quotes the direction it found, and the build guide's preparation phase says
    which way the board favours before a single joint is made.
  - **The addresses printed on the board** (`board.labels`), the `A`..`Z` / `01`..`22`
    legend these boards carry. It is the same address space the guide, DRC and the MCP
    tools already speak, so a builder reads "C7" off the copper instead of counting holes
    from a corner — and the guide's phase 0 stops telling them to mark A1, because the
    board already has. Drawn in 2D, in 3D and on the 1:1 PDF that gets taped to the board.
    Printed row numbers may be zero-padded (`rowDigits`), which is typography and not a
    different numbering: `A07` is still rejected as an address.
  - **Mounting holes and edge-connector fingers** (`mountingHoles`, `edgeConnectors`),
    with `mounting-hole.add` / `.addMany` / `.delete` and `edge-connector.add` / `.delete`
    on the same bus as everything else, and a **File → Board Features…** dialog.
    Four corner holes go in as one command, so one Ctrl+Z does not leave three drilled.
- **`board.borderXMm` / `borderYMm`**, substrate beyond the usual half pitch. It exists
  because the legend has to be printed *somewhere*: half a pitch past the outer holes
  leaves 0.32 mm of bare board at 2.54 mm pitch with 1.9 mm pads, which is not room for a
  character, and the boards being modelled are physically wider at the edge for exactly
  that reason. **Two numbers, not one**: a 5 x 7 cm board carries about 2.1 mm at the
  sides and 4.5 mm top and bottom, and a single figure puts the 1:1 printout millimetres
  out on one axis — on the printout that gets taped onto the board.
  `geometry.board_edge_margin_mm` is the one place that says how much substrate is
  outside the grid; `hole_span_mm` is deliberately untouched, so mirroring to the solder
  side still lands hole 0 on hole *cols-1*.
- **The boards you can actually buy**, as presets: `2 x 8` through `20 x 30 cm` in the
  two families they are sold in, picked from **File → Board Setup…**. Perfboard is bought
  as "a 5 by 7" and never as a hole count, so the preset is keyed on the advertised size
  and the grid is what fits inside the printed border — which is why a 4 x 6 is 20 x 14
  and not the 15 x 23 that dividing by the pitch suggests. The border is then *solved*
  from the two, so the outline is the advertised size to the tenth of a millimetre.
  - **A preset is a product, not a grid size.** The green double-sided board arrives with
    its printed legend, oblong finger strips down the two edges that have room for them,
    and a screw hole in each corner sitting in the border; the orange phenolic one
    arrives with none of that, copper on one face and round pads throughout. Applying one
    is a single `board.applyPreset` — board, fingers and corner holes are one decision,
    and four commands would put four entries in the history and leave a board describable
    as a product nobody sells partway down the undo stack.
  - Which two edges carry the fingers is **derived from the border**, not named: the
    answer flips with the aspect ratio, and a named pair puts the strip down the cramped
    side of a portrait board.
- **Single-sided boards** (`board.singleSided`) — the cheap brown/orange phenolic kind.
  Copper on the solder side only: the component side is bare substrate with drilled holes
  and nothing to solder to, which is most of what makes those boards look and behave
  differently from the double-sided FR-4 ones. Both renderers draw the holes on that face
  and no pads, rather than the blank slab that skipping the grid entirely would give.
- **`edgeConnector.insetMm`**, bare substrate between a finger's outer end and the board
  edge. Zero is a true card edge, where reaching the edge is the point; anything else is
  what the prototyping boards do — the elongated pads stop short, and the strip left
  outside them is where the row numbers are printed. Without it the fingers swallow the
  whole border and the legend has nowhere to go, which is exactly what the first attempt
  did.
- **`mountingHole.offsetXMm` / `offsetYMm`**, so a corner hole can sit in the border
  instead of on the grid. That is where every real board puts them: the copper is
  untouched and the screws go outside it. Pinned to a grid position, a mounting hole
  reports four pads destroyed that are perfectly intact. Still addressed by the nearest
  hole, so "the hole outside A1" is something a builder can still find.
- Two DRC rules for mounting holes. **`mounting-hole-conflict`** is an *error*, and the
  only rule in the file that is: every other one describes a board that will probably
  fail, while this one describes a board that cannot work — there is no pad there to
  solder to. A 3.2 mm bore reaches 1.6 mm out and the neighbouring pad's near edge is
  1.59 mm away, so an M3 hole takes the copper off its four orthogonal neighbours as well
  as its own, which is not something anyone notices before the iron is hot.
  **`mounting-hole-clearance`** is a warning: the board is buildable, the screw just
  cannot be fitted without pressing on a part.
- `scenetext.draw_physical_label`, the exact opposite of `draw_label` and needed
  alongside it. An annotation this program adds should hold its size as the board zooms;
  ink printed on the board should not, and has to come out 1.2 mm on the 1:1 export. Both
  exist because asking for a millimetre-sized font directly gets a fraction of a point,
  which some font engines decline to draw at all while reporting no error.

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

- **Nothing is lost by closing the window.** There was no `closeEvent` and no notion of
  a modified document, so the X button silently discarded the work. Save / Discard /
  Cancel on close, open and new, a bullet in the title bar, and **File → New Board**,
  which did not exist at all — the only way to start a board was to quit and relaunch.
- **File → Board Setup** — the first thing anywhere able to reach `board.set`, which was
  unreachable from the GUI and MCP alike, so the grid was frozen at 60×40 FR-4 unless
  you hand-edited JSON. The material matters: it sets the iron temperature and dwell the
  build guide gives, and DRC's pad-lifting rule only fires on FR-2/FR-1 — the cheap
  phenolic most perfboard is actually sold as.
- **A Draw menu**, reaching `conductor.add`, which had existed since the first commit
  with nothing able to call it: on a perfboard tool there was no way to run a wire or lay
  a solder trace by hand. A wire is two clicks; a trace is a chain ending in Enter. The
  preview refuses a diagonal step on a trace visibly rather than ignoring the click.
- **Conductors are selectable and deletable.** Before this a single bad route could only
  be removed by undoing the whole autoroute or re-routing the entire board.
- The hole under the cursor is in the status bar. Every DRC message, guide step and MCP
  argument says "C7", and there was no way to tell which hole the pointer was on.
- **A Turkish interface** (`ui/i18n.py`): `perfstudio --lang tr`, `PERFSTUDIO_LANG=tr`,
  or the system locale. A dict rather than Qt Linguist, so there is no build step and no
  binary catalogue, and every key is the English string — a translation cannot attach to
  the wrong message and English is never "missing". `tests/test_i18n.py` fails if the
  catalogue names a string the interface no longer has (the way translation files
  normally rot), if a translation drops its `&` accelerator, or if two items in one menu
  claim the same one — which is how it caught **Aç** and **Ayarları** both claiming `A`
  in the File menu. Hole addresses, rule ids and every engine message stay untranslated
  on purpose: the addresses are the tool's vocabulary, and the engine's strings are
  compared byte for byte against the reference implementation.
- **The exploded view** (PLAN.md D7, milestone M4), **View → Exploded View** in the 3D
  panel. Every part rises off the board with a **leader line down to each hole it drops
  into**, which is not decoration: a plain vertical lift is ambiguous, and measurably so.
  From the standard three-quarter viewpoint a part over the middle of the board projects
  onto the board and reads as sitting on it, while an identical part near an edge reads
  as floating — the same lift meaning two different things depending only on where the
  part happens to be. The lines settle it at any lift, and they answer the question the
  view exists to ask, which is not "what is on this board" but "which holes does *this*
  one go in". The camera is left alone, as everywhere else in this view.
- **The board part-way through being built** (`guide.document_at_step`,
  `guide.step_focus`). One function that knows what "partly built" means, because the two
  things that need it must agree: an assembly animation is these documents played in
  sequence, and a step image is one of them with the step's own part picked out. Worked
  out separately they would drift, and a board would end up drawn with a part the step
  beside it has not asked for yet.
  - Build order, not document order and not the order the router produced: lowest part
    first, jumpers before whatever stands on them.
  - The board, its mechanical features and the schematic intent never change — they are
    what you started with. Only the parts and the copper arrive over time, one per step,
    and the last step is the document itself rather than a reconstruction of it.
  - The index is clamped at both ends, so a caller rendering a "before" frame and a
    "done" frame needs no special cases. A part the guide could not write a step for
    never appears at any index: a picture must not show a part in a hole the guide
    declined to name.
- **`view3d.render_offscreen` takes `exploded_mm` and `highlight`**, so one call is one
  step card's illustration. Highlighting dims the other parts and the copper and never
  the board — a step card says which holes a part goes in, and a reader who cannot see
  the holes has been handed the answer with the question rubbed out.
- **CI** (PLAN.md §14), on every push and pull request. This project's central claim is
  that the Python engine reproduces the retired TypeScript one byte for byte, and until
  now nothing checked that except somebody remembering to. Three jobs:
  - **tests** on Linux, Windows and macOS, on the Python floor `pyproject` promises and
    on the next version up. `fail-fast` is off so every platform reports: a rendering
    fault is usually specific to one of them, which is the whole reason the matrix
    exists. Linux installs the X, GL and dbus libraries Qt and VTK link against, and runs
    under `xvfb` — Qt's offscreen platform is not the same thing as a GL context, and the
    build guide's step images need a real one.
  - **Linux on every push; the whole matrix on `main`, on tags, and on request.** The
    repository is private, so runner minutes are metered, and not evenly: Windows bills
    at 2× and macOS at 10×. The full matrix everywhere costs upwards of sixty billed
    minutes per push, which for a thirty-second test suite is an allowance spent
    re-proving that Windows survived a docstring edit. A platform-specific fault still
    cannot reach a release without being seen, which is the property worth paying for.
  - **`mypy --strict src`**, and `src` deliberately: the engine is strict-clean and must
    stay that way, while the tests are not and never have been. Gating on something
    already broken teaches everyone to ignore the red tick.
  - The **headless run** on a golden fixture, which is the only thing that exercises 2D,
    3D and the PDF export against a real board rather than against assertions about them.
    What it drew is uploaded as an artifact.
  - **`ruff` reports and does not block, and the reason is written into the workflow.**
    `ruff check src tests` finds a few hundred things — overwhelmingly `E501` on message
    strings and `RUF001` on the Turkish catalogue's dotless *i* — and `ruff format` would
    rewrite 40 of the 57 files. Both are worth settling. Neither is worth settling by
    surprise inside a CI change, because the answer decides whether every line of blame
    in this repository points at a reformat.
- **Assembly playback in the 3D panel** (PLAN.md D7, closing milestone M4): a slider and
  a Play button under the view. Drag back and the parts and copper come off in reverse
  build order; press Play and the board assembles itself a step at a time, with the step
  being done picked out and named in the caption beside it.
  - **There is no "animation mode".** The slider's maximum is the finished board, which
    is where it sits, so an untouched panel behaves exactly as it did before. A mode
    would mean a way to be stuck in one, and a second thing to remember to turn off
    before the view means what it looks like it means.
  - The slider counts **things fitted**, not steps done, so its two ends are the two
    states anybody actually asks for: a bare board at 0 and a finished one at the top.
    `assembly_step_for` is a plain function for that arithmetic, because the first
    version returned -1 at both ends and drew a complete board at the position that means
    nothing has been fitted yet.
  - It returns to the end on every edit. A position part-way through a build that no
    longer exists is not a position: adding a part renumbers everything after it, so
    holding the index would quietly show a different moment than the one being looked at.
  - The camera is left alone throughout, as everywhere else in this view.
- **The build guide has pictures** (PLAN.md §7.2), one per step: the board as it stands
  at that point with the thing that step asks for picked out of it. Written by
  **File → Export Build Guide**, by headless mode, and by the MCP `generate_guide`.
  22 renders in about half a second on the NE555 fixture, because one render window is
  re-actored per step rather than stood up again for each.
  - **Photographed from the side the work is done on.** Almost every connection is made
    on the solder side, and shot from the component side it is behind 1.6 mm of board:
    the first version produced fourteen pictures of a board with nothing happening in
    them. There are two cameras now, and a step is shot from the face the builder is
    actually looking at when they do it.
  - **Tinted, not merely brightened.** The subject was first given its own colour with
    the light turned up, which is invisible when the subject is a black DIP and
    everything around it has been dimmed to near-black. A step image cannot depend on the
    part happening to be a light colour.
  - Within a face the camera frames the finished board once and is then left alone, so
    flipping through the guide reads as one board being built rather than a series of
    unrelated photographs.
  - `guide_export.guide_to_html` takes **raw PNG bytes** and base64s them into the
    document itself. Not paths and not URLs: a caller cannot hand it a link, so the
    finished guide cannot acquire a dependency on a server or on the folder it was
    written into. It still opens from a USB stick in five years, which is the whole
    reason that file has no CDN, no fonts and no network. Headless prints the resulting
    size, because that is the property that would quietly stop being true.
  - A guide with no pictures is still a complete guide. On an install where VTK will not
    load, MCP reports `step_images: 0` and writes every word of it anyway.

### Changed

- **The document format version stays at 1**, and that is a deliberate call rather than
  an oversight. Every field above is omitted from the JSON when it holds its default, so
  a board using none of them serializes to the bytes a build predating them wrote — all
  15 golden fixtures still round-trip byte for byte, and an older build opens such a file
  unchanged. The rule in this project is that the format version moves when an older file
  needs *migrating in order to load*, and none does. The cost is that a build predating
  these features will silently drop them from a file that does use them; the migration
  seam in `persist.py` is where that would be addressed if it ever bites. `heightLimitMm`
  follows the same rule, and a hand-edited zero or negative loads with a warning and is
  dropped rather than reporting every part on the board as too tall.
- **`jumper-under-body` joins `conductor-crossing` in `PYTHON_ONLY_RULES`**, and for a
  stronger reason: the TypeScript engine has no counterpart to disagree with, so no
  fixture records anything for it and the expected files cannot be regenerated to include
  it. It fires 15 times across 6 of the 15 fixtures — `dense` earns 6 on its own, because
  `cond-12` is a 29-hole top jumper straight across row 18 of a board that already has
  six overlapping bodies, and it runs over a header, a TO-92 and an LED on the way. A
  test pins those counts, so the divergence stays an improvement rather than becoming a
  hole in the proof. `heat-proximity` and `component-too-tall` fire nowhere in the
  fixtures — none carries a TO-220, a relay or a height limit — so the golden DRC data is
  untouched by them.
- **The editor's ruler stands down when the board prints its own addresses.** Drawing
  both put the same twenty-four letters on screen twice, a few millimetres apart and in
  two different styles, which reads as a rendering fault rather than as two features. The
  ruler is for boards that carry no addresses; **View → Show Hole Addresses** is greyed
  out with a reason on boards that do, and comes back when the board is flipped to a face
  the legend cannot be read from.
- **The printed legend goes on all four edges**, letters top and bottom and numbers down
  both sides, with the numbers turned on their side as the real boards set them — the
  strip beside a row is narrow across and a whole pitch deep, so a turned number fits
  where an upright one has to shrink.
- **An edge-connector finger is now the pad, not a layer over it.** The grid no longer
  draws a round pad underneath one, which is what made the fingers look like something
  laid on top of the board. `geometry.holes_without_grid_pad` is the single answer to
  "this hole has no ordinary pad", for either reason (a bore took the copper, or a finger
  is the copper) and for either face.
- **DRC's proximity rule measures the gap per pair instead of once per board.** The
  number was `pitch - padDiameter`, which is right for round pads and wrong for every
  other case — it cannot see that an oblong pad's neighbour is half as far away one way
  as the other, or that a pad widened into a connector finger has less clearance than the
  pad it replaced. Round-pad boards report exactly what they did before.
- **The 2D view is seven times faster on a large board**: a 100×60 grid (6000 holes) went
  from 112 ms a frame (8.9 fps) to 16 ms (62 fps), by rasterising one pad and blitting
  it. Two approaches that did not work are recorded in `PadGridItem` — one even-odd path
  for every ring took 5.8 seconds, and disabling antialiasing bought half as much while
  making the board look cheap.
- **Auto-place and autoroute run off the UI thread**, with a progress dialog that appears
  only if the work outlasts a grace period, and a Cancel that asks the placer to stop and
  return its best result so far rather than discarding it.
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

- **The pertinax board was still wrong in three more ways**, all corrected against the
  board in a user's hand rather than against a guess.
  - **Its finger strips were missing entirely.** The preset gave them only to the green
    board. Both families have them.
  - **They go across the SHORT edges.** The edges used to be derived from which *border*
    was wider, which gives the right answer on the green boards and the wrong one on the
    phenolic: its two borders are 2.14 mm and 1.98 mm, so a tenth of a millimetre decided
    which way a strip of contacts ran. `preset_strip_edges` asks the board's proportions
    now — a strip belongs across the narrow end, not down the length.
  - **A finger has no hole through it** (`geometry.undrilled_holes`). It is a solid
    contact soldered to from the surface, which is the whole difference between a finger
    and a pad. Both renderers were drilling them: the grid drills every position it has,
    and 2D then punched the bore back through the finger on top, on the stated reasoning
    that "the finger is copper laid over the pad". It is not laid over the pad; it *is*
    the pad.
  - And they are **solder-side only** on a single-sided board. `face: "both"` had put a
    strip of contacts on the bare phenolic face, where the board has nothing but
    substrate.
- **A corner screw hole was drilled through the end of the finger strip.** The strips ran
  the full width of the board and the corner holes went in independently, so nothing
  reconciled them: measured at 0.21 mm of overlap on the 2 × 8 and 6 × 8 presets, on a
  board the program produced before anybody had touched it. A bore removes copper, so
  that is a destroyed contact. The strip is trimmed to clear them now, and
  `preset_edge_connectors` asks `preset_mounting_holes` itself rather than leaving two
  features to be combined by a caller with no way to know they interfere.
  - **Clearance, not merely not-overlapping.** Trimming to first contact left 0.01 mm of
    board between copper and drill on the 5 × 7 and 0.09 mm on the 4 × 6, which is not a
    gap — a hole drilled that close breaks out into the pad. `FINGER_BORE_CLEARANCE_MM`
    is 0.3 mm and every preset now clears by at least 2.3 mm.
  - The 5 × 7 was clear by luck of the arithmetic before any of this, which is why it
    looked fine: it is the board that gets rendered when something is being checked.
- **A phenolic board wore FR-4's gold pads.** `BoardScheme` described the substrate and
  the silkscreen but not the copper, so one global gold served every board. The finish is
  one of the two things you notice first: a plated FR-4 board is yellow, a cheap phenolic
  board's pads are **bare copper**. The copper is part of the scheme now.
  - **The values are measured, not chosen.** Sampled off photographs of the boards: the
    substrate lands at `#c67a3f`, `#c17c58` and `#bb7441` across three of them, and the
    pads at hue 25-28° with saturation around 0.35 — warm, so copper rather than tin, and
    much paler than a first guess at "pink-brown" had them.
  - That same measurement caught a wrong turn in this changelog. The substrate was
    briefly changed to a mid-brown, on the reasoning that pertinax is the colour of
    cardboard. It is not; it is orange, near enough to what was there before, and the
    boards say so. It is the one colour in that file anybody can check.
- **The orange pertinax board had no printed addresses, and it should have.** The preset
  gave a legend to the green double-sided board and withheld it from the phenolic one, on
  the reasoning that the phenolic board is the stripped-down product — no fingers, no
  corner holes, copper on one face. That was wrong about the one thing it is not stripped
  of: these boards carry the same `A`..`Z` / `01`..`NN` print, and it is the cheapest
  marking on a board to apply.
  - It is not a cosmetic miss, which is why it was worth chasing. With no legend the 2D
    editor falls back to **its own ruler** — drawn outside the board, sized in screen
    pixels — so the addresses existed on the screen and not on the board in your hand,
    and they were absent from the 3D view and the 1:1 printout altogether. The board on
    screen stopped being the board you are holding, which is the one thing this view has
    to get right.
  - There was room all along: the 5 × 7 phenolic preset leaves 2.46 mm and 2.30 mm of
    bare substrate outside the outer pads, so the legend prints at the full 1.15 mm cap
    height a real board uses, with nothing clipped.
  - `test_every_preset_prints_its_own_addresses` now holds it for **every** preset in
    both families, rather than two tests pinning the old answer for one family each.
- **The application opened on a board nobody sells.** With no file to open, and behind
  **File → New Board**, it used `DEFAULT_BOARD` — a bare 60 × 40 grid with a zero border.
  A zero border leaves nowhere to print an address, so the very first board a user saw
  had no legend and fell back to the editor's ruler, and the 3D view and the 1:1 printout
  showed no addresses at all because neither has a ruler to fall back on. It now opens on
  the 5 × 7 cm double-sided board via `commands.create_starter_document`, as a **product**
  — printed legend, a finger strip down each of the two edges with room for one, and a
  screw hole in each corner. `DEFAULT_BOARD` stays exactly as it was: a bare grid is the
  right *engine* default and the wrong thing to open an application on.
  - Built directly rather than dispatched as `board.applyPreset`, so a new document does
    not open with an undo step already on its stack.
  - **File → New Board** now applies a chosen preset's fingers and corner holes too. It
    read `dialog.board()` and ignored `dialog.preset_features()`, so picking a preset
    there produced the grid and none of the product — the half-applied state
    `board.applyPreset` exists to make unreachable.
- **Column letters were printed underneath the connector fingers.** The legend's position
  was measured out from the grid pad, which is correct until the copper on that edge is
  an elongated finger reaching most of the way to the board edge. Ink is drawn under
  copper, so the letters did not come out faint — they did not come out at all, and the
  board showed row numbers and no letters. Both renderers now measure **in from the board
  edge**, which is the same answer on a plain board and the right one on a board with
  fingers; `legend_strip_mm` already reported the correct width there and this is the
  position to match it. The existing border test could not catch it: its band runs from
  the grid pad to the board edge, and the middle of a finger is inside that band.
- **The 3D legend read backwards on the underside.** Both faces were built from one set
  of glyphs at two different depths, so turning the board over showed every address
  mirrored — on the view whose whole job is checking the solder side. The bottom face is
  now reflected about the hole span, the same axis `view2d.hole_to_screen` mirrors about,
  so `A` still sits on the hole it names. 2D deliberately does the opposite for the face
  it is seeing *through* the board, and the two are not in conflict: 3D is looking at ink
  directly, 2D is looking at it through 1.6 mm of phenolic.

- **The build guide scheduled top jumpers after the parts they run under**, which is an
  order nobody can follow. Jumpers sit in phase 7 because they are usually soldered to
  pins already fitted, and that is still right for almost all of them — but by phase 7 a
  part standing over a jumper is on the board and the wire has nowhere to go. The ones
  `jumper-under-body` flags now move to phase 1, where PLAN.md §7.1 puts top-side
  jumpers in the first place, and the part standing over one gets a note saying to check
  it is down and lying flat first. Both read the same rule result, so the order and the
  note cannot disagree.

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
