"""KiCad netlist importer.

Maps a KiCad 6+ "export" netlist (the S-expression format KiCad writes via
File > Export > Netlist, or the equivalent produced from a schematic) onto
PerfStudio's `Net` / `NetNode` model, so the router and DRC can consume the
schematic's intent (PLAN.md Section 5.1, LVS).

This module is pure: it takes a string and returns data. Reading the .net file from
disk is the caller's job.

Ported from packages/parsers/src/kicad-netlist.ts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..model import Net, NetClass, NetNode
from .sexpr import SExpr, parse_sexpr

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportedComponent:
    ref: str
    value: str
    footprint: str | None = None
    lib_part: str | None = None


@dataclass(frozen=True, slots=True)
class KicadNetlistImport:
    components: tuple[ImportedComponent, ...]
    nets: tuple[Net, ...]
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Net class inference
# ---------------------------------------------------------------------------

_GROUND_NET_NAMES = frozenset({"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "0"})

_POWER_NET_NAMES = frozenset({"VCC", "VDD", "VEE", "VBUS", "V+", "V-", "+5V", "+3V3", "+12V", "-12V"})

#: Matches names like "+5V", "-12V", "3V3", "+3V3": an optional sign, digits, "V", more
#: digits.
_POWER_VOLTAGE_PATTERN = re.compile(r"^[+-]?\d+V\d*$")

_POWER_NET_PREFIXES = ("VCC", "VDD", "VBAT")


def infer_net_class(name: str) -> NetClass:
    """Infer a net's electrical class from its schematic name. Case-insensitive. Kept
    separate from `parse_kicad_netlist` so the classification rules can be
    unit-tested in isolation.
    """
    upper = name.strip().upper()
    if upper in _GROUND_NET_NAMES:
        return "ground"
    if upper in _POWER_NET_NAMES:
        return "power"
    if _POWER_VOLTAGE_PATTERN.match(upper):
        return "power"
    if any(upper.startswith(prefix) for prefix in _POWER_NET_PREFIXES):
        return "power"
    return "signal"


# ---------------------------------------------------------------------------
# S-expression tree helpers
# ---------------------------------------------------------------------------


def _is_tagged_list(node: SExpr, tag: str) -> bool:
    """True if `node` is a list whose first element is the atom `tag` (e.g.
    `(ref ...)`).
    """
    return isinstance(node, list) and len(node) > 0 and node[0] == tag


def _find_child(lst: list[SExpr], tag: str) -> list[SExpr] | None:
    """First child of `lst` that is itself a list tagged `tag`, e.g. `(ref ...)`
    inside `(comp ...)`.
    """
    for item in lst:
        if _is_tagged_list(item, tag):
            assert isinstance(item, list)
            return item
    return None


def _find_children(lst: list[SExpr], tag: str) -> list[list[SExpr]]:
    """All children of `lst` that are lists tagged `tag`, e.g. every `(node ...)`
    inside a `(net ...)`.
    """
    result: list[list[SExpr]] = []
    for item in lst:
        if _is_tagged_list(item, tag):
            assert isinstance(item, list)
            result.append(item)
    return result


def _field_value(lst: list[SExpr] | None, tag: str) -> str | None:
    """Extract the string value of a single-value tagged field, e.g. `(ref "R1")` or
    the unquoted `(ref R1)` -- both parse to the same atom via `parse_sexpr`, so this
    handles KiCad's inconsistent quoting for free.
    """
    if lst is None:
        return None
    field = _find_child(lst, tag)
    if field is None or len(field) < 2:
        return None
    value = field[1]
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

_UNCONNECTED_NET_NAME = re.compile(r"^unconnected-", re.IGNORECASE)


def _pluralize(count: int, noun: str) -> str:
    return f'{count} {noun}{"" if count == 1 else "s"}'


def _parse_components(components_list: list[SExpr] | None, warnings: list[str]) -> list[ImportedComponent]:
    if components_list is None:
        return []

    components: list[ImportedComponent] = []
    for comp in _find_children(components_list, "comp"):
        ref = _field_value(comp, "ref")
        if ref is None:
            warnings.append('Skipped a component with no "ref" field')
            continue

        value = _field_value(comp, "value")
        footprint = _field_value(comp, "footprint")
        if footprint is None:
            # Said, because it is what forces the host to GUESS a footprint from the
            # reference and pin count -- the one step the import dialog asks the user to
            # check, and it had nothing to point them at.
            warnings.append(f'Component "{ref}" has no footprint; one will be guessed from its reference')
        libsource = _find_child(comp, "libsource")
        lib_part = _field_value(libsource, "part") if libsource is not None else None

        components.append(
            ImportedComponent(
                ref=ref,
                value=value if value is not None else "",
                footprint=footprint,
                lib_part=lib_part,
            )
        )
    return components


@dataclass(frozen=True, slots=True)
class _SortableNet:
    net: Net
    #: Numeric KiCad net code, for ordering; NaN if the code was missing or
    #: non-numeric.
    code_key: float


def _parse_int_leading(s: str) -> float:
    """Mimics JS's `Number.parseInt(s, 10)`: parses an optional sign followed by
    leading decimal digits and ignores any trailing garbage, returning NaN if there
    are no leading digits at all.
    """
    match = re.match(r"^\s*([+-]?\d+)", s)
    return float(int(match.group(1))) if match else math.nan


def _parse_nets(nets_list: list[SExpr] | None, warnings: list[str]) -> list[Net]:
    if nets_list is None:
        return []

    unconnected_count = 0
    collected: list[_SortableNet] = []
    seen_ids: set[str] = set()

    for net_form in _find_children(nets_list, "net"):
        code = _field_value(net_form, "code")
        name = _field_value(net_form, "name") or ""

        if _UNCONNECTED_NET_NAME.match(name):
            unconnected_count += 1
            continue

        nodes: list[NetNode] = []
        for node_form in _find_children(net_form, "node"):
            node_ref = _field_value(node_form, "ref")
            pin = _field_value(node_form, "pin")
            if node_ref is None or pin is None:
                warnings.append(f'Skipped a node with missing "ref"/"pin" in net "{name}"')
                continue
            nodes.append(NetNode(component_ref=node_ref, pin=pin))

        if len(nodes) < 2:
            warnings.append(f'net "{name}" has only {_pluralize(len(nodes), "node")}')

        net_id: str
        code_key: float
        if code is not None:
            net_id = f"net-{code}"
            code_key = _parse_int_leading(code)
        else:
            warnings.append(f'Net "{name}" has no "code" field; using a positional id')
            net_id = f"net-{len(collected)}"
            code_key = math.nan
        if net_id in seen_ids:
            # Two nets with one code is a broken export, but it is the user's export and
            # the only alternative was refusing the whole import over one collision --
            # which the command downstream did, with nothing the user could do about it.
            original = net_id
            suffix = 2
            while f"{original}-{suffix}" in seen_ids:
                suffix += 1
            net_id = f"{original}-{suffix}"
            warnings.append(
                f'Net "{name}" shares its code with another net; imported as "{net_id}"'
            )
        seen_ids.add(net_id)

        collected.append(
            _SortableNet(
                net=Net(id=net_id, name=name, nodes=tuple(nodes), net_class=infer_net_class(name)),
                code_key=code_key,
            )
        )

    if unconnected_count > 0:
        warnings.append(f'Skipped {_pluralize(unconnected_count, "unconnected net")}')

    # Deterministic order: ascending numeric code; nets with a missing/non-numeric code
    # (NaN) sort last, tie-broken by id so the result never depends on sort stability
    # alone.
    def _sort_key(item: _SortableNet) -> tuple[int, float, str]:
        is_nan = math.isnan(item.code_key)
        return (1 if is_nan else 0, 0.0 if is_nan else item.code_key, item.net.id)

    collected.sort(key=_sort_key)
    return [c.net for c in collected]


def parse_kicad_netlist(source: str) -> KicadNetlistImport:
    """Parse a KiCad netlist (the `(export (version ...) (components ...) (nets ...))`
    S-expression format) into PerfStudio's component and net model.

    Raises only on malformed S-expression syntax (see `parse_sexpr`) or if the input
    has no top-level `export` form. Anything else recoverable -- a component missing
    its ref, an undersized net, KiCad's `unconnected-*` pseudo-nets -- is reported via
    `warnings` rather than raised, so a partially-off netlist can still be imported.
    """
    forms = parse_sexpr(source)
    root: list[SExpr] | None = None
    for f in forms:
        if _is_tagged_list(f, "export"):
            assert isinstance(f, list)
            root = f
            break
    if root is None:
        # Name what the file IS when it is recognisable: handing the importer the
        # schematic instead of the netlist exported from it is the first-time mistake,
        # and "no export form" does not say which file to use instead.
        head = next(
            (f[0] for f in forms if isinstance(f, list) and f and isinstance(f[0], str)), None
        )
        if head == "kicad_sch":
            raise ValueError(
                "This is a KiCad schematic, not a netlist. In KiCad, use File ▸ Export ▸ "
                "Netlist… and import the .net file it writes."
            )
        if head == "kicad_pcb":
            raise ValueError(
                "This is a KiCad board, not a netlist. Export a netlist from the schematic "
                "(File ▸ Export ▸ Netlist…) and import that."
            )
        raise ValueError('Not a KiCad netlist: no top-level "export" form found')

    warnings: list[str] = []
    components = _parse_components(_find_child(root, "components"), warnings)
    nets = _parse_nets(_find_child(root, "nets"), warnings)

    return KicadNetlistImport(components=tuple(components), nets=tuple(nets), warnings=tuple(warnings))
