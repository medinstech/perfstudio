"""Board colour schemes, shared by the 2D editor and the 3D view.

WHY THIS IS A VIEW SETTING AND NOT A DOCUMENT FIELD. Solder mask colour changes nothing
about the circuit: the same board in green and in blue is the same board, connects the
same pins and solders the same way. Putting it in ``Board`` would also add a field to the
``.perf`` format, which is compared byte for byte against output frozen from the reference
implementation -- a cosmetic preference is not worth reopening that. So it lives here, is
chosen from the View menu, and is not saved with the document.

WHAT THE SCHEMES ARE. The colours perfboard is actually sold in. Green FR-4 and brown
phenolic are what the substrate genuinely looks like for those materials, so they are the
defaults per material; the rest are real products (the blue double-sided boards are
everywhere) and are offered because someone matching a screenshot to the board in their
hand should be able to.

Each scheme carries both views' colours together, so a board cannot be green in the
editor and blue in the 3D view -- which would undermine the one job the 3D view has,
letting someone check that what they are about to solder is what they meant.
"""

from __future__ import annotations

from dataclasses import dataclass

from perfstudio.model import BoardMaterial


@dataclass(frozen=True, slots=True)
class BoardScheme:
    """One board's appearance. ``key`` is stable; ``label`` is shown and translated."""

    key: str
    label: str
    #: 2D: substrate fill and its edge, as hex.
    fill: str
    edge: str
    #: 3D: the same substrate as linear RGB, which is what VTK wants.
    rgb: tuple[float, float, float]
    #: Silkscreen printed on the board -- the row and column labels real boards carry.
    silk: str
    #: THE COPPER, and it belongs to the scheme for the same reason the substrate does.
    #: It was one global gold for every board, which is right for the plated FR-4 boards
    #: and wrong for the cheap phenolic one, whose pads are BARE COPPER -- pink-brown,
    #: not yellow. A pertinax board wearing FR-4's gold pads is not the board anybody has
    #: in their hand, and the finish is one of the two things you notice first.
    pad: str
    #: The darker edge of a pad, and the lighter catch of light across it.
    pad_ring: str
    pad_sheen: str
    #: The same copper for the 3D view.
    pad_rgb: tuple[float, float, float]


#: Plated copper on a masked board: the yellow of gold over nickel, which is what the
#: green, blue and black prototyping boards are finished with.
_GOLD = ("#c8a951", "#8a7331", "#e4cd83", (0.80, 0.66, 0.32))

#: BARE copper, unmasked and unplated, which is what a phenolic board has: pale warm
#: metal rather than yellow gold.
#:
#: MEASURED, not chosen. Sampled off product photographs of the boards themselves, which
#: put the pads at hue 25-28 degrees and saturation around 0.35 -- warm, so copper and
#: not tin, but much paler and less saturated than the pink-brown this first guessed at.
_BARE_COPPER = ("#e2a877", "#a86a3c", "#f6d0aa", (0.886, 0.659, 0.467))

SCHEMES: tuple[BoardScheme, ...] = (
    BoardScheme("green", "Green (FR-4)", "#2e6b3f", "#0d1a12", (0.16, 0.36, 0.21), "#e8f0ea", *_GOLD),
    BoardScheme("blue", "Blue", "#1f4e8c", "#0b1a2e", (0.10, 0.26, 0.50), "#e6edf7", *_GOLD),
    BoardScheme("black", "Black", "#232529", "#0a0b0d", (0.11, 0.12, 0.14), "#d8dbe0", *_GOLD),
    BoardScheme("red", "Red", "#8c2a26", "#2e0b0a", (0.50, 0.13, 0.12), "#f6e6e5", *_GOLD),
    BoardScheme("purple", "Purple", "#4a2a72", "#170d24", (0.26, 0.14, 0.42), "#ece4f6", *_GOLD),
    BoardScheme("white", "White", "#d8d6ce", "#a09c90", (0.84, 0.83, 0.79), "#3a3a36", *_GOLD),
    # Paper-phenolic, and ORANGE -- measured off photographs of the boards, which sample
    # at #c67a3f, #c17c58 and #bb7441 across three of them. It was briefly changed to a
    # brown on the reasoning that "pertinax is the colour of cardboard"; the boards say
    # otherwise, and this is the one colour in this file anybody can check.
    BoardScheme(
        "phenolic", "Orange (phenolic)", "#c47a41", "#3a2410", (0.769, 0.478, 0.255), "#2c2115",
        *_BARE_COPPER,
    ),
)

BY_KEY = {scheme.key: scheme for scheme in SCHEMES}

#: What each substrate material actually looks like, used until someone chooses otherwise.
#: FR-2 and FR-1 are the brown phenolic board, and looking like it is the point -- the
#: build guide derates the iron for exactly that material, so the two should agree on
#: sight.
DEFAULT_FOR_MATERIAL: dict[BoardMaterial, str] = {
    "FR4": "green",
    "FR2": "phenolic",
    "FR1": "phenolic",
}

_chosen: str | None = None


def choose(key: str | None) -> None:
    """Pick a scheme, or ``None`` to go back to following the board's material."""
    global _chosen
    _chosen = key if key in BY_KEY else None


def chosen_key() -> str | None:
    """The explicit choice, or None when the material decides."""
    return _chosen


def scheme_for(material: BoardMaterial) -> BoardScheme:
    """The scheme in force for a board of this material."""
    if _chosen is not None:
        return BY_KEY[_chosen]
    return BY_KEY[DEFAULT_FOR_MATERIAL.get(material, "green")]


__all__ = [
    "BY_KEY",
    "DEFAULT_FOR_MATERIAL",
    "SCHEMES",
    "BoardScheme",
    "choose",
    "chosen_key",
    "scheme_for",
]
