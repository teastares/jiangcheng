import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from solver.layout import Layout, Placement, Segment, assess, verify
from solver.model import Problem
from solver.rotation import ANGLES, rotate_layout, rotated_direction, rotated_point, rotation_plan, rotation_result


class RotationPolicyTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "solver" / "catalog.json"
        catalog = json.loads(path.read_text(encoding="utf-8"))
        catalog.pop("blueprint_format", None)
        for kind in ("inlet", "maker"):
            catalog["buildings"][kind]["size"] = [1, 1]
        self.problem = Problem(catalog, {"target": "石材", "machines": 1, "external": ["石头"], "rate": 10})
        rate = self.problem.rate
        inlet = Placement("inlet", 0, 1, 1, 1, (0, 1), "石头", rate, 0)
        maker = Placement("maker", 3, 1, 1, 1, (3, 1), "石材", rate, 1)
        outlet = Placement("outlet", 5, 1, 1, 1, (5, 1), "石材", rate, 2)
        segments = [Segment((x, 1), (x + 1, 1), "石头" if x < 3 else "石材", rate, x >= 3) for x in range(5)]
        self.layout = Layout(6, 3, [inlet, maker, outlet], {(x, 1): "belt1" for x in (1, 2, 4)}, segments,
                             {(3, 0), (3, 1)}, (3, 0), [(maker.item, rate, maker)])
        verify(self.problem, self.layout)
        assess(self.problem, self.layout)

    def test_rigid_rotation_preserves_device_and_line_counts(self):
        source = self.layout
        for angle in ANGLES:
            with self.subTest(angle=angle):
                result = rotation_result(self.problem, source, angle)
                self.assertEqual(result.method, "rigid")
                rotated = result.layout
                self.assertIsNotNone(rotated)

                def transform(point):
                    return rotated_point(point, source.width, source.height, angle)

                self.assertEqual(rotated.statistics["components"], source.statistics["components"])
                self.assertEqual(rotated.transport, {transform(point): kind for point, kind in source.transport.items()})
                self.assertEqual(rotated.wires, {transform(point) for point in source.wires})
                self.assertEqual(rotated.power_entry, transform(source.power_entry))
                self.assertEqual(rotated.segments, [replace(s, start=transform(s.start), end=transform(s.end))
                                                    for s in source.segments])
                for original, device in zip(source.placements, rotated.placements):
                    self.assertEqual((device.building, device.node, device.anchor, device.direction),
                                     (original.building, original.node, transform(original.anchor),
                                      rotated_direction(original.direction, angle)))

    def test_rerouting_obeys_capsule_budget(self):
        original = self.layout
        problem = self.problem
        rotated, placements, _ = rotation_plan(problem, original, 90, (1, 1), 0)
        machine = placements[1]
        rotated = replace(rotated, placements=placements, machines=[(machine.item, machine.rate, machine)])
        replacing = rotated.segments[1]
        points = [replacing.start, (2, 1), (2, 2), replacing.end]
        detour = replace(rotated, transport={**rotated.transport, (2, 1): "belt1", (2, 2): "belt1"},
                         segments=[s for s in rotated.segments if s != replacing] +
                         [replace(replacing, start=a, end=b) for a, b in zip(points, points[1:])])
        verify(problem, detour)
        assess(problem, detour)
        self.assertGreater(detour.statistics["components"], original.statistics["components"])

        # 只提供已验证可建但多花胶囊的重布结果，角度必须不可用。
        forced_changes = ({(0, 0): ((1, 1), (0, 1))}, {})
        with patch("solver.rotation.rotation_plan", return_value=(rotated, placements, forced_changes)), \
             patch("solver.rotation.rotated_placements", return_value=()), \
             patch("solver.rotation.seam_compactions", return_value=()), \
             patch("solver.rotation.rerouted_layouts", return_value=(detour,)) as reroute:
            unavailable = rotation_result(problem, original, 90)
            self.assertEqual(unavailable.method, "unavailable")
            self.assertIsNone(unavailable.layout)
            self.assertIsNone(rotate_layout(problem, original, 90))

            reroute.return_value = detour, rotated
            rewired = rotation_result(problem, original, 90)
            self.assertEqual(rewired.method, "rewired")
            self.assertEqual(rewired.layout.statistics["components"], original.statistics["components"])


if __name__ == "__main__":
    unittest.main()
