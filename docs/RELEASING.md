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

**3. Dry-run the release workflow, before the tag exists.**

```sh
gh workflow run release.yml -f dry_run=true
```

It builds and smoke-tests all three installers and publishes nothing. **Do not skip
this.** The tag is the only other thing that runs those jobs, and a tag that fails
halfway leaves a published release with some of its assets and a version number already
spent. v0.4.0's first dry run failed on all three platforms — an unbounded `mcp>=1.0`
had picked up a new major, and a Typer submodule the spec was collecting exited the
interpreter it was being collected in. Neither was visible from a passing local suite.
The macOS job has also failed once on `hdiutil detach` answering "resource busy" after
every check in it had passed, which is a flake, and a flake is exactly what you want to
meet here rather than on the tag.

**4. Verify, commit, tag.**

```sh
pytest                       # test_version.py checks steps 1 and 2 agree
mypy --strict src             # `src` ONLY -- see CONTRIBUTING.md
ruff check src tests          # a gate since 0.5.0; `ruff format` deliberately is not
git commit -am "Release 0.4.0"
git tag -a v0.4.0 -m "PerfStudio 0.4.0"
git push && git push --tags
```

Pushing the tag is what builds the installers: `.github/workflows/release.yml` refuses
to publish unless the tag, `version.py` and `CHANGELOG.md` agree, then builds and
smoke-tests a bundle on each of the three platforms and attaches them to the release.
See [Packaging](#packaging) below.

**5. Open the next cycle.** Set `__version__` to the next `.dev0` (`0.5.0.dev0`) and add
the empty `## [Unreleased]` heading if step 1 did not. Commit as `Open 0.5.0
development`. The empty section is expected here and the suite allows it — it did not
until 0.4.0, which is the first release anybody ran this far.

**6. Install what you published**, on at least one machine, and uninstall it again.
Nothing in CI does this: the jobs unpack the bundle and ask the binary its version, so
the installer *around* it is only ever built. v0.4.0's — the first anyone ran — put its
shortcuts in one profile on a machine-wide install and registered itself in the 32-bit
registry view.

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

## Packaging

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds three bundles from
one PyInstaller spec and attaches them to the release:

| | artefact | built on |
|---|---|---|
| Windows | `PerfStudio_vX.Y.Z_Setup.exe`, an NSIS installer | `windows-latest` |
| Linux | `perfstudio-X.Y.Z-x86_64.AppImage` | `ubuntu-22.04`, deliberately |
| macOS | `perfstudio-X.Y.Z-arm64.dmg` | `macos-15`, Apple silicon only |

Locally, on any of the three:

```sh
pip install -e ".[dev]"
python -m PyInstaller perfstudio.spec --noconfirm    # -> dist/
bash packaging/appimage.sh                            # Linux
bash packaging/macos.sh                               # macOS
makensis packaging/perfstudio.nsi                     # Windows (needs NSIS)
```

Three things about that table are decisions rather than defaults, and each is written out
where it is made:

- **Ubuntu 22.04, not `latest`.** A PyInstaller bundle carries Python and Qt but links
  against the host's glibc, and glibc is forward compatible only. Built on 24.04 it
  requires 2.39, which rules out Debian 12 and every enterprise distribution in service.
- **Apple silicon only.** Every x86-64 macOS runner GitHub offers is a paid larger
  runner; the free Intel image was retired. An Intel Mac installs from source.
- **The version is read, never typed.** The spec, the NSIS script and both shell scripts
  all parse `__version__` out of `version.py`, and the workflow refuses to publish if the
  tag disagrees with it, if the version still carries a `.devN` suffix, or if
  `CHANGELOG.md` has no section for it.

The application icon (`src/perfstudio/ui/assets/`) is generated by `python
tools/make_assets.py` and committed, because the installer and the AppImage's desktop
entry need real files to point at. Regenerate it if the board palette changes.

### Nothing is signed

Both the Windows installer and the macOS bundle ship unsigned, and the release notes say
so along with the click-through each platform requires. This is a cost, not an oversight:
a Windows EV certificate is around $300/year and Apple notarization $99/year (PLAN.md
§12). The hooks are already in place for when that changes — `packaging/perfstudio.nsi`
takes a `/DSIGNCMD=...` and applies it to both the installer and the uninstaller, which
matters because an unsigned uninstaller shows "Unknown Publisher" at the UAC prompt even
when the installer is signed.

The macOS bundle *is* signed ad-hoc, which is not a trust decision — it is the minimum
Apple silicon will execute at all.
