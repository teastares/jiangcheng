import unittest
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

from solver.layout import Layout, Placement, Segment
from solver.model import belt_options, transport_time
from solver.reroute import Connection, retier_layout, route_costs, search_route


class BeltPreferenceTests(unittest.TestCase):
    def setUp(self):
        belts = [{"level": level, "capacity": capacity, "seconds_per_cell": speed,
                  "building": f"belt{level}", "underground": f"tunnel{level}"}
                 for level, capacity, speed in ((3, 240, "1/4"), (2, 120, "1/2"), (1, 60, 1))]
        materials = {"belt1": 3, "belt2": 4, "belt3": 3, "tunnel1": 10, "tunnel2": 10, "tunnel3": 3}
        self.problem = SimpleNamespace(belts=belts, underground_spans={"tunnel1": 5, "tunnel2": 6, "tunnel3": 7},
                                       buildings={kind: {"materials": {"建材": qty}} for kind, qty in materials.items()},
                                       catalog={"junction_seconds": "1/2"})

    def test_choose_lowest_sufficient_belt_even_when_high_grade_uses_fewer_materials(self):
        problem = self.problem
        self.assertEqual([belt["level"] for belt in belt_options(problem, 96)], [2, 3])
        self.assertEqual([belt["level"] for belt in belt_options(problem, 96, span=7)], [3])
        shape = Layout(5, 1, [], {}, [], set(), (0, 0), [])
        start, end, east = (0, 0), (4, 0), (1, 0)
        prices = route_costs(problem, ("materials", "buildings"))
        edge = Segment(start, end, "原木", Fraction(96), 0)
        _, surface = search_route(problem, shape, Connection((edge,), start, east, end, east), {start, end}, [], prices)
        self.assertEqual(surface, {(1, 0): "belt2", (2, 0): "belt2", (3, 0): "belt2"})

        edge = Segment(start, end, "原木", Fraction(48), 0)
        _, tunnel = search_route(problem, shape, Connection((edge,), start, east, end, east),
                                 {start, end, (2, 0)}, [], prices)
        self.assertEqual(tunnel, {(1, 0): "tunnel1", (3, 0): "tunnel1"})

    def test_regrade_keeps_required_span_and_adjacent_nearest_pairs(self):
        point_loads = [((0, 1), (1, 1), 96), ((1, 0), (3, 0), 48), ((4, 0), (6, 0), 48),
                       ((1, 2), (8, 2), 96)]
        segments = [Segment(start, end, "原木", Fraction(rate), bus)
                    for bus, (start, end, rate) in enumerate(point_loads)]
        transport = {(1, 1): "belt3", (1, 0): "tunnel1", (3, 0): "tunnel1", (4, 0): "tunnel3", (6, 0): "tunnel3",
                     (1, 2): "tunnel3", (8, 2): "tunnel3"}
        layout = Layout(9, 3, [], transport, segments, set(), (0, 0), [])
        lower = retier_layout(self.problem, layout)
        self.assertEqual(lower.transport, {**transport, (1, 1): "belt2", (4, 0): "tunnel1", (6, 0): "tunnel1"})
        self.assertEqual(lower.segments, layout.segments)
        self.assertEqual(layout.transport[(1, 1)], "belt3")

    def test_transport_time_uses_junction_and_half_exit_cell_independent_of_span(self):
        points = [((0, 0), (1, 0), 60), ((1, 0), (2, 0), 60), ((2, 0), (3, 0), 30),
                  ((3, 0), (7, 0), 30), ((7, 0), (8, 0), 30), ((2, 0), (2, 1), 30)]
        segments = [Segment(start, end, "石材", rate, 0) for start, end, rate in points]
        outlet = Placement("outlet", 8, 0, 1, 1, (8, 0), "石材", Fraction(30), 0)
        layout = Layout(9, 2, [outlet], {(1, 0): "belt1", (2, 0): "split", (3, 0): "tunnel1", (7, 0): "tunnel1"},
                        segments, set(), (0, 0), [])
        value = transport_time(self.problem, layout)
        self.assertEqual(value, Fraction(11, 4))
        self.assertIsInstance(value, Fraction)
        moved = {(7, 0): (5, 0), (8, 0): (6, 0)}
        nearby = replace(layout, placements=[replace(outlet, x=6, anchor=(6, 0))],
                         transport={moved.get(point, point): kind for point, kind in layout.transport.items()},
                         segments=[replace(edge, start=moved.get(edge.start, edge.start), end=moved.get(edge.end, edge.end))
                                   for edge in layout.segments])
        self.assertEqual(transport_time(self.problem, nearby), value)


if __name__ == "__main__":
    unittest.main()
