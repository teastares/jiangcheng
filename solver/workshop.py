"""按存档解锁生成布局，并计算建造和外供需求。"""

import json
from collections import Counter, defaultdict
from fractions import Fraction
from html import escape
from math import ceil
from pathlib import Path
from time import perf_counter

from .blueprint import FORMAT, IDS
from .layout import solve
from .model import DAY, Infeasible, Problem
from .report import MODE_NAMES, OBJECTIVE_NAMES, choose, write_layout
from .simulate import simulate


GEOMETRY = {
    "inlet": ((2, 1), (0, 0), (), 0),
    "outlet": ((1, 1), (0, 0), (0,), None),
    "maker": ((2, 1), (0, 0), (0,), 0),
    "assembler": ((3, 2), (1, 1), (0, 1), 0),
    "synthesizer": ((3, 3), (1, 2), (0, 1, 2), 2),
    "smelter": ((2, 1), (0, 0), (0,), 0),
    "forge": ((3, 3), (1, 2), (0, 1), 1),
    "charger": ((3, 2), (1, 1), (0, 1), 0),
    "exchanger": ((3, 2), (0, 1), (0, 1), None),
    "grain_mill": ((2, 3), (1, 2), (0,), 2),
}
DEVICES = {IDS[k]: k for k in GEOMETRY if k not in ("inlet", "outlet")}
BELT_SPEEDS = (1, 2, 4, 8, 12)
UNDERGROUND_SPANS = (5, 6, 7, 8, 8)
BLUEPRINT_NODE = 15
DATABASE = Path(__file__).with_name("steam_database.json")


def read_database():
    return json.loads(DATABASE.read_text(encoding="utf-8"))


def configured_external_items(configuration):
    items = configuration.get("external", ())
    legacy = configuration.get("manual_daily", {})
    if not isinstance(items, (list, tuple)) or not isinstance(legacy, dict):
        raise ValueError("配置的外供物料应为名称列表")
    if any(not isinstance(item, str) or not item.strip() for item in (*items, *legacy)):
        raise ValueError("外供物料名称不能为空")
    # 旧配置中的外供件数已停用，保留用户指定的物料。
    return list(dict.fromkeys(item.strip() for item in (*items, *legacy)))


class Workshop:
    def __init__(self, profile, database=None):
        self.profile = profile
        self.db = database or read_database()
        if self.db["format"] != profile.version:
            raise ValueError("存档格式与当前参数库不匹配")
        self.items = self.db["items"]
        self.item_ids = {v["name"]: int(key) for key, v in self.items.items()}
        self.presets = {self.name(row["item"]): row for row in self.db["presets"] if row["item"] is not None}
        produced = {item for recipe in self.db["formulas"] for item, _ in recipe["outputs"]}
        self.raw_items = set(self.item_ids.values()) - produced
        self.variants = defaultdict(list)
        for recipe in self.db["formulas"]:
            if len(recipe["outputs"]) == 1:
                item, _ = recipe["outputs"][0]
                self.variants[item].append(recipe)

    def name(self, item):
        return self.items.get(str(item), {}).get("name", f"物品 {item}")

    def building_name(self, ident):
        return self.db["buildings"].get(str(ident), {}).get("name", f"建筑 {ident}")

    def alternatives(self, item):
        for formula in self.variants.get(self.item_ids.get(item), []):
            if formula["id"] not in self.profile.recipes or formula["ticks"] <= 0:
                continue
            for ident in formula["devices"]:
                if ident in DEVICES and ident in self.profile.buildings and not self.db["buildings"][str(ident)]["water"]:
                    geometry = GEOMETRY[DEVICES[ident]]
                    if 0 < len(formula["inputs"]) <= len(geometry[2]):
                        yield formula, ident

    def devices_for(self, item):
        return {self.building_name(device): device for _, device in self.alternatives(item)}

    def unavailable(self, item):
        preset = self.presets.get(item)
        if preset is not None:
            if not set(preset["recipes"]) & self.profile.recipes:
                return f"{item}配方未解锁（{preset['unlock']}）"
            return f"{item}缺少可用的自动生产设备（Wiki 设备：{preset['devices']}）"
        return f"{item}缺少当前可用的自动生产配方"

    def catalog(self, target, terminal):
        buildings = {}
        for key, ident in IDS.items():
            data = self.db["buildings"].get(str(ident))
            if data is not None:
                buildings[key] = {"name": data["name"], "power": data["power"],
                                  "materials": {self.name(i): n for i, n in data["materials"]}}
        for key, (size, anchor, inputs, output) in GEOMETRY.items():
            definition = buildings.setdefault(key, {"name": "成品接出带"})
            definition.update(size=size, anchor=anchor, inputs=inputs)
            if output is not None:
                definition["output"] = output
        belts = []
        for tier, speed in enumerate(BELT_SPEEDS, 1):
            key, underground = f"belt{tier}", f"tunnel{tier}"
            if IDS[key] in self.profile.buildings:
                spec = {"level": tier, "capacity": speed * 60, "seconds_per_cell": str(Fraction(1, speed)), "building": key}
                if IDS[underground] in self.profile.buildings:
                    spec["underground"] = underground
                    spec["underground_span"] = UNDERGROUND_SPANS[tier - 1]
                belts.append(spec)
        recipes = {}
        for item in self.variants:
            name = self.name(item)
            candidates = list(self.alternatives(name))
            if name == target and terminal is not None:
                candidates = [option for option in candidates if option[1] == terminal]
            if not candidates:
                continue

            def key(option):
                formula, ident = option
                geometry = GEOMETRY[DEVICES[ident]][0]
                definition = buildings[DEVICES[ident]]
                return (len(definition["inputs"]), geometry[0] * geometry[1],
                        definition["power"], formula["ticks"], formula["id"])

            formula, ident = min(candidates, key=key)
            recipes[name] = {"id": formula["id"], "machine": DEVICES[ident], "seconds": str(Fraction(formula["ticks"], 60)),
                             "quantity": formula["outputs"][0][1], "inputs": {self.name(i): n for i, n in formula["inputs"]},
                             "virtual": ident == 61}
        return {"platform": f"Steam Build {self.db['steam_build']}", "blueprint_format": FORMAT,
                "buildings": buildings, "belts": belts, "junction_capacity": 120, "junction_ports": 3,
                "recipes": recipes}

    def configure(self, configuration):
        if BLUEPRINT_NODE not in self.profile.nodes:
            raise Infeasible("T3 第 5 节点「蓝图」还未修复")
        for ident in (IDS["inlet"], IDS["wire"]):
            if ident not in self.profile.buildings:
                raise Infeasible(f"{self.building_name(ident)}尚未解锁")
        target = configuration["target"]
        if target not in self.presets:
            raise ValueError("请选择 Wiki 产物预设")
        terminal = configuration.get("terminal")
        if terminal is not None and terminal not in self.devices_for(target).values():
            raise Infeasible(f"所选末端机型 {self.building_name(terminal)} 在当前存档不能生产 {target}")
        catalog = self.catalog(target, terminal)
        if target not in catalog["recipes"]:
            raise Infeasible(self.unavailable(target))
        tiers = configuration.get("belt_levels")
        if tiers is not None:
            if not isinstance(tiers, (list, tuple)) or not tiers or any(type(tier) is not int or not 1 <= tier <= 5 for tier in tiers):
                raise ValueError("允许传送带等级应为 1 至 5 的列表")
            locked = set(tiers) - {belt["level"] for belt in catalog["belts"]}
            if locked:
                raise Infeasible("当前存档未解锁这些传送带等级：" + "、".join(map(str, sorted(locked))))
        specified_external = set(configured_external_items(configuration))
        for item in sorted(specified_external):
            if item == target or item not in self.item_ids:
                raise ValueError(f"外供物料「{item}」应为游戏中除目标产物外的物品")
        external, visited = set(), set()

        def visit(item):
            if item in visited:
                return
            visited.add(item)
            if item in specified_external or self.item_ids.get(item) in self.raw_items:
                external.add(item)
                return
            if item not in catalog["recipes"]:
                raise Infeasible(self.unavailable(item) + "。可核对科技与机器，或将该中间品指定为外供物料。")
            for material in catalog["recipes"][item]["inputs"]:
                visit(material)

        visit(target)
        case = {key: configuration[key] for key in ("target", "machines", "rate", "plot", "power_limit_kw", "objective", "modes",
                                                   "belt_levels", "simulation_candidates") if configuration.get(key) is not None}
        case["external"] = sorted(external)
        case.setdefault("simulation_candidates", 12)
        return Problem(catalog, case)

    def run(self, configuration, row_seconds=0.10):
        started = perf_counter()
        problem = self.configure(configuration)
        problem.case["requested_belt_levels"] = configuration.get("belt_levels")
        full = problem
        closest_locked = None
        below_target = False

        def accept(layout):
            nonlocal closest_locked
            locked = [(key, count) for key, count in layout.statistics["buildings"].items()
                      if IDS[key] not in self.profile.buildings]
            if not locked:
                return True
            if closest_locked is None or len(locked) < len(closest_locked):
                closest_locked = locked
            return False

        tiers = [belt["level"] for belt in problem.belts]
        for index in range(len(tiers)):
            if index:
                try:
                    problem = Problem(full.catalog, {**full.case, "belt_levels": tiers[index:]})
                except Infeasible:
                    continue
            results = solve(problem, row_seconds, simulate, accept)
            winner = choose(problem, results)
            if winner is not None:
                if results[winner][0].statistics["simulation"]["last_four_per_minute"] >= float(problem.rate):
                    problem.case["solve_seconds"] = perf_counter() - started
                    return problem, results, winner
                below_target = True
        if below_target:
            raise Infeasible("通过静态检查的方案在空线仿真末段未达目标产速。可增加仿真候选数或调整地块与产量")
        if closest_locked:
            missing = "、".join(f"{self.building_name(IDS[key])} × {count}" for key, count in closest_locked)
            raise Infeasible(f"最接近的候选布局仍需要 {missing}，这些建筑尚未解锁。")
        raise Infeasible("当前搜索范围内未找到满足地块、电力和物流上限的布局")


def write_workshop_report(directory, problem, results, winner):
    layout = results[winner][0]
    objectives = " → ".join(OBJECTIVE_NAMES[key] for key in problem.objective)
    width, height = (f"{limit} 格" if limit is not None else "不限" for limit in problem.plot_limits)
    power = f"{problem.power_limit:g} kW" if problem.power_limit is not None else "不限"
    parts = [f"<section><h2>比较方案</h2><p>优化优先级 {objectives}。已选 {MODE_NAMES[winner]}。</p>",
             f"<p>本次净空上限为宽 {width}、高 {height}。电网功率上限为 {power}。</p>",
             "<table><thead><tr><th>供料方式</th><th>占地</th><th>加工设备</th><th>建造胶囊</th><th>物流格</th>"
             "<th>电线格</th><th>建材件数</th><th>进货口</th><th>功耗</th><th>仿真末段</th></tr></thead><tbody>"]
    for mode, (candidate, _) in results.items():
        if candidate is None:
            status = "本轮未搜索" if mode not in problem.case.get("modes", MODE_NAMES) else "未找到可建布局"
            parts.append(f'<tr><td>{MODE_NAMES[mode]}</td><td colspan="9">{status}</td></tr>')
            continue
        stats = candidate.statistics
        rate = stats["simulation"]["last_four_per_minute"]
        parts.append(f"<tr><td>{MODE_NAMES[mode]}</td><td>{candidate.width} × {candidate.height}</td><td>{stats['machines']}</td>"
                     f"<td>{stats['components']}</td><td>{stats['transport']}</td><td>{stats['wires']}</td>"
                     f"<td>{sum(stats['materials'].values())}</td><td>{stats['buildings']['inlet']}</td><td>{stats['power']} kW</td>"
                     f"<td>{rate:g} 件/分钟</td></tr>")
    parts.append("</tbody></table><p>已按存档核对所用配方、设备和物流等级的解锁状态。</p>"
        "</section><section><h2>外供原料需求</h2><p>布局按外部持续供料计算。</p>"
        "<table><thead><tr><th>供给物料</th><th>每日备货量（逐口取整）</th></tr></thead><tbody>")
    delivery = Counter()
    for p in layout.placements:
        if p.building == "inlet":
            delivery[p.item] += ceil(p.rate * DAY)
    parts.extend(f"<tr><td>{escape(item)}</td><td>{amount} 件/日</td></tr>"
                 for item, amount in sorted(delivery.items()))
    parts.append("</tbody></table><p>按「进货口与日配额」一节持续补货。单口日用量超过单箱容量时可分批补货。</p></section>")
    write_layout(directory, "blueprint", problem, layout, winner, "".join(parts))
    return directory / "blueprint.html"
