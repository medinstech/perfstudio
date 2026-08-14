"""Whether this machine can open the offscreen GL context the 3D renders need.

The probe itself lives in the application (`ui/view3d.offscreen_gl_available`), not here,
and that is the point: the suite and the program have to agree about what "this machine
cannot render" means, or the tests skip on a machine where the application would have
crashed and pass on one where it would not.

WHY IT IS A CHILD PROCESS. VTK does not raise when there is no usable OpenGL behind an
offscreen window -- it ends the process. On GitHub's Windows runners `win.Render()` in
`render_step_images` killed pytest with an access violation, and the ~360 tests after it
were not reported as failed; they were not reported at all, with no summary line. A crash
cannot be caught where it happens, so it is spent somewhere it costs nothing.

Qt's offscreen platform plugin is a different thing from a GL context:
`QT_QPA_PLATFORM=offscreen` does not supply one, which is why the Linux CI job runs the
suite under xvfb rather than relying on it.

This module also holds the guard that keeps the marked list honest. Marking those tests
by hand found three on the first pass and missed two, at a full CI round each, so
`test_every_vtk_touching_test_is_marked` finds them by reading the sources instead.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

from perfstudio.ui.view3d import PROBE_FLAG, offscreen_gl_available

#: Applied to the tests that put a document through VTK. Everything else in the suite,
#: including every 2D render, runs on any machine.
requires_offscreen_gl = pytest.mark.skipif(
    not offscreen_gl_available(),
    reason="no offscreen GL context here; VTK would abort the process rather than raise",
)


def test_the_probe_flag_answers_and_does_not_start_the_application() -> None:
    """The child must exit on its own, whatever the answer is.

    A probe that opened a window, or waited for one, would hang the export it was
    supposed to protect -- and it would hang it on precisely the machines that cannot
    render, which are the ones being protected. `main` routes the flag before Qt is
    touched for that reason, and this is what would notice if it stopped doing so.
    """
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "perfstudio.ui.main", PROBE_FLAG],
        capture_output=True,
        timeout=180,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 120, f"the probe took {elapsed:.0f}s; it is meant to be one frame"
    # Zero where there is a context, anything else where there is not -- including the
    # abort itself, which is the answer arriving the hard way and is still an answer.
    assert (completed.returncode == 0) == offscreen_gl_available()


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
    """Every rendering function called anywhere inside ``node``, however it is qualified."""
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
