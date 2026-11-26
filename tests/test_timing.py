import unittest
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

from solver.layout import Layout, Placement, Segment
from solver.simulate import simulate


class TimingTests(unittest.TestCase):
    def make_line(self, *, rate, inlet_capacity=None, cycle=2, tunnel=False, steam=False, output=False):
        catalog = {"steam_belt_timing": steam, "item_stacks": {"成品": 3}}
        problem = SimpleNamespace(
            catalog=catalog,
            case={"inlet_capacity": inlet_capacity},
            recipes={"成品": {"seconds": cycle, "quantity": 1, "inputs": {"原料": 1}}},
            belts=[
                {"building": "belt1", "capacity": 60, "seconds_per_cell": 2 if tunnel else 1,
                 "underground": "tunnel1"},
                {"building": "slow", "capacity": 30, "seconds_per_cell": 1},
            ],
            junction_capacity=120,
            target="成品",
        )
        rate = Fraction(rate)
        path = [(0, 0), (1, 0), (5, 0), (6, 0)] if tunnel else [(0, 0), (1, 0), (2, 0)]
        source, terminal = path[0], path[-1]
        inlet = Placement("inlet", 0, 0, 1, 1, source, "原料", rate)
        machine = Placement("maker", terminal[0], 0, 1, 1, terminal, "成品", rate)
        placements = [inlet, machine]
        ports = {inlet: ((), source), machine: ((terminal,), terminal if output else None)}
        transport = dict.fromkeys(path[1:-1], "tunnel1" if tunnel else "belt1")
        segments = [Segment(start, end, "原料", rate, 0) for start, end in zip(path, path[1:])]
        if output:
            drive = (terminal[0] + 1, 0)
            outlet = (terminal[0] + 2, 0)
            transport[drive] = "slow"
            placements.append(Placement("outlet", outlet[0], 0, 1, 1, outlet, "成品", rate))
            segments.extend((Segment(terminal, drive, "成品", rate, 1), Segment(drive, outlet, "成品", rate, 1)))
        return simulate(problem, Layout(terminal[0] + 3, 1, placements, transport, segments, set(), source, []),
                        ports=ports)

    def test_planned_partial_utilization_does_not_count_as_starvation(self):
        result = self.make_line(rate=15, inlet_capacity=15)
        row, = result["timing"]["machine_rows"]
        self.assertEqual(result["last_four_per_minute"], 15)
        self.assertEqual(row["recipe_seconds"], 2)
        self.assertEqual(row["planned_period_seconds"], 4)
        self.assertEqual(row["max_start_interval_seconds"], 4)
        self.assertGreater(row["idle_input_seconds"], 0)
        self.assertEqual(row["extra_starved_seconds"], 0)
        self.assertEqual(row["inputs"][0]["max_arrival_gap_seconds"], 4)
        self.assertEqual(row["start_jitter_rms_seconds"], 0)

    def test_inadequate_feed_reports_machine_and_arrival_gaps(self):
        result = self.make_line(rate=30, inlet_capacity=20)
        row, = result["timing"]["machine_rows"]
        self.assertEqual(result["last_four_per_minute"], 20)
        self.assertEqual(row["planned_period_seconds"], 2)
        self.assertEqual(row["max_start_interval_seconds"], 3)
        self.assertGreater(row["extra_starved_seconds"], 0)
        self.assertEqual(result["timing"]["max_excess_gap_seconds"], 1)
        missing, = row["starved_inputs"]
        self.assertEqual(missing["item"], "原料")
        self.assertEqual(missing["planned_items_per_minute"], 30)
        self.assertEqual(missing["actual_items_per_minute"], 20)
        self.assertEqual(missing["feeder"]["rated_items_per_minute"], 60)

    def test_slow_output_causes_backpressure_with_item_stack_buffer(self):
        result = self.make_line(rate=45, cycle=1, steam=True, output=True)
        row, = result["timing"]["machine_rows"]
        self.assertEqual(result["last_four_per_minute"], 30)
        self.assertEqual(row["extra_starved_seconds"], 0)
        self.assertGreater(row["extra_blocked_seconds"], 0)
        bottleneck = result["timing"]["bottlenecks"][0]
        self.assertEqual((bottleneck["kind"], bottleneck["load"]), ("slow", 1.5))
        self.assertEqual(bottleneck["actual_items_per_minute"], 30)

    def test_steam_tunnel_moves_half_cell_per_portal_with_legacy_fallback(self):
        steam = self.make_line(rate=30, tunnel=True, steam=True)
        legacy = self.make_line(rate=30, tunnel=True)
        self.assertGreaterEqual(steam["first_seconds"], 4)
        self.assertLessEqual(steam["first_seconds"], 4.25)
        self.assertEqual(legacy["first_seconds"] - steam["first_seconds"], 1)
        self.assertEqual(steam["last_four_per_minute"], legacy["last_four_per_minute"])

    def test_large_inlet_supplies_two_independent_lines(self):
        warehouse = Placement("inlet_large", 0, 0, 2, 2, (0, 1), "原料", Fraction(60))
        machines = [Placement("maker", 3, y, 1, 1, (3, y), "成品", Fraction(30)) for y in (0, 1)]
        transport = {(2, y): "belt1" for y in (0, 1)}
        segments = [Segment((x, y), (x + 1, y), "原料", Fraction(30), y) for y in (0, 1) for x in (1, 2)]
        layout = Layout(4, 2, [warehouse, *machines], transport, segments, set(), (0, 0), [])
        problem = SimpleNamespace(
            catalog={"steam_belt_timing": True},
            case={"inlet_capacity": 30},
            recipes={"成品": {"seconds": 2, "quantity": 1, "inputs": {"原料": 1}}},
            belts=[{"building": "belt1", "capacity": 60, "seconds_per_cell": 1}],
            junction_capacity=120,
            target="成品",
        )
        result = simulate(problem, layout, ports={machine: ((machine.anchor,), None) for machine in machines})
        self.assertEqual(result["last_four_per_minute"], 60)
        self.assertEqual(len(result["timing"]["machine_rows"]), 2)
        self.assertEqual(result["timing"]["machine_delay_seconds"], 0)

    def test_faster_belt_recovers_synchronized_inputs(self):
        names = ("原料甲", "原料乙")
        inlets = [Placement("inlet", 0, y, 1, 1, (0, y), name, Fraction(30)) for y, name in enumerate(names)]
        machine = Placement("maker", 2, 0, 1, 2, (2, 0), "成品", Fraction(30))
        segments = [Segment((x, y), (x + 1, y), name, Fraction(30), y)
                    for y, name in enumerate(names) for x in (0, 1)]
        layout = Layout(3, 2, [*inlets, machine], {(1, 0): "belt2", (1, 1): "belt1"},
                        segments, set(), (0, 0), [])
        problem = SimpleNamespace(
            catalog={"steam_belt_timing": True},
            case={},
            recipes={"成品": {"seconds": 2, "quantity": 1, "inputs": dict.fromkeys(names, 1)}},
            belts=[{"building": "belt1", "capacity": 20, "seconds_per_cell": 2},
                   {"building": "belt2", "capacity": 60, "seconds_per_cell": 1}],
            junction_capacity=120,
            target="成品",
        )
        ports = {inlet: ((), inlet.anchor) for inlet in inlets}
        ports[machine] = (((2, 0), (2, 1)), None)
        stalled = simulate(problem, layout, ports=ports)
        recovered = simulate(problem, replace(layout, transport=dict.fromkeys(layout.transport, "belt2")), ports=ports)
        self.assertEqual(stalled["last_four_per_minute"], 20)
        self.assertEqual(recovered["last_four_per_minute"], 30)
        missing, = stalled["timing"]["machine_rows"][0]["starved_inputs"]
        self.assertEqual(missing["item"], names[1])
        self.assertEqual(missing["feeder"]["position"], (1, 1))
        self.assertEqual(recovered["timing"]["machine_delay_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
