"""Whether this machine can open the offscreen GL context the 3D renders need.

Asked rather than assumed, and asked in a child process, because the failure being
detected is not an exception. VTK does not raise when there is no usable OpenGL
implementation behind the offscreen window -- it takes the interpreter down with it, and
a crash cannot be caught in the process it happens in.

GitHub's Windows runners are exactly that machine. `win.Render()` in
`view3d.render_step_images` ends the pytest process with an access violation, so the
tests after it are not reported as failed; they are not reported at all. The Linux job
does not hit it because it runs the suite under xvfb, and Qt's offscreen platform plugin
is a different thing from a GL context -- `QT_QPA_PLATFORM=offscreen` does not supply
one.

The engine itself never needs this. Only the three paths that put a board through VTK do:
the build guide's step images, the offscreen 3D render, and the 3D panel.
"""

from __future__ import annotations

import functools
import subprocess
import sys

import pytest

#: The smallest program that fails the way the real renders fail: an offscreen window and
#: one Render(), which is the call that dies in `render_step_images`. Deliberately raw
#: VTK -- Qt is not involved in the crash and importing it here would only make the probe
#: slower and its result harder to read.
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
    to the question being asked: the tests that consult it are the ones that cannot run
    without VTK either.
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
