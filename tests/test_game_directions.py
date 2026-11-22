import json
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from solver.blueprint import IDS, Record, export, record_directions, records_for, verify_record_directions
from solver.compact import remove_cut
from solver.geometry import EAST, NORTH, SOUTH, WEST, assigned_ports, direct_logistics_allowed, oriented_geometry, placement_ports
from solver.layout import Layout, Placement, Segment, compact_routes, power_grid, verify
from solver.model import Infeasible, Problem
from solver.reroute import rerouted_layouts
from solver.workshop import GEOMETRY


class GameDirectionTests(unittest.TestCase):
    @staticmethod
    def stone_problem(count):
        path = Path(__file__).resolve().parents[1] / "solver" / "catalog.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        return Problem(catalog, {"target": "石材", "machines": count, "external": ["石头"], "rate": 10 * count})

    def test_direct_logistics_classes_follow_installed_game(self):
        allowed = (("inlet", "split"), ("maker", "merge"), ("split", "maker"), ("merge", "assembler"),
                   ("inlet", "belt1"), ("belt1", "maker"), ("tunnel1", "maker"), ("maker", "outlet"))
        prohibited = (("inlet", "maker"), ("maker", "assembler"), ("split", "split"), ("split", "merge"),
                      ("merge", "split"), ("merge", "merge"))
        for accepted, pairs in ((True, allowed), (False, prohibited)):
            for source, sink in pairs:
                with self.subTest(source=source, sink=sink):
                    self.assertEqual(direct_logistics_allowed(source, sink), accepted)

    def test_box_to_machine_requires_the_last_belt(self):
        problem = self.stone_problem(1)
        rate = problem.rate
        inlet = Placement("inlet", 0, 1, 2, 1, (0, 1), "石头", rate, 0)
        maker = Placement("maker", 3, 1, 2, 1, (3, 1), "石材", rate, 1)
        outlet = Placement("outlet", 5, 1, 1, 1, (5, 1), "石材", rate, 2)
        segments = [Segment((1, 1), (2, 1), "石头", rate, 0), Segment((2, 1), (3, 1), "石头", rate, 0),
                    Segment((4, 1), (5, 1), "石材", rate, 1)]
        layout = Layout(6, 3, [inlet, maker, outlet], {(2, 1): "belt1"}, segments,
                        {(3, 0), maker.anchor}, (3, 0), [(maker.item, rate, maker)])
        verify(problem, layout)
        self.assertIsNone(remove_cut(layout, 0))
        self.assertEqual(compact_routes(layout, (0,)).transport, layout.transport)

        closer = replace(maker, x=2, anchor=(2, 1))
        placements = [inlet, closer, replace(outlet, x=4, anchor=(4, 1))]
        invalid = Layout(5, 3, placements, {}, [Segment((1, 1), (2, 1), "石头", rate, 0),
                                               Segment((3, 1), (4, 1), "石材", rate, 1)],
                         {(2, 0), closer.anchor}, (2, 0), [(closer.item, rate, closer)])
        with self.assertRaisesRegex(Infeasible, "需要传送带"):
            verify(problem, invalid)
        self.assertFalse(list(rerouted_layouts(problem, layout, placements=placements, max_routes=0)))

    def test_split_to_machine_can_remove_the_last_belt(self):
        problem = self.stone_problem(2)
        rate = problem.rate / 2
        placements = [Placement("inlet", 3, 3, 1, 2, (3, 4), "石头", problem.rate, 0, NORTH),
                      Placement("maker", 5, 2, 2, 1, (5, 2), "石材", rate, 1),
                      Placement("maker", 1, 2, 2, 1, (1, 2), "石材", rate, 2, WEST),
                      Placement("outlet", 7, 2, 1, 1, (7, 2), "石材", rate, 3),
                      Placement("outlet", 0, 2, 1, 1, (0, 2), "石材", rate, 4, WEST)]
        split, belt = (3, 2), (4, 2)
        segments = [Segment((3, 3), split, "石头", problem.rate, 0), Segment(split, belt, "石头", rate, 0),
                    Segment(belt, (5, 2), "石头", rate, 0), Segment(split, (2, 2), "石头", rate, 0),
                    Segment((6, 2), (7, 2), "石材", rate, 1), Segment((1, 2), (0, 2), "石材", rate, 2)]
        machines = [(p.item, p.rate, p) for p in placements[1:3]]
        wires, entry = power_grid([p.anchor for _, _, p in machines], 8, 5)
        layout = Layout(8, 5, placements, {split: "split", belt: "belt1"}, segments, wires, entry, machines)
        verify(problem, layout)
        cut = remove_cut(layout, 0)
        self.assertIsNotNone(cut)
        routed = next(rerouted_layouts(problem, layout, placements=cut.placements, max_routes=0))
        for candidate in (cut, compact_routes(layout, (0,)), routed):
            with self.subTest(width=candidate.width, profile=candidate.profile):
                self.assertEqual(candidate.transport, {split: "split"})
                self.assertIn(Segment(split, belt, "石头", rate, 0), candidate.segments)
                wires, entry = power_grid([p.anchor for _, _, p in candidate.machines], candidate.width, candidate.height)
                verify(problem, replace(candidate, wires=wires, power_entry=entry))

    def test_north_facing_makers_require_an_empty_column_between_them(self):
        problem = self.stone_problem(2)
        rate = problem.rate / 2
        inlets = [Placement("inlet", 0, 5, 2, 1, (0, 5), "石头", rate, 0),
                  Placement("inlet", 6, 5, 2, 1, (6, 5), "石头", rate, 1, WEST)]

        def layout(right):
            columns = (2, right)
            makers = [Placement("maker", x, 1, 1, 2, (x, 2), "石材", rate, i + 2, NORTH)
                      for i, x in enumerate(columns)]
            outlets = [Placement("outlet", x, 0, 1, 1, (x, 0), "石材", rate, i + 4, NORTH)
                       for i, x in enumerate(columns)]
            raw_paths = [((1, 5), (2, 5), (2, 4), (2, 3), (2, 2)),
                         (*((x, 5) for x in range(6, right - 1, -1)), (right, 4), (right, 3), (right, 2))]
            transport = {point: "belt1" for path in raw_paths for point in path[1:-1]}
            segments = [Segment(a, b, "石头", rate, i) for i, path in enumerate(raw_paths) for a, b in zip(path, path[1:])]
            segments.extend(Segment((x, 1), (x, 0), "石材", rate, i + 2) for i, x in enumerate(columns))
            wires, entry = power_grid([p.anchor for p in makers], 8, 7)
            return Layout(8, 7, [*inlets, *makers, *outlets], transport, segments, wires, entry,
                          [(p.item, p.rate, p) for p in makers])

        with self.assertRaisesRegex(Infeasible, "实际机体碰撞"):
            verify(problem, layout(3))
        verify(problem, layout(4))

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

    def test_rotated_steam_geometry_keeps_game_anchor_and_nonrigid_ports(self):
        cases = (
            ("inlet", SOUTH, (1, 2), (0, 1), (), (0, 1)),
            ("maker", SOUTH, (1, 2), (0, 1), ((0, 0),), (0, 1)),
            ("smelter", NORTH, (1, 2), (0, 1), ((0, 1),), (0, 0)),
            ("assembler", WEST, (3, 2), (1, 1), ((2, 0), (2, 1)), (0, 1)),
            ("assembler", SOUTH, (2, 3), (0, 2), ((0, 0), (1, 0)), (1, 2)),
            ("charger", NORTH, (2, 3), (0, 2), ((0, 2), (1, 2)), (0, 0)),
            ("synthesizer", NORTH, (3, 3), (1, 2), ((1, 2), (0, 2), (2, 2)), (2, 0)),
            ("synthesizer", SOUTH, (3, 3), (1, 2), ((1, 0), (0, 0), (2, 0)), (0, 2)),
            ("forge", NORTH, (2, 3), (1, 2), ((1, 2), (0, 2)), (1, 0)),
            ("forge", SOUTH, (2, 3), (1, 2), ((1, 0), (0, 0)), (0, 2)),
            ("exchanger", WEST, (3, 2), (0, 1), ((2, 0), (2, 1)), None),
            ("exchanger", NORTH, (2, 2), (0, 1), ((0, 1), (1, 1)), None),
            ("exchanger", SOUTH, (2, 3), (0, 2), ((0, 0), (1, 0)), None),
            ("grain_mill", WEST, (2, 3), (0, 2), ((1, 2),), (0, 0)),
            ("grain_mill", NORTH, (3, 2), (1, 1), ((0, 1),), (2, 0)),
            ("grain_mill", SOUTH, (3, 2), (1, 1), ((2, 0),), (0, 1)),
        )
        for building, facing, size, anchor, inputs, output in cases:
            with self.subTest(building=building, facing=facing):
                definition = dict(zip(("size", "anchor", "inputs", "output"), GEOMETRY[building]))
                self.assertEqual(oriented_geometry(building, definition, facing),
                                 {"size": size, "anchor": anchor, "inputs": inputs, "output": output})
        custom = {"size": (4, 2), "anchor": (1, 1), "inputs": (0, 1), "output": 0}
        self.assertEqual(oriented_geometry("assembler", custom, NORTH),
                         {"size": (2, 4), "anchor": (1, 2), "inputs": ((0, 3), (1, 3)), "output": (0, 0)})

    def test_steam_machine_assigns_recipe_slots_by_material(self):
        definition = dict(zip(("size", "anchor", "inputs", "output"), GEOMETRY["synthesizer"]))
        problem = SimpleNamespace(catalog={"blueprint_format": "steam-25121460"}, buildings={"synthesizer": definition},
                                  recipes={"成品": {"inputs": {"物料甲": 1, "物料乙": 2}}})
        placement = Placement("synthesizer", 5, 6, 3, 3, (6, 8), "成品", direction=NORTH)
        first, second, third = (6, 8), (5, 8), (7, 8)
        incoming = {second: [Segment((5, 9), second, "物料乙", Fraction(2), 0)],
                    third: [Segment((7, 9), third, "物料甲", Fraction(1), 1)]}
        self.assertEqual(placement_ports(problem, placement, all_inputs=True), ((first, second, third), (7, 6)))
        self.assertEqual(assigned_ports(problem, placement, incoming), ((third, second), (7, 6)))
        self.assertEqual(assigned_ports(problem, placement, {second: incoming[second]}), ((first, second), (7, 6)))
        problem.catalog = {}
        self.assertEqual(assigned_ports(problem, placement, incoming), ((first, second), (7, 6)))

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
