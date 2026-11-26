"""使用 HiGHS 求解产能配置及固定线路的带级分配。"""

from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .model import DAY, Infeasible, belt_options, belt_seconds
from .reroute import distance, tunnel_segments


@dataclass(frozen=True)
class ProductionPlan:
    units: dict
    rates: dict
    power_kw: Fraction
    status: int
    mip_gap: float | None

    def details(self, copies):
        return {"status": self.status, "mip_gap": self.mip_gap,
                "minimum_power_kw": float(self.power_kw * copies),
                "units": {item: count * copies for item, count in self.units.items()},
                "rates": {item: str(rate * copies) for item, rate in self.rates.items()}}


def production_plan(problem, count):
    """固定配方链和目标速率，优化整数加工机与进货箱台数。"""
    items = (*problem.order, *problem.raw)
    n = len(items)
    index = {item: j for j, item in enumerate(items)}
    scale = Fraction(count, problem.count)
    # 每个物料有一个连续的批次速率（外供为件速率）和一个整数设备数。
    rows, lows, highs = [], [], []
    powers = {}

    def constrain(coefficients, low=-np.inf, high=np.inf):
        rows.append(coefficients)
        lows.append(low)
        highs.append(high)

    balance = [{j: problem.recipes[item]["quantity"] if item in problem.ranks else 1}
               for j, item in enumerate(items)]
    for j, item in enumerate(items):
        if item in problem.external:
            constrain({j: 1, n + j: -problem.box_capacity}, high=0)
            if item in problem.case.get("daily_supply", {}):
                maximum = Fraction(str(problem.case["daily_supply"][item])) * scale / DAY
                constrain({j: 1}, high=maximum)
            building = "inlet"
        else:
            recipe = problem.recipes[item]
            building = recipe["machine"]
            constrain({j: Fraction(str(recipe["seconds"])), n + j: -60}, high=0)
            if not recipe.get("virtual"):
                constrain({j: recipe["quantity"], n + j: -problem.capacity}, high=0)
            for material, amount in recipe["inputs"].items():
                balance[index[material]][j] = -amount
                constrain({j: amount, n + j: -problem.capacity}, high=0)
        powers[n + j] = Fraction(str(problem.buildings[building].get("power", 0)))
    for item, coefficients in zip(items, balance):
        net = problem.rate * scale if item == problem.target else 0
        constrain(coefficients, net, net)
    constrain({n + index[problem.target]: 1}, count, count)
    power_limit = None if problem.power_limit is None else Fraction(str(problem.power_limit)) * scale
    if power_limit is not None:
        constrain(powers, high=power_limit)

    row_ids, columns, values = [], [], []
    for i, row in enumerate(rows):
        for j, coefficient in row.items():
            row_ids.append(i)
            columns.append(j)
            values.append(float(coefficient))
    matrix = coo_matrix((values, (row_ids, columns)), shape=(len(rows), 2 * n)).tocsc()
    result = milp(np.r_[np.zeros(n), np.ones(n)], integrality=np.r_[np.zeros(n), np.ones(n)],
                  bounds=Bounds(np.zeros(2 * n), np.full(2 * n, np.inf)),
                  constraints=LinearConstraint(matrix, np.asarray(lows, float), np.asarray(highs, float)),
                  options={"time_limit": 1.0, "mip_rel_gap": 0.0})

    # 单配方无环链的流量可用精确分数复核，不把求解器容差当作额外产能。
    rates = {item: problem.demand[item] * scale for item in items}
    minimum = {item: (count if item == problem.target else ceil(rates[item] /
               (problem.box_capacity if item in problem.external else problem.machine_capacity(item)))) for item in items}
    power = sum(powers[n + index[item]] * minimum[item] for item in items)
    if power_limit is not None and power > power_limit:
        raise Infeasible(f"供需配平至少需要 {float(power * (problem.count / count)):g} kW，超过功率上限")
    solved = {item: round(result.x[n + j]) for item, j in index.items()} if result.x is not None else None
    status = int(result.status) if solved == minimum else -1
    gap = getattr(result, "mip_gap", None)
    return ProductionPlan(minimum if solved != minimum else solved, rates, power, status,
                          float(gap) if status >= 0 and gap is not None and np.isfinite(gap) else None)


def faster_belt_layout(problem, layout):
    """容量有余的低级带优先，有限升级预算用于接近饱和的物流格。"""
    if len(problem.belts) < 2:
        return None
    load = Counter()
    for edge in layout.segments:
        load[edge.end] += edge.rate
    timing = layout.statistics.get("simulation", {}).get("timing", {})
    observed = Counter()
    for machine in timing.get("machine_rows", ()):
        for deficient in machine["starved_inputs"]:
            feeder = deficient["feeder"]
            if feeder is not None and feeder["actual_load"] >= 0.8:
                observed[feeder["position"]] += deficient["seconds"]
    tiers = {kind: belt for belt in problem.belts for kind in (belt["building"], belt.get("underground")) if kind}
    tunnels = tunnel_segments(layout.segments, layout.transport)
    paired = {point for start, end, _ in tunnels for point in (start, end)}
    components = [((point,), kind, None) for point, kind in layout.transport.items() if point not in paired]
    components.extend(((start, end), kind, distance(start, end)) for start, end, kind in tunnels)
    candidates = []
    for points, kind, span in components:
        old = tiers.get(kind)
        if old is None:
            continue
        rate = load[points[-1]]
        if rate * 5 < old["capacity"] * 4 and not any(observed[point] for point in points):
            continue
        options = [belt for belt in belt_options(problem, rate, span=span) if belt["level"] >= old["level"]]
        if len(options) > 1:
            candidates.append((points, kind, span, rate, options))
    if not candidates:
        return None

    rows, cols, values, cost, variants = [], [], [], [], []
    for i, (points, kind, span, rate, options) in enumerate(candidates):
        old = tiers[kind]
        key = "building" if span is None else "underground"
        for belt in options:
            column = len(variants)
            variants.append((points, belt[key]))
            rows.extend((i, len(candidates)))
            cols.extend((column, column))
            values.extend((1, len(points) if belt[key] != kind else 0))
            # 同时降低高负荷与通过时间，升级一级足够时仍避免无条件选最高级。
            stress = max(Fraction(0), rate / Fraction(belt["capacity"]) - Fraction(4, 5))
            transit = belt_seconds(belt) / belt_seconds(old)
            weight = 1 + min(4, sum(observed[point] for point in points) / 30)
            cost.append(float(10 * stress + weight * transit / 4 + Fraction(belt["level"] - old["level"], 15)))
    n = len(candidates)
    budget = max(2, min(16, ceil(len(layout.transport) / 6)))
    matrix = coo_matrix((values, (rows, cols)), shape=(n + 1, len(variants))).tocsc()
    result = milp(cost, integrality=np.ones(len(variants)), bounds=Bounds(0, 1),
                  constraints=LinearConstraint(matrix, np.r_[np.ones(n), 0], np.r_[np.ones(n), budget]),
                  options={"time_limit": 0.5})
    if result.x is None:
        return None
    transport = layout.transport.copy()
    for (points, kind), chosen in zip(variants, result.x):
        if chosen > 0.5:
            transport.update(dict.fromkeys(points, kind))
    if transport == layout.transport:
        return None
    return replace(layout, transport=transport, statistics={}, baseline=False,
                   logistics_milp={"status": int(result.status), "budget": budget, "selected": True,
                                   "changed_cells": sum(transport[p] != kind for p, kind in layout.transport.items())},
                   profile=f"{layout.profile}／HiGHS 带速分配")
