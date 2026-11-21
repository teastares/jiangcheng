"""同配方与产速下可重新布置的参考起点。"""

import json
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def read_references():
    path = Path(__file__).with_name("reference_layouts.json")
    return json.loads(path.read_text(encoding="utf-8"))


def reference_candidates(problem):
    from .layout import Layout, Placement, Segment

    if not problem.case.get("reference_start", True) or not problem.catalog.get("blueprint_format"):
        return
    for data in read_references():
        if (data["target"] != problem.target or Fraction(data["target_per_minute"]) != problem.rate or
                data["platform"] != problem.catalog.get("platform")):
            continue
        machines = [p for p in data["placements"] if p["building"] not in ("inlet", "outlet")]
        if sum(p["item"] == problem.target for p in machines) != problem.count:
            continue
        if any(p["item"] not in problem.ranks or problem.recipes[p["item"]].get("id") != p["recipe"] or
               problem.recipes[p["item"]]["machine"] != p["building"] for p in machines):
            continue
        transport = {tuple(row["position"]): row["kind"] for row in data["transport"]}
        permitted = {belt["building"] for belt in problem.belts} | set(problem.underground_spans) | {"split", "merge"}
        if not set(transport.values()) <= permitted:
            continue
        placements = [Placement(p["building"], p["x"], p["y"], p["width"], p["height"], tuple(p["anchor"]), p["item"],
                                Fraction(p["rate"]), p["node"], p["direction"]) for p in data["placements"]]
        segments = [Segment(tuple(s["start"]), tuple(s["end"]), s["item"], Fraction(s["rate"]), s["bus"])
                    for s in data["segments"]]
        devices = [(p.item, p.rate, p) for p in placements if p.building not in ("inlet", "outlet")]
        yield Layout(*data["size"], placements, transport, segments, {tuple(p) for p in data["wires"]},
                     tuple(data["power_entry"]), devices, profile=data["profile"], extended=True)
