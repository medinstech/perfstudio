"""Whether this machine can open the offscreen GL context the 3D renders need.

Asked rather than assumed, and asked in a child process, because the failure being
detected is not an exception. VTK does not raise when there is no usable OpenGL
implementation behind an offscreen window -- it takes the interpreter down with it, and
a crash cannot be caught in the process it happens in.

GitHub's Windows runners are exactly that machine. `win.Render()` in
`view3d.render_step_images` ends the pytest process with an access violation, and the
tests after it are not reported as failed; they are not reported at all -- a suite of
1262 tests went quiet after about 900 of them and the summary line never printed. The
Linux job does not hit it because it runs under xvfb, and Qt's offscreen platform plugin
is a different thing from a GL context: `QT_QPA_PLATFORM=offscreen` does not supply one.

The engine never needs any of this. Only the paths that put a board through VTK do: the
build guide's step images and the offscreen 3D render.

This module also holds the guard that keeps the list of such tests honest. Marking them
by hand found three on the first pass and missed two, which cost a full CI round each --
so `test_every_vtk_touching_test_is_marked` now finds them by reading the sources
instead.
"""

from __future__ import annotations

import ast
import functools
import subprocess
import sys
from pathlib import Path

import pytest

#: The smallest program that fails the way the real renders fail: an offscreen window and
#: one Render(), which is the call that dies in `render_step_images`. Deliberately raw
#: VTK -- Qt is not involved in the crash, and importing it here would only make the
#: probe slower and its answer harder to read.
_PROBE = """
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  -- registers the OpenGL backend
from vtkmodules.vtkRenderingCore import vtkRenderer, vtkRenderWindow

window = vtkRenderWindow()
window.SetOffScreenRendering(1)
window.SetSize(16, 16)
window.AddRenderer(vtkRenderer())
window.Render()
"""


@functools.cache
def offscreen_gl_available() -> bool:
    """True if a child process can open an offscreen GL context and render one frame.

    A VTK that will not import at all answers False here too, which is the right answer
    to the question being asked: the tests that consult it cannot run without VTK either.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - environment-specific
        return False
    return completed.returncode == 0


#: Applied to the tests that put a document through VTK. Everything else in the suite,
#: including every 2D render, runs on any machine.
requires_offscreen_gl = pytest.mark.skipif(
    not offscreen_gl_available(),
    reason="no offscreen GL context here; VTK would abort the process rather than raise",
)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

#: Calls that reach `win.Render()`. `render_step_images` and `render_offscreen` are the
#: only two functions in the codebase that open a render window; the other two are the
#: paths that reach them, and are named because a test calls those rather than these.
_RENDERS_ALWAYS = frozenset({"render_step_images", "render_offscreen", "on_export_guide"})

#: `generate_guide()` renders only when it is given somewhere to write the pictures to.
#: The no-argument form answers "is this board buildable" and touches no GL, which is a
#: distinction worth keeping: it is the form an agent uses most.
_RENDERS_WITH_ARGUMENTS = frozenset({"generate_guide"})

_MARK = "requires_offscreen_gl"


def _called_names(node: ast.AST) -> set[str]:
    """Every function name called anywhere inside ``node``, however it is qualified."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name is None:
            continue
        if name in _RENDERS_ALWAYS or (name in _RENDERS_WITH_ARGUMENTS and child.args):
            names.add(name)
    return names


def test_every_vtk_touching_test_is_marked() -> None:
    """A test that renders and is not marked does not fail on a GL-less machine.

    It ABORTS, taking the rest of the session with it, so the cost of missing one is not
    a red test -- it is a run that stops reporting partway through and a summary line
    that never appears. That is worth a static check rather than a convention.
    """
    unmarked: list[str] = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            renders = _called_names(node)
            if not renders:
                continue
            decorators = {
                d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")
                for d in node.decorator_list
            }
            if _MARK not in decorators:
                unmarked.append(f"{path.name}::{node.name} calls {sorted(renders)}")

    assert not unmarked, (
        "these tests open a GL context and would abort the whole session on a machine "
        f"without one; decorate each with @{_MARK}:\n  " + "\n  ".join(unmarked)
    )
