# Prior art and clean-room record

This document records what was studied before PerfStudio was written, and how. It
exists so the provenance of the design is auditable, and so contributors know which
sources they may and may not read.

## Licence boundaries — read this before contributing

PerfStudio is Apache-2.0. Two existing tools in this space are GPL-3.0:

- **DIY Layout Creator** (bancika/diy-layout-creator) — GPL-3.0
- **VeroRoute** (Alex Lawrow, SourceForge) — GPL-3.0

**Do not read, copy, port or adapt source code from either project.** Their code cannot
be incorporated into an Apache-2.0 work. Studying their published behaviour, screenshots,
documentation and user discussion is fine and is what we did.

One project in this space is permissively licensed:

- **striprouter** (rogerdahl/striprouter) — MIT

Its published algorithm description may be referenced and is credited below.

## What existing tools do, and what they leave undone

| Tool | Netlist import | Autoroute | DRC | Build guide | 3D | Agent API | Open |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| DIY Layout Creator | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| VeroRoute | ✓ | ✓ | partial | ✗ | ✗ | ✗ | ✓ |
| striprouter | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ |
| PerfBoard.app | ✗ | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ |
| VeeCAD | ✓ | partial | partial | ✗ | ✗ | ✗ | ✗ |

No existing tool combines netlist-driven layout, autorouting, design-rule checking,
a detailed build guide and an agent-facing API. That combination is PerfStudio's
reason to exist.

### Recurring user complaints (from public forums)

These shaped the design more than any feature list did:

- *"It auto-routed but left four connections it couldn't route, which were then
  practically impossible to finish by hand."* → we position the router as an
  interactive assistant, and unroutable nets are reported explicitly rather than
  silently abandoned.
- *"Stripboard Magic is clunky, buggy, and only handles small circuits."*
- *"With VeeCAD you have to draw all your own components."* → parametric footprint
  and body generation.
- *"I can't find anything that converts a schematic to a board layout with the track
  breaks marked."*

## Influences, credited

**striprouter (MIT)** — its published description of combining topological ordering
with a genetic algorithm ("Topo-GA"), and of using uniform-cost search restricted to
the board's connection axes, informed our thinking about search over a constrained
grid. Our router is an independent implementation: A*/Lee maze routing with rip-up
and reroute over a layered cost graph, with a cost model specific to the six
conductor kinds described in PLAN.md §4.4.

**InteractiveHtmlBom (openscopeproject, MIT)** — the pattern of a single
self-contained offline HTML file for board assembly, with click-to-correlate between
a parts list and a board view, is the proven model our build guide export follows.
No code is shared; the idea is public and widely reimplemented.

**KiCad** — we parse KiCad's S-expression netlist format. The format is documented
publicly and the parser is written from the specification and from example files.

### KiCad library data

KiCad's symbol, footprint and 3D model libraries are CC-BY-SA-4.0 with an exception
that waives share-alike for *designs produced using* the library. That exception
covers a user's board; it does **not** cover redistributing the library itself inside
our application.

Consequence: we generate footprints and 3D bodies parametrically instead. If we ever
bundle KiCad library data, it must live in its own directory with its own LICENSE and
attribution, and that decision must be recorded here.

## Sources consulted

- VeroRoute — https://sourceforge.net/projects/veroroute/
- striprouter — https://github.com/rogerdahl/striprouter
- DIY Layout Creator — https://github.com/bancika/diy-layout-creator
- PerfBoard.app — https://perfboard.app/about
- InteractiveHtmlBom — https://github.com/openscopeproject/InteractiveHtmlBom
- KiCad netlist parser reference — https://docs.kicad.org/doxygen/classKICAD__NETLIST__PARSER.html
- KiCad libraries licence — https://www.kicad.org/libraries/license/
