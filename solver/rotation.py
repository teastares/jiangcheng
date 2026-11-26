"""优先等量整图旋转，必要时在原胶囊预算内重布。"""

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from itertools import islice, permutations, product

from .collision import belt_obstacles, body_collisions, collision_error, enclose_footprints, uses_steam_geometry
from .compact import seam_compactions
from .geometry import (EAST, INLET_KINDS, JUNCTIONS, NORTH, OFFSETS, SOUTH, WEST, assigned_ports, inlet_output_ports,
                       offset, oriented_geometry, placement_ports)
from .layout import Layout, assess, distance, power_grid, verify
from .model import Infeasible
from .reroute import connections, endpoint_leads, rerouted_layouts
from .simulate import simulate


HEADINGS = (EAST, SOUTH, WEST, NORTH)
ANGLES = (90, 180, 270)


@dataclass(frozen=True)
class RotationResult:
    layout: Layout | None
    method: str
    reason: str


def rotated_direction(direction, angle):
    return HEADINGS[(HEADINGS.index(direction) + angle // 90) % len(HEADINGS)]


def rotated_point(point, width, height, angle):
    x, y = point
    return {0: (x, y), 90: (height - 1 - y, x),
            180: (width - 1 - x, height - 1 - y), 270: (y, width - 1 - x)}[angle]


def output_slots(problem, placement, active=None):
    if placement.building in INLET_KINDS:
        ports = inlet_output_ports(problem, placement)
        return ports if active is None else tuple(point for point in ports if point in active)
    output = placement_ports(problem, placement)[1]
    return () if output is None else (output,)


def matched_slots(positions, slots):
    return min(permutations(slots, len(positions)),
               key=lambda cells: (sum(a != b for a, b in zip(cells, positions)),
                                  sum(distance(a, b) for a, b in zip(cells, positions))))


def short_shifts(problem, layout):
    """沿原朝向平移单台设施一两格，维持已分配的物料入口。"""
    incoming = defaultdict(list)
    for edge in layout.segments:
        incoming[edge.end].append(edge)
    active_outputs = {edge.start for edge in layout.segments}
    for distance_to_move in (1, 2):
        for index, p in enumerate(layout.placements):
            inputs = assigned_ports(problem, p, incoming)[0]
            outputs = output_slots(problem, p, active_outputs)
            for dx, dy in OFFSETS.values():
                dx, dy = dx * distance_to_move, dy * distance_to_move
                x, y = p.x + dx, p.y + dy
                if not (0 <= x <= layout.width - p.width and 0 <= y <= layout.height - p.height):
                    continue

                def shift(point):
                    return (point[0] + dx, point[1] + dy), OFFSETS[p.direction]

                moved = replace(p, x=x, y=y, anchor=shift(p.anchor)[0])
                placements = list(layout.placements)
                placements[index] = moved
                changes = {point: shift(point) for point in inputs}, {point: shift(point) for point in outputs}
                yield from rerouted_layouts(problem, layout, placements=placements, max_routes=0, port_changes=changes)


def rotated_placements(problem, source, angle, *, preserve_wires):
    """根据旋转后的实际端口反求设备锚点，保持带格和路径。"""
    steam = uses_steam_geometry(problem)
    incoming = defaultdict(list)
    for edge in source.segments:
        incoming[edge.end].append(edge)
    active_outputs = {edge.start for edge in source.segments}

    def rotated(point):
        return rotated_point(point, source.width, source.height, angle)

    old_wires = {rotated(point) for point in source.wires}
    choice = []
    for old in source.placements:
        facing = rotated_direction(old.direction, angle)
        geometry = oriented_geometry(old.building, problem.buildings[old.building], facing)
        prototype = replace(old, x=0, y=0, width=geometry["size"][0], height=geometry["size"][1],
                            anchor=tuple(geometry["anchor"]), direction=facing)
        desired_in = tuple(map(rotated, assigned_ports(problem, old, incoming)[0]))
        desired_out = tuple(map(rotated, output_slots(problem, old, active_outputs)))
        slots = placement_ports(problem, prototype, all_inputs=steam)[0]
        emissions = output_slots(problem, prototype)
        if desired_out:
            origins = [(desired_out[0][0] - x, desired_out[0][1] - y) for x, y in emissions]
        elif desired_in:
            first = desired_in[0]
            origins = [(first[0] - x, first[1] - y) for x, y in slots]
        else:
            anchor = rotated(old.anchor)
            origins = [(anchor[0] - prototype.anchor[0], anchor[1] - prototype.anchor[1])]

        poses = []
        for x, y in origins:
            anchor = x + prototype.anchor[0], y + prototype.anchor[1]
            actual_in = tuple((x + i, y + j) for i, j in slots)
            actual_out = {(x + i, y + j) for i, j in emissions}
            ports_match = set(desired_in) <= set(actual_in) if steam else actual_in == desired_in
            needs_power = problem.buildings[old.building].get("power")
            wires_match = not preserve_wires or not needs_power or anchor in old_wires
            if ports_match and set(desired_out) <= actual_out and wires_match:
                poses.append(replace(prototype, x=x, y=y, anchor=anchor))
        if not poses:
            return
        expected = rotated(old.anchor)
        choice.append(sorted(poses, key=lambda placement: (distance(expected, placement.anchor), placement.anchor)))
    yield from islice(product(*choice), 32)


def rotation_plan(problem, layout, angle, scale, margin, *, keep_anchor=False, cuts=((), ())):
    width, height = layout.width, layout.height
    scale_x, scale_y = scale

    def transform(point):
        x, y = rotated_point(point, width, height, angle)
        return (int(x * scale_x) + margin + bisect_right(cuts[0], x),
                int(y * scale_y) + margin + bisect_right(cuts[1], y))

    old_placements, placements = [], []
    inputs, outputs = {}, {}
    steam = bool(problem.catalog.get("blueprint_format"))
    incoming = defaultdict(list)
    for edge in layout.segments:
        incoming[edge.end].append(edge)
    active_outputs = {edge.start for edge in layout.segments}

    for old in layout.placements:
        facing = rotated_direction(old.direction, angle)
        corners = [transform((x, y)) for x in (old.x, old.x + old.width - 1)
                   for y in (old.y, old.y + old.height - 1)]
        left, top = min(x for x, _ in corners), min(y for _, y in corners)
        right, bottom = max(x for x, _ in corners), max(y for _, y in corners)
        anchor = transform(old.anchor)
        old_placements.append(replace(old, x=left, y=top, width=right - left + 1, height=bottom - top + 1,
                                      anchor=anchor, direction=facing))
        geometry = oriented_geometry(old.building, problem.buildings[old.building], facing)
        w, h = geometry["size"]
        ax, ay = geometry["anchor"]
        x, y = (anchor[0] - ax, anchor[1] - ay) if keep_anchor else ((left + right - w + 1) // 2,
                                                                   (top + bottom - h + 1) // 2)
        new = replace(old, x=x, y=y, width=w, height=h, anchor=(x + ax, y + ay), direction=facing)
        placements.append(new)
        old_inputs = assigned_ports(problem, old, incoming)[0]
        old_outputs = output_slots(problem, old, active_outputs)
        rotated_inputs = tuple(map(transform, old_inputs))
        rotated_outputs = tuple(map(transform, old_outputs))
        new_inputs = placement_ports(problem, new, all_inputs=steam)[0]
        new_outputs = matched_slots(rotated_outputs, output_slots(problem, new))
        if steam and rotated_inputs:
            # 游戏按物品 ID 分配配方槽，优先保留旋转后已经对齐的进料口。
            new_inputs = matched_slots(rotated_inputs, new_inputs)
        for point, after in zip(rotated_inputs, new_inputs):
            if point != after:
                inputs[point] = after, OFFSETS[facing]
        for point, after in zip(rotated_outputs, new_outputs):
            if point != after:
                outputs[point] = after, OFFSETS[facing]

    bound = (height, width) if angle in (90, 270) else (width, height)
    new_width, new_height = (int((length - 1) * factor) + 1 + margin * 2 + len(inserted)
                             for length, factor, inserted in zip(bound, scale, cuts))
    rotated = replace(layout, width=new_width, height=new_height, placements=old_placements,
                      transport={transform(point): kind for point, kind in layout.transport.items()},
                      segments=[replace(edge, start=transform(edge.start), end=transform(edge.end)) for edge in layout.segments],
                      wires={transform(point) for point in layout.wires} if scale == (1, 1) and not any(cuts) else set(),
                      power_entry=transform(layout.power_entry), statistics={}, row_optimal=False)
    return rotated, placements, (inputs, outputs)


def clearance_cuts(problem, layout, placements, changes):
    occupied = {point for p in placements for point in p.cells()}
    occupied.update(offset(p.anchor, p.direction) for p in placements if p.building == "outlet")
    occupied.update(point for point, kind in layout.transport.items() if kind in JUNCTIONS)
    inputs, outputs = changes
    leads = Counter()
    for route in connections(layout):
        start, incoming = outputs.get(route.start, (route.start, route.incoming))
        end, outgoing = inputs.get(route.end, (route.end, route.outgoing))
        leads.update(endpoint_leads(replace(route, start=start, incoming=incoming, end=end, outgoing=outgoing)))
    occupied.update(belt_obstacles(problem, placements))
    collisions = {point for point, count in leads.items() if point in occupied or count > 1}
    # 在发生引线冲突的坐标前留空，最多沿一个轴插入三格通道。
    variants = []
    for axis, bound in enumerate((layout.width, layout.height)):
        cuts = tuple(sorted({p[axis] for p in collisions if 0 < p[axis] < bound}))
        if 1 <= len(cuts) <= 3:
            variants.append((cuts, ()) if axis == 0 else ((), cuts))
    if uses_steam_geometry(problem):
        xs, ys = set(), set()
        for p, q in body_collisions(placements):
            left, right = sorted((p, q), key=lambda device: device.x)
            upper, lower = sorted((p, q), key=lambda device: device.y)
            if left.x + left.width <= right.x:
                xs.add(right.x)
            elif upper.y + upper.height <= lower.y:
                ys.add(lower.y)
        if xs or ys:
            variants.append((tuple(sorted(xs)), tuple(sorted(ys))))
    return sorted(set(variants), key=lambda cuts: ((layout.width + len(cuts[0])) * (layout.height + len(cuts[1])), cuts))


def check_rotation(problem, source, candidate, *, rewire=True):
    if rewire:
        candidate = enclose_footprints(problem, candidate)
        wire_rotation(problem, candidate)
    try:
        verify(problem, candidate)
        assess(problem, candidate)
        if "simulation" in source.statistics:
            result = simulate(problem, candidate)
            if (source.statistics["simulation"]["last_four_per_minute"] >= float(problem.rate) and
                    result["last_four_per_minute"] < float(problem.rate) - 1e-8):
                return None
            candidate.statistics["simulation"] = result
    except Infeasible:
        return None
    return candidate


def wire_rotation(problem, layout):
    anchors = {p.anchor for _, _, p in layout.machines if problem.buildings[p.building].get("power")}
    if anchors and not (anchors <= layout.wires and
                        (layout.power_entry[0] in (0, layout.width - 1) or layout.power_entry[1] in (0, layout.height - 1))):
        layout.wires, layout.power_entry = power_grid(anchors, layout.width, layout.height)


def rotation_result(problem, layout, angle):
    """优先纯旋转，重布候选也必须在原胶囊数内完成。"""
    original_scale = (1, 1)
    room = ((1.5, 1), (1, 1.5), (2, 1), (1, 2), (1.5, 1.5), (2, 1.5), (1.5, 2), (2, 2), (3, 3))
    original = rotation_plan(problem, layout, angle, original_scale, 0)
    machines = {p.node for _, _, p in layout.machines}

    def with_placements(plan, placements):
        return replace(plan, placements=list(placements),
                       machines=[(p.item, p.rate, p) for p in placements if p.node in machines])

    for placements in rotated_placements(problem, layout, angle, preserve_wires=True):
        if verified := check_rotation(problem, layout, with_placements(original[0], placements), rewire=False):
            return RotationResult(verified, "rigid", "沿同一格网旋转，设备、输送带、电线和胶囊数量全部保留。")

    capsules = len(layout.placements) + len(layout.transport)
    compact = ((False, False), (True, False), (False, True))
    no_cuts = (), ()
    settings = [(original_scale, 0, anchor, full, no_cuts) for anchor, full in compact]
    settings.extend((original_scale, 0, anchor, True, cuts) for cuts in clearance_cuts(problem, *original)
                    for anchor in (False, True))
    settings.extend((original_scale, 2, anchor, full, no_cuts) for anchor, full in compact)
    settings.extend((scale, 3, False, True, no_cuts) for scale in room)
    reason = "设备旋转后的端口、碰撞范围或电线接点发生变化，未找到不增加胶囊且通过核验的方案。"

    def candidates(draft, placements, changes, reroute_all, unchanged):
        if unchanged and not any(changes):
            yield with_placements(draft, placements)
        yield from rerouted_layouts(problem, draft, placements=placements, max_routes=0,
                                    port_changes=changes, reroute_all=reroute_all)

    def can_place(proposal):
        return collision_error(problem, proposal.placements, proposal.transport) is None

    def within_budget(proposal):
        return len(proposal.placements) + len(proposal.transport) <= capsules

    def tightened(proposal):
        return seam_compactions(proposal, can_place=can_place)

    def rewired(verified):
        verified.logistics_milp = {}
        return RotationResult(verified, "rewired",
                              f"已按实际端口、机体范围或电线接点重布，胶囊 {capsules} → {verified.statistics['components']} 个。")

    aligned = []
    for placements in rotated_placements(problem, layout, angle, preserve_wires=False):
        candidate = with_placements(original[0], placements)
        if verified := check_rotation(problem, layout, candidate):
            return rewired(verified)
        aligned.append(candidate)

    def proposals():
        # 端口已对齐时先保留原线网，只移动需要留出净空的设施。
        for candidate in aligned[:2]:
            for proposal in short_shifts(problem, candidate):
                yield from tightened(proposal)
                yield proposal
        if aligned:
            wider = rotation_plan(problem, layout, angle, original_scale, 1)[0]
            for candidate in aligned[:2]:
                placements = [replace(p, x=p.x + 1, y=p.y + 1, anchor=(p.anchor[0] + 1, p.anchor[1] + 1))
                              for p in candidate.placements]
                for proposal in short_shifts(problem, with_placements(wider, placements)):
                    yield from tightened(proposal)
                    yield proposal
        for scale, margin, keep_anchor, reroute_all, cuts in settings:
            unchanged = scale == original_scale and not (margin or any(cuts))
            if unchanged and not keep_anchor:
                rotated, placements, changes = original
            else:
                rotated, placements, changes = rotation_plan(problem, layout, angle, scale, margin,
                                                              keep_anchor=keep_anchor, cuts=cuts)
            for candidate in candidates(rotated, placements, changes, reroute_all, unchanged):
                if not unchanged or not within_budget(candidate):
                    yield from tightened(candidate)
                yield candidate

    near_budget = []
    for proposal in proposals():
        excess = len(proposal.placements) + len(proposal.transport) - capsules
        if excess <= 0:
            if verified := check_rotation(problem, layout, proposal):
                return rewired(verified)
        elif excess <= 3:
            rank = excess, proposal.width * proposal.height
            if len(near_budget) == 2 and rank >= near_budget[-1][0]:
                continue
            if verified := check_rotation(problem, layout, proposal):
                distinct = all((verified.transport, verified.placements) != (old.transport, old.placements)
                               for _, old in near_budget)
                if distinct:
                    near_budget.append((rank, verified))
                    near_budget.sort(key=lambda pair: pair[0])
                    del near_budget[2:]

    for _, near in near_budget:
        for candidate in short_shifts(problem, near):
            for proposal in (*tightened(candidate), candidate):
                if within_budget(proposal) and (verified := check_rotation(problem, layout, proposal)):
                    return rewired(verified)
    return RotationResult(None, "unavailable", reason)


def rotate_layout(problem, layout, angle):
    """返回不增加胶囊且可用的换向布局。"""
    return rotation_result(problem, layout, angle).layout
