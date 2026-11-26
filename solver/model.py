from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, isfinite


DAY = 16
DEFAULT_OBJECTIVE = ("area", "buildings", "lines", "cadence", "materials", "power")
OBJECTIVES = {*DEFAULT_OBJECTIVE, "inlets", "machines", "transport_time"}


class Infeasible(ValueError):
    """给定约束下无法构成候选产线。"""


@dataclass(frozen=True)
class Node:
    id: int
    kind: str
    item: str
    rank: int
    rate: Fraction


@dataclass(frozen=True)
class Port:
    node: int
    row: int
    rate: Fraction


@dataclass(frozen=True)
class Bus:
    item: str
    sources: tuple[Port, ...]
    sinks: tuple[Port, ...]


@dataclass
class Network:
    nodes: list[Node]
    buses: list[Bus]


def positive(value, name):
    try:
        number = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError(f"{name}应为正数") from error
    if number <= 0:
        raise ValueError(f"{name}应为正数")
    return number


def belt_options(problem, rate, *, span=None):
    """按等级列出能持续承载流量的带，提供跨度时筛选地下带。"""
    options = (belt for belt in problem.belts if belt["capacity"] >= rate and
               (span is None or problem.underground_spans.get(belt.get("underground"), 0) >= span))
    return sorted(options, key=belt_order)


def belt_order(belt):
    return belt.get("level", belt["capacity"]), belt["capacity"]


def belt_seconds(belt):
    if "seconds_per_cell" in belt:
        return Fraction(str(belt["seconds_per_cell"]))
    return Fraction(60) / Fraction(str(belt["capacity"]))


def transport_time(problem, layout):
    """按额定流量和通过时间估算输送中的件数，忽略排队和加工。"""
    durations = {kind: belt_seconds(belt) for belt in problem.belts
                 for kind in (belt["building"], belt.get("underground")) if kind}
    junction_seconds = Fraction(str(problem.catalog.get("junction_seconds", 0.5)))
    durations.update(split=junction_seconds, merge=junction_seconds)
    outlet_seconds = {}
    for placement in layout.placements:
        if placement.building == "outlet":
            options = belt_options(problem, placement.rate)
            if not options:
                raise Infeasible("成品接出口超出传送带容量")
            outlet_seconds[placement.anchor] = belt_seconds(options[0])
    total = Fraction(0)
    for edge in layout.segments:
        seconds = durations.get(layout.transport.get(edge.end), outlet_seconds.get(edge.end, 0))
        if seconds:
            span = abs(edge.start[0] - edge.end[0]) + abs(edge.start[1] - edge.end[1])
            # Steam 入口和出口各走半格，地下跨度不再重复计时。
            half = span > 1 or (problem.catalog.get("steam_belt_timing") and
                                layout.transport.get(edge.end, "").startswith("tunnel"))
            total += edge.rate * seconds / (120 if half else 60)
    return total


class Problem:
    def __init__(self, catalog, case):
        self.case = case
        self.catalog = catalog
        priority = tuple(case.get("objective", DEFAULT_OBJECTIVE))
        if not priority or len(set(priority)) != len(priority) or not set(priority) <= OBJECTIVES:
            raise ValueError("优化优先级含未知或重复指标")
        self.objective = priority + tuple(key for key in DEFAULT_OBJECTIVE if key not in priority)
        self.target = case["target"]
        self.count = case["machines"]
        if type(self.count) is not int or self.count < 1:
            raise ValueError("末端机器数应为正整数")
        plot = case.get("plot")
        if plot is not None and (not isinstance(plot, (list, tuple)) or len(plot) != 2 or
                                 any(value is not None and (type(value) is not int or value < 1) for value in plot)):
            raise ValueError("plot 的宽高应各填正整数或 null")
        self.plot_limits = tuple(plot) if plot is not None else (None, None)
        power = case.get("power_limit_kw")
        self.power_limit = float(power) if power is not None else None
        if self.power_limit is not None and (not isfinite(self.power_limit) or self.power_limit < 0):
            raise ValueError("功率上限应为非负的有限数字")
        self.simulation_candidates = case.get("simulation_candidates", 32)
        if type(self.simulation_candidates) is not int or self.simulation_candidates < 1:
            raise ValueError("仿真候选数量应为正整数")
        self.external = frozenset(case["external"])
        if self.target in self.external:
            raise ValueError("目标产物不能设为外供")
        self.recipes = catalog["recipes"]
        self.buildings = catalog["buildings"]
        self.belts = sorted(catalog["belts"], key=belt_order)
        if "belt_levels" in case:
            self.belts = [belt for belt in self.belts if belt["level"] in case["belt_levels"]]
        if not self.belts:
            raise Infeasible("没有已解锁传送带")
        self.underground_spans = {}
        for belt in self.belts:
            if belt.get("underground") and case.get("underground", True):
                span = belt.get("underground_span", 2)
                if type(span) is not int or span < 2:
                    raise ValueError("地下带最大跨度应为至少 2 格的整数")
                self.underground_spans[belt["underground"]] = span
        self.capacity = max(positive(belt["capacity"], "传送带容量") for belt in self.belts)
        self.junction_capacity = positive(case.get("junction_capacity", catalog["junction_capacity"]), "分合流容量")
        self.junction_ports = catalog.get("junction_ports", 2)
        if type(self.junction_ports) is not int or self.junction_ports not in (2, 3):
            raise ValueError("分合流的分支数应为 2 或 3")
        self.box_capacity = min(self.capacity, positive(case.get("inlet_capacity", self.capacity), "进货口容量"))
        enabled = set(case.get("enabled_recipes", self.recipes))
        self.ranks = {}
        visiting = set()

        def visit(item):
            if item in self.external:
                return 0
            if item in visiting:
                raise ValueError(f"配方有循环依赖：{item}")
            if item not in self.ranks:
                if item not in self.recipes or item not in enabled:
                    raise Infeasible(f"{item}缺少已解锁配方或外供来源")
                visiting.add(item)
                recipe = self.recipes[item]
                geometry = self.buildings[recipe["machine"]]
                inputs = recipe["inputs"]
                if not inputs or len(inputs) > len(geometry["inputs"]):
                    raise ValueError(f"{item}的配方与机器输入端口不匹配")
                if recipe.get("virtual") and item != self.target:
                    raise Infeasible(f"{item}直接入账，无法用于下游物流")
                if not recipe.get("virtual") and "output" not in geometry:
                    raise ValueError(f"{item}缺少机器输出端口")
                for material, amount in [(item, recipe["quantity"]), *inputs.items()]:
                    if type(amount) is not int or amount < 1:
                        raise ValueError(f"{material}每批数量应为正整数")
                positive(recipe["seconds"], f"{item}生产周期")
                self.ranks[item] = 1 + max(visit(material) for material in inputs)
                visiting.remove(item)
            return self.ranks[item]

        visit(self.target)
        self.order = sorted(self.ranks, key=self.ranks.get, reverse=True)
        rated = self.rated(self.target) * self.count
        self.rate = positive(case.get("rate", rated), "目标产量")
        if self.rate > rated or self.rate / self.count > self.machine_capacity(self.target):
            raise Infeasible("目标产量超过末端机器或单机端口的容量")
        demand = defaultdict(Fraction, {self.target: self.rate})
        for item in self.order:
            recipe = self.recipes[item]
            for material, count in recipe["inputs"].items():
                demand[material] += demand[item] * count / recipe["quantity"]
        self.demand = demand
        self.raw = {item: demand[item] for item in sorted(self.external & demand.keys())}
        for item, maximum in case.get("daily_supply", {}).items():
            if DAY * self.raw.get(item, 0) > Fraction(str(maximum)):
                raise Infeasible(f"{item}的 16 分钟用量超过供给上限")

    def rated(self, item):
        recipe = self.recipes[item]
        return Fraction(60) * recipe["quantity"] / positive(recipe["seconds"], "生产周期")

    def machine_capacity(self, item):
        recipe = self.recipes[item]
        input_bound = self.capacity * recipe["quantity"] / max(recipe["inputs"].values())
        output_bound = self.rated(item) if recipe.get("virtual") else self.capacity
        return min(self.rated(item), input_bound, output_bound)

    def geometry(self, node):
        key = self.recipes[node.item]["machine"] if node.kind == "machine" else node.kind
        return self.buildings[key]


def partition(ports, bus_limit, unit_limit, junction_limit, objective, raw=False, families=None):
    # 在固定端口顺序内穷举分割点，用动态规划选择分组。
    best = [(0, 0, 0, Fraction(0), [])]
    for end in range(1, len(ports) + 1):
        total = Fraction(0)
        choices = []
        branch_types = set()
        for start in range(end - 1, -1, -1):
            total += ports[start].rate
            if total > bus_limit:
                break
            if families is not None:
                branch_types.add(families[start])
            units = 1 if raw else ceil(total / unit_limit)
            if end - start + units > 2 and total > junction_limit:
                continue
            machines, groups, mixed, squared_load, cuts = best[start]
            choices.append((machines + units, groups + 1, mixed + max(0, len(branch_types) - 1),
                            squared_load + total * total, cuts + [(start, end, total, units)]))
        if not choices:
            raise Infeasible("单个端口的需求超过物流容量")
        if objective == "local":
            best.append(min(choices, key=lambda c: (c[0] + c[1], -c[1])))
        elif objective == "direct":
            # 同样的机器数下，多一组独立连接可以减少合流再分流的负担。
            # 外供组的机器数是进货箱数，同等箱数优先保持原料支线独立。
            best.append(min(choices, key=lambda c: (c[0], c[2], -c[1], c[3])))
        else:
            best.append(min(choices, key=lambda c: (c[0], c[1])))
    return best[-1][4]


def make_network(problem, count, strategy, limit, reverse=False, plan=None):
    nodes, buses = [], []
    required = defaultdict(list)

    def add(kind, item, rank, rate):
        node = Node(len(nodes), kind, item, rank, rate)
        nodes.append(node)
        if kind == "machine":
            recipe = problem.recipes[item]
            for row, (material, amount) in zip(problem.geometry(node)["inputs"], recipe["inputs"].items()):
                required[material].append(Port(node.id, row, rate * amount / recipe["quantity"]))
        return node

    final_rate = problem.rate / problem.count
    terminal = [add("machine", problem.target, problem.ranks[problem.target], final_rate) for _ in range(count)]
    if not problem.recipes[problem.target].get("virtual"):
        sources = tuple(Port(n.id, problem.geometry(n)["output"], n.rate) for n in terminal)
        export_limit = min(problem.capacity, max(limit, final_rate))
        for start, end, rate, _ in partition(sources, export_limit, export_limit, problem.junction_capacity, strategy, raw=True):
            sink = add("outlet", problem.target, problem.ranks[problem.target] + 1, rate)
            buses.append(Bus(problem.target, sources[start:end], (Port(sink.id, 0, rate),)))

    for item in [i for i in problem.order if i != problem.target] + list(problem.raw):
        consumers = required[item]
        if reverse:
            consumers = list(reversed(consumers))
        raw = item in problem.external
        unit = problem.box_capacity if raw else problem.machine_capacity(item)
        allowed = min(problem.capacity, unit) if raw else problem.capacity
        bus_limit = min(allowed, max(limit, max(p.rate for p in consumers)))
        junction_limit = problem.junction_capacity
        tactic = strategy
        if strategy == "distributed":
            if raw:
                tactic = "direct"
            else:
                # 多源多汇沿途取料时，干线总流量可以超过单段上限。布线后按实际负荷逐段检查。
                bus_limit *= 2
                junction_limit *= 2
        families = [nodes[p.node].item for p in consumers] if raw and tactic == "direct" else None
        total = sum(port.rate for port in consumers)
        if (strategy == "pooled" and plan is not None and plan.units[item] == 1 and
                total <= bus_limit and (len(consumers) == 1 or total <= junction_limit)):
            groups = [(0, len(consumers), total, plan.units[item])]
        elif (strategy == "pooled" and plan is not None and not raw and total <= bus_limit and
              total <= junction_limit and total <= unit * plan.units[item]):
            groups = [(0, len(consumers), total, plan.units[item])]
        else:
            groups = partition(consumers, bus_limit, unit, junction_limit, tactic, raw, families)
        for start, end, rate, number in groups:
            sources = []
            for _ in range(number):
                node = add("inlet" if raw else "machine", item, 0 if raw else problem.ranks[item], rate / number)
                sources.append(Port(node.id, problem.geometry(node)["output"], node.rate))
            buses.append(Bus(item, tuple(sources), tuple(consumers[start:end])))
    return Network(nodes, buses)
