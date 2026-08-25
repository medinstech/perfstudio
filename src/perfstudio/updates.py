"""What a release feed says, and what to do about it.

PLAN.md §14 asks for automatic updates and the three installers have never had them: an
update meant noticing a release on GitHub and fetching it by hand, which is a thing
nobody does on a schedule. This module is the half of that answer which can be settled
without a network -- given the JSON GitHub serves for ``/releases`` and the version this
build believes it is: which release is newer, which file on it is the one for *this*
machine, and what to say about it.

**Nothing here touches the network, the clock or the disk.** That is the rule the rest of
the engine follows -- ``persist.py`` turns documents into strings and back while the host
reads and writes the file, ``parsers/`` maps text to data and opens nothing -- and it is
what lets every interesting decision in an update check be tested by handing a function a
string. ``ui/updater.py`` is the host here: it fetches, it saves, it hashes what it
saved, and it stamps the clock.

``datetime`` is imported to PARSE a timestamp, which is not the same as reading one.
There is no ``now()`` in this file; :func:`is_check_due` is handed the current time by
its caller, exactly as ``meta.modified`` is stamped by the host rather than by the
engine.

TWO THINGS THIS MODULE REFUSES TO GUESS AT, both because a wrong guess ends with somebody
downloading three hundred megabytes that cannot run on their machine:

* **A tag it does not understand is skipped, not coerced.** ``docs/RELEASING.md`` defines
  the scheme as ``MAJOR.MINOR.PATCH`` with an optional ``.devN``; anything else is
  somebody else's convention and this checker has no business ranking it.
* **A platform with no asset gets no download offer.** The arm64 disk image is the only
  macOS build there is, and the release notes tell an Intel Mac to install from source.
  Offering that user the ``.dmg`` would be an update button that produces a broken app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from json import JSONDecodeError, loads
from typing import Any

#: Where the releases live. The API host and the human one are different machines and
#: neither is derivable from the other, so both are written down.
GITHUB_REPOSITORY = "medinstech/perfstudio"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"

#: The checksum file ``release.yml`` attaches to every release from 0.7.0 onwards. A
#: release older than that has none, and a download from one is reported as unverified
#: rather than as failed -- see :func:`expected_digest`.
CHECKSUM_ASSET = "SHA256SUMS"

#: How long an automatic check waits before asking again. A day: this project releases
#: every few weeks, and an application that phones home on every launch is one people
#: switch off.
DEFAULT_INTERVAL_HOURS = 24


@dataclass(frozen=True, slots=True)
class Asset:
    """One downloadable file attached to a release."""

    name: str
    url: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class Release:
    """One published release, in the terms an update check needs.

    ``notes`` is the release body, which ``release.yml`` builds out of this project's own
    changelog section -- so :func:`highlights` reads it knowing the shape it is in.
    """

    version: str
    tag: str
    url: str
    notes: str = ""
    published: str = ""
    prerelease: bool = False
    assets: tuple[Asset, ...] = field(default_factory=tuple)


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$")


def parse_version(text: str) -> tuple[int, int, int, int, int]:
    """``"v0.8.0"`` or ``"0.8.0.dev3"`` as something sortable, or raise ``ValueError``.

    The fourth element is 0 for a development build and 1 for a release, so that
    ``0.8.0.dev3 < 0.8.0`` -- which is the ordering an update check needs, and the
    OPPOSITE of ``version.version_tuple()``, where a ``.devN`` deliberately equals the
    release it is heading for. The two answer different questions: that one asks "which
    feature set is this?", this one asks "is there anything newer than what I am?".
    Somebody running 0.8.0.dev3 out of a clone should be told when 0.8.0 ships.
    """
    match = _VERSION.match(text.strip())
    if match is None:
        raise ValueError(f"not a PerfStudio version: {text!r}")
    major, minor, patch, dev = match.groups()
    if dev is None:
        return int(major), int(minor), int(patch), 1, 0
    return int(major), int(minor), int(patch), 0, int(dev)


def parse_releases(text: str) -> tuple[Release, ...]:
    """GitHub's ``/releases`` JSON as :class:`Release` records, newest version first.

    Raises ``ValueError`` if the response is not the array of objects that endpoint
    documents -- which is what a captive portal serving a login page looks like, and the
    host reports that as "could not check" rather than as "you are up to date". A single
    entry that is malformed, a draft, or tagged in some other scheme is dropped quietly:
    one odd tag in the list is not a reason to stop checking for updates.
    """
    try:
        payload: Any = loads(text)
    except JSONDecodeError as exc:
        raise ValueError(f"the release feed is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"the release feed is not a list of releases: {type(payload).__name__}")

    releases: list[Release] = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        tag = str(entry.get("tag_name") or "")
        try:
            parse_version(tag)
        except ValueError:
            continue
        releases.append(
            Release(
                version=tag.lstrip("v"),
                tag=tag,
                url=str(entry.get("html_url") or RELEASES_PAGE_URL),
                notes=str(entry.get("body") or ""),
                published=str(entry.get("published_at") or ""),
                prerelease=bool(entry.get("prerelease")),
                assets=tuple(
                    Asset(
                        name=str(asset.get("name") or ""),
                        url=str(asset.get("browser_download_url") or ""),
                        size=int(asset.get("size") or 0),
                    )
                    for asset in entry.get("assets") or []
                    if isinstance(asset, dict) and asset.get("browser_download_url")
                ),
            )
        )
    releases.sort(key=lambda release: parse_version(release.tag), reverse=True)
    return tuple(releases)


def newer_release(
    current: str,
    releases: tuple[Release, ...],
    *,
    allow_prerelease: bool = False,
) -> Release | None:
    """The newest release that beats ``current``, or ``None`` if there is none.

    Highest wins rather than first: the API returns releases by publication date, and a
    patch to an older line published after a newer one would otherwise be offered to
    everybody as an upgrade.
    """
    try:
        mine = parse_version(current)
    except ValueError:  # pragma: no cover - version.py is pinned by its own test
        return None
    candidates = [
        release
        for release in releases
        if (allow_prerelease or not release.prerelease) and parse_version(release.tag) > mine
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda release: parse_version(release.tag))


def _canonical_machine(machine: str) -> str:
    """``platform.machine()`` in the spelling the release assets use.

    Four names for two architectures: Windows says AMD64 where everything else says
    x86_64, and Linux says aarch64 where macOS says arm64. The asset names come from
    ``release.yml`` and use one spelling of each.
    """
    lowered = machine.strip().lower()
    if lowered in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if lowered in {"arm64", "aarch64"}:
        return "arm64"
    return lowered


def asset_for(release: Release, *, platform: str, machine: str) -> Asset | None:
    """The file on ``release`` that this machine can actually run, if there is one.

    ``platform`` is ``sys.platform`` and ``machine`` is ``platform.machine()``. Returns
    ``None`` for a machine the project does not build for -- an Intel Mac, an ARM Linux
    box -- and the host then offers the release notes instead of a download, which is
    where the "install from source" instructions are.
    """
    arch = _canonical_machine(machine)
    if platform.startswith("win"):
        wanted, needs_arch = ".exe", "x86_64"
    elif platform == "darwin":
        wanted, needs_arch = ".dmg", "arm64"
    elif platform.startswith("linux"):
        wanted, needs_arch = ".appimage", "x86_64"
    else:
        return None
    if arch != needs_arch:
        return None
    for asset in release.assets:
        if asset.name.lower().endswith(wanted):
            return asset
    return None


def expected_digest(sums: str, asset_name: str) -> str | None:
    """The SHA-256 recorded for ``asset_name``, or ``None`` if it is not in the file.

    ``None`` is not a failure. Releases before 0.7.0 have no ``SHA256SUMS`` attached at
    all, and a download nobody can check is still a download somebody can install -- the
    host says which of the two it got rather than refusing the older release.

    What the check is worth is worth being honest about: the sums file arrives from the
    same host over the same TLS connection as the installer, so it proves the download
    arrived intact, not that GitHub handed out the file this project built. The second
    claim needs the code signing PLAN.md §12 has not bought yet.
    """
    wanted = asset_name.strip().lower()
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        # The asterisk is coreutils' marker for a file read in binary mode.
        if name.lstrip("*").strip().lower() == wanted and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    return None


_HIGHLIGHT = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*", re.MULTILINE)


def highlights(notes: str, limit: int = 3) -> tuple[str, ...]:
    """The bold lead-in of each top-level changelog entry, for the notification strip.

    ``release.yml`` builds the release body out of this project's own changelog section,
    and every entry there opens with a bolded sentence saying what changed -- "**The
    placer knows what a stripboard is.**". Those sentences are the summary somebody
    already wrote by hand, so the strip quotes them rather than inventing one.

    Empty for a body with no such entries, and the strip then names the version and
    nothing else. A guessed summary that is wrong is worse than no summary: the button
    beside it opens the notes in full.
    """
    found: list[str] = []
    for match in _HIGHLIGHT.finditer(notes):
        text = match.group(1).replace("`", "").strip().rstrip(".")
        if text and text not in found:
            found.append(text)
        if len(found) == limit:
            break
    return tuple(found)


def is_check_due(
    last_checked: str | None,
    now: str,
    *,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
) -> bool:
    """Whether an automatic check should run, given when the last one did.

    Both arguments are ISO 8601. Never checked, an unreadable stamp, and a stamp in the
    future all mean "check now": a clock that went backwards -- a laptop crossing time
    zones, a machine whose battery died -- must not be able to switch update checks off
    for as long as it takes real time to catch up.
    """
    if not last_checked:
        return True
    try:
        previous = datetime.fromisoformat(last_checked)
        current = datetime.fromisoformat(now)
    except ValueError:
        return True
    # An aware datetime cannot be compared with a naive one, and a stamp stored by an
    # older build may not agree with this one about which it writes.
    if (previous.tzinfo is None) != (current.tzinfo is None):
        return True
    if previous > current:
        return True
    return current - previous >= timedelta(hours=interval_hours)


__all__ = [
    "CHECKSUM_ASSET",
    "DEFAULT_INTERVAL_HOURS",
    "GITHUB_REPOSITORY",
    "RELEASES_API_URL",
    "RELEASES_PAGE_URL",
    "Asset",
    "Release",
    "asset_for",
    "expected_digest",
    "highlights",
    "is_check_due",
    "newer_release",
    "parse_releases",
    "parse_version",
]
