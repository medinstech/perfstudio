/**
 * PerfStudio bench-3d — M0 risk-reduction harness.
 *
 * Renders a realistic worst-case perfboard scene with three.js and measures
 * frame timing, so we can decide whether Tauri's OS webview (WebKitGTK on
 * Linux, in particular) can carry PerfStudio's 3D board view, or whether the
 * project needs to fall back to Electron. See PLAN.md §8.3/§8.4 and §11 (M0).
 *
 * Domain vocabulary (holes, board sides, solder-trace chains of orthogonally
 * adjacent holes, component body archetypes) mirrors packages/core/src/model.ts.
 * This tool is intentionally standalone (no workspace dependency on
 * @perfstudio/core) so it stays a zero-install, drop-anywhere harness.
 */

import * as THREE from 'three';

// ---------------------------------------------------------------------------
// Scene parameters — from URL query params, with the spec's defaults.
// ---------------------------------------------------------------------------

interface SceneParams {
  readonly cols: number;
  readonly rows: number;
  readonly components: number;
  readonly wires: number;
  readonly traces: number;
}

function readParams(): SceneParams {
  const q = new URLSearchParams(window.location.search);
  const int = (name: string, fallback: number): number => {
    const raw = q.get(name);
    if (raw === null) return fallback;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) && n >= 0 ? n : fallback;
  };
  return {
    cols: int('cols', 100),
    rows: int('rows', 60),
    components: int('components', 60),
    wires: int('wires', 200),
    traces: int('traces', 40),
  };
}

const PARAMS = readParams();

// Perfboard constants (mm). We use 1 three.js unit = 1 mm throughout.
const PITCH_MM = 2.54; // STANDARD_PITCH_MM in packages/core/src/model.ts
const BOARD_THICKNESS_MM = 1.6;
const PAD_OUTER_DIAMETER_MM = 2.0;
const PAD_DRILL_DIAMETER_MM = 0.9;
const BOARD_MARGIN_MM = PITCH_MM; // unpopulated rim around the outermost holes

const BOARD_WIDTH_MM = PARAMS.cols * PITCH_MM;
const BOARD_DEPTH_MM = PARAMS.rows * PITCH_MM;

// ---------------------------------------------------------------------------
// Deterministic PRNG — the whole point of a benchmark is that two runs (or
// two platforms) exercise the identical scene. Seed is derived from the
// params themselves, so a given URL always regenerates the same layout.
// ---------------------------------------------------------------------------

function hashSeed(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed: number): () => number {
  let a = seed;
  return function random() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rng = mulberry32(
  hashSeed(`${PARAMS.cols}|${PARAMS.rows}|${PARAMS.components}|${PARAMS.wires}|${PARAMS.traces}`),
);
const randInt = (min: number, max: number): number => min + Math.floor(rng() * (max - min + 1));
const pick = <T,>(arr: readonly T[]): T => arr[randInt(0, arr.length - 1)] as T;

// ---------------------------------------------------------------------------
// Hole grid helpers — col increases right, row increases "down" (matches
// HoleCoord in model.ts). The grid is centred on the origin.
// ---------------------------------------------------------------------------

function holePosition(col: number, row: number): THREE.Vector2 {
  const x = (col - (PARAMS.cols - 1) / 2) * PITCH_MM;
  const z = (row - (PARAMS.rows - 1) / 2) * PITCH_MM;
  return new THREE.Vector2(x, z);
}

function isOrthogonalNeighbour(a: { col: number; row: number }, b: { col: number; row: number }): boolean {
  const dc = Math.abs(a.col - b.col);
  const dr = Math.abs(a.row - b.row);
  return dc + dr === 1;
}

// ---------------------------------------------------------------------------
// Renderer / scene / camera
// ---------------------------------------------------------------------------

const app = document.getElementById('app') as HTMLDivElement;

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111318);

const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 1, 20000);
const boardCenter = new THREE.Vector3(0, 0, 0);
const boardDiagonal = Math.hypot(BOARD_WIDTH_MM, BOARD_DEPTH_MM);
{
  const radius = boardDiagonal * 0.85;
  const phi = THREE.MathUtils.degToRad(55);
  const theta = THREE.MathUtils.degToRad(30);
  const offset = new THREE.Vector3().setFromSphericalCoords(radius, phi, theta);
  camera.position.copy(boardCenter).add(offset);
  camera.lookAt(boardCenter);
}

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// Simple, cheap lighting — no shadow maps, no post-processing (PLAN.md §8.3:
// "accurate and legible", not photorealistic).
scene.add(new THREE.AmbientLight(0xffffff, 0.65));
const keyLight = new THREE.DirectionalLight(0xffffff, 1.1);
keyLight.position.set(boardDiagonal * 0.4, boardDiagonal * 0.8, boardDiagonal * 0.3);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xaac0ff, 0.35);
fillLight.position.set(-boardDiagonal * 0.5, boardDiagonal * 0.3, -boardDiagonal * 0.4);
scene.add(fillLight);

// Everything that should flip together when the user views the solder side.
const boardGroup = new THREE.Group();
scene.add(boardGroup);

// ---------------------------------------------------------------------------
// Board substrate — one box, FR-4 green-brown.
// ---------------------------------------------------------------------------

{
  const geo = new THREE.BoxGeometry(
    BOARD_WIDTH_MM + BOARD_MARGIN_MM,
    BOARD_THICKNESS_MM,
    BOARD_DEPTH_MM + BOARD_MARGIN_MM,
  );
  const mat = new THREE.MeshStandardMaterial({ color: 0x5a6b3f, roughness: 0.85, metalness: 0.05 });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.name = 'board-substrate';
  boardGroup.add(mesh);
}

// ---------------------------------------------------------------------------
// Hole grid — ONE InstancedMesh for every pad, on both faces (through-hole
// pads are real on a real perfboard, and the flip feature needs something to
// show on the bottom). cols*rows*2 instances, single draw call.
// ---------------------------------------------------------------------------

const holeCount = PARAMS.cols * PARAMS.rows;

{
  const padGeo = new THREE.RingGeometry(PAD_DRILL_DIAMETER_MM / 2, PAD_OUTER_DIAMETER_MM / 2, 10);
  padGeo.rotateX(-Math.PI / 2); // lie flat on the XZ plane
  const padMat = new THREE.MeshStandardMaterial({
    color: 0xc9c9c9,
    metalness: 0.75,
    roughness: 0.35,
    side: THREE.DoubleSide, // visible from either side of the free-orbiting camera
  });

  const padMesh = new THREE.InstancedMesh(padGeo, padMat, holeCount * 2);
  padMesh.name = 'hole-pads';
  const m = new THREE.Matrix4();
  const surfaceY = BOARD_THICKNESS_MM / 2 + 0.02;
  let i = 0;
  for (let row = 0; row < PARAMS.rows; row++) {
    for (let col = 0; col < PARAMS.cols; col++) {
      const p = holePosition(col, row);
      m.makeTranslation(p.x, surfaceY, p.y);
      padMesh.setMatrixAt(i++, m);
      m.makeTranslation(p.x, -surfaceY, p.y);
      padMesh.setMatrixAt(i++, m);
    }
  }
  padMesh.instanceMatrix.needsUpdate = true;
  boardGroup.add(padMesh);
}

// ---------------------------------------------------------------------------
// Component bodies — three archetypes (PLAN.md §8.3 BodyArchetype), each its
// own InstancedMesh: axial cylinder (resistor/diode), radial can
// (electrolytic), DIP box.
// ---------------------------------------------------------------------------

function marginedHole(): { col: number; row: number } {
  const margin = 2;
  const col = randInt(margin, Math.max(margin, PARAMS.cols - 1 - margin));
  const row = randInt(margin, Math.max(margin, PARAMS.rows - 1 - margin));
  return { col, row };
}

function splitCount(total: number, ratios: readonly number[]): number[] {
  const counts = ratios.map((r) => Math.round(total * r));
  let diff = total - counts.reduce((a, b) => a + b, 0);
  let idx = 0;
  while (diff !== 0) {
    counts[idx % counts.length] = Math.max(0, (counts[idx % counts.length] ?? 0) + Math.sign(diff));
    diff -= Math.sign(diff);
    idx++;
  }
  return counts;
}

const ROTATIONS_DEG = [0, 90, 180, 270];

{
  const [axialCount, radialCount, dipCount] = splitCount(PARAMS.components, [0.45, 0.3, 0.25]);
  const topSurfaceY = BOARD_THICKNESS_MM / 2;

  // Archetype 1: axial cylinder, lying flat (resistor / DO-41 diode body).
  {
    const bodyLenMm = 6.3;
    const bodyRadiusMm = 1.1;
    const geo = new THREE.CylinderGeometry(bodyRadiusMm, bodyRadiusMm, bodyLenMm, 10);
    geo.rotateZ(Math.PI / 2); // axis along local X, laid flat
    const mat = new THREE.MeshStandardMaterial({ color: 0xd9c398, roughness: 0.6, metalness: 0.05 });
    const mesh = new THREE.InstancedMesh(geo, mat, Math.max(0, axialCount ?? 0));
    mesh.name = 'component-axial-cylinder';
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const s = new THREE.Vector3(1, 1, 1);
    for (let i = 0; i < mesh.count; i++) {
      const h = marginedHole();
      const p = holePosition(h.col, h.row);
      const rotY = THREE.MathUtils.degToRad(pick(ROTATIONS_DEG));
      q.setFromEuler(new THREE.Euler(0, rotY, 0));
      m.compose(new THREE.Vector3(p.x, topSurfaceY + bodyRadiusMm, p.y), q, s);
      mesh.setMatrixAt(i, m);
    }
    mesh.instanceMatrix.needsUpdate = true;
    boardGroup.add(mesh);
  }

  // Archetype 2: radial can, standing upright (electrolytic capacitor).
  {
    const canHeightMm = 7;
    const canRadiusMm = 2.5;
    const geo = new THREE.CylinderGeometry(canRadiusMm, canRadiusMm, canHeightMm, 14);
    const mat = new THREE.MeshStandardMaterial({ color: 0x16213a, roughness: 0.45, metalness: 0.2 });
    const mesh = new THREE.InstancedMesh(geo, mat, Math.max(0, radialCount ?? 0));
    mesh.name = 'component-radial-can';
    const m = new THREE.Matrix4();
    for (let i = 0; i < mesh.count; i++) {
      const h = marginedHole();
      const p = holePosition(h.col, h.row);
      m.makeTranslation(p.x, topSurfaceY + canHeightMm / 2, p.y);
      mesh.setMatrixAt(i, m);
    }
    mesh.instanceMatrix.needsUpdate = true;
    boardGroup.add(mesh);
  }

  // Archetype 3: DIP box (8-pin logic package).
  {
    const dipW = 7.62; // 0.3" row spacing
    const dipD = 10.0;
    const dipH = 3.2;
    const geo = new THREE.BoxGeometry(dipW, dipH, dipD);
    const mat = new THREE.MeshStandardMaterial({ color: 0x1b1b1e, roughness: 0.55, metalness: 0.1 });
    const mesh = new THREE.InstancedMesh(geo, mat, Math.max(0, dipCount ?? 0));
    mesh.name = 'component-dip';
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const s = new THREE.Vector3(1, 1, 1);
    for (let i = 0; i < mesh.count; i++) {
      const h = marginedHole();
      const p = holePosition(h.col, h.row);
      const rotY = THREE.MathUtils.degToRad(pick(ROTATIONS_DEG));
      q.setFromEuler(new THREE.Euler(0, rotY, 0));
      m.compose(new THREE.Vector3(p.x, topSurfaceY + dipH / 2, p.y), q, s);
      mesh.setMatrixAt(i, m);
    }
    mesh.instanceMatrix.needsUpdate = true;
    boardGroup.add(mesh);
  }
}

// ---------------------------------------------------------------------------
// Shared helper: orient a unit-height cylinder between two points, used by
// both wires and solder-trace segments.
// ---------------------------------------------------------------------------

const UP = new THREE.Vector3(0, 1, 0);
function segmentMatrix(a: THREE.Vector3, b: THREE.Vector3, radiusScale: number): THREE.Matrix4 {
  const dir = new THREE.Vector3().subVectors(b, a);
  const length = Math.max(dir.length(), 1e-4);
  const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(0.5);
  const q = new THREE.Quaternion().setFromUnitVectors(UP, dir.clone().normalize());
  const s = new THREE.Vector3(radiusScale, length, radiusScale);
  return new THREE.Matrix4().compose(mid, q, s);
}

// ---------------------------------------------------------------------------
// Wires — bottom face (solder side), point-to-point, thin cylinders. One
// shared InstancedMesh; colour varies per-instance via instanceColor to
// mimic real insulation colours (model.ts WireConductor.color).
// ---------------------------------------------------------------------------

const WIRE_COLORS = [0xd23c3c, 0x2f2f2f, 0xf0d030, 0x2e7d32, 0x2255aa, 0xf2f2f2, 0xff8a30];

{
  const wireGeo = new THREE.CylinderGeometry(1, 1, 1, 6, 1, true); // unit cylinder, no caps
  const wireMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.7, metalness: 0.1 });
  const mesh = new THREE.InstancedMesh(wireGeo, wireMat, PARAMS.wires);
  mesh.name = 'wires';
  const bottomY = -(BOARD_THICKNESS_MM / 2) - 0.6;
  const color = new THREE.Color();
  for (let i = 0; i < mesh.count; i++) {
    const ha = marginedHole();
    const hb = marginedHole();
    const pa = holePosition(ha.col, ha.row);
    const pb = holePosition(hb.col, hb.row);
    const a = new THREE.Vector3(pa.x, bottomY, pa.y);
    const b = new THREE.Vector3(pb.x, bottomY, pb.y);
    mesh.setMatrixAt(i, segmentMatrix(a, b, 0.25));
    mesh.setColorAt(i, color.setHex(pick(WIRE_COLORS)));
  }
  mesh.instanceMatrix.needsUpdate = true;
  if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  boardGroup.add(mesh);
}

// ---------------------------------------------------------------------------
// Solder traces — the visually distinctive PerfStudio primitive (PLAN.md
// §4.6, §8.3). Each trace is a random walk of 4-10 orthogonally-adjacent
// holes (matches the SolderTraceConductor.path invariant in model.ts):
// a bead (sphere) at every pad, thinner cylinders between them, bright
// metallic material to read as solder rather than insulated wire.
// ---------------------------------------------------------------------------

{
  const beadPositions: THREE.Vector3[] = [];
  const segments: [THREE.Vector3, THREE.Vector3][] = [];
  const bottomY = -(BOARD_THICKNESS_MM / 2) - 0.35;

  for (let t = 0; t < PARAMS.traces; t++) {
    const chainLen = randInt(4, 10);
    let cur = marginedHole();
    const chain: { col: number; row: number }[] = [cur];
    for (let step = 1; step < chainLen; step++) {
      const dirs = [
        { dc: 1, dr: 0 },
        { dc: -1, dr: 0 },
        { dc: 0, dr: 1 },
        { dc: 0, dr: -1 },
      ];
      let next: { col: number; row: number } | null = null;
      // try a few random directions before giving up and stopping the chain short
      for (let attempt = 0; attempt < 4; attempt++) {
        const d = pick(dirs);
        const cand = { col: cur.col + d.dc, row: cur.row + d.dr };
        if (cand.col >= 0 && cand.col < PARAMS.cols && cand.row >= 0 && cand.row < PARAMS.rows) {
          next = cand;
          break;
        }
      }
      if (!next) break;
      chain.push(next);
      cur = next;
    }
    // Sanity check against the domain invariant this generator is supposed to satisfy.
    for (let k = 1; k < chain.length; k++) {
      const prev = chain[k - 1];
      const c = chain[k];
      if (!prev || !c || !isOrthogonalNeighbour(prev, c)) {
        throw new Error('solder-trace generator produced a non-adjacent hop');
      }
    }
    const points = chain.map((h) => {
      const p = holePosition(h.col, h.row);
      return new THREE.Vector3(p.x, bottomY, p.y);
    });
    for (const pt of points) beadPositions.push(pt);
    for (let k = 1; k < points.length; k++) {
      const prev = points[k - 1];
      const c = points[k];
      if (prev && c) segments.push([prev, c]);
    }
  }

  const solderMat = new THREE.MeshStandardMaterial({
    color: 0xd7d7dd,
    metalness: 0.9,
    roughness: 0.22,
  });

  // Beads bulge at the pads; the profile "buildup" concept from PLAN.md §4.6/§8.3.
  const beadGeo = new THREE.SphereGeometry(PAD_OUTER_DIAMETER_MM * 0.42, 8, 6);
  const beadMesh = new THREE.InstancedMesh(beadGeo, solderMat, beadPositions.length);
  beadMesh.name = 'solder-trace-beads';
  {
    const m = new THREE.Matrix4();
    beadPositions.forEach((p, i) => {
      m.makeTranslation(p.x, p.y, p.z);
      beadMesh.setMatrixAt(i, m);
    });
    beadMesh.instanceMatrix.needsUpdate = true;
  }
  boardGroup.add(beadMesh);

  // Segments thin out between pads — thinner radius than the beads.
  const segGeo = new THREE.CylinderGeometry(1, 1, 1, 6, 1, true);
  const segMesh = new THREE.InstancedMesh(segGeo, solderMat, segments.length);
  segMesh.name = 'solder-trace-segments';
  segments.forEach(([a, b], i) => {
    segMesh.setMatrixAt(i, segmentMatrix(a, b, PAD_OUTER_DIAMETER_MM * 0.18));
  });
  segMesh.instanceMatrix.needsUpdate = true;
  boardGroup.add(segMesh);
}

// ---------------------------------------------------------------------------
// Orbit-style controls. Prefer three's own examples/jsm OrbitControls (it
// ships with the installed three package); fall back to a small hand-rolled
// pointer-based orbit if that import fails for any reason. Both expose the
// same tiny interface so the rest of the app doesn't care which is active.
// ---------------------------------------------------------------------------

interface OrbitLike {
  readonly kind: string;
  update(): void;
  setEnabled(enabled: boolean): void;
  /** Re-derive internal orbit state from the camera's actual transform. */
  syncFromCamera(): void;
}

function createFallbackControls(camera: THREE.PerspectiveCamera, dom: HTMLElement, target: THREE.Vector3): OrbitLike {
  let enabled = true;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  const spherical = new THREE.Spherical();
  const offset = new THREE.Vector3();

  const syncFromCamera = () => {
    offset.copy(camera.position).sub(target);
    spherical.setFromVector3(offset);
  };
  syncFromCamera();

  const apply = () => {
    spherical.phi = THREE.MathUtils.clamp(spherical.phi, 0.05, Math.PI - 0.05);
    spherical.radius = THREE.MathUtils.clamp(spherical.radius, boardDiagonal * 0.15, boardDiagonal * 6);
    offset.setFromSpherical(spherical);
    camera.position.copy(target).add(offset);
    camera.lookAt(target);
  };

  dom.addEventListener('pointerdown', (e) => {
    if (!enabled) return;
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    syncFromCamera();
    dom.setPointerCapture(e.pointerId);
  });
  dom.addEventListener('pointermove', (e) => {
    if (!enabled || !dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    spherical.theta -= dx * 0.006;
    spherical.phi -= dy * 0.006;
    apply();
  });
  dom.addEventListener('pointerup', () => {
    dragging = false;
  });
  dom.addEventListener('pointerleave', () => {
    dragging = false;
  });
  dom.addEventListener(
    'wheel',
    (e) => {
      if (!enabled) return;
      e.preventDefault();
      syncFromCamera();
      spherical.radius *= e.deltaY > 0 ? 1.08 : 1 / 1.08;
      apply();
    },
    { passive: false },
  );

  return {
    kind: 'fallback-pointer-orbit',
    update() {
      /* state is applied directly on input; nothing to integrate per frame */
    },
    setEnabled(v: boolean) {
      enabled = v;
      dragging = dragging && v;
    },
    syncFromCamera,
  };
}

async function createControls(
  camera: THREE.PerspectiveCamera,
  dom: HTMLElement,
  target: THREE.Vector3,
): Promise<OrbitLike> {
  try {
    // Deep import into three's own examples tree (not an external/CDN path).
    const mod = await import('three/examples/jsm/controls/OrbitControls.js');
    const oc = new mod.OrbitControls(camera, dom);
    oc.target.copy(target);
    oc.enableDamping = true;
    oc.dampingFactor = 0.08;
    oc.minDistance = boardDiagonal * 0.15;
    oc.maxDistance = boardDiagonal * 6;
    oc.update();
    return {
      kind: 'three/examples OrbitControls',
      update: () => oc.update(),
      setEnabled: (v: boolean) => {
        oc.enabled = v;
      },
      syncFromCamera: () => oc.update(), // OrbitControls re-derives spherical from camera.position every call
    };
  } catch (err) {
    console.warn('[bench-3d] three/examples OrbitControls unavailable, using fallback controls:', err);
    return createFallbackControls(camera, dom, target);
  }
}

let controls: OrbitLike = createFallbackControls(camera, renderer.domElement, boardCenter);
void createControls(camera, renderer.domElement, boardCenter).then((c) => {
  controls = c;
});

// ---------------------------------------------------------------------------
// Board flip — rotates the whole board group 180°, a real flip (PLAN.md
// §8.3: "3D'de gerçek çevirme"), not just a camera move, so the underside
// is genuinely facing the default view once flipped.
// ---------------------------------------------------------------------------

let flipTarget = 0; // 0 = component side, 1 = solder side
let flipProgress = 0; // animates toward flipTarget
const FLIP_SPEED = 3.2; // progress units per second (~310ms for a full flip)

window.addEventListener('keydown', (e) => {
  if (e.key.toLowerCase() === 'f') {
    flipTarget = flipTarget === 0 ? 1 : 0;
  }
});

function updateFlip(dtSeconds: number): void {
  const delta = flipTarget - flipProgress;
  if (Math.abs(delta) < 1e-4) {
    flipProgress = flipTarget;
  } else {
    flipProgress += Math.sign(delta) * Math.min(Math.abs(delta), FLIP_SPEED * dtSeconds);
  }
  boardGroup.rotation.x = flipProgress * Math.PI;
}

// ---------------------------------------------------------------------------
// Benchmark: spin the camera at a fixed rate for a fixed duration, so the
// rendered workload (visible instances, overdraw) is the same every run and
// on every platform. Camera is snapped to a canonical framing first.
// ---------------------------------------------------------------------------

const BENCH_DURATION_S = 10;
const SPIN_RATE_RAD_S = (2 * Math.PI) / BENCH_DURATION_S; // one full revolution per run

let benchmarkActive = false;
let benchmarkStartTime = 0;
let benchmarkFrameTimes: number[] = [];
const spinSpherical = new THREE.Spherical();
const spinOffset = new THREE.Vector3();

function applySpin(elapsedSeconds: number): void {
  spinSpherical.theta = elapsedSeconds * SPIN_RATE_RAD_S;
  spinOffset.setFromSpherical(spinSpherical);
  camera.position.copy(boardCenter).add(spinOffset);
  camera.lookAt(boardCenter);
}

interface BenchmarkSummary {
  readonly userAgent: string;
  readonly cols: number;
  readonly rows: number;
  readonly components: number;
  readonly wires: number;
  readonly traces: number;
  readonly frames: number;
  readonly fps_mean: number;
  readonly fps_p1_low: number;
  readonly frame_ms_mean: number;
  readonly frame_ms_p50: number;
  readonly frame_ms_p95: number;
  readonly frame_ms_max: number;
  readonly drawCalls: number;
  readonly triangles: number;
}

function mean(arr: readonly number[]): number {
  if (arr.length === 0) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

function percentile(sortedAsc: readonly number[], p: number): number {
  if (sortedAsc.length === 0) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.ceil((p / 100) * sortedAsc.length) - 1));
  return sortedAsc[idx] ?? 0;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function computeSummary(frameTimesMs: readonly number[]): BenchmarkSummary {
  const sorted = [...frameTimesMs].sort((a, b) => a - b);
  const frameMsMean = mean(frameTimesMs);
  // "1% low": the mean of the slowest 1% of frames, converted to fps — this
  // is what actually reads as stutter, unlike a plain average.
  const worstCount = Math.max(1, Math.round(sorted.length * 0.01));
  const worstFrames = sorted.slice(sorted.length - worstCount);
  const worstMeanMs = mean(worstFrames);

  return {
    userAgent: navigator.userAgent,
    cols: PARAMS.cols,
    rows: PARAMS.rows,
    components: PARAMS.components,
    wires: PARAMS.wires,
    traces: PARAMS.traces,
    frames: frameTimesMs.length,
    fps_mean: round2(frameMsMean > 0 ? 1000 / frameMsMean : 0),
    fps_p1_low: round2(worstMeanMs > 0 ? 1000 / worstMeanMs : 0),
    frame_ms_mean: round2(frameMsMean),
    frame_ms_p50: round2(percentile(sorted, 50)),
    frame_ms_p95: round2(percentile(sorted, 95)),
    frame_ms_max: round2(sorted[sorted.length - 1] ?? 0),
    drawCalls: renderer.info.render.calls,
    triangles: renderer.info.render.triangles,
  };
}

// ---------------------------------------------------------------------------
// HUD
// ---------------------------------------------------------------------------

const hudBody = document.getElementById('hudBody') as HTMLDivElement;
const runBtn = document.getElementById('runBtn') as HTMLButtonElement;
const benchStatus = document.getElementById('benchStatus') as HTMLDivElement;
const resultsPanel = document.getElementById('results') as HTMLDivElement;
const resultSummaryEl = document.getElementById('resultSummary') as HTMLPreElement;
const resultJsonEl = document.getElementById('resultJson') as HTMLPreElement;
const copyBtn = document.getElementById('copyBtn') as HTMLButtonElement;
const copyStatus = document.getElementById('copyStatus') as HTMLSpanElement;

// Rolling 1s FPS window: timestamps of frames rendered in the last 1000ms.
const recentFrameTimestamps: number[] = [];

function updateHud(frameMs: number): void {
  const info = renderer.info;
  const side = flipTarget === 0 ? 'TOP (components)' : 'BOTTOM (solder)';
  hudBody.textContent =
    `fps (1s):     ${recentFrameTimestamps.length}\n` +
    `frame time:   ${frameMs.toFixed(2)} ms\n` +
    `draw calls:   ${info.render.calls}\n` +
    `triangles:    ${info.render.triangles.toLocaleString()}\n` +
    `geometries:   ${info.memory.geometries}\n` +
    `textures:     ${info.memory.textures}\n` +
    `side:         ${side}\n` +
    `controls:     ${controls.kind}\n` +
    `params:       cols=${PARAMS.cols} rows=${PARAMS.rows} components=${PARAMS.components} ` +
    `wires=${PARAMS.wires} traces=${PARAMS.traces}\n` +
    `holes:        ${holeCount.toLocaleString()} (pad instances: ${(holeCount * 2).toLocaleString()})` +
    (benchmarkActive
      ? `\n\nBENCHMARK RUNNING: ${((performance.now() - benchmarkStartTime) / 1000).toFixed(1)}s / ${BENCH_DURATION_S}s`
      : '');
}

function renderSummary(summary: BenchmarkSummary): void {
  resultSummaryEl.textContent =
    `fps mean:        ${summary.fps_mean}\n` +
    `fps 1% low:      ${summary.fps_p1_low}\n` +
    `frame ms mean:   ${summary.frame_ms_mean}\n` +
    `frame ms p50:    ${summary.frame_ms_p50}\n` +
    `frame ms p95:    ${summary.frame_ms_p95}\n` +
    `frame ms max:    ${summary.frame_ms_max}\n` +
    `frames:          ${summary.frames}\n` +
    `draw calls:      ${summary.drawCalls}\n` +
    `triangles:       ${summary.triangles.toLocaleString()}`;
  resultJsonEl.textContent = JSON.stringify(summary, null, 2);
  resultsPanel.classList.add('visible');
}

copyBtn.addEventListener('click', () => {
  void copyResultJson();
});

async function copyResultJson(): Promise<void> {
  const text = resultJsonEl.textContent ?? '';
  try {
    await navigator.clipboard.writeText(text);
    copyStatus.textContent = 'copied';
  } catch {
    // Some embedded webviews restrict the async Clipboard API even on a
    // user gesture; fall back to the classic textarea+execCommand trick.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand('copy');
      copyStatus.textContent = 'copied';
    } catch {
      copyStatus.textContent = 'copy failed — select the text manually';
    }
    document.body.removeChild(ta);
  }
  setTimeout(() => {
    copyStatus.textContent = '';
  }, 2000);
}

// ---------------------------------------------------------------------------
// Benchmark run lifecycle
// ---------------------------------------------------------------------------

function beginBenchmark(): void {
  if (benchmarkActive) return;
  spinSpherical.radius = boardDiagonal * 0.85;
  spinSpherical.phi = THREE.MathUtils.degToRad(55);
  applySpin(0);

  controls.setEnabled(false);
  benchmarkActive = true;
  benchmarkStartTime = performance.now();
  benchmarkFrameTimes = [];
  runBtn.disabled = true;
  runBtn.textContent = 'Running…';
  benchStatus.textContent = `benchmark running: 0.0s / ${BENCH_DURATION_S}s`;
}

function endBenchmark(): void {
  benchmarkActive = false;
  controls.setEnabled(true);
  controls.syncFromCamera();
  runBtn.disabled = false;
  runBtn.textContent = 'Run 10s benchmark';
  benchStatus.textContent = 'benchmark complete — see result below';

  const summary = computeSummary(benchmarkFrameTimes);
  renderSummary(summary);
  // Single-line JSON, exactly as specified, so it's trivially copy-pasteable
  // out of a headless/remote devtools console too.
  console.log(JSON.stringify(summary));
}

runBtn.addEventListener('click', beginBenchmark);

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

let lastFrameTime = performance.now();

function animate(now: number): void {
  requestAnimationFrame(animate);
  const frameMs = now - lastFrameTime;
  lastFrameTime = now;

  recentFrameTimestamps.push(now);
  while (recentFrameTimestamps.length > 0 && now - (recentFrameTimestamps[0] ?? now) > 1000) {
    recentFrameTimestamps.shift();
  }

  const dtSeconds = Math.min(frameMs, 250) / 1000; // clamp to avoid a huge jump after a tab switch

  if (benchmarkActive) {
    const elapsed = (now - benchmarkStartTime) / 1000;
    benchmarkFrameTimes.push(frameMs);
    applySpin(elapsed);
    benchStatus.textContent = `benchmark running: ${elapsed.toFixed(1)}s / ${BENCH_DURATION_S}s`;
    if (elapsed >= BENCH_DURATION_S) {
      endBenchmark();
    }
  } else {
    controls.update();
  }

  updateFlip(dtSeconds);

  renderer.render(scene, camera);
  updateHud(frameMs);
}

requestAnimationFrame(animate);
