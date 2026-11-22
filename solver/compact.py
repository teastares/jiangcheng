"""在保留建筑端口和物流连接的条件下压紧格位。"""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace

from .geometry import direct_logistics_allowed


def find_cut(allowed, barriers, shortcuts):
    paths = {column: (int((column, 0) in shortcuts), 0, (column,)) for column in allowed[0]}
    for row in range(1, len(allowed)):
        boundary = sorted(barriers[row - 1])
        groups = defaultdict(list)
        for column, score in paths.items():
            groups[bisect_right(boundary, column)].append((column, score))
        paths = {}
        for column in allowed[row]:
            choices = groups[bisect_right(boundary, column)]
            if choices:
                saved = int((column, row) in shortcuts)
                paths[column] = max((length + saved, turns - abs(column - previous), (*path, column))
                                    for previous, (length, turns, path) in choices)
        if not paths:
            return None
    return max(paths.values())[2] if paths else None


def remove_cut(layout, axis):
    def axes(point):
        return point[axis], point[1 - axis]

    width, height = axes((layout.width, layout.height))
    if width <= 1:
        return None
    allowed = [set(range(width)) for _ in range(height)]
    barriers = [set() for _ in range(height - 1)]
    for placement in layout.placements:
        x, y = axes((placement.x, placement.y))
        w, h = axes((placement.width, placement.height))
        for row in range(y, y + h):
            allowed[row].difference_update(range(x, x + w))
        for row in range(y, y + h - 1):
            barriers[row].update(range(x, x + w))

    incoming, outgoing = defaultdict(list), defaultdict(list)
    tunnel_spans = []
    for segment in layout.segments:
        incoming[segment.end].append(segment)
        outgoing[segment.start].append(segment)
        left, top = axes(segment.start)
        right, bottom = axes(segment.end)
        if left == right:
            for row in range(min(top, bottom), max(top, bottom)):
                barriers[row].add(left)
        elif top == bottom and abs(left - right) > 1:
            tunnel_spans.append((top, min(left, right), max(left, right)))

    kinds = {**layout.transport, **{point: p.building for p in layout.placements for point in p.cells()}}
    shortcuts = {}
    for point, kind in layout.transport.items():
        x, y = axes(point)
        ins, outs = incoming[point], outgoing[point]
        if kind.startswith("belt") and len(ins) == len(outs) == 1:
            a, b = ins[0], outs[0]
            left, top = axes(a.start)
            right, bottom = axes(b.end)
            if (top == y == bottom and {left, right} == {x - 1, x + 1} and a.bus == b.bus and a.rate == b.rate and
                    direct_logistics_allowed(kinds[a.start], kinds[b.end])):
                shortcuts[x, y] = a, b
                continue
        allowed[y].discard(x)
    for row, left, right in tunnel_spans:
        allowed[row].difference_update(range(left, right + 1))

    # 切口每行可拐弯，经过设备跨行矩形或横跨切口的物流时必须保持同侧。
    path = find_cut(allowed, barriers, shortcuts)
    if path is None:
        return None

    def shift(point):
        x, y = axes(point)
        return axes((x - (x > path[y]), y))

    removed = {axes((x, y)): shortcuts[x, y] for y, x in enumerate(path) if (x, y) in shortcuts}
    segments = [segment for segment in layout.segments if segment.start not in removed and segment.end not in removed]
    segments.extend(replace(a, end=b.end) for a, b in removed.values())
    placements = {}
    for p in layout.placements:
        x, y = shift((p.x, p.y))
        placements[p.node] = replace(p, x=x, y=y, anchor=shift(p.anchor))
    w, h = axes((width - 1, height))
    return replace(layout, width=w, height=h, placements=list(placements.values()),
                   transport={shift(point): kind for point, kind in layout.transport.items() if point not in removed},
                   segments=[replace(s, start=shift(s.start), end=shift(s.end)) for s in segments],
                   wires=set(), power_entry=(0, 0), machines=[(item, rate, placements[p.node]) for item, rate, p in layout.machines],
                   row_optimal=False, statistics={}, baseline=False)


def seam_compactions(layout, *, can_place=None):
    """依次尝试两个方向，返回待重拉电线和验证的压紧方案。"""
    candidates = []
    for axes in ((0, 1), (1, 0)):
        candidate = layout
        while True:
            previous = candidate
            for axis in axes:
                reduced = remove_cut(candidate, axis)
                if reduced is not None and (can_place is None or can_place(reduced)):
                    candidate = reduced
            if candidate is previous:
                break
        if candidate is not layout and candidate not in candidates:
            candidates.append(candidate)
            yield candidate
