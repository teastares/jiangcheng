"""在固定设备和分合流之间重新搜索单物料物流路径。"""

from collections import defaultdict
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from itertools import count

from .geometry import OFFSETS, placement_ports


STEPS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def step(point, heading, span=1):
    return point[0] + heading[0] * span, point[1] + heading[1] * span


def heading(start, end):
    return tuple((b > a) - (b < a) for a, b in zip(start, end))


def distance(start, end):
    return sum(abs(a - b) for a, b in zip(start, end))


@dataclass(frozen=True)
class Connection:
    edges: tuple
    start: tuple
    incoming: tuple
    end: tuple
    outgoing: tuple

    @property
    def cells(self):
        return tuple(edge.end for edge in self.edges[:-1])


def connections(layout):
    fixed = {point for p in layout.placements for point in p.cells()}
    fixed.update(point for point, kind in layout.transport.items() if kind in ("split", "merge"))
    outgoing = defaultdict(list)
    for edge in layout.segments:
        outgoing[edge.start].append(edge)
    seen = set()
    result = []
    for edge in layout.segments:
        if edge.start not in fixed:
            continue
        path = [edge]
        seen.add(edge)
        end = edge.end
        while end not in fixed:
            if len(outgoing[end]) != 1 or outgoing[end][0] in seen:
                return []
            next_edge = outgoing[end][0]
            seen.add(next_edge)
            path.append(next_edge)
            end = next_edge.end
        result.append(Connection(tuple(path), edge.start, heading(edge.start, edge.end), end,
                                 heading(path[-1].start, end)))
    return result if len(seen) == len(layout.segments) else []


def tunnel_segments(edges, transport):
    return [(edge.start, edge.end, transport[edge.start]) for edge in edges if distance(edge.start, edge.end) > 1]


def compatible_tunnel(start, end, kind, existing, span):
    horizontal = start[1] == end[1]
    axis = 0 if horizontal else 1
    other = 1 - axis
    first, last = sorted((start[axis], end[axis]))
    direction = 1 if start[axis] < end[axis] else -1
    for a, b, old_kind in existing:
        if kind != old_kind or not a[other] == b[other] == start[other]:
            continue
        low, high = sorted((a[axis], b[axis]))
        if max(first, low) <= min(last, high):
            return False
        if (b[axis] - a[axis]) * direction > 0:
            if (2 <= (b[axis] - start[axis]) * direction <= span or
                    2 <= (end[axis] - a[axis]) * direction <= span):
                return False
    return True


def route_costs(problem, objective):
    names = {belt[key] for belt in problem.belts for key in ("building", "underground") if belt.get(key)}
    prices = {}
    for name in names:
        building = problem.buildings[name]
        materials = sum(building.get("materials", {}).values())
        metrics = {"buildings": 1, "lines": 1, "materials": materials, "power": building.get("power", 0)}
        prices[name] = tuple(metrics[key] for key in objective if key in metrics) + (1, materials)
    return prices


def add_cost(left, right):
    return tuple(a + b for a, b in zip(left, right))


def search_route(problem, layout, connection, blocked, existing, prices, ceiling=None):
    source, sink = connection.start, connection.end
    first = step(source, connection.incoming)
    last = step(sink, connection.outgoing, -1)
    if first == sink and connection.incoming == connection.outgoing:
        if all(layout.transport.get(point) in ("split", "merge") for point in (source, sink)):
            return None
        return [replace(connection.edges[0], start=source, end=sink)], {}

    load = connection.edges[0].rate
    options = [belt for belt in problem.belts if belt["capacity"] >= load]
    if not options:
        return None
    belt = min((level["building"] for level in options), key=lambda key: (prices[key], key))
    tunnels = [(level["underground"], problem.underground_spans[level["underground"]]) for level in options
               if level.get("underground") in problem.underground_spans]

    def allowed(point):
        x, y = point
        return 0 <= x < layout.width and 0 <= y < layout.height and point not in blocked

    if not allowed(first) or not allowed(last):
        return None
    unit = min([prices[belt][0], *(prices[key][0] / length for key, length in tunnels)])
    zero = (0,) * len(prices[belt])
    initial = (first, connection.incoming, "")
    scores, previous = {initial: zero}, {initial: None}
    sequence = count()
    queue = [(distance(first, last) * unit, zero, next(sequence), initial)]
    best_score, best_end = ceiling, None

    while queue:
        estimate, score, _, state = heappop(queue)
        if score != scores[state]:
            continue
        if best_score is not None and estimate > best_score[0] + 1e-9:
            break
        point, entered, landing = state
        reverse = step((0, 0), entered, -1)
        if point == last and connection.outgoing != reverse and (not landing or connection.outgoing == entered):
            total = add_cost(score, prices[landing or belt])
            if best_score is None or total < best_score:
                best_score, best_end = total, state
            continue
        history, previous_tunnels = set(), []
        ancestor = state
        while ancestor is not None:
            pos, _, arrival = ancestor
            history.add(pos)
            parent = previous[ancestor]
            if arrival:
                previous_tunnels.append((parent[0], pos, arrival))
            ancestor = parent
        moves = [(step(point, direction), direction, "", landing or belt) for direction in STEPS
                 if direction != reverse and (not landing or direction == entered)]
        if not landing:
            for key, max_span in tunnels:
                for length in range(2, max_span + 1):
                    destination = step(point, entered, length)
                    if (allowed(destination) and compatible_tunnel(point, destination, key, existing, max_span) and
                            compatible_tunnel(point, destination, key, previous_tunnels, max_span)):
                        moves.append((destination, entered, key, key))
        for destination, direction, arrives_by_tunnel, kind in moves:
            if destination in history or not allowed(destination):
                continue
            cost = add_cost(score, prices[kind])
            successor = (destination, direction, arrives_by_tunnel)
            if successor in scores and cost >= scores[successor]:
                continue
            estimate = cost[0] + unit * distance(destination, last)
            if best_score is not None and estimate > best_score[0] + 1e-9:
                continue
            scores[successor], previous[successor] = cost, state
            heappush(queue, (estimate, cost, next(sequence), successor))
    if best_end is None:
        return None

    chain = []
    state = best_end
    while state is not None:
        chain.append(state)
        state = previous[state]
    chain.reverse()
    cells = [point for point, _, _ in chain]
    types = {point: landing or (chain[index + 1][2] if index + 1 < len(chain) else "") or belt
             for index, (point, _, landing) in enumerate(chain)}
    points = [source, *cells, sink]
    return [replace(connection.edges[0], start=a, end=b) for a, b in zip(points, points[1:])], types


def placement_changes(problem, layout, placements):
    updated = {p.node: p for p in placements}
    if len(placements) != len(layout.placements) or len(updated) != len(placements):
        return None
    inputs, outputs = {}, {}
    for old in layout.placements:
        new = updated.get(old.node)
        if new is None or (new.building, new.item, new.rate) != (old.building, old.item, old.rate):
            return None
        if new == old:
            continue
        old_inputs, old_output = placement_ports(problem, old)
        new_inputs, new_output = placement_ports(problem, new)
        direction = OFFSETS[new.direction]
        inputs.update((a, (b, direction)) for a, b in zip(old_inputs, new_inputs))
        if old_output is not None:
            outputs[old_output] = new_output, direction
    return inputs, outputs


def rerouted_layouts(problem, layout, *, placements=None, max_routes=24, objectives=None):
    """返回保留工序、分合流及物料流量的重布候选，交由调用者校验并仿真。"""
    paths = connections(layout)
    if not paths:
        return
    if placements is None:
        placements = layout.placements
        inputs, outputs = {}, {}
    else:
        changes = placement_changes(problem, layout, placements)
        if changes is None:
            return
        inputs, outputs = changes
    reserved = {step(p.anchor, OFFSETS[p.direction]) for p in placements if p.building == "outlet"}
    cells = [point for p in placements for point in p.cells()]
    junctions = {point: kind for point, kind in layout.transport.items() if kind in ("split", "merge")}
    occupied = set(cells)
    if len(occupied) != len(cells) or occupied & junctions.keys() or reserved & (occupied | junctions.keys()):
        return
    if any(not (0 <= x < layout.width and 0 <= y < layout.height) for x, y in occupied):
        return
    mandatory = [i for i, route in enumerate(paths)
                 if route.start in outputs or route.end in inputs or (occupied | reserved).intersection(route.cells)]
    remapped = {i: replace(paths[i], start=start, incoming=out, end=end, outgoing=into)
                for i in mandatory
                for start, out in (outputs.get(paths[i].start, (paths[i].start, paths[i].incoming)),)
                for end, into in (inputs.get(paths[i].end, (paths[i].end, paths[i].outgoing)),)}
    mandatory.sort(key=lambda i: (distance(remapped[i].start, remapped[i].end), len(paths[i].edges)), reverse=True)
    optional = sorted((i for i, route in enumerate(paths) if i not in remapped and len(route.edges) > 2),
                      key=lambda i: len(paths[i].edges), reverse=True)[:max_routes]

    for objective in objectives or (problem.objective,):
        prices = route_costs(problem, objective)
        reserved_cells = occupied | reserved
        route_types = {i: {p: layout.transport[p] for p in route.cells} for i, route in enumerate(paths) if i not in remapped}
        edges = {i: list(route.edges) for i, route in enumerate(paths) if i not in remapped}
        transport = {**junctions, **{point: kind for types in route_types.values() for point, kind in types.items()}}
        changed = False
        for i in (*mandatory, *optional):
            route = remapped.get(i, paths[i])
            old_types = route_types.get(i, {})
            ceiling = None
            if i not in remapped:
                ceiling = tuple(map(sum, zip(*(prices[kind] for kind in old_types.values()))))
                for point in old_types:
                    del transport[point]
            jumps = [jump for j, segments in edges.items() if j != i for jump in tunnel_segments(segments, transport)]
            result = search_route(problem, layout, route, reserved_cells | transport.keys(), jumps, prices, ceiling)
            if result is None:
                if i in remapped:
                    break
                transport.update(old_types)
            else:
                edges[i], types = result
                transport.update(types)
                changed = True
        else:
            if changed:
                machine_nodes = {p.node for _, _, p in layout.machines}
                machines = [(p.item, p.rate, p) for p in placements if p.node in machine_nodes]
                yield replace(layout, placements=placements, machines=machines, transport=transport,
                              segments=[segment for i in range(len(paths)) for segment in edges[i]],
                              statistics={}, profile=f"{layout.profile}／线路重布", row_optimal=False, baseline=False)
