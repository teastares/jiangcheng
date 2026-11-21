"""围绕现有物流端点生成换向与两侧进货候选。"""

from collections import defaultdict
from dataclasses import replace

from .geometry import EAST, NORTH, OPPOSITE, SOUTH, WEST, observed_directions, offset, oriented_geometry, placement_ports


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
