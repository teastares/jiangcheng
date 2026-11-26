from collections import defaultdict, deque
from dataclasses import dataclass, field
from fractions import Fraction
from math import ceil, lcm, sqrt

from .blueprint import SPLIT_OUTPUTS
from .geometry import INLET_KINDS, assigned_ports, direction, inlet_output_ports
from .layout import distance
from .model import DAY, belt_options, belt_seconds


STEADY_MINUTES = 4


@dataclass(slots=True)
class _MachineState:
    item: str
    position: tuple[int, int]
    terminal: bool
    cycle: int
    inputs: list[tuple[tuple[int, int], str, int]]
    exit: tuple[int, int] | None
    quantity: int
    output_limit: int
    planned_ticks: float
    busy_until: int | None = None
    produced: int = 0
    last_start: int | None = None
    due: int = 0
    intervals: list[int] = field(default_factory=list)
    steady_batches: int = 0
    idle_input: int = 0
    blocked_output: int = 0
    starved: int = 0
    blocked: int = 0
    missing: dict[tuple[int, int], int] = field(default_factory=dict)


def simulate(problem, layout, *, ports=None):
    """在空线模型中跟踪逐件移动、生产批次与缓存在途量。

    ports 可给出每个进货箱和加工设备的实际端口，值为按配方排列的输入坐标和一个输出坐标。
    """
    steam_timing = problem.catalog.get("steam_belt_timing", False)
    placements = sorted(layout.placements, key=lambda placement: placement.anchor)
    used = set(layout.transport.values())
    outlet_belts = {p.anchor: belt_options(problem, p.rate)[0] for p in placements if p.building == "outlet"}
    used.update(belt["building"] for belt in outlet_belts.values())
    active_belts = [b for b in problem.belts if b["building"] in used or b.get("underground") in used]
    ticks_per_second = lcm(4, *((b["capacity"] // 60) * (2 if b.get("underground") in used else 1)
                                 for b in active_belts if type(b["capacity"]) is int and b["capacity"] >= 60))
    minute = 60 * ticks_per_second

    def ticks(seconds):
        return max(1, ceil(Fraction(str(seconds)) * ticks_per_second))

    incoming, outgoing, flow = defaultdict(list), defaultdict(list), defaultdict(Fraction)
    for segment in layout.segments:
        outgoing[segment.start].append(segment.end)
        incoming[segment.end].append(segment)
        flow[segment.start] += segment.rate
    for point, targets in outgoing.items():
        if steam_timing and layout.transport.get(point) == "split":
            headings = SPLIT_OUTPUTS[direction(incoming[point][0].start, point)]
            targets.sort(key=lambda end: headings.index(direction(point, end)))
        else:
            targets.sort()

    capacities = {key: Fraction(belt["capacity"]) for belt in problem.belts
                  for key in (belt["building"], belt.get("underground")) if key}
    durations = {key: ticks(belt_seconds(belt)) for belt in problem.belts
                 for key in (belt["building"], belt.get("underground")) if key}
    for key in ("merge", "split"):
        capacities[key] = problem.junction_capacity
        durations[key] = ticks(problem.catalog.get("junction_seconds", 0.5))

    queues = {cell: deque() for cell in layout.transport}
    next_move = dict.fromkeys(queues, 0)
    cursor = dict.fromkeys(queues, 0)
    spans = [(segment.start, segment.end) for segment in layout.segments if distance(segment.start, segment.end) > 1]
    # 地下带从入口移至出口中心，每端约走半格。所跨地面格不会增加运输时间。
    underground_ends = {cell for start, end in spans for cell in ((start, end) if steam_timing else (end,))}
    travel = {cell: ceil(durations[key] / 2) if cell in underground_ends else durations[key]
              for cell, key in layout.transport.items()}
    spacing = {cell: ceil(minute / capacities[key]) for cell, key in layout.transport.items()}
    buffer_override = problem.case.get("simulation_buffer_batches")
    buffer_cycles = int(buffer_override) if buffer_override is not None else (3 if steam_timing else 4)
    if buffer_cycles < 1:
        raise ValueError("仿真缓存批次数应为正整数")
    item_stacks = problem.catalog.get("item_stacks", {})
    slots = {cell: 1 if layout.transport[cell].startswith("tunnel") else max(2, ceil(travel[cell] / spacing[cell]) + 1)
             for cell in queues}
    if steam_timing:
        slots.update({cell: 3 for cell, kind in layout.transport.items() if kind in ("split", "merge")})

    inlets, outlets, machines = [], {}, []
    bin_sizes, bins = {}, {}

    def actual_ports(placement):
        return ports[placement] if ports is not None else assigned_ports(problem, placement, incoming)

    for p in placements:
        if p.building in INLET_KINDS:
            if p.building == "inlet":
                _, point = actual_ports(p)
                source_ports = (point,)
            else:
                source_ports = inlet_output_ports(problem, p)
            # 大型进货箱的端口共用同种箱内存货，按各自下游速度出货。
            inlets.extend((point, outgoing[point][0]) for point in source_ports if point in outgoing)
        elif p.building == "outlet":
            belt = outlet_belts[p.anchor]
            outlets[p.anchor] = (ticks(belt_seconds(belt)), ceil(minute / belt["capacity"]))
        else:
            recipe = problem.recipes[p.item]
            entries, exit_cell = actual_ports(p)
            inputs = [(cell, name, amount) for cell, (name, amount) in zip(entries, recipe["inputs"].items())]
            for cell, _, amount in inputs:
                bin_sizes[cell] = amount * buffer_cycles
                bins[cell] = 0
            cycle, quantity = ticks(recipe["seconds"]), recipe["quantity"]
            output_limit = (buffer_cycles * quantity if buffer_override is not None
                            else item_stacks.get(p.item, 4 * quantity))
            planned_ticks = float(minute * quantity / p.rate) if p.rate else float(cycle)
            machines.append(_MachineState(p.item, p.anchor, p.item == problem.target, cycle, inputs, exit_cell,
                                          quantity, output_limit, planned_ticks))

    outlet_items = {cell: deque() for cell in outlets}
    outlet_next = dict.fromkeys(outlets, 0)
    source_next = dict.fromkeys((point for point, _ in inlets), 0)
    # 机器和进货箱向相邻设备交接，采用接收端间隔。可选的进货限速另行叠加。
    receive_spacing = {**spacing, **{cell: delay[1] for cell, delay in outlets.items()}}
    supply_cap = problem.case.get("inlet_capacity")
    inlet_spacing = ceil(minute / Fraction(str(supply_cap))) if supply_cap is not None else 1
    observation = STEADY_MINUTES * minute
    steady_start = DAY * minute - observation
    first, total, late_total = None, 0, 0
    transfer_counts = dict.fromkeys((*queues, *outlets), 0)
    blocked_ticks = dict.fromkeys(queues, 0)
    arrivals = dict.fromkeys(bins, 0)
    last_arrival = {}
    arrival_gap = dict.fromkeys(bins, 0)

    def deliver(cell, now):
        if cell in queues and len(queues[cell]) < slots[cell]:
            queues[cell].append(now + travel[cell])
            return True
        if cell in bins and bins[cell] < bin_sizes[cell]:
            bins[cell] += 1
            if now > steady_start:
                arrivals[cell] += 1
                if cell in last_arrival:
                    arrival_gap[cell] = max(arrival_gap[cell], now - last_arrival[cell])
                last_arrival[cell] = now
            return True
        if cell in outlets and now >= outlet_next[cell]:
            delay, interval = outlets[cell]
            outlet_next[cell] = now + interval
            outlet_items[cell].append(now + delay)
            return True
        return False

    # 用坐标固定顺序，经 JSON 回读和原地生成使用同一套时序。
    # 每帧交替处理方向，避免同一合并器输入一直优先获得空位。
    belt_cells = sorted(queues)
    for now in range(DAY * minute + 1):
        steady = now > steady_start
        for cell, queue in outlet_items.items():
            while queue and queue[0] <= now:
                queue.popleft()
                total += 1
                if steady:
                    transfer_counts[cell] += 1
        for machine in machines:
            if machine.busy_until is not None:
                if machine.busy_until > now:
                    continue
                if machine.exit is None:
                    if machine.terminal:
                        total += machine.quantity
                elif machine.produced + machine.quantity <= machine.output_limit:
                    machine.produced += machine.quantity
                else:
                    if steady:
                        machine.blocked_output += 1
                        if now >= machine.due:
                            machine.blocked += 1
                    continue
                if steam_timing:
                    for cell, _, amount in machine.inputs:
                        bins[cell] -= amount
                machine.busy_until = None
            if all(bins[cell] >= amount for cell, _, amount in machine.inputs):
                if not steam_timing:
                    for cell, _, amount in machine.inputs:
                        bins[cell] -= amount
                if steady:
                    machine.steady_batches += 1
                    if machine.last_start is not None and machine.last_start > steady_start:
                        machine.intervals.append(now - machine.last_start)
                machine.last_start = now
                # 游戏有料便开工。计划间隔只用于区分设计余量与超计划的等待。
                machine.due = now + ceil(machine.planned_ticks)
                machine.busy_until = now + machine.cycle
            elif steady:
                machine.idle_input += 1
                if now >= machine.due:
                    machine.starved += 1
                    for cell, _, amount in machine.inputs:
                        if bins[cell] < amount:
                            machine.missing[cell] = machine.missing.get(cell, 0) + 1
        order = belt_cells if now % 2 else reversed(belt_cells)
        for cell in order:
            queue = queues[cell]
            if not queue or queue[0] > now or now < next_move[cell]:
                continue
            targets = outgoing[cell]
            # Steam 分流器逐帧轮询。模型时间步内合并连续失败的尝试，仍按设备的端口顺序移交。
            for offset in range(len(targets)):
                current = (cursor[cell] + offset) % len(targets)
                target = targets[current]
                if deliver(target, now):
                    queue.popleft()
                    cursor[cell] = (current + 1) % len(targets)
                    next_move[cell] = now + spacing[cell]
                    if steady:
                        transfer_counts[cell] += 1
                    break
            else:
                if steady:
                    blocked_ticks[cell] += 1
        for machine in machines:
            cell = machine.exit
            if cell is not None and machine.produced and now >= source_next.get(cell, 0) and deliver(outgoing[cell][0], now):
                machine.produced -= 1
                source_next[cell] = now + receive_spacing.get(outgoing[cell][0], 1)
        for point, target in inlets:
            if now >= source_next[point] and deliver(target, now):
                source_next[point] = now + max(inlet_spacing, receive_spacing.get(target, 1))
        if first is None and total:
            first = now / ticks_per_second
        if now == steady_start:
            late_total = total

    transport_rows = {}
    for cell, kind in sorted(layout.transport.items()):
        transport_rows[cell] = _transport_sample(cell, kind, capacities[kind], flow[cell], transfer_counts[cell],
                                                 blocked_ticks[cell] / ticks_per_second, travel[cell] / ticks_per_second,
                                                 incoming[cell])
    for p in placements:
        if p.anchor in outlet_belts:
            delay, _ = outlets[p.anchor]
            belt = outlet_belts[p.anchor]
            transport_rows[p.anchor] = _transport_sample(p.anchor, "outlet", belt["capacity"], p.rate,
                                                         transfer_counts[p.anchor], 0, delay / ticks_per_second,
                                                         incoming[p.anchor])
    machine_rows = [_machine_sample(machine, ticks_per_second, incoming, transport_rows, arrivals, arrival_gap)
                    for machine in machines]
    return {
        "first_seconds": first,
        "first_day": total,
        "last_four_per_minute": (total - late_total) / STEADY_MINUTES,
        "timing": {
            "measurement_seconds": observation / ticks_per_second,
            "machine_rows": machine_rows,
            "machine_delay_seconds": sum(row["extra_starved_seconds"] + row["extra_blocked_seconds"]
                                         for row in machine_rows),
            "worst_machine_delay_seconds": max((row["extra_starved_seconds"] + row["extra_blocked_seconds"]
                                                for row in machine_rows), default=0),
            "max_excess_gap_seconds": max((row["excess_gap_seconds"] for row in machine_rows), default=0),
            "max_start_jitter_seconds": max((row["start_jitter_rms_seconds"] or 0 for row in machine_rows), default=0),
            "max_belt_load": max((row["load"] for row in transport_rows.values()
                                  if row["kind"] not in ("split", "merge")), default=0),
            "bottlenecks": sorted(transport_rows.values(), key=_transport_priority, reverse=True)[:8],
        },
    }


def _transport_sample(point, kind, capacity, planned, transfers, blocked, travel, incoming):
    return {
        "position": point,
        "kind": kind,
        "item": next((segment.item for segment in incoming), ""),
        "planned_items_per_minute": float(planned),
        "rated_items_per_minute": float(capacity),
        "actual_items_per_minute": transfers / STEADY_MINUTES,
        "load": float(planned / capacity),
        "actual_load": float(transfers / (STEADY_MINUTES * capacity)),
        "blocked_seconds": blocked,
        "travel_seconds": travel,
    }


def _transport_priority(row):
    return row["load"], row["actual_load"], row["blocked_seconds"]


def _machine_sample(machine, ticks_per_second, incoming, transport_rows, arrivals, arrival_gap):
    planned = machine.planned_ticks
    intervals = machine.intervals
    inputs = [{
        "item": name,
        "planned_items_per_minute": 60 * ticks_per_second * amount / planned,
        "actual_items_per_minute": arrivals[cell] / STEADY_MINUTES,
        "max_arrival_gap_seconds": arrival_gap[cell] / ticks_per_second if arrivals[cell] > 1 else None,
        "seconds": machine.missing.get(cell, 0) / ticks_per_second,
        "feeder": _feeder(cell, incoming, transport_rows) if cell in machine.missing else None,
    } for cell, name, amount in machine.inputs]
    return {
        "item": machine.item,
        "position": machine.position,
        "recipe_seconds": machine.cycle / ticks_per_second,
        "planned_period_seconds": planned / ticks_per_second,
        "mean_start_interval_seconds": sum(intervals) / (len(intervals) * ticks_per_second) if intervals else None,
        "max_start_interval_seconds": max(intervals) / ticks_per_second if intervals else None,
        "excess_gap_seconds": max(0, max(intervals) - planned) / ticks_per_second if intervals else 0,
        "start_jitter_rms_seconds": (sqrt(sum((elapsed - planned) ** 2 for elapsed in intervals) / len(intervals))
                                     / ticks_per_second if intervals else None),
        "actual_started_items_per_minute": machine.steady_batches * machine.quantity / STEADY_MINUTES,
        "idle_input_seconds": machine.idle_input / ticks_per_second,
        "blocked_output_seconds": machine.blocked_output / ticks_per_second,
        "extra_starved_seconds": machine.starved / ticks_per_second,
        "extra_blocked_seconds": machine.blocked / ticks_per_second,
        "inputs": inputs,
        "starved_inputs": [row for row in inputs if row["seconds"]],
    }


def _feeder(input_cell, incoming, transport_rows):
    # 仅沿缺料口的上游运输网回溯，遇到另一个生产设备或进货口即停止。
    frontier = [input_cell]
    visited = set(frontier)
    rows = []
    while frontier:
        for segment in incoming[frontier.pop()]:
            cell = segment.start
            if cell in transport_rows and cell not in visited:
                visited.add(cell)
                rows.append(transport_rows[cell])
                frontier.append(cell)
    return max(rows, key=_transport_priority, default=None)
