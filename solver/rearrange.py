"""搜索贴近用料端的进货箱和成组换位。"""

from collections import Counter, defaultdict
from dataclasses import replace
from itertools import combinations

from .geometry import OFFSETS, OPPOSITE, offset, oriented_geometry, placement_ports
from .reroute import connections, distance, placement_changes, step


def reordered_inputs(problem, network, ordering):
    """为 Steam 同类并行设备生成交错或翻转的物理进料口。"""
    rows_by_node = {}
    copies = Counter()
    for node in network.nodes:
        if node.kind != "machine":
            continue
        count = len(problem.recipes[node.item]["inputs"])
        if count < 2:
            continue
        if ordering == 2 or copies[node.item] % 2 == ordering:
            rows = problem.geometry(node)["inputs"][:count]
            rows_by_node[node.id] = dict(zip(rows, reversed(rows)))
        copies[node.item] += 1
    buses = [replace(bus, sinks=tuple(replace(p, row=rows_by_node[p.node][p.row]) if p.node in rows_by_node else p
                                      for p in bus.sinks)) for bus in network.buses]
    return replace(network, buses=buses)


def short_supply_trials(problem, layout, *, per_inlet=4):
    paths = {route.start: route for route in connections(layout)}
    occupied = {cell for p in layout.placements for cell in p.cells()}
    occupied.update(point for point, kind in layout.transport.items() if kind in ("split", "merge"))

    for index, inlet in enumerate(layout.placements):
        if inlet.building != "inlet":
            continue
        _, source = placement_ports(problem, inlet)
        route = paths.get(source)
        if route is None or len(route.edges) <= 1:
            continue
        free = occupied.difference(inlet.cells())
        last = step(route.end, route.outgoing, -1)
        options = []
        for facing, vector in OFFSETS.items():
            geometry = oriented_geometry("inlet", problem.buildings["inlet"], facing)
            w, h = geometry["size"]
            ax, ay = geometry["anchor"]
            ox, oy = geometry["output"]
            for gap in range(3):
                if not gap and vector != route.outgoing:
                    continue
                x, y = offset(last, OPPOSITE[facing], gap)
                x, y = x - ox, y - oy
                if not (0 <= x <= layout.width - w and 0 <= y <= layout.height - h):
                    continue
                moved = replace(inlet, x=x, y=y, width=w, height=h, anchor=(x + ax, y + ay), direction=facing)
                if moved == inlet or free.intersection(moved.cells()):
                    continue
                options.append((gap, distance(moved.anchor, route.end), facing, moved))
        for _, _, _, moved in sorted(options, key=lambda trial: trial[:3])[:per_inlet]:
            placements = list(layout.placements)
            placements[index] = moved
            yield placements, "就近进货"


def neighboring_supply_swaps(problem, layout):
    """交换给同一多料设备进料的相邻工位，并同步交换该设备的物料入口。"""
    if not problem.catalog.get("blueprint_format"):
        return
    paths = connections(layout)
    upstream = defaultdict(list)
    downstream = {}
    for index, p in enumerate(layout.placements):
        _, output = placement_ports(problem, p)
        if output is not None and p.building != "inlet":
            downstream[output] = index
    for route in paths:
        if route.start in downstream:
            upstream[route.end].append((downstream[route.start], route))
    moves = []
    for sink in layout.placements:
        ports, _ = placement_ports(problem, sink, all_inputs=True)
        branches = [pair for port in ports for pair in upstream[port]]
        for (a, left), (b, right) in combinations(branches, 2):
            p, q = layout.placements[a], layout.placements[b]
            if (p.item == q.item or (p.width, p.height, p.direction) != (q.width, q.height, q.direction) or
                    distance((p.x, p.y), (q.x, q.y)) > max(p.width, p.height)):
                continue
            if sum(route.start in (left.start, right.start) for route in paths) != 2:
                continue
            moves.append((a, b, left, right))
    if not moves:
        return

    def candidate(group):
        placements = list(layout.placements)
        switched = {}
        for a, b, left, right in group:
            p, q = placements[a], placements[b]
            placements[a] = replace(p, x=q.x, y=q.y, anchor=q.anchor)
            placements[b] = replace(q, x=p.x, y=p.y, anchor=p.anchor)
            switched.update({left.end: (right.end, right.outgoing), right.end: (left.end, left.outgoing)})
        inputs, outputs = placement_changes(problem, layout, placements)
        return placements, ({**inputs, **switched}, outputs), "相邻工位换位"

    used, combined = set(), []
    for move in moves:
        a, b, _, _ = move
        if {a, b}.isdisjoint(used):
            combined.append(move)
            used.update((a, b))
    if len(combined) > 1:
        yield candidate(combined)
    for move in moves[:6]:
        yield candidate((move,))
