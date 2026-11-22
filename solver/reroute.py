"""在固定设备和分合流之间重新搜索单物料物流路径。"""

from collections import defaultdict
from dataclasses import dataclass, replace
from heapq import heappop, heappush
from itertools import count

from .blueprint import tunnel_directions
from .collision import belt_obstacles, collision_error
from .geometry import JUNCTIONS, OFFSETS, OPPOSITE, assigned_ports, direct_logistics_allowed, offset, placement_ports
from .model import belt_options, belt_seconds


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
    return [(edge.start, edge.end, transport.get(edge.start)) for edge in edges if distance(edge.start, edge.end) > 1]


def tunnel_pairing_valid(pairs, spans):
    """检查安装版以方向找最近端点的规则是否得到全部预期配对。"""
    endpoints = {}
    for start, end, kind in pairs:
        length = distance(start, end)
        limit = spans.get(kind, 0)
        if not 1 <= length <= limit or (start[0] != end[0] and start[1] != end[1]):
            return False
        if start in endpoints or end in endpoints:
            return False
        entrance, exit = tunnel_directions(start, end)
        endpoints[start] = entrance % 4, limit, end
        endpoints[end] = exit % 4, limit, start
    for point, (raw, limit, expected) in endpoints.items():
        facing = OPPOSITE[raw]
        for length in range(1, limit + 1):
            match = offset(point, facing, length)
            if match in endpoints and endpoints[match][0] == facing:
                if match != expected:
                    return False
                break
        else:
            return False
    return True


def compatible_tunnel(start, end, kind, existing, spans):
    other = 1 if start[1] == end[1] else 0
    relevant = [(start, end, kind)]
    for a, b, old_kind in existing:
        if start in (a, b) or end in (a, b):
            return False
        if a[other] == b[other] == start[other]:
            relevant.append((a, b, old_kind))
    return tunnel_pairing_valid(relevant, spans)


def select_grade(options, kind, key, keep_suitable):
    if keep_suitable and any(option[key] == kind for option in options):
        return kind
    return options[0][key] if options else None


def regrade_transport(problem, layout, *, keep_suitable):
    transport = layout.transport.copy()
    # 参考布局可能含有当前名单之外的带，仍需按原来的地面和地下类别分别适配。
    known = [*problem.belts, *problem.catalog.get("belts", ())]
    surface = {belt["building"] for belt in known}
    underground = {belt["underground"] for belt in known if belt.get("underground")}
    loads = defaultdict(int)
    for edge in layout.segments:
        loads[edge.end] += edge.rate
    old_tunnels = tunnel_segments(layout.segments, transport)
    endpoints = {point for start, end, _ in old_tunnels for point in (start, end)}

    for point, kind in transport.items():
        if kind in surface or kind.startswith("belt"):
            replacement = select_grade(belt_options(problem, loads[point]), kind, "building", keep_suitable)
            if replacement is None:
                return None
            transport[point] = replacement
        if (kind in underground or kind.startswith("tunnel")) and point not in endpoints:
            return None

    updated_tunnels = []
    for start, end, kind in old_tunnels:
        if kind is None or transport.get(end) != kind or (kind not in underground and not kind.startswith("tunnel")):
            return None
        options = belt_options(problem, loads[end], span=distance(start, end))
        replacement = select_grade(options, kind, "underground", keep_suitable)
        if replacement is None:
            return None
        transport[start] = transport[end] = replacement
        updated_tunnels.append((start, end, replacement))
    return transport if tunnel_pairing_valid(updated_tunnels, problem.underground_spans) else None


def fit_belt_grades(problem, layout):
    """给结构参考中不可用的带换上最低足用的等级，无法适配时返回 None。"""
    transport = regrade_transport(problem, layout, keep_suitable=True)
    if transport is None:
        return None
    if transport != layout.transport:
        return replace(layout, transport=transport, statistics={}, profile=f"{layout.profile}／带级适配", baseline=False)
    return layout


def retier_layout(problem, layout):
    """在不改变设备和路线的情况下，把足够的低级带用于各物流格及地下对。"""
    transport = regrade_transport(problem, layout, keep_suitable=False)
    if transport is not None and transport != layout.transport:
        return replace(layout, transport=transport, statistics={}, profile=f"{layout.profile}／低级带替换", baseline=False)
    return None


def route_costs(problem, objective):
    prices = {}
    for belt in problem.belts:
        for name in (belt["building"], belt.get("underground")):
            if name:
                building = problem.buildings[name]
                materials = sum(building.get("materials", {}).values())
                metrics = {"buildings": 1, "lines": 1, "materials": materials, "power": building.get("power", 0)}
                prices[name] = tuple(metrics[key] for key in objective if key in metrics) + (1, materials, belt_seconds(belt))
    return prices


def add_cost(left, right):
    return tuple(a + b for a, b in zip(left, right))


def travel_cost(price, *, tunnel_exit):
    return (*price[:-1], price[-1] / 2) if tunnel_exit else price


def search_route(problem, layout, connection, blocked, existing, prices, ceiling=None, *, terminal_kinds=None):
    source, sink = connection.start, connection.end
    first = step(source, connection.incoming)
    last = step(sink, connection.outgoing, -1)
    if first == sink and connection.incoming == connection.outgoing:
        if terminal_kinds is None:
            allowed_direct = not all(layout.transport.get(point) in JUNCTIONS for point in (source, sink))
        else:
            allowed_direct = direct_logistics_allowed(terminal_kinds[source], terminal_kinds[sink])
        if not allowed_direct:
            return None
        return [replace(connection.edges[0], start=source, end=sink)], {}

    load = connection.edges[0].rate
    options = belt_options(problem, load)
    if not options:
        return None
    belt = options[0]["building"]
    max_span = min(max(layout.width, layout.height) - 1, max(problem.underground_spans.values(), default=1))
    tunnels = {span: levels[0]["underground"] for span in range(2, max_span + 1)
               if (levels := belt_options(problem, load, span=span))}

    def allowed(point):
        x, y = point
        return 0 <= x < layout.width and 0 <= y < layout.height and point not in blocked

    if not allowed(first) or not allowed(last):
        return None
    unit = min([prices[belt][0], *(prices[kind][0] / span for span, kind in tunnels.items())])
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
            total = add_cost(score, travel_cost(prices[landing or belt], tunnel_exit=bool(landing)))
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
            all_jumps = (*existing, *previous_tunnels)
            for length, key in tunnels.items():
                destination = step(point, entered, length)
                if allowed(destination) and compatible_tunnel(point, destination, key, all_jumps, problem.underground_spans):
                    moves.append((destination, entered, key, key))
        for destination, direction, arrives_by_tunnel, kind in moves:
            if destination in history or not allowed(destination):
                continue
            cost = add_cost(score, travel_cost(prices[kind], tunnel_exit=bool(landing)))
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
    incoming = defaultdict(list)
    for segment in layout.segments:
        incoming[segment.end].append(segment)
    for old in layout.placements:
        new = updated.get(old.node)
        if new is None or (new.building, new.item, new.rate) != (old.building, old.item, old.rate):
            return None
        if new == old:
            continue
        old_inputs, old_output = assigned_ports(problem, old, incoming)
        new_inputs, new_output = placement_ports(problem, new)
        direction = OFFSETS[new.direction]
        inputs.update((a, (b, direction)) for a, b in zip(old_inputs, new_inputs))
        if old_output is not None:
            outputs[old_output] = new_output, direction
    return inputs, outputs


def endpoint_leads(route):
    first = step(route.start, route.incoming)
    last = step(route.end, route.outgoing, -1)
    # 设备或分合流直接相连时，中间没有需要保留的带格。
    if first == route.end and last == route.start:
        return set()
    return {first, last}


def remap_endpoint(point, direction, ports, junction_moves):
    if point not in junction_moves:
        return ports.get(point, (point, direction))
    new_point, turns = junction_moves[point]
    dx, dy = direction
    for _ in range(turns % 4):
        dx, dy = -dy, dx
    return new_point, (dx, dy)


def reroute_order(paths, mandatory, reroute_all):
    ordered = sorted(mandatory, key=lambda i: (distance(paths[i].start, paths[i].end), len(paths[i].edges)), reverse=True)
    yield ordered
    if reroute_all and len(ordered) > 1:
        yield ordered[::-1]
        if ordered != list(mandatory) and ordered[::-1] != list(mandatory):
            yield mandatory


def reroute_paths(problem, layout, paths, remapped, order, optional, junctions, reserved, leads, prices, terminal_kinds):
    route_types = {i: {p: layout.transport[p] for p in route.cells} for i, route in enumerate(paths) if i not in remapped}
    edges = {i: list(route.edges) for i, route in enumerate(paths) if i not in remapped}
    transport = {**junctions, **{point: kind for types in route_types.values() for point, kind in types.items()}}
    for i in (*order, *optional):
        route = remapped.get(i, paths[i])
        old_types = route_types.get(i, {})
        ceiling = None
        if i not in remapped:
            costs = [travel_cost(prices[old_types[edge.end]], tunnel_exit=distance(edge.start, edge.end) > 1)
                     for edge in route.edges[:-1]]
            ceiling = tuple(map(sum, zip(*costs)))
            for point in old_types:
                del transport[point]
        jumps = [jump for j, segments in edges.items() if j != i for jump in tunnel_segments(segments, transport)]
        blocked = reserved | transport.keys() | (leads.keys() - endpoint_leads(route))
        result = search_route(problem, layout, route, blocked, jumps, prices, ceiling, terminal_kinds=terminal_kinds)
        if result is None:
            if i in remapped:
                return None
            transport.update(old_types)
        else:
            edges[i], types = result
            transport.update(types)
    segments = [segment for i in range(len(paths)) for segment in edges[i]]
    return transport, segments


def rerouted_layouts(problem, layout, *, placements=None, max_routes=24, objectives=None, port_changes=None,
                     reroute_all=False, junction_moves=None):
    """返回保留工序、分合流及物料流量的重布候选，交由调用者校验并仿真。"""
    paths = connections(layout)
    if not paths:
        return
    if port_changes is not None:
        inputs, outputs = port_changes
        placements = layout.placements if placements is None else placements
    elif placements is None:
        placements = layout.placements
        inputs, outputs = {}, {}
    else:
        changes = placement_changes(problem, layout, placements)
        if changes is None:
            return
        inputs, outputs = changes
    reserved = {step(p.anchor, OFFSETS[p.direction]) for p in placements if p.building == "outlet"}
    cells = [(point, p.building) for p in placements for point in p.cells()]
    devices = dict(cells)
    previous_junctions = {point: kind for point, kind in layout.transport.items() if kind in JUNCTIONS}
    junction_moves = junction_moves or {}
    if junction_moves.keys() - previous_junctions.keys():
        return
    junctions = {junction_moves.get(point, (point, 0))[0]: kind for point, kind in previous_junctions.items()}
    if len(devices) != len(cells) or len(junctions) != len(previous_junctions) or devices.keys() & junctions.keys():
        return
    if collision_error(problem, placements, junctions):
        return
    terminal_kinds = devices | junctions
    if reserved & terminal_kinds.keys() or any(not (0 <= x < layout.width and 0 <= y < layout.height)
                                               for x, y in terminal_kinds):
        return
    reserved_cells = reserved | devices.keys() | belt_obstacles(problem, placements)
    fixed_cells = reserved_cells | junctions.keys()
    mapped, leads = [], {}
    for i, route in enumerate(paths):
        start, out = remap_endpoint(route.start, route.incoming, outputs, junction_moves)
        end, into = remap_endpoint(route.end, route.outgoing, inputs, junction_moves)
        if start not in terminal_kinds or end not in terminal_kinds:
            return
        candidate = replace(route, start=start, incoming=out, end=end, outgoing=into)
        for x, y in endpoint_leads(candidate):
            lead = x, y
            if lead in leads or lead in fixed_cells or not (0 <= x < layout.width and 0 <= y < layout.height):
                return
            leads[lead] = i
        mapped.append(candidate)
    remapped = {i: mapped[i] for i, route in enumerate(paths)
                if reroute_all or mapped[i] != route or route.start in outputs or route.end in inputs or
                fixed_cells.intersection(route.cells) or
                any(point in leads and leads[point] != i for point in route.cells)}
    optional = sorted((i for i, route in enumerate(paths) if i not in remapped and len(route.edges) > 2),
                      key=lambda i: len(paths[i].edges), reverse=True)[:max_routes]
    changed_placements = placements != layout.placements
    if not remapped and not optional and not changed_placements:
        return
    seen = set()
    for objective in objectives or (problem.objective,):
        prices = route_costs(problem, objective)
        for order in reroute_order(mapped, tuple(remapped), reroute_all):
            result = reroute_paths(problem, layout, paths, remapped, order, optional, junctions, reserved_cells, leads,
                                   prices, terminal_kinds)
            if result is None:
                continue
            transport, segments = result
            if not remapped and not changed_placements and transport == layout.transport and set(segments) == set(layout.segments):
                continue
            signature = (frozenset(transport.items()), frozenset(segments))
            if signature in seen:
                continue
            seen.add(signature)
            machine_nodes = {p.node for _, _, p in layout.machines}
            machines = [(p.item, p.rate, p) for p in placements if p.node in machine_nodes]
            yield replace(layout, placements=placements, machines=machines, transport=transport, segments=segments,
                          statistics={}, profile=f"{layout.profile}／线路重布", row_optimal=False, baseline=False)
