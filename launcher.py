"""Frozen-build entry point.

PyInstaller runs its entry script as a top-level module, which breaks the relative
imports inside a package. Going through this file keeps ``perfstudio`` a real package at
runtime.

It exists only for the packaged builds. Running from source goes through the
``perfstudio`` console script that ``pyproject.toml`` declares, which calls the same
``main``.
"""

import io
import sys


def _ensure_streams() -> None:
    """Give the windowed build somewhere to write.

    A frozen windowed process has no console, and PyInstaller sets ``sys.stdout`` and
    ``sys.stderr`` to ``None`` rather than to something inert. Anything that prints then
    raises ``AttributeError`` on ``None.write`` -- and ``perfstudio --version`` prints,
    so it would not print nothing, it would fall over. The command line has its own
    console build (``perfstudio-cli.exe``); this is so that the windowed one cannot be
    taken down by a stray write.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.StringIO())


_ensure_streams()

from perfstudio.ui.main import main  # noqa: E402  - streams first, then imports

if __name__ == "__main__":
    raise SystemExit(main())
