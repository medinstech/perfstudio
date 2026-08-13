"""Render the animated demo the README shows: a board assembling itself.

    python tools/make_demo.py                 # -> docs/images/assembly.gif
    python tools/make_demo.py --width 700

Needs Pillow, which is not a dependency of the application -- only of this script, which
is run by hand when the demo needs regenerating.

WHY THIS ANIMATION AND NOT A SCREENCAST. A recording of somebody using the application
would show the menus, and the menus are not the interesting part. What is interesting is
that the build guide has an *order* -- shortest parts first, so a tall part fitted early
does not stop the board lying flat on the bench; the solder side before the long wires;
ICs last, for heat and ESD -- and that the tool knows it. Playing that order back is the
one picture that says what the guide is for.

It is also the honest one: every frame comes from ``guide.document_at_step``, which is
the same function the 3D panel's assembly slider calls, so the animation cannot show a
build order the guide does not actually prescribe.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from perfstudio import persist  # noqa: E402
from perfstudio.footprints import footprint_lookup  # noqa: E402
from perfstudio.guide import all_steps, build_guide, document_at_step  # noqa: E402
from perfstudio.ui import view3d  # noqa: E402

BOARD = REPO_ROOT / "examples" / "ne555-astable.perf"
OUT = REPO_ROOT / "docs" / "images" / "assembly.gif"

#: Milliseconds per frame. Slow enough to see what arrived, fast enough that a reader
#: scrolling past a README gets the whole build before they lose interest.
FRAME_MS = 420
#: The finished board is held at the end, because a loop that snaps back the instant it
#: completes never shows anybody the thing it was building.
HOLD_MS = 2500


def main(argv: list[str]) -> int:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        print("this script needs Pillow:  pip install pillow", file=sys.stderr)
        return 1

    width = 760
    if "--width" in argv:
        width = int(argv[argv.index("--width") + 1])
    height = int(width * 0.68)

    lookup = footprint_lookup()
    result = persist.deserialize_document(BOARD.read_text(encoding="utf-8"))
    if not result.ok:
        print(f"LOAD FAILED [{result.code}] {result.message}", file=sys.stderr)
        return 1
    document = result.document
    guide = build_guide(document, lookup)
    steps = all_steps(guide)

    scratch = REPO_ROOT / "build" / "demo"
    scratch.mkdir(parents=True, exist_ok=True)

    # ONE RENDERER, ONE CAMERA, FOR EVERY FRAME.
    #
    # The obvious way to write this loop is a `view3d.render_offscreen` per step, and it
    # produces an animation that jitters: that function calls `build_renderer`, which calls
    # `apply_default_camera`, which calls `ResetCamera` -- and ResetCamera frames whatever
    # is currently in the scene. A bare board and a finished one have different bounds,
    # because the parts stand above the surface, so the camera pulls back a little every
    # time a part arrives. Measured on the first version of this script: the board's
    # bounding box wandered from (100, 50) to (109, 62) across the build. Nothing about the
    # board moved; the camera did, and the whole picture shifting under a static background
    # is what a viewer reads as flicker.
    #
    # So the camera is framed ONCE, on the finished board -- the state with the largest
    # bounds, so nothing is ever clipped -- and then only the actors are replaced.
    # `populate_renderer` is exactly that operation, and it is the same one the 3D panel's
    # assembly slider uses, which is why dragging that slider in the application does not
    # jitter either.
    renderer, _ = view3d.build_renderer(document, lookup)

    vtk = view3d.vtk
    window = vtk.vtkRenderWindow()
    window.SetOffScreenRendering(1)
    window.AddRenderer(renderer)
    window.SetSize(width, height)

    def shoot(at, path: Path) -> Image.Image:
        view3d.populate_renderer(renderer, at, lookup)
        window.Render()
        grab = vtk.vtkWindowToImageFilter()
        grab.SetInput(window)
        # Without these the filter caches the first frame it was shown and every
        # subsequent Update() returns that same image -- an animation of one picture.
        grab.ReadFrontBufferOff()
        grab.Modified()
        grab.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(str(path))
        writer.SetInputConnection(grab.GetOutputPort())
        writer.Write()
        return Image.open(path).convert("RGB")

    # TWO PASSES, BECAUSE A BOARD HAS TWO FACES AND THIS ONE IS OPAQUE.
    #
    # A single component-side pass over all 22 steps produces eight distinct pictures.
    # Fourteen of those steps lay copper on the SOLDER side, which from above is behind
    # the board and invisible -- so the animation held still through most of the build and
    # then jumped. (The first version of this script hid that: its camera drifted every
    # frame, so every frame differed and the loop looked busier than it was.)
    #
    # So the demo does what the builder does. Parts go in with the board component-side up,
    # then it gets turned over and the copper goes on. That is the order the guide
    # prescribes -- phases 0 to 5 are parts, 6 is the solder side -- and turning the board
    # over is not a presentational flourish, it is the step in the middle.
    #
    # Frames identical to the one before are dropped rather than emitted: a step whose work
    # is on the face you are not looking at should not cost the viewer a beat of held
    # still picture.
    frames: list[Image.Image] = []
    counter = 0
    flip_at: int | None = None
    for pass_number, flipped in ((0, False), (1, True)):
        if flipped:
            flip_at = len(frames)
        view3d.apply_default_camera(renderer, flipped)
        pass_frames: list[Image.Image] = []
        for index in range(len(steps) + 1):
            # Index 0 is the bare board -- the state before any step -- and index
            # len(steps) is the finished one, so the range is inclusive at both ends.
            at = document_at_step(document, guide, index)
            counter += 1
            image = shoot(at, scratch / f"frame_{counter:03d}.png")
            if pass_frames and ImageChops.difference(pass_frames[-1], image).getbbox() is None:
                continue
            pass_frames.append(image)
            print(f"  pass {pass_number}  step {index:3}/{len(steps)}  "
                  f"{len(pass_frames)} frame(s)", end="\r", flush=True)
        frames.extend(pass_frames)
    print()

    if not frames:
        print("nothing to animate", file=sys.stderr)
        return 1

    durations = [FRAME_MS] * len(frames)
    durations[-1] = HOLD_MS
    # The first frame is held too: a GIF that starts mid-build reads as broken, and the
    # bare board is what the animation is a departure from.
    durations[0] = HOLD_MS // 2
    # And the last component-side frame is held, because the next one is the board turned
    # over. Without a beat there, the flip reads as a glitch rather than as the thing the
    # builder actually does halfway through.
    if flip_at is not None and 0 < flip_at <= len(durations):
        durations[flip_at - 1] = HOLD_MS

    # ONE PALETTE FOR THE WHOLE ANIMATION, and this is the part that has to be done by
    # hand. A GIF holds 256 colours *per frame*, and left alone Pillow picks an adaptive
    # palette for each frame separately -- so as parts arrive, the 256 best colours for
    # frame 11 are not the 256 best for frame 10, the board's green lands on a slightly
    # different index either side, and the whole picture visibly shifts colour every time
    # a component appears. Passing `palette=` to save() does not fix it: that names a
    # palette for the *first* frame, and the rest are still quantized on their own.
    #
    # So every frame is mapped onto one palette before saving. It is built from a strip of
    # all of them rather than from any single frame: the last frame has every part on the
    # board but the earliest have the most bare substrate, and a palette chosen from
    # either end alone spends its colours in the wrong place for the other.
    strip = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for index, frame in enumerate(frames):
        strip.paste(frame, (0, index * frames[0].height))
    palette = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT)

    # Dither.NONE deliberately. Floyd-Steinberg scatters its error differently in each
    # frame, so flat areas that are identical between two frames come out as two different
    # patterns of noise -- which reads as a shimmer over the whole board, the same
    # complaint as the palette shift and less obvious to diagnose. The render is mostly
    # flat shading, so there is little for dithering to buy here anyway.
    mapped = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    mapped[0].save(
        OUT,
        save_all=True,
        append_images=mapped[1:],
        duration=durations,
        loop=0,
        # Every frame is a full frame over the last one. The default disposal leaves
        # Pillow free to write partial frames, which is smaller but depends on the two
        # frames agreeing about their palette -- they now do, but a full replace is what
        # this animation actually is, and it cannot half-erase a part.
        disposal=1,
        optimize=False,
    )

    size_kb = OUT.stat().st_size // 1024
    seconds = (sum(durations)) / 1000
    print(f"{OUT.relative_to(REPO_ROOT)}  {len(frames)} frames  {seconds:.1f}s  {size_kb} KB")
    if size_kb > 6000:
        print("  (large for a README -- consider --width 620)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
