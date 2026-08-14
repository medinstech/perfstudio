"""The version number and the changelog must agree, in both directions.

A version written down in two places disagrees with itself sooner or later, and it
usually happens inside a released artefact where it is expensive to notice. So the
string lives in exactly one file and everything else derives from it -- and the tests
here are what make that claim true rather than merely intended.

The changelog check runs the other way for the same reason. A release with no changelog
entry is indistinguishable, six months later, from a release that changed nothing. So
this file fails if the version is bumped without closing a changelog section, AND if a
section is closed without bumping the version.

The release ritual these tests enforce is written out in docs/RELEASING.md.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

import perfstudio
from perfstudio.model import DOCUMENT_FORMAT_VERSION
from perfstudio.version import (
    __version__,
    describe,
    is_development_build,
    release_version,
    version_tuple,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASING = REPO_ROOT / "docs" / "RELEASING.md"

#: PEP 440 for the dev suffix, SemVer for the rest. Anything else -- a "v" prefix, a
#: date, a four-part version -- is rejected rather than tolerated, because tooling that
#: sorts versions will disagree about what an unusual one means.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$")

#: "## [0.3.0] - 2026-08-11". The date is required and must be a real ISO date, since a
#: released section with a placeholder date is the commonest way this file rots.
RELEASE_HEADING_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$")

UNRELEASED_HEADING = "## [Unreleased]"

#: Keep a Changelog 1.1.0, in the order that specification lists them. Restricted
#: deliberately: "Notes" is ours and is for things that are neither a change to the
#: software nor invisible, such as a recorded divergence from the reference engine.
#:
#: A tuple rather than a set because the order is checked as well as the membership --
#: see test_subsections_are_in_keep_a_changelog_order.
SUBSECTION_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Notes")

ALLOWED_SUBSECTIONS = frozenset(SUBSECTION_ORDER)


@dataclass(frozen=True)
class Section:
    """One ``## [x.y.z] - date`` heading and the lines beneath it."""

    version: str
    date: str
    body: list[str]


def _changelog_lines() -> list[str]:
    return CHANGELOG.read_text(encoding="utf-8").splitlines()


def _released_sections() -> list[Section]:
    """Every released section, in the order the file lists them."""
    sections: list[Section] = []
    current: Section | None = None
    for line in _changelog_lines():
        match = RELEASE_HEADING_RE.match(line)
        if match:
            current = Section(version=match.group(1), date=match.group(2), body=[])
            sections.append(current)
        elif line.startswith("## "):
            current = None  # Unreleased, or a prose heading such as "Before 0.1.0".
        elif current is not None:
            current.body.append(line)
    return sections


def _unreleased_body() -> list[str]:
    body: list[str] = []
    collecting = False
    for line in _changelog_lines():
        if line == UNRELEASED_HEADING:
            collecting = True
            continue
        if collecting and line.startswith("## "):
            break
        if collecting:
            body.append(line)
    return body


def _subsections() -> dict[str, list[str]]:
    """Each ``## `` heading mapped to the ``### `` headings beneath it, in file order."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in _changelog_lines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif line.startswith("### ") and current is not None:
            sections[current].append(line[4:].strip())
    return sections


def _has_content(body: list[str]) -> bool:
    """Whether a section says anything, as opposed to holding blank lines."""
    return any(line.strip() for line in body)


# ---------------------------------------------------------------------------
# The version string itself
# ---------------------------------------------------------------------------


def test_version_is_wellformed() -> None:
    assert VERSION_RE.match(__version__), f"{__version__!r} is not MAJOR.MINOR.PATCH[.devN]"


def test_package_reexports_the_version() -> None:
    """``perfstudio.__version__`` is what every other Python package trains people to try."""
    assert perfstudio.__version__ == __version__


def test_release_version_drops_the_dev_suffix() -> None:
    assert release_version() == __version__.split(".dev")[0]
    assert ".dev" not in release_version()


def test_version_tuple_matches_the_string() -> None:
    assert ".".join(str(part) for part in version_tuple()) == release_version()


def test_is_development_build_agrees_with_the_suffix() -> None:
    assert is_development_build() == (".dev" in __version__)


def test_describe_names_both_version_numbers() -> None:
    """--version output has to answer "can this build read that file", not just "which build"."""
    line = describe()
    assert __version__ in line
    assert f"document format {DOCUMENT_FORMAT_VERSION}" in line


def test_describe_survives_without_qt() -> None:
    """The engine is usable headless; describe() must not require a GUI toolkit."""
    # Not a mock: it simply must not raise on this machine either way, and the branch
    # for a missing PySide6 is exercised by reading the string it produces.
    assert "PySide6" in describe()


# ---------------------------------------------------------------------------
# version.py is the single source
# ---------------------------------------------------------------------------


def test_pyproject_derives_the_version_from_the_package() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    assert "version" not in project, "pyproject must not repeat the version; see version.py"
    assert project["dynamic"] == ["version"]
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "perfstudio.version.__version__"


def test_version_module_imports_nothing_at_module_scope() -> None:
    """The build backend reads __version__ out of this file before the package exists.

    A single top-level import of anything at all would turn a working build into a
    confusing one, so the constraint is checked rather than left to a comment.
    """
    source = (REPO_ROOT / "src" / "perfstudio" / "version.py").read_text(encoding="utf-8")
    module_scope = [
        line
        for line in source.splitlines()
        if (line.startswith("import ") or line.startswith("from "))
        and "__future__" not in line
    ]
    assert module_scope == [], f"version.py imports at module scope: {module_scope}"


# ---------------------------------------------------------------------------
# The changelog
# ---------------------------------------------------------------------------


def test_changelog_exists_and_starts_with_unreleased() -> None:
    lines = _changelog_lines()
    headings = [line for line in lines if line.startswith("## ")]
    assert headings, "CHANGELOG.md has no sections"
    assert headings[0] == UNRELEASED_HEADING, (
        f"the first section must be {UNRELEASED_HEADING!r}, found {headings[0]!r}"
    )


def test_every_bracketed_heading_is_a_wellformed_release() -> None:
    for line in _changelog_lines():
        if line.startswith("## [") and line != UNRELEASED_HEADING:
            assert RELEASE_HEADING_RE.match(line), f"malformed release heading: {line!r}"


def test_releases_are_listed_newest_first() -> None:
    versions = [tuple(int(p) for p in s.version.split(".")) for s in _released_sections()]
    assert versions == sorted(versions, reverse=True), f"out of order: {versions}"
    assert len(set(versions)) == len(versions), "a version is listed twice"


def test_releases_are_dated_newest_first() -> None:
    dates = [s.date for s in _released_sections()]
    assert dates == sorted(dates, reverse=True), f"release dates go backwards: {dates}"


def test_no_released_section_is_empty() -> None:
    for section in _released_sections():
        assert _has_content(section.body), f"{section.version} has no entries"


def test_subsection_headings_are_from_the_agreed_set() -> None:
    for line in _changelog_lines():
        if line.startswith("### "):
            name = line[4:].strip()
            assert name in ALLOWED_SUBSECTIONS, (
                f"unknown changelog subsection {name!r}; allowed: {sorted(ALLOWED_SUBSECTIONS)}"
            )


def test_no_section_repeats_a_subsection() -> None:
    """A version's section is published verbatim as that release's notes.

    ``release.yml`` cuts the section for the tag being built out of this file and hands
    it to ``gh release create``, so a section carrying two ``### Added`` blocks with a
    ``### Changed`` between them ships a release page that reads as though its notes were
    assembled by accident. Entries accumulate here over weeks, which is exactly how that
    happens -- so it is checked rather than noticed at the tag.
    """
    for section, names in _subsections().items():
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, f"[{section}] repeats {duplicates}; its headings are {names}"


def test_subsections_are_in_keep_a_changelog_order() -> None:
    """Added before Changed before Fixed, as the format this file claims to follow says."""
    for section, names in _subsections().items():
        ranked = [SUBSECTION_ORDER.index(name) for name in names if name in SUBSECTION_ORDER]
        assert ranked == sorted(ranked), (
            f"[{section}] lists {names}; expected them in the order {list(SUBSECTION_ORDER)}"
        )


def test_every_referenced_section_has_a_link_definition() -> None:
    """The bracketed headings are links; a heading with no definition renders as literal
    brackets, which is how this file quietly stops being readable on GitHub."""
    text = CHANGELOG.read_text(encoding="utf-8")
    labels = ["Unreleased"] + [s.version for s in _released_sections()]
    for label in labels:
        assert f"\n[{label}]: " in text, f"no link definition for [{label}]"


# ---------------------------------------------------------------------------
# The two must agree -- this is the pair that enforces the release ritual
# ---------------------------------------------------------------------------


def test_development_builds_have_an_open_unreleased_section() -> None:
    """A development version must not be describing a version that already shipped.

    It used to require the Unreleased section to have entries in it as well, which
    contradicted the ritual the same tests exist to enforce: docs/RELEASING.md step 4
    opens the next cycle with an EMPTY Unreleased heading, and nothing has accumulated
    towards it yet by definition. The contradiction survived three releases because it
    can only be reached by finishing one, and 0.4.0 was the first that was.

    Dropping it costs nothing that matters. The direction worth protecting is that a
    RELEASE documents itself, and that is held from the other side:
    `test_no_released_section_is_empty` refuses a closed section with no entries, and
    `test_released_builds_match_the_newest_changelog_section` refuses a version whose
    section is not the newest one. Neither can be satisfied by forgetting to write
    anything down.
    """
    if not is_development_build():
        pytest.skip("this is a released version; the released-build check covers it")
    released = {s.version for s in _released_sections()}
    assert release_version() not in released, (
        f"{release_version()} is already a released section, but version.py still says "
        f"{__version__}. Open the next cycle: see docs/RELEASING.md step 4."
    )


def test_released_builds_match_the_newest_changelog_section() -> None:
    if is_development_build():
        pytest.skip("this is a development build; the development check covers it")
    newest = _released_sections()[0]
    assert newest.version == __version__, (
        f"version.py says {__version__} but the newest changelog section is "
        f"{newest.version}. See docs/RELEASING.md."
    )
    assert not _has_content(_unreleased_body()), (
        "a released build must have an empty Unreleased section; move its entries into "
        f"the {__version__} section"
    )


def test_the_next_version_is_ahead_of_every_released_one() -> None:
    released = [tuple(int(p) for p in s.version.split(".")) for s in _released_sections()]
    if released:
        assert version_tuple() >= max(released), (
            f"version.py ({__version__}) is behind the newest changelog release"
        )


def test_the_release_procedure_is_written_down() -> None:
    """Enforcement is worth nothing if the thing being enforced is undocumented."""
    assert RELEASING.exists()
    text = RELEASING.read_text(encoding="utf-8")
    for required in ("version.py", "CHANGELOG.md", "git tag"):
        assert required in text
