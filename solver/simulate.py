from collections import defaultdict, deque
from fractions import Fraction
from math import ceil, lcm

from .geometry import placement_ports
from .layout import distance
from .model import DAY


def simulate(problem, layout, *, ports=None):
    """在空线模型中跟踪逐件移动、生产批次与缓存在途量。

    ports 可给出每个进货箱和加工设备的实际端口，值为按配方排列的输入坐标和一个输出坐标。
    """
    used = set(layout.transport.values())
    outlet_load = max((p.rate for p in layout.placements if p.building == "outlet"), default=0)
    outlet_level = next((b["building"] for b in problem.belts if b["capacity"] >= outlet_load), None)
    ticks_per_second = lcm(4, *(b["capacity"] // 60 for b in problem.belts
                                 if (b["building"] == outlet_level or b["building"] in used or b.get("underground") in used)
                                 and type(b["capacity"]) is int and b["capacity"] >= 60))
    minute = 60 * ticks_per_second

    def ticks(seconds):
        return max(1, ceil(Fraction(str(seconds)) * ticks_per_second))

    outgoing = defaultdict(list)
    for segment in layout.segments:
        outgoing[segment.start].append(segment.end)
    for targets in outgoing.values():
        targets.sort()

    capacities = {key: Fraction(belt["capacity"]) for belt in problem.belts
                  for key in (belt["building"], belt.get("underground")) if key}
    durations = {key: ticks(belt["seconds_per_cell"]) for belt in problem.belts
                 for key in (belt["building"], belt.get("underground")) if key}
    for key in ("merge", "split"):
        capacities[key] = problem.junction_capacity
        durations[key] = ticks(problem.catalog.get("junction_seconds", 0.5))

    queues = {cell: deque() for cell in layout.transport}
    next_move = dict.fromkeys(queues, 0)
    cursor = dict.fromkeys(queues, 0)
    travel = {cell: durations[key] for cell, key in layout.transport.items()}
    spacing = {cell: ceil(minute / capacities[key]) for cell, key in layout.transport.items()}
    buffer_cycles = int(problem.case.get("simulation_buffer_batches", 4))
    if buffer_cycles < 1:
        raise ValueError("仿真缓存批次数应为正整数")
    slots = {cell: max(2, ceil(travel[cell] / spacing[cell]) + 1) for cell in queues}

    inlets, outlets, machines = [], {}, []
    bin_sizes, bins = {}, {}
    for p in sorted(layout.placements, key=lambda placement: placement.anchor):
        if p.building == "inlet":
            point = (ports[p] if ports is not None else placement_ports(problem, p))[1]
            inlets.append((point, outgoing[point][0]))
        elif p.building == "outlet":
            belt = next(b for b in problem.belts if b["capacity"] >= p.rate)
            outlets[p.anchor] = (ticks(belt["seconds_per_cell"]), ceil(minute / belt["capacity"]))
        else:
            recipe = problem.recipes[p.item]
            amounts = list(recipe["inputs"].values())
            entries, exit_cell = ports[p] if ports is not None else placement_ports(problem, p)
            for cell, amount in zip(entries, amounts):
                bin_sizes[cell] = amount * buffer_cycles
                bins[cell] = 0
            machines.append((p.item == problem.target, ticks(recipe["seconds"]), list(zip(entries, amounts)), exit_cell, recipe["quantity"]))

    outlet_items = {cell: deque() for cell in outlets}
    outlet_next = dict.fromkeys(outlets, 0)
    source_next = dict.fromkeys((point for point, _ in inlets), 0)
    busy_until = [None] * len(machines)
    produced = [0] * len(machines)
    first, total, late_total = None, 0, 0

    def deliver(cell, now, multiplier=1):
        if cell in queues and len(queues[cell]) < slots[cell]:
            queues[cell].append(now + travel[cell] * multiplier)
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
                if deliver(target, now, distance(cell, target)):
                    queue.popleft()
                    cursor[cell] = (current + 1) % len(targets)
                    next_move[cell] = now + spacing[cell]
                    break
        for i, (_, _, _, cell, _) in enumerate(machines):
            if cell is not None and produced[i] and now >= source_next.get(cell, 0) and deliver(outgoing[cell][0], now):
                produced[i] -= 1
                source_next[cell] = now + ceil(minute / problem.capacity)
        for point, target in inlets:
            if now >= source_next[point] and deliver(target, now):
                source_next[point] = now + ceil(minute / problem.box_capacity)
        if first is None and total:
            first = now / ticks_per_second
        if now == (DAY - 4) * minute:
            late_total = total
    return {"first_seconds": first, "first_day": total, "last_four_per_minute": (total - late_total) / 4}
