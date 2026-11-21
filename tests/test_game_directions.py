import unittest
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

from solver.blueprint import IDS, Record, export, record_directions, records_for, verify_record_directions
from solver.geometry import EAST, NORTH, SOUTH, WEST, oriented_geometry
from solver.layout import Layout, Placement, Segment
from solver.workshop import GEOMETRY


class GameDirectionTests(unittest.TestCase):
    def test_east_machine_ports_match_installed_game_anchor_offsets(self):
        # BMachine.rotate 的东向 CDP 坐标按配方顺序登记，受电口为原始定位格。
        expected = {
            "maker": (((-1, 0),), (2, 0)),
            "smelter": (((-1, 0),), (2, 0)),
            "assembler": (((-2, -1), (-2, 0)), (2, -1)),
            "charger": (((-2, -1), (-2, 0)), (2, -1)),
            "synthesizer": (((-2, -2), (-2, -1), (-2, 0)), (2, 0)),
            "forge": (((-2, -2), (-2, -1)), (2, -1)),
            "exchanger": (((-1, -1), (-1, 0)), None),
            "grain_mill": (((-2, -2),), (1, 0)),
        }
        for building, ports in expected.items():
            with self.subTest(building=building):
                size, anchor, inputs, output = GEOMETRY[building]
                geometry = oriented_geometry(building, {"size": size, "anchor": anchor,
                                                       "inputs": inputs, "output": output}, EAST)
                ax, ay = geometry["anchor"]
                incoming = tuple((x - ax - 1, y - ay) for x, y in geometry["inputs"])
                out = geometry["output"]
                outgoing = None if out is None else (out[0] - ax + 1, out[1] - ay)
                self.assertEqual((incoming, outgoing), ports)

    def test_split_uses_installed_game_primary_port_and_branch_order(self):
        # 按安装版 BSplit.rotate 的端口顺序和 GBuildSplit.getDir 核对。
        cases = (
            (0, SOUTH, (SOUTH, WEST, EAST)),
            (1, EAST, (SOUTH, NORTH, EAST)),
            (2, WEST, (SOUTH, NORTH, WEST)),
            (3, NORTH, (NORTH, WEST, EAST)),
        )
        for raw, incoming, outgoing in cases:
            with self.subTest(raw=raw):
                self.assertEqual(record_directions(Record(40, 3, 1, raw, raw)), ((incoming,), outgoing))
        self.assertEqual(record_directions(Record(41, 3, 1, EAST, EAST)),
                         ((SOUTH, EAST, NORTH), (EAST,)))
        self.assertEqual(record_directions(Record(21, 3, 1, NORTH, 6)), ((EAST,), (NORTH,)))
        self.assertEqual(record_directions(Record(32, 3, 1, SOUTH, SOUTH)), ((NORTH,), (NORTH,)))
        self.assertEqual(record_directions(Record(32, 3, 1, NORTH + 4, NORTH + 4)), ((NORTH,), (NORTH,)))

    def test_export_accepts_correct_split_and_rejects_original_reversed_main_port(self):
        problem = SimpleNamespace(
            belts=[{"building": "belt1", "capacity": 60}],
            buildings={"inlet": {"size": (2, 1), "anchor": (0, 0), "output": 0},
                       "outlet": {"size": (1, 1), "anchor": (0, 0), "inputs": (0,)}},
        )
        item, rate = "原木", Fraction(20)
        placements = [
            Placement("inlet", 0, 1, 2, 1, (0, 1), item, rate * 2),
            Placement("outlet", 2, 0, 1, 1, (2, 0), item, rate, direction=NORTH),
            Placement("outlet", 3, 1, 1, 1, (3, 1), item, rate, direction=EAST),
        ]
        split = (2, 1)
        segments = [
            Segment((1, 1), split, item, rate * 2, 0),
            Segment(split, (2, 0), item, rate, 0),
            Segment(split, (3, 1), item, rate, 0),
        ]
        layout = Layout(4, 2, placements, {split: "split"}, segments, set(), (0, 0), [])
        _, records, wires = export(problem, layout, "分流核验", "")
        split_record = next(record for record in records if record.id == IDS["split"])
        self.assertEqual(split_record, Record(IDS["split"], *split, WEST, WEST))
        broken = [replace(record, direction=EAST, shape=EAST) if record == split_record else record for record in records]
        with self.assertRaisesRegex(ValueError, "入口方向"):
            verify_record_directions(problem, layout, broken, wires)
        self.assertEqual(records_for(problem, layout), (records, wires))


if __name__ == "__main__":
    unittest.main()
