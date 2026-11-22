from collections import defaultdict, deque
from fractions import Fraction
from math import ceil, lcm

from .geometry import assigned_ports
from .layout import distance
from .model import DAY, belt_options, belt_seconds


def simulate(problem, layout, *, ports=None):
    """在空线模型中跟踪逐件移动、生产批次与缓存在途量。

    ports 可给出每个进货箱和加工设备的实际端口，值为按配方排列的输入坐标和一个输出坐标。
    """
    used = set(layout.transport.values())
    outlet_belts = {p.anchor: belt_options(problem, p.rate)[0] for p in layout.placements if p.building == "outlet"}
    used.update(belt["building"] for belt in outlet_belts.values())
    active_belts = [b for b in problem.belts if b["building"] in used or b.get("underground") in used]
    ticks_per_second = lcm(4, *((b["capacity"] // 60) * (2 if b.get("underground") in used else 1)
                                 for b in active_belts if type(b["capacity"]) is int and b["capacity"] >= 60))
    minute = 60 * ticks_per_second

    def ticks(seconds):
        return max(1, ceil(Fraction(str(seconds)) * ticks_per_second))

    incoming, outgoing = defaultdict(list), defaultdict(list)
    for segment in layout.segments:
        outgoing[segment.start].append(segment.end)
        incoming[segment.end].append(segment)
    for targets in outgoing.values():
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
    underground_exits = {s.end for s in layout.segments if distance(s.start, s.end) > 1}
    travel = {cell: ceil(durations[key] / 2) if cell in underground_exits else durations[key]
              for cell, key in layout.transport.items()}
    spacing = {cell: ceil(minute / capacities[key]) for cell, key in layout.transport.items()}
    buffer_cycles = int(problem.case.get("simulation_buffer_batches", 4))
    if buffer_cycles < 1:
        raise ValueError("仿真缓存批次数应为正整数")
    slots = {cell: 1 if layout.transport[cell].startswith("tunnel") else max(2, ceil(travel[cell] / spacing[cell]) + 1)
             for cell in queues}

    inlets, outlets, machines = [], {}, []
    bin_sizes, bins = {}, {}

    def actual_ports(placement):
        return ports[placement] if ports is not None else assigned_ports(problem, placement, incoming)

    for p in sorted(layout.placements, key=lambda placement: placement.anchor):
        if p.building == "inlet":
            _, point = actual_ports(p)
            inlets.append((point, outgoing[point][0]))
        elif p.building == "outlet":
            belt = outlet_belts[p.anchor]
            outlets[p.anchor] = (ticks(belt_seconds(belt)), ceil(minute / belt["capacity"]))
        else:
            recipe = problem.recipes[p.item]
            amounts = list(recipe["inputs"].values())
            entries, exit_cell = actual_ports(p)
            for cell, amount in zip(entries, amounts):
                bin_sizes[cell] = amount * buffer_cycles
                bins[cell] = 0
            machines.append((p.item == problem.target, ticks(recipe["seconds"]), list(zip(entries, amounts)), exit_cell, recipe["quantity"]))

    outlet_items = {cell: deque() for cell in outlets}
    outlet_next = dict.fromkeys(outlets, 0)
    source_next = dict.fromkeys((point for point, _ in inlets), 0)
    # 机器和进货箱向相邻设备交接，采用接收端间隔。可选的进货限速另行叠加。
    receive_spacing = {**spacing, **{cell: delay[1] for cell, delay in outlets.items()}}
    supply_cap = problem.case.get("inlet_capacity")
    inlet_spacing = ceil(minute / Fraction(str(supply_cap))) if supply_cap is not None else 1
    busy_until = [None] * len(machines)
    produced = [0] * len(machines)
    first, total, late_total = None, 0, 0

    def deliver(cell, now):
        if cell in queues and len(queues[cell]) < slots[cell]:
            queues[cell].append(now + travel[cell])
            return True
        if cell in bins and bins[cell] < bin_sizes[cell]:
            bins[cell] += 1
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
        for cell, queue in outlet_items.items():
            while queue and queue[0] <= now:
                queue.popleft()
                total += 1
        for index, (terminal, cycle, inputs, exit_cell, quantity) in enumerate(machines):
            if busy_until[index] is not None and busy_until[index] <= now:
                if exit_cell is None:
                    if terminal:
                        total += quantity
                    busy_until[index] = None
                elif produced[index] + quantity <= buffer_cycles * quantity:
                    produced[index] += quantity
                    busy_until[index] = None
            if busy_until[index] is None and all(bins[cell] >= amount for cell, amount in inputs):
                for cell, amount in inputs:
                    bins[cell] -= amount
                busy_until[index] = now + cycle
        order = belt_cells if now % 2 else reversed(belt_cells)
        for cell in order:
            queue = queues[cell]
            if not queue or queue[0] > now or now < next_move[cell]:
                continue
            targets = outgoing[cell]
            for offset in range(len(targets)):
                current = (cursor[cell] + offset) % len(targets)
                target = targets[current]
                if deliver(target, now):
                    queue.popleft()
                    cursor[cell] = (current + 1) % len(targets)
                    next_move[cell] = now + spacing[cell]
                    break
        for i, (_, _, _, cell, _) in enumerate(machines):
            if cell is not None and produced[i] and now >= source_next.get(cell, 0) and deliver(outgoing[cell][0], now):
                produced[i] -= 1
                source_next[cell] = now + receive_spacing.get(outgoing[cell][0], 1)
        for point, target in inlets:
            if now >= source_next[point] and deliver(target, now):
                source_next[point] = now + max(inlet_spacing, receive_spacing.get(target, 1))
        if first is None and total:
            first = now / ticks_per_second
        if now == (DAY - 4) * minute:
            late_total = total
    return {"first_seconds": first, "first_day": total, "last_four_per_minute": (total - late_total) / 4}
