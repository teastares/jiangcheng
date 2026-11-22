"""按真实端口旋转成品布局，并在端口错位时修正接线。"""

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
from itertools import permutations

from .collision import belt_obstacles, body_collisions, collision_error, enclose_footprints, uses_steam_geometry
from .compact import seam_compactions
from .geometry import EAST, NORTH, OFFSETS, SOUTH, WEST, assigned_ports, offset, oriented_geometry, placement_ports
from .layout import assess, distance, power_grid, verify
from .model import Infeasible
from .reroute import connections, endpoint_leads, rerouted_layouts
from .simulate import simulate


HEADINGS = (EAST, SOUTH, WEST, NORTH)
ANGLES = (90, 180, 270)


def rotated_direction(direction, angle):
    return HEADINGS[(HEADINGS.index(direction) + angle // 90) % len(HEADINGS)]


def rotated_point(point, width, height, angle):
    x, y = point
    return {0: (x, y), 90: (height - 1 - y, x),
            180: (width - 1 - x, height - 1 - y), 270: (y, width - 1 - x)}[angle]


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
        old_inputs, old_output = assigned_ports(problem, old, incoming)
        rotated_inputs = tuple(map(transform, old_inputs))
        new_inputs, new_output = placement_ports(problem, new, all_inputs=steam)
        if steam and rotated_inputs:
            # 游戏按物品 ID 分配配方槽，优先保留旋转后已经对齐的进料口。
            new_inputs = min(permutations(new_inputs, len(rotated_inputs)),
                             key=lambda cells: (sum(a != b for a, b in zip(cells, rotated_inputs)),
                                                sum(distance(a, b) for a, b in zip(cells, rotated_inputs))))
        for point, after in zip(rotated_inputs, new_inputs):
            if point != after:
                inputs[point] = after, OFFSETS[facing]
        if old_output is not None and (point := transform(old_output)) != new_output:
            outputs[point] = new_output, OFFSETS[facing]

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
    occupied.update(point for point, kind in layout.transport.items() if kind in ("split", "merge"))
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


def check_rotation(problem, source, candidate):
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


def rotate_layout(problem, layout, angle):
    """保留工序与端口流量，为目标方向生成一个通过核验的布局。"""
    original_scale = (1, 1)
    room = ((1.5, 1), (1, 1.5), (2, 1), (1, 2), (1.5, 1.5), (2, 1.5), (1.5, 2), (2, 2), (3, 3))
    original = rotation_plan(problem, layout, angle, original_scale, 0)
    compact = ((False, False), (True, False), (False, True))
    no_cuts = (), ()
    settings = [(original_scale, 0, anchor, full, no_cuts) for anchor, full in compact]
    settings.extend((original_scale, 0, anchor, True, cuts) for cuts in clearance_cuts(problem, *original)
                    for anchor in (False, True))
    settings.extend((original_scale, 2, anchor, full, no_cuts) for anchor, full in compact)
    settings.extend((scale, 3, False, True, no_cuts) for scale in room)
    machines = {p.node for _, _, p in layout.machines}
    for scale, margin, keep_anchor, reroute_all, cuts in settings:
        if scale == original_scale and not (margin or keep_anchor or any(cuts)):
            rotated, placements, changes = original
        else:
            rotated, placements, changes = rotation_plan(problem, layout, angle, scale, margin, keep_anchor=keep_anchor, cuts=cuts)
        rotated.machines = [(p.item, p.rate, p) for p in placements if p.node in machines]
        wire_rotation(problem, rotated)
        direct = replace(rotated, placements=placements)
        if scale == original_scale and not (any(changes) or any(cuts)):
            if verified := check_rotation(problem, layout, direct):
                return verified
        for candidate in rerouted_layouts(problem, rotated, placements=placements, max_routes=0,
                                          port_changes=changes, reroute_all=reroute_all):
            if verified := check_rotation(problem, layout, candidate):
                if margin or any(cuts):
                    def can_place(proposal):
                        return collision_error(problem, proposal.placements, proposal.transport) is None

                    for reduced in seam_compactions(verified, can_place=can_place):
                        if compact := check_rotation(problem, layout, reduced):
                            if compact.width * compact.height < verified.width * verified.height:
                                return compact
                return verified
    return None
