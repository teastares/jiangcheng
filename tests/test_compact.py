import unittest
from fractions import Fraction

from solver.compact import seam_compactions
from solver.layout import Layout, Placement, Segment


class CompactTests(unittest.TestCase):
    def test_cut_can_bend_between_independent_rows(self):
        rate = Fraction(1)
        placements = [Placement("inlet", 0, 0, 2, 1, (0, 0), "原料", rate, 0),
                      Placement("inlet", 1, 1, 2, 1, (1, 1), "原料", rate, 1),
                      Placement("maker", 4, 0, 1, 1, (4, 0), "产物", rate, 2),
                      Placement("maker", 4, 1, 1, 1, (4, 1), "产物", rate, 3)]
        paths = (((1, 0), (2, 0), (3, 0), (4, 0)), ((2, 1), (3, 1), (4, 1)))
        segments = [Segment(a, b, "原料", rate, bus)
                    for bus, points in enumerate(paths) for a, b in zip(points, points[1:])]
        layout = Layout(5, 2, placements, {(2, 0): "belt1", (3, 0): "belt1", (3, 1): "belt1"}, segments,
                        set(), (0, 0), [(p.item, p.rate, p) for p in placements[2:]])
        (result,) = seam_compactions(layout)
        self.assertEqual((result.width, result.height), (4, 2))
        self.assertEqual(result.transport, {(2, 0): "belt1", (2, 1): "belt1"})
        self.assertEqual({(s.start, s.end, s.bus, s.rate) for s in result.segments},
                         {((x, row), (x + 1, row), row, rate) for x in (1, 2) for row in (0, 1)})


if __name__ == "__main__":
    unittest.main()
