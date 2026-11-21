import json
import unittest
from base64 import b64decode
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from solver.blueprint import decode, encode_record
from solver.layout import bus_row_span, optimize_rows, simulation_indices, solve, verify
from solver.model import Bus, Infeasible, Port, Problem, partition
from solver.report import data_for, render_svg
from solver.simulate import simulate


ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class SolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load(ROOT / "solver/catalog.json")
        cls.cases = load(ROOT / "solver/examples.json")

    def test_green_crystal_supply(self):
        config = self.cases["green-crystal"]
        problem = Problem(self.catalog, config)
        self.assertEqual(problem.rate, 12)
        self.assertEqual(problem.raw, {"原木": 128, "石头": 24})
        with self.assertRaisesRegex(Infeasible, "超过供给上限"):
            Problem(self.catalog, {**config, "daily_supply": {"原木": 2047}})
        with self.assertRaisesRegex(Infeasible, "缺少已解锁配方"):
            Problem(self.catalog, {**config, "enabled_recipes": ["绿色晶能"]})

    def test_bus_capacity_and_direct_port(self):
        ports = [Port(0, 0, Fraction(90)), Port(1, 0, Fraction(30))]
        groups = partition(ports, 120, 120, 60, "balanced", raw=True)
        self.assertEqual([group[2] for group in groups], [90, 30])

    def test_row_model_minimizes_span_between_actual_ports(self):
        buses = [Bus("原料", (Port(0, 1, Fraction(1)),), (Port(1, 0, Fraction(1)),))]
        rows, height_proven = optimize_rows([2, 2], [(0, 1, 2, 2)], 4, 1, buses)
        self.assertEqual(rows, (0, 2))
        self.assertEqual(bus_row_span(buses, rows), 1)
        self.assertFalse(height_proven)

    def test_simulation_budget_includes_distinct_production_plans(self):
        common = SimpleNamespace(machines=[("布", 8, None), ("布", 8, None)], placements=[], baseline=False)
        different = SimpleNamespace(machines=[("布", 16, None)], placements=[], baseline=False)
        original = SimpleNamespace(machines=common.machines, placements=[], baseline=True)
        ranked = [((i,), common) for i in range(8)] + [((8,), different), ((9,), original)]
        self.assertEqual(simulation_indices(ranked, 4), [0, 1, 2, 8, 9])

    def test_power_budget_changes_feasibility(self):
        config = {**self.cases["basic-tools"], "power_limit_kw": 50}
        problem = Problem(self.catalog, config)
        results = solve(problem, 0)
        shared, count = results["shared"]
        self.assertGreater(count, 0)
        self.assertEqual(shared.statistics["power"], 48)
        self.assertIsNone(results["separate"][0])
        verify(problem, shared)
        result = simulate(problem, shared)
        self.assertEqual(result["last_four_per_minute"], 12)
        self.assertLess(result["first_day"], 192)

    def test_svg_and_quota_use_identical_layout(self):
        problem = Problem(self.catalog, self.cases["cloth"])
        layout, _ = solve(problem, 0)["separate"]
        data = data_for(problem, layout, "separate")
        svg = ElementTree.fromstring(render_svg(problem, layout, "separate"))
        points = {(int(p.attrib["data-x"]), int(p.attrib["data-y"]))
                  for p in svg.iter() if p.attrib.get("data-kind") == "inlet"}
        self.assertEqual(points, {tuple(row["position"]) for row in data["inlets"]})
        self.assertEqual(sum(row["per_day"] for row in data["inlets"]), Fraction(2560, 3))
        self.assertEqual(data["theoretical_per_day"], 320)

    def test_blueprint_encoder_matches_existing_sample(self):
        sample = ROOT / "blueprints/green-crystal-raw-6pm.txt"
        text = sample.read_text(encoding="utf-8-sig").strip()
        _, _, _, size, buildings, wires = decode(text)
        self.assertEqual(size, (24, 22))
        self.assertEqual((len(buildings), len(wires)), (116, 64))
        original = b64decode(text)
        groups = (buildings, wires, [])
        record_bytes = b"".join(len(group).to_bytes(4, "little") + b"".join(encode_record(r) for r in group) for group in groups)
        self.assertTrue(original.endswith(record_bytes))


if __name__ == "__main__":
    unittest.main()
