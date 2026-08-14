"""Rendering the soldering guide to the formats PLAN.md Sec 7.6 asks for.

Three writers, one model. ``guide.py`` decides WHAT the guide says; this file decides
what it looks like, and knows nothing about perfboard.

  HTML   one self-contained file. No CDN, no fonts, no network: it is meant to be opened
         on a phone propped against the monitor in a room that may not have wifi, and to
         still work in five years. Steps tick off and the progress is kept in
         localStorage, keyed by the document name, so closing the tab mid-build does not
         lose the place.
  CSV    the cut list and the BOM -- the two things people paste into a spreadsheet or
         hand to a supplier.
  JSON   the whole guide, for agents and integrations. Stable key order.

Pure and deterministic, stdlib only: no Qt, no I/O of its own beyond returning strings.
That is what lets the MCP server and a headless CI run produce the same guide the
desktop app does.

The 1:1 printable sheets are a different thing and live in ui/export_pdf.py, because
they need a real renderer. This file references them rather than reproducing them.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from html import escape
from typing import Any

from .geometry import format_hole
from .guide import Checkpoint, ConductorStep, Guide, GuideStep, PartStep, step_focus
from .model import HoleCoord
from .version import __version__

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def guide_to_json(guide: Guide, indent: int = 2) -> str:
    """The whole guide as JSON, for agents and integrations.

    Hole coordinates are emitted BOTH ways -- ``{"col": 2, "row": 6, "ref": "C7"}`` --
    because a consumer needs the numbers and a human reading the file needs the address,
    and making either one derive the other means two implementations of the hole
    encoding in the wild.
    """
    return json.dumps(
        {
            "generator": f"PerfStudio {__version__}",
            "document": guide.document_name,
            "board": {
                "cols": guide.board.cols,
                "rows": guide.board.rows,
                "pitch_mm": guide.board.pitch,
                "material": guide.board.material,
                "thickness_mm": guide.board.thickness,
            },
            "iron": _plain(guide.iron),
            "tools": list(guide.tools),
            "warnings": [_plain(w) for w in guide.warnings],
            "bom": [_plain(line) for line in guide.bom],
            "cut_list": [_plain(cut) for cut in guide.cut_list],
            "spine_list": [_plain(spine) for spine in guide.spine_list],
            "phases": [
                {
                    "number": phase.number,
                    "title": phase.title,
                    "summary": phase.summary,
                    "steps": [_plain(step) for step in phase.steps],
                    "checkpoints": [_plain(check) for check in phase.checkpoints],
                }
                for phase in guide.phases
            ],
            "totals": {
                "part_steps": guide.part_steps,
                "conductor_steps": guide.conductor_steps,
                "checkpoints": guide.checkpoint_count,
            },
        },
        indent=indent,
        ensure_ascii=False,
    )


def _plain(value: Any) -> Any:
    """Dataclasses to dicts, with every HoleCoord carrying its own address.

    Walks the fields itself rather than calling ``dataclasses.asdict``, which recurses
    first and would hand back holes already flattened to ``{"col", "row"}`` -- the
    address would be silently missing from every nested step.
    """
    if isinstance(value, HoleCoord):
        return {"col": value.col, "row": value.row, "ref": format_hole(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def cut_list_to_csv(guide: Guide) -> str:
    """The wire cut list (PLAN.md Sec 7.3), plus the spine wires as their own rows.

    One table rather than two files: at the bench they are the same job -- cut these
    lengths of this wire -- and the ``type`` column is enough to tell them apart.
    """
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        ["type", "net", "from", "to", "path_mm", "cut_mm", "strip_mm", "awg", "colour", "note"]
    )
    for cut in guide.cut_list:
        writer.writerow(
            [
                "insulated wire" if cut.insulated else "bare wire",
                cut.net_name,
                format_hole(cut.from_hole),
                format_hole(cut.to_hole),
                f"{cut.path_mm:.1f}",
                f"{cut.cut_mm:.1f}",
                f"{cut.strip_mm:.1f}",
                cut.awg,
                cut.color,
                "",
            ]
        )
    for spine in guide.spine_list:
        writer.writerow(
            [
                "trace spine",
                spine.net_name,
                "",
                "",
                f"{spine.length_mm:.1f}",
                f"{spine.length_mm:.1f}",
                "0.0",
                "",
                spine.material,
                f"{spine.gauge_mm:g} mm, laid along {spine.pads} pads",
            ]
        )
    return out.getvalue()


def bom_to_csv(guide: Guide) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["quantity", "value", "footprint", "references"])
    for line in guide.bom:
        writer.writerow([line.quantity, line.value, line.footprint_name, line.refs])
    return out.getvalue()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_STYLE = """
:root {
  --bg: #14161b; --panel: #1b1e25; --panel-2: #22262f; --line: #2e333d;
  --text: #e7e9ee; --dim: #9aa3b2; --accent: #6fb3ff; --ok: #57c785;
  --warn: #f0b34a; --error: #ef6b6b; --trace: #d8a44a;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --panel-2: #f0f2f5; --line: #d9dee6;
    --text: #1a1d23; --dim: #5d6673; --accent: #1667c8; --ok: #1d7a49;
    --warn: #8a5a00; --error: #b32020; --trace: #8a6100;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 54rem; margin: 0 auto; padding: 1.5rem 1rem 6rem; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.2rem; margin: 2.5rem 0 .25rem; }
h3 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: var(--dim);
     text-transform: uppercase; letter-spacing: .06em; }
p  { margin: .4rem 0; }
a  { color: var(--accent); }
.sub { color: var(--dim); }
.phase { border-top: 2px solid var(--line); padding-top: .5rem; margin-top: 2rem; }
.phase-num { color: var(--accent); font-variant-numeric: tabular-nums; }
.step, .check {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: .7rem .9rem; margin: .5rem 0; display: flex; gap: .75rem; align-items: start;
}
.step.done, .check.done { opacity: .45; }
.step.done .title { text-decoration: line-through; }
input[type=checkbox] { width: 1.25rem; height: 1.25rem; margin-top: .2rem; flex: none; }
.body { flex: 1; min-width: 0; }
.title { font-weight: 600; }
.meta { color: var(--dim); font-size: .875rem; }
.note { font-size: .9rem; margin-top: .35rem; }
.shot { display: block; width: 100%; max-width: 30rem; margin: .6rem 0 .1rem;
        border: 1px solid var(--line); border-radius: 8px; background: #14161b; }
.hole { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        background: var(--panel-2); border-radius: 4px; padding: 0 .3rem; }
.tag { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
       border: 1px solid var(--line); border-radius: 999px; padding: .1rem .5rem;
       color: var(--dim); white-space: nowrap; }
.polarity { color: var(--warn); font-weight: 600; }
.risk { color: var(--error); }
.check { border-left: 3px solid var(--ok); }
.check.isolation { border-left-color: var(--warn); }
.check.blocking { border-left-color: var(--error); }
.warnbox { background: var(--panel-2); border-left: 3px solid var(--warn);
           border-radius: 6px; padding: .6rem .9rem; margin: .5rem 0; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; margin: .5rem 0; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid var(--line); }
th { color: var(--dim); font-weight: 600; }
.wrap { overflow-x: auto; }
.progress { position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel);
            border-top: 1px solid var(--line); padding: .6rem 1rem;
            display: flex; gap: 1rem; align-items: center; justify-content: center; }
.bar { flex: 1; max-width: 30rem; height: 8px; background: var(--panel-2);
       border-radius: 999px; overflow: hidden; }
.bar > i { display: block; height: 100%; width: 0; background: var(--ok); }
button { font: inherit; color: var(--text); background: var(--panel-2);
         border: 1px solid var(--line); border-radius: 6px; padding: .3rem .7rem;
         cursor: pointer; }
@media print {
  /* The whole palette, not just body: printed from a browser whose OS is in dark mode
     the tokens above are still the dark ones, and browsers drop background colours when
     they print -- so --dim's pale grey lands on white paper and the meta line under
     every step is the part that fades out. This guide gets taped next to the board. */
  :root {
    --bg: #fff; --panel: #fff; --panel-2: #f0f0f0; --line: #bbb;
    --text: #000; --dim: #444; --accent: #10305c; --ok: #14562f;
    --warn: #6b4300; --error: #8c1616; --trace: #6b4b00;
  }
  .progress, input[type=checkbox] { display: none; }
  body { background: #fff; color: #000; }
  .step, .check { break-inside: avoid; }
  .shot { break-inside: avoid; max-width: 20rem; }
}
"""

_SCRIPT = """
(function () {
  var key = 'perfstudio-guide:' + document.body.dataset.doc;
  var boxes = Array.prototype.slice.call(document.querySelectorAll('input[type=checkbox]'));
  var fill = document.querySelector('.bar > i');
  var count = document.querySelector('.count');
  var done = {};
  try { done = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { done = {}; }

  function paint() {
    var n = 0;
    boxes.forEach(function (box) {
      box.closest('.step, .check').classList.toggle('done', box.checked);
      if (box.checked) n++;
    });
    if (fill) fill.style.width = (boxes.length ? (n / boxes.length) * 100 : 0) + '%';
    if (count) count.textContent = n + ' of ' + boxes.length + ' done';
  }

  boxes.forEach(function (box) {
    box.checked = !!done[box.id];
    box.addEventListener('change', function () {
      done[box.id] = box.checked;
      try { localStorage.setItem(key, JSON.stringify(done)); } catch (e) {}
      paint();
    });
  });

  var reset = document.querySelector('.reset');
  if (reset) reset.addEventListener('click', function () {
    done = {};
    try { localStorage.removeItem(key); } catch (e) {}
    boxes.forEach(function (box) { box.checked = false; });
    paint();
  });

  paint();
})();
"""


def guide_to_html(guide: Guide, step_images: Mapping[str, bytes] | None = None) -> str:
    """One self-contained HTML file: no network, no assets, no build step.

    Deliberately not a framework. This file has to open from a USB stick on a phone in
    five years' time, which rules out every dependency that could stop existing.

    ``step_images`` are the illustrations PLAN.md §7.2 asks for, keyed by
    ``guide.step_focus(step)``. **Raw image bytes, not URLs or paths** — this function
    base64s them into the document itself. That is the whole point: a caller cannot hand
    it a link, so the finished file cannot acquire a dependency on a server, a folder
    beside it, or a phone's network. Rendering them needs VTK and a real board, which is
    the host's job (``ui/view3d.render_offscreen``); this module has never known what a
    board looks like and still does not — including what format its pictures are in,
    which ``_image_media_type`` reads off the bytes rather than agreeing in advance.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(guide.document_name)} — build guide</title>",
        f"<style>{_STYLE}</style></head>",
        f'<body data-doc="{escape(guide.document_name)}"><main>',
        f"<h1>{escape(guide.document_name)}</h1>",
        f'<p class="sub">Soldering guide · {guide.board.cols}×{guide.board.rows} '
        f"{escape(guide.board.material)} perfboard at {guide.board.pitch:g} mm pitch · "
        f"{guide.total_steps} steps, {guide.checkpoint_count} checks · "
        f"PerfStudio {escape(__version__)}</p>",
    ]

    for warning in guide.warnings:
        parts.append(
            f'<div class="warnbox"><b>{escape(warning.code)}</b> — '
            f"{escape(warning.message)}</div>"
        )

    parts.append(_html_preparation(guide))

    images = step_images or {}
    step_id = 0
    for phase in guide.phases:
        if phase.is_empty or phase.number == 0:
            continue
        parts.append(
            f'<section class="phase"><h2><span class="phase-num">Phase {phase.number}</span> '
            f"— {escape(phase.title)}</h2>"
            f'<p class="sub">{escape(phase.summary)}</p>'
        )
        for step in phase.steps:
            step_id += 1
            parts.append(_html_step(step, f"s{step_id}", images))
        if phase.checkpoints:
            parts.append("<h3>Check before moving on</h3>")
            for check in phase.checkpoints:
                step_id += 1
                parts.append(_html_check(check, f"s{step_id}"))
        parts.append("</section>")

    parts.append(_html_tables(guide))
    parts.append(
        '</main><div class="progress"><span class="count"></span>'
        '<span class="bar"><i></i></span>'
        '<button class="reset" type="button">Reset progress</button></div>'
        f"<script>{_SCRIPT}</script></body></html>"
    )
    return "\n".join(parts)


def _hole(at: HoleCoord) -> str:
    return f'<span class="hole">{escape(format_hole(at))}</span>'


def _html_preparation(guide: Guide) -> str:
    phase = guide.phases[0]
    rows = "".join(f"<li>{escape(tool)}</li>" for tool in guide.tools)
    checks = "".join(_html_check(check, f"p0-{n}") for n, check in enumerate(phase.checkpoints))
    return (
        f'<section class="phase"><h2><span class="phase-num">Phase 0</span> — '
        f"{escape(phase.title)}</h2>"
        f'<p class="sub">{escape(phase.summary)}</p>'
        f"<h3>On the bench</h3><ul>{rows}</ul>"
        f"<h3>Iron</h3><p>{guide.iron.temperature_c} °C, no more than "
        f"{guide.iron.max_dwell_s:g} s on any one pad. {escape(guide.iron.note)}</p>"
        f"<h3>The board</h3><p>Cut it to {guide.board.cols} × {guide.board.rows} holes "
        f"({guide.board.cols * guide.board.pitch:.1f} × "
        f"{guide.board.rows * guide.board.pitch:.1f} mm) and mark hole "
        f'<span class="hole">A1</span> in the top-left corner on the COMPONENT side. '
        f"Every address below is counted from it, so if it is marked wrong, everything "
        f"else is.</p>{checks}</section>"
    )


def _html_step(step: GuideStep, dom_id: str, images: Mapping[str, bytes]) -> str:
    picture = _html_step_image(step, images)
    if isinstance(step, PartStep):
        return _html_part_step(step, dom_id, picture)
    return _html_conductor_step(step, dom_id, picture)


#: Magic bytes to media type, longest signature first. A data URI carries its own type,
#: so the one thing this must never do is guess: a JPEG announced as ``image/png`` is a
#: broken picture in the one place there is no network to re-fetch it from.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),  # bytes 8..12 say WEBP; _image_media_type checks them
    (b"\x89PNG", "image/png"),  # the truncated stub the tests hand over
)


def _image_media_type(data: bytes) -> str:
    """What the bytes actually are, read off the front of them.

    The renderer's format is its own decision (``ui/view3d`` writes JPEG, and wrote PNG
    before that), and this module has no way to ask. Sniffing keeps the two from having
    to agree in advance -- and keeps a stale caller from mislabelling its own picture.
    """
    for magic, media in _IMAGE_MAGIC:
        if not data.startswith(magic):
            continue
        if magic == b"RIFF" and data[8:12] != b"WEBP":
            continue
        return media
    return "application/octet-stream"


def _html_step_image(step: GuideStep, images: Mapping[str, bytes]) -> str:
    """The board as it stands at this step, with this step's own part picked out.

    Inlined as a data URI. The alt text is the step's title rather than "step image":
    printed, or read aloud, or opened where the picture will not load, the sentence that
    survives has to be the one that says what to do.
    """
    picture = images.get(step_focus(step))
    if not picture:
        return ""
    encoded = base64.b64encode(picture).decode("ascii")
    return (
        f'<img class="shot" alt="{escape(step.title)}" '
        f'src="data:{_image_media_type(picture)};base64,{encoded}">'
    )


def _html_part_step(step: PartStep, dom_id: str, picture: str = "") -> str:
    holes = " ".join(f"{escape(number)}:{_hole(at)}" for number, at in step.pin_holes)
    bits = [
        f'<div class="title">{escape(step.title)}</div>',
        f'<div class="meta">{escape(step.footprint_name)} · pins {holes}'
        + (f" · {step.rotation}°" if step.rotation else "")
        + "</div>",
    ]
    if step.bend_template_mm:
        bits.append(
            f'<div class="note">Bend the leads to {step.bend_template_mm:.2f} mm '
            f"({step.bend_template_mm / 2.54:.0f} holes).</div>"
        )
    if step.polarity:
        bits.append(f'<div class="note polarity">{escape(step.polarity)}</div>')
    for note in step.notes:
        bits.append(f'<div class="note sub">{escape(note)}</div>')
    return (
        f'<label class="step" for="{dom_id}">'
        f'<input type="checkbox" id="{dom_id}">'
        f'<span class="body">{"".join(bits)}{picture}</span>'
        f'<span class="tag">{escape(step.archetype)}</span></label>'
    )


def _html_conductor_step(step: ConductorStep, dom_id: str, picture: str = "") -> str:
    bits = [
        f'<div class="title">{escape(step.net_name)}: '
        f"{_hole(step.path[0])} → {_hole(step.path[-1])}</div>"
        if step.path
        else f'<div class="title">{escape(step.title)}</div>',
        f'<div class="meta">{escape(step.conductor_kind.replace("-", " "))} · '
        f"{step.length_mm:.1f} mm"
        + (f" · {step.pads} pads" if step.pads > 2 else "")
        + (
            f" · about {step.resistance_ohm * 1000:.1f} mΩ"
            if step.resistance_ohm is not None
            else ""
        )
        + (f" · {step.drop_mv:.0f} mV drop" if step.drop_mv is not None else "")
        + "</div>",
    ]
    if step.pads > 2 and step.path:
        bits.append(
            '<div class="meta">Path: '
            + " → ".join(_hole(at) for at in step.path)
            + "</div>"
        )
    if step.cut is not None:
        bits.append(
            f'<div class="note">Cut {step.cut.cut_mm:.0f} mm of '
            f"{escape(step.cut.color)} AWG {step.cut.awg}, strip {step.cut.strip_mm:.0f} mm "
            "at each end.</div>"
        )
    if step.spine is not None:
        bits.append(
            f'<div class="note">Spine: {step.spine.length_mm:.0f} mm of '
            f"{step.spine.gauge_mm:g} mm {escape(step.spine.material)}.</div>"
        )
    for note in step.notes:
        bits.append(f'<div class="note sub">{escape(note)}</div>')
    for risk in step.risks:
        bits.append(f'<div class="note risk">⚠ {escape(risk.message)}</div>')
    return (
        f'<label class="step" for="{dom_id}">'
        f'<input type="checkbox" id="{dom_id}">'
        f'<span class="body">{"".join(bits)}{picture}</span>'
        f'<span class="tag" style="color:var(--trace)">'
        f'{escape(step.conductor_kind.replace("-", " "))}</span></label>'
    )


def _html_check(check: Checkpoint, dom_id: str) -> str:
    classes = f"check {check.kind}" + (" blocking" if check.blocking else "")
    holes = (
        '<div class="meta">Probe ' + " and ".join(_hole(at) for at in check.holes) + "</div>"
        if check.holes
        else ""
    )
    gate = (
        '<div class="note risk">Do not apply power until this passes.</div>'
        if check.blocking
        else ""
    )
    return (
        f'<label class="{classes}" for="{dom_id}">'
        f'<input type="checkbox" id="{dom_id}">'
        f'<span class="body"><div class="title">{escape(check.title)}</div>'
        f'<div class="note">{escape(check.instruction)}</div>{holes}'
        f'<div class="note sub">Expect: {escape(check.expected)}</div>{gate}</span>'
        f'<span class="tag">{escape(check.kind)}</span></label>'
    )


def _html_tables(guide: Guide) -> str:
    parts = ['<section class="phase"><h2>Lists</h2>']

    parts.append("<h3>Parts</h3><div class=\"wrap\"><table>")
    parts.append("<tr><th>Qty</th><th>Value</th><th>Package</th><th>References</th></tr>")
    for line in guide.bom:
        parts.append(
            f"<tr><td>{line.quantity}</td><td>{escape(line.value)}</td>"
            f"<td>{escape(line.footprint_name)}</td><td>{escape(line.refs)}</td></tr>"
        )
    parts.append("</table></div>")

    if guide.cut_list or guide.spine_list:
        parts.append('<h3>Wire</h3><div class="wrap"><table>')
        parts.append(
            "<tr><th>Type</th><th>Net</th><th>From</th><th>To</th><th>Cut</th>"
            "<th>AWG</th><th>Colour</th></tr>"
        )
        for cut in guide.cut_list:
            parts.append(
                f'<tr><td>{"insulated" if cut.insulated else "bare"}</td>'
                f"<td>{escape(cut.net_name)}</td><td>{_hole(cut.from_hole)}</td>"
                f"<td>{_hole(cut.to_hole)}</td><td>{cut.cut_mm:.0f} mm</td>"
                f"<td>{cut.awg}</td><td>{escape(cut.color)}</td></tr>"
            )
        for spine in guide.spine_list:
            parts.append(
                f"<tr><td>spine</td><td>{escape(spine.net_name)}</td><td colspan=2>"
                f"{spine.pads} pads</td><td>{spine.length_mm:.0f} mm</td><td>—</td>"
                f"<td>{spine.gauge_mm:g} mm {escape(spine.material)}</td></tr>"
            )
        parts.append("</table></div>")

    parts.append("</section>")
    return "".join(parts)


__all__ = [
    "bom_to_csv",
    "cut_list_to_csv",
    "guide_to_html",
    "guide_to_json",
]
