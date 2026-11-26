"""同一物料的双口进货候选，保留每条支路的配平和接入设备。"""

from collections import defaultdict
from dataclasses import replace
from itertools import combinations

from .collision import collision_error, enclose_footprints
from .geometry import JUNCTIONS, OFFSETS, inlet_output_ports, offset, oriented_geometry
from .layout import assess, layout_rank, power_grid, verify
from .model import Infeasible
from .reroute import connections, distance, endpoint_leads, rerouted_layouts
from .simulate import simulate


def source_groups(problem, layout, paths):
    starts = defaultdict(list)
    for route in paths:
        starts[route.start].append(route)
    supplies = defaultdict(list)
    for p in layout.placements:
        if p.building != "inlet":
            continue
        point, = inlet_output_ports(problem, p)
        route, = starts[point]
        supplies[p.item].append((p, route))
        if layout.transport.get(route.end) == "split" and len(starts[route.end]) == 2:
            yield (p,), tuple(starts[route.end]), route
    for supply in supplies.values():
        for (left, a), (right, b) in combinations(supply, 2):
            if abs(left.x - right.x) + abs(left.y - right.y) <= 8:
                yield (left, right), (a, b), None


def proposed_origins(problem, layout, seed, branches, fixed, limit):
    geometries = {facing: oriented_geometry("inlet_large", problem.buildings["inlet_large"], facing)
                  for facing in OFFSETS}
    proposals = []
    for facing, geometry in geometries.items():
        w, h = geometry["size"]
        ax, ay = geometry["anchor"]
        for y in range(max(0, seed.y - 3), min(layout.height - h + 1, seed.y + 4)):
            for x in range(max(0, seed.x - 3), min(layout.width - w + 1, seed.x + 4)):
                p = replace(seed, building="inlet_large", direction=facing, x=x, y=y, width=w, height=h,
                            anchor=(x + ax, y + ay), rate=sum(branch.edges[0].rate for branch in branches))
                occupied = fixed | set(p.cells())
                if len(occupied) != len(fixed) + w * h:
                    continue
                ports = inlet_output_ports(problem, p)
                for port_order in (ports, ports[::-1]):
                    routes = tuple(replace(branch, start=point, incoming=OFFSETS[facing])
                                   for branch, point in zip(branches, port_order))
                    leads = set().union(*(endpoint_leads(route) for route in routes))
                    if any(lead in occupied or not (0 <= lead[0] < layout.width and 0 <= lead[1] < layout.height)
                           for lead in leads):
                        continue
                    length = sum(distance(offset(route.start, facing), route.end) for route in routes)
                    proposals.append((length, distance(p.anchor, seed.anchor), p, routes))
    for _, _, p, routes in sorted(proposals, key=lambda row: row[:2])[:limit]:
        yield p, routes


def improve_large_inlets(problem, layout, accept, limit=8):
    """在当前布局尝试合并进货，只有净空、建材及仿真都合格才纳入比较。"""
    if "inlet_large" not in problem.buildings:
        return layout, 0
    best, checked = layout, 0
    for draft in (layout, with_left_margin(problem, layout)):
        if draft is None:
            continue
        best, searched = search_inlets(problem, draft, best, accept, limit)
        checked += searched
    return best, checked


def with_left_margin(problem, layout):
    """现有紧凑产线左缘过窄时，比较留出两列进货转接格的方案。"""
    margin = 2
    bound = problem.plot_limits[0]
    if (layout.statistics["simulation"]["last_four_per_minute"] >= float(problem.rate) and
            problem.objective[0] == "area") or bound is not None and layout.width + margin > bound:
        return None

    def shift(point):
        return point[0] + margin, point[1]

    placements = {p: replace(p, x=p.x + margin, anchor=shift(p.anchor)) for p in layout.placements}
    return replace(layout, width=layout.width + margin, placements=list(placements.values()),
                   transport={shift(point): kind for point, kind in layout.transport.items()},
                   segments=[replace(s, start=shift(s.start), end=shift(s.end)) for s in layout.segments],
                   machines=[(item, rate, placements[p]) for item, rate, p in layout.machines],
                   wires=set(), power_entry=(0, 0))


def search_inlets(problem, layout, best, accept, limit):
    checked = 0
    paths = connections(layout)
    if not paths:
        return best, 0
    for supplies, branches, trunk in source_groups(problem, layout, paths):
        remaining = [p for p in layout.placements if p not in supplies]
        transport = layout.transport
        if trunk is not None:
            transport = {cell: kind for cell, kind in transport.items() if cell != trunk.end}
        pending = [route for route in paths if route not in branches and route != trunk]
        fixed = {point for p in remaining for point in p.cells()}
        fixed.update(offset(p.anchor, p.direction) for p in remaining if p.building == "outlet")
        fixed.update(point for point, kind in transport.items() if kind in JUNCTIONS)
        draft = replace(layout, transport=transport)
        for inlet, routes in proposed_origins(problem, layout, supplies[0], branches, fixed, limit):
            placements = [*remaining, inlet]
            if collision_error(problem, placements, {pos: kind for pos, kind in transport.items() if kind in JUNCTIONS}):
                continue
            outputs = {route.start: (route.start, route.incoming) for route in routes}
            candidates = rerouted_layouts(problem, draft, placements=placements, port_changes=({}, outputs),
                                          paths=[*pending, *routes], max_routes=0)
            for candidate in candidates:
                candidate = enclose_footprints(problem, candidate)
                if any(bound is not None and value > bound
                       for value, bound in zip((candidate.width, candidate.height), problem.plot_limits)):
                    continue
                anchors = {p.anchor for _, _, p in candidate.machines if problem.buildings[p.building].get("power", 0)}
                if anchors and not candidate.wires:
                    candidate.wires, candidate.power_entry = power_grid(anchors, candidate.width, candidate.height)
                try:
                    verify(problem, candidate)
                    assess(problem, candidate)
                except Infeasible:
                    continue
                if not accept(candidate):
                    continue
                checked += 1
                candidate.statistics["simulation"] = simulate(problem, candidate)
                if candidate.statistics["simulation"]["last_four_per_minute"] < float(problem.rate) - 1e-8:
                    continue
                if layout_rank(problem, candidate) < layout_rank(problem, best):
                    candidate.profile = f"{candidate.profile}／双口进货"
                    candidate.logistics_milp = {}
                    best = candidate
    return best, checked
