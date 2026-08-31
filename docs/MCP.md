# Driving PerfStudio from an agent

PerfStudio ships an [MCP](https://modelcontextprotocol.io) server, so a model can design
a board, verify it and produce the soldering guide without a GUI. It drives the same
command bus the desktop app does — undo works, the journal is shared, and the agent
cannot reach the document by any other route.

```sh
pip install "perfstudio[mcp]"     # ...or pip install -e ".[mcp]" from a clone
perfstudio-mcp                    # stdio, the primary transport
perfstudio-mcp board.perf         # ...opening a document
perfstudio-mcp --http             # streamable HTTP on localhost
```

`python -m perfstudio.mcp` is the same server and takes the same arguments; the console
script is easier to point a client at, because it lives beside the Python that has
PerfStudio installed rather than needing that Python to be named.

## Registering it

**Claude Code**

```sh
claude mcp add perfstudio -- uvx --from "perfstudio[mcp]" perfstudio-mcp
```

`uvx` fetches the package into its own cache on first use, so nothing has to be installed
first and nothing else on the machine is touched. With PerfStudio already installed,
`claude mcp add perfstudio -- perfstudio-mcp` is the same server without the fetch.

**Anything that reads a JSON config** (Claude Desktop, Antigravity, Cursor, …):

```json
{
  "mcpServers": {
    "perfstudio": {
      "command": "uvx",
      "args": ["--from", "perfstudio[mcp]", "perfstudio-mcp"]
    }
  }
}
```

Without `uv`, use the absolute path to the `perfstudio-mcp` in the environment that has
PerfStudio installed — a virtualenv's `bin/` (or `Scripts\perfstudio-mcp.exe` on Windows).
A bare `python -m perfstudio.mcp` finds whichever Python is first on `PATH`, which is the
usual reason a client reports that the server exited immediately.

## The shape of a session

One board per server process. There is no workspace of several, on purpose: "which board
did that tool mean" is a whole class of confusing failure, avoided by not having it.

Holes are addressed the way people talk about perfboard — column letters then a 1-based
row: `A1`, `C7`, `AC12`. Every tool speaks that and only that. There are no raw
coordinates anywhere in the API, and a test enforces it.

A refused command comes back as data (`{"ok": false, "code": "component-locked", ...}`),
not as an error. An agent has to be able to try something and be told no. Only genuinely
malformed input — a hole address that will not parse, a component that is not there —
raises, and the message names what would have worked.

## A working order

```
get_status                     → where is this board
import_netlist                 → ...or create_net / connect_pins, with no KiCad at all
place_component
optimize_placement             → apply=False first if you want to look
autoroute                      → reports every connection it could NOT make
run_drc / run_lvs              → is it right
generate_guide                 → is it buildable, and what is missing
```

`snapshot` before anything drastic. Every edit is also undoable one step at a time, and
batched operations (`autoroute`, `optimize_placement`) undo as one step rather than one
conductor at a time.

## The tools

| | |
|---|---|
| **Reading** | `get_status` · `get_board_info` · `list_components` · `get_component` · `get_nets` · `get_net_connections` · `list_footprints` |
| **Seeing** | `render_2d_view` · `render_3d_view` |
| **Documents** | `new_document` · `open_document` · `save_document` · `import_netlist` |
| **Netlist** | `create_net` · `connect_pins` · `disconnect_pins` · `update_net` · `delete_net` |
| **The board** | `set_board` · `add_mounting_hole` · `add_edge_connector` · `cut_track` · `remove_board_feature` |
| **Editing** | `place_component` · `move_component` · `rotate_component` · `set_component_locked` · `delete_component` · `add_wire` · `add_solder_trace` · `remove_stale_conductors` · `set_height_limit` |
| **Planning** | `autoroute` · `optimize_placement` |
| **Verifying** | `run_drc` · `run_lvs` · `check_heights` |
| **Output** | `generate_guide` · `export_pdf` |
| **State** | `snapshot` · `restore` · `undo` · `redo` |

Forty-four, against PLAN.md §2's "~25, deliberately narrow". Each is a verb that cannot
be composed from the others, and the surface was trimmed rather than grown where it
could be: the history listing folded into `get_status`, there is no separate "add solder
bridge" because a bridge is a two-pad solder trace and one concept should not have two
names, and one `remove_board_feature` covers mounting holes, edge connectors and track
cuts because the three differ only in which list the id is in.

**The board group is the newest and the asymmetry it fixes is the argument for it.**
`get_board_info` reported mounting holes and edge connectors that nothing could add, and
the only route to a different board size, material or type was `new_document`, which
throws the work away. `set_board` also carries the board TYPE, which is not a display
setting: on stripboard whole rows of holes arrive already joined, so connectivity, DRC,
the build guide and `autoroute` all answer differently — `autoroute` on one plans cuts
and links rather than traces and wires, and reports the pairs it could not separate.

`check_heights` is the one from PLAN.md §9.2's list that neither render tool can stand
in for: a part too tall for the case looks exactly like one that fits, from every angle
a picture is taken from. It reports every part tallest-first whether or not a limit is
set, because "what decides the enclosure height" is a question worth asking before there
is an enclosure. `set_height_limit` is what makes DRC's `component-too-tall` reachable
without the GUI; it goes through the command bus, so it undoes like everything else.

The two that matter most are the ones PLAN.md §9.2 calls out. `render_2d_view` /
`render_3d_view`, because an agent editing a board it cannot see is working blind — the
solder-side render in particular shows what you would actually see holding the board up,
which is where people make mistakes. And `snapshot` / `restore`, because it has to be
able to try something drastic and get back out.

## Things worth knowing

**Rendering needs Qt.** `render_2d_view`, `render_3d_view` and `export_pdf` import
PySide6 and VTK lazily, so a headless or engine-only install still gets every other
tool; the render tools report their absence instead of taking the server down at import.

**A netlist no longer has to come from KiCad.** `create_net` / `connect_pins` build one
up on the board itself, which is what makes `autoroute` and `run_lvs` reachable on a
board that never had a schematic — without a net there is no ratsnest, and so nothing to
route and nothing to check. `import_netlist` still replaces the netlist wholesale,
because that is what re-exporting a schematic means; the others edit it. A pin belongs
to exactly one net, so `connect_pins` refuses one that another net already holds rather
than moving it, and naming a part that is not on the board yet is allowed and reported
back as `unplaced_pins` — declaring the circuit first and placing it afterwards is a
real order of work.

**`restore` clears the undo stack.** It replaces the session's bus, because there is no
command meaning "become this other document" and inventing one would put a
whole-document payload in the journal. Nothing is lost: the board being replaced is
snapshotted as `before-restore` first, so the way back is another restore.

**`autoroute` clears stale copper first**, exactly as the GUI does, and says how much it
removed. A conductor is stale when the island it sits in no longer holds two pins of the
net it claims — which is the shape of what a moved part leaves behind.

**Unrouted connections are never summarised away.** PLAN.md §13 names "it routed most of
it and left four connections" as the trap every previous perfboard autorouter fell into.
`autoroute` returns each failure with the router's own reason. Usually the answer is
`optimize_placement`, not more routing.

**All logging goes to stderr.** On stdio, stdout *is* the protocol: one stray `print`
corrupts the stream and the client reports something baffling and unrelated. A test
checks that nothing under `perfstudio/mcp/` prints.

## Without MCP at all

The project file is stable-key-order, pretty-printed JSON with a `.perf` extension, so
an agent that can only read and write files can still work: edit the document, and the
desktop app picks it up when reopened. The MCP server is the convenient path, not the
only one.
