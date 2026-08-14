"""The headless run: every output this application can produce, with no display.

``python -m perfstudio.ui.main --headless board.perf`` renders the 2D editor view, both
1:1 PDFs, the 3D view from each face, the build guide with its step images, and prints
DRC, LVS, ratsnest, autoroute, style-sweep and placement timings.

WHY IT MATTERS MORE THAN A CLI USUALLY DOES. It is the only thing that exercises 2D, 3D
and the PDF export against a real board rather than against assertions about one, which
makes it what CI runs and the fastest way to find out whether a rendering change still
draws a board. It also INSPECTS a document and never edits one -- nothing here dispatches
a command, so it cannot be the thing that broke a file.

Its own module rather than the bottom of ``main.py``, where it lived: it is a program in
its own right, it shares nothing with the window but the scene it renders, and being
importable on its own is what lets a test call it without standing up a MainWindow.

THE TIMINGS ARE THE POINT of the numbers it prints. Each one is a claim CI can watch --
the router's cost before and after a change, whether a style still earns its place in the
sweep, whether the placer is still worth its runtime.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from perfstudio import persist
from perfstudio.autoroute import describe as describe_plan
from perfstudio.autoroute import describe_best, plan_autoroute, plan_best_autoroute
from perfstudio.drc import run_drc
from perfstudio.footprints import footprint_lookup
from perfstudio.geometry import board_outline_mm, hole_span_mm
from perfstudio.guide import build_guide
from perfstudio.guide import describe as describe_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.lvs import run_lvs
from perfstudio.placer import describe as describe_placement
from perfstudio.placer import plan_placement
from perfstudio.ratsnest import ratsnest, summarize
from perfstudio.version import describe as describe_version

from . import view3d
from .export_pdf import export_pdf, verify_scale
from .view2d import RULER_MARGIN_MM, BoardScene


def _find_repo_root() -> Path:
    """The repository this file lives in, for the default fixture path.

    Walks up looking for the golden directory rather than counting parents, so it keeps
    working from an installed package as well as from a working tree -- and returns the
    working directory when there is no repository, which is what an installed build gets.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tools" / "diffcheck" / "golden").is_dir():
            return parent
    return Path.cwd()


def _default_headless_platform() -> str:
    """Which Qt platform plugin to render headlessly with.

    "offscreen" everywhere except Windows. On Windows that plugin ships no font database at
    all -- ``QFontInfo(QFont()).family()`` comes back empty -- so every label renders as a
    missing-glyph box while looking perfect in the GUI. Since Windows always has a window
    station available, the normal plugin renders into a QImage without ever showing a window
    and gets real text. An explicit QT_QPA_PLATFORM still wins over this.
    """
    return "windows" if sys.platform == "win32" else "offscreen"


def headless(argv: list[str]) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", _default_headless_platform())
    app = QApplication.instance() or QApplication(sys.argv[:1])
    lookup = footprint_lookup()

    positional = [a for a in argv if not a.startswith("--")]
    perf_path = Path(positional[0]) if positional else _find_repo_root() / "tools" / "diffcheck" / "golden" / "dense.perf"

    out_dir = Path.cwd() / "headless_out"
    out_dir.mkdir(exist_ok=True)

    print(describe_version())
    print(f"document     {perf_path}")
    if not perf_path.exists():
        print(f"LOAD FAILED  no such file: {perf_path}")
        return 1
    text = perf_path.read_text(encoding="utf-8")
    result = persist.deserialize_document(text)
    if not result.ok:
        print(f"LOAD FAILED  [{result.code}] {result.message} (path={result.path})")
        return 1
    doc = result.document
    if result.warnings:
        print(f"warnings     {len(result.warnings)}")
        for w in result.warnings:
            print(f"  - {w}")

    board = doc.board
    outline = board_outline_mm(board)
    w_mm, h_mm = outline.width, outline.height
    sw, sh = hole_span_mm(board)
    print(f"board        {board.cols}x{board.rows} {board.material}")
    print(f"substrate    {w_mm:.2f} x {h_mm:.2f} mm   (hole span {sw:.2f} x {sh:.2f} mm)")
    print(f"parts        {len(doc.components)}   conductors {len(doc.conductors)}   nets {len(doc.nets)}")

    scene = BoardScene(doc, lookup, side="top")

    # --- 2D render ---
    # The source rect includes the ruler margin, unlike the print path below: this PNG is
    # a picture OF THE EDITOR, so it should show what the editor shows.
    px_per_mm = 12
    margin = RULER_MARGIN_MM
    src_w, src_h = w_mm + margin + 4, h_mm + margin + 4
    img_w, img_h = int(src_w * px_per_mm), int(src_h * px_per_mm)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
    image.fill(QColor("#12131a"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    source = QRectF(outline.x - margin, outline.y - margin, src_w, src_h)
    t0 = time.perf_counter()
    scene.render(painter, QRectF(0, 0, img_w, img_h), source)
    t_2d = (time.perf_counter() - t0) * 1000
    painter.end()
    out_2d = out_dir / "out_2d.png"
    image.save(str(out_2d))
    print(f"\n2D render    {t_2d:6.1f} ms   -> {out_2d}")
    if scene.pad_grid is not None:
        print(f"pads painted {scene.pad_grid.drawn} of {board.cols * board.rows} (Qt culls the rest)")

    # --- 1:1 scale verification and print sheets ---
    # Built WITHOUT the editor overlays: see MainWindow._export_scene for why the ratsnest
    # must not reach a sheet someone solders from.
    print_top = BoardScene(doc, lookup, side="top", show_ratsnest=False, show_rulers=False)
    print_bottom = BoardScene(doc, lookup, side="bottom", show_ratsnest=False, show_rulers=False)

    check = verify_scale(print_top, board)
    print(
        f"\n1:1 check    {check.span_holes} holes at {check.dpi:.0f} dpi: "
        f"expected {check.expected_px:.3f} px, measured {check.measured_px:.3f} px, "
        f"error {check.error_mm * 1000:.2f} um  -> {'PASS' if check.ok else 'FAIL'}"
    )

    pdf_component = export_pdf(board, print_top, out_dir / "board_1to1_component_side.pdf")
    pdf_solder = export_pdf(board, print_bottom, out_dir / "board_1to1_solder_side.pdf", mirrored=True)
    print(f"PDF          {pdf_component.stat().st_size / 1024:.0f} KB -> {pdf_component.name}")
    print(f"PDF mirrored {pdf_solder.stat().st_size / 1024:.0f} KB -> {pdf_solder.name}")

    # --- DRC / LVS, timed. This is the number that matters for "is DRC fast enough to
    # run after every drag": see the docstring on view2d.BoardScene.mouseReleaseEvent
    # and main.py's on_bus_changed -- DRC runs on drag RELEASE, once, not per frame.
    t0 = time.perf_counter()
    violations = run_drc(doc, lookup)
    t_drc = (time.perf_counter() - t0) * 1000
    errors = sum(1 for v in violations if v.severity == "error")
    warns = sum(1 for v in violations if v.severity == "warning")
    print(f"\nDRC          {t_drc:6.1f} ms   {errors} errors, {warns} warnings ({len(violations)} total)")

    t0 = time.perf_counter()
    lvs_result = run_lvs(doc, lookup)
    t_lvs = (time.perf_counter() - t0) * 1000
    s = lvs_result.summary
    print(
        f"LVS          {t_lvs:6.1f} ms   {s.matched_nets}/{s.schematic_nets} nets matched, "
        f"{s.opens} open, {s.shorts} short, {s.physical_nets} physical nets"
    )

    # --- Ratsnest and a dry-run autoroute ---
    # Reported but NOT committed: headless mode inspects a document, it does not edit one.
    # The point is to show what routing this board would cost, and to give CI a number that
    # moves when the router's quality changes.
    t0 = time.perf_counter()
    remaining = summarize(ratsnest(doc, lookup))
    t_rats = (time.perf_counter() - t0) * 1000
    print(
        f"ratsnest     {t_rats:6.1f} ms   {remaining.links} connection(s) left across "
        f"{remaining.nets - remaining.closed_nets} open net(s), {remaining.total_length_mm:.0f} mm total"
    )

    if doc.nets:
        t0 = time.perf_counter()
        plan = plan_autoroute(doc, lookup)
        t_plan = (time.perf_counter() - t0) * 1000
        print(f"autoroute    {t_plan:6.1f} ms   (dry run, nothing committed)")
        print(f"             {describe_plan(plan)}")
        after = run_lvs(plan.document, lookup).summary
        print(
            f"             would leave LVS at {after.matched_nets}/{after.schematic_nets} matched, "
            f"{after.opens} open, {after.shorts} short"
        )

        # Every style, measured. This is the number CI should watch alongside the placer's:
        # it says whether a style still earns its place, and a change to any cost table
        # shows up here as a different winner rather than as a silently different board.
        t0 = time.perf_counter()
        best = plan_best_autoroute(doc, lookup)
        t_sweep = (time.perf_counter() - t0) * 1000
        print(f"\nstyle sweep  {t_sweep:6.1f} ms   (dry run, nothing committed)")
        for line in describe_best(best).splitlines():
            print(f"             {line}")

    # --- Placement, also a dry run. The number CI should watch is the routing cost
    # before and after: it is the one that says whether the placer is still earning its
    # runtime, and it moves when either the placer or the router changes.
    if doc.components:
        t0 = time.perf_counter()
        placement = plan_placement(doc, lookup)
        t_place = (time.perf_counter() - t0) * 1000
        print(f"\nauto-place   {t_place:6.1f} ms   (dry run, nothing committed)")
        print(f"             {describe_placement(placement)}")
        placed_errors = sum(
            1 for v in run_drc(placement.document, lookup) if v.severity == "error"
        )
        print(
            f"             HPWL {placement.before.hpwl_mm:.0f} -> {placement.after.hpwl_mm:.0f} mm, "
            f"overlaps {placement.before.overlap_pairs} -> {placement.after.overlap_pairs}, "
            f"DRC errors {errors} -> {placed_errors}"
        )

    # --- The build guide, written out. This is the project's actual output, so a
    # headless run that renders the board and does not produce it is only testing half
    # the pipeline.
    guide = build_guide(doc, lookup)
    # Asked once, here, and reused by the 3D stage below. On a machine with no offscreen
    # GL this run reports what it could not draw and still produces every other output,
    # rather than dying halfway through with a crash dump: a headless run is what CI and
    # a bug report both use, and both are worse off if it stops at the first stage that
    # needs a graphics driver.
    can_render_3d = view3d.offscreen_gl_available()
    t0 = time.perf_counter()
    images = view3d.render_step_images(doc, guide, lookup) if can_render_3d else {}
    t_shots = (time.perf_counter() - t0) * 1000
    html = guide_to_html(guide, images)
    (out_dir / "guide.html").write_text(html, encoding="utf-8")
    (out_dir / "guide.json").write_text(guide_to_json(guide), encoding="utf-8")
    (out_dir / "cut_list.csv").write_text(cut_list_to_csv(guide), encoding="utf-8")
    (out_dir / "bom.csv").write_text(bom_to_csv(guide), encoding="utf-8")
    print(f"\nbuild guide  {describe_guide(guide)}")
    print(f"             {guide.part_steps} part step(s), {guide.conductor_steps} connection(s), "
          f"{len(guide.cut_list)} wire(s) -> guide.html")
    # The weight is printed because the images are inlined: the guide has to stay a file
    # somebody opens on a phone, and this is the number that would quietly stop being
    # true.
    print(f"step images  {t_shots:6.1f} ms   {len(images)} render(s), "
          f"guide.html is {len(html.encode('utf-8')) // 1024} KB")
    for warning in guide.warnings:
        print(f"  ! {warning.code}: {warning.message}")

    # --- 3D render, offscreen: the build-guide image path ---
    if not can_render_3d:
        # Said loudly rather than skipped quietly, and NOT an error: nothing is wrong
        # with the document, and every check this run exists to perform has already run.
        # A CI job that prints this is telling you its runner has no GPU, which is worth
        # knowing and is not a regression.
        print("\n3D SKIPPED: no offscreen GL context on this machine (VTK would abort)")
        print(f"\noutputs written to {out_dir}")
        del app
        return 0 if check.ok else 1

    try:
        t0 = time.perf_counter()
        stats = view3d.render_offscreen(doc, lookup, str(out_dir / "out_3d.png"))
        t_3d = (time.perf_counter() - t0) * 1000
        print(f"\n3D offscreen {t_3d:6.1f} ms   -> out_3d.png")
        print(f"actors       {stats['actors']} total for {stats['pads']} pads (instanced)")
        view3d.render_offscreen(doc, lookup, str(out_dir / "out_3d_solder.png"), flipped=True)
        print("             out_3d_solder.png (flipped to the solder side)")
    except Exception as exc:
        print(f"\n3D FAILED: {exc}")
        return 1

    print(f"\noutputs written to {out_dir}")
    del app
    return 0 if check.ok else 1

