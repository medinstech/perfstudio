# Contributing to PerfStudio

Thank you for looking. This is a pre-alpha, which means the useful contributions are
not only code: a board you tried to design and could not, a rule that fired when it
should not have, or a build guide step that did not match the part in your hand are all
worth an issue.

One thing to read before you write any code is the [licence boundary](#the-licence-boundary).
It is stricter here than in most projects and it is not negotiable.

## Getting set up

Requires Python 3.12+.

```sh
git clone https://github.com/medinstech/perfstudio.git
cd perfstudio
pip install -e ".[dev,mcp]"

pytest                 # the whole suite, ~1231 tests in about 40 seconds
perfstudio             # launch the app on a blank board
```

The suite is fast enough that there is no reason to narrow it except while iterating.
When you do:

```sh
pytest tests/test_router.py            # one file
pytest tests/test_drc.py::test_name -x # one test, stop on first failure
pytest -k "proximity"                  # by name fragment
```

`python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf` renders
2D/3D/PDF into `headless_out/`, runs DRC and LVS and prints timings with no display. It
is the fastest way to check that a rendering change did not crash, and it never edits a
document.

## The checks, and which ones are gates

**`mypy --strict src` — a gate. `src` only, never `src tests`.** The engine is
strict-clean and must stay that way. The tests are not and never have been: `--strict`
over `tests` reports a few hundred errors, nearly all `no-untyped-def` on UI test
helpers. CI gates on `src` alone, deliberately — gating on something already broken
teaches everyone to ignore the red tick.

**Ruff — not a gate, and please do not run `ruff format` casually.** `ruff check src
tests` currently reports a few hundred findings, overwhelmingly `E501` on message strings
and `RUF001` on the Turkish catalogue's dotless `ı`. `ruff format` would rewrite 40 of
the 57 files and point every line of blame in the repository at a reformat. Both are
worth settling; neither is worth settling as a side effect of an unrelated change. The
CI `lint` job is `continue-on-error: true` for exactly this reason.

**The full suite — a gate**, on Linux for every push and on all three platforms for
`main`, tags and manual dispatch.

UI tests run under `QT_QPA_PLATFORM=offscreen`. Qt's offscreen plugin ships no font
database on Windows, so tests that assert on rendered *text* are skipped there — see the
`skipif` guards in `tests/test_ui.py` before adding one.

## Things that will surprise you

These are the conventions that a reasonable change tends to break. Each exists for a
reason that is written down at its definition; this is the short version.

**Every mutation is a command.** `model.py` dataclasses are all `frozen=True, slots=True`
and nothing writes to a document in place. A mutation is a `CommandDefinition` in
`commands.py` dispatched through `CommandBus`. The GUI, the MCP server, the headless CLI
and a replayed journal all go through that one bus — which is what makes undo work across
a mixed human/agent session. Adding a mutation means adding a command, not writing to the
document from the caller.

**Commands enforce integrity; DRC reports quality.** Ids unique, references resolve,
paths on the board → hard error, mutation refused. Overlapping bodies, bridging risk,
inadequate copper → reported by `drc.py`, never refused. When deciding where a new check
belongs, ask whether the result is still a *document* (DRC) or not (command).

**The engine is pure.** `src/perfstudio/` outside `ui/` and `mcp/` has no clock, no RNG,
no filesystem, and no Qt or VTK import. Reading a file is the host's job; timestamps are
stamped by the host. Breaking this breaks the differential proof below.

**Golden fixtures are byte-for-byte.** This Python engine is a port of the TypeScript one
in `packages/`, and the tests assert against its frozen output exactly. In particular,
**a new optional field must be omitted from the JSON when it holds its default** (see
`stripAxis` for the pattern) — a field emitted unconditionally breaks all 15 fixtures.
`persist.py` hand-rolls its JSON writer to match `JSON.stringify(x, null, 2)`, and key
order comes from explicit `*_KEY_ORDER` tuples rather than dict insertion order. Changing
`DEFAULT_ROUTER_COSTS` is a deliberate act with fixture regeneration attached.

**Appearance is a view setting, not a document field.** Solder-mask colour changes nothing
about the circuit, and adding it to `Board` would reopen the byte-for-byte `.perf` format
for a cosmetic preference. `ui/boardcolors.py` owns that sort of thing.

**Adding a menu item means adding its Turkish.** `ui/i18n.py` is a dict whose keys are the
English strings. `tests/test_i18n.py` fails if the catalogue names a string the interface
no longer has, if a translation loses its `&` accelerator, or if two items in one menu
claim the same accelerator. Engine-generated text — DRC/LVS messages, rule ids, hole
addresses, net and component names — is deliberately **not** translated: it is compared
byte-for-byte by fixtures, and the addresses are the tool's vocabulary in every language.

**Versions.** `version.py` is the single source for the application version;
`model.DOCUMENT_FORMAT_VERSION` tracks the `.perf` format and moves only when an older
file needs migrating in order to load, with the migration in the same commit.
`tests/test_version.py` fails if `version.py` and `CHANGELOG.md` disagree in either
direction. The ritual is in [docs/RELEASING.md](./docs/RELEASING.md).

There is a longer tour of the architecture, and of why each of these is the way it is, in
[CLAUDE.md](./CLAUDE.md).

## The licence boundary

PerfStudio is Apache-2.0 and is written **clean-room** with respect to the GPL-licensed
tools in this space.

**Do not read, copy, port or adapt source code from DIY Layout Creator or VeroRoute.**
Their code cannot be incorporated into an Apache-2.0 work. Studying their published
behaviour, screenshots, documentation and user discussion is fine, and is what was done.
One permissively licensed project — striprouter (MIT) — informed the routing approach at
the level of published algorithm description, and is credited.

If you have read GPL source for one of those projects, say so in your pull request. It is
not a disqualification from contributing generally, but it matters for which parts of
this codebase you should touch. The full record is in
[docs/prior-art.md](./docs/prior-art.md), and it is meant to stay auditable.

By contributing you agree that your contributions are licensed under Apache-2.0.

## Pull requests

- Branch from `main`, and keep a pull request to one idea.
- Run `pytest` and `mypy --strict src` before pushing.
- New behaviour needs a test. New *rendering* behaviour needs at least a headless run you
  looked at.
- Add a `CHANGELOG.md` entry under `## [Unreleased]` for anything a user would notice.
- Commit messages here say what the commit *does for the person using the thing*, in the
  imperative — "Stop losing work, and let the board be edited by hand" rather than
  "refactor persist". Match that if you can; it is not a blocker.

## Reporting things

- **Bugs and ideas:** open an issue. For a bug, `perfstudio --version` output and the
  `.perf` file (or the smallest one that still shows it) are worth more than anything else
  you can send.
- **Security:** see [SECURITY.md](./SECURITY.md) — do not open a public issue.
- **Conduct:** see [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).
