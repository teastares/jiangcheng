import json
import unittest
from pathlib import Path

from solver.blueprint import export
from solver.geometry import EAST
from solver.layout import Layout, Placement, Segment, assess, power_grid, verify
from solver.model import Problem
from solver.orient import outlet_trials
from solver.reroute import rerouted_layouts
from solver.simulate import simulate


class OutletTrialTests(unittest.TestCase):
    def test_close_outlet_rotates_merge_without_moving_machines(self):
        path = Path(__file__).resolve().parents[1] / "solver" / "catalog.json"
        with path.open(encoding="utf-8") as stream:
            problem = Problem(json.load(stream), {"target": "石材", "machines": 2, "external": ["石头"]})
        rate = problem.rate / 2
        placements = [Placement("inlet", 0, 2, 2, 1, (0, 2), "石头", rate, 0),
                      Placement("inlet", 0, 4, 2, 1, (0, 4), "石头", rate, 1),
                      Placement("maker", 3, 2, 2, 1, (3, 2), "石材", rate, 2),
                      Placement("maker", 3, 4, 2, 1, (3, 4), "石材", rate, 3),
                      Placement("outlet", 7, 0, 1, 1, (7, 0), "石材", problem.rate, 4)]
        paths = [(((1, 2), (2, 2), (3, 2)), "石头", rate, 0),
                 (((1, 4), (2, 4), (3, 4)), "石头", rate, 1),
                 (((4, 2), (5, 2), (6, 2)), "石材", rate, 2),
                 (((4, 4), (5, 4), (6, 4), (6, 3), (6, 2)), "石材", rate, 2),
                 (((6, 2), (6, 1), (6, 0), (7, 0)), "石材", problem.rate, 2)]
        segments = [Segment(a, b, item, flow, bus) for points, item, flow, bus in paths for a, b in zip(points, points[1:])]
        transport = {point: "belt1" for points, _, _, _ in paths for point in points[1:-1]}
        transport[(6, 2)] = "merge"
        machines = [(p.item, p.rate, p) for p in placements if p.building == "maker"]
        wires, entry = power_grid([p.anchor for _, _, p in machines], 8, 6)
        original = Layout(8, 6, placements, transport, segments, wires, entry, machines)
        verify(problem, original)
        assess(problem, original)

        moved, changes, _ = next(outlet_trials(problem, original))
        (candidate,) = rerouted_layouts(problem, original, placements=moved, port_changes=changes, max_routes=0)
        verify(problem, candidate)
        assess(problem, candidate)
        export(problem, candidate, "就近接出测试", "")
        self.assertEqual((moved[-1].anchor, moved[-1].direction), ((7, 2), EAST))
        self.assertIn(Segment((6, 2), (7, 2), "石材", problem.rate, 2), candidate.segments)
        self.assertEqual(candidate.statistics["components"], original.statistics["components"] - 2)
        self.assertEqual(candidate.statistics["power"], original.statistics["power"])
        self.assertEqual(candidate.machines, original.machines)
        self.assertEqual(simulate(problem, candidate)["last_four_per_minute"], problem.rate)


if __name__ == "__main__":
    unittest.main()
