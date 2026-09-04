"""Draw the two cards that carry the product's face, from one design.

    python tools/make_banners.py

Writes both into `docs/images/`:

  `banner.png` (1280x400)          the README's header, on GitHub and on PyPI
  `social-preview.png` (1280x640)  what GitHub shows wherever the repository is linked,
                                   at the 2:1 ratio Slack, Discord and X crop towards

WHY THIS IS A SCRIPT. The same argument `tools/make_assets.py` makes for the application
icon. Both cards take their colours from `ui/theme.py` and their picture from
`docs/images/`, so a card drawn once by hand starts becoming a picture of an older
version of the product the first time either changes. And they are ONE design in two
crops rather than two designs: the header somebody scrolls past and the card somebody
sees in a feed are the same object, and drawing them separately is how they stop being.

GITHUB WILL NOT TAKE THE PREVIEW FROM A COMMIT. The social preview is repository
metadata, not a file in the tree: there is no REST API for it, so the image is committed
here and set by hand at Settings -> General -> Social preview. The file being in the
repository is what makes "which image is up there?" answerable at all. `banner.png` is
different -- the README references it like any other image.

WHAT IS ON THEM. The name, the sentence the README opens with, and the routed NE555
bleeding off the right edge. Cropped to the circuit rather than scaled down from the
whole window, because both cards are rendered small and a whole editor screenshot at
that size is grey mush. Somebody should be able to see parts and copper on them.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from perfstudio.ui import theme  # noqa: E402

COPPER = "#c8a06a"  # ui/boardcolors.py's tinned pad, for the second half of the rule.

# One sentence, wrapped by each card to its own text column rather than broken by hand:
# the two cards have different room, and a hand-broken line that fits the wide one runs
# straight into the board on the narrow one.
TAGLINE = (
    "Design circuits on perfboard the way you would on a PCB — "
    "then get a soldering guide you can actually build from."
)

# The screenshot's own coordinates. Each card names its own box, held inside the board's
# edges on all four sides so the crop bleeds off the card rather than showing the
# editor's background as a border, and shaped like the panel it fills so the board is
# never squashed. The short banner takes a band through the circuit at 1:1 rather than
# the whole board scaled down -- at 520 px the parts have to stay legible.
BANNER_CROP = (690, 260, 1210, 660)
PREVIEW_CROP = (660, 170, 1243, 735)

# Regular, semibold and a mono, in the order each platform is likely to have them.
_FAMILIES = {
    "bold": ("segoeuib.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc", "Arial Bold.ttf"),
    "text": ("segoeui.ttf", "DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"),
    "mono": ("consola.ttf", "DejaVuSansMono.ttf", "Menlo.ttc", "Courier New.ttf"),
}


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: float
) -> list[str]:
    """`text` broken into the longest lines that fit `max_w`, greedily."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > max_w:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in _FAMILIES[kind]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    raise SystemExit(f"no {kind} font found; install DejaVu or run this on Windows")


def _card(
    width: int,
    height: int,
    panel_w: int,
    *,
    crop: tuple[int, int, int, int],
    fade_w: int,
    margin: int,
    name_size: int,
    name_y: int,
    tagline_size: int,
    tagline_y: int,
    footer: bool,
) -> Image.Image:
    card = Image.new("RGB", (width, height), theme.WINDOW)
    draw = ImageDraw.Draw(card)

    shot = Image.open(REPO_ROOT / "docs/images/editor-component-side.png").convert("RGB")
    panel = shot.crop(crop).resize((panel_w, height), Image.LANCZOS)
    card.paste(panel, (width - panel_w, 0))

    # Fade the screenshot's left edge into the background, so the card reads as one
    # object and not as two pictures butted together.
    fade = Image.new("L", (fade_w, height))
    fade_draw = ImageDraw.Draw(fade)
    for x in range(fade_w):
        fade_draw.line([(x, 0), (x, height)], fill=int(255 * (1 - x / fade_w) ** 1.15))
    card.paste(Image.new("RGB", (fade_w, height), theme.WINDOW), (width - panel_w, 0), fade)

    draw.text((margin, name_y), "PerfStudio", font=_font("bold", name_size), fill=theme.TEXT)

    # A rule in the two colours the product is made of: the interface's accent, and the
    # board's copper.
    rule_y = name_y + round(name_size * 1.33)
    unit = round(name_size * 1.14)
    draw.rectangle([margin, rule_y, margin + unit, rule_y + 5], fill=theme.ACCENT)
    draw.rectangle(
        [margin + unit + 8, rule_y, margin + unit + 8 + round(unit * 0.67), rule_y + 5],
        fill=COPPER,
    )

    tagline = _font("text", tagline_size)
    step = round(tagline_size * 1.39)
    # The text column stops where the board's panel starts. The fade is not room: text
    # laid over it is text over a picture of a circuit board.
    for i, line in enumerate(_wrap(draw, TAGLINE, tagline, width - panel_w - margin)):
        draw.text((margin, tagline_y + i * step), line, font=tagline, fill=theme.TEXT_DIM)

    if footer:
        draw.text(
            (margin, 500),
            "Autorouter · perfboard DRC · LVS · MCP server",
            font=_font("text", 23),
            fill=theme.TEXT_DIM,
        )
        draw.rectangle([margin, 546, margin + 356, 548], fill=theme.BORDER)
        draw.text(
            (margin, 566),
            "Get it from the releases page",
            font=_font("mono", 22),
            fill=theme.ACCENT,
        )
    return card


def main() -> None:
    out = REPO_ROOT / "docs/images"
    cards = {
        # The README header: wide and short, so it does not push the first paragraph off
        # the screen. GitHub renders it at about 830 px across.
        "banner.png": _card(
            1280, 400, 520, crop=BANNER_CROP, fade_w=150, margin=64,
            name_size=68, name_y=104, tagline_size=25, tagline_y=252, footer=False,
        ),
        "social-preview.png": _card(
            1280, 640, 660, crop=PREVIEW_CROP, fade_w=210, margin=78,
            name_size=84, name_y=150, tagline_size=31, tagline_y=306, footer=True,
        ),
    }
    for name, card in cards.items():
        card.save(out / name, optimize=True)
        print(f"wrote docs/images/{name} ({card.width}x{card.height})")


if __name__ == "__main__":
    main()
