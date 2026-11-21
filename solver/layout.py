from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from fractions import Fraction
from functools import lru_cache
from math import ceil
from random import Random

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .blueprint import EAST
from .compact import seam_compactions
from .model import Infeasible, make_network
from .orient import placement_trials
from .power import refine_power_grid
from .reference import reference_candidates
from .reroute import rerouted_layouts


Point = tuple[int, int]


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point
    item: str
    rate: Fraction
    bus: int


@dataclass(frozen=True)
class Placement:
    building: str
    x: int
    y: int
    width: int
    height: int
    anchor: Point
    item: str = ""
    rate: Fraction = Fraction(0)
    node: int | None = None
    direction: int = EAST

    def cells(self):
        return ((x, y) for x in range(self.x, self.x + self.width) for y in range(self.y, self.y + self.height))


@dataclass
class Layout:
    width: int
    height: int
    placements: list[Placement]
    transport: dict[Point, str]
    segments: list[Segment]
    wires: set[Point]
    power_entry: Point
    machines: list[tuple[str, Fraction, Placement]]
    profile: str = ""
    row_optimal: bool = False
    copies: int = 1
    statistics: dict = field(default_factory=dict)
    baseline: bool = False
    extended: bool = False


def aisle_positions(problem, network, tracks):
    x = 0
    xpos, columns = {}, {}
    nodes_by_rank = defaultdict(list)
    for node in network.nodes:
        nodes_by_rank[node.rank].append(node)
    for rank, nodes in sorted(nodes_by_rank.items()):
        for members, width in tracks.get(rank, []):
            for bus in members:
                columns[bus] = x + width // 2
            x += width
        for node in nodes:
            xpos[node.id] = x
        x += max(problem.geometry(n)["size"][0] for n in nodes)
    return x, xpos, columns


def leg_intervals(problem, network, xpos, columns):
    for bus_id, bus in enumerate(network.buses):
        cx = columns[bus_id]
        for port in bus.sources:
            node = network.nodes[port.node]
            yield bus_id, port, xpos[port.node] + problem.geometry(node)["size"][0], cx
        for port in bus.sinks:
            yield bus_id, port, cx, xpos[port.node] - 1


def conflicts(problem, network, xpos, columns, align_through=False):
    spans = []
    for node in network.nodes:
        width, height = problem.geometry(node)["size"]
        spans.append((node.id, xpos[node.id], xpos[node.id] + width - 1, 0, height - 1, None))
    for bus, port, left, right in leg_intervals(problem, network, xpos, columns):
        spans.append((port.node, left, right, port.row, port.row, bus))
    rules = set()
    for index, (a, al, ar, at, ab, ag) in enumerate(spans):
        for b, bl, br, bt, bb, bg in spans[:index]:
            if a == b or ar < bl or br < al:
                continue
            common = ag is not None and ag == bg
            if common and len(network.buses[ag].sources) + len(network.buses[ag].sinks) == 2:
                continue
            # 同一干线两侧的水平腿可在干线处直通。实际分合流由路由结果核对。
            if common and align_through and (al < ar == bl < br or bl < br == al < ar):
                continue
            gap = 2 if common else 1
            pair = (a, b, ab - bt + gap, bb - at + gap)
            rules.add(pair if a < b else (b, a, pair[3], pair[2]))
    return sorted(rules)


def greedy_rows(heights, rules, orders, neighbors):
    options = []
    related = defaultdict(list)
    for a, b, below, above in rules:
        related[a].append((b, below, above))
        related[b].append((a, above, below))
    for order in orders:
        for align in (False, True):
            y = [-1] * len(heights)
            used = 0
            for a in order:
                forbidden = set()
                for b, below, above in related[a]:
                    if y[b] >= 0:
                        forbidden.update(range(y[b] - below + 1, y[b] + above))
                position = 0
                while position in forbidden:
                    position += 1
                if align:
                    desired = [y[b] + offset for b, offset in neighbors[a] if y[b] >= 0]
                    upper = max(used - heights[a], position)
                    position = min((candidate for candidate in range(position, upper + 1) if candidate not in forbidden),
                                   key=lambda p: (sum(abs(p - goal) for goal in desired), p))
                y[a] = position
                used = max(used, position + heights[a])
            options.append((used, tuple(y)))
    return sorted(set(options))


def optimize_rows(heights, rules, bound, time_limit, buses=()):
    if time_limit <= 0:
        return None, False
    count = len(heights)
    extent_start = count + 1 + len(rules)
    size = extent_start + 2 * len(buses)
    matrix_rows, matrix_cols, coefficients, lower, upper = [], [], [], [], []

    def constraint(terms, minimum=-np.inf, maximum=np.inf):
        for variable, coefficient in terms.items():
            matrix_rows.append(len(lower))
            matrix_cols.append(variable)
            coefficients.append(coefficient)
        lower.append(minimum)
        upper.append(maximum)

    for i, height in enumerate(heights):
        constraint({i: 1, count: -1}, maximum=-height)
    big_m = bound + max(heights) + 2
    for i, (a, b, below, above) in enumerate(rules, count + 1):
        constraint({b: 1, a: -1, i: big_m}, minimum=below)
        constraint({a: 1, b: -1, i: -big_m}, minimum=above - big_m)
    objective = np.zeros(size)
    objective[count] = 1
    for index, bus in enumerate(buses):
        top = extent_start + 2 * index
        bottom = top + 1
        for port in bus.sources + bus.sinks:
            constraint({top: 1, port.node: -1}, maximum=port.row)
            constraint({bottom: 1, port.node: -1}, minimum=port.row)
        # 给定可行高度上限，先缩短总干线跨度，再缩小外接高度。
        objective[top] = -(bound + 1)
        objective[bottom] = bound + 1
    matrix = coo_matrix((coefficients, (matrix_rows, matrix_cols)), shape=(len(lower), size)).tocsc()
    integrality = np.ones(size)
    integrality[extent_start:] = 0
    bounds = [bound] * (count + 1) + [1] * len(rules) + [bound] * (2 * len(buses))
    result = milp(objective, integrality=integrality, bounds=Bounds(0, bounds),
                  constraints=LinearConstraint(matrix, lower, upper),
                  options={"time_limit": time_limit, "mip_rel_gap": 0})
    if result.x is None:
        return None, False
    return tuple(round(value) for value in result.x[:count]), result.status == 0 and not buses


def bus_row_span(buses, y):
    total = 0
    for bus in buses:
        rows = [y[port.node] + port.row for port in bus.sources + bus.sinks]
        total += max(rows) - min(rows)
    return total


def combine_tracks(problem, network, tracks, y):
    extents = {}
    for bus_id, bus in enumerate(network.buses):
        rows = [y[p.node] + p.row for p in bus.sources + bus.sinks]
        extents[bus_id] = min(rows), max(rows)
    compact = {}
    for rank, entries in tracks.items():
        lanes = []
        for bus_id in sorted((members[0] for members, _ in entries), key=extents.get):
            top, bottom = extents[bus_id]
            for index, (last, members) in enumerate(lanes):
                if last < top:
                    lanes[index] = (bottom, members + [bus_id])
                    break
            else:
                lanes.append((bottom, [bus_id]))
        compact[rank] = [(members, 3) for _, members in lanes]
    _, xpos, columns = aisle_positions(problem, network, compact)
    legs = list(leg_intervals(problem, network, xpos, columns))
    crossed = set()
    for bus_id, (top, bottom) in extents.items():
        if any(other != bus_id and left < columns[bus_id] < right and top < y[port.node] + port.row < bottom
               for other, port, left, right in legs):
            crossed.add(bus_id)
    for rank, entries in compact.items():
        compact[rank] = [(members, 3 if any(i in crossed or len(network.buses[i].sources + network.buses[i].sinks) > 2
                                             for i in members) else 1) for members, _ in entries]
    return compact


def distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def unit_path(start, end, vertical_first=False):
    point = start
    yield point
    for axis in ((1, 0) if vertical_first else (0, 1)):
        while point[axis] != end[axis]:
            step = 1 if point[axis] < end[axis] else -1
            point = (point[0] + step, point[1]) if axis == 0 else (point[0], point[1] + step)
            yield point


def power_grid(anchors, width, height):
    if not anchors:
        return set(), (0, 0)
    solutions = []
    extremes = {min(anchors), max(anchors), min(anchors, key=lambda p: p[::-1]), max(anchors, key=lambda p: p[::-1])}
    for start_point in sorted(extremes):
        wire = {start_point}
        needs = set(anchors) - wire
        nearest = {p: (distance(p, start_point), start_point) for p in needs}
        while needs:
            start = min(needs, key=lambda p: (nearest[p], p))
            end = nearest[start][1]
            paths = [set(unit_path(start, end, first)) for first in (False, True)]
            added = min((path - wire for path in paths), key=len)
            wire.update(added)
            needs.difference_update(wire)
            for p in needs:
                nearest[p] = min(nearest[p], min((distance(p, cell), cell) for cell in added))
        root = connect_boundary(wire, width, height)
        solutions.append((len(wire), root, wire))
    _, root, wire = min(solutions)
    return wire, root


def connect_boundary(wire, width, height):
    _, start, root = min((distance((x, y), end), (x, y), end) for x, y in wire
                         for end in ((0, y), (width - 1, y), (x, 0), (x, height - 1)))
    wire.update(unit_path(start, root))
    return root


def route(problem, network, tracks, y, profile, optimal):
    width, xpos, columns = aisle_positions(problem, network, tracks)
    placements, machines = [], []
    for node in network.nodes:
        geometry = problem.geometry(node)
        x, row = xpos[node.id], y[node.id]
        ax, ay = geometry["anchor"]
        key = problem.recipes[node.item]["machine"] if node.kind == "machine" else node.kind
        placed = Placement(key, x, row, *geometry["size"], (x + ax, row + ay), node.item, node.rate, node.id)
        placements.append(placed)
        if node.kind == "machine":
            machines.append((node.item, node.rate, placed))
    height = max(p.y + p.height for p in placements)
    occupied = {cell: p for p in placements for cell in p.cells()}
    if len(occupied) != sum(p.width * p.height for p in placements):
        raise Infeasible("建筑占位冲突")

    edges = {}
    vertical = {}

    def write(a, b, bus, rate):
        key = (a, b)
        if key in edges or (b, a) in edges:
            raise Infeasible("物流线重叠")
        edges[key] = Segment(a, b, network.buses[bus].item, rate, bus)

    for i, bus in enumerate(network.buses):
        cx = columns[i]
        deltas = defaultdict(Fraction)
        for port in bus.sources:
            p, row = placements[port.node], y[port.node] + port.row
            deltas[row] += port.rate
            if p.x + p.width - 1 >= cx:
                raise Infeasible("建筑出料口与干线间无空格")
        for port in bus.sinks:
            p, row = placements[port.node], y[port.node] + port.row
            deltas[row] -= port.rate
            if p.x <= cx:
                raise Infeasible("建筑入料口与干线间无空格")
        flow = Fraction(0)
        for row in range(min(deltas), max(deltas) + 1):
            flow += deltas[row]
            if flow:
                a, b = (cx, row), (cx, row + 1)
                if flow < 0:
                    a, b = b, a
                write(a, b, i, abs(flow))
                if any(point in vertical and vertical[point] != i for point in (a, b)):
                    raise Infeasible("竖向干线重叠")
                vertical[a] = vertical[b] = i
        if flow:
            raise Infeasible("干线流量未配平")

    sources = [set(bus.sources) for bus in network.buses]
    for i, port, left, right in leg_intervals(problem, network, xpos, columns):
        row = y[port.node] + port.row
        if port in sources[i]:
            left -= 1
        else:
            right += 1
        x = left
        while x < right:
            crossing = vertical.get((x + 1, row))
            step = 2 if crossing is not None and crossing != i else 1
            if x + step > right or (step == 2 and (not problem.case.get("underground", True)
                                                    or (x, row) in occupied or (x + 2, row) in occupied)):
                raise Infeasible("物流交叉处没有可用的地下带端点")
            write((x, row), (x + step, row), i, port.rate)
            x += step

    incoming, outgoing = defaultdict(list), defaultdict(list)
    for segment in edges.values():
        outgoing[segment.start].append(segment)
        incoming[segment.end].append(segment)
    transport = {}
    for point in sorted(incoming.keys() | outgoing.keys()):
        if point in occupied:
            continue
        ins, outs = incoming[point], outgoing[point]
        if min(len(ins), len(outs)) != 1 or max(len(ins), len(outs)) > problem.junction_ports:
            raise Infeasible("传送带断开或存在非法交叉")
        if len({s.bus for s in ins + outs}) != 1:
            raise Infeasible("不同干线共用一个格位")
        load = sum(s.rate for s in ins)
        if load != sum(s.rate for s in outs):
            raise Infeasible("物流件没有配平")
        spans = [s for s in ins + outs if distance(s.start, s.end) > 1]
        if spans:
            level = next((b for b in problem.belts if b.get("underground") and b["capacity"] >= load), None)
            if len(spans) != 1 or len(ins + outs) != 2 or level is None:
                raise Infeasible("地下带容量不足或端点冲突")
            transport[point] = level["underground"]
        elif len(ins) > 1 or len(outs) > 1:
            if load > problem.junction_capacity:
                raise Infeasible("分合流容量不足")
            transport[point] = "merge" if len(ins) > 1 else "split"
        else:
            level = next((b for b in problem.belts if b["capacity"] >= load), None)
            if level is None:
                raise Infeasible("传送带容量不足")
            transport[point] = level["building"]
    return Layout(width, height, placements, transport, list(edges.values()), set(), (0, 0), machines, profile, optimal)


def compact_routes(layout, axes):
    for axis in axes:
        line = 0
        while line < (layout.width, layout.height)[axis]:
            occupied = {cell[axis] for p in layout.placements for cell in p.cells()}
            if line in occupied:
                line += 1
                continue
            on_line = {cell for cell in layout.transport if cell[axis] == line}
            incoming = {s.end: s for s in layout.segments if s.end in on_line}
            outgoing = {s.start: s for s in layout.segments if s.start in on_line}
            tunnels = [s for s in layout.segments if distance(s.start, s.end) > 1]

            def removable(point):
                a, b = incoming[point], outgoing[point]
                return (layout.transport[point].startswith("belt") and a.bus == b.bus and a.rate == b.rate and
                        a.start[1-axis] == point[1-axis] == b.end[1-axis] and abs(a.start[axis] - b.end[axis]) == 2 and
                        any(layout.transport.get(cell, "").startswith("belt") for cell in (a.start, b.end)))

            if any(min(s.start[axis], s.end[axis]) <= line <= max(s.start[axis], s.end[axis]) for s in tunnels) or not all(map(removable, on_line)):
                line += 1
                continue

            def shift(cell):
                return (cell[0] - (axis == 0 and cell[0] > line), cell[1] - (axis == 1 and cell[1] > line))

            edges = [s for s in layout.segments if s.start not in on_line and s.end not in on_line]
            edges.extend(Segment(incoming[p].start, outgoing[p].end, outgoing[p].item, outgoing[p].rate, outgoing[p].bus) for p in sorted(on_line))
            placed = {}
            for p in layout.placements:
                x, y = shift((p.x, p.y))
                placed[p.node] = replace(p, x=x, y=y, anchor=shift(p.anchor))
            layout = Layout(layout.width - (axis == 0), layout.height - (axis == 1), list(placed.values()),
                            {shift(cell): kind for cell, kind in layout.transport.items() if cell not in on_line},
                            [Segment(shift(s.start), shift(s.end), s.item, s.rate, s.bus) for s in edges], set(), (0, 0),
                            [(item, rate, placed[p.node]) for item, rate, p in layout.machines], layout.profile)
    return layout


def assess(problem, layout):
    counts = Counter(p.building for p in layout.placements if p.building != "outlet")
    counts.update(layout.transport.values())
    for p in layout.placements:
        if p.building == "outlet":
            counts[next(b["building"] for b in problem.belts if b["capacity"] >= p.rate)] += 1
    power = sum(count * problem.buildings[key].get("power", 0) for key, count in counts.items())
    materials = Counter()
    for key, count in (counts | Counter({"wire": len(layout.wires)})).items():
        for material, amount in problem.buildings[key].get("materials", {}).items():
            materials[material] += amount * count
    layout.statistics = {"buildings": dict(sorted(counts.items())), "materials": dict(sorted(materials.items())),
                         "components": sum(counts.values()), "transport": len(layout.transport) + sum(p.building == "outlet" for p in layout.placements), "power": power,
                         "wires": len(layout.wires), "machines": len(layout.machines)}
    return score_layout(problem, layout)


def score_layout(problem, layout):
    s = layout.statistics
    values = {"area": layout.width * layout.height, "buildings": s["components"], "lines": s["transport"] + s["wires"],
              "materials": sum(s["materials"].values()), "power": s["power"], "inlets": s["buildings"].get("inlet", 0),
              "machines": s["machines"]}
    return tuple(values[key] for key in problem.objective)


def placement_networks(network):
    yield network, ""
    # 将浅支线移到最早的下游工序前，保持每条物流从较低层流向较高层。
    downstream = defaultdict(set)
    for bus in network.buses:
        for source in bus.sources:
            downstream[source.node].update(port.node for port in bus.sinks)
    ordered = sorted(network.nodes, key=lambda node: node.rank, reverse=True)
    previous = {tuple(node.rank for node in network.nodes)}
    for include_machines, label in ((False, "进货靠近工序"), (True, "工序靠近下游")):
        ranks = [node.rank for node in network.nodes]
        for node in ordered:
            if downstream[node.id] and (include_machines or node.kind == "inlet"):
                ranks[node.id] = min(ranks[sink] for sink in downstream[node.id]) - 1
        if tuple(ranks) not in previous:
            previous.add(tuple(ranks))
            nodes = [replace(node, rank=ranks[node.id]) for node in network.nodes]
            yield replace(network, nodes=nodes), label


def track_plans(network, include_early):
    ranks = sorted({node.rank for node in network.nodes})
    for early in (False, True) if include_early else (False,):
        aisles = defaultdict(list)
        shifted = False
        for index, bus in enumerate(network.buses):
            right = min(network.nodes[p.node].rank for p in bus.sinks)
            rank = right
            if early:
                left = max(network.nodes[p.node].rank for p in bus.sources)
                rank = next(rank for rank in ranks if rank > left)
                shifted |= rank < right
            aisles[rank].append(([index], 3))
        if not early or shifted:
            for flip in (False, True) if any(len(entries) > 1 for entries in aisles.values()) else (False,):
                tracks = {rank: entries[::-1] if flip else entries for rank, entries in aisles.items()}
                yield tracks, early, flip


def upstream_order(network):
    upstream = defaultdict(list)
    for bus in network.buses:
        for sink in bus.sinks:
            upstream[sink.node].extend(source.node for source in bus.sources)
    roots = sorted(network.nodes, key=lambda node: (-node.rank, node.id))
    stack = [node.id for node in reversed(roots)]
    ordered, visited = [], set()
    while stack:
        node = stack.pop()
        if node not in visited:
            visited.add(node)
            ordered.append(node)
            stack.extend(reversed(upstream[node]))
    return ordered


def row_placements(problem, network, tracks, time_limit, full, prefer_short, align_through=False):
    _, xpos, columns = aisle_positions(problem, network, tracks)
    heights = [problem.geometry(n)["size"][1] for n in network.nodes]
    rules = conflicts(problem, network, xpos, columns, align_through=align_through)
    ids = list(range(len(heights)))
    bank = sorted(ids, key=lambda i: (network.nodes[i].rank, i))
    degree = Counter(i for a, b, _, _ in rules for i in (a, b))
    flow_order = sorted(ids, key=lambda i: (degree[i], heights[i]), reverse=True)
    random = Random(1427)
    orders = [ids, ids[::-1], bank, bank[::-1], flow_order] + [random.sample(ids, len(ids)) for _ in range(32)]
    neighbors = defaultdict(list)
    for bus in network.buses:
        for a in bus.sources:
            for b in bus.sinks:
                neighbors[a.node].append((b.node, b.row - a.row))
                neighbors[b.node].append((a.node, a.row - b.row))
    options = greedy_rows(heights, rules, orders, neighbors)
    best_height = options[0][0]
    rows, optimal = optimize_rows(heights, rules, best_height, time_limit if full else 0)
    candidates = {y: (optimal and height == best_height and rows == y, full) for height, y in options[:12 if full else 6]}
    if rows is not None and rows not in candidates:
        candidates = {rows: (optimal, full), **candidates}
    nearby = upstream_order(network)
    local_options = greedy_rows(heights, rules, (nearby, nearby[::-1]), neighbors)
    for _, y in local_options:
        candidates.setdefault(y, (False, False))
    max_height = best_height + max(2, best_height // 4)
    short = sorted((option for option in options if option[0] <= max_height),
                   key=lambda option: (bus_row_span(network.buses, option[1]), option))
    for _, y in short[:5]:
        candidates.setdefault(y, (False, False))
    if prefer_short:
        rows, _ = optimize_rows(heights, rules, max_height, time_limit / 2, network.buses)
        if rows is not None:
            candidates.setdefault(rows, (False, False))
    return candidates.items()


def routed_layouts(problem, network, tracks, candidates, profile):
    for y, (proven, original_rows) in candidates:
        compact = combine_tracks(problem, network, tracks, y)
        for used_tracks in (compact, tracks) if compact != tracks else (tracks,):
            try:
                layout = route(problem, network, used_tracks, y, profile, proven)
            except Infeasible:
                continue
            for axes in ((0, 1), (1, 0)):
                yield compact_routes(layout, axes), original_rows


def layout_candidates(problem, count, time_limit):
    tried, layouts_seen = set(), {}
    wiring = lru_cache(maxsize=256)(power_grid)
    limits = sorted({problem.capacity, *(min(problem.capacity, Fraction(rate)) for rate in (60, 36, 24))}, reverse=True)
    profiles = [(limit, strategy, reverse) for limit in limits
                for strategy, reverse in (("balanced", False), ("balanced", True), ("local", False))]
    profiles.extend((problem.capacity, strategy, False) for strategy in ("direct", "distributed"))
    for limit, strategy, reverse in profiles:
        try:
            network = make_network(problem, count, strategy, limit, reverse)
        except Infeasible:
            continue
        signature = tuple(network.buses)
        if signature in tried:
            continue
        tried.add(signature)
        for placements, stage in placement_networks(network):
            for tracks, early, flip in track_plans(placements, include_early=not stage and limit == problem.capacity):
                original = not stage and not early
                prefer_short = not flip and not early and limit == problem.capacity and not reverse
                candidates = dict(row_placements(problem, placements, tracks, time_limit, original, prefer_short))
                if prefer_short and problem.junction_ports > 2 and stage != "进货靠近工序":
                    aligned = row_placements(problem, placements, tracks, time_limit, False, False, align_through=True)
                    for rows, case in aligned:
                        candidates.setdefault(rows, case)
                labels = (str(limit), strategy, stage, "干线靠近供料" if early else "")
                profile = "／".join(filter(None, labels))
                for layout, original_rows in routed_layouts(problem, placements, tracks, candidates.items(), profile):
                    layout.baseline = original_rows and strategy in ("balanced", "local")
                    occupied = frozenset((p.building, p.x, p.y, p.item, p.rate) for p in layout.placements)
                    routes = frozenset((s.start, s.end, s.rate, s.item) for s in layout.segments)
                    signature = (layout.width, layout.height, occupied, frozenset(layout.transport.items()), routes)
                    if signature in layouts_seen:
                        if layout.baseline:
                            layouts_seen[signature].baseline = True
                        continue
                    layouts_seen[signature] = layout
                    anchors = tuple(sorted(p.anchor for _, _, p in layout.machines if problem.buildings[p.building].get("power", 0)))
                    wires, layout.power_entry = wiring(anchors, layout.width, layout.height)
                    layout.wires = set(wires)
                    yield layout


def repeat_layout(layout, copies, columns):
    placements, segments, transport, machines, wire, roots = [], [], {}, [], set(), []
    bus_count = 1 + max(s.bus for s in layout.segments)
    for index in range(copies):
        dx, dy = (index % columns) * (layout.width + 1), (index // columns) * layout.height

        def shift(p):
            return p[0] + dx, p[1] + dy

        placed = {}
        for p in layout.placements:
            node = index * len(layout.placements) + p.node
            clone = replace(p, x=p.x + dx, y=p.y + dy, anchor=shift(p.anchor), node=node)
            placed[p.node] = clone
            placements.append(clone)
        machines.extend((item, rate, placed[p.node]) for item, rate, p in layout.machines)
        segments.extend(Segment(shift(s.start), shift(s.end), s.item, s.rate, s.bus + index * bus_count)
                        for s in layout.segments)
        transport.update({shift(p): key for p, key in layout.transport.items()})
        wire.update(shift(p) for p in layout.wires)
        roots.append(shift(layout.power_entry))
    width = min(copies, columns) * (layout.width + 1) - 1
    height = ceil(copies / columns) * layout.height
    if wire:
        for a, b in zip(roots, roots[1:]):
            wire.update(unit_path(a, b))
    root = connect_boundary(wire, width, height) if wire else (0, 0)
    return Layout(width, height, placements, transport, segments, wire, root, machines,
                  layout.profile, layout.row_optimal, copies, baseline=layout.baseline)


def simulation_indices(ranked, limit):
    if len(ranked) <= limit:
        return range(len(ranked))

    def signature(layout):
        machines = Counter(item for item, _, _ in layout.machines)
        inlets = sorted((p.item, p.rate) for p in layout.placements if p.building == "inlet")
        return tuple(sorted(machines.items())), tuple(inlets)

    primary = (limit + 1) // 2
    indices = set(range(primary))
    seen = {signature(ranked[i][1]) for i in indices}
    # 先保留最优布局，再给不同设备配平与进货分组留出仿真名额。
    for index in range(primary, len(ranked)):
        if len(indices) == limit:
            break
        key = signature(ranked[index][1])
        if key not in seen:
            seen.add(key)
            indices.add(index)
    for index in range(primary, len(ranked)):
        if len(indices) == limit:
            break
        indices.add(index)
    # 扩大搜索族后仍给原有排布同等机会，避免新候选挤掉原来能跑满的布局。
    original = [i for i, (_, layout) in enumerate(ranked) if layout.baseline]
    return sorted(indices.union(original[:limit]))


def solve(problem, time_limit, simulation=None, accept=None):
    width, height = problem.plot_limits
    power = problem.power_limit

    def excess(layout):
        return max(0, layout.width - (width or layout.width)) + max(0, layout.height - (height or layout.height))

    def check(layout, allow_outside=False):
        metrics = assess(problem, layout)
        if power is not None and layout.statistics["power"] > power:
            return None
        try:
            verify(problem, layout)
        except Infeasible:
            return None
        if not allow_outside and (excess(layout) or (accept is not None and not accept(layout))):
            return None
        return metrics

    def rewire(layout):
        anchors = [p.anchor for _, _, p in layout.machines if problem.buildings[p.building].get("power", 0)]
        old = (layout.wires, layout.power_entry) if layout.wires else (set(), (0, 0))
        new = power_grid(anchors, layout.width, layout.height)
        choice = old if old[0] and len(old[0]) < len(new[0]) else new
        layout.wires, layout.power_entry = refine_power_grid(anchors, layout.width, layout.height, choice)

    def reductions(layout):
        for reduced in seam_compactions(layout):
            if excess(reduced):
                continue
            rewire(reduced)
            metrics = check(reduced)
            if metrics is not None:
                reduced.profile += "／局部压紧"
                yield metrics, reduced

    def finish(layout):
        result = layout.statistics.get("simulation")
        return result is None or result["last_four_per_minute"] >= float(problem.rate) - 1e-8

    def geometry_trials(seed, limit):
        for moved in rerouted_layouts(problem, seed, max_routes=limit):
            yield replace(moved, extended=True)
        for placements, label in placement_trials(problem, seed):
            for moved in rerouted_layouts(problem, seed, placements=placements, max_routes=0):
                yield replace(moved, profile=f"{seed.profile}／{label}", wires=set(), extended=True)

    def find(count, repeat, mode):
        pending, outside = [], []
        for module in layout_candidates(problem, count, time_limit):
            options = (repeat_layout(module, repeat, cols) for cols in range(1, repeat + 1)) if repeat > 1 else (module,)
            for layout in options:
                over = excess(layout)
                metrics = check(layout, allow_outside=bool(over))
                if metrics is None:
                    continue
                if over:
                    outside.append(((over, metrics), layout))
                else:
                    pending.append((metrics, layout, module))
        ranked = []
        for metrics, layout, module in pending:
            layout.baseline = module.baseline
            ranked.append((metrics, layout))
        # 显式净空边界下，允许最接近上限的候选先做局部压缩。
        outside.sort(key=lambda option: option[0])
        for _, layout in outside[:min(12, problem.simulation_candidates)]:
            ranked.extend(reductions(layout))
        references = []
        if mode == "shared":
            for layout in reference_candidates(problem):
                if check(layout, allow_outside=True) is None:
                    continue
                rewire(layout)
                metrics = check(layout)
                if metrics is not None:
                    ranked.append((metrics, layout))
                ranked.extend(reductions(layout))
                references.append(layout)
        ranked.sort(key=lambda option: option[0])
        if not ranked:
            return None, 0
        winner = ranked[0][1]
        if simulation:
            for index in simulation_indices(ranked, problem.simulation_candidates):
                _, layout = ranked[index]
                layout.statistics["simulation"] = simulation(problem, layout)
                if finish(layout):
                    winner = layout
                    break
        best = (not finish(winner), score_layout(problem, winner))
        validated_count = len(ranked)

        def consider(candidates):
            nonlocal winner, best, validated_count
            for metrics, candidate in candidates:
                validated_count += 1
                if not best[0] and metrics >= best[1]:
                    continue
                if simulation:
                    candidate.statistics["simulation"] = simulation(problem, candidate)
                rank = (not finish(candidate), metrics)
                if rank < best:
                    winner, best = candidate, rank

        # 先压缩当前入选者，再尝试新朝向、换边进货和实际的多向布线。
        consider(reductions(winner))
        if problem.case.get("orientation_search", True):
            seeds = [winner]
            independent_start = next((layout for _, layout in ranked if not layout.extended), None)
            if winner.extended and independent_start is not None:
                seeds.append(independent_start)

            for seed in seeds:
                for candidate in geometry_trials(seed, 24 if seed.extended else 12):
                    rewire(candidate)
                    metrics = check(candidate)
                    if metrics is not None:
                        consider(((metrics, candidate),))
                        consider(reductions(candidate))
        return winner, validated_count

    modes = problem.case.get("modes", ("shared", "separate"))
    shapes = {"shared": (problem.count, 1), "separate": (1, problem.count)}
    return {mode: find(*shapes[mode], mode) if mode in modes else (None, 0) for mode in shapes}


def verify(problem, layout):
    from .geometry import OFFSETS, OPPOSITE, offset, oriented_geometry, placement_ports

    incoming, outgoing = defaultdict(list), defaultdict(list)
    belt_capacity = {b["building"]: b["capacity"] for b in problem.belts}
    belt_capacity.update((b["underground"], b["capacity"]) for b in problem.belts
                         if b.get("underground") in problem.underground_spans)
    occupied = set(layout.transport)
    tunnel_ranges = defaultdict(list)

    def require(condition, message):
        if not condition:
            raise Infeasible(message)

    for p in layout.placements:
        cells = set(p.cells())
        require(not cells & occupied, f"{p.item}与其他组件占位冲突")
        occupied.update(cells)
    require(all(0 <= x < layout.width and 0 <= y < layout.height for x, y in occupied | layout.wires), "组件超出外接矩形")
    for s in layout.segments:
        require(s.start in occupied and s.end in occupied, "有物流线连接空格")
        jump = distance(s.start, s.end)
        require(s.rate > 0 and jump >= 1, "跨格距离或流量非法")
        if jump > 1:
            start, end = s.start, s.end
            kind = layout.transport.get(start)
            require(kind == layout.transport.get(end) and jump <= problem.underground_spans.get(kind, 0) and
                    (start[0] == end[0] or start[1] == end[1]), "地下带配对或跨度错误")
            axis = int(start[0] == end[0])
            lo, hi = sorted((start[axis], end[axis]))
            tunnel_ranges[kind, axis, start[1 - axis]].append((lo, hi))
        if layout.transport.get(s.start) in ("split", "merge") and layout.transport.get(s.end) in ("split", "merge"):
            raise Infeasible("分流器与合并器必须用带段隔开")
        outgoing[s.start].append(s)
        incoming[s.end].append(s)
    for spans in tunnel_ranges.values():
        ordered = sorted(spans)
        require(all(a[1] < b[0] for a, b in zip(ordered, ordered[1:])), "同级地下带在同一轴线上跨越重叠")

    def heading(a, b):
        return next((d for d, (dx, dy) in OFFSETS.items()
                     if (b[0] - a[0], b[1] - a[1]) == (dx * distance(a, b), dy * distance(a, b))), None)

    accepts, sends = {}, {}
    for point, kind in layout.transport.items():
        ins, outs = incoming[point], outgoing[point]
        if kind == "split":
            valid_ports = len(ins) == 1 and 2 <= len(outs) <= problem.junction_ports
        elif kind == "merge":
            valid_ports = len(outs) == 1 and 2 <= len(ins) <= problem.junction_ports
        else:
            valid_ports = len(ins) == len(outs) == 1
        require(valid_ports, f"{point}物流端口数错误")
        require(kind in belt_capacity or kind in ("split", "merge"), f"{point}使用了不允许的物流设备")
        load = sum(s.rate for s in ins)
        require(len({s.item for s in ins + outs}) == 1 and load == sum(s.rate for s in outs), f"{point}流量或物料不守恒")
        require(load <= belt_capacity.get(kind, problem.junction_capacity), f"{point}超出物流容量")
        if kind.startswith("tunnel") or layout.extended:
            in_heading = heading(ins[0].start, point)
            out_heading = heading(point, outs[0].end)
            require(in_heading is not None and out_heading is not None, "物流方向错误")
            if kind.startswith("tunnel"):
                underground_in = distance(ins[0].start, point) > 1
                underground_out = distance(point, outs[0].end) > 1
                require(in_heading == out_heading and underground_in != underground_out, "地下带端点方向错误")
                if not underground_in:
                    accepts[point] = (in_heading,)
                if not underground_out:
                    sends[point] = (out_heading,)
            elif kind == "split":
                accepts[point] = (in_heading,)
                sends[point] = tuple(d for d in OFFSETS if d != OPPOSITE[in_heading])
            elif kind == "merge":
                accepts[point] = tuple(d for d in OFFSETS if d != OPPOSITE[out_heading])
                sends[point] = (out_heading,)
            else:
                accepts[point], sends[point] = (in_heading,), (out_heading,)

    consumed = Counter()
    for p in layout.placements:
        geo = problem.buildings[p.building]
        try:
            oriented = oriented_geometry(p.building, geo, p.direction)
            input_cells, output_cell = placement_ports(problem, p)
        except ValueError as error:
            raise Infeasible(str(error)) from error
        require(tuple(oriented["size"]) == (p.width, p.height) and
                (p.x + oriented["anchor"][0], p.y + oriented["anchor"][1]) == p.anchor, f"{p.item}的朝向或定位格错误")
        expected_in, expected_out = {}, {}
        if p.building == "inlet":
            expected_out[output_cell] = (p.item, p.rate)
            require(p.rate <= problem.box_capacity, "进货口超出供料速率")
            consumed[p.item] += p.rate
        elif p.building == "outlet":
            expected_in[input_cells[0]] = (p.item, p.rate)
            require(p.rate <= problem.capacity and offset(p.anchor, p.direction) not in occupied, "成品接出口被遮挡")
        else:
            require(p.item in problem.ranks, f"{p.item}缺少可用配方")
            recipe = problem.recipes[p.item]
            require(p.building == recipe["machine"] and 0 < p.rate <= problem.rated(p.item), f"{p.item}机型或加工速度错误")
            for cell, (material, amount) in zip(input_cells, recipe["inputs"].items()):
                expected_in[cell] = (material, p.rate * amount / recipe["quantity"])
            if not recipe.get("virtual"):
                expected_out[output_cell] = (p.item, p.rate)
        if layout.extended:
            accepts.update((point, (p.direction,)) for point in expected_in)
            sends.update((point, (p.direction,)) for point in expected_out)
        for point in p.cells():
            ins, outs = incoming[point], outgoing[point]
            input_spec = [] if point not in expected_in else [expected_in[point]]
            output_spec = [] if point not in expected_out else [expected_out[point]]
            require([(s.item, s.rate) for s in ins] == input_spec and all(s.start == offset(point, OPPOSITE[p.direction]) for s in ins),
                    f"{p.item}的进料端口或流量错误")
            require([(s.item, s.rate) for s in outs] == output_spec and all(s.end == offset(point, p.direction) for s in outs),
                    f"{p.item}的出料端口或流量错误")
    if layout.extended:
        declared = {(s.start, s.end) for s in layout.segments if distance(s.start, s.end) == 1}
        for point, directions in sends.items():
            for direction in directions:
                neighbor = offset(point, direction)
                require(direction not in accepts.get(neighbor, ()) or (point, neighbor) in declared,
                        f"{point}与{neighbor}存在未规划的物流连接")
    finals = [p for item, _, p in layout.machines if item == problem.target]
    require(len(finals) == problem.count and sum(p.rate for p in finals) == problem.rate, "末端数量或产量错误")
    require(dict(consumed) == problem.raw, "原料用量未配平")
    anchors = {p.anchor for _, _, p in layout.machines if problem.buildings[p.building].get("power", 0)}
    if anchors:
        visited = {layout.power_entry}
        stack = [layout.power_entry]
        require(layout.power_entry in layout.wires and
                (layout.power_entry[0] in (0, layout.width - 1) or layout.power_entry[1] in (0, layout.height - 1)), "缺少边界电力接入口")
        while stack:
            x, y = stack.pop()
            for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if cell in layout.wires and cell not in visited:
                    visited.add(cell)
                    stack.append(cell)
        require(visited == layout.wires and anchors <= visited, "电网不连通")
