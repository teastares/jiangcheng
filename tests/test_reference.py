import json
import unittest
from collections import Counter
from dataclasses import replace

from solver.blueprint import IDS, decode, records_for, split_direction
from solver.compact import seam_compactions
from solver.layout import assess, power_grid, verify
from solver.model import Problem
from solver.power import refine_power_grid
from solver.reference import reference_candidates
from solver.simulate import simulate
from tools.benchmark_legacy_green import ROOT, SAMPLE, reference_layout


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog = json.loads((ROOT / "solver" / "catalog.json").read_text(encoding="utf-8"))
        cls.problem = Problem(catalog, {"target": "绿色晶能", "machines": 2, "external": ["原木", "石头"],
                                        "power_limit_kw": 200})

    def test_reference_reencodes_geometry_with_corrected_split_records(self):
        _, _, _, _, original_buildings, original_wires = decode(SAMPLE.with_suffix(".txt").read_text(encoding="ascii"))
        # 旧图将逻辑行进方向直接写入分流器两个字段，保留其布局，纠正原始编号。
        corrected = [replace(record, direction=split_direction(record.direction), shape=split_direction(record.shape))
                     if record.id == IDS["split"] else record for record in original_buildings]
        for layout in (reference_layout(self.problem), next(reference_candidates(self.problem))):
            with self.subTest(profile=layout.profile):
                verify(self.problem, layout)
                buildings, wires = records_for(self.problem, layout)
                self.assertEqual(Counter(buildings), Counter(corrected))
                self.assertEqual(len(wires), len(original_wires) + 1)
                self.assertTrue(set(original_wires) <= set(wires))

    def test_reference_compaction_preserves_rate_and_reduces_construction(self):
        layout = reference_layout(self.problem)
        reduced = next(seam_compactions(layout))
        anchors = [p.anchor for _, _, p in reduced.machines]
        initial = power_grid(anchors, reduced.width, reduced.height)
        reduced.wires, reduced.power_entry = refine_power_grid(anchors, reduced.width, reduced.height, initial)
        verify(self.problem, reduced)
        assess(self.problem, reduced)
        self.assertLess(reduced.width * reduced.height, 43 * 20)
        self.assertLess(reduced.statistics["components"], 264)
        self.assertLess(sum(reduced.statistics["materials"].values()), 1665)
        self.assertLess(reduced.statistics["transport"] + reduced.statistics["wires"], 352)
        self.assertEqual(reduced.statistics["power"], 200)
        self.assertEqual(simulate(self.problem, reduced)["last_four_per_minute"], 12.0)


if __name__ == "__main__":
    unittest.main()
