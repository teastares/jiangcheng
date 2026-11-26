"""设备朝向、占位、锚点和物流端口的公用几何。"""

from .device_catalog import LARGE_INLET_OUTPUT_OFFSETS


SOUTH, WEST, EAST, NORTH = range(4)
OFFSETS = {SOUTH: (0, 1), WEST: (-1, 0), EAST: (1, 0), NORTH: (0, -1)}
OPPOSITE = {SOUTH: NORTH, WEST: EAST, EAST: WEST, NORTH: SOUTH}
DIRECTION_NAMES = {SOUTH: "南", WEST: "西", EAST: "东", NORTH: "北"}

# Steam 建筑与物流记录的方向编号不同。
MACHINE_DIRECTIONS = {NORTH: 0, EAST: 1, WEST: 2, SOUTH: 3}

JUNCTIONS = {"split", "merge"}
INLET_KINDS = {"inlet", "inlet_large"}
STEAM_CELL_PIXELS = 16


def _axis_rects(horizontal, vertical):
    return {EAST: horizontal, WEST: horizontal, NORTH: vertical, SOUTH: vertical}


# Steam 25121460 的 BBase.computeXY/computeRect 和各设备 setXY/getRect。
# 相对于记录的定位格，顺序为左、上、右、下，单位为安装版世界像素。
_MAKER_RECTS = _axis_rects((2, -8, 30, 20), (-6, -13, 22, 15))
_ASSEMBLER_RECTS = _axis_rects((-14, -20, 30, 20), (-6, -25, 38, 15))
_STEAM_COLLISION_RECTS = {
    "inlet": _axis_rects((2, -4, 30, 20), (-6, -9, 22, 15)),
    "inlet_large": _axis_rects((-14, -16, 30, 20), (-6, -21, 38, 15)),
    "maker": _MAKER_RECTS,
    "smelter": _MAKER_RECTS,
    "assembler": _ASSEMBLER_RECTS,
    "charger": _ASSEMBLER_RECTS,
    "synthesizer": _axis_rects((-14, -22, 30, 20), (-19, -23, 35, 15)),
    "forge": _axis_rects((-14, -24, 30, 14), (-22, -23, 22, 15)),
    "exchanger": {EAST: (2, -15, 40, 20), WEST: (10, -15, 48, 20),
                  NORTH: (-3, -20, 35, 15), SOUTH: (-3, -20, 35, 15)},
    "grain_mill": {EAST: (-14, -32, 14, 18), WEST: (2, -32, 30, 20),
                   NORTH: (-20, -15, 32, 15), SOUTH: (-16, -15, 36, 15)},
}


def steam_collision_rect(kind, anchor=(0, 0), facing=EAST):
    """返回 Steam 设施的像素碰撞矩形，电线和未核对的设施返回空值。"""
    if kind in _STEAM_COLLISION_RECTS:
        rect = _STEAM_COLLISION_RECTS[kind][facing]
    elif kind in JUNCTIONS:
        rect = (0, 1, 12, 15)
    elif kind == "outlet" or kind in ("belt1", "belt2", "belt3", "belt4", "belt5",
                                      "tunnel1", "tunnel2", "tunnel3", "tunnel4", "tunnel5"):
        rect = (1, 9, 14, 14)
    else:
        return None
    left, top, right, bottom = rect
    x, y = anchor[0] * STEAM_CELL_PIXELS, anchor[1] * STEAM_CELL_PIXELS
    return left + x, top + y, right + x, bottom + y


def collision_rectangles_intersect(a, b):
    """按安装版 IRect.intersects 比较，边缘接触也属于相交。"""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def steam_hard_cells(kind, anchor=(0, 0), facing=EAST, *, against="belt1", against_facing=EAST):
    """枚举与该设施碰撞的目标锚格，默认以地面带作为目标。"""
    rect = steam_collision_rect(kind, anchor, facing)
    other = steam_collision_rect(against, facing=against_facing)
    if rect is None or other is None:
        return frozenset()
    left, top, right, bottom = rect
    other_left, other_top, other_right, other_bottom = other
    pitch = STEAM_CELL_PIXELS
    min_x, max_x = (left - other_right + pitch - 1) // pitch, (right - other_left) // pitch
    min_y, max_y = (top - other_bottom + pitch - 1) // pitch, (bottom - other_top) // pitch
    return frozenset((x, y) for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1))


def direct_logistics_allowed(source, sink):
    """判断两个相邻设施能否直接交接物料，端口方向另行校验。"""
    if source in JUNCTIONS and sink in JUNCTIONS:
        return False
    if source in JUNCTIONS or sink in JUNCTIONS:
        return True
    return any(kind == "outlet" or (kind or "").startswith(("belt", "tunnel")) for kind in (source, sink))

# 安装版 BMachine.rotate 和 BInBox.rotate 记录的是定位格外侧的相邻物流格。
# 每行是占位尺寸、定位格，以及相对定位格的外侧进出料格。
_INLET = {
    WEST: ((2, 1), (0, 0), (), (-1, 0)),
    NORTH: ((1, 2), (0, 1), (), (0, -2)),
    SOUTH: ((1, 2), (0, 1), (), (0, 1)),
}
_SINGLE_INPUT = {
    WEST: ((2, 1), (0, 0), ((2, 0),), (-1, 0)),
    NORTH: ((1, 2), (0, 1), ((0, 1),), (0, -2)),
    SOUTH: ((1, 2), (0, 1), ((0, -2),), (0, 1)),
}
_TWO_INPUTS = {
    WEST: ((3, 2), (1, 1), ((2, -1), (2, 0)), (-2, 0)),
    NORTH: ((2, 3), (0, 2), ((0, 1), (1, 1)), (0, -3)),
    SOUTH: ((2, 3), (0, 2), ((0, -3), (1, -3)), (1, 1)),
}
_STEAM_ROTATIONS = {
    "inlet": (((2, 1), (0, 0), (), 0), _INLET),
    "maker": (((2, 1), (0, 0), (0,), 0), _SINGLE_INPUT),
    "smelter": (((2, 1), (0, 0), (0,), 0), _SINGLE_INPUT),
    "assembler": (((3, 2), (1, 1), (0, 1), 0), _TWO_INPUTS),
    "charger": (((3, 2), (1, 1), (0, 1), 0), _TWO_INPUTS),
    "synthesizer": (((3, 3), (1, 2), (0, 1, 2), 2), {
        WEST: ((3, 3), (1, 2), ((2, -2), (2, -1), (2, 0)), (-2, -2)),
        NORTH: ((3, 3), (1, 2), ((0, 1), (-1, 1), (1, 1)), (1, -3)),
        SOUTH: ((3, 3), (1, 2), ((0, -3), (-1, -3), (1, -3)), (-1, 1)),
    }),
    "forge": (((3, 3), (1, 2), (0, 1), 1), {
        WEST: ((3, 3), (1, 2), ((2, -2), (2, -1)), (-2, -2)),
        NORTH: ((2, 3), (1, 2), ((0, 1), (-1, 1)), (0, -3)),
        SOUTH: ((2, 3), (1, 2), ((0, -3), (-1, -3)), (-1, 1)),
    }),
    "exchanger": (((3, 2), (0, 1), (0, 1), None), {
        WEST: ((3, 2), (0, 1), ((3, -1), (3, 0)), None),
        NORTH: ((2, 2), (0, 1), ((0, 1), (1, 1)), None),
        # 南朝碰撞主体仍为两行，规划额外预留北侧入料通道的一行。
        SOUTH: ((2, 3), (0, 2), ((0, -3), (1, -3)), None),
    }),
    "grain_mill": (((2, 3), (1, 2), (0,), 2), {
        WEST: ((2, 3), (0, 2), ((2, 0),), (-1, -2)),
        NORTH: ((3, 2), (1, 1), ((-1, 1),), (1, -2)),
        SOUTH: ((3, 2), (1, 1), ((1, -2),), (-1, 1)),
    }),
}


def offset(point, direction, distance=1):
    dx, dy = OFFSETS[direction]
    return point[0] + dx * distance, point[1] + dy * distance


def direction(start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    return {(-1, 0): WEST, (1, 0): EAST, (0, -1): NORTH, (0, 1): SOUTH}[
        (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)]


def observed_directions(building):
    """给出既有 Steam 25121460 蓝图样本涉及的设备布置方向。"""
    return {"inlet": (EAST, WEST, NORTH), "maker": (EAST, WEST),
            "assembler": (EAST, WEST), "exchanger": (EAST, NORTH),
            "outlet": (EAST, WEST, NORTH, SOUTH)}.get(building, (EAST,))


def oriented_geometry(building, geometry, facing):
    """按配方顺序给出输入坐标，以及相对于占位左上角的输出和锚点。"""
    if building == "inlet_large":
        size, anchor = ((3, 2), (1, 1)) if facing in (EAST, WEST) else ((2, 3), (0, 2))
        x, y = LARGE_INLET_OUTPUT_OFFSETS[facing][0]
        output = offset((anchor[0] + x, anchor[1] + y), OPPOSITE[facing])
        return {"size": size, "anchor": anchor, "inputs": (), "output": output}
    w, h = geometry["size"]
    anchor = tuple(geometry["anchor"])
    input_rows = tuple(geometry.get("inputs", ()))
    output_row = geometry.get("output")
    steam = _STEAM_ROTATIONS.get(building)
    if steam and steam[0] == ((w, h), anchor, input_rows, output_row) and facing in steam[1]:
        size, anchor, outer_inputs, outer_output = steam[1][facing]
        ax, ay = anchor
        inputs = tuple(offset((ax + x, ay + y), facing) for x, y in outer_inputs)
        output = None if outer_output is None else offset((ax + outer_output[0], ay + outer_output[1]), OPPOSITE[facing])
        return {"size": size, "anchor": anchor, "inputs": inputs, "output": output}
    if facing == EAST:
        size = (w, h)
        inputs = tuple((0, row) for row in input_rows)
        output = None if output_row is None else (w - 1, output_row)
    elif facing == WEST:
        size = (w, h)
        # 西向组装机保留投入的上下顺序，从左下出料。
        inputs = tuple((w - 1, row) for row in input_rows)
        output = None if output_row is None else (0, h - 1 - output_row)
    elif facing == NORTH:
        size = (h, w)
        anchor = (anchor[1], w - 1 - anchor[0])
        if building == "exchanger" and (w, h) == (3, 2):
            # 旧双绿瓶样本的北向置换机使用独立的 2×2 图块。
            size, anchor = (2, 2), (0, 1)
        inputs = tuple((row, size[1] - 1) for row in input_rows)
        output = None if output_row is None else (output_row, 0)
    elif facing == SOUTH:
        size = (h, w)
        anchor = (h - 1 - anchor[1], anchor[0])
        inputs = tuple((h - 1 - row, 0) for row in input_rows)
        output = None if output_row is None else (h - 1 - output_row, w - 1)
    else:
        raise ValueError("未知设备方向")
    return {"size": size, "anchor": anchor, "inputs": inputs, "output": output}


def placement_ports(problem, placement, *, all_inputs=False):
    """返回设备的绝对端口坐标，默认只取当前配方使用的输入。"""
    building = placement.building
    geometry = oriented_geometry(building, problem.buildings[building], placement.direction)
    inputs, output = geometry["inputs"], geometry["output"]
    if building not in INLET_KINDS and building != "outlet":
        recipe = problem.recipes[placement.item]
        if not all_inputs:
            inputs = inputs[:len(recipe["inputs"])]
        if recipe.get("virtual"):
            output = None

    def absolute(point):
        return placement.x + point[0], placement.y + point[1]

    return tuple(absolute(point) for point in inputs), None if output is None else absolute(output)


def inlet_output_ports(problem, placement):
    """返回与实际外部出带格相邻的两个或一个箱内出货格。"""
    if placement.building == "inlet_large":
        facing = placement.direction
        x, y = placement.anchor
        return tuple(offset((x + dx, y + dy), OPPOSITE[facing])
                     for dx, dy in LARGE_INLET_OUTPUT_OFFSETS[facing])
    return (placement_ports(problem, placement)[1],)


def assigned_ports(problem, placement, incoming):
    """依真实入料物品匹配 Steam 机台的物理入口，返回配方顺序。"""
    if (placement.building in INLET_KINDS or placement.building == "outlet" or
            getattr(problem, "catalog", {}).get("blueprint_format") != "steam-25121460"):
        return placement_ports(problem, placement)
    inputs, output = placement_ports(problem, placement, all_inputs=True)
    recipe_inputs = problem.recipes[placement.item]["inputs"]
    connected = [(point, incoming[point]) for point in inputs if incoming.get(point)]
    if len(connected) == len(recipe_inputs) and all(len(segments) == 1 for _, segments in connected):
        by_item = {segments[0].item: point for point, segments in connected}
        if by_item.keys() == recipe_inputs.keys():
            return tuple(by_item[item] for item in recipe_inputs), output
    return placement_ports(problem, placement)
