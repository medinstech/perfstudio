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

import ast
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
#:
#: ADJACENT LITERALS COUNT AS ONE, because the interface is full of them: a tooltip is a
#: sentence or two and lives in the source as three quoted fragments on three lines. A
#: scanner that only understood a single literal saw every one of those as untranslated
#: AND reported its catalogue key as stale -- so wrapping a tooltip in t() failed the
#: build, and the tooltips stayed English.
_TRANSLATED = re.compile(
    r"""(?<![\w.])t\(\s*((?:(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*)+)\)"""
)


def translated_literals() -> set[str]:
    found: set[str] = set()
    for source in UI_DIR.glob("*.py"):
        if source.name == "i18n.py":
            continue
        for pieces in _TRANSLATED.findall(source.read_text(encoding="utf-8")):
            # Evaluated rather than sliced out: this is what joins the fragments, and it
            # is also the only thing that turns a \n in the source into the newline the
            # catalogue key actually contains.
            found.add(ast.literal_eval(f"({pieces})"))
    return found


def loop_built_labels() -> set[str]:
    """Labels that reach ``t()`` through a variable, so the literal scan cannot see them.

    Derived from the data that builds them rather than listed by hand: a hand-kept list
    would be one more thing to drift, which is the exact failure this file exists to
    catch. The drawing-tool names are the one set with no data structure behind them yet,
    so they are read out of the source that defines them.
    """
    from perfstudio.ui.boardcolors import SCHEMES

    source = (UI_DIR / "main.py").read_text(encoding="utf-8")
    tools = set(re.findall(r'\(\s*"[a-z-]+",\s*"((?:[^"\\]|\\.)+)",\s*"(?:[^"\\]|\\.)*",\s*$',
                           source, re.MULTILINE))
    # The (value, label) tables the dialogs build their combo boxes from -- pad shape,
    # pad long axis, board edge. Read from the source for the same reason as above: a
    # hand-kept list is the drift this file exists to catch. Material descriptions match
    # this shape too and are deliberately untranslated, which is harmless -- this set only
    # excuses a catalogue key from needing a literal t("..."), it never demands one.
    pairs = set(re.findall(r'\(\s*"[a-z-]+",\s*"((?:[^"\\]|\\.)+)"\s*\),\s*$',
                           source, re.MULTILINE))
    return {scheme.label for scheme in SCHEMES} | tools | pairs


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
    stale = sorted(set(TURKISH) - literals - loop_built_labels())
    assert stale == [], f"catalogue keys the interface no longer uses: {stale}"


def test_the_drawing_tool_labels_are_still_the_ones_the_catalogue_expects() -> None:
    """The one place the scan cannot see, checked against the source of truth instead."""
    source = (UI_DIR / "main.py").read_text(encoding="utf-8")
    for label in ("&Solder Trace", "Solder Trace with S&pine", "&Bare Wire",
                  "&Insulated Wire", "Top &Jumper"):
        assert f'"{label}"' in source, label


def test_translation_coverage_is_reported_rather_than_enforced(capsys) -> None:
    """Missing translations fall through to English on purpose, so this measures rather
    than fails -- but it prints, so the number cannot quietly go to zero.

    The floor was 0.8 while the catalogue covered the menus and nothing else. It is now
    what a complete catalogue leaves room for: every string the interface translates has
    a Turkish entry, and the slack is for the ones that come out the same in both
    languages -- "Net" is the whole list at the time of writing.
    """
    literals = translated_literals()
    covered = literals & set(TURKISH)
    ratio = len(covered) / len(literals) if literals else 1.0
    missing = sorted(literals - covered)
    print(f"\nTurkish covers {len(covered)}/{len(literals)} translated strings ({ratio:.0%})")
    assert ratio > 0.97, f"the Turkish catalogue has fallen behind the interface: {missing}"


#: A user-facing string handed straight to a widget, with no ``t()`` around it.
#:
#: Only the setters whose argument is ALWAYS prose. ``setText`` is deliberately not here:
#: it is the one every status field and every f-string composed of engine output goes
#: through, and a rule that flags those is a rule people turn off.
_UNWRAPPED = re.compile(
    r"""\.(setToolTip|setPlaceholderText|setStatusTip|setHeaderLabels)\(\s*\[?\s*(("(?:[^"\\]|\\.)*")|('(?:[^'\\]|\\.)*'))"""
)

#: Prose has a word in it. Four letters, because the strings that are deliberately left
#: alone are examples of the tool's own vocabulary -- "GND, +5V, OUT…", "R1, C3, U2…",
#: "10k, 100nF, NE555…" -- and the longest alphabetic run in any of them is two. Derived
#: rather than listed, because a hand-kept exception list is the drift this file exists
#: to catch.
_HAS_A_WORD = re.compile(r"[A-Za-z]{4}")


def test_no_tooltip_or_placeholder_is_left_out_of_the_catalogue() -> None:
    """The direction the coverage number cannot see.

    A string never wrapped in ``t()`` is not a missing translation -- it is not in the
    system at all, so it moves no number and nothing reports it. Nearly every tooltip in
    the application was in exactly that state: Turkish menu items with English
    explanations underneath, which is the half a user stops to read.
    """
    offenders: list[str] = []
    for source in sorted(UI_DIR.glob("*.py")):
        if source.name == "i18n.py":
            continue
        text = source.read_text(encoding="utf-8")
        for match in _UNWRAPPED.finditer(text):
            if not _HAS_A_WORD.search(match.group(2)):
                continue
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{source.name}:{line}: {match.group(2)[:60]}")
    assert offenders == [], "user-facing strings not wrapped in t():\n" + "\n".join(offenders)


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
        "file": ["&New Board…", "&Open…", "Open &Recent", "&Save", "Save &As…",
                 "Re&load from Disk", "&Board Setup…", "Board &Features…",
                 "&Import KiCad Netlist…", "Export &Build Guide…", "&Quit"],
        "help": ["&Keyboard Shortcuts…", "Check for &Updates…",
                 "Check Automatically at &Startup", "&About PerfStudio"],
        "edit": ["&Undo", "&Redo", "Cop&y", "&Paste", "Dupl&icate", "Rotate &Clockwise",
                 "Rotate Counter-clock&wise", "&Mirror", "Toggle &Lock", "&Delete",
                 "Proper&ties…"],
        "draw": ["&Solder Trace", "Solder Trace with S&pine", "&Bare Wire",
                 "&Insulated Wire", "Top &Jumper", "&Cut Track",
                 "&Stop the Current Tool"],
        "net": ["&Connect Two Pins", "&New Net…", "&Add Pins to Net", "&Finish Adding Pins",
                "&Edit Net…", "&Disconnect Selected Pins", "De&lete Net"],
        # Route ▸ Preferred Connection. Five items in one submenu, and the one place in the
        # application where a user picks between whole routing strategies -- so an
        # unreachable item here costs them a strategy, not a shortcut.
        "routing style": ["&Try each and keep the best", "&Solder trace where possible",
                          "&Balanced", "&Wire where possible",
                          "Bend component &legs where possible"],
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
# Choosing the language from inside the application
# ---------------------------------------------------------------------------


@pytest.fixture
def stored_language(tmp_path, monkeypatch):
    """A settings store of our own. The real one is the user's registry."""
    from PySide6.QtCore import QSettings

    from perfstudio.ui import main as main_module

    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(main_module, "app_settings", lambda: store)
    return store


def test_the_stored_choice_is_used_when_nothing_overrides_it(stored_language, monkeypatch) -> None:
    """The language could only be chosen by an environment variable or a command-line
    flag -- which is to say, not by anybody running the application normally."""
    from perfstudio.ui.main import LANGUAGE_KEY, _preferred_language

    monkeypatch.delenv("PERFSTUDIO_LANG", raising=False)
    stored_language.setValue(LANGUAGE_KEY, "tr")

    assert _preferred_language(["perfstudio"]) == "tr"


def test_the_flag_beats_the_variable_which_beats_the_stored_choice(
    stored_language, monkeypatch
) -> None:
    """An environment variable is set for this run; a menu choice was made for every run."""
    from perfstudio.ui.main import LANGUAGE_KEY, _preferred_language

    stored_language.setValue(LANGUAGE_KEY, "tr")
    monkeypatch.setenv("PERFSTUDIO_LANG", "en")

    # None hands the question to set_language, which is the one place that reads the
    # variable -- so the two cannot disagree about precedence.
    assert _preferred_language(["perfstudio"]) is None
    assert _preferred_language(["perfstudio", "--lang", "tr"]) == "tr"


def test_nothing_stored_and_nothing_set_asks_the_system(stored_language, monkeypatch) -> None:
    from perfstudio.ui.main import _preferred_language

    monkeypatch.delenv("PERFSTUDIO_LANG", raising=False)

    assert _preferred_language(["perfstudio"]) is None


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
