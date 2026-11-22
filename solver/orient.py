"""围绕现有物流端点生成换向、进货和邻近出货候选。"""

from collections import defaultdict
from dataclasses import replace

from .geometry import EAST, NORTH, OFFSETS, OPPOSITE, SOUTH, WEST, observed_directions, offset, oriented_geometry, placement_ports
from .model import belt_options
from .reroute import connections, distance


def terminal(point, edges, transport):
    seen = set()
    while transport.get(point, "").startswith(("belt", "tunnel")) and point not in seen:
        seen.add(point)
        (point,) = edges[point]
    return point


def port_distance(point, facing, goal, incoming):
    step = offset(point, OPPOSITE[facing] if incoming else facing)
    return 1 + abs(step[0] - goal[0]) + abs(step[1] - goal[1])


def candidate_origins(layout, p, geometry, facing, goals):
    width, height = geometry["size"]
    if p.building == "inlet":
        gx, gy = goals[-1][0]
        if facing in (EAST, WEST):
            x = 0 if facing == EAST else layout.width - width
            for y in (gy, gy - 1, gy + 1, gy - 2, gy + 2, p.y):
                yield x, y
        else:
            y = 0 if facing == SOUTH else layout.height - height
            for x in (gx, gx - 1, gx + 1, gx - 2, gx + 2, p.x):
                yield x, y
        return
    ax, ay = geometry["anchor"]
    yield p.anchor[0] - ax, p.anchor[1] - ay
    x = p.x + (p.width - width) // 2
    y = p.y + (p.height - height) // 2
    for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        yield x + dx, y + dy


def placement_trials(problem, layout, *, machines=4, inlets=4):
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for segment in layout.segments:
        incoming[segment.end].append(segment.start)
        outgoing[segment.start].append(segment.end)
    occupied = {cell: p.node for p in layout.placements for cell in p.cells()}
    trials = {False: [], True: []}
    for index, p in enumerate(layout.placements):
        if p.building == "outlet":
            continue
        current_inputs, output = placement_ports(problem, p)
        goals = [(terminal(incoming[point][0], incoming, layout.transport), True) for point in current_inputs]
        ports = list(current_inputs)
        if output is not None:
            ports.append(output)
            goals.append((terminal(outgoing[output][0], outgoing, layout.transport), False))
        previous_length = sum(port_distance(point, p.direction, dest, feeds) for point, (dest, feeds) in zip(ports, goals))
        inlet = p.building == "inlet"
        for facing in observed_directions(p.building):
            if facing == p.direction and not inlet:
                continue
            geometry = oriented_geometry(p.building, problem.buildings[p.building], facing)
            width, height = geometry["size"]
            ax, ay = geometry["anchor"]
            best = None
            origins = candidate_origins(layout, p, geometry, facing, goals)
            for x, y in dict.fromkeys(origins):
                if (x, y, facing) == (p.x, p.y, p.direction) or not (0 <= x <= layout.width - width and 0 <= y <= layout.height - height):
                    continue
                moved = replace(p, x=x, y=y, width=width, height=height, anchor=(x + ax, y + ay), direction=facing)
                if any(cell in occupied and occupied[cell] != p.node for cell in moved.cells()):
                    continue
                ins, out = placement_ports(problem, moved)
                new_ports = (*ins, out) if out is not None else ins
                length = sum(port_distance(point, facing, dest, feeds) for point, (dest, feeds) in zip(new_ports, goals))
                rank = (length - previous_length, abs(x - p.x) + abs(y - p.y), y, x)
                if best is None or rank < best[0]:
                    best = rank, moved
            if best is not None:
                # 左右换边单独保留试跑名额，避免被同侧挪箱全部挤掉。
                opposite_side = inlet and facing in (EAST, WEST) and facing != p.direction
                trials[inlet].append((not opposite_side, best[0], index, facing, best[1]))
    for inlet, budget in ((False, machines), (True, inlets)):
        for same_side, _, index, _, moved in sorted(trials[inlet])[:budget]:
            placements = list(layout.placements)
            placements[index] = moved
            label = ("进货换位" if same_side else "进货换边") if inlet else "机台换向"
            yield placements, label


def outlet_trials(problem, layout, *, limit=8):
    """沿四边搜索靠近末级合并器或工位且朝外的成品接出格。"""
    outlets = [(i, p) for i, p in enumerate(layout.placements) if p.building == "outlet"]
    if not outlets:
        return
    occupied = {point for p in layout.placements for point in p.cells()}
    paths = {route.end: route for route in connections(layout)}
    proposals = []
    for i, p in outlets:
        route = paths.get(p.anchor)
        if route is None or not route.cells or not belt_options(problem, p.rate):
            continue
        x, y = route.start
        old_length = sum(distance(edge.start, edge.end) for edge in route.edges)
        fixed = (occupied - {p.anchor}) | (layout.transport.keys() - set(route.cells))
        merge = layout.transport.get(route.start) == "merge"
        for facing in (EAST, WEST, NORTH, SOUTH):
            if facing in (EAST, WEST):
                column = layout.width - 1 if facing == EAST else 0
                points = ((column, row) for row in range(max(0, y - 2), min(layout.height, y + 3)))
            else:
                row = 0 if facing == NORTH else layout.height - 1
                points = ((column, row) for column in range(max(0, x - 2), min(layout.width, x + 3)))
            for point in points:
                short = distance(route.start, point)
                if point in fixed or short > old_length or (point, facing) == (p.anchor, p.direction):
                    continue
                direction = OFFSETS[facing]
                previous = offset(point, OPPOSITE[facing])
                if previous in fixed and previous != route.start:
                    continue
                if short == 1 and (previous != route.start or (not merge and direction != route.incoming)):
                    continue
                straight = point == offset(route.start, facing, short)
                if merge and straight and offset(route.start, facing) not in fixed:
                    outputs = {route.start: (route.start, direction)}
                else:
                    outputs = {}
                moved = replace(p, x=point[0], y=point[1], anchor=point, direction=facing)
                proposals.append(((short - old_length, short, i, point, facing), moved, outputs))
    for (_, _, i, _, _), moved, outputs in sorted(proposals, key=lambda candidate: candidate[0])[:limit]:
        placements = list(layout.placements)
        old = placements[i]
        placements[i] = moved
        inputs = {old.anchor: (moved.anchor, OFFSETS[moved.direction])}
        yield placements, (inputs, outputs), "成品就近接出"
