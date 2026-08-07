/**
 * Minimal recording stub for `CanvasRenderingContext2D`, used only by this package's
 * own tests. It implements exactly the subset of methods/properties `renderBoard`
 * calls, and logs every method invocation so tests can assert on *what* was drawn
 * without depending on a real DOM or a headless-canvas package — this package must
 * stay host-agnostic and DOM-free at runtime, including in its own test suite.
 *
 * It deliberately does not attempt to structurally satisfy the full
 * `CanvasRenderingContext2D` interface (that interface has hundreds of members); call
 * sites cast it with `createRecordingContext()`, which returns it pre-cast.
 */

export interface RecordedCall {
  readonly method: string;
  readonly args: readonly unknown[];
}

export class RecordingContext2D {
  readonly calls: RecordedCall[] = [];

  fillStyle = '#000000';
  strokeStyle = '#000000';
  lineWidth = 1;
  lineCap = 'butt';
  lineJoin = 'miter';
  font = '10px sans-serif';
  textAlign = 'start';
  textBaseline = 'alphabetic';
  globalAlpha = 1;

  private record(method: string, args: readonly unknown[]): void {
    this.calls.push({ method, args });
  }

  save(): void {
    this.record('save', []);
  }

  restore(): void {
    this.record('restore', []);
  }

  scale(x: number, y: number): void {
    this.record('scale', [x, y]);
  }

  clearRect(x: number, y: number, w: number, h: number): void {
    this.record('clearRect', [x, y, w, h]);
  }

  fillRect(x: number, y: number, w: number, h: number): void {
    this.record('fillRect', [x, y, w, h]);
  }

  beginPath(): void {
    this.record('beginPath', []);
  }

  closePath(): void {
    this.record('closePath', []);
  }

  moveTo(x: number, y: number): void {
    this.record('moveTo', [x, y]);
  }

  lineTo(x: number, y: number): void {
    this.record('lineTo', [x, y]);
  }

  arc(x: number, y: number, radius: number, startAngle: number, endAngle: number, ccw?: boolean): void {
    this.record('arc', [x, y, radius, startAngle, endAngle, ccw]);
  }

  fill(): void {
    this.record('fill', []);
  }

  stroke(): void {
    this.record('stroke', []);
  }

  setLineDash(segments: number[]): void {
    this.record('setLineDash', [segments]);
  }

  fillText(text: string, x: number, y: number): void {
    this.record('fillText', [text, x, y]);
  }

  /** All recorded calls to a given method, in call order. */
  callsOf(method: string): RecordedCall[] {
    return this.calls.filter((c) => c.method === method);
  }
}

/** Creates a fresh recording stub, cast to the shape `renderBoard` expects. */
export function createRecordingContext(): { ctx: CanvasRenderingContext2D; recorder: RecordingContext2D } {
  const recorder = new RecordingContext2D();
  return { ctx: recorder as unknown as CanvasRenderingContext2D, recorder };
}
