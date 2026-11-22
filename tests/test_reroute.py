import unittest
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

from solver.blueprint import FORMAT
from solver.collision import collision_error
from solver.geometry import NORTH, WEST
from solver.layout import Layout, Placement, Segment, verify
from solver.model import Infeasible, Problem
from solver.reroute import Connection, compatible_tunnel, rerouted_layouts, route_costs, search_route, step, tunnel_pairing_valid


class RerouteTests(unittest.TestCase):
    def setUp(self):
        self.problem = SimpleNamespace(objective=("buildings",), underground_spans={"tunnel1": 5},
                                       belts=[{"building": "belt1", "underground": "tunnel1", "capacity": 60}],
                                       buildings={"belt1": {"materials": {"木板": 1}},
                                                  "tunnel1": {"materials": {"木板": 5}}})

    def test_route_can_use_five_cell_tunnel_in_every_direction(self):
        prices = route_costs(self.problem, self.problem.objective)
        for direction, source in (((1, 0), (0, 0)), ((-1, 0), (7, 0)), ((0, 1), (0, 0)), ((0, -1), (0, 7))):
            with self.subTest(direction=direction):
                sink = step(source, direction, 7)
                blocked = {source, sink, *(step(source, direction, n) for n in range(2, 6))}
                size = (8, 1) if direction[0] else (1, 8)
                layout = Layout(*size, [], {}, [], set(), (0, 0), [])
                edge = Segment(source, sink, "原木", Fraction(15), 0)
                connection = Connection((edge,), source, direction, sink, direction)
                segments, types = search_route(self.problem, layout, connection, blocked, [], prices)
                first, last = step(source, direction), step(source, direction, 6)
                self.assertEqual([(s.start, s.end) for s in segments], [(source, first), (first, last), (last, sink)])
                self.assertEqual(types, {first: "tunnel1", last: "tunnel1"})
                self.problem.underground_spans.clear()
                self.assertIsNone(search_route(self.problem, layout, connection, blocked, [], prices))
                self.problem.underground_spans["tunnel1"] = 5

    def test_crossing_tunnels_keep_unambiguous_pairs(self):
        spans = {"tunnel1": 5, "tunnel2": 6}
        existing = [((1, 0), (1, 4), "tunnel1")]
        self.assertTrue(compatible_tunnel((0, 2), (5, 2), "tunnel1", existing, spans))
        self.assertFalse(compatible_tunnel((1, 2), (1, 6), "tunnel2", existing, spans))
        self.assertTrue(compatible_tunnel((1, 5), (1, 7), "tunnel2", existing, spans))
        self.assertTrue(compatible_tunnel((1, 5), (1, 7), "tunnel1", [((1, 2), (1, 4), "tunnel1")], spans))

    def test_game_pairing_scans_adjacent_endpoint_and_ignores_tier_and_role(self):
        spans = {"tunnel1": 5, "tunnel2": 6, "tunnel3": 7, "tunnel4": 8, "tunnel5": 8}
        for tier, limit in spans.items():
            self.assertTrue(tunnel_pairing_valid([((0, 0), (limit, 0), tier)], spans))
            self.assertFalse(tunnel_pairing_valid([((0, 0), (limit + 1, 0), tier)], spans))
        pairs = [((2, 0), (7, 0), "tunnel1"), ((3, 0), (1, 0), "tunnel2")]
        self.assertTrue(all(tunnel_pairing_valid([pair], spans) for pair in pairs))
        self.assertFalse(tunnel_pairing_valid(pairs, spans))

    def test_higher_tier_cannot_bypass_endpoint_pairing(self):
        self.problem.belts.append({"building": "belt2", "underground": "tunnel2", "capacity": 120})
        self.problem.buildings.update({"belt2": {"materials": {"铁板": 4}}, "tunnel2": {"materials": {"铁板": 6}}})
        self.problem.underground_spans["tunnel2"] = 6
        source, sink, direction = (0, 0), (6, 0), (1, 0)
        blocked = {(x, 0) for x in (0, 2, 3, 4, 6)}
        layout = Layout(7, 1, [], {}, [], set(), (0, 0), [])
        connection = Connection((Segment(source, sink, "原木", Fraction(30), 0),), source, direction, sink, direction)
        existing = [((2, 0), (4, 0), "tunnel1")]
        prices = route_costs(self.problem, self.problem.objective)
        self.assertIsNone(search_route(self.problem, layout, connection, blocked, existing, prices))

    def test_layout_verifier_rejects_cross_tier_pairing_before_export(self):
        belts = [{**self.problem.belts[0], "underground_span": 5},
                 {"building": "belt2", "underground": "tunnel2", "capacity": 120, "underground_span": 6}]
        catalog = {"belts": belts, "junction_capacity": 120,
                   "buildings": {**self.problem.buildings, "belt2": {}, "tunnel2": {},
                                 "inlet": {"size": (2, 1), "anchor": (0, 0), "output": 0},
                                 "maker": {"size": (2, 1), "anchor": (0, 0), "inputs": (0,)}},
                   "recipes": {"木材": {"machine": "maker", "inputs": {"原木": 1}, "quantity": 1,
                                        "seconds": 1, "virtual": True}}}
        problem = Problem(catalog, {"target": "木材", "machines": 2, "external": ["原木"], "rate": 48})
        rate = Fraction(24)

        def test_layout(row):
            placements = [Placement("inlet", 0, row, 2, 1, (0, row), "原木", rate, 0),
                          Placement("inlet", 1, 0, 2, 1, (1, 0), "原木", rate, 1),
                          Placement("maker", 9, row, 2, 1, (9, row), "木材", rate, 2),
                          Placement("maker", 9, 3, 2, 1, (9, 3), "木材", rate, 3)]
            long = [(x, row) for x in (1, 2, 8, 9)]
            short = [(2, 0), (3, 0), (3, 1), (3, 2), (4, 2), (6, 2), (7, 2), (7, 3), (8, 3), (9, 3)]
            transport = dict.fromkeys(short[1:-1], "belt1")
            transport.update({(4, 2): "tunnel1", (6, 2): "tunnel1", (2, row): "tunnel2", (8, row): "tunnel2"})
            segments = [Segment(a, b, "原木", rate, bus) for bus, path in enumerate((long, short))
                        for a, b in zip(path, path[1:])]
            machines = [(p.item, p.rate, p) for p in placements[2:]]
            return Layout(11, 4, placements, transport, segments, set(), (0, 0), machines, extended=True)

        verify(problem, test_layout(1))
        with self.assertRaisesRegex(Infeasible, "地下带最近端点"):
            verify(problem, test_layout(2))

    def test_cheaper_tunnel_is_excluded_when_capacity_is_insufficient(self):
        self.problem.belts.append({"building": "belt2", "underground": "tunnel2", "capacity": 120})
        self.problem.buildings.update({"belt2": {"materials": {"铁板": 4}}, "tunnel2": {"materials": {"铁板": 6}}})
        self.problem.underground_spans["tunnel2"] = 5
        source, sink, direction = (0, 0), (7, 0), (1, 0)
        blocked = {(x, 0) for x in (0, 2, 3, 4, 5, 7)}
        layout = Layout(8, 1, [], {}, [], set(), (0, 0), [])
        edge = Segment(source, sink, "原木", Fraction(90), 0)
        connection = Connection((edge,), source, direction, sink, direction)
        prices = route_costs(self.problem, ("materials", "buildings"))
        _, types = search_route(self.problem, layout, connection, blocked, [], prices)
        self.assertEqual(set(types.values()), {"tunnel2"})
        self.problem.underground_spans["tunnel2"] = 2
        self.assertIsNone(search_route(self.problem, layout, connection, blocked, [], prices))

    def test_direct_route_requires_compatible_terminals(self):
        source, sink, east = (0, 0), (1, 0), (1, 0)
        layout = Layout(2, 1, [], {}, [], set(), (0, 0), [])
        edge = Segment(source, sink, "原木", Fraction(24), 0)
        connection = Connection((edge,), source, east, sink, east)
        prices = route_costs(self.problem, self.problem.objective)
        self.assertEqual(search_route(self.problem, layout, connection, {source, sink}, [], prices), ([edge], {}))
        self.assertIsNone(search_route(self.problem, layout, connection, {source, sink}, [], prices,
                                       terminal_kinds={source: "inlet", sink: "maker"}))
        self.assertEqual(search_route(self.problem, layout, connection, {source, sink}, [], prices,
                                      terminal_kinds={source: "split", sink: "maker"}), ([edge], {}))

    def test_move_split_rotates_each_branch_and_reconnects_flow(self):
        old, new = (4, 4), (5, 4)
        item = "原木"
        paths = [
            [(0, 4), (1, 4), (2, 4), (3, 4), old],
            [old, (4, 3), *((x, 2) for x in range(4, 10))],
            [old, (4, 5), (4, 6), *((x, 7) for x in range(4, 10))],
        ]
        rates = (Fraction(48), Fraction(24), Fraction(24))
        transport = {point: "belt1" for path in paths for point in path[1:-1]}
        transport[old] = "split"
        segments = [Segment(a, b, item, rate, 0) for path, rate in zip(paths, rates) for a, b in zip(path, path[1:])]
        terminals = (paths[0][0], paths[1][-1], paths[2][-1])
        placements = [Placement(kind, *point, 1, 1, point, item, rate, i)
                      for i, (point, rate, kind) in enumerate(zip(terminals, rates, ("inlet", "maker", "maker")))]
        crossing = [(5, 6), (5, 5), new, (5, 1), (5, 0)]
        crossing_rate = Fraction(12)
        placements.extend(Placement(kind, *point, 1, 1, point, item, crossing_rate, i, NORTH)
                          for i, (kind, point) in enumerate(zip(("inlet", "maker"), (crossing[0], crossing[-1])), 3))
        transport.update({crossing[1]: "belt1", new: "tunnel1", crossing[-2]: "tunnel1"})
        segments.extend(Segment(a, b, item, crossing_rate, 1) for a, b in zip(crossing, crossing[1:]))
        original = Layout(11, 10, placements, transport, segments, set(), (0, 0), [])

        (result,) = rerouted_layouts(self.problem, original, junction_moves={old: (new, 1)}, max_routes=0)
        self.assertEqual({p for p, kind in result.transport.items() if kind == "split"}, {new})
        self.assertEqual([(s.start, s.rate) for s in result.segments if s.end == new], [((5, 3), rates[0])])
        self.assertEqual({(s.end, s.rate) for s in result.segments if s.start == new},
                         {((6, 4), rates[1]), ((4, 4), rates[2])})
        rerouted_crossing = [s for s in result.segments if s.bus == 1]
        self.assertEqual((rerouted_crossing[0].start, rerouted_crossing[-1].end), (crossing[0], crossing[-1]))
        self.assertTrue(all(new not in (s.start, s.end) and s.rate == crossing_rate for s in rerouted_crossing))
        self.assertEqual(result.placements, placements)
        for invalid in (terminals[0], (11, 4)):
            self.assertEqual(list(rerouted_layouts(self.problem, original, junction_moves={old: (invalid, 1)})), [])

    def test_reroute_reduces_surface_pieces_and_preserves_flow(self):
        rate = Fraction(24)
        material = "原木"
        old_path = [(0, 1), (1, 1), (1, 0), *((x, 0) for x in range(2, 7)), (6, 1), (7, 1)]
        placements = [Placement("inlet", 0, 1, 1, 1, (0, 1), material, rate, 0),
                      Placement("maker", 7, 1, 1, 1, (7, 1), "木材", rate, 1)]
        segments = [Segment(a, b, material, rate, 0) for a, b in zip(old_path, old_path[1:])]
        original = Layout(8, 3, placements, dict.fromkeys(old_path[1:-1], "belt1"), segments, set(), (0, 0), [])
        (result,) = rerouted_layouts(self.problem, original, max_routes=1)
        self.assertEqual(result.transport, {(1, 1): "tunnel1", (6, 1): "tunnel1"})
        self.assertEqual(result.placements, original.placements)
        self.assertEqual({(s.item, s.rate, s.bus) for s in result.segments}, {(material, rate, 0)})
        self.assertEqual(len(original.transport), 8)
        (cheaper,) = rerouted_layouts(self.problem, original, max_routes=1, objectives=(("materials", "buildings"),))
        self.assertEqual(cheaper.transport, {(x, 1): "belt1" for x in range(1, 7)})

    def test_reroute_avoids_machine_body_beyond_its_grid_cells(self):
        problem = SimpleNamespace(**vars(self.problem), catalog={"blueprint_format": FORMAT})
        rate = Fraction(24)
        source = Placement("inlet", 0, 1, 2, 1, (0, 1), "原木", rate, 0)
        sink = Placement("maker", 8, 1, 2, 1, (8, 1), "木材", rate, 1)
        neighbor = Placement("maker", 4, 2, 2, 1, (4, 2), "木材", rate, 2)
        transport = dict.fromkeys(((x, 1) for x in range(2, 8)), "belt1")
        segments = [Segment((x, 1), (x + 1, 1), "原木", rate, 0) for x in range(1, 8)]
        original = Layout(10, 5, [source, sink, neighbor], transport, segments, set(), (0, 0), [])
        self.assertTrue(transport.keys().isdisjoint(neighbor.cells()))
        self.assertIsNotNone(collision_error(problem, original.placements, transport))

        (result,) = rerouted_layouts(problem, original, max_routes=0)
        self.assertEqual(result.transport, {(2, 1): "tunnel1", (7, 1): "tunnel1"})
        self.assertIsNone(collision_error(problem, result.placements, result.transport))

        neighbor = replace(neighbor, x=2, anchor=(2, 2))
        blocked_entering_cell = replace(original, placements=[source, sink, neighbor])
        self.assertEqual(list(rerouted_layouts(problem, blocked_entering_cell, max_routes=0)), [])

    def test_reconnect_rotated_machine_and_keep_production_target(self):
        rate = Fraction(24)
        catalog = {"belts": self.problem.belts, "junction_capacity": 120,
                   "buildings": {**self.problem.buildings,
                                 "inlet": {"size": (2, 1), "anchor": (0, 0), "output": 0},
                                 "maker": {"size": (2, 1), "anchor": (0, 0), "inputs": (0,)}},
                   "recipes": {"木材": {"machine": "maker", "inputs": {"原木": 1}, "quantity": 1,
                                        "seconds": 1, "virtual": True}}}
        problem = Problem(catalog, {"target": "木材", "machines": 1, "external": ["原木"], "rate": 24})
        source = Placement("inlet", 0, 0, 2, 1, (0, 0), "原木", rate, 0)
        machine = Placement("maker", 5, 0, 2, 1, (5, 0), "木材", rate, 1)
        old_path = [(x, 0) for x in range(1, 6)]
        segments = [Segment(a, b, "原木", rate, 0) for a, b in zip(old_path, old_path[1:])]
        layout = Layout(8, 3, [source, machine], dict.fromkeys(old_path[1:-1], "belt1"), segments,
                        set(), (0, 0), [("木材", rate, machine)])
        verify(problem, layout)
        turned = replace(machine, direction=WEST)
        (result,) = rerouted_layouts(problem, layout, placements=[source, turned], max_routes=0)
        verify(problem, result)
        self.assertEqual((result.segments[-1].start, result.segments[-1].end), ((7, 0), (6, 0)))
        self.assertEqual(result.machines, [("木材", rate, turned)])


if __name__ == "__main__":
    unittest.main()
