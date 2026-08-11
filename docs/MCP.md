# Driving PerfStudio from an agent

PerfStudio ships an [MCP](https://modelcontextprotocol.io) server, so a model can design
a board, verify it and produce the soldering guide without a GUI. It drives the same
command bus the desktop app does — undo works, the journal is shared, and the agent
cannot reach the document by any other route.

```sh
pip install -e ".[mcp]"
python -m perfstudio.mcp          # stdio, the primary transport
python -m perfstudio.mcp board.perf   # ...opening a document
python -m perfstudio.mcp --http   # streamable HTTP on localhost
```

## Registering it

**Claude Code**

```sh
claude mcp add perfstudio -- python -m perfstudio.mcp
```

**Anything that reads a JSON config** (Claude Desktop, Antigravity, Cursor, …):

```json
{
  "mcpServers": {
    "perfstudio": {
      "command": "python",
      "args": ["-m", "perfstudio.mcp"]
    }
  }
}
```

Use the absolute path to the Python that has PerfStudio installed if it is not the one
on `PATH` — a virtualenv's `bin/python` (or `Scripts\python.exe` on Windows).

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
import_netlist / place_component
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
| **Editing** | `place_component` · `move_component` · `rotate_component` · `set_component_locked` · `delete_component` · `add_wire` · `add_solder_trace` · `remove_stale_conductors` |
| **Planning** | `autoroute` · `optimize_placement` |
| **Verifying** | `run_drc` · `run_lvs` |
| **Output** | `generate_guide` · `export_pdf` |
| **State** | `snapshot` · `restore` · `undo` · `redo` |

Thirty-one, against PLAN.md §2's "~25, deliberately narrow". Each is a verb that cannot
be composed from the others, and the surface was trimmed rather than grown: the history
listing folded into `get_status`, and there is no separate "add solder bridge" because a
bridge is a two-pad solder trace and one concept should not have two names.

The two that matter most are the ones PLAN.md §9.2 calls out. `render_2d_view` /
`render_3d_view`, because an agent editing a board it cannot see is working blind — the
solder-side render in particular shows what you would actually see holding the board up,
which is where people make mistakes. And `snapshot` / `restore`, because it has to be
able to try something drastic and get back out.

## Things worth knowing

**Rendering needs Qt.** `render_2d_view`, `render_3d_view` and `export_pdf` import
PySide6 and VTK lazily, so a headless or engine-only install still gets every other
tool; the render tools report their absence instead of taking the server down at import.

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
