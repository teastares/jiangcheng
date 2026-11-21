import json
import unittest
from collections import Counter
from fractions import Fraction
from pathlib import Path

from solver.model import Port, Problem, make_network, partition


ROOT = Path(__file__).resolve().parents[1]


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "solver/catalog.json").read_text(encoding="utf-8"))
        cls.case = json.loads((ROOT / "solver/examples.json").read_text(encoding="utf-8"))["green-crystal"]

    def test_direct_groups_preserve_machines_and_avoid_unneeded_junctions(self):
        ports = [Port(index, 0, Fraction(18)) for index in range(4)]
        merged = partition(ports, 120, 10, 120, "balanced")
        direct = partition(ports, 120, 10, 120, "direct")
        self.assertEqual(sum(group[3] for group in merged), 8)
        self.assertEqual([group[3] for group in direct], [2, 2, 2, 2])

    def test_direct_green_crystal_keeps_local_branches(self):
        problem = Problem(self.catalog, self.case)
        network = make_network(problem, problem.count, "direct", problem.capacity)
        machines = Counter(node.item for node in network.nodes if node.kind == "machine")
        self.assertEqual(sum(machines.values()), 29)
        self.assertEqual(machines["木纤维"], 5)
        self.assertEqual(sorted(node.rate for node in network.nodes if node.kind == "inlet" and node.item == "原木"), [32, 96])
        for bus in network.buses:
            self.assertEqual(sum(port.rate for port in bus.sources), sum(port.rate for port in bus.sinks))
            if bus.item == "线":
                self.assertEqual((len(bus.sources), len(bus.sinks)), (2, 1))

    def test_distributed_group_respects_each_source_capacity(self):
        problem = Problem(self.catalog, self.case)
        network = make_network(problem, problem.count, "distributed", problem.capacity)
        fiber_buses = [bus for bus in network.buses if bus.item == "木纤维"]
        self.assertEqual(len(fiber_buses), 1)
        self.assertEqual(len(fiber_buses[0].sources), 5)
        self.assertEqual(sum(port.rate for port in fiber_buses[0].sources), 144)
        for node in network.nodes:
            limit = problem.box_capacity if node.kind == "inlet" else problem.machine_capacity(node.item)
            self.assertLessEqual(node.rate, limit)
