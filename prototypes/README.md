# prototypes

Spikes, kept because they are the evidence behind a decision — not because anything
imports them. Nothing here is on the import path, in the test suite, or in a wheel.

## `qt/`

The spike that chose the UI stack, before `src/perfstudio/ui/` existed. Its own docstring
says what it was for: whether `QGraphicsView` feels right for a grid CAD editor or is
something to fight, whether VTK carries the 3D requirement on ordinary hardware, and
whether Qt really produces a true 1:1 printable sheet.

It answers those by drawing a fixed `.perf` the TypeScript engine in `packages/` produced,
through a read-only `board_model.py` that deliberately ports no engine logic —
`make_fixture.mjs` is how that board was generated and `footprints.json` is the shape data
it needed, both from a time before `footprints.py` was written.

The answer was yes three times, and the shipped application is the rewrite of that answer
rather than this code. The difference is measured: this `PadGridItem` draws every pad with
`drawEllipse`, which is the obvious way and cost 124 ms a frame on a 6000-hole board. The
real one blits a single pre-rasterised pad pixmap per hole, and says so at the call site.

---

Both this and `tools/bench-3d` are marked `linguist-vendored` in `.gitattributes`, so they
stay fully readable in a diff but do not claim to be what this project is written in.
