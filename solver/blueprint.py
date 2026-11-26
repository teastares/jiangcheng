from base64 import b64decode, b64encode
from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
from struct import pack, unpack
from uuid import NAMESPACE_URL, uuid5

from .device_catalog import BASIC_SPLITTER, LARGE_INLET, LARGE_INLET_OUTPUT_OFFSETS, SMART_SPLITTER
from .geometry import (EAST, INLET_KINDS, NORTH, SOUTH, WEST, MACHINE_DIRECTIONS, OPPOSITE,
                       direction, inlet_output_ports, offset, placement_ports)
from .model import belt_options


FORMAT = "steam-25121460"
IDS = {"inlet": 4, "inlet_large": LARGE_INLET, "split": BASIC_SPLITTER, "merge": 41, "maker": 50, "assembler": 51, "synthesizer": 52,
       "smelter": 55, "forge": 56, "charger": 60, "exchanger": 61, "grain_mill": 62, "wire": 250}
IDS.update({f"belt{tier}": 19 + tier for tier in range(1, 6)})
IDS.update({f"tunnel{tier}": 29 + tier for tier in range(1, 6)})
MACHINE_SETTINGS = {50: (4, 1), 51: (18, 2), 52: (64, 3), 55: (5, 1), 56: (16, 2),
                    60: (20, 2), 61: (18, 2), 62: (8, 1)}
INLET_SETTINGS = {4: (56, "自动进货箱"), LARGE_INLET: (120, "大型进货箱")}
RECORD_BYTES = {4: 331, LARGE_INLET: 587, BASIC_SPLITTER: 76, SMART_SPLITTER: 89, 41: 76, 250: 80, **{i: 80 for i in range(20, 25)},
                **{i: 84 for i in range(30, 35)}, **{i: 108 + 4 * settings[1] for i, settings in MACHINE_SETTINGS.items()}}
TURN = {(EAST, SOUTH): 4, (NORTH, WEST): 5, (EAST, NORTH): 6, (SOUTH, WEST): 7,
        (SOUTH, EAST): 8, (WEST, NORTH): 9, (NORTH, EAST): 10, (WEST, SOUTH): 11}
TURN_PORTS = {shape: pair for pair, shape in TURN.items()}
MACHINE_HEADINGS = {value: heading for heading, value in MACHINE_DIRECTIONS.items()}
SPLIT_OUTPUTS = {SOUTH: (SOUTH, WEST, EAST), WEST: (SOUTH, NORTH, WEST),
                 EAST: (SOUTH, NORTH, EAST), NORTH: (NORTH, WEST, EAST)}


@dataclass(frozen=True)
class Record:
    id: int
    x: int
    y: int
    direction: int
    shape: int
    recipe: int = -1


def blueprint_id(problem, kind):
    """保留布局的设施功能，按求解所选型号写入安装版建筑 ID。"""
    return problem.buildings.get(kind, {}).get("blueprint_id", IDS[kind])


def is_split_id(ident):
    return ident in (BASIC_SPLITTER, SMART_SPLITTER)


def split_direction(facing):
    """分流器东西朝向在游戏记录中的编号，与实际行进方向可逆转换。"""
    return {EAST: WEST, WEST: EAST}.get(facing, facing)


def tunnel_directions(start, end):
    """给出从 start 输送到 end 的一对原始地下带朝向。"""
    heading = direction(start, end)
    return OPPOSITE[heading], heading + 4


def record_directions(record):
    """按安装版端口规则回读一条建筑记录的输入和输出行进方向。"""
    kind, raw, shape = record.id, record.direction, record.shape
    if is_split_id(kind):
        incoming = split_direction(raw)
        return (incoming,), SPLIT_OUTPUTS[incoming]
    if kind == IDS["merge"]:
        return tuple(d for d in OPPOSITE if d != OPPOSITE[raw]), (raw,)
    if 20 <= kind <= 24:
        incoming = TURN_PORTS.get(shape, (shape, shape))[0]
        outgoing = TURN_PORTS.get(raw, (raw, raw))[1]
        return (incoming,), (outgoing,)
    if 30 <= kind <= 34:
        incoming = OPPOSITE[shape] if shape < 4 else shape - 4
        outgoing = OPPOSITE[raw] if raw < 4 else raw - 4
        return (incoming,), (outgoing,)
    if kind in INLET_SETTINGS:
        return (), (raw,)
    if kind in MACHINE_SETTINGS:
        heading = MACHINE_HEADINGS[raw]
        return (heading,), () if kind == IDS["exchanger"] else (heading,)
    return (), ()


def record_facing(record):
    """返回直带、设备和分合流主口的实际行进方向。"""
    incoming, outgoing = record_directions(record)
    if is_split_id(record.id):
        return incoming[0]
    return next(iter(outgoing or incoming), None)


def records_for(problem, layout):
    inputs, outputs = defaultdict(list), defaultdict(list)
    for segment in layout.segments:
        inputs[segment.end].append(segment.start)
        outputs[segment.start].append(segment.end)
    buildings = []
    for p in layout.placements:
        if p.building in INLET_KINDS:
            buildings.append(Record(blueprint_id(problem, p.building), *p.anchor, p.direction, p.direction))
        elif p.building == "outlet":
            kind = belt_options(problem, p.rate)[0]["building"]
            buildings.append(Record(blueprint_id(problem, kind), *p.anchor, p.direction, p.direction))
        else:
            buildings.append(Record(blueprint_id(problem, p.building), *p.anchor, MACHINE_DIRECTIONS[p.direction], 0,
                                    problem.recipes[p.item]["id"]))

    for point, kind in sorted(layout.transport.items()):
        if kind == "split":
            # BSplit.rotate 的东西 raw dir 与传送带编号相反。
            facing = shape = split_direction(direction(inputs[point][0], point))
        elif kind == "merge":
            facing = shape = direction(point, outputs[point][0])
        elif kind.startswith("tunnel"):
            # 地下出口的上游跨距大于一格，入口的记录方向朝向供给侧。
            previous = inputs[point][0]
            incoming = direction(previous, point)
            if previous == offset(point, OPPOSITE[incoming]):
                facing = shape = tunnel_directions(point, outputs[point][0])[0]
            else:
                facing = shape = tunnel_directions(previous, point)[1]
        else:
            incoming = direction(inputs[point][0], point)
            facing = direction(point, outputs[point][0])
            shape = facing if facing == incoming else TURN[incoming, facing]
        buildings.append(Record(blueprint_id(problem, kind), *point, facing, shape))
    wires = [Record(IDS["wire"], *point, 0, 0) for point in sorted(layout.wires)]
    return buildings, wires


def verify_record_directions(problem, layout, records, wires):
    by_position = {(r.x, r.y): r for r in records}
    positions = {p.anchor for p in layout.placements} | layout.transport.keys()
    if positions != by_position.keys() or len(by_position) != len(records):
        raise ValueError("导入建筑数量或定位格与布局不符")

    incoming, outgoing, tunnel_entries = defaultdict(list), defaultdict(list), set()
    for segment in layout.segments:
        heading = direction(segment.start, segment.end)
        outgoing[segment.start].append(heading)
        incoming[segment.end].append(heading)
        if segment.end != offset(segment.start, heading):
            tunnel_entries.add(segment.start)

    def check_ports(record, input_points, output_points):
        accepted, emitted = record_directions(record)
        if any(d not in accepted for point in input_points for d in incoming[point]):
            raise ValueError(f"导入建筑 {record.x},{record.y} 的入口方向与布局不符")
        if any(d not in emitted for point in output_points for d in outgoing[point]):
            raise ValueError(f"导入建筑 {record.x},{record.y} 的出口方向与布局不符")

    for p in layout.placements:
        record = by_position.get(p.anchor)
        kind = belt_options(problem, p.rate)[0]["building"] if p.building == "outlet" else p.building
        if record is None or record.id != blueprint_id(problem, kind) or record_facing(record) != p.direction:
            raise ValueError(f"导入设备 {p.anchor} 的型号或朝向与布局不符")
        inputs, output = placement_ports(problem, p, all_inputs=True)
        if p.building in INLET_KINDS:
            output_points = inlet_output_ports(problem, p)
        else:
            output_points = (p.anchor,) if p.building == "outlet" else (output,) if output is not None else ()
        if record.id == LARGE_INLET:
            directions = LARGE_INLET_OUTPUT_OFFSETS[record.direction]
            expected = {(record.x + dx, record.y + dy) for dx, dy in directions}
            connected = {offset(point, record.direction) for point in output_points if outgoing[point]}
            if connected != expected:
                raise ValueError(f"大型进货箱 {p.anchor} 没有正确连接两条出带")
        check_ports(record, inputs, output_points)

    for point, kind in layout.transport.items():
        record = by_position.get(point)
        if record is None or record.id != blueprint_id(problem, kind):
            raise ValueError(f"导入物流件 {point} 的型号与布局不符")
        check_ports(record, (point,), (point,))
        if kind.startswith("tunnel"):
            is_entry = point in tunnel_entries
            if is_entry != (record.direction < 4) or is_entry != (record.shape < 4):
                raise ValueError(f"导入地下带 {point} 的进出端类型与布局不符")
    expected_wires = {Record(IDS["wire"], *point, 0, 0) for point in layout.wires}
    if set(wires) != expected_wires or len(wires) != len(expected_wires):
        raise ValueError("导入电线记录与布局不符")


def string(value):
    encoded = value.encode("utf-8")
    return pack("<i", len(encoded)) + encoded


def encode_record(r):
    power, slots = MACHINE_SETTINGS.get(r.id, (0, 0))
    result = (pack("<iiiiiiiiiiBBii", r.id, r.x, r.y, r.direction, r.shape, -power, -power, 0, 0,
                   100 if power else -1, 1, 0, -1, -1) + string("pNull") + string("wNull") + pack("<i", -1))
    if r.id in INLET_SETTINGS:
        count, name = INLET_SETTINGS[r.id]
        result += pack("<i", count) + bytes((count + 1) * 4) + string(name) + bytes(8)
    elif slots:
        result += pack("<i", slots) + bytes(slots * 4) + pack("<8i", 1, 0, 1, 0, 0, r.recipe, 0, 0)
    elif 20 <= r.id <= 24 or 30 <= r.id <= 34:
        result += pack("<ii", r.id % 10, 0) + (bytes(4) if r.id >= 30 else b"")
    elif r.id == SMART_SPLITTER:
        # 无缓存物品，三个出口均未配置过滤，且允许任意物料。
        result += pack("<4i?", 0, 0, 0, 0, True)
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
    decoded = decode(text)
    if decoded != (key, name, description, size, buildings, wires):
        raise ValueError("蓝图回读与布局不一致")
    verify_record_directions(problem, layout, decoded[-2], decoded[-1])
    return text, buildings, wires
