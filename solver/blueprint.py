from base64 import b64decode, b64encode
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from struct import pack, unpack
from uuid import NAMESPACE_URL, uuid5

from .geometry import EAST, NORTH, SOUTH, WEST, MACHINE_DIRECTIONS, OPPOSITE, direction, offset


FORMAT = "steam-25121460"
IDS = {"inlet": 4, "split": 40, "merge": 41, "maker": 50, "assembler": 51, "synthesizer": 52,
       "smelter": 55, "forge": 56, "charger": 60, "exchanger": 61, "grain_mill": 62, "wire": 250}
IDS.update({f"belt{tier}": 19 + tier for tier in range(1, 6)})
IDS.update({f"tunnel{tier}": 29 + tier for tier in range(1, 6)})
MACHINE_SETTINGS = {50: (4, 1), 51: (18, 2), 52: (64, 3), 55: (5, 1), 56: (16, 2),
                    60: (20, 2), 61: (18, 2), 62: (8, 1)}
RECORD_BYTES = {4: 331, 40: 76, 41: 76, 250: 80, **{i: 80 for i in range(20, 25)},
                **{i: 84 for i in range(30, 35)}, **{i: 108 + 4 * settings[1] for i, settings in MACHINE_SETTINGS.items()}}
TURN = {(EAST, SOUTH): 4, (NORTH, WEST): 5, (EAST, NORTH): 6, (SOUTH, WEST): 7,
        (SOUTH, EAST): 8, (WEST, NORTH): 9, (NORTH, EAST): 10, (WEST, SOUTH): 11}


@dataclass(frozen=True)
class Record:
    id: int
    x: int
    y: int
    direction: int
    shape: int
    recipe: int = -1


def records_for(problem, layout):
    inputs, outputs = defaultdict(list), defaultdict(list)
    for segment in layout.segments:
        inputs[segment.end].append(segment.start)
        outputs[segment.start].append(segment.end)
    buildings = []
    for p in layout.placements:
        if p.building == "inlet":
            buildings.append(Record(IDS[p.building], *p.anchor, p.direction, p.direction))
        elif p.building == "outlet":
            kind = next(b["building"] for b in problem.belts if b["capacity"] >= p.rate)
            buildings.append(Record(IDS[kind], *p.anchor, p.direction, p.direction))
        else:
            buildings.append(Record(IDS[p.building], *p.anchor, MACHINE_DIRECTIONS[p.direction], 0,
                                    problem.recipes[p.item]["id"]))

    for point, kind in sorted(layout.transport.items()):
        if kind == "split":
            facing = shape = direction(inputs[point][0], point)
        elif kind == "merge":
            facing = shape = direction(point, outputs[point][0])
        elif kind.startswith("tunnel"):
            # 地下出口的上游跨距大于一格，入口的记录方向朝向供给侧。
            previous = inputs[point][0]
            incoming = direction(previous, point)
            facing = shape = OPPOSITE[incoming] if previous == offset(point, OPPOSITE[incoming]) else incoming + 4
        else:
            incoming = direction(inputs[point][0], point)
            facing = direction(point, outputs[point][0])
            shape = facing if facing == incoming else TURN[incoming, facing]
        buildings.append(Record(IDS[kind], *point, facing, shape))
    wires = [Record(IDS["wire"], *point, 0, 0) for point in sorted(layout.wires)]
    return buildings, wires


def string(value):
    encoded = value.encode("utf-8")
    return pack("<i", len(encoded)) + encoded


def encode_record(r):
    power, slots = MACHINE_SETTINGS.get(r.id, (0, 0))
    result = (pack("<iiiiiiiiiiBBii", r.id, r.x, r.y, r.direction, r.shape, -power, -power, 0, 0,
                   100 if power else -1, 1, 0, -1, -1) + string("pNull") + string("wNull") + pack("<i", -1))
    if r.id == IDS["inlet"]:
        result += pack("<i", 56) + bytes(57 * 4) + string("自动进货箱") + bytes(8)
    elif slots:
        result += pack("<i", slots) + bytes(slots * 4) + pack("<8i", 1, 0, 1, 0, 0, r.recipe, 0, 0)
    elif 20 <= r.id <= 24 or 30 <= r.id <= 34:
        result += pack("<ii", r.id % 10, 0) + (bytes(4) if r.id >= 30 else b"")
    else:
        result += bytes(8 if r.id == IDS["wire"] else 4)
    if len(result) != RECORD_BYTES[r.id]:
        raise ValueError("蓝图组件编码长度错误")
    return result


def decode(text):
    """回读当前 Steam 样本涉及的建筑、电线和空水管列表。"""
    stream = BytesIO(b64decode(text.strip(), validate=True))

    def integers(count):
        return unpack(f"<{count}i", stream.read(count * 4))

    def read_string():
        return stream.read(integers(1)[0]).decode("utf-8")

    def read_records():
        out = []
        for _ in range(integers(1)[0]):
            prefix = integers(5)
            size = RECORD_BYTES[prefix[0]]
            tail = stream.read(size - 20)
            if len(tail) != size - 20:
                raise ValueError("蓝图组件数据不完整")
            recipe = unpack("<i", tail[-12:-8])[0] if prefix[0] in MACHINE_SETTINGS else -1
            out.append(Record(*prefix, recipe))
        return out

    key, name, description = (read_string() for _ in range(3))
    size = integers(2)
    buildings, wires = read_records(), read_records()
    if integers(1)[0] or stream.read(1):
        raise ValueError("蓝图包含当前格式之外的数据")
    return key, name, description, size, buildings, wires


def export(problem, layout, name, description):
    buildings, wires = records_for(problem, layout)
    records = b"".join(pack("<i", len(group)) + b"".join(encode_record(r) for r in group)
                       for group in (buildings, wires, ()))
    size = layout.width, layout.height
    key = f"KEY-{uuid5(NAMESPACE_URL, name + str(size) + records.hex())}"
    prefix = string(key) + string(name) + string(description) + pack("<ii", *size)
    text = b64encode(prefix + records).decode("ascii")
    if decode(text) != (key, name, description, size, buildings, wires):
        raise ValueError("蓝图回读与布局不一致")
    return text, buildings, wires
