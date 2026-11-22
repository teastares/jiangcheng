"""联合移动进货箱及其首级分流器，缩短进入用料工位的干线。"""

from collections import defaultdict
from dataclasses import replace

from .geometry import JUNCTIONS, OFFSETS, offset, oriented_geometry, placement_ports
from .reroute import connections, distance, endpoint_leads, placement_changes, remap_endpoint, step


def _short_feeds(split, into, facing):
    """从分流输入反推箱体出料格和直连或单转角所需的带格。"""
    if facing == into:
        for length in range(1, 4):
            yield step(split, into, -length), tuple(step(split, into, -i) for i in range(1, length))
    elif facing != step((0, 0), into, -1):
        for tail in (1, 2):
            bend = step(split, into, -tail)
            for head in (1, 2):
                cells = tuple(step(split, into, -i) for i in range(1, tail + 1))
                cells += tuple(step(bend, facing, -i) for i in range(1, head))
                yield step(bend, facing, -head), cells


def _available(points, forbidden, bound):
    return all(0 <= x < bound[0] and 0 <= y < bound[1] and (x, y) not in forbidden for x, y in points)


def _path_leads(routes, fixed, junctions, bound):
    leads = set()
    length = 0
    for route in routes:
        cells = endpoint_leads(route)
        if route.start == route.end or not _available(cells, fixed | leads, bound):
            return None
        if not cells and route.start in junctions and route.end in junctions:
            return None
        leads.update(cells)
        first = step(route.start, route.incoming)
        last = step(route.end, route.outgoing, -1)
        length += 2 + distance(first, last) if cells else 1
    return leads, length


def _supply_origins(inlet, split, into, geometries, blocked, bound):
    for facing, vector in OFFSETS.items():
        geometry = geometries[facing]
        w, h = geometry["size"]
        ax, ay = geometry["anchor"]
        ox, oy = geometry["output"]
        for output, cells in _short_feeds(split, into, vector):
            x, y = output[0] - ox, output[1] - oy
            moved = replace(inlet, x=x, y=y, width=w, height=h, anchor=(x + ax, y + ay), direction=facing)
            if _available(moved.cells(), blocked | set(cells), bound) and _available(cells, blocked, bound):
                yield moved, output, len(cells)


def split_supply_trials(problem, layout, *, placements=None, changes=None, limit=10):
    """保留配平和已指定的端口换位，给分流器与进货箱提供联合重布起点。"""
    base = layout.placements if placements is None else placements
    if changes is None:
        changes = ({}, {}) if placements is None else placement_changes(problem, layout, base)
    if changes is None or limit <= 0:
        return
    inputs, outputs = changes
    paths = connections(layout)
    starts = defaultdict(list)
    indices = {p: index for index, p in enumerate(base) if p.building == "inlet"}
    mapped = []
    for index, route in enumerate(paths):
        starts[route.start].append(index)
        start, out = outputs.get(route.start, (route.start, route.incoming))
        end, into = inputs.get(route.end, (route.end, route.outgoing))
        mapped.append(replace(route, start=start, incoming=out, end=end, outgoing=into))
    groups = []
    for p in layout.placements:
        index = indices.get(p)
        if index is None:
            continue
        _, source = placement_ports(problem, p)
        if len(starts[source]) != 1 or source in outputs:
            continue
        feed, = starts[source]
        split = paths[feed].end
        if layout.transport.get(split) == "split" and split not in inputs and split not in outputs:
            branches = starts[split]
            if not inputs or any(paths[k].end in inputs for k in branches):
                groups.append((index, source, feed, split, branches))
    if not groups:
        return

    occupied = {point for p in base for point in p.cells()}
    occupied.update(offset(p.anchor, p.direction) for p in base if p.building == "outlet")
    junctions = {point for point, kind in layout.transport.items() if kind in JUNCTIONS}
    geometries = {facing: oriented_geometry("inlet", problem.buildings["inlet"], facing) for facing in OFFSETS}
    bound = layout.width, layout.height
    proposals = []
    for index, source, feed, split, branches in groups:
        inlet = base[index]
        group = {feed, *branches}
        fixed = (occupied - set(inlet.cells())) | (junctions - {split})
        other_routes = (route for i, route in enumerate(mapped) if i not in group)
        other_leads = _path_leads(other_routes, fixed, junctions, bound)
        if other_leads is None:
            continue
        fixed.update(other_leads[0])
        old_length = sum(distance(edge.start, edge.end) for k in group for edge in paths[k].edges)
        sites = ((x, y) for x in range(split[0] - 2, split[0] + 3) for y in range(split[1] - 2, split[1] + 3))
        for point in sites:
            if distance(split, point) > 2 or not _available((point,), fixed, bound):
                continue
            new_junctions = (junctions - {split}) | {point}
            blocked = fixed | {point}
            for quarters in range(4):
                junction_moves = {split: (point, quarters)}
                _, into = remap_endpoint(split, paths[feed].outgoing, {}, junction_moves)
                reroutes = (replace(mapped[k], start=point,
                                    incoming=remap_endpoint(split, paths[k].incoming, {}, junction_moves)[1])
                            for k in branches)
                branch_leads = _path_leads(reroutes, blocked, new_junctions, bound)
                if branch_leads is None:
                    continue
                leads, branch_length = branch_leads
                for moved, output, gap in _supply_origins(inlet, point, into, geometries, blocked | leads, bound):
                    if moved == inlet and point == split and quarters == 0:
                        continue
                    # 同等走线优先把补货格留在外缘，再优先缩短供料干线。
                    x, y = moved.anchor
                    inset = min(x, y, bound[0] - 1 - x, bound[1] - 1 - y)
                    rank = branch_length + gap + 1 - old_length, inset, gap, distance(split, point)
                    proposals.append((rank, index, source, output, moved, junction_moves))
    for _, index, source, output, moved, junction_moves in sorted(proposals, key=lambda p: p[0])[:limit]:
        proposed = list(base)
        proposed[index] = moved
        supply_outputs = {**outputs, source: (output, OFFSETS[moved.direction])}
        yield proposed, (inputs, supply_outputs), junction_moves, "进货与分流联动"
