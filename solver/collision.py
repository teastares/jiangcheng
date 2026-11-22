"""用安装版像素碰撞范围校验格网中的布置。"""

from dataclasses import replace
from functools import lru_cache

from .geometry import (JUNCTIONS, STEAM_CELL_PIXELS, collision_rectangles_intersect,
                       steam_collision_rect, steam_hard_cells)


def uses_steam_geometry(problem):
    return getattr(problem, "catalog", {}).get("blueprint_format") == "steam-25121460"


@lru_cache(maxsize=None)
def relative_obstacles(building, facing, against):
    return steam_hard_cells(building, facing=facing, against=against)


def obstacles(placement, against):
    ax, ay = placement.anchor
    return ((ax + dx, ay + dy) for dx, dy in relative_obstacles(placement.building, placement.direction, against))


def belt_obstacles(problem, placements):
    if not uses_steam_geometry(problem):
        return set()
    return {point for p in placements for point in obstacles(p, "belt1")}


def body_collisions(placements):
    """按左边界扫描，只配对水平范围相交的实际机体。"""
    placed = [(rect, p) for p in placements
              if (rect := steam_collision_rect(p.building, p.anchor, p.direction)) is not None]
    placed.sort(key=lambda pair: pair[0][0])
    for index, (rect, p) in enumerate(placed):
        for other, q in placed[index + 1:]:
            if other[0] > rect[2]:
                break
            if collision_rectangles_intersect(rect, other):
                yield p, q


def collision_error(problem, placements, transport):
    if not uses_steam_geometry(problem):
        return None
    for p, q in body_collisions(placements):
        return f"{p.item} {p.anchor} 与{q.item} {q.anchor}的实际机体碰撞"
    if transport:
        for p in placements:
            for against in ("belt1", "split"):
                for point in obstacles(p, against):
                    kind = transport.get(point)
                    if kind and ((kind in JUNCTIONS) == (against in JUNCTIONS)):
                        return f"{p.item} {p.anchor}的实际机体遮挡物流设施 {point}"
    return None


def footprint_bounds(layout):
    """以格为单位返回能包住逻辑布局与实际机体的矩形。"""
    pitch = STEAM_CELL_PIXELS
    left, top, right, bottom = 0, 0, layout.width, layout.height
    for p in layout.placements:
        rect = steam_collision_rect(p.building, p.anchor, p.direction)
        if rect is not None:
            x1, y1, x2, y2 = rect
            left, top = min(left, x1 // pitch), min(top, y1 // pitch)
            right, bottom = max(right, x2 // pitch + 1), max(bottom, y2 // pitch + 1)
    return left, top, right, bottom


def enclose_footprints(problem, layout):
    if not uses_steam_geometry(problem):
        return layout
    left, top, right, bottom = footprint_bounds(layout)
    if (left, top, right, bottom) == (0, 0, layout.width, layout.height):
        return layout

    def shift(point):
        return point[0] - left, point[1] - top

    placed = {p: replace(p, x=p.x - left, y=p.y - top, anchor=shift(p.anchor)) for p in layout.placements}
    return replace(layout, width=right - left, height=bottom - top,
                   placements=list(placed.values()),
                   transport={shift(point): kind for point, kind in layout.transport.items()},
                   segments=[replace(s, start=shift(s.start), end=shift(s.end)) for s in layout.segments],
                   machines=[(item, rate, placed[p]) for item, rate, p in layout.machines],
                   wires=set(), power_entry=(0, 0), statistics={})
