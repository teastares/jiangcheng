import json
import unittest
from fractions import Fraction
from pathlib import Path

from solver.layout import Layout, Segment
from solver.model import Infeasible, Problem
from solver.optimization import faster_belt_layout, production_plan


class OptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = Path(__file__).resolve().parents[1] / "solver"
        cls.catalog = json.loads((data / "catalog.json").read_text(encoding="utf-8"))
        cls.case = json.loads((data / "examples.json").read_text(encoding="utf-8"))["basic-tools"]

    def test_production_capacity_includes_cycle_and_fixed_terminal_count(self):
        problem = Problem(self.catalog, {**self.case, "power_limit_kw": 50})
        shared = production_plan(problem, 2)
        self.assertEqual(shared.status, 0)
        self.assertEqual(shared.mip_gap, 0)
        self.assertEqual(shared.power_kw, 48)
        self.assertEqual(shared.units["初级工具"], 2)
        self.assertEqual(shared.rates, problem.demand)
        with self.assertRaisesRegex(Infeasible, "功率上限"):
            production_plan(problem, 1)

    def test_belt_milp_respects_budget_and_keeps_underground_ends_paired(self):
        problem = Problem(self.catalog, self.case)
        transport = {(1, 0): "belt1", (1, 1): "tunnel1", (3, 1): "tunnel1"}
        full = Fraction(60)
        segments = [Segment((0, 0), (1, 0), "原木", full / 2, 0),
                    Segment((0, 1), (1, 1), "原木", full, 1),
                    Segment((1, 1), (3, 1), "原木", full, 1)]
        layout = Layout(4, 2, [], transport, segments, set(), (0, 0), [])
        candidate = faster_belt_layout(problem, layout)
        self.assertEqual(candidate.logistics_milp["status"], 0)
        self.assertEqual(candidate.logistics_milp["changed_cells"], 2)
        self.assertEqual(candidate.transport, {(1, 0): "belt1", (1, 1): "tunnel2", (3, 1): "tunnel2"})
        self.assertEqual(layout.transport, transport)
        self.assertEqual(candidate.segments, segments)


if __name__ == "__main__":
    unittest.main()
