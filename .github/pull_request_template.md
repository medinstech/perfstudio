<!--
Thanks for the pull request. CONTRIBUTING.md has the details; this is the short list.
Delete anything that does not apply.
-->

## What this changes

<!-- What does it do for someone using PerfStudio? One or two sentences. -->

## Why

<!-- The problem, or the issue number it closes. -->

## Checks

- [ ] `pytest` passes
- [ ] `mypy --strict src` passes (`src` only — never `src tests`)
- [ ] New behaviour has a test; new *rendering* behaviour has a headless run I looked at
      (`python -m perfstudio.ui.main --headless tools/diffcheck/golden/dense.perf`)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`, if a user would notice this
- [ ] New or changed UI strings have Turkish entries in `ui/i18n.py`

## Things this repository is fussy about

<!-- Tick only what applies; each one has bitten before. -->

- [ ] This adds a **mutation**, and it is a command dispatched through `CommandBus` rather
      than a write to the document
- [ ] This adds an **optional model field**, and it is omitted from the JSON when it holds
      its default (otherwise every golden fixture breaks)
- [ ] This changes **golden output** (`DEFAULT_ROUTER_COSTS`, footprint arithmetic,
      serialisation), and the fixtures were regenerated deliberately
- [ ] I have **not** read or adapted source code from the GPL-licensed tools in this space
      (see CONTRIBUTING.md § The licence boundary). If you have, say so here rather than
      ticking this.
