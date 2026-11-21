"""在已有可行电网上比较有界的接电走法。"""

from random import Random


def refine_power_grid(anchors, width, height, incumbent):
    from .layout import connect_boundary, distance, unit_path

    anchors = sorted(set(anchors))
    if len(anchors) <= 1:
        return incumbent

    random = Random(25121460)
    best_wires, best_entry = incumbent
    for _ in range(24):
        start = random.choice(anchors)
        wires = {start}
        needs = set(anchors) - wires
        distances = {point: distance(point, start) for point in needs}
        while needs:
            shortest = min(distances[point] for point in needs)
            point = random.choice(sorted(point for point in needs if distances[point] == shortest))
            contact = random.choice(sorted(cell for cell in wires if distance(point, cell) == shortest))
            path = set(unit_path(point, contact, vertical_first=bool(random.randrange(2))))
            added = path - wires
            wires.update(added)
            needs.difference_update(added)
            for next_point in needs:
                distances[next_point] = min(distances[next_point], min(distance(next_point, cell) for cell in added))
        entry = connect_boundary(wires, width, height)
        if len(wires) < len(best_wires):
            best_wires, best_entry = wires, entry
    return best_wires, best_entry
