import json
import unittest
from dataclasses import replace

from solver.geometry import EAST, NORTH, WEST, oriented_geometry
from solver.model import Problem
from solver.simulate import simulate
from tools.benchmark_legacy_green import ROOT, reference_layout


class GeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = json.loads((ROOT / "solver" / "catalog.json").read_text(encoding="utf-8"))
        cls.problem = Problem(catalog, {"target": "绿色晶能", "machines": 2, "external": ["原木", "石头"]})

    def test_oriented_ports_follow_sample_recipe_order(self):
        assembler = self.problem.buildings["assembler"]
        self.assertEqual(oriented_geometry("assembler", assembler, EAST),
                         {"size": (3, 2), "anchor": (1, 1), "inputs": ((0, 0), (0, 1)), "output": (2, 0)})
        self.assertEqual(oriented_geometry("assembler", assembler, WEST),
                         {"size": (3, 2), "anchor": (1, 1), "inputs": ((2, 0), (2, 1)), "output": (0, 1)})
        self.assertEqual(oriented_geometry("exchanger", self.problem.buildings["exchanger"], NORTH),
                         {"size": (2, 2), "anchor": (0, 1), "inputs": ((0, 1), (1, 1)), "output": None})

    def test_simulation_uses_coordinates_instead_of_component_record_order(self):
        layout = reference_layout(self.problem)
        reordered = replace(layout, placements=list(reversed(layout.placements)),
                            transport=dict(reversed(list(layout.transport.items()))),
                            segments=list(reversed(layout.segments)))
        self.assertEqual(simulate(self.problem, reordered), simulate(self.problem, layout))


if __name__ == "__main__":
    unittest.main()
