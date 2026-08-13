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
        from PIL import Image
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

    frames: list[Image.Image] = []
    # Index 0 is the bare board -- the state before any step -- and index len(steps) is
    # the finished one, so the range is inclusive at both ends.
    for index in range(len(steps) + 1):
        at = document_at_step(document, guide, index)
        path = scratch / f"frame_{index:03d}.png"
        view3d.render_offscreen(at, lookup, str(path), width=width, height=height)
        frames.append(Image.open(path).convert("RGB"))
        print(f"  frame {index:3}/{len(steps)}", end="\r", flush=True)
    print()

    if not frames:
        print("nothing to animate", file=sys.stderr)
        return 1

    durations = [FRAME_MS] * len(frames)
    durations[-1] = HOLD_MS
    # The first frame is held too: a GIF that starts mid-build reads as broken, and the
    # bare board is what the animation is a departure from.
    durations[0] = HOLD_MS // 2

    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        OUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        # A GIF has 256 colours and a 3D render has rather more, so the question is only
        # which 256. An adaptive palette over the first frame would be chosen from a bare
        # board -- all substrate and copper, none of the part colours that arrive later --
        # so it is taken from the LAST frame, where everything that will ever be on the
        # board is present.
        palette=frames[-1].quantize(colors=255, method=Image.Quantize.MEDIANCUT),
        optimize=True,
    )

    size_kb = OUT.stat().st_size // 1024
    seconds = (sum(durations)) / 1000
    print(f"{OUT.relative_to(REPO_ROOT)}  {len(frames)} frames  {seconds:.1f}s  {size_kb} KB")
    if size_kb > 6000:
        print("  (large for a README -- consider --width 620)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
