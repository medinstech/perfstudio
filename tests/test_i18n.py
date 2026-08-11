"""Tests for the interface translation (src/perfstudio/ui/i18n.py).

A translation catalogue rots in one specific way: the interface changes, the catalogue
does not, and nobody notices until someone runs the application in that language and
finds half a menu in English. So the load-bearing test here is not "does t() return the
Turkish" -- it is test_the_catalogue_names_no_string_the_interface_no_longer_has, which
scans the UI source for every translated literal and fails when the two drift apart.

The other direction is deliberately NOT an error. A string with no translation falls
through to English, because a half-translated interface is usable and a placeholder in
the middle of a menu is not. It is reported as a coverage number instead, so it can be
seen without failing a build over it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from perfstudio.ui.i18n import AVAILABLE, CATALOGUES, TURKISH, language, set_language, t

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "perfstudio" / "ui"

#: Every string literal wrapped in t("...") anywhere in the UI.
#:
#: The lookbehind matters more than it looks: without it this also matches the tail of
#: ``by_number.get("1")``, and the engine-purity test below then reports guide.py as
#: translating a string. Caught by that test failing on a file that does no such thing.
#: Both quote styles, because inside an f-string the inner literal has to be the other
#: one -- and a scanner that only saw double quotes reported those strings as stale
#: catalogue entries when they were in use two lines away.
_TRANSLATED = re.compile(
    r"""(?<![\w.])t\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*\)"""
)


def translated_literals() -> set[str]:
    found: set[str] = set()
    for source in UI_DIR.glob("*.py"):
        if source.name == "i18n.py":
            continue
        for double, single in _TRANSLATED.findall(source.read_text(encoding="utf-8")):
            found.add(double or single)
    return found


@pytest.fixture(autouse=True)
def _english_again():
    """Language is process-wide state, so no test may leave it changed."""
    before = language()
    yield
    set_language(before)


# ---------------------------------------------------------------------------
# The catalogue and the interface may not drift apart
# ---------------------------------------------------------------------------


def test_the_catalogue_names_no_string_the_interface_no_longer_has() -> None:
    """The load-bearing test. A key for a string that has been reworded or removed is a
    translation that will never appear, and nothing else would ever say so."""
    literals = translated_literals()
    # Labels built in a loop reach t() through a variable rather than a literal, so they
    # are named here explicitly. Keep this list honest: a name that is no longer a
    # drawing tool should fail this test, not be excused by it.
    from perfstudio.ui.main import MainWindow  # noqa: PLC0415 - Qt import kept local

    del MainWindow
    loop_built = {
        "&Solder Trace",
        "Solder Trace with S&pine",
        "&Bare Wire",
        "&Insulated Wire",
        "Top &Jumper",
    }
    stale = sorted(set(TURKISH) - literals - loop_built)
    assert stale == [], f"catalogue keys the interface no longer uses: {stale}"


def test_the_drawing_tool_labels_are_still_the_ones_the_catalogue_expects() -> None:
    """The one place the scan cannot see, checked against the source of truth instead."""
    source = (UI_DIR / "main.py").read_text(encoding="utf-8")
    for label in ("&Solder Trace", "Solder Trace with S&pine", "&Bare Wire",
                  "&Insulated Wire", "Top &Jumper"):
        assert f'"{label}"' in source, label


def test_translation_coverage_is_reported_rather_than_enforced(capsys) -> None:
    """Missing translations fall through to English on purpose, so this measures rather
    than fails -- but it prints, so the number cannot quietly go to zero."""
    literals = translated_literals()
    covered = literals & set(TURKISH)
    ratio = len(covered) / len(literals) if literals else 1.0
    print(f"\nTurkish covers {len(covered)}/{len(literals)} translated strings ({ratio:.0%})")
    assert ratio > 0.8, "the Turkish catalogue has fallen behind the interface"


def test_every_catalogue_translates_to_something_different() -> None:
    """A key mapped to itself is a translation somebody forgot to finish."""
    for code, catalogue in CATALOGUES.items():
        same = sorted(key for key, value in catalogue.items() if key == value)
        # "DRC / LVS" is an acronym pair and is the same in both languages.
        assert same == ["DRC / LVS"], f"{code} has untranslated entries: {same}"


def test_accelerators_survive_translation() -> None:
    """Qt's & marks the keyboard accelerator. A translation that loses it loses the
    keyboard shortcut with it, silently."""
    for code, catalogue in CATALOGUES.items():
        for key, value in catalogue.items():
            if "&" in key:
                assert "&" in value, f"{code}: {key!r} lost its accelerator"


def test_no_two_entries_in_a_menu_claim_the_same_accelerator() -> None:
    """Two items with the same & letter in one menu means one of them cannot be reached."""
    from collections import Counter

    groups = {
        "file": ["&New Board…", "&Open…", "&Save", "Save &As…", "&Board Setup…",
                 "&Import KiCad Netlist…", "Export &Build Guide…", "&Quit"],
        "edit": ["&Undo", "&Redo", "Rotate &Clockwise", "Rotate Counter-clock&wise",
                 "&Mirror", "Toggle &Lock", "&Delete"],
        "draw": ["&Solder Trace", "Solder Trace with S&pine", "&Bare Wire",
                 "&Insulated Wire", "Top &Jumper", "&Stop Drawing"],
    }
    for name, keys in groups.items():
        for code, catalogue in CATALOGUES.items():
            letters = [
                catalogue.get(key, key).split("&", 1)[1][0].lower()
                for key in keys
                if "&" in catalogue.get(key, key)
            ]
            clashes = [letter for letter, n in Counter(letters).items() if n > 1]
            assert not clashes, f"{code} {name} menu: duplicate accelerators {clashes}"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_english_is_the_source_language_and_needs_no_catalogue() -> None:
    set_language("en")
    assert t("&File") == "&File"
    assert "en" not in CATALOGUES


def test_turkish_translates() -> None:
    set_language("tr")
    assert t("&File") == "&Dosya"
    assert t("Columns") == "Sütun"


def test_an_untranslated_string_falls_through_to_english() -> None:
    set_language("tr")
    assert t("a string nobody has translated") == "a string nobody has translated"


def test_an_unknown_language_falls_back_rather_than_failing() -> None:
    """A misspelled environment variable must not stop the application starting."""
    assert set_language("klingon") == "en"
    assert t("&File") == "&File"


def test_a_regional_code_selects_its_base_language() -> None:
    assert set_language("tr_TR") == "tr"
    assert set_language("tr-TR.UTF-8") == "tr"


def test_the_environment_variable_is_consulted(monkeypatch) -> None:
    monkeypatch.setenv("PERFSTUDIO_LANG", "tr")
    assert set_language(None) == "tr"


def test_an_explicit_choice_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PERFSTUDIO_LANG", "tr")
    assert set_language("en") == "en"


def test_the_command_line_flag_is_parsed_both_ways() -> None:
    from perfstudio.ui.main import _language_argument

    assert _language_argument(["perfstudio", "--lang", "tr"]) == "tr"
    assert _language_argument(["perfstudio", "--lang=tr"]) == "tr"
    assert _language_argument(["perfstudio", "board.perf"]) is None
    # A trailing --lang with nothing after it must not raise.
    assert _language_argument(["perfstudio", "--lang"]) is None


def test_available_lists_exactly_what_can_be_selected() -> None:
    assert set(AVAILABLE) == {"en", *CATALOGUES}


# ---------------------------------------------------------------------------
# What is deliberately not translated
# ---------------------------------------------------------------------------


def test_hole_addresses_and_rule_ids_are_not_translated() -> None:
    """The addresses are the tool's vocabulary and are the same in every language; the
    rule ids are identifiers that appear in saved files and in MCP output."""
    set_language("tr")
    for untouchable in ("A1", "C7", "AC12", "solder-trace-proximity", "component-off-board"):
        assert t(untouchable) == untouchable


def test_the_engine_carries_no_translation_calls() -> None:
    """The engine has no UI dependency, and its DRC and LVS messages are compared byte
    for byte against golden fixtures dumped from the reference implementation. A
    translation call in there would break the differential proof."""
    engine = Path(__file__).resolve().parents[1] / "src" / "perfstudio"
    for source in engine.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "from .ui" not in text, f"{source.name} imports the UI"
        assert not _TRANSLATED.search(text), f"{source.name} translates a string"
