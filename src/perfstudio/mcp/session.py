"""The board an agent is working on, and every operation it can perform on one.

This file is where the MCP tools actually live. ``server.py`` only binds them to the
protocol -- names, docstrings, transport -- so that everything below can be tested by
calling it, with no client, no stdio and no event loop. A test that needs a live MCP
session is testing the transport; the interesting failures are all here.

TWO RULES, both inherited rather than invented:

  EVERY MUTATION GOES THROUGH THE COMMAND BUS (PLAN.md Sec 8.1). Not one method here
  writes to a document. That is what makes undo work for an agent exactly as it works
  for the user, what puts the agent's edits in the same journal, and what lets the two
  drive the same board without one of them silently winning.

  EVERY RESULT IS PLAIN JSON-ABLE DATA. No dataclasses cross this boundary, no tuples of
  tuples, and every hole is given as its ADDRESS ("C7") because that is the language the
  rest of the tool, the DRC messages and the build guide all speak. An agent that has to
  convert between two hole encodings will eventually convert one of them wrongly.

A refused command comes back as ``{"ok": false, "code": ..., "message": ...}`` rather
than raising, matching ``CommandBus.dispatch``'s own contract. An agent needs to be able
to try something and be told no.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, get_args

from perfstudio import persist
from perfstudio.autoroute import AutorouteOptions, describe_reroute, plan_autoroute, plan_reroute
from perfstudio.autoroute import describe as describe_route
from perfstudio.command import CommandBus, CommandContext
from perfstudio.commands import (
    AddConductorPayload,
    DeleteComponentPayload,
    ImportNetlistPayload,
    MoveComponentPayload,
    NewSolderTraceConductor,
    NewWireConductor,
    PlaceComponentPayload,
    RotateComponentPayload,
    UpdateComponentPayload,
    create_document_id_generator,
    create_empty_document,
    create_standard_registry,
)
from perfstudio.connectivity import extract_physical_nets
from perfstudio.drc import run_drc
from perfstudio.footprints import footprint_lookup, standard_footprints
from perfstudio.geometry import all_pin_holes, format_hole, hole_ref_to_coord
from perfstudio.guide import build_guide
from perfstudio.guide import describe as describe_guide
from perfstudio.guide_export import bom_to_csv, cut_list_to_csv, guide_to_html, guide_to_json
from perfstudio.lvs import run_lvs, stale_conductor_ids
from perfstudio.model import (
    Board,
    ComponentInstance,
    DocumentMeta,
    HoleCoord,
    PerfDocument,
    Rotation,
    SpineSpec,
)
from perfstudio.placer import PlacementOptions, plan_placement
from perfstudio.placer import describe as describe_placement
from perfstudio.ratsnest import ratsnest, summarize
from perfstudio.router import RoutingStyle, options_for_style
from perfstudio.version import __version__

# ---------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Bad input from the caller: a hole that will not parse, a ref that is not there.

    Distinct from a REFUSED command, which is a legitimate answer and comes back as
    data. This is "the request did not make sense", and the server turns it into an
    error the model can read.
    """


def _ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def _refused(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def _hole(ref: str) -> HoleCoord:
    """Parse a hole address, or say clearly what was wrong with it."""
    try:
        return hole_ref_to_coord(ref.strip().upper())
    except Exception as err:  # noqa: BLE001 - the parser's exception type is its own business
        raise SessionError(
            f"{ref!r} is not a hole address. They look like 'A1', 'C7' or 'AC12': "
            f"column letters then a 1-based row ({err})."
        ) from err


def _now_iso() -> str:
    """Timestamp for a new document's metadata.

    The only clock in the engine's world, and it lives here rather than in core on
    purpose: commands.py and persist.py are pure, and a clock inside them would make
    replay non-deterministic.
    """
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class _Snapshot:
    label: str
    document: PerfDocument


@dataclass
class BoardSession:
    """One board, its bus, and the snapshots an agent has taken of it.

    Deliberately a single board rather than a workspace of them. An agent working on two
    boards at once is not a thing anybody asked for, and the ambiguity of "which board
    did that tool mean" is a whole class of confusing failure avoided for free.
    """

    document: PerfDocument = field(
        default_factory=lambda: create_empty_document(
            DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso())
        )
    )
    path: Path | None = None
    bus: CommandBus = field(init=False)
    snapshots: dict[str, _Snapshot] = field(default_factory=dict, init=False)
    _snapshot_counter: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.lookup = footprint_lookup()
        self._install(self.document)

    def _install(self, document: PerfDocument) -> None:
        """Point the session at a document, with a bus seeded from its own ids.

        Seeded, not fresh: a loaded document already contains cmp-1..cmp-8, and a
        generator starting at 1 would have its first edit refused as a duplicate id.
        """
        self.document = document
        self.bus = CommandBus(
            document,
            create_standard_registry(),
            CommandContext(next_id=create_document_id_generator(document)),
        )
        self.bus.subscribe(self._on_change)

    def _on_change(self, document: PerfDocument, _entry: Any) -> None:
        self.document = document

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, type_: str, payload: Any) -> dict[str, Any]:
        result = self.bus.dispatch(type_, payload)
        if not result.ok:
            return _refused(result.code or "refused", result.message or "Command refused.")
        return _ok(description=result.description, undo_depth=len(self.bus.history()))

    # -- reading -----------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Where the board is right now: counts, verification and the undo depth.

        The tool an agent should call first and after anything surprising, which is why
        it carries the DRC and LVS summaries rather than making that two more round
        trips.
        """
        drc = run_drc(self.document, self.lookup)
        lvs = run_lvs(self.document, self.lookup)
        remaining = summarize(ratsnest(self.document, self.lookup))
        return {
            "version": __version__,
            "document": self.document.meta.name,
            "path": str(self.path) if self.path else None,
            "board": f"{self.document.board.cols}x{self.document.board.rows} "
            f"{self.document.board.material}",
            "components": len(self.document.components),
            "conductors": len(self.document.conductors),
            "schematic_nets": len(self.document.nets),
            "drc": {
                "errors": sum(1 for v in drc if v.severity == "error"),
                "warnings": sum(1 for v in drc if v.severity == "warning"),
            },
            "lvs": {
                "matched_nets": lvs.summary.matched_nets,
                "schematic_nets": lvs.summary.schematic_nets,
                "opens": lvs.summary.opens,
                "shorts": lvs.summary.shorts,
            },
            "unrouted_connections": remaining.links,
            "stale_conductors": len(stale_conductor_ids(self.document, self.lookup)),
            "undo_depth": len(self.bus.history()),
            # The last few entries, not the whole journal: an agent wants to know what
            # just happened, and a hundred-entry list in every status call is context
            # spent on nothing.
            "recent_history": list(self.bus.history()[-8:]),
            "snapshots": sorted(self.snapshots),
        }

    def get_board_info(self) -> dict[str, Any]:
        board = self.document.board
        return {
            "type": board.type,
            "cols": board.cols,
            "rows": board.rows,
            "pitch_mm": board.pitch,
            "thickness_mm": board.thickness,
            "material": board.material,
            "pad_diameter_mm": board.pad_diameter,
            "drill_diameter_mm": board.drill_diameter,
            "top_left_hole": format_hole(HoleCoord(0, 0)),
            "bottom_right_hole": format_hole(HoleCoord(board.cols - 1, board.rows - 1)),
        }

    def list_components(self) -> list[dict[str, Any]]:
        return [self._component_summary(c) for c in self.document.components]

    def _component_summary(self, component: ComponentInstance) -> dict[str, Any]:
        footprint = self.lookup(component.footprint_id)
        return {
            "ref": component.ref,
            "value": component.value,
            "footprint_id": component.footprint_id,
            "footprint_known": footprint is not None,
            "anchor": format_hole(component.anchor),
            "rotation": component.rotation,
            "mirrored": component.mirrored,
            "locked": component.locked,
        }

    def get_component(self, ref: str) -> dict[str, Any]:
        component = self._require_component(ref)
        summary = self._component_summary(component)
        footprint = self.lookup(component.footprint_id)
        if footprint is not None:
            summary["footprint_name"] = footprint.name
            summary["body_height_mm"] = footprint.body_height
            summary["polarized"] = footprint.polarized
            summary["pins"] = [
                {"pin": pin.number, "name": pin.name, "hole": format_hole(at)}
                for pin, at in all_pin_holes(component, footprint)
            ]
        return summary

    def get_nets(self) -> list[dict[str, Any]]:
        """The SCHEMATIC's nets -- what the circuit is supposed to connect.

        What the board actually connects is a different question, answered by
        ``get_net_connections``. Keeping the two apart is the whole basis of LVS.
        """
        return [
            {
                "id": net.id,
                "name": net.name,
                "net_class": net.net_class,
                "pins": [f"{node.component_ref}.{node.pin}" for node in net.nodes],
                "current_a": net.current_a,
                "voltage_v": net.voltage_v,
            }
            for net in self.document.nets
        ]

    def get_net_connections(self, name: str) -> dict[str, Any]:
        """What the BOARD joins for one schematic net, and what it still does not."""
        net = next((n for n in self.document.nets if n.name == name or n.id == name), None)
        if net is None:
            raise SessionError(
                f"No net called {name!r}. Known nets: "
                f"{', '.join(n.name for n in self.document.nets) or '(none imported)'}."
            )

        physical = extract_physical_nets(self.document, self.lookup)
        wanted = {(node.component_ref, node.pin) for node in net.nodes}
        groups: list[dict[str, Any]] = []
        for island in physical:
            members = [p for p in island.pins if (p.component_ref, p.pin) in wanted]
            if members:
                groups.append(
                    {
                        "pins": [f"{p.component_ref}.{p.pin}" for p in members],
                        "conductors": list(island.conductor_ids),
                    }
                )

        entry = next(
            (n for n in ratsnest(self.document, self.lookup) if n.net_id == net.id), None
        )
        return {
            "net": net.name,
            "net_class": net.net_class,
            "declared_pins": sorted(f"{ref}.{pin}" for ref, pin in wanted),
            "physical_groups": groups,
            "connected": len(groups) <= 1 and bool(groups),
            "remaining_connections": [
                {
                    "from": format_hole(link.from_),
                    "to": format_hole(link.to),
                    "length_mm": round(link.length_mm, 2),
                }
                for link in (entry.links if entry is not None else ())
            ],
        }

    def list_footprints(self, search: str = "") -> list[dict[str, Any]]:
        """The parts library. Free text matches the id, the name or the body type."""
        needle = search.strip().lower()
        out: list[dict[str, Any]] = []
        for footprint in standard_footprints().values():
            haystack = f"{footprint.id} {footprint.name} {footprint.body.archetype}".lower()
            if needle and needle not in haystack:
                continue
            out.append(
                {
                    "id": footprint.id,
                    "name": footprint.name,
                    "archetype": footprint.body.archetype,
                    "pins": len(footprint.pins),
                    "height_mm": footprint.body_height,
                    "polarized": footprint.polarized,
                }
            )
        out.sort(key=lambda entry: str(entry["id"]))
        return out

    # -- documents ---------------------------------------------------------

    def open_document(self, path: str) -> dict[str, Any]:
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as err:
            raise SessionError(f"Cannot open {target}: {err.strerror or err}.") from err
        result = persist.deserialize_document(text)
        if not result.ok:
            return _refused(result.code or "parse-error", result.message or "Unreadable document.")
        self.path = target
        self._install(result.document)
        self.snapshots.clear()
        return _ok(
            opened=str(target),
            warnings=list(result.warnings),
            status=self.get_status(),
        )

    def save_document(self, path: str | None = None) -> dict[str, Any]:
        target = Path(path) if path else self.path
        if target is None:
            raise SessionError(
                "This board has never been saved, so save_document needs a path."
            )
        stamped = dataclasses.replace(
            self.document,
            meta=dataclasses.replace(self.document.meta, modified=_now_iso()),
        )
        try:
            target.write_text(persist.serialize_document(stamped), encoding="utf-8")
        except OSError as err:
            raise SessionError(f"Cannot write {target}: {err.strerror or err}.") from err
        self.path = target
        return _ok(saved=str(target), bytes=target.stat().st_size)

    def import_netlist(self, path: str) -> dict[str, Any]:
        from perfstudio.parsers.kicad import parse_kicad_netlist

        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as err:
            raise SessionError(f"Cannot open {target}: {err.strerror or err}.") from err
        try:
            imported = parse_kicad_netlist(text)
        except ValueError as err:
            return _refused("parse-error", f"{target.name}: {err}")

        result = self._dispatch("netlist.import", ImportNetlistPayload(nets=imported.nets))
        if result["ok"]:
            result["nets"] = len(imported.nets)
            result["warnings"] = list(imported.warnings)
            result["missing_components"] = sorted(
                {
                    node.component_ref
                    for net in imported.nets
                    for node in net.nodes
                    if not any(c.ref == node.component_ref for c in self.document.components)
                }
            )
        return result

    # -- editing -----------------------------------------------------------

    def place_component(
        self,
        ref: str,
        footprint_id: str,
        hole: str,
        value: str = "",
        rotation: int = 0,
    ) -> dict[str, Any]:
        if self.lookup(footprint_id) is None:
            raise SessionError(
                f"No footprint {footprint_id!r} in the library. Call list_footprints to see "
                "what is available."
            )
        return self._dispatch(
            "component.place",
            PlaceComponentPayload(
                ref=ref,
                value=value,
                footprint_id=footprint_id,
                anchor=_hole(hole),
                rotation=_rotation(rotation),
            ),
        )

    def move_component(self, ref: str, hole: str) -> dict[str, Any]:
        component = self._require_component(ref)
        return self._dispatch(
            "component.move", MoveComponentPayload(id=component.id, anchor=_hole(hole))
        )

    def rotate_component(self, ref: str, rotation: int) -> dict[str, Any]:
        component = self._require_component(ref)
        return self._dispatch(
            "component.rotate",
            RotateComponentPayload(id=component.id, rotation=_rotation(rotation)),
        )

    def set_component_locked(self, ref: str, locked: bool) -> dict[str, Any]:
        """Pin a part in place so the placement optimiser works around it."""
        component = self._require_component(ref)
        return self._dispatch(
            "component.update", UpdateComponentPayload(id=component.id, locked=locked)
        )

    def delete_component(self, ref: str) -> dict[str, Any]:
        component = self._require_component(ref)
        return self._dispatch("component.delete", DeleteComponentPayload(id=component.id))

    def add_wire(
        self, from_hole: str, to_hole: str, kind: str = "insulated-wire", net: str | None = None
    ) -> dict[str, Any]:
        if kind not in ("bare-wire", "insulated-wire", "top-jumper"):
            raise SessionError(
                f"{kind!r} is not a wire. Use 'insulated-wire' (may cross other copper), "
                "'bare-wire' (may not) or 'top-jumper' (over the component side)."
            )
        return self._dispatch(
            "conductor.add",
            AddConductorPayload(
                conductor=NewWireConductor(
                    path=(_hole(from_hole), _hole(to_hole)),
                    kind=kind,  # type: ignore[arg-type]
                    net_id=self._net_id(net),
                )
            ),
        )

    def add_solder_trace(
        self, holes: list[str], spine_gauge_mm: float | None = None, net: str | None = None
    ) -> dict[str, Any]:
        """Join adjacent pads with solder (PLAN.md D8).

        ``holes`` must be an orthogonally-adjacent chain; the command refuses a diagonal
        step, because solder does not reliably span the 1.7 mm diagonal gap the way it
        spans the 0.6 mm orthogonal one. A two-hole trace is what other tools call a
        solder bridge -- one concept, one rule set, so there is no separate tool for it.
        """
        if len(holes) < 2:
            raise SessionError("A solder trace needs at least two holes.")
        spine = (
            SpineSpec(material="tinned-copper", gauge=spine_gauge_mm)
            if spine_gauge_mm is not None
            else None
        )
        return self._dispatch(
            "conductor.add",
            AddConductorPayload(
                conductor=NewSolderTraceConductor(
                    path=tuple(_hole(h) for h in holes),
                    kind="solder-trace-wired" if spine is not None else "solder-trace",
                    spine=spine,
                    net_id=self._net_id(net),
                )
            ),
        )

    # -- the planners ------------------------------------------------------

    def autoroute(self, nets: list[str] | None = None, style: str = "balanced") -> dict[str, Any]:
        """Plan and commit the routing, as one undoable command."""
        if not self.document.nets:
            return _refused("no-netlist", "Nothing to route: no netlist has been imported.")

        only = None
        if nets:
            only = tuple(self._net_id_strict(name) for name in nets)

        # Cleared BEFORE planning, exactly as the GUI does it, so the plan is made
        # against the board as it will be rather than around copper that is about to go.
        # An agent that moves a part and re-routes must get the same result a user does.
        cleared = stale_conductor_ids(self.document, self.lookup)
        if cleared:
            self.remove_stale_conductors()

        plan = plan_autoroute(
            self.document, self.lookup, _route_options(style), only_net_ids=only
        )
        if plan.is_empty:
            return _ok(
                committed=False,
                summary=describe_route(plan),
                routed=0,
                unrouted=0,
                stale_removed=len(cleared),
            )

        result = self._dispatch("conductor.addMany", plan.payload())
        if not result["ok"]:
            return result
        result.update(
            committed=True,
            summary=describe_route(plan),
            stale_removed=len(cleared),
            routed=plan.summary.links_routed,
            unrouted=plan.summary.links_unrouted,
            # Never summarised away: PLAN.md Sec 13 names "it routed most of it and left
            # four connections" as the trap this project is built to avoid.
            unrouted_detail=[
                {
                    "net": item.link.net_name,
                    "from": format_hole(item.link.from_),
                    "to": format_hole(item.link.to),
                    "reason": item.reason,
                }
                for outcome in plan.nets
                for item in outcome.unrouted
            ],
        )
        return result

    def reroute(self, nets: list[str] | None = None, style: str = "balanced") -> dict[str, Any]:
        """Rip up the existing routing and plan it again, as one undoable command.

        Different from ``autoroute``, which only ADDS: after a part moves, the copper
        laid for its old position still joins the right pins, so nothing reports it and
        autoroute simply puts more copper beside it. Measured on the NE555 fixture:
        14 conductors routed fresh, 16 after moving one resistor and autorouting again,
        14 again after this. Removes only conductors that claim one of these nets;
        copper with no net is left alone.
        """
        if not self.document.nets:
            return _refused("no-netlist", "Nothing to route: no netlist has been imported.")
        only = tuple(self._net_id_strict(name) for name in nets) if nets else None

        plan = plan_reroute(
            self.document, self.lookup, only_net_ids=only, options=_route_options(style)
        )
        if plan.is_empty:
            return _ok(committed=False, summary=describe_reroute(plan))

        result = self._dispatch("conductor.replace", plan.payload())
        if not result["ok"]:
            return result
        result.update(
            committed=True,
            summary=describe_reroute(plan),
            ripped_up=len(plan.remove_ids),
            routed=plan.summary.links_routed,
            unrouted=plan.summary.links_unrouted,
            unrouted_detail=[
                {
                    "net": item.link.net_name,
                    "from": format_hole(item.link.from_),
                    "to": format_hole(item.link.to),
                    "reason": item.reason,
                }
                for outcome in plan.nets
                for item in outcome.unrouted
            ],
        )
        return result

    def optimize_placement(self, seed: int = 0, apply: bool = True) -> dict[str, Any]:
        """Anneal the placement of every unlocked part (PLAN.md Sec 6.3).

        ``apply=False`` reports what it would do without touching the board, which is
        what an agent should use before spending a user's layout on it.
        """
        plan = plan_placement(self.document, self.lookup, PlacementOptions(seed=seed))
        report = {
            "summary": describe_placement(plan),
            "seed": plan.seed,
            "moves": [
                {
                    "ref": change.ref,
                    "from": format_hole(change.from_anchor),
                    "to": format_hole(change.to_anchor),
                    "rotation": change.to_rotation,
                }
                for change in plan.changes
            ],
            "hpwl_mm_before": round(plan.before.hpwl_mm, 1),
            "hpwl_mm_after": round(plan.after.hpwl_mm, 1),
            "router_cost": plan.route_cost,
            "legal": plan.after.is_legal,
        }
        if plan.is_empty:
            return _ok(committed=False, **report)
        if not apply:
            return _ok(committed=False, **report)

        result = self._dispatch("component.moveMany", plan.payload())
        if not result["ok"]:
            return result
        result.update(committed=True, **report)
        return result

    def remove_stale_conductors(self) -> dict[str, Any]:
        """Delete copper a moved part left behind, as one undoable command."""
        strays = stale_conductor_ids(self.document, self.lookup)
        if not strays:
            return _ok(removed=0, message="Every conductor still connects the net it claims.")
        from perfstudio.commands import DeleteConductorsPayload

        result = self._dispatch(
            "conductor.deleteMany",
            DeleteConductorsPayload(ids=strays, label=f"Remove {len(strays)} stale conductor(s)"),
        )
        if result["ok"]:
            result["removed"] = len(strays)
        return result

    # -- verification ------------------------------------------------------

    def run_drc(self) -> dict[str, Any]:
        violations = run_drc(self.document, self.lookup)
        return {
            "errors": sum(1 for v in violations if v.severity == "error"),
            "warnings": sum(1 for v in violations if v.severity == "warning"),
            "violations": [
                {
                    "rule": v.rule,
                    "severity": v.severity,
                    "message": v.message,
                    "holes": [format_hole(h) for h in v.holes],
                }
                for v in violations
            ],
        }

    def run_lvs(self) -> dict[str, Any]:
        result = run_lvs(self.document, self.lookup)
        return {
            "ok": result.ok,
            "matched_nets": result.summary.matched_nets,
            "schematic_nets": result.summary.schematic_nets,
            "opens": result.summary.opens,
            "shorts": result.summary.shorts,
            "physical_nets": result.summary.physical_nets,
            "issues": [
                {
                    "kind": issue.kind,
                    "message": issue.message,
                    "nets": list(issue.net_names),
                    "pins": [f"{p.component_ref}.{p.pin}" for p in issue.pins],
                }
                for issue in result.issues
            ],
        }

    # -- output ------------------------------------------------------------

    def generate_guide(self, directory: str | None = None) -> dict[str, Any]:
        """Build the soldering guide, and optionally write it out.

        Without a directory it returns the summary and the warnings only. That is
        usually what an agent wants -- "is this board buildable, and what is missing" --
        and it avoids writing four files into somebody's project as a side effect of
        asking a question.
        """
        guide = build_guide(self.document, self.lookup)
        report: dict[str, Any] = {
            "summary": describe_guide(guide),
            "part_steps": guide.part_steps,
            "conductor_steps": guide.conductor_steps,
            "checkpoints": guide.checkpoint_count,
            "wires_to_cut": len(guide.cut_list),
            "phases": [
                {"number": p.number, "title": p.title, "steps": len(p.steps),
                 "checks": len(p.checkpoints)}
                for p in guide.phases
                if not p.is_empty
            ],
            "warnings": [{"code": w.code, "message": w.message} for w in guide.warnings],
        }
        if directory is None:
            return _ok(written=[], **report)

        out = Path(directory)
        try:
            out.mkdir(parents=True, exist_ok=True)
            written = []
            for name, text in (
                ("guide.html", guide_to_html(guide)),
                ("guide.json", guide_to_json(guide)),
                ("cut_list.csv", cut_list_to_csv(guide)),
                ("bom.csv", bom_to_csv(guide)),
            ):
                (out / name).write_text(text, encoding="utf-8")
                written.append(str(out / name))
        except OSError as err:
            raise SessionError(f"Cannot write the guide to {out}: {err.strerror or err}.") from err
        return _ok(written=written, **report)

    def export_pdf(self, directory: str | None = None) -> dict[str, Any]:
        """The 1:1 printable sheets, component side and mirrored solder side.

        Needs Qt, which the engine does not, so it is imported here and its absence is
        reported as a refusal rather than taking the server down at import time.
        """
        out = Path(directory) if directory else Path.cwd()
        try:
            from perfstudio.ui.export_pdf import export_pdf as write_pdf
            from perfstudio.ui.export_pdf import verify_scale
            from perfstudio.ui.view2d import BoardScene
        except Exception as err:  # pragma: no cover - only on a Qt-less install
            return _refused(
                "qt-unavailable",
                f"The 1:1 PDF export needs PySide6, which is not usable here ({err}).",
            )

        _ensure_qt_application()
        out.mkdir(parents=True, exist_ok=True)
        top = BoardScene(self.document, self.lookup, side="top", show_ratsnest=False,
                         show_rulers=False)
        bottom = BoardScene(self.document, self.lookup, side="bottom", show_ratsnest=False,
                            show_rulers=False)
        check = verify_scale(top, self.document.board)
        component_side = write_pdf(self.document.board, top, out / "board_component_side.pdf")
        solder_side = write_pdf(
            self.document.board, bottom, out / "board_solder_side.pdf", mirrored=True
        )
        return _ok(
            written=[str(component_side), str(solder_side)],
            scale_exact=check.ok,
            scale_error_um=round(check.error_mm * 1000, 3),
        )

    def render_2d(self, side: str = "top", px_per_mm: int = 12) -> tuple[bytes, dict[str, Any]]:
        """A PNG of the board as the editor draws it. Returns (png_bytes, metadata).

        The most important tool in the write-side surface, per PLAN.md Sec 9.2: without
        a picture an agent is editing a board it cannot see, and every judgement about
        whether a change helped is guesswork.
        """
        if side not in ("top", "bottom"):
            raise SessionError("side must be 'top' (component side) or 'bottom' (solder side).")
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        from perfstudio.geometry import board_size_mm
        from perfstudio.ui.view2d import RULER_MARGIN_MM, BoardScene

        _ensure_qt_application()
        board = self.document.board
        scene = BoardScene(self.document, self.lookup, side=side)  # type: ignore[arg-type]
        width_mm, height_mm = board_size_mm(board)
        margin = RULER_MARGIN_MM
        src_w, src_h = width_mm + margin + 4, height_mm + margin + 4
        image = QImage(int(src_w * px_per_mm), int(src_h * px_per_mm), QImage.Format.Format_ARGB32)
        image.fill(QColor("#12131a"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene.render(
            painter,
            QRectF(0, 0, image.width(), image.height()),
            QRectF(-board.pitch / 2 - margin, -board.pitch / 2 - margin, src_w, src_h),
        )
        painter.end()
        return _png_bytes(image), {
            "side": side,
            "width_px": image.width(),
            "height_px": image.height(),
        }

    def render_3d(self, flipped: bool = False) -> tuple[bytes, dict[str, Any]]:
        """A PNG of the 3D view, component side or turned over."""
        import tempfile

        from perfstudio.ui import view3d

        _ensure_qt_application()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "view.png"
            stats = view3d.render_offscreen(
                self.document, self.lookup, str(target), flipped=flipped
            )
            return target.read_bytes(), {"flipped": flipped, **stats}

    # -- history and snapshots ---------------------------------------------

    def undo(self) -> dict[str, Any]:
        if not self.bus.can_undo():
            return _refused("nothing-to-undo", "The undo stack is empty.")
        last = self.bus.history()[-1]
        self.bus.undo()
        return _ok(undone=last, undo_depth=len(self.bus.history()))

    def redo(self) -> dict[str, Any]:
        if not self.bus.can_redo():
            return _refused("nothing-to-redo", "The redo stack is empty.")
        self.bus.redo()
        return _ok(undo_depth=len(self.bus.history()))

    def history(self) -> list[str]:
        return list(self.bus.history())

    def snapshot(self, label: str = "") -> dict[str, Any]:
        """Remember the board as it is now, under a name.

        Cheap: documents are immutable and commands replace only the tuples they touch,
        so a snapshot is a reference, not a copy. This and ``restore`` are what let an
        agent try something drastic and get out of it -- the second-most-important pair
        in this surface after the renderers.
        """
        self._snapshot_counter += 1
        key = label.strip() or f"snapshot-{self._snapshot_counter}"
        self.snapshots[key] = _Snapshot(label=key, document=self.document)
        return _ok(snapshot=key, snapshots=sorted(self.snapshots))

    #: Where ``restore`` parks the board it is about to replace.
    AUTO_SNAPSHOT = "before-restore"

    def restore(self, label: str) -> dict[str, Any]:
        """Go back to a snapshot.

        RESTORING CLEARS THE UNDO STACK, and that is worth stating because it is the one
        surprising thing in this surface. It happens because restoring replaces the
        session's bus: there is no command meaning "become this other document", and
        inventing one would put a whole-document payload in the journal that replay
        would then have to honour.

        So instead of pretending, the state being replaced is snapshotted first, under
        ``before-restore``. Nothing is lost, and the way back is another restore rather
        than an undo.
        """
        snapshot = self.snapshots.get(label)
        if snapshot is None:
            return _refused(
                "no-such-snapshot",
                f"No snapshot called {label!r}. Have: {', '.join(sorted(self.snapshots)) or 'none'}.",
            )
        self.snapshots[self.AUTO_SNAPSHOT] = _Snapshot(
            label=self.AUTO_SNAPSHOT, document=self.document
        )
        self._install(snapshot.document)
        return _ok(
            restored=label,
            undo_stack_cleared=True,
            previous_state_saved_as=self.AUTO_SNAPSHOT,
            status=self.get_status(),
        )

    # -- helpers -----------------------------------------------------------

    def _require_component(self, ref: str) -> ComponentInstance:
        for component in self.document.components:
            if component.ref == ref:
                return component
        known = ", ".join(c.ref for c in self.document.components) or "(the board is empty)"
        raise SessionError(f"No component called {ref!r}. On this board: {known}.")

    def _net_id(self, name: str | None) -> str | None:
        if not name:
            return None
        return self._net_id_strict(name)

    def _net_id_strict(self, name: str) -> str:
        for net in self.document.nets:
            if net.name == name or net.id == name:
                return net.id
        known = ", ".join(n.name for n in self.document.nets) or "(none imported)"
        raise SessionError(f"No net called {name!r}. Known nets: {known}.")


def _route_options(style: str) -> AutorouteOptions:
    """Turn a style name into router options, or say what the names are.

    The style is a judgement about the builder rather than about the board -- which
    primitive they would rather use -- so it is per call, not a session setting: an agent
    may reasonably want the power rails as solder rails and the signals as wire.
    """
    if style not in get_args(RoutingStyle):
        raise SessionError(
            f"{style!r} is not a routing style. Use one of: {', '.join(get_args(RoutingStyle))}."
        )
    return AutorouteOptions(router=options_for_style(cast(RoutingStyle, style)))


def _rotation(value: int) -> Rotation:
    if value not in (0, 90, 180, 270):
        raise SessionError(f"Rotation must be 0, 90, 180 or 270 degrees, not {value}.")
    return value  # type: ignore[return-value]


def _ensure_qt_application() -> None:
    """A QApplication must exist before any QPainter or scene is built.

    Offscreen by default, but not on Windows: that platform plugin ships no font
    database at all, so every label renders as a missing-glyph box while looking perfect
    in the GUI. The normal plugin draws into a QImage without ever showing a window.
    """
    import os
    import sys

    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        os.environ.setdefault(
            "QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen"
        )
        QApplication(["perfstudio-mcp"])


def _png_bytes(image: Any) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def new_board(cols: int = 30, rows: int = 20, material: str = "FR4") -> PerfDocument:
    """A blank document, for a session that starts from nothing."""
    if material not in ("FR4", "FR2", "FR1"):
        raise SessionError("Board material must be FR4, FR2 or FR1.")
    board = Board(
        type="pad-per-hole",
        cols=cols,
        rows=rows,
        pitch=2.54,
        thickness=1.6,
        material=material,  # type: ignore[arg-type]
        pad_diameter=1.9,
        drill_diameter=0.8,
    )
    return create_empty_document(
        DocumentMeta(name="untitled", created=_now_iso(), modified=_now_iso()), board
    )
