"""Tests for the update check's decisions (src/perfstudio/updates.py).

Every question an update check has to answer is in this file, and none of them needs a
network: which release is newer, which file suits this machine, when to look again, and
what the release says about itself. That is the whole reason the module has no I/O in it
-- the interesting failures here are all of the form "offered somebody the wrong file",
and a test that had to stand up a web server to reach one would not be written.

Three of these pin a decision that is easy to reverse by accident and expensive when it
is:

  - test_a_development_build_is_older_than_the_release_it_is_heading_for, because
    ``version.version_tuple()`` deliberately says the opposite and the two are three
    lines apart in the imports.
  - test_an_intel_mac_is_offered_no_download, because there is exactly one macOS asset
    and it is arm64. Matching on the extension alone hands an Intel Mac a bundle that
    cannot start.
  - test_the_newest_version_wins_rather_than_the_newest_publication, because the feed
    arrives in publication order and a patch to an old line published later than a new
    minor would otherwise be offered to everyone as an upgrade.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from perfstudio.updates import (
    RELEASES_PAGE_URL,
    Asset,
    Release,
    asset_for,
    expected_digest,
    highlights,
    is_check_due,
    newer_release,
    parse_releases,
    parse_version,
)

# The shape GitHub actually serves, trimmed to the fields this module reads. Kept as text
# rather than as objects because text is what the host hands over.
FEED = json.dumps(
    [
        {
            "tag_name": "v0.8.0",
            "html_url": "https://github.com/medinstech/perfstudio/releases/tag/v0.8.0",
            "published_at": "2026-09-01T10:00:00Z",
            "draft": False,
            "prerelease": False,
            "body": "### Added\n\n- **Automatic updates.** The installers can now say so.\n"
            "- **A second thing** happened as well.\n",
            "assets": [
                {
                    "name": "PerfStudio_0.8.0_Setup.exe",
                    "browser_download_url": "https://example.invalid/setup.exe",
                    "size": 314_572_800,
                },
                {
                    "name": "perfstudio-0.8.0-x86_64.AppImage",
                    "browser_download_url": "https://example.invalid/app.AppImage",
                    "size": 298_000_000,
                },
                {
                    "name": "perfstudio-0.8.0-arm64.dmg",
                    "browser_download_url": "https://example.invalid/app.dmg",
                    "size": 301_000_000,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": "https://example.invalid/SHA256SUMS",
                    "size": 200,
                },
            ],
        },
        {
            "tag_name": "v0.7.0",
            "html_url": "https://github.com/medinstech/perfstudio/releases/tag/v0.7.0",
            "published_at": "2026-08-25T10:00:00Z",
            "draft": False,
            "prerelease": False,
            "body": "",
            "assets": [],
        },
    ]
)


def a_release(tag: str = "v0.8.0", **fields: object) -> Release:
    defaults: dict[str, object] = {
        "version": tag.lstrip("v"),
        "tag": tag,
        "url": RELEASES_PAGE_URL,
    }
    defaults.update(fields)
    return Release(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.8.0", (0, 8, 0, 1, 0)),
        ("v0.8.0", (0, 8, 0, 1, 0)),
        ("  v1.2.3  ", (1, 2, 3, 1, 0)),
        ("0.8.0.dev4", (0, 8, 0, 0, 4)),
    ],
)
def test_a_version_parses(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "latest", "v0.8", "0.8.0-rc1", "release-2026", "0.8.0.post1"])
def test_a_tag_outside_the_scheme_is_refused_rather_than_guessed_at(text: str) -> None:
    """docs/RELEASING.md defines the scheme; anything else is somebody else's."""
    with pytest.raises(ValueError):
        parse_version(text)


def test_a_development_build_is_older_than_the_release_it_is_heading_for() -> None:
    """The opposite of ``version.version_tuple()``, on purpose.

    That function answers "which feature set is this?" and a .devN deliberately equals
    the release it is heading for. This one answers "is there anything newer than what I
    am running?", and somebody on 0.8.0.dev3 out of a clone should be told when 0.8.0
    ships.
    """
    assert parse_version("0.8.0.dev3") < parse_version("0.8.0")
    assert parse_version("0.8.0.dev3") > parse_version("0.7.9")


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


def test_the_feed_parses_into_releases() -> None:
    releases = parse_releases(FEED)
    assert [release.version for release in releases] == ["0.8.0", "0.7.0"]
    newest = releases[0]
    assert newest.tag == "v0.8.0"
    assert newest.url.endswith("/v0.8.0")
    assert newest.published == "2026-09-01T10:00:00Z"
    assert not newest.prerelease
    assert [asset.name for asset in newest.assets] == [
        "PerfStudio_0.8.0_Setup.exe",
        "perfstudio-0.8.0-x86_64.AppImage",
        "perfstudio-0.8.0-arm64.dmg",
        "SHA256SUMS",
    ]
    assert newest.assets[0].size == 314_572_800


def test_a_draft_is_not_a_release() -> None:
    feed = json.dumps([{"tag_name": "v0.9.0", "draft": True}, {"tag_name": "v0.8.0"}])
    assert [release.tag for release in parse_releases(feed)] == ["v0.8.0"]


def test_a_tag_in_another_scheme_is_skipped_and_the_rest_still_read() -> None:
    """One odd tag in a list is not a reason to stop checking for updates."""
    feed = json.dumps([{"tag_name": "nightly"}, {"tag_name": "v0.8.0"}])
    assert [release.tag for release in parse_releases(feed)] == ["v0.8.0"]


def test_an_asset_with_no_download_url_is_not_an_asset() -> None:
    feed = json.dumps(
        [{"tag_name": "v0.8.0", "assets": [{"name": "notes.txt"}, "nonsense"]}]
    )
    assert parse_releases(feed)[0].assets == ()


@pytest.mark.parametrize(
    "text",
    [
        "<html><body>Sign in to the hotel wi-fi</body></html>",
        '{"message": "API rate limit exceeded"}',
        "",
    ],
)
def test_a_response_that_is_not_a_release_feed_is_an_error_not_an_answer(text: str) -> None:
    """A captive portal must read as "could not check", never as "you are up to date"."""
    with pytest.raises(ValueError):
        parse_releases(text)


# ---------------------------------------------------------------------------
# Which one to offer
# ---------------------------------------------------------------------------


def test_a_newer_release_is_offered() -> None:
    found = newer_release("0.7.0", parse_releases(FEED))
    assert found is not None and found.version == "0.8.0"


def test_the_version_you_are_running_is_not_an_update() -> None:
    assert newer_release("0.8.0", parse_releases(FEED)) is None


def test_a_development_build_is_told_about_the_release_it_is_heading_for() -> None:
    found = newer_release("0.8.0.dev1", parse_releases(FEED))
    assert found is not None and found.version == "0.8.0"


def test_the_newest_version_wins_rather_than_the_newest_publication() -> None:
    """The API returns releases by date; a patch to an old line can be the latest one."""
    feed = json.dumps(
        [
            {"tag_name": "v0.7.1", "published_at": "2026-09-10T00:00:00Z"},
            {"tag_name": "v0.8.0", "published_at": "2026-09-01T00:00:00Z"},
        ]
    )
    found = newer_release("0.7.0", parse_releases(feed))
    assert found is not None and found.version == "0.8.0"


def test_a_prerelease_is_not_offered_unless_it_is_asked_for() -> None:
    feed = json.dumps([{"tag_name": "v0.9.0", "prerelease": True}, {"tag_name": "v0.8.0"}])
    releases = parse_releases(feed)
    stable = newer_release("0.8.0", releases)
    assert stable is None
    early = newer_release("0.8.0", releases, allow_prerelease=True)
    assert early is not None and early.version == "0.9.0"


# ---------------------------------------------------------------------------
# Which file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform,machine,expected",
    [
        ("win32", "AMD64", "PerfStudio_0.8.0_Setup.exe"),
        ("win32", "x86_64", "PerfStudio_0.8.0_Setup.exe"),
        ("linux", "x86_64", "perfstudio-0.8.0-x86_64.AppImage"),
        ("darwin", "arm64", "perfstudio-0.8.0-arm64.dmg"),
    ],
)
def test_each_platform_gets_its_own_installer(
    platform: str, machine: str, expected: str
) -> None:
    asset = asset_for(parse_releases(FEED)[0], platform=platform, machine=machine)
    assert asset is not None and asset.name == expected


def test_an_intel_mac_is_offered_no_download() -> None:
    """The one macOS asset is arm64, and the release notes say "install from source".

    Matching on the extension alone would hand an Intel Mac 300 MB that cannot start,
    which is worse than telling it there is nothing here for it.
    """
    assert asset_for(parse_releases(FEED)[0], platform="darwin", machine="x86_64") is None


def test_an_arm_linux_box_is_offered_no_download() -> None:
    assert asset_for(parse_releases(FEED)[0], platform="linux", machine="aarch64") is None


def test_a_platform_nobody_builds_for_is_offered_no_download() -> None:
    assert asset_for(parse_releases(FEED)[0], platform="freebsd14", machine="x86_64") is None


def test_a_release_with_no_assets_offers_none() -> None:
    assert asset_for(parse_releases(FEED)[1], platform="win32", machine="AMD64") is None


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

DIGEST = "9f" * 32
SUMS = f"{DIGEST}  PerfStudio_0.8.0_Setup.exe\n{'ab' * 32} *perfstudio-0.8.0-arm64.dmg\n"


def test_a_published_digest_is_found_by_asset_name() -> None:
    assert expected_digest(SUMS, "PerfStudio_0.8.0_Setup.exe") == DIGEST


def test_the_binary_mode_asterisk_is_not_part_of_the_name() -> None:
    """coreutils writes ``*name`` for a file it read in binary mode."""
    assert expected_digest(SUMS, "perfstudio-0.8.0-arm64.dmg") == "ab" * 32


def test_an_asset_the_sums_file_does_not_mention_has_no_digest() -> None:
    """Not a failure: releases before 0.7.0 published no SHA256SUMS at all."""
    assert expected_digest(SUMS, "perfstudio-0.8.0-x86_64.AppImage") is None


@pytest.mark.parametrize("junk", ["", "not a checksum file", "zzz  file.exe", "abc file.exe"])
def test_a_line_that_is_not_a_digest_is_not_read_as_one(junk: str) -> None:
    assert expected_digest(junk, "file.exe") is None


# ---------------------------------------------------------------------------
# What to say about it
# ---------------------------------------------------------------------------


def test_the_strip_quotes_the_changelog_rather_than_summarising_it() -> None:
    """release.yml builds the body from CHANGELOG.md, whose entries open in bold."""
    assert highlights(parse_releases(FEED)[0].notes) == (
        "Automatic updates",
        "A second thing",
    )


def test_highlights_stop_at_the_limit() -> None:
    notes = "\n".join(f"- **Thing {n}** and its detail" for n in range(10))
    assert len(highlights(notes)) == 3
    assert len(highlights(notes, limit=5)) == 5


def test_a_release_body_with_no_entries_gets_no_summary() -> None:
    """Rather than a guessed one: the button beside it opens the notes in full."""
    assert highlights("Just some prose about the release.") == ()
    assert highlights("") == ()


# ---------------------------------------------------------------------------
# When to look
# ---------------------------------------------------------------------------


def test_a_machine_that_has_never_looked_looks_now() -> None:
    assert is_check_due(None, "2026-08-25T12:00:00+00:00")
    assert is_check_due("", "2026-08-25T12:00:00+00:00")


def test_a_check_this_morning_is_enough_for_today() -> None:
    assert not is_check_due("2026-08-25T09:00:00+00:00", "2026-08-25T12:00:00+00:00")


def test_a_check_yesterday_is_not() -> None:
    assert is_check_due("2026-08-24T09:00:00+00:00", "2026-08-25T12:00:00+00:00")


def test_the_interval_is_a_parameter() -> None:
    assert is_check_due(
        "2026-08-25T09:00:00+00:00", "2026-08-25T12:00:00+00:00", interval_hours=2
    )


@pytest.mark.parametrize(
    "stored",
    [
        "2026-08-26T09:00:00+00:00",  # a clock that went backwards
        "not a timestamp",  # something else wrote the key
        "2026-08-25T09:00:00",  # naive, where the host now stamps aware
    ],
)
def test_a_stamp_that_cannot_be_trusted_means_check_now(stored: str) -> None:
    """A dead battery or a time zone must not switch update checks off until real time
    catches up."""
    assert is_check_due(stored, "2026-08-25T12:00:00+00:00")


def test_the_release_dataclass_is_frozen_like_everything_else_here() -> None:
    release = a_release()
    with pytest.raises(FrozenInstanceError):
        release.version = "9.9.9"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Asset(name="x", url="y").name = "z"  # type: ignore[misc]
