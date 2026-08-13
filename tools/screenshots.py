"""Regenerate the screenshots the README embeds.

    python tools/screenshots.py

Writes into ``docs/images/``, which is committed -- a README on GitHub cannot reach a
file that is only in ``headless_out/``.

This exists as a script rather than as a folder of hand-grabbed PNGs because the last
set went stale without anybody noticing: they were taken before parts were drawn as
their real bodies, so the project's front page advertised a rendering bug for as long as
they sat there. A screenshot that can be regenerated in one command is a screenshot that
gets regenerated.

The two editor shots drive the real ``MainWindow`` -- the same window a user gets --
rather than re-rendering the scene the way ``--headless`` does. The point of those
particular pictures is the application around the board: the nets panel, the toolbar,
the DRC/LVS dock and the status bar all carry information that a bare board render does
not, and a README's job is to show what the thing looks like to use.

The 3D shot goes through ``view3d.render_offscreen`` instead of grabbing the 3D dock.
Grabbing the dock does not work and quietly produces a black rectangle: the panel builds
its widget from a visibility signal and VTK renders into it on some later trip through
the event loop, so a grab taken from the same script always wins the race. Offscreen is
also the path the build guide's step images take, so this picture is a real product of
the shipping code rather than a special case.

The window is shown briefly on a real display, which is unavoidable: Qt's offscreen
plugin has no font database on Windows (see ``_default_headless_platform`` in
ui/main.py), so an offscreen grab would come back with every label as a missing-glyph
box.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from perfstudio import persist  # noqa: E402
from perfstudio.footprints import footprint_lookup  # noqa: E402
from perfstudio.placer import PlacementOptions, plan_placement  # noqa: E402
from perfstudio.ui import view3d  # noqa: E402
from perfstudio.ui.main import MainWindow  # noqa: E402

#: The NE555 astable, which is also what ``examples/ne555-astable.net`` imports to. A
#: recognisable circuit beats a denser but meaningless one: a reader who knows the 555
#: can check the picture against what they expect, which is the whole point of a
#: screenshot on a landing page.
FIXTURE = REPO_ROOT / "tools" / "diffcheck" / "golden" / "ne555.perf"

OUT_DIR = REPO_ROOT / "docs" / "images"


def _settle(app: QApplication, rounds: int = 12) -> None:
    """Let Qt finish laying out and painting before grabbing.

    A single ``processEvents`` is not enough: fitting the view and re-rendering the pad
    grid both land on later trips through the loop.
    """
    for _ in range(rounds):
        app.processEvents()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    assert isinstance(app, QApplication)

    result = persist.deserialize_document(FIXTURE.read_text(encoding="utf-8"))
    if not result.ok:
        print(f"LOAD FAILED [{result.code}] {result.message}")
        return 1

    window = MainWindow(result.document, FIXTURE)
    window.resize(1600, 1000)
    window.show()
    _settle(app)

    # Auto-place, then route -- the order the README documents, so the picture is of the
    # workflow it describes rather than of a fixture. The golden document's placement is
    # spread over a mostly empty board, which routes into long insulated wires and shows
    # the tool at its least flattering for a reason that has nothing to do with the tool.
    #
    # The planner and the bus are driven directly rather than through on_autoplace(),
    # which asks for confirmation first -- correct for a person, a deadlock for a script.
    # This is the path its accept button takes.
    plan = plan_placement(window.bus.document, window.lookup, PlacementOptions(seed=0))
    if not plan.is_empty:
        moved = window.bus.dispatch("component.moveMany", plan.payload())
        if not moved.ok:
            print(f"PLACEMENT REFUSED [{moved.code}] {moved.message}")
            return 1
    _settle(app)

    # An unrouted board is a picture of a to-do list. This is the same call the toolbar
    # button makes, so the copper in the picture is what the shipped router produces.
    window.on_autoroute_all()
    _settle(app)

    # Ratsnest off. It is on by default in the app and should be, but every net here is
    # routed, and leaving the unrouted-connection overlay switched on in a picture of a
    # finished board buries the copper -- which is the one thing these images exist to
    # show -- under the guides that were there to help lay it.
    window.act_ratsnest.setChecked(False)
    _settle(app)
    window.view.fit_board()
    _settle(app)

    shots: list[tuple[str, str]] = []

    window.grab().save(str(OUT_DIR / "editor-component-side.png"))
    shots.append(("editor-component-side.png", "2D editor, component side, routed"))

    # The solder side, where the copper actually is -- and where the far-side hatching
    # earns its place, since the parts are now the things on the other face.
    window.on_flip_board()
    _settle(app)
    window.view.fit_board()
    _settle(app)
    window.grab().save(str(OUT_DIR / "editor-solder-side.png"))
    shots.append(("editor-solder-side.png", "2D editor, solder side"))

    document = window.bus.document
    window.close()
    _settle(app)

    # 3D offscreen, not a grab of the dock -- see this module's docstring.
    lookup = footprint_lookup()
    view3d.render_offscreen(document, lookup, str(OUT_DIR / "board-3d.png"), width=1400, height=950)
    shots.append(("board-3d.png", "3D view, component side"))

    for name, what in shots:
        size = (OUT_DIR / name).stat().st_size
        print(f"{name:32} {size // 1024:5} KB   {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
