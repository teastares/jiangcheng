import json
import tempfile
import unittest
from base64 import b64decode
from dataclasses import replace
from pathlib import Path
from struct import pack
from types import SimpleNamespace
from unittest.mock import patch

from solver.blueprint import Record, decode, encode_record, string
from solver.layout import solve
from solver.model import Infeasible
from solver.save import SaveError, parse, read_save
from solver.workshop import Workshop, write_workshop_report


def ints(*values):
    return pack(f"<{len(values)}i", *values)


def slots(entries):
    return ints(len(entries)) + b"".join(ints(0) if entry is None else ints(1, *entry) for entry in entries)


def fixture(buildings=None, nodes=(15,), inventory=True, optional_map_materials=False, resource_allocations=(200, 400), recipes=(1, 9)):
    """生成匿名测试数据，覆盖有工坊、库存、解锁和资源点的 117 布局。"""
    if buildings is None:
        buildings = (2, 4, 20, 22, 30, 32, 40, 41, 50, 250)
    actor = ints(*([0] * 42))
    map_area = bytes(260 * 200 * 4)
    # 实际存档允许大地图建筑只有费用，建材列表写为 -1。
    map_count = 177 if optional_map_materials else 1
    map_object = ints(map_count) + b"".join(
        ints(ident, ident, ident - 33, 5, 1, 1, 0, 0) + b"\x01" + ints(0) + b"\x00"
        for ident in range(33, 33 + map_count)
    )
    map_dict = ints(1, 33, 33, 0, 141, 0, 0, 0)
    if optional_map_materials:
        map_dict += string("synthetic-map-building") + string("") * 2 + ints(1, 400, -1)
        map_dict += string("finish()") + ints(2, 1) + string("map.png") + string("测试建筑") + b"\x01"
    else:
        map_dict += string("") * 3 + ints(0)
    world = map_area + map_object + map_dict + map_area + ints(33, 10, 10)
    # 同一工坊里包含分流器、制造机、进货箱与电线。
    factory = ints(1, 700) + string("测试工坊") + ints(0, 1) + string("8,3") + ints(1, 8, 3)
    factory += ints(3)
    for n in (Record(4, 0, 0, 2, 2), Record(40, 3, 0, 2, 2), Record(50, 5, 0, 1, 0, 9)):
        factory += string(f"{n.x},{n.y}") + encode_record(n)
    factory += ints(0, 0, 0, 1) + string("5,0") + encode_record(Record(250, 5, 0, 0, 0))
    factory += ints(0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0) + pack("<q", 0)
    ores = ints(2, 21, 21, 1, 0, -1, 8, 0, 22, 22, 2, 3, -1, 5, 0)
    bag = [(990, 200), (60, 30), None, (62, 30), (65, 30), (76, 120), (75, 80), (61, 30), (69, 30)] if inventory else []
    records = (ints(117) + string("合成") + string("样本") + string("工坊") + actor + world + factory + ores + ints(64)
               + slots(bag) + slots([(990, 220)] if inventory else []) + bytes(8 + 31 * 4)
               + ints(1, 4, 4, 1, 8) + slots([None, None, (10, 3), None])[4:]
               + bytes(8 * 4) + ints(*([0] * 17)) + ints(1, 32, 1, 100) + ints(-1, -1, -1, -1, -1)
               + ints(len(recipes), *recipes) + ints(len(buildings), *buildings) + ints(4, 10, 20, 30, 35) + ints(7, len(nodes)))
    for node in nodes:
        records += ints(node, node) + b"\x01" + slots([None])
    records += bytes(15 * 4) + ints(1, 8) + slots([(50, 3)]) + ints(0) + bytes(10 * 4)
    records += ints(2, 49, 1, 50, 200, 2) + string("忽略")
    records += ints(1, 500, 15) + b"\x00\x00\x01" + ints(1, 57, -1, 0)
    records += ints(0, 0) + bytes(6 * 4) + ints(0, 0, 0, 0, 0, 0, 0) + bytes(12) + b"\x00"
    timber, stone = resource_allocations
    records += ints(2, 0, 1, 0, 1, 7, timber, 0, 3, 5, 3, 1, 8, stone, 1)
    stock = [(2, 100), (10, 300), (68, 300), (50, 250), (51, 100), (70, 200), (990, 200)] if inventory else []
    records += ints(70) + slots(stock) + ints(0, 0) + bytes(16) + b"\x00" + ints(1, 16, 1, 1, 800, 2)
    return records + bytes(40)


class WorkshopTests(unittest.TestCase):
    def test_optional_map_materials_at_reported_offset(self):
        data = fixture(optional_map_materials=True)
        self.assertEqual(data[215006:215010], ints(-1))
        self.assertEqual(parse(data), parse(fixture()))
        invalid = data[:215006] + ints(-2) + data[215010:]
        with self.assertRaisesRegex(SaveError, "偏移 215006"):
            parse(invalid)

    def test_parse_and_guard_version(self):
        state = parse(fixture())
        self.assertEqual(state.recipes, {1, 9, 600})
        self.assertEqual(state.nodes, {15})
        self.assertEqual(state.technology_levels, {800: 2})
        self.assertEqual(state.technologies, {10, 20, 30, 35})
        self.assertEqual(state, parse(fixture(inventory=False, resource_allocations=(100_000, 100_000))))
        for invalid in (fixture()[:1209], ints(116) + fixture()[4:]):
            with self.assertRaises(SaveError):
                parse(invalid)

    def test_read_save_retries_if_source_changes_during_read(self):
        data = fixture(inventory=False)
        before = SimpleNamespace(st_size=len(data), st_mtime_ns=1)
        after = SimpleNamespace(st_size=len(data), st_mtime_ns=2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "save_synthetic.savns")
            path.write_bytes(data)
            with patch("solver.save.fstat", side_effect=(before, after, after, after)) as stat:
                self.assertEqual(read_save(path).version, 117)
            self.assertEqual(stat.call_count, 4)
            self.assertEqual(path.read_bytes(), data)

    def test_unlock_constraints_and_html_without_inventory(self):
        config = {"target": "石材", "machines": 2, "plot": [24, 16], "power_limit_kw": 20, "objective": ["inlets", "area"]}
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            save = folder / "save_synthetic"
            original = fixture(inventory=False, resource_allocations=(200, 100_000))
            save.write_bytes(original)
            workshop = Workshop(read_save(save))
            self.assertEqual(len(workshop.presets), 77)
            self.assertEqual([b["level"] for b in workshop.catalog("石材", None)["belts"]], [1, 3])
            problem, results, winner = workshop.run({**config, "belt_levels": [3]}, 0)
            layout = results[winner][0]
            self.assertEqual(layout.statistics["buildings"]["inlet"], 1)
            self.assertIn("belt3", layout.transport.values())
            report = write_workshop_report(folder / "result", problem, results, winner)
            html = report.read_text(encoding="utf-8")
            text = report.with_suffix(".txt").read_text(encoding="ascii")
            self.assertIn("已按存档核对所用配方、设备和物流等级的解锁状态", html)
            header = html.split("</header>", 1)[0]
            self.assertIn(f'所需建造胶囊 <strong>{layout.statistics["components"]}</strong> 个', header)
            self.assertIn("<h2>建材</h2>", html)
            self.assertIn("<td>石头</td><td>640 件/日</td>", html)
            self.assertNotIn("库存", html)
            self.assertIn(report.with_suffix(".svg").read_text(encoding="utf-8"), html)
            payload = html.split("data:text/plain;charset=utf-8;base64,", 1)[1].split('"', 1)[0]
            self.assertEqual(b64decode(payload).decode(), text)
            _, _, _, size, buildings, _ = decode(text)
            self.assertEqual(size, (layout.width, layout.height))
            self.assertEqual(len(buildings), layout.statistics["components"])
            self.assertIn("设施方向核对", html)
            self.assertIn("额定输送在途量", html)
            self.assertEqual(save.read_bytes(), original)
            self.assertEqual(json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))["target"], "石材")
        state = parse(fixture(inventory=False))
        with self.assertRaisesRegex(Infeasible, "石材缺少可用的自动生产设备"):
            Workshop(replace(state, buildings=state.buildings - {50})).configure(config)
        without_junctions = Workshop(replace(state, buildings=state.buildings - {40, 41}))
        _, candidates, selected = without_junctions.run({**config, "belt_levels": [3]}, 0)
        self.assertEqual(candidates[selected][0].statistics["buildings"]["inlet"], 2)
        with self.assertRaisesRegex(Infeasible, "蓝图.*还未修复"):
            Workshop(parse(fixture(nodes=()))).configure(config)
        with self.assertRaisesRegex(Infeasible, "未解锁.*5"):
            Workshop(parse(fixture())).configure({**config, "belt_levels": [1, 5]})

    def test_green_crystal_without_supply_quota(self):
        config = {"target": "绿色晶能", "machines": 2}
        state = parse(fixture(buildings=(2, 4, 20, 21, 30, 31, 40, 41, 50, 51, 61, 250),
                              inventory=False, resource_allocations=(100_000, 100_000), recipes=(1, 6, 9, 50, 51, 52, 60, 101)))
        problem = Workshop(state).configure(config)
        self.assertEqual(problem.rate, 12)
        self.assertEqual(problem.raw, {"原木": 128, "石头": 24})
        locked = Workshop(replace(state, recipes=state.recipes - {50}))
        with self.assertRaisesRegex(Infeasible, "木纤维.*配方"):
            locked.configure(config)
        for options in ({"external": ["木纤维"]}, {"manual_daily": {"木纤维": 1}}):
            with self.subTest(options=options):
                problem = locked.configure({**config, **options})
                self.assertEqual(problem.raw, {"原木": 32, "木纤维": 144, "石头": 24})

    def test_optional_layout_limits_and_belt_default(self):
        workshop = Workshop(parse(fixture()))
        config = {"target": "石材", "machines": 2}
        default = workshop.configure(config)
        self.assertEqual(default.rate, 40)
        self.assertEqual(default.plot_limits, (None, None))
        self.assertIsNone(default.power_limit)
        self.assertEqual([b["level"] for b in default.belts], [1, 3])
        override = workshop.configure({**config, "belt_levels": [3]})
        self.assertEqual([b["level"] for b in override.belts], [3])
        for limits in ([10, None], [None, 3]):
            layouts = [layout for layout, _ in solve(workshop.configure({**config, "plot": limits}), 0).values() if layout]
            self.assertTrue(layouts)
            for layout in layouts:
                for value, limit in zip((layout.width, layout.height), limits):
                    if limit is not None:
                        self.assertLessEqual(value, limit)
        self.assertTrue(all(not layout for layout, _ in solve(workshop.configure({**config, "power_limit_kw": 0}), 0).values()))


if __name__ == "__main__":
    unittest.main()
