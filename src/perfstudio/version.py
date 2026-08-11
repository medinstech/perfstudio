"""The one place PerfStudio's version number is written down.

Everything else reads it from here: ``pyproject.toml`` through
``[tool.setuptools.dynamic]``, ``perfstudio.__version__``, the window title, the
``--version`` flag and the build guide's own footer. A version number duplicated in two
files is a version number that will disagree with itself, usually in a released
artefact, so this module has exactly one job.

This file deliberately imports nothing from the package. The build backend reads
``__version__`` out of it statically while assembling the wheel -- before dependencies
are installed and before ``perfstudio`` is importable -- and a single top-level import
of anything at all would turn a working build into a confusing one.

There are two version numbers in this project and they move independently:

* **This one** tracks the application. It follows semantic versioning, with the
  pre-1.0 convention that a minor bump is where breaking changes land.
* ``model.DOCUMENT_FORMAT_VERSION`` tracks the ``.perf`` file format, and is bumped
  only when an older document needs migrating in order to load. It has never moved.

The release ritual (bump here, close the changelog section, tag) is written down in
``docs/RELEASING.md`` and enforced by ``tests/test_version.py``, which fails if this
string and ``CHANGELOG.md`` disagree in either direction.
"""

from __future__ import annotations

#: PEP 440 / SemVer. A ``.devN`` suffix means "not released yet": the changelog's
#: Unreleased section describes what is accumulating towards it.
__version__ = "0.4.0.dev0"


def version_tuple() -> tuple[int, int, int]:
    """``(major, minor, patch)``, with any ``.devN`` suffix dropped.

    For comparing versions. ``"0.4.0.dev0" -> (0, 4, 0)``, deliberately equal to the
    release it is heading for rather than sorting below it, because the question this
    answers is "which feature set is this?" and not "which artefact is newer?".
    """
    major, minor, patch = release_version().split(".")
    return int(major), int(minor), int(patch)


def release_version() -> str:
    """The version this build is heading for, without the ``.devN`` suffix."""
    return __version__.split(".dev")[0]


def is_development_build() -> bool:
    """Whether this is a working tree between releases rather than a released version."""
    return ".dev" in __version__


def describe() -> str:
    """One line naming the version, both Python and Qt, and the document format.

    This is what ``--version`` prints and what a bug report should quote. The document
    format version is included because "which PerfStudio wrote this file" and "can this
    PerfStudio read that file" are different questions, and the second one is the one
    that produces a confusing failure when it goes unanswered.
    """
    import platform
    import sys

    # Imported here, not at module scope: see this module's docstring -- the build
    # backend reads __version__ out of this file before the package can be imported.
    from perfstudio.model import DOCUMENT_FORMAT_VERSION

    try:
        from PySide6 import __version__ as qt_version
    except Exception:  # pragma: no cover - a headless engine-only install is legitimate
        qt_version = "not installed"

    # ASCII only, deliberately. This line's whole purpose is to be pasted into a bug
    # report, and a Windows console at cp1252 turns a nice typographic separator into a
    # question mark -- which then travels into the report as evidence of a bug that
    # isn't there.
    return (
        f"PerfStudio {__version__}"
        f" (document format {DOCUMENT_FORMAT_VERSION})"
        f" [Python {platform.python_version()} on {sys.platform}, PySide6 {qt_version}]"
    )
