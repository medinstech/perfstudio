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

### Changed

- **The board is the board people actually buy, checked against a photograph of one.**
  Somebody held a 5 × 7 cm double-sided board up against the screen and asked whether it
  matched. The geometry did — 70 × 50 mm, the grid, the four corner screw holes in the
  border, the finger strips down both short edges — and two things did not:
  - **The pads are TINNED, not gold.** `_GOLD` claimed to be "what the green, blue and
    black prototyping boards are finished with", and on the photograph they are plainly
    not: the pads there are neutral and light, at a blue/red ratio of about 1.0 where
    gold's is 0.4. It is HASL — hot-air levelled solder over copper. `_TINNED` replaces it
    for every masked scheme, kept warmer and lighter than the grey a solder run is drawn
    in, because those two are nearly the same metal on a real board and telling them apart
    is what this application is for. The 2D render signatures were re-blessed after
    reading the diff: ink coverage identical to three decimals, every cell moved about ten
    levels — a colour change and not a geometry one, which is exactly what it should be.
  - The legend was taken off the green board in the same pass and **put straight back**:
    the 400-pixel product shot resolved no printing, a photograph of the same product at a
    usable size shows the column letters plainly along the border under the bottom row,
    beside the part number and "5X7CM". A thumbnail too small to resolve a print is
    evidence that the print cannot be SEEN, not that the board has none — which is now
    written at `board_from_preset` and in the test, because the same mistake is available
    to anyone reading the same photograph.
- **The corner screw hole is AT the corner position, centred, and the pad it replaces is
  gone.** It used to be pushed as far diagonally into the printed border as it would fit,
  to spare that pad — and one offset was used for both axes, so on a 5 x 7, whose two
  borders are 3.41 and 5.79 mm, the bore ended up 0.20 mm from one edge of the board and
  floating 2.6 mm from the other, with the corner pad jammed against it. Nothing was
  centred in anything, and it is the corner somebody looking at the board looks at first.
  - The board is manufactured with a screw hole at that position INSTEAD of a pad, so the
    pad is consumed — through `consumed_holes`, which DRC, both renderers and the guide
    already read, with no special case for corners.
  - **A board whose border is too thin to hold a screw steps the hole one position inward**
    rather than drilling a notch out of its own edge: `CORNER_HOLE_WEB_MM` is 1 mm of board
    and the corner position leaves 0.31 mm of it on the 20 x 30 and 0.88 mm on the 7 x 9.
    Both are now measured by a test rather than left to the eye. It also means every
    double-sided preset gets its four screw holes; the six largest boards used to get none
    at all, because the old arithmetic gave up and returned zero.
  - The finger strip already trimmed itself clear of the screws, but by taking the run
    from the first clear finger to the last — which assumed the obstruction is always at an
    END. An inset screw obstructs one in the middle, and first-to-last quietly put it back;
    it takes the longest contiguous clear run now.
- **A mounting bore takes the copper it is drilled through, including a finger.** A hole
  put one position in from the corner — which is what Board Features offers by default —
  reaches the edge-connector strip, and the finger it went through was still drawn intact
  in both views: a 3.2 mm hole sitting on top of a contact the board no longer has. A bore
  does not distinguish between the two shapes of copper it destroys, so
  `geometry.surviving_finger_holes` answers that for both renderers the way
  `consumed_holes` already answered it for a round pad.
- **The printed legend is not printed into a hole.** Ink goes ON the substrate and a bore
  takes the substrate away, so a letter under one is not faint, it is absent — which is
  what a real board shows. `geometry.printed_label_is_clear` is asked by the 2D legend and
  the 3D one, in the same units, because a legend that agreed with the board in one view
  and not the other is the disagreement this pair keeps producing.
- **The board in 3D has holes in it.** Which sounds like a statement of the obvious and
  was not true: the substrate was one solid cube and every hole on it was faked by laying
  a dark cylinder over the top. A dark disc on green reads as a mark printed on the board,
  not as something you can push a lead through — and it was worst on a mounting bore,
  which has no copper ring around it to explain the darkness, so a screw hole read as a
  sticker. The plate is punched now: both faces, every hole, and the wall of the bore
  visible inside it.
  - **Affordably, which is why the fake existed.** A boolean subtraction per hole is
    nearly two thousand of them on a 945-hole board, and that argument still holds. This
    is neither: a face is ONE TILE — a pitch square with its hole taken out — glyphed at
    every hole, so both faces of any board cost one source and two actors. The tiles are
    exactly a pitch across, so neighbours share an edge and the surface is watertight;
    what is left between the outermost tiles and the board's edge is the printed border,
    drawn as rectangles, and a bore in it is cut out of them.
  - **A mounting bore takes whole tiles and lays one patch over them**, and the tiles it
    takes are exactly the holes `geometry.consumed_holes` reports the copper gone from —
    one bore, one answer, in the renderer and in DRC. It was also being drawn at its hole
    ADDRESS rather than at `mounting_hole_centre_mm`, so every corner hole appeared back
    on the grid in the middle of four pads that are perfectly intact. CLAUDE.md has said
    not to do that since the feature was written.
  - An edge-connector finger keeps a solid tile: it is a contact with no bore, and
    drilling through it is what made a finger look like a long pad.
- **The parts look like the parts.** Every model was looked at on its own, close up, which
  is how most of these were found: at the whole-board zoom a wrong shape reads as a small
  coloured blob and nothing more.
  - A **resistor** was a flat-ended tube; it has the moulded shoulder at each end now,
    with the barrel shortened by what the domes add back so the part still measures the
    length its footprint says it does.
  - A **film capacitor** was a plain orange brick. The dipped case is a slab with
    half-round ends, so it is one now.
  - A **disc ceramic** was a coin with a sharp rim; the dipped case is a lens, thicker in
    the middle, and that is the same one solid.
  - A **TO-92** was a squashed cylinder — which is an ellipse, so it has two flats and
    marks nothing. It is the D-shaped case now, with the flat facing its row of pins, and
    getting a transistor round the wrong way is the classic way to spend an evening.
  - A **TO-220**'s tab was a slab the width of the package sitting on top of it. The tab
    is sheet metal the plastic is moulded around: thin, flush with the back face, and with
    the bolt hole through it — the side a heatsink goes on.
  - An **HC-49 crystal** was a flat-topped tube. It has the pressed cap and the welded lip
    at its base, which is also what says which way up it goes.
  - A **DIP**'s pins are flat blades rather than round wire, it has the semicircular notch
    at the pin-1 end as well as the dot, and the dot is a dimple rather than a headlamp:
    at a tenth of the package in the accent colour it read as a component of its own.
  - An **electrolytic** gained the crimped rim and the vent scored into its top. It had
    been given a WHITE top — and two of them on a board then read as screw heads, which is
    exactly what the first person to see it called them.
- **The 2D and the 3D board draw the same board.** Three things had them disagreeing, and
  all three were the 2D view or the renderer being wrong rather than a difference of
  opinion:
  - **A corner mounting hole was drawn on the grid in 2D.** `MountingHoleItem` read the
    hole ADDRESS and ignored the offset — the very thing CLAUDE.md says to go through
    `mounting_hole_centre_mm` for — so a bore that sits in the printed border was drawn a
    whole pad's width away from where 3D and DRC both put it. `view2d.mm_to_screen` is the
    mapping it needed: the same reflection `hole_to_screen` applies, for the things that
    are not on the grid.
  - **A bore in the border blinded the hole beside it.** The patch of plate laid over a
    mounting bore takes whole tiles, and it was grown OUT to tile edges — so a corner bore
    that touches no tile at all swallowed its neighbour, leaving a pad drawn over solid
    board with no hole through it. The patch now takes the tiles the bore actually reaches
    and, for whatever of it lies outside the tiled grid, a rectangle clipped so it cannot
    reach a tile the bore never touched.
  - **The copper was clipping.** VTK's default specular POWER is 1.0, which is not a
    highlight — it adds the specular term flat across the whole surface. A pad carrying
    0.4 of it rendered a fifth brighter than its own colour and blew out: measured at
    (255, 255, 125) against the `#c8a951` the 2D view paints from the same table, which
    also flattened the shading off the metal and made the board a grid of flat yellow
    rings. `COPPER_SPECULAR_POWER` is on the pads, the strips and the fingers now, and
    `test_the_board_s_copper_is_never_blown_out` renders a board and counts the clipped
    pixels.
- **A part is now the height its footprint says it is, and that is measured.** VTK applies
  an actor's scale BEFORE its orientation, and the turn that stands a cylinder up maps the
  source's own z onto world y — so a scale written to flatten the width of an upright can
  flattened its LENGTH instead. The crystal came out a quarter short with its cap floating
  in the air above it, and the render looked like a part, just not that part.
  `test_every_part_stands_exactly_as_tall_as_its_footprint_says` measures every archetype
  against the number the height-limit rule works from.
- **No mesh library, and that is still the decision.** The only comprehensive set of
  through-hole models is KiCad's, under CC-BY-SA-4.0: a share-alike licence in a
  repository that is Apache-2.0, for assets that would then have to be shipped, versioned
  and kept in step with the footprints. PLAN.md D6 declined it and the reason has not
  changed — and there is a second one now, which is that `bodies.py` derives the 2D
  footprint and the 3D solid from the same `BodySpec`, so a mesh would be a third
  description of a part free to disagree with both.

### Fixed

- **Every lead now goes down its hole and out the other side.** A part in the 3D view
  hovered over the holes it is meant to be soldered into: an axial lead ran horizontally
  to the pin position and stopped there in mid-air, a radial capacitor had no legs at all,
  and a DIP had no pins — it was a black block resting on the board. The lead turns down
  at the pin, disappears into the hole (the bore is opaque, as a board is) and reappears
  trimmed on the solder side, which is the only evidence that face has that anything came
  through it. Every archetype, because the omission was per-builder, and a DIP's pins run
  down the OUTSIDE of its two long sides the way the real package's do.
  - One instanced actor per component, not one per pin: a 2×20 header has forty, and the
    pad grid is instanced for exactly this reason.
  - `_upright_cylinder` hands the glyph its polydata rather than a pipeline connection.
    A connection holds a RAW pointer back to the source that produced it, and the source
    there is a local — connecting one segfaults the interpreter outright, which is how
    this was found rather than reasoned about.
- **The holes read as holes rather than as buttons.** The dark bore stood 0.10 mm PROUD of
  the copper at both faces, so at any grazing angle — which is most of them, since this
  view is orbited — every hole showed a black cap above its own pad, occluding the pads on
  the rows behind it. A board of 700 holes read as a grid of black buttons. The bore now
  stops just short of the copper on both faces (`BORE_UNDER_PAD_MM`), so the ring is the
  topmost thing at every hole. Mounting bores use the same span, because a screw hole
  drawn to a different depth from the grid around it reads as a different kind of thing.
  - `pad_z` and the new `bore_span_z` are pure and asserted directly, as the pad plane
    already was: what a photograph shows and no test could see is exactly what needs one.

- **`pnpm build` works from a clean clone.** The root `tsconfig.json` that `tsc -b` looks
  for was never committed, so the command failed with `TS6053: File 'tsconfig.json' not
  found` — and `pnpm build && node tools/diffcheck/generate.mjs` is the documented way to
  regenerate the golden fixtures this Python engine is proved byte-for-byte against. The
  solution file compiles nothing itself; it names the four packages, in whatever order
  their own `references` imply. `tools/bench-3d` is deliberately not among them: it is
  `composite: false` and `noEmit`, which a solution build cannot reference. Verified by
  regenerating every fixture and finding the tree unchanged.

## [0.10.0] - 2026-09-02

A polish release: nothing new to learn, and a long list of things that were almost right.
Four audits — the window, the three views, the engine's own prose and the loader's
tolerance for bad files — each came back with what they found, and what follows is what
survived being checked.

### Added

- **The three panels that had no way back.** Parts, Nets and DRC / LVS can be closed from
  their title bars, and the only route back was a right-click on the menu bar, which nobody
  finds. View ▸ Show Parts (`Ctrl+1`), Show Nets (`Ctrl+2`) and Show DRC / LVS (`Ctrl+6`)
  join the three toggles the other panels already had.
- **Right-clicking a conductor.** The menu over a trace was the bare-board menu — Paste,
  Connect, Fit — with no way to delete the trace under the pointer. It now selects the
  conductor and offers Copy, Duplicate and Delete, the way a right-click on a part already
  selects the part.
- **`Ctrl+0`, `Ctrl++` and `Ctrl+-` follow the keyboard focus.** Over the schematic panel
  they fit and zoom the sheet, which had a Fit button and no key; over the board they do
  what they always did. The sheet's zoom also gained the board's limits — a few trackpad
  flicks used to leave a blank field or one pin, with nothing but the button to recover.
- **Undo and Redo say what they will do.** The Edit menu read "Undo" and "Redo" and the
  answer was one hover away; it now reads "Undo Move R4 to C7", and Redo — which never
  named anything, because the bus had no way to ask — reads the same. `CommandBus` gained
  `redo_history()` for it.
- **Load warnings are shown, not counted.** Opening a file that was read differently from
  how it was written — a diagonal solder-trace step, an unknown property, a duplicated
  reference — put "(2 warning(s))" in the status bar and nothing else anywhere. They are
  listed in a dialog now. And a duplicated reference IS a warning now: every net node
  names a part by reference, so two R1s is a netlist that cannot say which one it wired.
- **Two DRC rules for the file only a hand can write.** `conductor-off-board` (a conductor
  with a hole outside the grid, the twin of `component-off-board`) and `unknown-footprint`
  (a part whose footprint neither the library nor the id grammar can produce). No command
  can create either state, so both only ever fire on a document edited by hand or written
  by another tool — which is exactly the file that must still open and be told what is
  wrong with it. Both are Python-only (`PYTHON_ONLY_RULES`); the second turned out to fire
  on **eight of the fifteen golden fixtures**, all on one id, `c-disc-1`, which exists in
  neither engine. The fixtures are dumps of the original and are left as they are; the
  rule is excluded from the differential comparison and pinned by its own test.
- **The interface is Turkish where it was still English.** The status bar's DRC, LVS and
  ratsnest fields, the Nets panel's "done", the DRC panel's summaries, the schematic
  panel's summary, the three file dialogs, the progress dialog's five labels, the
  confirmations' bodies, the "select a part first" line and its five verbs, the Board Setup
  material list, and the board's own prose while measuring, picking pins and joining them.
  `tests/test_i18n.py` now also checks the View menu for accelerator clashes, which found
  one in each language.

### Changed

- **A group moved together is one undo step.** Five parts dragged or nudged at once used
  to be five `component.move` commands, so five `Ctrl+Z` presses to put them back — and a
  refusal part-way through left the group torn apart. The scene dispatches one
  `component.moveMany`, which is all-or-nothing: a locked part in the selection refuses the
  whole move and says which part.
- **Every destructive question puts Cancel under Enter.** Delete parts, delete conductors,
  delete a net and reload from disk all used `QMessageBox.question` with no buttons named,
  which puts Yes under Enter — so a confirmation answered by reflex was a deletion. They
  share one box now, `MainWindow._confirm`, whose button carries the verb ("Delete") and
  whose default is Cancel. Tests that used to stub `QMessageBox.question` stub `_confirm`.
- **Deleting a mixed selection deletes all of it.** A rubber band that took a part and the
  two traces on it used to delete the part and keep the traces without a word; the
  question now says the conductors go too, and they do.
- **The loader refuses what `board.set` refuses.** A `.perf` with `"pitch": 0`, no columns,
  or a drill wider than its pad used to load without a word, and then every repaint divided
  by it — in the pure render path, a segfault. Two components sharing an id (a part no edit
  could ever reach, and one the placer refused the whole board over) and a `formatVersion`
  below 1 (no such version has existed) are refused too, with the same codes and paths the
  other load errors carry. `Missing required field "cols"` now says `"board.cols"`, the
  path every neighbouring message already quoted.
- **`CommandBus.dispatch` never raises, as its contract says.** A payload of the wrong
  shape — a dict where a dataclass was expected, a tuple where a `HoleCoord` was — used to
  surface as an `AttributeError` out of the command; it is `{ok: false, code:
  "invalid-payload"}` now, which is what an agent on the far side of the MCP server needs.
  The undo stack is bounded at a thousand entries, where it had no bound at all.
- **Save is enabled only when there is something to save.** `Ctrl+S` on an unmodified
  board rewrote the file — a new modified stamp, a new mtime — for nothing. An untitled
  board can always be saved somewhere, so Save stays live there.
- **The Downloaded strip has a Close button.** Its only button was Hide, which skips that
  version for good — so closing the strip after a successful download told the application
  never to mention the release the user had just fetched. Hide stays on the announcement,
  where it means what it says.
- **`board.set` names every stranded part, and refuses to strand a track cut.** Shrinking a
  board with three parts outside the new size used to name the first, so the refusal
  repeated once per part, each discovered only after the previous one had been moved. A
  cut off the board was kept silently — and if the board was grown again, it broke a strip
  nobody remembered cutting. A component also needs a non-empty reference now; the wiring
  cannot name one without.
- **MCP results mean one thing by `ok`.** `run_lvs` returned `ok: false` when the board
  did not match the schematic, while everywhere else `ok: false` means the call was
  refused; it returns `ok: true` with `matches_schematic` now, and `run_drc` carries
  `ok: true` too. `autoroute` on stripboard returned `cuts: 0` for an empty plan and
  `cuts: [...]` for a committed one; it is always a list, with `cut_count` and
  `link_count` beside it. `export_pdf` with no `directory` wrote two PDFs into the
  server's working directory, against the instructions' own promise that nothing writes to
  disk unless a path is named — it refuses with `no-directory`. Paths accept `~`, and an
  empty path is refused rather than blamed on permissions. The server's instructions now
  list all six connection kinds (not four), give the design-first working order
  `docs/MCP.md` gives, say what `plain` is as a board type, and send an agent to `reroute`
  after `optimize_placement` rather than to `autoroute`, which only adds.
- **The KiCad importer says what it was handed.** A `.kicad_sch` given to it — the
  schematic instead of the netlist exported from it, which is the first-time mistake — got
  "no export form found"; it now says it is a schematic and where the netlist comes from.
  Two nets sharing a `code` (a broken export, but the user's export) used to refuse the
  whole import as a duplicate id; they are imported with a suffix and a warning. A
  component with no footprint is warned about, since it is what forces the host to guess
  one.

### Fixed

- **Measuring, then flipping the board, broke every tool.** The measurement marker is an
  item like the others and a rebuild destroys it, but the mode kept a wrapper for it — so
  the next click, or the next arming of any tool (all of which disarm measuring first),
  raised from `removeItem` and left every board mode unreachable until the window was
  reopened. `test_measuring_survives_a_flip` is the reproduction.
- **Three more things a rebuild lost.** A half-drawn trace kept its path and lost its
  picture, so after a flip or an undo the tool was armed with nothing on the board and
  Enter committed an invisible chain. The placement ghost came back at A1 after every
  placement — at exactly the moment the next part was being lined up. And a conductor
  selected to be deleted was deselected by whatever unrelated command came first, while
  parts had always been re-selected by id.
- **Picking a part mid-trace armed both.** Clicking the Parts panel while a trace was being
  drawn left the ghost following the pointer while every click still extended the trace,
  so the part could never be placed. The board modes are mutually exclusive and placement
  now disarms drawing, as drawing already disarmed placement.
- **The solder side and the keyboard disagreed.** The arrow keys moved a part one column in
  the document, which on the mirrored solder side is a step to the LEFT under `Right`;
  Rotate Clockwise turned the other way there for the same reason; and the placement ghost
  painted a DIP's pins running one way while the placement put them the other. A drag was
  right all along, because it round-trips through `screen_to_hole`. All three now correct
  for the side, and `test_arrow_keys_follow_the_screen_on_the_solder_side` holds it.
- **Flipping the board flips the view and the camera with it.** The scene is mirrored
  about the hole span's midpoint, so a flip while zoomed in on the left edge showed the
  right edge; the viewport centre is mirrored too now. And the 3D panel, whose own
  docstring said the camera turns over "when the board is flipped", stayed on the component
  side — it turns over.
- **The last build step was unreachable on the assembly slider.** With N steps the slider's
  maximum was N, and the maximum means "the finished board", so step N had no position of
  its own: picking it in the Build Guide panel showed the finished board with nothing picked
  out. The range is N+1.
- **Delete was greyed out for a conductor on its own.** Which is the one selection
  conductors were made selectable for. The status bar also said nothing about a
  conductor-only selection while Copy and Duplicate were live for it.
- **Save could not fail.** `Ctrl+S` into a read-only folder or onto a full disk was an
  unhandled traceback with the board still unsaved behind it, and the close guard would
  have treated it as saved. It is a dialog, the close guard is told, and a failed Save As
  does not adopt the path it failed to write.
- **Exports that reported success for files that were never written.** The 1:1 PDF export
  trusted `QPdfWriter`, which reports an unwritable target as a warning on stderr and
  returns; the 3D snapshot trusted `vtkPNGWriter`, which does the same — and the snapshot
  had none of the "is there GL here" guard the guide export has, so on a machine without
  offscreen GL it ended the process with every unsaved edit in it. Both check the file on
  disk; the PDF export offers to open its output like the other two exports.
- **A freshly opened board inherited the previous one's "parts moved" marks.** Net ids are
  document-local — every board has a `net-1` — and the record of which nets were routed for
  a position a part had since left was never cleared on Open, so `Ctrl+R` on a new file
  asked about re-routing nets nobody had touched. Open, New and Recover all forget it now,
  with the schematic selection, the described custom parts and the placement seed.
- **Board Features ▸ Remove with nothing chosen deleted the first row.** Chosen for the
  user. The button follows the selection now.
- **The Cut Track tooltip stuck on "only stripboard".** A board switched to stripboard
  under an open window kept the explanation of why the tool was unavailable on a tool that
  had started working. Reset 3D Camera and Exploded View, which do nothing until the 3D
  panel has been opened, are greyed out until then rather than silently inert.
- **Escape wiped the status bar when there was nothing to leave.** The routing summary is
  posted with no timeout so it can be read, and a reflex Escape cleared it. It clears only
  when a mode was actually left.
- **"Saved …" and "Exported …" never went away.** Both were posted with no timeout, so the
  path of the last save sat in the status bar for the rest of the session. Eight seconds,
  like every comparable message.
- **Menu tooltips were switched on for four menus of twelve.** The explanations written for
  Reload, Board Setup, Board Features, the exports, Measure and the 3D items, and the full
  path behind each recent file, never appeared anywhere. Every menu shows them.
- **The recent-files menu went stale in place.** It was rebuilt on save, open and clear
  only, so a file moved or deleted while the window was up stayed listed and failed when
  picked. It is rebuilt as it opens.
- **The recovery offer and the first-run update question stacked.** Both are scheduled a
  moment after the window appears, and the second opened its modal on top of the first's.
  The update question waits while another dialog is up.
- **A recovery record that would not parse was offered forever.** And stood in front of
  every other record behind it. It is dropped after the failure to open it is reported.
- **The "cannot write the recovery file" warning was wiped within seconds.** It was a status
  message, and the next command's description replaced it — so autosave was dead for the
  rest of the session with nothing on screen saying so. It is a permanent field now, shown
  while the condition lasts and cleared when a write succeeds again.
- **A file change under a running planner swapped the board out from under the plan.**
  The planner pumps the event loop so the window can repaint and offer Cancel, which also
  let the file watcher reload the document mid-plan — and the plan was then committed into
  a different board. The watcher waits for the planner, and so does the close button, whose
  caller would otherwise have dispatched into a window that had already cleared its
  recovery record.
- **A refused net dialog threw away what was typed.** A duplicate name was refused into
  the status bar after the dialog had closed; it is a dialog now, and the form comes back
  filled in.
- **The placement cursor was the only mode cursor.** Drawing, connecting, cutting and
  measuring all left the ordinary arrow over a board where a click no longer selected
  anything; every mode shows the crosshair.
- **Leaving the board cleared nothing.** The status bar kept naming the hole the pointer
  left the board over, and Paste from the menu still landed there. Panning with the middle
  button and zooming from the keyboard also left the ghost and the address on the hole
  that had scrolled away; both re-read what is under a pointer that has not moved.
- **Zoom stopped one notch short of its own limit.** The last wheel step before a bound did
  nothing rather than landing on the bound, and a `fitInView` that landed outside the range
  — two pads framed from a DRC finding in a large viewport — left the wheel dead in both
  directions. Steps are clamped; fits are pulled back inside.
- **Opening a file from the command line did not fit it.** The dialog route fitted the
  board; `perfstudio big-board.perf` arrived at a fixed six pixels per millimetre, which is
  a corner of anything bigger than a 7 × 9 cm board.
- **A truncated `.net` was a traceback.** `SExprSyntaxError` was not a `ValueError`, which is
  what both importers catch, so the commonest real breakage escaped the window and the MCP
  server alike. It is a `ValueError` now.
- **Double-clicking a pin while wiring the sheet opened a dialog** over the half-made
  connection. The sheet follows the board's rule: a mode owns the first click of any pair.
- **A download that could not be written was an exception in the event loop.** A disk
  that fills part-way through 300 MB now cancels the transfer, reports why, and removes the
  `.part` file, like every other failure in the updater.
- **The Schematic panel's Remove was enabled for a symbol nothing defines** — one that
  exists only because a net names it — with an explanation of why it could not work as its
  only outcome. The empty panel also said "0 part(s), 0 net(s)" and nothing else; it now
  says what the two buttons under it are for.
- **Tooltips learned the two things people hover for.** A conductor's tooltip names its
  net, and a part's names its hole and rotation.
- **The guide said several things that were not so.** "Cut it to 32 × 22 holes (W × H mm)"
  computed the size from the pitch alone and dropped the printed border, so on a board from
  a preset it was 4 to 9 mm too small. The DIP step said to leave the IC "until phase 8",
  where there is no such step; it now says the socket goes in now and the IC after the
  closing checks. "Put U1 into their sockets" agrees in number and allows for no socket.
  Bare wire was told to be stripped 0 mm. "Flux is not optional on a run this long" was
  appended to two-pad joints. A trapped jumper was named `cond-7`, which nobody at a
  bench can look up; it is named by its ends. The polarity note's dash swallowed the hole
  address. The header spelled the board `32×22 FR4` twelve lines above `32 × 22` and
  `FR-4`. One separator for a part's span (`→`), one spelling for a wire's colour across
  the CSV, the HTML and the JSON (`colour`), one wording for its type. The goldens were
  re-blessed after reading every changed line.
- **DRC, LVS and the router name things by address.** Four DRC rules identified a run only
  by its conductor id; they carry its ends now, and the material is spelled `FR-2` rather
  than the enum's `FR2`. `mOhm` is `mΩ`, as the guide already had it. An LVS short could
  print an internal physical-net id where an address was promised. Router explanations
  are capitalised consistently. The autoroute summary the headless CLI prints used a
  non-ASCII separator four hundred lines below the comment forbidding one; `--headless`
  also pluralises one way throughout, uses the window's own reader (so a directory is one
  line and not a traceback), and says so when it cannot create its output folder.
- **The documentation caught up with the code.** Both READMEs claimed `v0.4.0` and
  forty-four MCP tools (it is `v0.9.0` and fifty-one), recommended the `python -m`
  invocation `docs/MCP.md` warns against, and — in Turkish — gave English menu paths.
  `CONTRIBUTING.md` said ruff was not a gate and counted ~1260 tests; it is, and there are
  ~2070. `examples/README.md` said the FR-2 guide "halves the dwell"; it cuts it from
  three seconds to two.
- **The AppImage build fetches its own runtime.** v0.9.0's release job died on
  `Failed to download runtime: server returned status code 302` twenty minutes after
  the same job had passed on a dry run: an AppImage is a squashfs image behind a small
  runtime, appimagetool downloads that runtime itself at build time, and its fetcher
  does not follow a redirect. `curl -L` does, and `--runtime-file` is the way round that
  appimagetool's own error message names.
  - It also closes a hole the script was already arguing against. `appimagetool` is
    pinned to a release because "whatever was on the server that morning" is not
    something a release can be reproduced from — and the runtime, which is the part that
    ends up INSIDE the artefact, was being pulled from `continuous` by something nobody
    could see. It is a visible input now.

## [0.9.0] - 2026-08-31

### Added

- **The schematic leaves the window: File ▸ Export Schematic…, and the button beside Fit
  the Sheet.** 0.8.0 gave the circuit a picture and left it on the screen — the sheet could
  be looked at and not printed, embedded, attached to a forum post or handed to anyone who
  was not sitting at the machine. Three files land beside the document: `_schematic.svg`,
  `_schematic.pdf` and `_schematic.png`.
  - **One renderer, three formats.** `schematic_export.py` writes the SVG and
    `ui/export_schematic.py` asks Qt to paginate or rasterise *that string* — it draws
    nothing itself. Three writers over one `SchematicDrawing` would be three chances for
    the printed sheet, the emailed PNG and the embedded SVG to disagree about what the
    circuit is, and one export that contradicts another is worse than no export.
  - **The writer is an engine module**, so it is pure and its output is comparable: both
    frozen sheets in `tests/schematic_golden/` now have an `.svg` beside the text dump,
    blessed by the same `PERFSTUDIO_BLESS_SCHEMATIC=1`, because they describe one drawing
    and blessing half of it would leave the two disagreeing. Every board in the repository
    is checked for well-formed XML and for dropping nothing on the way out — a wire, a
    junction dot or a whole symbol kind quietly not handled still looks like a schematic.
  - **It is not a copy of the panel, and the two differences are the point.** Screen labels
    hold a pixel size, because a reference that shrank to nothing when the sheet was fitted
    would make the fitted view the one view that says nothing; paper labels are millimetres
    of sheet, because paper does not zoom. And the panel is light ink on a dark sheet, which
    is right at midnight and wrong on every printer, so the export is black on white —
    monochrome, because the rail glyphs already say which rail sinks and which sources and a
    photocopier keeps shapes rather than colours. `SheetInk` keeps the three net classes as
    separate fields for a caller who knows its output is a screen.
  - **What they must NOT decide separately is geometry.** The rail glyph's bars moved into
    `schematic.rail_glyph_bars`, which the panel and the writer both call. They sit in room
    the *layout* cleared on the strength of `RAIL_GLYPH_MM`, so a renderer holding its own
    half-width could draw bars through wires the sheet believed it had cleared. The shared
    function also honours the rail's own direction rather than assuming downward — no board
    here produces an upward ground rail, so that is the reading that stays correct rather
    than a bug anybody has seen.
  - **Two Qt traps are written down where they bite.** Qt's SVG support is SVG Tiny 1.2,
    which has no `dominant-baseline` — and Qt is what makes the PDF, so the writer computes
    text baselines itself; an attribute nothing implements is ignored rather than refused,
    and every reference would land on top of its own symbol on paper while the browser
    preview looked perfect. And text painted onto `Format_RGB32` on Windows gets ClearType,
    which draws black text as orange and blue pixels because it exploits one monitor's
    stripe order — fine as a rendering trick, wrong as file contents, since it survives the
    print and the resize. Asking for an alpha channel forces greyscale antialiasing;
    `test_an_exported_png_has_no_subpixel_colour_fringes` measures it, because nothing in
    the code says "no ClearType".
- **Crash recovery.** The window has always refused to reload over unsaved edits, because
  losing somebody's work to a background event is the one outcome that must not happen. The
  process stopping without asking anybody was the other half of that sentence, and until now
  everything since the last Ctrl+S went with it. A modified board is now written to the
  user's own data directory every thirty seconds, and the next start offers it back.
  - **It protects work and never restores any.** The recovered board is offered, the default
    answer is *Decide Later*, and the file on disk is not touched until the user saves — the
    same position `ui/updater.py` takes with an installer it declines to run, and it matters
    more here. What is at risk is somebody's own board, and a restore that guessed wrong
    would put an older document in front of them that they would then save over the good
    one. There is nothing to undo that with, so the decision is theirs.
  - **Two things are checked before anything is offered**, because a crash can land in the
    gap between a save and the deletion that follows it: a record identical to the file means
    nothing was lost and is dropped without a question, and a record OLDER than the file is
    the stale copy and is never offered at all.
  - **Records live in the user's data directory, never beside the board.** A sidecar file
    would be litter in a folder somebody curates, would fail on a read-only or network
    location, and — the case that matters most — has nowhere to go at all for a board that
    was never saved, which is precisely the board with the most to lose.
  - **The write is atomic and cannot raise into the window.** A crash during the write must
    not leave a half-file where the good one was, and a full disk or a locked profile must
    not take the board down *from the code that exists to protect it*; the window says so
    once and carries on. `recovery.py` is pure and `ui/autosave.py` is the host, the same
    split as `updates.py` against `ui/updater.py`, so what a record means is decided by
    functions a test can hand a string to.
- **A part the library does not have: Custom Part…, in the Parts dock and in Add a Part.**
  Sixty-one footprints is a good library and not every part anybody owns, and until now a
  part that was not in it could not be placed at all — the board could not be built, and the
  guide said `unknown-footprint` about a component the user had no route to define. That is
  the likeliest thing to arrive through the "board I could not build" issue template.
  - **The identifier IS the definition, so nothing is stored anywhere.**
    `box-4x2-p1-r3-15x10x8` is not a name that refers to a definition kept elsewhere; it is
    a four-by-two pin grid, three holes between the rows, in a 15 × 10 × 8 mm body, and
    `footprints.py` builds it on demand. The `.perf` format does not move, the fifteen
    golden fixtures are untouched, and a board mailed to a stranger opens with the same part
    on it — no library to install and nothing to go missing. The two alternatives were both
    worse in a way this project has already ruled on: a footprint stored in the document
    reopens the byte-for-byte format for something computable (PLAN.md D6's argument about
    meshes, D3's about symbol positions), and a user library beside the application means a
    board that opens on the machine that drew it and nowhere else, which is exactly the
    failure `unknown-footprint` already describes.
  - **`generic-box` was an archetype nothing built.** It was already coloured by
    `ui/bodies.py`, phased by the guide, drawn as a labelled box on the schematic and
    extruded in 3D — every consumer was ready for a part no generator produced.
    `generic_box_footprint` is that generator, and a rectangle with a grid of leads covers a
    sensor board, a transformer, a seven-segment display and a module nobody has heard of.
    Its pins are numbered row by row, which is what a module's silkscreen does; a DIP goes
    anti-clockwise, and `dip_footprint` is still there for when that is the answer.
  - **One spelling per part, enforced rather than tested for.** Every parse ends by
    rebuilding the id from what it read and refusing anything that does not come back
    identical, so `dip-08` is not a footprint. Without that, a document could hold a part
    whose own id disagrees with the name it is stored under, and two ids could mean one
    part. The same requirement is why the auto-generated ids for the electrolytic, disc and
    film capacitors gained their missing dimension: an id that dropped the can height meant
    two different parts could be handed one id.
  - **The dialog never spells an identifier.** It calls the engine's own generator and shows
    what came back, so there is one description of the grammar and not two — and it refuses
    to close while the engine will not build the part, with the numbers that caused it still
    on screen rather than three steps later as a footprint nothing recognises.
  - **An agent gets told at the moment it needs telling.** Placing an id nothing recognises
    used to say "call list_footprints"; it now returns the whole grammar. An agent that has
    just been told a part does not exist is about to give up on the part, and "describe it by
    its measurements" is not something it could guess from a list of sixty-one names. It is
    in the refusal rather than a tool description because it is a page long and only matters
    once.
- **`render_schematic`, the MCP server's third picture.** `render_2d_view` and
  `render_3d_view` both show COPPER, which is what a circuit was turned *into*; an agent
  that had just called `connect_pins` eleven times had changed the thing this application
  exists to build and no tool would show it. The notes travel back with the image, because
  a pin the netlist names that the footprint does not have is a hole in the design that a
  picture alone would hide. It needs no GL, so it works where `render_3d_view` cannot.
- **`--headless` writes the sheet too.** It is the only place the SVG writer, Qt's SVG
  renderer and a real board meet on all three operating systems: the goldens prove the
  writer says the same thing everywhere, and this proves what it says can still be turned
  into a page.

### Fixed

- **The rule that holds the MCP tool count down had quietly stopped being true, and is now
  measured.** PLAN.md §13's answer to "the tool surface will explode" was never the ~25
  ceiling — it was that every tool is tied to a group and a reason in `docs/MCP.md`.
  Nothing checked it: `reroute` had been registered and never documented, and the count read
  forty-four in `docs/MCP.md` and fifty in PLAN.md while the server had fifty. Three numbers
  in three files is what a rule nobody measures decays into.
  `test_every_tool_is_named_in_the_documentation_and_nothing_else_is` now reads the table
  and both prose counts, in both directions — an undocumented tool is one nobody had to
  argue for, and a documented one that no longer exists sends an agent, the reader that
  document is written for, after something that is not there.

## [0.8.1] - 2026-08-31

### Fixed

- **0.8.0 never reached PyPI.** The upload was refused with a 400 for one string:
  `Topic :: Scientific/Engineering :: Electronic Design Automation` is not a trove
  classifier and `... Electronic Design Automation (EDA)` is. Nothing else about 0.8.0 was
  wrong — the three installers built, published and smoke-tested normally, and are on that
  release — so this carries the same application with the metadata corrected.
  - **The gap that let it through is the interesting part, and it is closed.** Classifiers
    are a closed list PyPI validates against, and NOTHING local validates them: `twine
    check --strict` checks that the long description renders and does not look at
    classifiers at all. So an invented one passes the build, passes `twine check`, and
    passes the release dry run — which gates the publish step off and therefore never
    contacts PyPI. The first thing that can refuse it is the real upload, by which point
    the tag exists.
  - `test_every_classifier_is_one_pypi_actually_has` now checks them against
    `trove_classifiers`, which is the same list PyPI validates against shipped as data, so
    it is the real check rather than an approximation. It runs on every push and locally,
    which is where a release-blocking mistake has to be caught — a check that only runs
    during a release is a check that first runs when it is too late.

## [0.8.0] - 2026-08-31

### Added

- **The circuit has a picture now: View ▸ Show Schematic (`Ctrl+5`).** The board has always
  answered "where does this go" and the ratsnest has always drawn what is still owed, but
  the thing the board is a way of BUILDING — the circuit — existed only as a tree of net
  names in a dock. LVS would say *net VOUT is open* to somebody with no way to look at what
  VOUT is. The new panel draws `doc.nets` as a schematic: symbols, orthogonal wires,
  junction dots, ground and power glyphs, references and values.
  - **It is generated, not stored, and that is the same decision footprints made** (PLAN.md
    D6, and D3 for why this is a view and not an editor). A `.perf` file carries no symbol
    positions and neither does a KiCad netlist, so there is nothing to read; putting them in
    would reopen the byte-for-byte format for something no user edits, and would be a
    schematic editor arriving one dataclass at a time. `schematic.py` is an engine module
    and obeys the engine's rule — no clock, no RNG, no filesystem, no Qt — so the whole
    layout is reachable from a test that hands it a document, and two sheets are frozen
    whole in `tests/schematic_golden/` the way the build guide is.
  - **Ground and power become rail glyphs rather than wires**, which is the single largest
    difference between a readable sheet and a hairball: a GND net touching eleven pins drawn
    as wires is eleven lines crossing everything. The classes were already in the document —
    `Net.net_class`, filled in on import by `parsers.kicad.infer_net_class` — so it costs
    nothing. Rails are also kept out of the layering graph, because a net touching every
    part would otherwise make every part adjacent to every other and collapse the columns:
    the same hairball, arriving by the back door. `SchematicOptions.rail_classes` turns it
    off for a four-part circuit where the glyphs are more ceremony than the sheet needs.
  - **No wire can cross a symbol, and that is a consequence rather than a tuning
    parameter.** Symbols sit in a column/row grid; verticals run only in the channel between
    two columns and horizontal trunks only in the channel between two rows, each channel
    widened to fit the tracks a left-edge sweep assigns it. Rail anchors come out of the same
    track pool as the trunks, so a wire can never be drawn along the bars of a ground symbol
    — a crossing carries no dot and reads correctly, a line lying ON the glyph does not.
    Both properties are measured on all nineteen boards in the repository, not on a chosen
    one, because "holds by construction" is a claim about every input.
  - **A symbol gets its real shape only where the registry knows what each lead IS.** A
    resistor, capacitor, electrolytic, diode, LED, crystal and potentiometer are drawn as
    themselves; polarity comes from the registry's own pin names with pin 1 as the cathode
    for an unnamed polarised part — exactly the rule `guide._polarity_note` follows, and for
    the reason it exists: an LED's pin 1 is its anode and a diode's is its cathode, so a
    convention keyed on pin 1 draws one of the two backwards on the screen and then on the
    bench. A TO-92 has no E/B/C anywhere in this codebase, so it is a labelled box with
    numbered pins. Drawing a transistor symbol would assert a pinout nothing here holds.
  - **Clicking it moves the same selection everything else shares.** A symbol selects that
    part on the board and centres the view on it; double-clicking opens its properties; a
    wire selects its net in the Nets dock, which is what already lights the net up on the
    board. A part named by the netlist and missing from the board is drawn dashed and says
    so rather than selecting nothing. Cross-probing is the whole reason the sheet is in the
    window instead of an exported file.
  - Like the 3D and build-guide panels it fills itself only while open, and a redraw leaves
    the viewpoint alone — the rule `view3d.populate_renderer` follows about the camera, so
    editing one net cannot throw away the part of the sheet you were reading.

- **The circuit can be drawn before the board exists.** The order every other EDA tool
  works in, and the one this application could not do: every route a part had into a
  document ended in `component.place`, which needs a hole — so you had to choose where a
  resistor went before you had finished deciding there was a resistor. Now the schematic
  panel adds parts, wires them and hands the finished design to the board.
  - **`doc.parts` is the design; `doc.components` is the board.** A `SchematicPart` has a
    reference, a value and a footprint, and no anchor. It is a SEPARATE list rather than a
    `ComponentInstance` with an optional anchor, and that is the whole safety of it: DRC,
    occupancy, connectivity, the router, the placer, the guide, the 1:1 PDF and both
    renderers all iterate `doc.components` and are right to assume every entry has a
    position. An optional anchor would have made sixty-odd sites responsible for
    remembering that a part might be nowhere, and the first one to forget would either
    crash or quietly treat the part as sitting at hole A1. The cost is one rule instead —
    **a reference is unique across both lists**, because every net node is a `(ref, pin)`
    pair and two R4s is a netlist that cannot say which one it wired.
  - Six commands: `part.add`, `part.update`, `part.delete`, `part.place` (a whole design in
    one undo step) and `component.unplace`, the inverse. Placing is a MOVE between the two
    lists that keeps the part's id, so a replayed journal still describes one thing.
    `net.connect` needed no change at all — `assert_pins_free` has always said in its
    docstring that it does not check whether a component exists, and this is the workflow
    that was waiting for it.
  - **Removing means two different things and the button knows which.** `part.delete`
    takes the net nodes with it, because a net naming a part the design does not have is
    asking for something nothing has heard of. `component.delete` still does not, because
    off the board is an LVS open the schematic is right to keep asking about. Clicking
    Remove on a placed part unplaces it, since "wrong hole" is what that almost always
    means and deleting the circuit around it would be a much larger answer.
  - **Renaming now carries the wiring**, for a schematic part and a placed component
    alike. It did not before: R1 wired into six nets and relabelled R7 came out the other
    side connected to nothing, and the properties dialog carried a tooltip warning people
    to rename before importing a netlist rather than after. That was a wart, not a
    decision — a reference is the only name a net has for a part. Refused, rather than
    silently merging two parts, when the new reference already has those pins wired.
  - **Joining two pins is one function now** (`view2d.join_pins`), shared by the board's
    connect tool and the sheet's. The board joins pads and the sheet joins symbol pins,
    and the two must not disagree about the cases that are not "make a net": one pin
    already on a rail, both already on the same net, both on different nets — refused,
    because merging two nets is a decision about the circuit rather than about two clicks.
  - The panel gained **Add Part**, **Wire**, **Remove** and **Place on the Board**. Placing
    lands the parts in a grid and says to press Ctrl+Shift+A next rather than quietly
    optimising: arranging a board is a second of annealing and the one step somebody most
    wants to watch and re-run.
  - **The agent surface keeps parity**, which is the rule this project holds itself to:
    `add_part`, `update_part`, `delete_part`, `list_parts`, `place_parts` and
    `unplace_component`, plus `parts_not_placed` in `get_status` — because "the circuit is
    drawn and nothing is placed" and "the board is empty" look identical from a component
    count and call for opposite next moves.
  - **PLAN.md D3 is intact rather than reversed**, and the note under it now says where it
    landed. What D3 declined to write was a *geometric* schematic editor — symbols you drag,
    wire corners you break, sheet coordinates in the file. There are still none of those:
    every sheet is derived from the document by `schematic.py`, so there is no second truth
    to keep in step with the netlist. `.perf` gains one optional `parts` array, omitted when
    empty, and all fifteen golden fixtures are byte-for-byte unchanged.

- **`pip install perfstudio`.** The application has been telling people to do this since
  0.7.0 — `ui/updater.py` offers a `pip` install no download at all, on the grounds that
  "its update is `pip install -U perfstudio`" — and there was nothing on PyPI to install.
  `release.yml` now builds an sdist and a pure-Python wheel on every tag and every dry
  run, and publishes them by **trusted publishing**, so there is no API token anywhere in
  this repository. The one-time setup on the PyPI side is written down in
  [docs/RELEASING.md](./docs/RELEASING.md#pypi); the environment name in it is not
  optional.
  - **It does not wait for the three installer jobs.** A wheel has different failure modes
    from a PyInstaller bundle, and hanging it off ninety minutes of Qt packing would make a
    broken sdist the last thing anybody heard about. It also means the wheel is still
    published when an installer fails — which is the right way round, since the wheel is
    what a platform with no bundle falls back to. An Intel Mac and an ARM Linux box have an
    install now.
  - **The wheel is installed and run before it is published**, with its dependencies, in a
    clean virtualenv, from a directory that is not the checkout. Nothing else in the
    workflow tests the claim a PyPI release actually makes: the three installers carry
    their own Python and never resolve a dependency.
  - Two checks that only exist because the answers are invisible until published. `twine
    check --strict` — which failed on the first wheel built here, because `pyproject.toml`
    had no `readme` and the project page would have shipped blank. And the wheel's contents
    against `[tool.setuptools.package-data]`, because that list is written out file by file
    on purpose, and a list nothing checks is a glob that stops matching the day an icon is
    renamed.
  - `docs/MCP.md` can now point an agent at `uvx --from "perfstudio[mcp]" perfstudio-mcp`,
    which needs nothing installed first. The old instructions required a clone, an editable
    install and then the absolute path of the Python that had received it — which the
    document had to warn about, because a bare `python -m perfstudio.mcp` finds whichever
    Python is first on `PATH` and that is the usual reason a client reports the server
    exiting immediately.
  - `README.md` carries absolute links now rather than relative ones. That is `readme =
    "README.md"` doing it: PyPI resolves neither images nor links against the repository,
    so the natural form renders there as four broken images and fourteen dead links.
    GitHub renders the absolute form identically, so there is still one README and no
    second copy to drift out of step. `README.tr.md` keeps its relative links — nothing
    packages it.

## [0.7.0] - 2026-08-25

### Added

- **PerfStudio can tell you a new version exists, and fetch it.** The last unwritten line
  of PLAN.md §14's launch checklist: the three installers have shipped since 0.4.0 and
  updating meant noticing a release on GitHub and downloading it by hand, which is a thing
  nobody does on a schedule. Help ▸ **Check for Updates** asks now; Help ▸ **Check
  Automatically at Startup** asks once a day as the window opens. What comes back is one
  dismissible strip above the board naming both versions and quoting the changelog's own
  bolded lead-ins — the release notes are built out of `CHANGELOG.md` by `release.yml`, so
  the summary in the strip is the sentence somebody already wrote by hand rather than one
  the application invented.
  - **It does not install anything, and that is the design rather than an unfinished
    edge.** The file is downloaded into the user's Downloads folder, checked against the
    `SHA256SUMS` now attached to every release, and shown to them in their file manager.
    Running it needs elevation on Windows, replacing a bundle inside `/Applications` on
    macOS and overwriting a running AppImage on Linux — and doing that on somebody's
    behalf with an installer nobody has signed (§12, unbought) is a mechanism that is
    indistinguishable from malware and has no way back when it goes wrong. The last click
    is the user's.
  - **Nobody is checked up on before being asked.** A check is a request to GitHub
    carrying the machine's address, so the first run asks, in one sentence with two
    buttons, and remembers the answer — which is why the stored preference has three
    states and not two: "not asked yet" is not "said no". Either answer is a menu item
    away afterwards.
  - **The decisions are separated from the network, the same way the engine is separated
    from the filesystem.** `updates.py` has no I/O in it at all — which release is newer,
    which of the three files suits this machine, when to look again, what the notes say —
    and `ui/updater.py` does the fetching, the hashing and the clock. Fifty tests reach
    every decision by handing a function a string; none of them opens a socket.
  - Four answers here are the kind that are wrong by being plausible, and each is pinned:
    a `.devN` build sorts BELOW the release it is heading for (`version.version_tuple()`
    deliberately says the opposite, and somebody running 0.8.0.dev3 out of a clone should
    be told when 0.8.0 ships); the highest VERSION wins rather than the newest
    publication, because a patch to an old line published later would otherwise be offered
    to everyone as an upgrade; an Intel Mac and an ARM Linux box are offered the release
    notes and no download, because the only assets are arm64 and x86_64 respectively and a
    match on the extension alone hands somebody 300 MB that cannot start; and a response
    that is not a release feed — a hotel wi-fi login page — reads as "could not check",
    never as "you are up to date".
  - A build installed with `pip` is told about the release and sent to the release page
    rather than offered an installer, because its update is `pip install -U` and an
    installer is no use to it.
  - The check runs from `main()` a moment after the window is on screen and never from a
    constructor, which is what keeps a suite that builds a great many windows off the
    network. `test_building_a_window_checks_nothing` asserts it rather than trusting it:
    it is one convenient line away from being untrue at any time.

- **Every release now carries a `SHA256SUMS` file** (`release.yml`), covering all three
  installers. It is what the in-application download verifies against and what the release
  notes tell a person to run by hand. Worth being precise about what it proves: it arrives
  from the same host over the same connection as the installer, so it says the download
  came through intact, not that GitHub handed out the file this project built. That second
  claim needs the code signing §12 has not bought.
  - The job that builds it now runs on a **dry run** as well, with every step that
    publishes gated individually. It was skipped there, which left three globs that must
    each match exactly one file as the only thing in the release path never executed
    anywhere but on the tag — and `docs/RELEASING.md` exists to say that the tag is the
    worst place to meet a first run.

- **The build guide is compared against a known-good one** (`tests/test_guide_golden.py`,
  `tests/guide_golden/`). All four exports of the routed NE555 fixture — JSON, the
  self-contained HTML, the cut list and the BOM — are stored whole and compared whole.
  `test_guide.py` was thorough about the guide *model* and asked the exporters targeted
  questions, and targeted questions can only catch what somebody thought to name: a phase
  that swapped places, a checkpoint that stopped being generated, a sentence that lost its
  polarity warning, a BOM row that vanished when a footprint was renamed. The guide is what
  this application is *for* — the thing a person prints and follows with an iron in their
  hand — and nothing compared one against a known-good one.
  - Not part of the differential proof: the TypeScript engine never had a guide exporter,
    so there is nothing to be differential against. These are our own output, blessed
    deliberately, which is why they sit under `tests/` beside `render_signatures.json`
    rather than in `tools/diffcheck/golden/`.
  - Floats are compared at **12 significant digits**, not bit for bit, for the reason
    `test_footprints.py` records: the JSON emits full-precision lengths through
    `math.hypot`, and macOS arm64's libm disagrees with x86-64's in the last ULP. Twelve
    digits absorbs ten of those and still fails on a tenth of a millimetre. The other three
    formats print to one decimal place and are compared as text.

### Changed

- **The placer knows what a stripboard is.** `striproute.py` has said so in its own
  docstring since it was written — "`placer.py` is the tool for that half, and it does not
  yet score strip alignment" — and the gap showed as the one failure a user could not act
  on: `cannot-separate`, two pins of different nets in neighbouring holes of one strip,
  with no hole between them to put a drill in. The planner reports it and says *move one
  of the parts*; the placer is what moves parts, and it was arranging boards with no idea
  the copper was already joined. Three things changed together, and none of them touches a
  pad-per-hole board — every term is zero there and the arithmetic is bit-for-bit what it
  was:
  - **The pairs the planner would refuse are priced** (`PlacementWeights.strip_conflict`),
    counted by exactly the rule `striproute` refuses them by, from one constant —
    `stripboard.MIN_SEPARABLE_GAP` — that both modules now read. Same shape as
    `heat-proximity` and the placer reading one clearance out of `model.py`: an optimiser
    that packs a board its own planner then declines to wire is worse than no optimiser.
    A pin nobody declared a net for is a party to no pair and still blocks the drill,
    because that is what it physically does and what the planner already assumed.
  - **Alignment counts the strip axis and not the other one.** The term takes the cheaper
    of "one rail along a row" and "one rail along a column", which is right on a board
    where a rail can be run either way and wrong on one where the copper runs the way it
    runs: two pins sharing a column on a horizontal-strip board are joined by nothing, and
    pricing that arrangement as free priced a connection the board does not make.
  - **A candidate stripboard is judged by `plan_stripboard`, not by the autorouter.** The
    winner among restarts is chosen by planning each one and asking what it would cost to
    build — and a stripboard is not routed by `autoroute.py` at all, since its copper is
    subtracted rather than added and a wire on its solder side shorts every strip it
    crosses. It was being ranked by a build nobody was going to follow, which could and did
    outrank the cost function above.
  - The visible effect is the one a stripboard user would ask for first: a three-pin
    inline part — a TO-92, a regulator, a 3-pin header — laid **along** the strips has
    three nets in three neighbouring holes and cannot be wired at all. It now gets turned
    a quarter so its pins land one per strip. Nothing else in the cost function can see
    that: it is the same three pins over the same span, so HPWL, overlap and heat all
    score the two orientations identically.

### Fixed

- **No two conductors are drawn in the same place, and there is a test that measures it.**
  Squinting at a render does not settle whether two solids overlap, so
  `test_no_two_conductors_are_drawn_in_the_same_place` walks the centrelines `view3d`
  builds and the radii it tubes them at and measures segment to segment, across all
  fifteen golden fixtures. Two conductors soldered into one hole are one joint and are
  excluded; anything else closer than the sum of its radii is a bug however good the
  picture looks from the default camera. It found both of the faults nothing else had:
  - **`layer_z` was counted twice.** `stacking_layers` returned a level and `conductor_z`
    added the document's own `layer_z` to it again — so a pair the stacker had deliberately
    separated (one at `layer_z` 1 and stack 0, the other at `layer_z` 0 and stack 1) came
    out at the same height and was drawn straight through. **Four of the fifteen fixtures
    had exactly that pair.** `layer_z` is the stacker's *floor* now, and the level it
    returns is the whole answer.
  - **A wire's descent into its pad swept too far.** The bend was as long as it was deep,
    so a wire coming down two stacking levels ramped nearly four millimetres across the
    board, through whatever was lying under it. Held inside a third of a pitch now, where
    the only other thing is something soldered to the same hole.

- **A solder run is one piece of metal, a wire goes into its holes, and the holes are
  round.** The second pass over the same view, from a camera put close enough to see what
  it was actually drawing.
  - **A run is one surface.** It was a constant tube with a sphere dropped on every pad —
    two primitives meeting in a hard crease all the way round, which is what read as beads
    threaded on a stick. The tube's own radius varies now: **1.2 mm across a joint, 0.72 mm
    across the bridge between two pads**. The narrowing is not decoration — it is what
    makes the joints countable, and counting joints along a run against the real board is
    exactly what somebody following the build guide does. The two end pads keep a solid,
    at precisely the radius the tube already has there, so a tube's flat cap never shows.
  - **A run is IN the surface; a wire is ON it.** Solder wets copper and stands as a
    half-round ridge, so a run's centreline is the pad plane itself. Both used to be a
    radius clear of it, which is why a joint drawn at the run's own height sat behind the
    pad it was made on and slid off it from every oblique angle — which is every angle
    this view is used from. The distinction PLAN.md §8.4 makes a requirement is in the
    geometry now, not only in the colour.
  - **A wire bends down into its pads.** It was a stick floating parallel to the board and
    stopping in mid-air above each hole, so it neither entered the board nor reached what
    it was soldered to — and once a wire could be lifted over another, its ends hung a
    millimetre above the copper. Each end drops to the pad over a short run-in, long enough
    to read as a bend and never more than a fraction of the segment it bends within.
  - **Holes and pads are round.** The bore was a 12-sided cylinder and a perfboard is
    mostly holes, so that one number set how machine-made the whole board looked. 28 and 40
    now; both are one glyphed source instanced at every hole, so the cost is per board.

- **Solder in the 3D view stops looking like grey plumbing.** Three things, and the
  lighting was the biggest of them.
  - **The lights travel with the camera.** They were nailed to world positions, one above
    the board and one below — and the lower one was the deliberately dimmer *fill*, so the
    solder side, the face you turn a board over to inspect, was lit by the weaker lamp at
    an angle unrelated to where you were looking from. A camera light keeps whichever face
    is towards you the lit one, however the board is turned, which is also what somebody
    with a lamp on the bench has. The key is offset up and left rather than dead-on,
    because a headlight flattens the shape of a fillet, which is the thing this view is for.
  - **A run is the size of a run.** The tube was 0.34 mm radius — under half a real solder
    run, and *thinner than the bead drawn at every pad*, which is why the silhouette came
    out as balls threaded on a stick. It is 0.50 now, wide enough to bridge the 0.64 mm
    gap to the next pad and still narrow enough to leave the pad visible; the joint swells
    to 1.3× rather than 1.9×, so a pad reads as a fillet instead of a ball. Insulated wire
    is 0.55, which is 24 AWG over the sleeve. Tubes and beads get 20 facets rather than 10
    and 12, since a joint is what a person zooms in on.
  - **Solder is metal.** A broad soft sheen — rough metal — where it was almost matte and
    unlit from behind, and a little ambient so a fillet turned away from the lamp is still
    a fillet. Still nothing like the tight glint on tinned wire, which is the distinction
    PLAN.md §8.3 requires and a test enforces.
  - `STACK_STEP_MM` follows the radii, so it still clears the widest pair that can cross —
    an insulated wire over a solder run.

- **Conductors stop levitating, and crossings actually clear each other.** Two crossing
  wires used to be drawn intersecting, and the fix for that was an offset per conductor —
  a running index over `doc.conductors`, 0.08 mm a step. It bought neither thing it was
  for: 0.08 mm is a tenth of what two 0.42 mm tubes need before they stop overlapping, so
  crossing wires went on interpenetrating exactly as before, while the offset accumulated
  into **4.47 mm of float on the dense fixture — a board 1.6 mm thick** — carrying the
  solder traces, which are soldered flat *onto* the pads, up with it.
  - `occupancy.stacking_layers` answers the question properly: which conductors actually
    cross which, from `geometry.paths_cross` — the same predicate DRC's
    `conductor-crossing` reads, so a shared endpoint stays the junction it is. A solder
    trace is pinned to level 0 (it *is* the copper; it cannot pass over anything), the two
    faces stack independently, and a board where nothing crosses comes out flat. On the
    NE555 routed solder-first: **6 of 23 conductors lifted, one level**.
  - Only then is a step big enough to work affordable, so `STACK_STEP_MM` is now derived
    from the tube radii rather than guessed. `layer_z` uses the same step, so a level
    somebody assigned by hand lifts a wire clear too.
  - **Solder rests on the copper.** A run sat 0.45 mm below the pad surface against a
    0.34 mm radius — 0.11 mm of daylight between a solder run and the pad it is soldered
    to, visible as a shadow line under every run. The height comes off the pad plane and
    the tube's own radius now, so it is tangent by construction, and one more magic number
    is gone.
  - **2D reads the same levels**, so the wire drawn passing over another there is the one
    drawn over it in 3D — it used to be whichever the scene happened to paint last — and a
    conductor that passes over another gets the dark outline that says so from directly
    above, where two lines at one depth otherwise read as a junction.

- **Two solder runs lying side by side are no longer a finding, and one gap is no longer
  two.** Routed with the solder-first style — the style a perfboard builder picks — the
  NE555 fixture came back with **51 proximity warnings on a board the tool had just routed
  itself**. 30 of those were runs beside runs, and 20 of the 51 were the same physical gap
  named twice, once from each run, because the rule walked each conductor separately.
  - **One physical pair is one finding.** Two pads either side of a 0.6 mm gap are one
    risk. This half is not a judgement call.
  - **A run beside a run is not reported; a run beside a PIN still is.** The gap is the
    same 0.6 mm, so the distinction is about attention rather than millimetres: a run
    beside another run is one you are laying yourself, on the face you are looking at, in
    the same phase — running parallel returns is how dense perfboard is built, and calling
    it out is the tool objecting to ordinary practice. A run passing a pin is a pad
    belonging to a part soldered three phases ago, with a lead through it for solder to
    wick up, that nobody is watching while they drag the iron. On that NE555 board:
    **51 → 17**.
  - The router still *prices* the proximity (`RouterCosts.proximity_risk`), so it steers
    around it where it can; it just no longer complains afterwards about the arrangement
    the chosen style asked for. Golden routes are unchanged.
  - Eight findings across three fixtures change, all named in `DIVERGES_FROM_TYPESCRIPT`
    (renamed from `SHARPER_THAN_TYPESCRIPT`, which now covers both directions) and pinned
    by tests that assert the *reason* — what is standing in the neighbouring hole — not
    just that the finding is gone.
  - The panel's gathering of these into one row per run (0.6.0) was the first answer and
    was a workaround at the wrong layer. It stays, because a run passing a DIP's row of
    pins still produces seven findings about one run, and that is what it is for.

- **The resistance checkpoint asks for a measurement the guide's own tool list can make.**
  Found by reading a generated guide end to end the way somebody holding an iron would,
  which is the dogfood test PLAN.md §11 says M5 does not close without. Every resistance
  checkpoint the four shipped example boards produce lands between **5.1 and 6.4 mΩ** —
  and the guide asks the reader to bring "a multimeter with a continuity buzzer", which
  resolves 0.1 Ω. So the tool printed *"about 6.4 mΩ (accept 3.2–9.6 mΩ)"*, a target and a
  tolerance band both **ten to forty times below one count** of the instrument it had just
  told you to fetch, and hedged it with "use four-wire mode if your meter has it". A
  verification step nobody following the guide could carry out — in the list of
  verification steps that is the entire reason this application exists, and the first thing
  the README claims makes it different.
  - The gate was a pad count; the question is whether the person holding the meter can
    tell a good run from a bad one, so `GuideOptions.meter_resolution_ohm` decides now.
    Below it, the check becomes the one that cheap meter *can* perform and that catches
    exactly the failure the check is for: **it must read as a dead short**, because a cold
    joint or a cracked run reads in ohms — three orders of magnitude away and unmissable
    on anything. The computed value is still quoted, as the context it always was.
  - Above it — a long pure-solder run where a wire spine was declined — the number and the
    band stay, because there the meter can resolve them. Both branches are now tested;
    before, only the unreachable one was.
- **A resistor sitting diagonally off an electrolytic is no longer a DRC error.** Rule 1
  (`component-body-overlap`) compared axis-aligned bounding boxes, and the note in the
  source called what was missing "a true rotated-polygon intersection test" — which
  pointed at a problem this project does not have. A part turns only by a multiple of 90
  degrees, so a rectangular courtyard never leaves the axes: for **53 of the 61** generated
  footprints the box *is* the polygon and the check was already exact. The other 8 are the
  circular courtyards — the electrolytics and the LEDs, a 24-gon from `_circle_outline` —
  where a box is **29% more area**, all of it in the corners. So the entire defect was a
  rectangle clipping the corner of a circle and being reported as an **error** on two
  parts that genuinely clear each other, which is how a findings panel becomes the thing
  people scroll past.
  - Both paths are kept, and deliberately: the boxes still decide for two rectangles,
    because the separating-axis projections multiply by an edge length and scaling can
    collapse two floats that differed — which would move the verdict on a pair overlapping
    by one ULP. The placer packs parts until their courtyards meet, so that pair is not
    hypothetical.
  - `placer` reads the same predicate, out of `geometry.convex_polygons_overlap`, because
    an optimiser that scores an overlap its own checker will not confirm moves parts apart
    to satisfy nobody. Its overlap AREA — the annealer's gradient — is charged only for a
    pair the verdict says overlaps, so it never prices a square millimetre that is not
    there.
  - **One recorded divergence from the TypeScript engine**, and it is the first in that
    direction: `random-02`'s X3 against X6, listed by name in `SHARPER_THAN_TYPESCRIPT` and
    pinned by a test that asserts the geometry — boxes meeting, courtyards clear — rather
    than merely that the finding is gone. Across the 15 fixtures, 41 body-overlap findings
    become 40. Recorded in the test rather than edited into the `.expected.json` files, for
    the same reason `PYTHON_ONLY_RULES` is: those are dumps from the engine this one is a
    port of, and hand-editing one would make the next regeneration silently disagree.

## [0.6.0] - 2026-08-14

### Added

- **A pin on an edge-connector finger is a DRC error** (`edge-connector-conflict`), the
  third member of the family with `mounting-hole-conflict` and `cut-track-conflict` and
  the most absolute of the three: a bore and a cut each destroy the pad and leave a hole,
  but a finger is solid copper that was *never drilled* (`geometry.undrilled_holes`), so a
  through-hole lead cannot be fitted there at all. Nothing checked it, so a part dropped on
  the finger strip was accepted in silence — and the finger strip runs along the board
  edge, which is exactly where a connector or a terminal block gets placed.

### Changed

- **The placement ghost goes red over a hole with no pad.** A mounting bore, an
  edge-connector finger and a cut track all leave a position on the grid with nothing to
  solder a lead into, and DRC has always called each of them an error — but the ghost
  stayed green right up to the click, so the part went down and the only thing that ever
  said so was a line in the DRC panel afterwards. The status line now names it too. The
  placement is still not *refused*: a mounting hole can be added over a part that was
  already there, so refusing would only make the same board harder to reach while leaving
  it reachable.
- **The blank-board guidance appears until you have placed your first part, then stops.**
  It is for the first launch; repeating it on every launch afterwards is the application
  explaining its own front door to somebody who has been through it a hundred times — and
  there was nowhere to click it away, because the block is transparent to the mouse by
  design.

- **Two solder traces running side by side are one row in the findings panel, not
  sixteen.** `solder-trace-proximity` fires once per pad *per trace*, so eight pads of
  perfectly ordinary parallel routing put sixteen copies of one sentence in the panel and
  pushed everything else off the bottom — which is how the most valuable rule in the tool
  became the one people scroll past. Gathered by the run now (`C4–J4 · 8 pads`), with
  every pad still there one level down.
  - The rule itself is unchanged, and deliberately: it is a **warning**, not an error —
    nothing is refused and the board is legal — and DRC's output is compared byte-for-byte
    against the reference implementation this port is proved against
    (`test_matches_typescript_golden_drc`). Grouping in the engine was written, measured
    at 8 findings → 4 on `random-09`, and reverted for that reason. The noise was never in
    the engine; it was in the panel.

- **Auto-place stops turning parts for nothing.** The annealer accepts any move whose
  cost delta is `<= 0`, and a rotation's delta is *exactly* zero for every part the cost
  function cannot tell apart turned — one on no net, or one whose courtyard is square. So
  it turned parts for no reason at all: on the `dense` fixture it turned 11, and 5 of
  those cost 0.00 to turn back. That is not free to whoever is holding the iron — every
  rotation is an orientation to get right at the bench and a polarity line in the build
  guide, and a plan reading "11 turned" describes work the tool did rather than noise it
  made. When the placer has no preference, the user's own orientation is now the one
  kept: across eight fixtures and eight seeds, **parts turned went from 46 to 30 with the
  routed cost identical to the last decimal**.
  - Applied to the WINNER only, after `_pick_best` has chosen, and checked against the
    router. Tidying every candidate *before* the choice changes the boards the router is
    shown and therefore which one wins — measured on `dense`, where doing it that way
    left the mean routed cost 31.5 → 35.9 while the best was unchanged.
  - Letting a rotation nudge the anchor at the same time — the obvious fix for a part
    that can only be turned if it also moves a hole or two — was tried and **measured
    worse**: summed mean routed cost 254.1 → 261.0 at half the rotations nudging, 260.0 at
    a quarter, 262.6 at 0.15, with ne555 losing most of it. That result is written down at
    the proposal site so nobody re-derives it.

## [0.5.0] - 2026-08-14

### Added

- **A part can be given a value** (double-click it, or `F2`). This was the one field on
  the document no human could reach: every part the window placed was created with
  `value=""` and nothing could change it afterwards, while an agent on the MCP server has
  been able to pass one to `place_component` since that server existed. `guide._bom`
  groups on exactly this field, so the tool's own build guide printed "Resistor × 4"
  where it meant "10k × 4".
  - The dialog carries **only what `component.update` carries** — reference, value, lock.
    Rotation is a command of its own, and putting it here would make one press of OK into
    two entries on the undo stack for what the user experienced as one edit.
  - The **Parts panel has a value box** as well, applied to each part as it is placed. A
    board is populated in runs — five 10k resistors, then three 100nF — and naming each
    one afterwards through its own dialog is the same work done once per part instead of
    once per run.
- **Right-click menus** on the board, the Nets panel and the findings panel. There were
  none anywhere: everything a part can be told to do lived in the menu bar at the top of a
  window a metre wide, while the part itself was under the pointer in the middle of the
  board. Every menu is built from the **same `QAction` objects the menu bar holds**, so an
  action greyed out in one place is greyed out in the other. Right-click already means
  "finish" inside a board mode, and the guards for that are described in
  `BoardView.contextMenuEvent`.
- **The window remembers its layout** — geometry, dock sizes and positions, board colour,
  the ratsnest, ruler and hatch toggles, and the preferred connection style. All of it
  reset on every launch, and the cost was paid by whoever used the tool most. The 3D panel
  deliberately still starts closed: restoring it open would build VTK's whole pipeline
  during startup to show a board nobody has looked at yet.
- **The build guide, in the window** (`Ctrl+4`). The soldering order is the thing this
  application is *for*, and the only way to see one was to export four files and go and
  find them — so the order the tool had worked out was invisible while the board was being
  designed, which is when it is worth knowing. Picking a step selects its parts on the 2D
  board, brings its holes into view, and moves the 3D assembly slider with it. Closed by
  default and rebuilt only while open, because building a guide runs DRC and LVS.
- **The export offers to open what it wrote**, rather than ending at a line in the status
  bar naming a file in a directory the user then has to go and find.
- **A language menu** (View ▸ Language). The Turkish catalogue has existed the whole time
  and could be selected only by an environment variable or a command-line flag. Applied at
  the next start, and it says so: every label is translated once as the window is built,
  and the widgets a live rebuild missed would be exactly the ones nobody would notice had
  stayed English.
- **Files can be dropped on the window** — a `.perf` to open it (through the same
  unsaved-work guard the Open menu item uses) or a `.net` to import it. Nothing happened
  before, which reads as the application refusing that kind of file rather than refusing
  drops.
- **The findings panel has a filter box**, like the parts and nets panels. A board
  mid-layout carries a hundred proximity warnings, and "show me the errors" is how anybody
  reads a list that long.

- **Copy, paste and duplicate a block of board** (`Ctrl+C` / `Ctrl+V` / `Ctrl+D`). A
  perfboard project repeats itself in a way a PCB does not — eight identical channels,
  the same RC pair at every op-amp — and until now the only way to build the second one
  was to place every part again by hand.
  - **A block is parts AND the copper between them**, placed by one new command,
    `block.place`. Two commands would put a state on the undo stack nobody chose: the
    parts down with their wiring gone, one `Ctrl+Z` from a board that looks finished and
    is not. The copper is validated against a document the new parts have *already*
    joined, which is what lets a pasted lead bend name the part it is a leg of.
  - **JSON on the system clipboard**, not a variable on the window, so a block crosses
    documents and crosses two running copies of the application — which is the "channel 1
    into that other board" case. It is readable, so a block can be pasted into a bug
    report, for the same reason the project file is diffable.
  - **A copy of R1 is not R1.** Pasted parts get fresh references counted from the board,
    and pasted copper carries **no net claim**: copper that kept `net_id` would tell LVS
    the new block is wired to a schematic that has never heard of it. Unclaimed copper is
    also the one kind rip-up and the stale-conductor cleanup both promise never to touch.
  - Three things are deliberately left behind, and each is reported rather than silent: a
    lead bend whose part was not in the selection (it would be a leg of nothing), the
    lock (a pasted part is one you are still positioning), and copper that the offset
    would have pushed off the board.
  - **Duplicate does not touch the system clipboard.** Duplicating a part is a board
    operation; it has no business throwing away what somebody copied elsewhere.

- **Stripboard is a board type this application can actually design on**, rather than a
  string the data model accepted and nothing understood. `board.type` and `strip_axis`
  have been in `model.py`, `persist.py` and the `.perf` format since the first commit, a
  `cut.add` command has existed the whole time, and **nothing read any of it**: the
  connectivity engine did not know a strip joins the holes along it, so a stripboard
  loaded as a pad-per-hole board with a decorative field set.
  - **A cut destroys the copper AT a hole** (`stripboard.py`). That is how a track is
    actually broken — a spot-face cutter or a drill bit turned by hand in the hole, which
    takes the pad with it. The alternative model, a cut *between* two holes, describes a
    knife scored across the track: a real technique, much harder to do reliably at 2.54 mm
    and much harder to inspect, and one that would leave a cut without an address in an
    application where every message is addressed.
  - **Connectivity gained a fourth rule** beside the three it has always had: on
    stripboard the BOARD joins holes, and nobody soldered those connections. Only holes
    something is soldered into take part — a strip physically joins all thirty holes in
    its row, and registering the twenty-six nobody used would put every empty pad on the
    board into a net, which is exactly what the module's existing note says not to do.
    Gated on the board type, which is why fifteen golden fixtures still reproduce byte
    for byte.
  - **The autorouter for a stripboard subtracts before it adds** (`striproute.py`). Two
    pins of different nets on one strip are shorted by the board itself, so the first
    pass is cuts and the second is links; the links go over the COMPONENT side, because
    the solder side is one sheet of parallel copper and a wire laid across it there shorts
    every strip it crosses. Pins it cannot separate — adjacent, with no hole between them
    to drill — are reported by name rather than routed around, because the fix is to move
    a part and that is the user's decision.
  - **The cuts and the links commit as one command** (`stripboard.apply`). Separately,
    one `Ctrl+Z` leaves a board cut apart with nothing linking it, or linked with nothing
    cut — which is a short across two nets, and a state nobody designed.
  - **DRC reports a pin standing in a cut hole** (`cut-track-conflict`), an error for the
    same reason `mounting-hole-conflict` is: the pad is gone, so the board cannot work
    rather than probably will not.
  - **Both renderers draw the strips**, and the 2D view marks each cut. A stripboard drawn
    as a grid of separate pads is a picture of a different board, on a view whose whole
    job is to be checked against the real one.
  - **The build guide cuts first and measures each one.** The cuts are made from the
    copper side with a drill, and once a part is over a hole there is no way back to it —
    so they are phase 0, and each gets a blocking isolation probe, because a cut that did
    not go all the way through looks exactly like one that did.
  - **Board Setup chooses the type and which way the strips run**, and Draw ▸ Cut Track
    (`X`) makes and un-makes cuts by clicking. Not done: the placer does not yet reward
    putting a net's pins on one strip, which on stripboard is most of the design — said
    here rather than left to be discovered.

- **Something now checks what the render looks like** (PLAN.md §10 asks for visual
  regression). The headless run has always produced PNGs and CI has always kept them as
  artefacts, and the only thing asserted about one was that it began with the PNG magic
  bytes — so a render that lost every pad, drew the board inside out or came out blank
  passed the build, and somebody had to open the artefact and look.
  - **Not a pixel diff.** Antialiasing and Qt's own version move individual pixels across
    platforms and this suite runs on three; a per-pixel golden would fail on macOS for
    reasons that have nothing to do with the board, and a test that fails for the wrong
    reason gets switched off. `tests/test_render_golden.py` compares the **mean colour of
    each cell of a 6 × 6 grid** against a checked-in signature: re-rendering the board
    moves it by 0.0 of 255, rendering the other face by 27.5, and rendering it with every
    part and conductor removed by 22.6 — against a tolerance of 3.
  - The first attempt measured how much of each cell was covered in ink and was nearly
    useless: a perfboard is mostly board, so losing every part moved a cell by 2.6 points
    against a tolerance of 2. That is recorded in the file, because it looked reasonable.

- **The window notices when the file changes underneath it** (PLAN.md §9.3). The project
  file is diffable and agent-friendly precisely so that a session which only *writes
  files* still works — and the window was the one participant that did not notice: the
  board on screen went quietly stale and the next save overwrote everything the agent had
  done.
  - **A window with no unsaved edits reloads itself.** One with unsaved edits does not,
    and says so instead: the file and the window have both moved, and only the person in
    front of it can say which is right. Losing somebody's work to a background event is
    the one outcome that must not happen, so **File ▸ Reload from Disk** (`F5`) is the
    manual way to take the file's version.
  - The viewport is left where it was on a reload — somebody watching an agent work is
    looking at a particular corner of the board — and a save does not trigger one, which
    would have thrown away the window's own undo history for nothing.

- **Five MCP tools for the board itself**: `set_board`, `add_mounting_hole`,
  `add_edge_connector`, `cut_track` and `remove_board_feature`. The asymmetry they fix is
  the argument for them: `get_board_info` reported mounting holes and edge connectors that
  nothing could add, and the only route to a different board size, material or type was
  `new_document`, which throws the work away. One delete covers all three kinds of feature
  because they differ only in which list the id is in. Forty-four tools now, against
  PLAN.md §2's "~25, deliberately narrow" — the cap is a decision somebody has to make on
  purpose, and `tests/test_mcp.py` still makes them make it.

- **Measure the distance between two holes** (View ▸ Measure Distance, `Ctrl+M`). It
  reports three numbers because they answer three different questions: **holes across**
  is what a footprint and the build guide are written in, **mm** is what a lead-bending
  jig and a pair of pliers are set to, and **steps** is how much solder trace it would
  take — a diagonal is two steps of copper, not 1.4, because solder crosses the 0.6 mm
  orthogonal gap and not the 1.7 mm diagonal one. The answer follows the pointer, because
  the question is usually "how far to about there". The one tool in the window that
  changes nothing.

- **Go to Part** (View ▸ Go to Part…, `Ctrl+G`) — filters on reference, value and
  footprint together and centres the view on what it finds. Which of the three somebody
  remembers depends on why they are looking: `R37` from a DRC message, `10k` from the
  schematic, `TO-220` from the pile of parts on the bench. On a dense board there was no
  way to answer "where is R37" except to read the screen until it turned up.

### Changed

- **The Turkish translation is complete.** 216 strings were wrapped in `t()` and roughly
  67 were not — including *every* tooltip and all the tree headers, so `--lang tr` gave
  Turkish menu items with English explanations under them, which is the half a user stops
  to read. `tests/test_i18n.py` now checks the direction it could not see before: a
  user-facing string never wrapped in `t()` is not a missing translation, it is not in the
  system at all, so it moved no number and nothing reported it.
  - The catalogue scanner understands **adjacent string literals** as one key, because a
    tooltip lives in the source as three quoted fragments on three lines. Without that,
    wrapping a tooltip both failed the coverage check *and* reported its own catalogue key
    as stale — which is why the tooltips stayed English.
- **The findings panel keeps your place across an edit.** It rebuilt from `clear()` on
  every command, throwing away the expanded groups and the selected row — so working
  through a rule meant re-expanding it after every attempt to fix what the rule was
  complaining about. Groups are restored **by name**, since a rule that gained a violation
  moves down the tree and one that lost its last disappears. Severity now colours the row
  too, in the colours the status bar already uses for the same counts.
- The Nets panel's **"Left" column is now "To route"**. One English key cannot carry two
  meanings in a catalogue whose keys *are* the English strings, and Board Features already
  has an edge called Left. Saying what the number counts is better English anyway.
- **Autorouting a big board is a third faster**, and the interesting part is which third.
  A 100 × 60 board with 60 parts took **6.8 s**; it takes **4.5 s**. Every golden route
  reproduces byte for byte, which is the only reason to believe the change was safe.
  - **The A\* open list was not the problem**, which was worth measuring before trading
    away the differential proof to fix it. `router.py`'s docstring has always said a
    binary heap would change tie-breaking among equal-f nodes and could silently pick a
    different equal-cost path; a profile put the search loop at under a tenth of the time.
    It stays a linear scan, now with the measurement written beside the reason.
  - **The time was in R5'.** Pricing bridging risk into the search — the thing this
    project is organised around — asked `_has_foreign_neighbour` a million times for about
    two thousand distinct questions per route, rebuilding a set of "our" nets on every one
    of them for a value that cannot change while a search runs. Both are memoised on the
    route context now.
  - **And in building `"37,12"` strings**: 15.8 million calls to `geometry.hole_key`, more
    than the search itself cost. `router.py` keys its own sets on `(col, row)` tuples.
    `hole_key` stays the one encoding for everything that crosses a module boundary —
    occupancy, connectivity, DRC — all of which have golden output that must not move.
  - Two tests pin the properties rather than a stopwatch: a timing assertion is a flaky
    test wearing a useful hat.

- **The headless run is its own module** (`ui/headless.py`). It is a program in its own
  right, it shares nothing with the window but the scene it renders, and being importable
  on its own is what lets a test call it without standing up a `MainWindow`. `main.py`
  loses 228 lines, and starting the GUI no longer imports the CLI.

- **Importing a netlist places its parts in one undo step**, which is what the code doing
  it has claimed in its own docstring since it was written. It dispatched one
  `component.place` per part, so taking back a thirty-part import took thirty presses of
  `Ctrl+Z` and every one of them left a half-imported board. It is one `block.place` now.

- **The build guide is a third of the size it was.** `dense.perf` produced a 6378 KB
  `guide.html`; the same board now produces 2070 KB, and nothing was dropped from it.
  - The step images are **JPEG rather than PNG**. They are photographs of a lit 3D scene
    — smooth shading, no flat colour, no sharp text — which is the exact content PNG
    stores worst: 135.6 KB per image against 47.0 KB at quality 82, measured on the 33
    steps of `dense.perf`. Below about quality 70 the compression starts ringing around
    the thin leader lines in the exploded shots, so 82 is where it stopped rather than at
    the smallest number that still looked fine in a thumbnail.
  - **JPEG and not WebP**, which would have been smaller again: `vtkJPEGWriter` is linked
    into VTK, while Qt's WebP writer is an image-format plugin that has to be collected
    into a PyInstaller bundle — and a missing plugin is a failure on the user's machine,
    not on ours. The guide's whole promise is that it opens anywhere, later.
  - **`guide_export` reads the media type off the bytes** instead of naming PNG in the
    data URI. It is inlined into a file with no network behind it, so a picture announced
    as the wrong type is a broken image in the one place nobody can re-fetch it — and the
    renderer's format has now changed once, which is the argument against agreeing on it
    in two places.

- **The printed guide is legible when the browser is in dark mode.** `@media print` reset
  the body colours but not the palette tokens, and browsers drop background colours when
  they print — so on a dark-mode machine every `.meta` line printed pale grey on white
  paper. The print block now redefines the whole palette. This guide is meant to be taped
  next to the board.

- **Ruff is a gate.** It reported 466 findings and did not block, which is the worst of
  both worlds: a permanently red tick is one nobody reads, so it protects nothing while
  looking like it does.
  - **389 of the 466 were two rules that are wrong for this codebase**, and they are off
    in `pyproject.toml` with the argument written at the switch. `E501` fired 235 times at
    a median of 105 characters, almost all of it prose in a comment or a message string —
    which a formatter cannot split either, and ruff's own guidance is to leave line length
    to the formatter. `RUF001`/`2`/`3` fired 154 times and 137 were the dotless **ı** in
    the Turkish catalogue: not a suspicious lookalike of `i`, but a different letter of the
    language the interface speaks. The rest were `ρ` for resistivity and `×` in "5 × 7 cm".
  - **The other 77 were fixed rather than configured away**, including a `zip` that now
    says `strict=False` where it means it, two nested `if`s that read better as one, and
    an `int(round(...))` that was rounding an int.
  - **Four were fixed by hand because the automatic fix made the code worse.** `RUF005`
    rewrites `[a] + b` as `[*a, *b]`, and on a multi-line constructor call it does that by
    inlining the whole thing onto one 200-character line — which only looked acceptable
    because `E501` had just been switched off. The same edit made by hand is an
    improvement; made by the tool it was vandalism with a green tick.
  - **`UP040` would have broken the application silently, and the suite caught it.**
    Rewriting `X: TypeAlias = Literal[...]` as `type X = Literal[...]` is correct for 32
    of the 33 aliases and wrong for the four that are read at RUN time: `get_args` returns
    an empty tuple for a PEP 695 alias, so `BoardMaterial`, `BodyArchetype` and
    `ConductorKind` stopped listing anything and three completeness tests started
    asserting that an empty set equals an empty set. `RoutingStyle` was worse — MCP
    validates an agent's requested style against `get_args(RoutingStyle)`, so every style
    would have been refused, by a check that raises nothing. All four keep the old
    spelling with the reason written above them.
  - **The linter is pinned to a minor version.** An unbounded dependency took this project
    apart twice this morning; a linter is the same hazard in a smaller way, since rules
    arrive with releases and a gate that fails on a tree nobody touched is a gate people
    switch off.
  - **`ruff format` still is not adopted, and making the linter a gate nearly adopted it
    by accident.** The same job also ran `ruff format --check`, which had been failing
    quietly under `continue-on-error` for as long as it existed; removing that flag turned
    "41 files would be reformatted" into a failed build, deciding the exact question the
    comment beside it says is not being decided. It reports now and does not block. The
    number is worth watching; it is not worth watching from behind a red tick that means
    something else.

- **CI runs all three platforms on every push**, which is what `ci.yml`'s own condition
  said to do on going public: standard runners are free on public repositories, so the
  metered-minutes trade-off it encoded stopped applying. The first full matrix that ran
  under the old rule is the argument for not restricting it again — it found a VTK abort
  on Windows and two footprint goldens off by a ULP on macOS arm64, neither visible on
  Linux, both an ordinary edit away from coming back. "Seen before a release" turns out
  to be a weak property when a release is where they were seen.

### Fixed

- **Exporting a build guide on a machine with no OpenGL killed the application**, and the
  code that was supposed to prevent it could not run. Both export paths wrap the render in
  `except Exception` and fall back to a picture-less guide — the promise that a guide
  without illustrations is still a complete guide — but VTK does not raise when there is
  no context behind an offscreen window. It ends the process. On a virtual machine, a
  remote desktop session or an old driver, **File ▸ Export Build Guide** took the window
  down with every unsaved edit in it, and the handler for exactly that case never
  executed. 0.4.0 shipped with this; the release notes named it as unfixed.
  - **A crash cannot be caught where it happens, so it is spent where it costs nothing.**
    `view3d.offscreen_gl_available()` opens a 16 × 16 offscreen window in a **child
    process** and reports by exit status. One spawn per run, cached — the answer cannot
    change while the application is open — and about 0.9 s.
  - **A frozen build has no separate Python to spawn, so it probes by running itself.**
    `sys.executable` is the application, and `--probe-offscreen-gl` is answered before Qt
    is touched. That is not a documented option and is not meant to be used by hand; it
    exists because the installed build is exactly where this has to keep working, and the
    Windows release job now runs it that way on a runner with no GPU.
  - **Three consumers, one answer**: the MCP `generate_guide`, the GUI's export, and
    `--headless`, which now prints *"3D SKIPPED: no offscreen GL context on this machine"*
    and writes everything else rather than stopping at the first stage that needs a
    graphics driver. The suite asks the same function, so a test skips exactly where the
    application would have declined to render — rather than the two disagreeing about what
    the machine can do.
  - Both CI workflows are held to the exit status again. The Windows steps had been
    allowed to fail while this was outstanding, which meant the one runner most likely to
    show a real Windows fault was the one whose result was being ignored.

- **A machine-wide installer was putting its shortcuts in one person's profile.** Found
  by installing v0.4.0 on a real desktop, which is a thing that had never been done: CI
  unpacks the bundle and asks the binary its version, and the installer around it had only
  ever been *built*. It installs into `$PROGRAMFILES64` under `RequestExecutionLevel
  admin`, and `$SMPROGRAMS` / `$DESKTOP` default to the **current** user — which under
  elevation is whoever answered the UAC prompt. Install for a standard user from an
  administrator account and every shortcut lands in the administrator's profile and none
  in the profile of the person who will use the program. `SetShellVarContext all`, in the
  install and uninstall sections both.
  - **And its uninstall entry was in the 32-bit registry.** `makensis` produces a 32-bit
    installer, so `HKLM\Software` is redirected through WOW64: a 64-bit application in
    `Program Files` was registering itself where 32-bit applications live. Add/Remove
    Programs reads both views so it still appeared, which is why nothing looked wrong —
    but every inventory tool and script that reads the 64-bit view saw nothing.
    `SetRegView 64`.
  - **Both fixes have to look backwards.** Every v0.4.0 install in the world recorded
    itself in the 32-bit view with per-user shortcuts, so the next installer searches both
    registry views for a previous version and clears both shortcut contexts. Without that
    it would find nothing to remove and unpack itself over the bundle it is replacing —
    the exact failure that block exists to prevent.
  - What did work, end to end and on the first try: silent install in 21 s, 522 MB across
    1008 files, the `.perf` association (double-clicking a board opened it in the installed
    build), the Turkish Start-menu entries the machine's locale selected, the full headless
    pipeline out of `Program Files` including 29 rendered step images, and a silent
    uninstall that left nothing behind — no files, no registry keys, no shortcuts.

- **The release ritual's own last step failed the tests that enforce it.**
  [docs/RELEASING.md](./docs/RELEASING.md) step 4 opens the next cycle by putting the
  `.devN` suffix back on and leaving an **empty** `## [Unreleased]` heading — and
  `test_development_builds_have_an_open_unreleased_section` then failed the whole suite,
  because it also demanded that section have entries in it. Nothing has accumulated
  towards a version opened a minute ago; that is what opening one means. The
  contradiction stood through three versions because reaching it requires finishing a
  release, and 0.4.0 is the first release this project has actually completed. The test
  now checks the half that cannot be satisfied by forgetting to write anything down —
  that a development version is not describing a version which already shipped — and the
  release side still refuses a closed section with no entries, and a released build whose
  section is not the newest.

## [0.4.0] - 2026-08-14

### Added

- **Installers, on all three platforms, from one PyInstaller spec.** Pushing a `v*` tag
  builds a Windows installer, a Linux AppImage and a macOS disk image, smoke-tests each
  one by unpacking it and asking the binary inside what version it is, and attaches them
  to the release. Nothing has been tagged yet, so nothing has been published.
  - **The tag is checked against the source rather than trusted.** A `v0.4.0` tag on a
    tree that still says `0.4.0.dev0` publishes an installer that disagrees with its own
    file properties, and nothing downstream would notice — so the workflow refuses that,
    refuses a version that still carries a `.devN` suffix, and refuses one with no
    `CHANGELOG.md` section. The release notes are that section, so the two cannot say
    different things about what shipped.
  - **Nothing is code-signed**, and the release notes say so along with the click-through
    each platform needs. A Windows EV certificate is ~$300/year and Apple notarization
    $99/year (PLAN.md §12). The macOS bundle is signed *ad-hoc*, which is not a trust
    decision — it is the minimum Apple silicon will execute at all.
  - Ubuntu 22.04 and macOS 15 are pinned rather than `-latest`: a PyInstaller bundle
    carries Python and Qt but links against the host's glibc, and glibc is forward
    compatible only, so a 24.04 build would require 2.39 and rule out Debian 12 and every
    enterprise distribution still in service.
  - The Windows installer speaks **English and Turkish**, because the application does,
    and an installer that could only speak English would be the one part of the product
    that does not. All three platforms claim `.perf`, so double-clicking a board opens it.

- **Four example circuits, each shipped as both the netlist and the finished board.**
  There was one netlist and no board. `examples/` now carries the NE555 astable, an LM317
  adjustable supply, a one-transistor guitar booster and an Arduino I/O shield — the
  `.net` a schematic tool exports, and the `.perf` that importing, placing and routing it
  produces.
  - Chosen for what they make the tool say rather than for variety. The LM317 is a
    **TO-220**, so `heat-proximity` has a hot part to measure from, and that board carries
    the set's one `solder-trace-proximity` warning — R5' doing its job and becoming a
    measurement checkpoint. The booster is on **FR-2**, so the guide drops the iron 30 °C
    and `pad-lifting-risk` can fire at all. The shield is two headers, which is where lead
    bends and short traces do nearly all the work.
  - `tests/test_examples.py` asserts on every commit that all four load without warnings,
    round-trip byte-identically, match their schematics under LVS and carry no DRC error.
    A broken example on the front page is worse than no example.

- **An application icon**, drawn in code by `tools/make_assets.py` and committed as a
  `.png` and an `.ico` — because unlike the toolbar, an installer and a `.desktop` entry
  need real files to point at. Every colour comes from `ui/boardcolors`, so the mark is
  the same green and the same gold as the board in the editor.

- **A demo animation of the build order** (`tools/make_demo.py`), generated by playing the
  guide back through `document_at_step` — the same function behind the 3D panel's assembly
  slider, so it cannot show an order the guide does not actually prescribe.

- **Copper on the face you are not looking at is hatched.** `View ▸ Hatch Copper on the Far
  Side`, on by default. The board is opaque: a solder-side trace drawn solid while you are
  looking at the component side says *this is in front of you*, which is exactly the
  misreading `_paint_body_shadow` already exists to prevent for part bodies — and the one
  that gets a board soldered on the wrong face. A conductor and a part now say "I am on the
  other side" the same way, in the one visual word this application already had for it.
  - **Stroked into a fillable shape rather than dashed.** A dash already means a top jumper
    in `CONDUCTOR_STYLE`, and giving one mark two meanings costs more than it saves. The
    outline around the hatch is what keeps a run traceable end to end at low zoom, where a
    0.9 mm trace is a few pixels wide and hatching alone cannot read.
  - **The joints stay solid.** Where a conductor is soldered down does not change with the
    face you look from — the hole goes through the board — and it is what someone counts
    pads against while tracing a run. A test pins that a hatched trace still marks every
    hole it contacts.
  - The hatch brush carries the inverse of the painter transform, so it holds its spacing on
    screen while the board zooms instead of turning solid zoomed in and vanishing zoomed out
    — the same correction the body shadow needs, for the same reason.
  - Off is a real option, not a concession: someone tracing a dense solder side may simply
    want to see it plainly. On is the default because the default has to be the reading that
    cannot mislead.

- **The router can try every style and keep the best.** `Route ▸ Preferred Connection ▸
  Try each and keep the best`, `style: "best"` over MCP. Picking a routing style meant
  guessing — before seeing a single route — whether this particular board comes out better
  with solder or with wire. Planning is pure and a plan is cheap, so the tool can stop
  guessing and measure: it routes the board once per style and keeps the one that is least
  work to build. Costs about two ordinary routes (607 ms against 273 ms on the NE555
  fixture), because `balanced`'s own rip-up passes are the slow ones.
  - **It does not compare costs, and that is the whole trick.** Each style's plan carries a
    `total_cost` quoted in that style's own currency: the `wire` table prices a solder step
    at 4 and an insulated wire at 6, so its plans are cheap *by its own definition of
    cheap*, and `min(total_cost)` would pick wire on every board ever. The comparison is on
    physical facts instead — traces, wires, millimetres of wire, holes at R5' bridging
    risk — which mean the same thing whichever table produced them. A test asserts
    `score_plan` never mentions `total_cost`.
  - **An unrouted connection is a gate, not a term.** A plan that leaves one can never win
    on being tidier elsewhere, however large the gap: PLAN.md §13 names "it routed most of
    it and left four connections" as the trap every previous perfboard autorouter fell into.
  - **A wire costs more to build than a trace**, and by a fixed amount before a single
    millimetre exists — measure, cut, strip, tin, dress, solder twice. The first version of
    this scoring priced them the same, which undercharges the one primitive with real
    preparation behind it and makes every comparison meaningless.
  - **Every loser is kept and reported**, with its measurements, in the status bar's tooltip
    and in `comparison` over MCP. The winner is chosen on an exchange rate between wires and
    bridging risk that the user is entitled to disagree with, and they cannot disagree with
    numbers they were never shown — so all four styles stay pickable by hand.
  - Ties fall to the earlier style tried, so `balanced` keeps its place and the sweep
    reduces to today's behaviour when nothing beats it. On both the NE555 and dense
    fixtures, nothing does.
  - Headless prints the whole table, so a change to any cost table shows up in CI as a
    different winner rather than as a silently different board.

- **Resistors wear their colour code, in both views.** A resistor is the commonest part on
  almost any board and every one of them was an anonymous beige blob — so *"is the 10k in
  the right place"* could not be answered by looking, which is the one job the 3D view has.
  The bands are decoded from `ComponentInstance.value`, which is already in the document,
  so nothing new is stored and the bands cannot disagree with the netlist.
  - **It never guesses.** A wrong band is worse than no band: somebody would read it and
    fit the wrong part. The parser understands the way schematics actually write a
    resistance — `470`, `470R`, `4R7`, `10k`, `4k7`, `2.2k`, `2M2`, `1kΩ`, where the unit
    letter stands in for the decimal point — and returns nothing at all for anything else.
    `100nF`, `10uH`, `2A`, `NE555` and dense.perf's placeholder `v12` all decode to no
    bands rather than to a plausible resistance, and that is the load-bearing test.
  - **A diode is not a resistor**, though they share the `axial-cylinder` archetype. A
    polarized axial body keeps its cathode stripe and is never banded, the same split
    `style_for` already makes.
  - Both views read `bodies.resistor_bands`, so the editor and the 3D view cannot print
    different parts. The library icons still draw three generic bands, and correctly: that
    list shows footprints, which have no value to decode — it is a picture of *a resistor*,
    where the board is a picture of *a 10k*.

- **Parts are lit like the material they are made of.** `BodyStyle.metallic` and
  `BodyStyle.lens` were documented as shading hints for both renderers and read by almost
  neither — the HC-49 crystal, the one part in the registry that is literally a metal can,
  hardcoded its own metal shading while carrying the flag that says so, and no LED was ever
  lit as a lens. Two sources of truth for one fact, in the module that exists to prevent
  exactly that. `bodies.surface_for` is now the single answer, and both views ask it.
  - The 2D view gained a highlight swept across the short axis of a body, because that is
    the direction a cylinder curves in. A flat fill is what made every part read as a
    sticker printed on the board rather than an object standing on it.
  - A LED now looks lit, a crystal can looks like metal, and a DIP still looks like matte
    plastic — which is most of what makes a rendered board look like a board.

- **A solder run reads as copper again.** Each joint gained a thin darker rim and, close
  in, a small highlight off its top-left. At 2.54 mm pitch a run of beads brighter than the
  trace beneath them merged into one lumpy caterpillar, so the eye read a necklace rather
  than a length of copper soldered down at every pad. Each joint is individually countable
  now — which is what somebody tracing the run against the real board is doing — while the
  trace itself carries the line. The bead-per-pad distinction is untouched: it still comes
  from `model.contacts_every_path_hole`, and a wire still gets a fillet at its two ends and
  nothing in between.

- **Two clicks join two pins.** `Net ▸ Connect Two Pins` (C, and the first button on the
  toolbar) is the netlist reduced to what a person is actually doing: pointing at two legs
  and saying *those* go together. Click one pin, click another, and they end up on the
  same net — an existing one if either pin is already on it, or a new one named `N1` for
  you if neither is. Declaring a net, filling it and then routing it is still the honest
  model, and it was also four steps deep in two menus before a single pin could be joined
  to anything.
  - It produces exactly the documents the long way round does: one command per pair, on
    the same bus, undoing one pair at a time.
  - **Both pins already on different nets is refused**, naming both. Merging two nets is a
    change to the circuit, and not one a two-click tool may make on its own.
  - The tool stays armed, because a board is a list of connections rather than one, and a
    refused pair clears the half-made connection — otherwise the next click joins something
    the user has stopped thinking about.

- **The parts library has pictures of the parts.** Sixty-one footprints were sixty-one
  lines of grey text, which is a poor way to answer the question people bring to that
  panel: *which of these is the fat blue one with a stripe*. Every row now carries a small
  colour drawing — a beige resistor with bands, a dark blue electrolytic with its polarity
  stripe, a black DIP with its pin-1 notch, a red LED with one lead longer than the other.
  - **The colours are not chosen here.** Every one comes from `bodies.style_for`, the same
    fill, edge and accent the 2D board and the 3D view draw that part with, so picking a
    part from the list and finding it on the board is recognition rather than reading. A
    second palette would have drifted from the first the moment either was touched.
  - The view is per archetype and deliberately not the board's top-down one: a resistor is
    recognised side-on by its bands and a DIP from above by its notch. This list is about
    the part in the drawer; the board beside it is what shows the footprint.
  - A test fails if the model gains an archetype this cannot draw, because a blank row in
    a list of pictures reads as breakage rather than as a missing icon.

- **A toolbar with pictures on it.** Every tool is now on the bar — connect, new net, all
  five conductor kinds, auto-place, autoroute, rotate, mirror, delete, flip, ratsnest, 3D,
  fit — each with an icon and a short label under it. The old bar was eleven identical grey
  rectangles of text that had to be read left to right every time, which is how the tools
  ended up being hunted for in the menus, which is the thing a toolbar exists to prevent.
  - **The icons are drawn in code** (`ui/icons.py`), from the same palette as the rest of
    the chrome, so they cannot fall out of step with the window and there is no asset
    directory, resource system or licence to track.
  - **The four conductor icons share one drawing** and differ only in what runs between the
    two pads — a solid bar for solder, a thin line for bare wire, a sleeved line for
    insulated, an arc that lifts off the board for a top jumper. That difference is what
    this application is about, so it is what the icons are built around.
  - Buttons carry a short label and menus keep the full wording (Qt draws `iconText` on a
    toolbar and `text` in a menu), without which sixteen tools at menu length run off the
    end of a 1600 px window and half of them end up behind an overflow arrow.

- **The window now says what state it is in.** Placing, drawing and picking pins each arm
  a mode in which a click means something other than what it usually means, and the only
  place any of them said so was the status bar — the bottom edge of a window a metre wide,
  while the cursor is in the middle of the board. A mode nobody can see is
  indistinguishable from an application that has stopped responding to clicks.
  - **A banner over the board** names the armed mode and both ways out of it ("Adding pins
    to GND: U1.8, C2.2 · Enter or right-click finishes, Esc cancels"). It is derived from
    the scene on every change rather than remembered, so it cannot disagree with what the
    next click will actually do, and it is transparent to the mouse — an overlay that ate
    the click it was describing would be worse than no overlay.
  - **An empty board says what to do with itself.** The application opens on a blank
    5 × 7, and every route, check and export needs something on it first; a blank viewport
    under a full menu bar is the one screen where a person cannot tell a tool that is ready
    from one that is broken. It withdraws as soon as a part lands or a mode is armed.

- **Recent files, and a toolbar you can save and undo from.** `File ▸ Open Recent` keeps
  the last eight boards across runs, skipping any that have since moved — a perfboard
  project is worked on across evenings, and hunting the same file out of a tree every time
  was friction the application was adding for no reason. Save, Undo and Redo joined the
  toolbar, and **undo and redo now grey out** when there is nothing behind or ahead: the
  bus has always known, the window simply never asked, so an undo at the bottom of the
  stack looked exactly like one that worked. Undo's tooltip names the command it would
  take back.

- **`Help ▸ Keyboard Shortcuts…` (F1), read off the real menus.** A hand-kept shortcut card
  goes stale the first time an action moves, and a stale card teaches something that no
  longer works, so this one is generated from the menus themselves and cannot describe a
  binding the application does not have. It also lists the board gestures — middle-drag to
  pan, right-click to finish a run, arrows to nudge a part a hole at a time — which are on
  no menu at all and were previously discoverable only by reading the source. A test fails
  if two actions ever claim one binding.

- **Panel ergonomics.** A filter box on the Nets panel that matches pins as well as names,
  because half the time the question is "what is U1.3 on" rather than "where is GND"; full
  part names in a tooltip where the dock elides them; a wider parts column and a shorter
  DRC panel, which was opening a quarter of the window tall to show four rows of a clean
  board.

- **A netlist you can write yourself, without KiCad.** `netlist.import` was the only way
  a net could ever enter a document, which quietly made a schematic capture package a
  prerequisite for the whole tool: with no net there is no ratsnest, and so nothing for
  autoroute to route, nothing for LVS to check and no continuity tests in the build
  guide. Nobody opens KiCad to wire four parts on a scrap of perfboard. Five commands —
  `net.add`, `net.update`, `net.delete`, `net.connect`, `net.disconnect` — put the same
  intent in by hand, and import still replaces the netlist wholesale because that is
  what re-exporting a schematic means.
  - **You click the pins, rather than typing their names.** "New Net…" asks for a name
    and a class and then arms a mode over the board: every click adds the pin in that
    hole, right-click or Enter finishes, and the whole session lands on the history as
    one `net.connect`. On a perfboard a pin *is* a hole you can point at, so a dialog
    listing "U1.8, U1.7, C2.2" would ask the user to do a translation the board is
    already doing for them. Every refusal the command would make is made per click
    instead — an empty hole, a pin already listed, a pin another net holds — because a
    session that collects five pins and then bounces the batch for something done four
    clicks ago is worse than no help at all.
  - **The Nets panel became an editor.** Each net now lists its pins as rows underneath
    it, which is what makes a pin selectable and so removable; expansion survives the
    rebuild that follows every command, so a net opened to take a pin off does not shut
    as the pin comes off.
  - **A pin belongs to exactly one net**, so claiming one another net holds is refused
    and the message names the holder, rather than the pin being silently moved out of a
    net the command never mentioned. A pin naming a part that is not on the board yet is
    *allowed*: declaring the circuit and then placing what it asks for is a real order of
    work, it is what importing a netlist does, and the ratsnest already reports those as
    unresolved pins.
  - **Deleting a net leaves its copper alone** and releases the `net_id` claim on it in
    the same step — a reference to a net that is gone is exactly what commands exist to
    prevent. That copper becomes indistinguishable from hand-drawn work, which is
    precisely what re-route and the stale-conductor sweep both promise never to touch:
    with the intent gone there is nothing left to route it against.
  - **`current_a` and `voltage_v` finally have a way in.** No netlist format carries
    them, so DRC's current-capacity rule, its creepage rule and the wire gauge on the
    build guide's cut list have been silent since they were written. The net dialog and
    `update_net` are their only route into a document.
  - **Five MCP tools** (`create_net`, `connect_pins`, `disconnect_pins`, `update_net`,
    `delete_net`), which is the largest single addition the server has taken and is
    argued at length in its module docstring: an agent could place parts, draw copper and
    route, but could not state the intent all three are measured against.

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

- **A routing style is now a commitment, not a weighting.** Picking `Solder trace where
  possible` used to mean "solder where solder happens to be cheapest", and the difference
  showed: on the NE555 fixture the default table turned a clean five-pad trace into a
  10 mm bare wire because *one* pad sat next to another net. `RouterOptions.prefer` makes
  the menu item mean what it says — every strategy in the family the builder committed to
  outranks every strategy outside it, whatever the two cost, and cost only decides within
  the family. Wire is reached when a trace physically cannot make the connection, not when
  it scores badly.
  - **Two numbers in the default cost table are why this was needed**, and they are worth
    writing down because they are not obvious: `proximity_risk` is 12 a hole while
    `bare_wire_fixed` is 8, so a single risky pad costs more than an entire wire; and
    `bare_wire_per_mm` at 0.15 works out to 0.38 per pad against `solder_trace_step`'s
    1.0, so wire is 2.6× cheaper per unit distance and wins every long run outright. The
    table is unchanged — the commitment sits above it — so all the golden routes stand.
  - NE555 with `solder`: all 14 connections are traces (7 plain, 6 hopped over a crossing,
    1 spined) and not one is a wire. Nothing is left unrouted, and LVS and DRC still pass
    for every style.
  - **A hopped trace counts as solder.** It is a solder run with a two-hole jumper where it
    had to cross something; classing it as wire would make a preference for solder reject
    the one mechanism that gets solder past an obstacle, leaving the whole connection to be
    a wire — more wire, not less.
  - **A rail is a solder concept**, so committing to wire no longer comes back with solder
    rails in it. This is the one place a commitment has to be honoured outside
    `route_connection`'s candidate sort, because `_rail_net` reaches past `result.best` to
    pick a strategy that contacts every pad it passes.
  - `balanced` alone makes no commitment, and that is what balanced means. It is also what
    every golden route is produced with, so that branch stays a no-op.

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

- **The MCP server did not import at all against the current SDK.** `pyproject` asked for
  `mcp>=1.0` with no upper bound, so a fresh install resolved to `mcp` 2.0.0 — which
  removes `mcp.server.fastmcp`, the decorator API every one of the 39 tools is bound with.
  `python -m perfstudio.mcp` died on the import. Capped at `<2`: 1.29.0 still ships
  fastmcp and deprecates nothing.
  - **A development machine could not have noticed.** `pip install -e ".[mcp]"` leaves an
    already-satisfied requirement alone, so a tree that installed 1.x weeks ago keeps it
    and stays green — while every CI runner, every packaging job and every new user starts
    from nothing and gets 2.0.0. The local suite and a fresh clone were testing two
    different dependency sets, which is exactly the failure an unbounded range invites.
  - Lifting the cap means porting the tool surface to the 2.x API. That is a change with
    its own verification attached, not a version bump, so it is not being done on the way
    out of the door.

- **No installer could be built on any platform, and the reason was older than the SDK
  break above.** `perfstudio.spec` collects the MCP package with
  `collect_submodules("mcp")`, which *imports* every module it walks — including
  `mcp.cli`, a Typer front end that does `print(...); sys.exit(1)` at import time when
  typer is absent. So the collecting child process exits, PyInstaller reports *"Child
  process call to _collect_submodules() failed"*, and all three bundles die before a byte
  is packed. `mcp.cli` is excluded now; nothing in this application invokes the SDK's
  command line, and the 19 `fastmcp` modules that matter are untouched.
  - This is present in `mcp` 1.x as well as 2.x, so capping the version did not fix it
    and would not have. It built on the machine it was written on because that machine
    happened to have typer pulled in by something unrelated — the same class of fault as
    the cap above, found the same way, and the reason both are in this release rather
    than in the first bug report from somebody who downloaded nothing.

- **The macOS bundle could not be built at all**, on a line that has never run anywhere
  else. `perfstudio.spec` builds the `.icns` by scaling the mark to each size Finder
  wants, and passed the aspect and transformation modes as the bare integers `1` and `1`
  with the enum names in a comment beside them. PySide6 6.10 refuses an int where an enum
  is declared — *"QImage.scaled called with wrong argument values"* — and that branch runs
  on macOS alone, so the machine this was written on could not have executed it. The enums
  are passed by name now, which is also what the comment said all along.

- **Two footprints failed their golden comparison on macOS arm64, and the bound was
  measuring at the wrong scale.** A circle vertex is `centre + radius * cos(theta)`, and
  the ULP bound the test allows for a trig disagreement was counted on the vertex — which
  is right until that addition cancels, and on a circle it cancels somewhere by
  construction. `led-3mm` vertex 8 is `1.27 + (-1.385)`: one ULP at the scale the
  arithmetic is done at is **sixteen** ULPs at the scale of the 0.115 that survives it.
  The observed failures were exactly 16 ULPs there and 4 on `c-elec-d10-p3`, matching each
  vertex's own cancellation factor to the digit — a second libm disagreeing by the
  smallest amount a libm can, not a formula difference. The bound is now applied at the
  scale the terms are added at, where the error is actually made, and it still rejects a
  divergence of a nanometre.

- **The suite aborted rather than failed on a machine with no OpenGL.** VTK does not
  decline when there is no context behind an offscreen window — it takes the interpreter
  down, so on GitHub's Windows runners `win.Render()` in `render_step_images` ended the
  pytest process with an access violation and every test after it was not reported at all.
  `tests/test_gl.py` now asks the question in a child process, where a crash is an answer
  rather than the end of the session, and the five tests that put a board through VTK
  skip when it says no. Qt's offscreen platform plugin is not a GL context, which is the
  same reason the Linux job runs under xvfb.
  - **Which five is not left to whoever remembers.** Marking them by hand found three and
    missed two, at a full CI round each, because a test reaches the renderer through
    `on_export_guide` or `generate_guide(directory)` without naming it. A test now reads
    the sources and fails if a test function calls one of those and is not marked — worth
    a static check rather than a convention, since the cost of missing one is not a red
    test but a run that stops reporting partway through.
  - The frozen bundle's smoke test on that runner is no longer held to its exit status
    either, and the reason is written into both workflows. Everything the check exists for
    still happens before the 3D stage — the app starts, opens a document, renders 2D,
    writes the 1:1 PDF, runs DRC and LVS — and a missing `out_2d.png` still fails the job.
  - **What this does not fix**: `generate_guide` catches an exception from the render and
    writes a picture-less guide, which is the right behaviour and is unreachable when the
    failure is an abort rather than an exception. Surviving that means rendering in a
    subprocess, which is a change to the application rather than to its tests.

- **Escape did not cancel placing a part.** Reported. It was bound to the Draw menu's stop
  entry, which cancelled drawing, pin-picking and connecting — and not placement. Worse,
  being a *window* shortcut it fires before the board scene sees the key at all, so the
  scene's own Escape handling for placement was unreachable in the running application:
  a part armed from the parts list could not be cancelled from the keyboard by any route,
  while the hint under that very list said "Esc cancels" the whole time. Leaving a mode is
  now one method on the scene that disarms all four, called by both the shortcut and the
  key handler, so the two cannot cancel different sets of things. The menu entry says what
  it now does — "Stop the Current Tool" rather than "Stop Drawing". Four regression tests
  press a real Escape from the focus a user actually has, because a test that called the
  handler directly would have passed against the broken build.

- **The menus could be destroyed out from under the menu bar.** `QMenuBar.addMenu` hands
  PySide a `QMenu` that Python believes it owns, and every menu in this window was held
  only by a local variable inside the builder — so a garbage collection was free to delete
  the real menu and leave the bar holding an action that pointed at freed memory. It
  survived this long because nothing had ever walked the menus after building them; the
  shortcut card is the first thing that does, and it found a destroyed `QMenu` on its
  first run. Every menu is now referenced by the window, and a test collects garbage and
  checks they are all still there.

- **Quit had no working shortcut on Windows.** `QKeySequence.StandardKey.Quit` resolves
  there to a key almost no keyboard has (it reports itself as "Exit"), so the binding was
  effectively absent — and the new shortcut card printed it, which is how it was noticed.
  It is Ctrl+Q now, which Qt maps to Cmd+Q on macOS.

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

[Unreleased]: https://github.com/medinstech/perfstudio/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/medinstech/perfstudio/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/medinstech/perfstudio/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/medinstech/perfstudio/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/medinstech/perfstudio/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/medinstech/perfstudio/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/medinstech/perfstudio/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/medinstech/perfstudio/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/medinstech/perfstudio/compare/e36ac8c...v0.4.0
[0.3.0]: https://github.com/medinstech/perfstudio/compare/e66e3f8...e36ac8c
[0.2.0]: https://github.com/medinstech/perfstudio/compare/11cb8af...e66e3f8
[0.1.0]: https://github.com/medinstech/perfstudio/compare/2c7daa6...11cb8af
