# bench-3d — M0 webview risk-reduction harness

> **Historical. The architecture this measures was not taken.** PerfStudio ships as a
> PySide6 (Qt 6) desktop application with a VTK viewport, not as Tauri and a webview, so
> nothing below describes how the application is built today. It is kept because it is
> the measurement the decision was made on, and it still runs. See `PLAN.md` §11 M0.


PerfStudio ships as a Tauri v2 desktop app, which renders through the OS's
own webview: WebView2 (Chromium) on Windows, WKWebView (WebKit) on macOS, and
WebKitGTK on Linux. The open question this tool answers is whether
WebKitGTK's WebGL implementation can carry PerfStudio's 3D board scene. If it
can't, PerfStudio falls back to Electron on Linux (or everywhere). See
`PLAN.md` §8.3/§8.4 and §11 (milestone M0).

This is a standalone Vite + three.js page — no dependency on the rest of the
monorepo — so it is trivially runnable on any machine a tester has at hand.

## Running it

From the repo root:

```
pnpm --filter @perfstudio/bench-3d dev
```

This starts a Vite dev server (default `http://localhost:5173/`). Open that
URL in the browser listed for your platform below, **not** whatever your
default browser happens to be — the whole point is to exercise the same
rendering engine Tauri will actually use.

| Platform | Open the page in | Why |
|---|---|---|
| Windows | **Microsoft Edge** | WebView2 is Chromium; Edge is the closest match without embedding Tauri itself. |
| macOS | **Safari** | WKWebView is WebKit; Safari is WebKit. |
| Linux | **Epiphany / GNOME Web** (or another WebKitGTK browser) | Tauri on Linux embeds WebKitGTK. **Do not test in Chrome/Chromium on Linux** — that's the Blink engine, a different WebGL stack, and it will report a misleadingly good result that Tauri's actual webview won't reproduce. Install with `sudo apt install epiphany-browser` (Debian/Ubuntu) or your distro's equivalent. |

Scene size is controlled by URL query params, all optional:

```
http://localhost:5173/?cols=100&rows=60&components=60&wires=200&traces=40
```

The defaults above are the ones to report against; they're a deliberately
worst-case-ish board (100×60 holes = 6000 holes, 60 populated components, 200
point-to-point wires, 40 solder traces on the underside).

The scene is generated with a PRNG seeded from the params themselves, so the
same URL always produces the identical layout — runs are comparable across
platforms and across repeated trials.

## Using the page

- **Drag** to orbit, **scroll/wheel** to zoom.
- **F** flips the board — a real 180° rotation of the model (not just a
  camera move), so the solder side is genuinely facing you afterward.
- The HUD (top-left, always on) shows rolling 1s FPS, instantaneous frame
  time, `renderer.info` draw calls/triangles/geometries/textures, and the
  active scene params.
- **Run 10s benchmark** snaps the camera to a fixed, reproducible framing and
  spins it through one full revolution over 10 seconds (this is why the
  benchmark takes over the camera and ignores mouse input for that window —
  reproducibility requires everyone's benchmark to render the same frames).
  It records every frame time, then prints a summary both into the on-page
  results panel and as a single-line JSON object to `console.log`:

  ```
  { userAgent, cols, rows, components, wires, traces, frames,
    fps_mean, fps_p1_low, frame_ms_mean, frame_ms_p50, frame_ms_p95,
    frame_ms_max, drawCalls, triangles }
  ```

  `fps_p1_low` is the mean FPS of the slowest 1% of frames — the metric that
  actually correlates with perceived stutter, since a fine-looking average
  can hide regular hitches.

  Use the **Copy JSON** button to grab the exact text (works even where the
  async Clipboard API is restricted) and paste it back into the M0 tracking
  issue, tagged with platform + browser, e.g.:

  `Linux / Epiphany 45 / WebKitGTK 2.44 → { ... }`

## Pass / fail bar

**Proposed pass bar (default scene, tested in the platform's real-webview
browser from the table above):**

- `fps_mean >= 30` **and**
- `fps_p1_low >= 20` (equivalently, `frame_ms_p95 <= 50`)

**Fail** (triggers the Electron-fallback decision for that platform,
Linux/WebKitGTK in particular per PLAN.md §11's M0 exit criterion):

- `fps_mean < 20` **or** `fps_p1_low < 10`

Anything strictly between those two bands is a judgment call for the team,
not an automatic pass or fail.

**Why these numbers:** 30fps mean is the long-standing floor for 3D camera
manipulation feeling connected to the mouse rather than laggy — below it,
orbiting a board to inspect it (the primary use of the 3D view per PLAN.md
§8.4) stops feeling direct. The `fps_p1_low >= 20` guard exists because a
good *average* can still contain regular sub-20fps hitches (GC pauses,
instance-buffer uploads) that are exactly what a user perceives as "this app
feels janky" even when the headline FPS number looks fine — 1% lows are what
actually make a UI feel bad.

## Assumptions made

- 1 three.js unit = 1 mm throughout.
- Pad geometry: 2.0mm outer diameter, 0.9mm drill, rendered as a flat
  10-segment ring — `model.ts`'s `Board.padDiameter`/`drillDiameter` don't
  fix a default, so plausible standard perfboard values were used.
- Through-hole pads are instanced on **both** faces of the board
  (`cols*rows*2` instances in the single required hole-grid
  `InstancedMesh`), so the flip view has something correct to show on the
  solder side; the spec's "6000 holes" count refers to the grid itself.
- Component archetypes are split 45% axial-cylinder / 30% radial-can / 25%
  DIP across the `components` count, placed on random (margined) hole
  positions with a random 0/90/180/270° yaw — matching `Rotation` in
  `model.ts`.
- Wires and solder-trace segments share unit-length, cap-less cylinder
  geometry, scaled/rotated per instance — the "shared geometry" instancing
  the spec asks for.
- Solder traces are generated as random walks of 4-10 orthogonally-adjacent
  holes, which is the same adjacency invariant `SolderTraceConductor.path`
  encodes in `model.ts`; a runtime assertion checks every generated chain
  against it.
