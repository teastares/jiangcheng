import unittest
from fractions import Fraction
from types import SimpleNamespace

from solver.layout import Layout, Segment
from solver.reroute import fit_belt_grades


class FitBeltGradesTests(unittest.TestCase):
    def setUp(self):
        self.belts = [{"level": level, "capacity": capacity, "building": f"belt{level}",
                       "underground": f"tunnel{level}"} for level, capacity in ((1, 60), (2, 120), (3, 240))]

    def problem(self, *levels):
        return SimpleNamespace(belts=[belt for belt in self.belts if belt["level"] in levels], catalog={"belts": self.belts},
                               underground_spans={f"tunnel{level}": level + 4 for level in levels})

    def line(self, rate):
        path = [(x, 0) for x in (0, 1, 2, 8, 9)]
        segments = [Segment(a, b, "原木", Fraction(rate), 0) for a, b in zip(path, path[1:])]
        return Layout(10, 1, [], dict(zip(path[1:-1], ("belt2", "tunnel2", "tunnel2"))), segments, set(), (0, 0), [])

    def test_only_third_grade_can_reuse_second_grade_reference(self):
        original = self.line(96)
        fitted = fit_belt_grades(self.problem(3), original)
        self.assertEqual(fitted.transport, {(1, 0): "belt3", (2, 0): "tunnel3", (8, 0): "tunnel3"})
        self.assertEqual(fitted.segments, original.segments)
        self.assertEqual(original.transport[(1, 0)], "belt2")
        self.assertIs(fit_belt_grades(self.problem(1, 2, 3), original), original)

    def test_must_fit_both_rate_and_span(self):
        self.assertIsNone(fit_belt_grades(self.problem(1), self.line(96)))
        self.assertIsNone(fit_belt_grades(self.problem(1), self.line(48)))
        low_flow = fit_belt_grades(self.problem(1, 3), self.line(48))
        self.assertEqual(low_flow.transport, {(1, 0): "belt1", (2, 0): "tunnel3", (8, 0): "tunnel3"})

    def test_nearest_pairing_failure_is_never_returned(self):
        pairs = [((1, 0), (6, 0), "tunnel1"), ((2, 0), (4, 0), "tunnel2")]
        transport = {point: kind for a, b, kind in pairs for point in (a, b)}
        segments = [Segment(a, b, "原木", Fraction(48), bus) for bus, (a, b, _) in enumerate(pairs)]
        layout = Layout(7, 1, [], transport, segments, set(), (0, 0), [])
        self.assertIsNone(fit_belt_grades(self.problem(3), layout))


if __name__ == "__main__":
    unittest.main()
