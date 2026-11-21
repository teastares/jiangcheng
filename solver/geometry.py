"""设备朝向、占位、锚点和物流端口的公用几何。"""

SOUTH, WEST, EAST, NORTH = range(4)
OFFSETS = {SOUTH: (0, 1), WEST: (-1, 0), EAST: (1, 0), NORTH: (0, -1)}
OPPOSITE = {SOUTH: NORTH, WEST: EAST, EAST: WEST, NORTH: SOUTH}
DIRECTION_NAMES = {SOUTH: "南", WEST: "西", EAST: "东", NORTH: "北"}

# Steam 建筑与物流记录的方向编号不同。
MACHINE_DIRECTIONS = {NORTH: 0, EAST: 1, WEST: 2, SOUTH: 3}


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
    w, h = geometry["size"]
    anchor = tuple(geometry["anchor"])
    input_rows = geometry.get("inputs", ())
    output_row = geometry.get("output")
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


def placement_ports(problem, placement):
    """返回设备的绝对端口坐标，并仅保留当前配方使用的输入。"""
    building = placement.building
    geometry = oriented_geometry(building, problem.buildings[building], placement.direction)
    inputs, output = geometry["inputs"], geometry["output"]
    if building not in ("inlet", "outlet"):
        recipe = problem.recipes[placement.item]
        inputs = inputs[:len(recipe["inputs"])]
        if recipe.get("virtual"):
            output = None

    def absolute(point):
        return placement.x + point[0], placement.y + point[1]

    return tuple(absolute(point) for point in inputs), None if output is None else absolute(output)
