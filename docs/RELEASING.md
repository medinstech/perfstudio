# Releasing PerfStudio

Four steps, in this order. `tests/test_version.py` enforces the parts that can be
enforced, so a half-done release fails the suite rather than shipping.

## The scheme

`MAJOR.MINOR.PATCH`, [SemVer](https://semver.org/spec/v2.0.0.html), with the pre-1.0
convention that **a minor bump is where breaking changes land**. While the major version
is 0:

| Bump | When |
|---|---|
| **minor** (`0.3.0` → `0.4.0`) | new capability, or anything that breaks a document, a command payload, an MCP tool signature or the CLI |
| **patch** (`0.4.0` → `0.4.1`) | fixes and internals only, nothing a caller can see |
| **major** (`0.x` → `1.0.0`) | once M7 ships and the `.perf` format, the command names and the MCP tool surface are committed to |

Between releases the version carries a `.devN` suffix — `0.4.0.dev0` means "0.4.0 is
being built and has not shipped". The suffix is what tells `test_version.py` to expect
an open `## [Unreleased]` section instead of a closed one.

The `.perf` **document format version** is separate and lives in `model.py`. Bump it
only when an older file needs migrating in order to load, and write the migration in the
same commit. It is at 1.

## Releasing

**1. Close the changelog section.** In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
using today's real date, and add a fresh empty `## [Unreleased]` above it. Update the
link definitions at the bottom of the file.

**2. Bump the version.** In `src/perfstudio/version.py`, drop the `.devN` suffix:

```python
__version__ = "0.4.0"
```

**3. Verify, commit, tag.**

```sh
pytest                       # test_version.py checks steps 1 and 2 agree
mypy --strict src tests
git commit -am "Release 0.4.0"
git tag -a v0.4.0 -m "PerfStudio 0.4.0"
git push && git push --tags
```

**4. Open the next cycle.** Set `__version__` to the next `.dev0` (`0.5.0.dev0`) and add
the empty `## [Unreleased]` heading if step 1 did not. Commit as `Open 0.5.0
development`.

## Why the test is strict in both directions

A version number duplicated across files is a version number that will disagree with
itself, usually inside a released artefact where it is expensive to notice. So there is
exactly one string, in `version.py`, and `pyproject.toml` reads it through
`[tool.setuptools.dynamic]` rather than repeating it.

The changelog check exists for the same reason in the other direction: a release with no
changelog entry is indistinguishable, six months later, from a release that changed
nothing. The test therefore fails if the version is bumped without closing a section
**and** if a section is closed without bumping the version.

## Checking a build

```sh
perfstudio --version
```

prints the app version, the document format version, and the Python and PySide6 it is
running on. That line is what a bug report should quote — "which PerfStudio wrote this
file" and "can this PerfStudio read that file" are different questions, and the second
is the one that produces a confusing failure when it goes unanswered.
