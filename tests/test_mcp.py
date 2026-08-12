"""Tests for the MCP surface (src/perfstudio/mcp/).

Almost all of it exercises ``BoardSession`` directly rather than through a client. That
is deliberate and is why the session exists as its own module: a test that stands up a
stdio server is testing the transport, and every failure worth catching here is about
what the tools DO -- whether an agent's edit really went through the command bus,
whether a refusal comes back as data instead of an exception, whether the board an agent
is told about is the board it has.

The three claims this file is organised around:

  EVERY MUTATION GOES THROUGH THE BUS (PLAN.md Sec 8.1). If a tool wrote to the document
  directly, undo would silently stop working for the agent while still working for the
  user, and the two would be editing different histories of the same board.

  A REFUSAL IS AN ANSWER. Locked parts, off-board holes, diagonal solder traces: an
  agent has to be able to try something and be told no, with a code it can branch on.
  Only genuinely malformed input (an unparseable hole, an unknown ref) raises.

  THE WHOLE WORKFLOW WORKS. test_an_agent_can_take_a_blank_board_to_a_build_guide is the
  M6 exit criterion, and it is the test that would notice if any of the pieces stopped
  fitting together.

The one test that touches the protocol layer checks the two things only it can: that the
tools are registered and that the module logs to stderr, since a stray byte on stdout
corrupts the stdio protocol and produces a baffling, unrelated error at the client.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from perfstudio.mcp.session import BoardSession, SessionError, new_board
from perfstudio.model import ComponentInstance, HoleCoord

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "tools" / "diffcheck" / "golden" / "ne555.perf"
NETLIST = REPO_ROOT / "examples" / "ne555-astable.net"


@pytest.fixture
def session() -> BoardSession:
    return BoardSession(document=new_board(cols=24, rows=16))


@pytest.fixture
def loaded() -> BoardSession:
    board = BoardSession()
    assert board.open_document(str(GOLDEN))["ok"]
    return board


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_status_answers_where_the_board_stands(loaded: BoardSession) -> None:
    status = loaded.get_status()

    assert status["components"] == 8
    assert status["schematic_nets"] == 7
    assert status["drc"]["errors"] == 0
    assert status["unrouted_connections"] > 0
    assert status["undo_depth"] == 0


def test_board_info_gives_the_corner_addresses(session: BoardSession) -> None:
    """An agent reasoning about where to put things needs the extents in the same
    language every other tool speaks."""
    info = session.get_board_info()
    assert info["top_left_hole"] == "A1"
    assert info["bottom_right_hole"] == "X16"


def test_a_component_reports_the_hole_each_pin_sits_in(loaded: BoardSession) -> None:
    part = loaded.get_component("U1")
    assert part["ref"] == "U1"
    assert len(part["pins"]) == 8
    assert all(isinstance(pin["hole"], str) for pin in part["pins"])


def test_asking_for_a_part_that_is_not_there_says_which_ones_are(loaded: BoardSession) -> None:
    with pytest.raises(SessionError) as err:
        loaded.get_component("R99")
    assert "R99" in str(err.value)
    assert "U1" in str(err.value)


def test_schematic_nets_and_board_connections_are_different_questions(
    loaded: BoardSession,
) -> None:
    """The whole basis of LVS. An API that conflated them would make the tool unable to
    answer the only question that matters before soldering."""
    assert len(loaded.get_nets()) == 7

    connections = loaded.get_net_connections("GND")
    assert connections["net"] == "GND"
    assert connections["connected"] is False  # Nothing is routed on the fixture.
    assert connections["remaining_connections"]


def test_the_footprint_library_is_searchable(session: BoardSession) -> None:
    assert len(session.list_footprints()) > 50
    dips = session.list_footprints("dip")
    assert dips and all("dip" in str(entry["id"]) for entry in dips)


# ---------------------------------------------------------------------------
# Editing, through the bus
# ---------------------------------------------------------------------------


def test_every_edit_lands_on_the_undo_stack(session: BoardSession) -> None:
    """If a tool wrote to the document directly this would still "work" -- and undo
    would silently stop working for the agent while still working for the user."""
    session.place_component(ref="R1", footprint_id="r-axial-4", hole="C3")
    session.move_component("R1", "F5")
    session.rotate_component("R1", 90)

    assert len(session.history()) == 3
    session.undo()
    assert session.get_component("R1")["rotation"] == 0
    session.undo()
    assert session.get_component("R1")["anchor"] == "C3"


def test_redo_puts_it_back(session: BoardSession) -> None:
    session.place_component(ref="R1", footprint_id="r-axial-4", hole="C3")
    session.move_component("R1", "F5")
    session.undo()
    assert session.get_component("R1")["anchor"] == "C3"
    assert session.redo()["ok"]
    assert session.get_component("R1")["anchor"] == "F5"


def test_undo_on_an_empty_stack_is_refused_rather_than_raising(session: BoardSession) -> None:
    result = session.undo()
    assert result["ok"] is False
    assert result["code"] == "nothing-to-undo"


def test_an_off_board_placement_comes_back_as_a_refusal(session: BoardSession) -> None:
    """dispatch()'s contract is that it never raises for bad input, and this surface
    depends on it: an agent has to be able to try something and be told no."""
    result = session.place_component(ref="R1", footprint_id="r-axial-4", hole="Z14")

    assert result["ok"] is False
    assert result["code"]
    assert session.get_status()["components"] == 0


def test_a_locked_part_refuses_to_move(session: BoardSession) -> None:
    session.place_component(ref="J1", footprint_id="hdr-1x2", hole="C3")
    session.set_component_locked("J1", True)

    result = session.move_component("J1", "H8")

    assert result["ok"] is False
    assert result["code"] == "component-locked"


def test_a_diagonal_solder_trace_is_refused(session: BoardSession) -> None:
    """Solder spans the 0.6 mm orthogonal pad gap and not the 1.7 mm diagonal one. The
    command knows that; the tool must not be able to talk it out of it."""
    result = session.add_solder_trace(["C3", "D4"])
    assert result["ok"] is False


def test_an_orthogonal_solder_trace_is_accepted_with_a_spine(session: BoardSession) -> None:
    result = session.add_solder_trace(["C3", "D3", "E3", "F3"], spine_gauge_mm=0.6)
    assert result["ok"], result
    conductor = session.document.conductors[0]
    assert conductor.kind == "solder-trace-wired"
    assert conductor.spine is not None and conductor.spine.gauge == 0.6


def test_an_unparseable_hole_says_what_one_looks_like(session: BoardSession) -> None:
    with pytest.raises(SessionError) as err:
        session.place_component(ref="R1", footprint_id="r-axial-4", hole="banana")
    assert "A1" in str(err.value)


def test_an_unknown_footprint_points_at_the_library(session: BoardSession) -> None:
    with pytest.raises(SessionError) as err:
        session.place_component(ref="R1", footprint_id="not-a-part", hole="C3")
    assert "list_footprints" in str(err.value)


def test_an_invalid_rotation_is_rejected(session: BoardSession) -> None:
    session.place_component(ref="R1", footprint_id="r-axial-4", hole="C3")
    with pytest.raises(SessionError):
        session.rotate_component("R1", 45)


def test_naming_a_net_that_does_not_exist_lists_the_ones_that_do(loaded: BoardSession) -> None:
    with pytest.raises(SessionError) as err:
        loaded.add_wire("C3", "C7", net="NOT_A_NET")
    assert "GND" in str(err.value)


# ---------------------------------------------------------------------------
# The planners
# ---------------------------------------------------------------------------


def test_autoroute_commits_as_one_undo_step_and_never_hides_a_failure(
    loaded: BoardSession,
) -> None:
    result = loaded.autoroute()

    assert result["ok"] and result["committed"]
    assert result["routed"] > 0
    assert "unrouted_detail" in result  # Present even when empty: PLAN.md Sec 13.
    assert len(result["unrouted_detail"]) == result["unrouted"]
    assert len(loaded.history()) == 1

    loaded.undo()
    assert loaded.get_status()["conductors"] == 0


def test_autoroute_without_a_netlist_is_refused_with_a_reason(session: BoardSession) -> None:
    result = session.autoroute()
    assert result["ok"] is False
    assert result["code"] == "no-netlist"


def test_autoroute_clears_stale_copper_first_exactly_as_the_gui_does(
    session: BoardSession,
) -> None:
    """An agent that leaves stranded copper and re-routes has to get the same board a
    user does -- the GUI clears strays before planning, so this must too.

    Stale is a claim test, not a geometry one: a conductor is stale when the island it
    sits in no longer holds two pins of the net it says it implements. The wire below
    says VCC and reaches one VCC pin, which is exactly the shape of what a moved part
    leaves behind.
    """
    session.import_netlist(str(NETLIST))
    session.place_component(ref="R1", footprint_id="r-axial-4", hole="C3")
    added = session.add_wire("C3", "I3", kind="bare-wire", net="VCC")
    assert added["ok"]
    stranded = session.document.conductors[0].id
    assert session.get_status()["stale_conductors"] == 1

    result = session.autoroute()

    assert result["stale_removed"] == 1
    assert stranded not in {c.id for c in session.document.conductors}


def test_optimize_placement_can_report_without_touching_the_board(
    loaded: BoardSession,
) -> None:
    before = [c["anchor"] for c in loaded.list_components()]

    result = loaded.optimize_placement(seed=3, apply=False)

    assert result["committed"] is False
    assert result["moves"]
    assert [c["anchor"] for c in loaded.list_components()] == before


def test_optimize_placement_applies_as_one_undo_step(loaded: BoardSession) -> None:
    before = [c["anchor"] for c in loaded.list_components()]

    result = loaded.optimize_placement(seed=3)

    assert result["committed"] and result["ok"]
    assert [c["anchor"] for c in loaded.list_components()] != before
    assert len(loaded.history()) == 1
    loaded.undo()
    assert [c["anchor"] for c in loaded.list_components()] == before


# ---------------------------------------------------------------------------
# Verification and output
# ---------------------------------------------------------------------------


def test_drc_and_lvs_come_back_as_data_not_prose(loaded: BoardSession) -> None:
    drc = loaded.run_drc()
    assert set(drc) == {"errors", "warnings", "violations"}
    assert all({"rule", "severity", "message", "holes"} <= set(v) for v in drc["violations"])

    lvs = loaded.run_lvs()
    assert lvs["schematic_nets"] == 7
    assert lvs["opens"] > 0  # Nothing routed yet, and it says so rather than pretending.


def test_generating_a_guide_writes_nothing_unless_asked(loaded: BoardSession) -> None:
    """Asking "is this buildable" should not scatter four files through a project."""
    result = loaded.generate_guide()
    assert result["ok"] and result["written"] == []
    assert result["part_steps"] == 8
    assert any(w["code"] == "lvs-open" for w in result["warnings"])


def test_generating_a_guide_with_a_directory_writes_all_four_files(
    loaded: BoardSession, tmp_path: Path
) -> None:
    loaded.autoroute()

    result = loaded.generate_guide(str(tmp_path / "out"))

    written = sorted(Path(p).name for p in result["written"])
    assert written == ["bom.csv", "cut_list.csv", "guide.html", "guide.json"]
    assert "<!doctype html>" in (tmp_path / "out" / "guide.html").read_text(encoding="utf-8")


def test_hole_addresses_are_the_only_coordinate_system_on_this_surface() -> None:
    """An agent that has to convert between two hole encodings will eventually convert
    one of them wrongly, so raw col/row must not leak out of any tool."""
    board = BoardSession()
    board.open_document(str(GOLDEN))
    board.autoroute()

    blob = json.dumps(
        [
            board.get_status(),
            board.list_components(),
            board.get_component("U1"),
            board.get_net_connections("GND"),
            board.run_drc(),
        ]
    )
    assert '"col"' not in blob
    assert '"row"' not in blob


# ---------------------------------------------------------------------------
# Documents and snapshots
# ---------------------------------------------------------------------------


def test_opening_a_document_seeds_the_id_generator_past_what_is_there(
    loaded: BoardSession,
) -> None:
    """A generator starting at 1 would have the first edit refused as a duplicate id --
    which is exactly the bug the GUI hit."""
    result = loaded.place_component(ref="R9", footprint_id="r-axial-4", hole="A15")
    assert result["ok"], result


def test_a_missing_file_is_reported_rather_than_raised_as_a_traceback() -> None:
    board = BoardSession()
    with pytest.raises(SessionError) as err:
        board.open_document("no/such/board.perf")
    assert "Cannot open" in str(err.value)


def test_save_round_trips_through_the_real_format(loaded: BoardSession, tmp_path: Path) -> None:
    target = tmp_path / "out.perf"
    assert loaded.save_document(str(target))["ok"]

    reopened = BoardSession()
    assert reopened.open_document(str(target))["ok"]
    assert reopened.get_status()["components"] == loaded.get_status()["components"]


def test_saving_a_never_saved_board_without_a_path_says_so(session: BoardSession) -> None:
    with pytest.raises(SessionError) as err:
        session.save_document()
    assert "needs a path" in str(err.value)


def test_importing_a_netlist_names_the_parts_the_board_does_not_have(
    session: BoardSession,
) -> None:
    result = session.import_netlist(str(NETLIST))

    assert result["ok"] and result["nets"] == 7
    assert sorted(result["missing_components"]) == ["C1", "C2", "J1", "LED1", "R1", "R2", "R3", "U1"]


def test_snapshot_and_restore_are_the_way_out_of_something_drastic(
    loaded: BoardSession,
) -> None:
    loaded.snapshot("good")
    loaded.delete_component("R1")
    assert loaded.get_status()["components"] == 7

    result = loaded.restore("good")

    assert result["ok"]
    assert loaded.get_status()["components"] == 8


def test_restoring_says_that_it_clears_the_undo_stack_and_saves_what_it_replaced(
    loaded: BoardSession,
) -> None:
    """The one surprising thing in this surface, so it is reported rather than
    discovered -- and nothing is lost, because the replaced board is snapshotted too."""
    loaded.snapshot("good")
    loaded.delete_component("R1")

    result = loaded.restore("good")

    assert result["undo_stack_cleared"] is True
    assert result["previous_state_saved_as"] == BoardSession.AUTO_SNAPSHOT
    assert loaded.undo()["ok"] is False
    assert loaded.restore(BoardSession.AUTO_SNAPSHOT)["ok"]
    assert loaded.get_status()["components"] == 7


def test_restoring_a_snapshot_that_does_not_exist_lists_the_ones_that_do(
    loaded: BoardSession,
) -> None:
    loaded.snapshot("alpha")
    result = loaded.restore("beta")
    assert result["ok"] is False
    assert "alpha" in result["message"]


# ---------------------------------------------------------------------------
# The workflow -- M6's exit criterion
# ---------------------------------------------------------------------------


def test_an_agent_can_take_a_blank_board_to_a_build_guide(tmp_path: Path) -> None:
    """PLAN.md Sec 11 M6: "a board is designed end to end and a guide produced from
    Claude Code and Antigravity". This is that path, minus the transport.

    It goes: blank board, import the netlist, place the parts the netlist names,
    optimise the placement, route, verify, generate. Every step through the same command
    bus the GUI uses.
    """
    board = BoardSession(document=new_board(cols=30, rows=20))

    imported = board.import_netlist(str(NETLIST))
    assert imported["ok"] and imported["nets"] == 7

    guesses = {"U": "dip-8", "R": "r-axial-4", "C": "c-elec-d5-p2", "J": "hdr-1x2", "L": "led-5mm"}
    for index, ref in enumerate(imported["missing_components"]):
        placed = board.place_component(
            ref=ref,
            footprint_id=guesses.get(ref[0], "r-axial-4"),
            hole=f"{chr(ord('A') + (index % 4) * 6)}{2 + (index // 4) * 6}",
        )
        assert placed["ok"], placed

    assert board.optimize_placement(seed=1)["ok"]
    routed = board.autoroute()
    assert routed["ok"] and routed["unrouted"] == 0

    lvs = board.run_lvs()
    assert lvs["opens"] == 0 and lvs["shorts"] == 0
    assert lvs["matched_nets"] == lvs["schematic_nets"] == 7
    assert board.run_drc()["errors"] == 0

    guide = board.generate_guide(str(tmp_path))
    assert guide["warnings"] == []
    assert guide["part_steps"] == 8
    assert guide["checkpoints"] > 0
    assert (tmp_path / "guide.html").exists()


# ---------------------------------------------------------------------------
# The protocol layer -- only what nothing else can check
# ---------------------------------------------------------------------------


def test_the_tool_surface_is_registered_and_stays_narrow() -> None:
    """PLAN.md Sec 2 caps the tool count at "~25, deliberately narrow", and every new one
    is supposed to need a justification. A test is what turns that into a decision
    somebody has to make on purpose rather than a number that drifts."""
    import asyncio

    from perfstudio.mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}

    # Raised from 34 for the netlist group (create_net, connect_pins, disconnect_pins,
    # update_net, delete_net), which is argued at length in server.py's module docstring:
    # without it an agent could place, draw and route but could not state the intent all
    # three are measured against.
    assert len(tools) <= 39, f"{len(tools)} tools; see the note in server.py before adding more"
    for critical in ("render_2d_view", "render_3d_view", "snapshot", "restore"):
        assert critical in names, f"{critical} is named in PLAN.md Sec 9.2 as load-bearing"
    assert all(tool.description for tool in tools), "a tool with no description is unusable"


def test_nothing_in_the_mcp_package_writes_to_stdout() -> None:
    """On stdio, stdout IS the protocol. One stray print corrupts the stream and the
    client reports something baffling and unrelated -- PLAN.md Sec 9.1's named trap."""
    mcp_dir = REPO_ROOT / "src" / "perfstudio" / "mcp"
    for source in mcp_dir.glob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "file=sys.stderr" in stripped:
                continue
            assert not stripped.startswith("print("), f"{source.name}:{number} prints to stdout"


def test_reroute_replaces_the_routing_rather_than_adding_to_it(loaded: BoardSession) -> None:
    """autoroute ADDS; after a part moves that grows the board every time, because the
    copper laid for the old position still joins the right pins and nothing flags it."""
    loaded.autoroute()
    fresh = loaded.get_status()["conductors"]

    loaded.move_component("R1", "A15")
    loaded.autoroute()
    assert loaded.get_status()["conductors"] > fresh

    result = loaded.reroute()

    assert result["ok"] and result["committed"]
    assert result["ripped_up"] > 0
    assert loaded.get_status()["conductors"] == fresh
    assert loaded.run_lvs()["opens"] == 0


def test_reroute_is_one_undo_step(loaded: BoardSession) -> None:
    loaded.autoroute()
    before = loaded.get_status()["conductors"]
    depth = len(loaded.history())

    loaded.reroute()

    assert len(loaded.history()) == depth + 1
    loaded.undo()
    assert loaded.get_status()["conductors"] == before


def test_reroute_without_a_netlist_is_refused(session: BoardSession) -> None:
    result = session.reroute()
    assert result["ok"] is False
    assert result["code"] == "no-netlist"


def test_the_routing_style_changes_which_primitive_is_used(loaded: BoardSession) -> None:
    """A judgement about the builder, per call rather than per session: an agent may want
    the power rails as solder and the signals as wire."""
    solder = BoardSession()
    solder.open_document(str(GOLDEN))
    solder.autoroute(style="solder")
    kinds = {c.kind for c in solder.document.conductors}
    assert not (kinds & {"bare-wire", "insulated-wire"}) or kinds <= {
        "solder-trace", "solder-trace-wired", "insulated-wire"
    }
    assert any(k.startswith("solder-trace") for k in kinds)

    wire = BoardSession()
    wire.open_document(str(GOLDEN))
    wire.autoroute(style="wire")
    assert not any(c.kind.startswith("solder-trace") for c in wire.document.conductors)

    del loaded


def test_an_unknown_routing_style_lists_the_real_ones(session: BoardSession) -> None:
    session.import_netlist(str(NETLIST))
    with pytest.raises(SessionError) as err:
        session.autoroute(style="magic")
    assert "solder" in str(err.value) and "balanced" in str(err.value)


# ---------------------------------------------------------------------------
# Heights: the question a render cannot answer
# ---------------------------------------------------------------------------


def test_check_heights_answers_before_any_limit_is_set(session: BoardSession) -> None:
    """The tallest part decides which enclosure to buy, which is a thing to know BEFORE
    there is an enclosure. So this reports whether or not a limit exists."""
    session.place_component("Q1", "to220", "C3")
    session.place_component("R1", "r-axial-4", "H3")

    heights = session.check_heights()

    assert heights["height_limit_mm"] is None
    assert heights["tallest_ref"] == "Q1"
    assert heights["tallest_mm"] == 20
    assert heights["over_limit"] == []
    assert [p["ref"] for p in heights["parts"]] == ["Q1", "R1"]


def test_check_heights_names_what_is_over_the_limit(session: BoardSession) -> None:
    session.place_component("Q1", "to220", "C3")
    session.place_component("R1", "r-axial-4", "H3")
    assert session.set_height_limit(15.0)["ok"]

    heights = session.check_heights()

    assert heights["height_limit_mm"] == 15.0
    assert heights["over_limit"] == ["Q1"]


def test_a_height_limit_reaches_drc_and_undoes_like_anything_else(
    session: BoardSession,
) -> None:
    session.place_component("Q1", "to220", "C3")
    assert not [v for v in session.run_drc()["violations"] if v["rule"] == "component-too-tall"]

    session.set_height_limit(15.0)
    flagged = [v for v in session.run_drc()["violations"] if v["rule"] == "component-too-tall"]
    assert len(flagged) == 1
    assert "Q1" in flagged[0]["message"]

    session.undo()
    assert session.get_board_info().get("height_limit_mm") is None


def test_check_heights_will_not_let_an_unmeasured_part_read_as_a_pass() -> None:
    """A component whose footprint is not in the library has no height. Saying nothing
    about it would let "over_limit is empty" mean two different things.

    The document is built rather than placed through the session, because the session
    refuses an unknown footprint outright — the only way to hold one is to open a file
    that references it, which is exactly when this matters.
    """
    document = new_board(cols=24, rows=16)
    document = dataclasses.replace(
        document,
        components=(
            ComponentInstance(
                id="cmp-1", ref="R1", value="", footprint_id="r-axial-4",
                anchor=HoleCoord(2, 2), rotation=0, mirrored=False, locked=False,
            ),
            ComponentInstance(
                id="cmp-2", ref="X1", value="", footprint_id="not-a-real-footprint",
                anchor=HoleCoord(8, 2), rotation=0, mirrored=False, locked=False,
            ),
        ),
    )

    heights = BoardSession(document=document).check_heights()

    assert heights["unknown_footprints"] == ["X1"]
    assert [p["ref"] for p in heights["parts"]] == ["R1"]


# ---------------------------------------------------------------------------
# The netlist, without KiCad
#
# Until these existed, import_netlist was the only way a net could enter a document: an
# agent could place parts, draw copper and route, but could not produce the INTENT all
# three are measured against. On a board that never had a schematic there was no
# ratsnest, so nothing for autoroute to route and nothing for run_lvs to check.
# ---------------------------------------------------------------------------


def test_an_agent_can_declare_a_net_and_fill_it(session: BoardSession) -> None:
    session.place_component("R1", "r-axial-5", "C3")
    session.place_component("R2", "r-axial-5", "C8")

    created = session.create_net("GND", "ground", ["R1.1"])
    added = session.connect_pins("GND", ["R2.2"])

    assert created["ok"] and added["ok"], (created, added)
    net = session.get_nets()[0]
    assert (net["name"], net["net_class"]) == ("GND", "ground")
    assert net["pins"] == ["R1.1", "R2.2"]


def test_a_hand_built_netlist_gives_the_ratsnest_something_to_route(
    session: BoardSession,
) -> None:
    """The point of the whole group: with no net there is nothing to route, so a board
    that never had a schematic could not be autorouted at all."""
    session.place_component("R1", "r-axial-5", "C3")
    session.place_component("R2", "r-axial-5", "C8")
    assert session.get_status()["unrouted_connections"] == 0

    session.create_net("SIG", "signal", ["R1.2", "R2.1"])

    assert session.get_status()["unrouted_connections"] == 1
    routed = session.autoroute()
    assert routed["ok"] and routed["routed"] == 1, routed
    assert session.get_status()["unrouted_connections"] == 0


def test_a_pin_that_belongs_to_another_net_is_refused_as_data(session: BoardSession) -> None:
    session.place_component("R1", "r-axial-5", "C3")
    session.create_net("GND", "ground", ["R1.1"])
    session.create_net("+5V", "power")

    result = session.connect_pins("+5V", ["R1.1"])

    assert result["ok"] is False
    assert result["code"] == "pin-in-another-net"
    assert "GND" in result["message"]


def test_a_pin_naming_a_part_that_is_not_placed_yet_is_allowed_and_reported(
    session: BoardSession,
) -> None:
    """Declaring the circuit and then placing what it asks for is a real order of work --
    it is what importing a netlist does -- but an agent that meant U1 and typed U11 needs
    to hear about it now rather than from LVS at the end."""
    result = session.create_net("GND", "ground", ["U11.8"])

    assert result["ok"], result
    assert result["unplaced_pins"] == ["U11"]


def test_a_malformed_pin_raises_rather_than_refusing(session: BoardSession) -> None:
    """The same split the hole parser makes: "did not make sense" is not "no"."""
    with pytest.raises(SessionError, match=r"U1\.8"):
        session.create_net("GND", "ground", ["U1 pin 8"])


def test_disconnecting_a_pin_takes_it_off_and_undoes_as_one_step(
    session: BoardSession,
) -> None:
    session.place_component("R1", "r-axial-5", "C3")
    session.create_net("GND", "ground", ["R1.1", "R1.2"])
    depth = len(session.history())

    removed = session.disconnect_pins("GND", ["R1.1", "R1.2"])

    assert removed["ok"], removed
    assert session.get_nets()[0]["pins"] == []
    assert len(session.history()) == depth + 1
    session.undo()
    assert session.get_nets()[0]["pins"] == ["R1.1", "R1.2"]


def test_current_and_voltage_can_only_be_stated_here_and_are_not_erased_by_omission(
    session: BoardSession,
) -> None:
    """No netlist format carries either, and DRC's capacity and creepage rules and the
    guide's wire gauge are all silent without them -- so update_net is their only route
    into a document, and an agent that omits a field has not asked to erase it."""
    session.create_net("+12V", "power")

    session.update_net("+12V", current_a=2.5, voltage_v=12.0)
    assert session.get_nets()[0]["current_a"] == 2.5

    session.update_net("+12V", new_name="+12V rail")
    net = session.get_nets()[0]
    assert (net["name"], net["current_a"], net["voltage_v"]) == ("+12V rail", 2.5, 12.0)

    session.update_net("+12V rail", clear_current=True)
    net = session.get_nets()[0]
    assert net["current_a"] is None and net["voltage_v"] == 12.0


def test_deleting_a_net_keeps_the_copper_and_releases_its_claim(session: BoardSession) -> None:
    session.place_component("R1", "r-axial-5", "C3")
    session.place_component("R2", "r-axial-5", "C8")
    session.create_net("SIG", "signal", ["R1.2", "R2.1"])
    session.autoroute()
    conductors = session.get_status()["conductors"]
    assert conductors > 0

    removed = session.delete_net("SIG")

    assert removed["ok"], removed
    assert session.get_nets() == []
    assert session.get_status()["conductors"] == conductors


def test_an_unknown_net_name_raises_and_lists_the_ones_there_are(
    session: BoardSession,
) -> None:
    session.create_net("GND", "ground")

    with pytest.raises(SessionError, match="GND"):
        session.connect_pins("GROUND", ["R1.1"])
