"""回读旧双绿瓶蓝图，按现有模型静态配平并进行空线仿真。

在项目根目录运行 python -m tools.benchmark_legacy_green。
"""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree

from scipy.optimize import linprog

from solver.blueprint import IDS, TURN, Record, decode
from solver.geometry import MACHINE_DIRECTIONS, OFFSETS, OPPOSITE, offset, placement_ports
from solver.layout import Layout, Placement, Segment, assess
from solver.model import DAY, Problem
from solver.report import data_for
from solver.simulate import simulate


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "blueprints" / "green-crystal-raw-12pm"
INLETS = {(0, 9): ("原木", 96), (41, 9): ("原木", 32), (35, 13): ("石头", 24)}
MACHINE_HEADING = {value: key for key, value in MACHINE_DIRECTIONS.items()}
BELT_INPUT = {shape: facing for (facing, _), shape in TURN.items()}
BUILDING_KEYS = {value: key for key, value in IDS.items()}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_footprints(buildings, wires):
    svg = ElementTree.parse(SAMPLE.with_suffix(".svg"))
    drawings = {}
    for group in svg.iterfind(".//{*}g[@data-id]"):
        record = Record(*(int(group.attrib[f"data-{key}"]) for key in ("id", "x", "y", "dir", "shape", "recipe")))
        require(record not in drawings, "旧图含有重复组件记录")
        rectangle = group.find("{*}rect")
        if rectangle is None:
            drawings[record] = (record.x, record.y, 1, 1)
        else:
            x, y, w, h = (int(rectangle.attrib[key]) for key in ("x", "y", "width", "height"))
            # 实物画在 27 像素网格内，矩形四周各留 3 像素。
            drawings[record] = (x // 27, y // 27, (w + 6) // 27, (h + 6) // 27)
    require(drawings.keys() == set(buildings + wires), "旧图与导入文本的组件记录不一致")
    return drawings


def read_topology(problem):
    _, _, _, (width, height), records, wires = decode(SAMPLE.with_suffix(".txt").read_text(encoding="ascii"))
    footprints = load_footprints(records, wires)
    items = {recipe["id"]: item for item, recipe in problem.recipes.items()}
    occupied, transport, placements, ports = set(), {}, [], {}
    accepts, sends, entries, exits = {}, {}, {}, {}
    for r in records:
        point, key = (r.x, r.y), BUILDING_KEYS[r.id]
        if key.startswith(("belt", "tunnel")) or key in ("split", "merge"):
            x, y, w, h = r.x, r.y, 1, 1
            transport[point] = key
            if key == "split":
                accepts[point] = (r.direction,)
                sends[point] = tuple(d for d in OFFSETS if d != OPPOSITE[r.direction])
            elif key == "merge":
                accepts[point] = tuple(d for d in OFFSETS if d != OPPOSITE[r.direction])
                sends[point] = (r.direction,)
            elif key.startswith("tunnel"):
                if r.direction < 4:
                    heading = OPPOSITE[r.direction]
                    accepts[point] = (heading,)
                    entries[point] = (key, heading)
                else:
                    heading = r.direction - 4
                    exits[point] = (key, heading)
                    sends[point] = (heading,)
            else:
                accepts[point] = (BELT_INPUT.get(r.shape, r.direction),)
                sends[point] = (r.direction,)
        else:
            x, y, w, h = footprints[r]
            if key == "inlet":
                item, rate = INLETS[point]
                heading = r.direction
            else:
                item, rate = items[r.recipe], 0
                heading = MACHINE_HEADING[r.direction]
                require(problem.recipes[item]["machine"] == key, f"{item}机型配置不一致")
            placement = Placement(key, x, y, w, h, point, item, Fraction(rate), len(placements), direction=heading)
            placement_inputs, output = placement_ports(problem, placement)
            for port in placement_inputs:
                accepts[port] = (heading,)
            if output is not None:
                sends[output] = (heading,)
            placements.append(placement)
            ports[placement] = (placement_inputs, output)
        cells = {(cx, cy) for cx in range(x, x + w) for cy in range(y, y + h)}
        require(not cells & occupied, f"{point}的组件占位冲突")
        occupied.update(cells)

    edges = [(start, end) for start, headings in sends.items() for heading in headings
             if heading in accepts.get(end := offset(start, heading), ())]
    for entry, (key, heading) in entries.items():
        matches = [offset(entry, heading, length) for length in range(2, max(width, height))
                   if exits.get(offset(entry, heading, length)) == (key, heading)]
        require(len(matches) == 1, f"地下带入口 {entry} 的出口无法唯一识别")
        edges.append((entry, matches[0]))
    require({t for _, t in edges} >= exits.keys(), "地下带存在未配对的出口")
    junctions = {point for point, key in transport.items() if key in ("split", "merge")}
    require(not any(a in junctions and b in junctions for a, b in edges), "旧图的分合流组件直接相连")

    wire_cells = {(r.x, r.y) for r in wires}
    power_entry = (28, 19)
    seen, todo = set(), [offset(power_entry, d) for d in OFFSETS]
    while todo:
        point = todo.pop()
        if point in wire_cells and point not in seen:
            seen.add(point)
            todo.extend(offset(point, d) for d in OFFSETS)
    require(wire_cells == seen and all(p.anchor in seen for p in placements if p.building != "inlet"), "电网不连通")
    area = occupied | wire_cells
    require(all(0 <= x < width and 0 <= y < height for x, y in area), "蓝图组件超出边界")
    size = (max(x for x, _ in area) - min(x for x, _ in area) + 1,
            max(y for _, y in area) - min(y for _, y in area) + 1)
    layout = Layout(width, height, placements, transport, [], wire_cells, power_entry,
                    [(p.item, p.rate, p) for p in placements if p.building != "inlet"])
    spans = [abs(x1 - x2) + abs(y1 - y2) for (x1, y1), (x2, y2) in edges if (x1, y1) in entries]
    return layout, ports, edges, size, sorted(spans)


def check_flows(problem, layout, ports, edges):
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for i, (start, end) in enumerate(edges):
        incoming[end].append(i)
        outgoing[start].append(i)

    items = {}
    expected = {}
    for p, (inputs, output) in ports.items():
        if output is not None:
            require(len(outgoing[output]) == 1, f"{p.item} {p.anchor} 的输出无唯一连接")
            todo = outgoing[output].copy()
            while todo:
                index = todo.pop()
                if index in items:
                    require(items[index] == p.item, f"{edges[index]}混入了两种物料")
                    continue
                items[index] = p.item
                todo.extend(outgoing[edges[index][1]])
        if p.building != "inlet":
            expected.update(zip(inputs, problem.recipes[p.item]["inputs"]))
    require(len(items) == len(edges), "存在未连到任何供料口或设备输出的物流段")
    for point, item in expected.items():
        supplied = [items[i] for i in incoming[point]]
        require(not outgoing[point] and supplied == [item], f"设备端口 {point} 需要 {item}，实际物流连接为 {supplied}")

    terminals = [p for p in layout.placements if p.item == problem.target]
    require(len(terminals) == problem.count, "末端台数不等于基准目标")
    supplied = Counter()
    for p in layout.placements:
        if p.building == "inlet":
            supplied[p.item] += p.rate
    require(dict(supplied) == problem.raw, "三口用量与反推需求不一致")

    bounds = [(0, None)] * len(edges)
    eq, eq_right, ub, ub_right = [], [], [], []
    machines = [p for p in layout.placements if p.building != "inlet"]
    index_for_machine = {p: i for i, p in enumerate(machines, len(edges))}
    for p in machines:
        rate = problem.rate / problem.count
        bounds.append((float(rate), float(rate)) if p in terminals else (0, float(problem.machine_capacity(p.item))))

    def constraint(terms, rhs, equality=True):
        row = [0.0] * len(bounds)
        for idx, value in terms:
            row[idx] += float(value)
        (eq if equality else ub).append(row)
        (eq_right if equality else ub_right).append(float(rhs))

    capacities = {belt[key]: belt["capacity"] for belt in problem.belts for key in ("building", "underground")}
    for point, key in layout.transport.items():
        ins, outs = incoming[point], outgoing[point]
        degrees = {"split": ((1, 2), (1, 3)), "merge": ((2, 1), (3, 1))}.get(key, ((1, 1),))
        require((len(ins), len(outs)) in degrees, f"{point} 的{key}端口数错误 {(len(ins), len(outs))}")
        require(len({items[i] for i in ins + outs}) == 1, f"{point}的物流组件混入了两种物料")
        ins_total = [(i, 1) for i in ins]
        constraint(ins_total + [(i, -1) for i in outs], 0)
        constraint(ins_total, capacities.get(key, problem.junction_capacity), equality=False)
    for p, (inputs, output) in ports.items():
        if p.building == "inlet":
            require(p.rate <= problem.box_capacity, f"{p.item}进货口超出供料速率")
            constraint([(outgoing[output][0], 1)], p.rate)
            continue
        idx = index_for_machine[p]
        recipe = problem.recipes[p.item]
        for point, amount in zip(inputs, recipe["inputs"].values()):
            constraint([(incoming[point][0], 1), (idx, -Fraction(amount, recipe["quantity"]))], 0)
        if output is not None:
            constraint([(outgoing[output][0], 1), (idx, -1)], 0)

    result = linprog([0] * len(bounds), A_eq=eq, b_eq=eq_right, A_ub=ub, b_ub=ub_right, bounds=bounds, method="highs")
    require(result.success, f"旧图在统一的物流及设备容量下无法配平：{result.message}")
    topology = {}
    for index, (start, end) in enumerate(edges):
        if index in topology:
            continue
        connected = [index]
        while connected:
            other = connected.pop()
            if other in topology:
                continue
            topology[other] = index
            for point in edges[other]:
                if point in layout.transport:
                    connected.extend(incoming[point])
                    connected.extend(outgoing[point])
    layout.segments = [Segment(start, end, items[i], Fraction(float(result.x[i])).limit_denominator(), topology[i])
                       for i, (start, end) in enumerate(edges)]
    rated = {p: replace(p, rate=Fraction(float(result.x[idx])).limit_denominator())
             for p, idx in index_for_machine.items()}
    layout.placements = [rated.get(p, p) for p in layout.placements]
    layout.machines = [(p.item, p.rate, p) for p in layout.placements if p.building != "inlet"]


def reference_layout(problem):
    """返回实际占地的旧双绿瓶布局，并将外接电位置并入线网。"""
    layout, ports, edges, size, _ = read_topology(problem)
    check_flows(problem, layout, ports, edges)
    return crop_reference(layout, size)


def crop_reference(layout, size):
    return replace(layout, width=size[0], height=size[1], wires=layout.wires | {layout.power_entry},
                   profile="手工基准", extended=True)


def main():
    parser = argparse.ArgumentParser(description="复算旧双绿瓶蓝图并导出参考布局。")
    parser.add_argument("--reference-json", type=Path, help="导出实际占地和稳态流量的参考布局 JSON")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "solver" / "catalog.json").read_text(encoding="utf-8"))
    problem = Problem(catalog, {"target": "绿色晶能", "machines": 2, "external": ["原木", "石头"]})
    layout, ports, edges, size, spans = read_topology(problem)
    check_flows(problem, layout, ports, edges)
    assess(problem, layout)
    stats = layout.statistics
    print(json.dumps({"蓝图边界": [layout.width, layout.height], "占位边界": size, "地下带跨度": spans,
                      "加工设备": stats["machines"], "非电线建筑": stats["components"],
                      "物流格": stats["transport"], "电线格": stats["wires"],
                      "建材总量": sum(stats["materials"].values()), "功耗": stats["power"],
                      "进货口日配额": {str(point): [item, rate * DAY] for point, (item, rate) in INLETS.items()},
                      "仿真": simulate(problem, layout)}, ensure_ascii=False, indent=2))
    if args.reference_json is not None:
        reference = crop_reference(layout, size)
        assess(problem, reference)
        data = data_for(problem, reference, "shared")
        for row, placement in zip(data["placements"], reference.placements):
            row["direction"] = placement.direction
        args.reference_json.write_text(json.dumps(data, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
