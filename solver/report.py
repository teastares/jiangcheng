import json
from base64 import b64encode
from collections import Counter, defaultdict
from dataclasses import asdict
from fractions import Fraction
from html import escape
from math import gcd, lcm
from pathlib import Path

from .blueprint import FORMAT, IDS, blueprint_id, export, is_split_id, record_facing
from .collision import collision_error, footprint_bounds, uses_steam_geometry
from .geometry import DIRECTION_NAMES, INLET_KINDS, OFFSETS, STEAM_CELL_PIXELS, inlet_output_ports, steam_collision_rect
from .layout import distance, layout_rank
from .model import DAY, Infeasible, belt_options
from .rotation import ANGLES, rotation_result


MODE_NAMES = {"shared": "共用上游", "separate": "独立供料"}
OBJECTIVE_NAMES = {"area": "占地面积", "buildings": "建筑数", "lines": "走线格数", "materials": "建材总件数",
                   "power": "额定功耗", "inlets": "进货口数量", "machines": "加工设备数", "transport_time": "输送在途量",
                   "cadence": "生产节拍"}
COLORS = ("#187467", "#8c5231", "#375d99", "#8b4672", "#5c59a1", "#846007", "#166b85", "#536634", "#a34839")


def inlet_rows(layout):
    return sorted((p for p in layout.placements if p.building in INLET_KINDS), key=lambda p: (p.y, p.x))


def outlet_rows(layout):
    return sorted((p for p in layout.placements if p.building == "outlet"), key=lambda p: (p.y, p.x))


def ratio(raw):
    multiplier = lcm(*(v.denominator for v in raw.values()))
    integers = [int(v * multiplier) for v in raw.values()]
    common = gcd(*integers)
    return " ∶ ".join(raw) + " = " + " ∶ ".join(str(v // common) for v in integers)


def direction_counts(problem, layout, buildings, wires):
    groups = defaultdict(Counter)
    outlets = {p.anchor for p in outlet_rows(layout)}
    facility_names = {blueprint_id(problem, key): problem.buildings[key]["name"] for key in IDS if key in problem.buildings}
    for record in buildings:
        if (record.x, record.y) in outlets:
            group = "成品接出带"
        elif record.id in (IDS["inlet"], IDS["inlet_large"]):
            group = "进货箱" if record.id == IDS["inlet"] else problem.buildings["inlet_large"]["name"]
        elif is_split_id(record.id):
            group = f"{problem.buildings['split']['name']}主进料" if record.id != IDS["split"] else "分流器主进料"
        elif record.id == IDS["merge"]:
            group = "合并器主出料"
        elif 20 <= record.id <= 24:
            group = "地面弯带" if record.shape >= 4 else "地面直带"
        elif 30 <= record.id <= 34:
            group = "地下带入口" if record.direction < 4 else "地下带出口"
        else:
            group = facility_names[record.id]
        groups[group][DIRECTION_NAMES[record_facing(record)]] += 1
    if wires:
        groups["电线"]["无"] = len(wires)
    return {name: dict(counts) for name, counts in groups.items()}


def _collision_dimensions(problem, layout, records):
    names = {blueprint_id(problem, kind): kind for kind in IDS if kind in problem.buildings}
    placed = {p.anchor: p for p in layout.placements}
    sizes = Counter()
    for record in records:
        p = placed.get((record.x, record.y))
        kind = p.building if p is not None else names[record.id]
        facing = record_facing(record)
        rect = steam_collision_rect(kind, facing=facing)
        if rect is not None:
            left, top, right, bottom = rect
            logical = (p.width, p.height) if p is not None else (1, 1)
            sizes[kind, facing, logical, (right - left, bottom - top)] += 1
    return [{"name": problem.buildings[kind]["name"], "direction": DIRECTION_NAMES[facing],
             "logical_cells": logical, "pixels": pixels, "count": count}
            for (kind, facing, logical, pixels), count in sorted(sizes.items())]


def render_svg(problem, layout, mode, game_records=None):
    cell = 25
    gx, gy = 46, 105
    grid_width, grid_height = cell * layout.width, cell * layout.height
    legends = sorted({s.item for s in layout.segments})
    colors = {item: COLORS[i % len(COLORS)] for i, item in enumerate(legends)}
    side_x = gx + grid_width + 24
    panel_width = 278
    total_width = max(side_x + panel_width, 620)
    power_y = gy + grid_height + 75
    steam = uses_steam_geometry(problem)
    keys = ["箭头　物流方向与设备朝向", "小圆点　建筑定位格", "罗马数字　传送带等级", "分 / 合　分流器 / 合并器", "入 / 出　地下带，虚线指向出口", "进 / 出　补货口 / 成品接出带"]
    if steam:
        keys.extend(("机台填色框　逻辑端口格", "浅色虚线框　游戏碰撞范围"))
    legend_y = gy + 49 + 21 * len(keys)
    total_height = max(power_y + grid_height + 35, legend_y + 10 + 22 * len(legends))
    title = f"{problem.target} · {MODE_NAMES[mode]}"
    description = f"{layout.width}列、{layout.height}行。左上角为(0,0)。上图为物流，下图为电网。"
    if steam:
        description += "机台和进货箱的浅色虚线框表示游戏像素碰撞范围。"
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="{total_height}" viewBox="0 0 {total_width} {total_height}" role="img" aria-labelledby="title description">',
             f'<title id="title">{escape(title)}</title><desc id="description">{description}</desc>',
             '<defs><pattern id="grid" width="25" height="25" patternUnits="userSpaceOnUse"><path d="M25 0H0V25" fill="none" stroke="#d9e0d9"/></pattern></defs>',
             '<style>text{font-family:"Microsoft YaHei","Noto Sans CJK SC","PingFang SC",sans-serif;fill:#19362e}.tick{font-size:9px;fill:#4c665d}.meta{font-size:13px;fill:#3e5b51}.machine{font-size:11px;font-weight:650}.legend{font-size:12px}.title{font-size:28px;font-weight:750}.section{font-size:16px;font-weight:700}</style>',
             f'<rect width="{total_width}" height="{total_height}" fill="#f5f7f2"/>',
             f'<text x="{gx}" y="43" class="title">{escape(title)}</text>',
             f'<text x="{gx}" y="68" class="meta">{layout.width} × {layout.height} 格　·　{layout.statistics["machines"]} 台加工设备　·　{layout.statistics["power"]} kW　·　稳态理论日产 {problem.rate * DAY}</text>']

    def center(point, origin_y=gy):
        return gx + cell * (point[0] + .5), origin_y + cell * (point[1] + .5)

    buildings, wires = ({(r.x, r.y): r for r in group} for group in (game_records or ([], [])))

    def attrs(record):
        if record is None:
            return ""
        return f' data-id="{record.id}" data-dir="{record.direction}" data-shape="{record.shape}" data-recipe="{record.recipe}"'

    def grid(origin_y):
        parts.append(f'<rect x="{gx}" y="{origin_y}" width="{grid_width}" height="{grid_height}" fill="#fffef9" stroke="#c4d0c6"/>')
        parts.append(f'<rect x="{gx}" y="{origin_y}" width="{grid_width}" height="{grid_height}" fill="url(#grid)"/>')
        interval = 1 if max(layout.width, layout.height) <= 18 else 5
        for x in range(0, layout.width, interval):
            parts.append(f'<text x="{center((x, 0), origin_y)[0]}" y="{origin_y-5}" text-anchor="middle" class="tick">{x}</text>')
        for y in range(0, layout.height, interval):
            parts.append(f'<text x="{gx-7}" y="{center((0, y), origin_y)[1]+3}" text-anchor="end" class="tick">{y}</text>')

    grid(gy)
    for segment in sorted(layout.segments, key=lambda s: distance(s.start, s.end), reverse=True):
        x1, y1 = center(segment.start)
        x2, y2 = center(segment.end)
        color = colors[segment.item]
        dx = (segment.end[0] > segment.start[0]) - (segment.end[0] < segment.start[0])
        dy = (segment.end[1] > segment.start[1]) - (segment.end[1] < segment.start[1])
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        tunnel = distance(segment.start, segment.end) > 1
        dash = ' stroke-dasharray="4 3"' if tunnel else ''
        arrow = f'{mx+4*dx},{my+4*dy} {mx-2*dx+3*dy},{my-2*dy-3*dx} {mx-2*dx-3*dy},{my-2*dy+3*dx}'
        parts.append(f'<g class="route"><title>{escape(segment.item)}：{segment.rate} 件/分钟；{segment.start} → {segment.end}</title>'
                     f'<path d="M{x1} {y1}L{x2} {y2}" fill="none" stroke="{color}" stroke-width="2.2"{dash}/>'
                     f'<polygon points="{arrow}" fill="{color}"/></g>')

    tunnels = {p: (symbol, distance(s.start, s.end)) for s in layout.segments if distance(s.start, s.end) > 1
               for p, symbol in ((s.start, "入"), (s.end, "出"))}
    for point, kind in sorted(layout.transport.items()):
        cx, cy = center(point)
        end, span = tunnels.get(point, ("", None))
        label = {"split": "分", "merge": "合"}.get(kind, end)
        tier = kind[-1]
        if kind.startswith(("belt", "tunnel")) and tier in "2345":
            label += {"2": "Ⅱ", "3": "Ⅲ", "4": "Ⅳ", "5": "Ⅴ"}[tier]
        svg_type = ' class="transport"'
        tooltip = f'{problem.buildings[kind]["name"]}｜{point}'
        record = buildings.get(point)
        if record is not None:
            role = {"split": "主进料", "merge": "主出料"}.get(kind, "物流")
            tooltip += f"｜{role}向{DIRECTION_NAMES[record_facing(record)]}"
        if span is not None:
            tooltip += f"｜跨度 {span} 格"
        parts.append(f'<g{svg_type} data-x="{point[0]}" data-y="{point[1]}" data-kind="{kind}"{attrs(record)}><title>{escape(tooltip)}</title>')
        if label:
            half = 9 if len(label) > 1 else 6.6
            parts.append(f'<rect x="{cx-half}" y="{cy-6.6}" width="{half*2}" height="13.2" rx="2.3" fill="#eef4eb" stroke="#546e62"/>'
                         f'<text x="{cx}" y="{cy+3.4}" font-size="8.5" text-anchor="middle">{label}</text>')
        else:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="6" fill="transparent"/>')
        parts.append('</g>')

    inputs = {p.node: f"进{i}" for i, p in enumerate(inlet_rows(layout), 1)}
    outputs = {p.node: f"出{i}" for i, p in enumerate(outlet_rows(layout), 1)}
    outlines = []
    for p in layout.placements:
        x, y = gx + p.x * cell, gy + p.y * cell
        color = colors.get(p.item, "#187467")
        label = f"{inputs[p.node]} {p.item}" if p.building in INLET_KINDS else outputs.get(p.node, p.item)
        title_text = f'{label}｜{problem.buildings[p.building]["name"]}｜定位格 {p.anchor}｜朝{DIRECTION_NAMES[p.direction]}｜{p.rate} 件/分钟'
        if steam and p.building != "outlet" and (rect := steam_collision_rect(p.building, p.anchor, p.direction)) is not None:
            left, top, right, bottom = rect
            width, height = right - left, bottom - top
            px = cell / STEAM_CELL_PIXELS
            title_text += f"｜碰撞范围 {width} × {height} 像素"
            outlines.append(f'<rect x="{gx+left*px:g}" y="{gy+top*px:g}" width="{width*px:g}" height="{height*px:g}"/>')
        ax, ay = center(p.anchor)
        fill = "#fff2d8" if p.building in INLET_KINDS else "#e7f0e5"
        parts.append(f'<g data-kind="{p.building}" data-node="{p.node}" data-x="{p.anchor[0]}" data-y="{p.anchor[1]}"{attrs(buildings.get(p.anchor))}><title>{escape(title_text)}</title>'
                     f'<rect x="{x+2}" y="{y+2}" width="{p.width*cell-4}" height="{p.height*cell-4}" rx="4" fill="{fill}" stroke="{color}"/>'
                     f'<circle cx="{ax-cell*.30}" cy="{ay+cell*.31}" r="2" fill="{color}"/>'
                     f'<text x="{x+p.width*cell/2}" y="{y+p.height*cell/2+3}" text-anchor="middle" class="machine">{escape(label)}</text>')
        dx, dy = OFFSETS[p.direction]
        tip_x = x + p.width * cell / 2 + dx * (p.width * cell / 2 - 4)
        tip_y = y + p.height * cell / 2 + dy * (p.height * cell / 2 - 4)
        arrow = (f"{tip_x},{tip_y} {tip_x - 5*dx + 3*dy},{tip_y - 5*dy - 3*dx} "
                 f"{tip_x - 5*dx - 3*dy},{tip_y - 5*dy + 3*dx}")
        parts.append(f'<polygon points="{arrow}" fill="{color}"/>')
        parts.append('</g>')

    if outlines:
        parts.append('<g class="collision-outlines" aria-hidden="true" pointer-events="none" fill="none" '
                     'stroke="#527384" stroke-opacity=".72" stroke-width="1.1" stroke-dasharray="3 2">')
        parts.extend(outlines)
        parts.append('</g>')
    parts.append(f'<text x="{side_x}" y="{gy+12}" class="section">物流与设备</text>')
    for i, label in enumerate(keys):
        parts.append(f'<text x="{side_x}" y="{gy+39+21*i}" class="legend">{label}</text>')
    for i, item in enumerate(legends):
        y = legend_y + i * 22
        parts.append(f'<path d="M{side_x} {y-4}h18" stroke="{colors[item]}" stroke-width="3"/>'
                     f'<text x="{side_x+27}" y="{y}" class="legend">{escape(item)}</text>')

    parts.append(f'<text x="{gx}" y="{power_y-38}" class="section">电网 · 同一组坐标</text>')
    grid(power_y)
    for _, _, p in layout.machines:
        parts.append(f'<rect x="{gx+p.x*cell+2}" y="{power_y+p.y*cell+2}" width="{p.width*cell-4}" height="{p.height*cell-4}" rx="3" fill="#e9eee5" stroke="#b4c4b8"/>')
    for point in sorted(layout.wires):
        cx, cy = center(point, power_y)
        for neighbor in ((point[0]+1, point[1]), (point[0], point[1]+1)):
            if neighbor in layout.wires:
                nx, ny = center(neighbor, power_y)
                parts.append(f'<path d="M{cx} {cy}L{nx} {ny}" stroke="#947234" stroke-width="2"/>')
        parts.append(f'<circle class="wire" data-x="{point[0]}" data-y="{point[1]}"{attrs(wires.get(point))} cx="{cx}" cy="{cy}" r="2.5" fill="#947234"/>')
    if layout.wires:
        px, py = center(layout.power_entry, power_y)
        parts.append(f'<circle cx="{px}" cy="{py}" r="6" fill="#edba50" stroke="#77541c" stroke-width="2"/>'
                     f'<text x="{gx}" y="{power_y-20}" class="legend">电力接入 {layout.power_entry} · {layout.statistics["power"]} kW</text>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def data_for(problem, layout, mode):
    inlet_rates = Counter()
    for segment in layout.segments:
        inlet_rates[segment.start] += segment.rate
    inputs = [{"id": f"进{i}", "position": p.anchor, "item": p.item, "direction": DIRECTION_NAMES[p.direction],
               "per_minute": p.rate, "per_day": p.rate * DAY,
               "feed_ports": [{"position": port, "per_minute": inlet_rates[port], "per_day": inlet_rates[port] * DAY}
                              for port in inlet_output_ports(problem, p) if inlet_rates[port]]}
              for i, p in enumerate(inlet_rows(layout), 1)]
    outputs = [{"id": f"出{i}", "position": p.anchor, "item": p.item, "per_minute": p.rate, "direction": DIRECTION_NAMES[p.direction],
                "belt": belt_options(problem, p.rate)[0]["level"]}
               for i, p in enumerate(outlet_rows(layout), 1)]
    directions = defaultdict(lambda: {"incoming": [], "outgoing": []})
    for segment in layout.segments:
        directions[segment.start]["outgoing"].append(segment.end)
        directions[segment.end]["incoming"].append(segment.start)
    machine_nodes = {machine.node for _, _, machine in layout.machines}
    buildings = [{**asdict(p), "facing": p.direction, "direction": ("下", "左", "右", "上")[p.direction],
                  "recipe": problem.recipes[p.item].get("id") if p.node in machine_nodes else None}
                 for p in layout.placements]
    if uses_steam_geometry(problem):
        for p, building in zip(layout.placements, buildings):
            rect = steam_collision_rect(p.building, p.anchor, p.direction)
            if rect is not None:
                left, top, right, bottom = rect
                building["collision_rect_px"] = rect
                building["collision_size_px"] = right - left, bottom - top
    return {"target": problem.target, "mode": mode, "size": [layout.width, layout.height], "effective_minutes": DAY,
            "target_per_minute": problem.rate, "theoretical_per_day": problem.rate * DAY, "raw_ratio": ratio(problem.raw),
            "inlets": inputs, "outlets": outputs, "power_entry": layout.power_entry if layout.wires else None,
            "platform": problem.catalog.get("platform"), "profile": layout.profile, "rows_optimal_before_compaction": layout.row_optimal,
            "requested_belt_levels": problem.case.get("requested_belt_levels", problem.case.get("belt_levels")),
            "allowed_belt_levels": [belt["level"] for belt in problem.belts],
            "search_belt_levels": layout.search_belt_levels or tuple(belt["level"] for belt in problem.belts),
            "solve_seconds": problem.case.get("solve_seconds"),
            "statistics": layout.statistics, "placements": buildings,
            "optimization": {"production": layout.production_milp, "logistics": layout.logistics_milp},
            "transport": [{"position": pos, "kind": kind, **directions[pos]} for pos, kind in sorted(layout.transport.items())],
            "segments": [asdict(s) for s in layout.segments], "wires": sorted(layout.wires)}


def choose(problem, results):
    return min((mode for mode, (layout, _) in results.items() if layout is not None),
               key=lambda mode: layout_rank(problem, results[mode][0]), default=None)


BLUEPRINT_ACTIONS_STYLE = """.blueprint-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.blueprint-copy{border:1px solid #187467;border-radius:6px;padding:7px 14px;background:#fffef9;color:#187467;font:inherit;cursor:pointer}
"""
BLUEPRINT_INSTRUCTIONS = "复制蓝图后，在 Steam 游戏蓝图界面粘贴导入。也可下载 TXT 并复制全文。自动建造还需备齐下方建材。"
BLUEPRINT_ACTIONS_SCRIPT = '''<script>
(() => {
  const view = document.getElementById("blueprint-view");
  const report = view || document;
  const rotation = document.getElementById("blueprint-rotation");

  function enableCopy() {
    const button = report.querySelector("#blueprint-copy");
    if (button) button.hidden = false;
  }

  function copyFromSelection(text, button) {
    const field = document.createElement("textarea");
    field.value = text;
    field.readOnly = true;
    field.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.append(field);
    try {
      field.focus({preventScroll: true});
      field.select();
      return document.execCommand("copy");
    } finally {
      field.remove();
      if (button.isConnected) button.focus({preventScroll: true});
    }
  }

  report.addEventListener("click", async event => {
    const button = event.target.closest?.("#blueprint-copy");
    if (!button) return;
    const actions = button.parentElement;
    const download = actions.querySelector("#blueprint-download");
    const status = actions.querySelector("#blueprint-copy-status");
    status.textContent = "";
    try {
      const url = download.getAttribute("href");
      const text = atob(url.slice(url.indexOf(",") + 1));
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        if (!copyFromSelection(text, button)) throw new Error();
      }
      status.textContent = "已复制蓝图。";
    } catch {
      status.textContent = "复制失败，请下载 TXT。";
    }
  });

  if (view && rotation) {
    const original = view.cloneNode(true);
    rotation.addEventListener("change", () => {
      const template = document.getElementById(`blueprint-view-${rotation.value}`);
      const source = rotation.value === "0" ? original : template?.content;
      if (source) {
        view.replaceChildren(...source.cloneNode(true).childNodes);
        enableCopy();
      }
    });
    rotation.closest("p").hidden = false;
  }
  enableCopy();
})();
</script>'''


def render_blueprint_actions(filename, text, *, include_script=True):
    payload = b64encode(text.encode("ascii")).decode("ascii")
    return f'''<p class="blueprint-actions">
<a id="blueprint-download" class="download" download="{escape(filename)}" href="data:text/plain;charset=utf-8;base64,{payload}">导出游戏导入文本</a>
<button type="button" id="blueprint-copy" class="blueprint-copy" hidden>复制蓝图</button>
<span id="blueprint-copy-status" role="status"></span>
</p>''' + (BLUEPRINT_ACTIONS_SCRIPT if include_script else "")


def _render_timing(simulation):
    timing = simulation.get("timing") if simulation else None
    if timing is None:
        return ""

    def number(value):
        return "—" if value is None else f"{value:.2f}"

    def table(headings, rows):
        header = "".join(f"<th>{value}</th>" for value in headings)
        body = "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
        return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"

    machines = []
    for row in timing["machine_rows"]:
        missing = "、".join(f"{entry['item']} {entry['seconds']:.2f} 秒" for entry in row["starved_inputs"])
        machines.append((row["position"], row["item"], number(row["recipe_seconds"]),
                         number(row["planned_period_seconds"]), number(row["mean_start_interval_seconds"]),
                         number(row["max_start_interval_seconds"]), missing or "—", number(row["extra_blocked_seconds"])))
    belts = []
    for row in timing["bottlenecks"]:
        belts.append((row["position"], row["item"], number(row["planned_items_per_minute"]),
                      number(row["actual_items_per_minute"]), number(row["rated_items_per_minute"]),
                      f"{row['load']:.0%}", number(row["travel_seconds"])))
    machine_table = table(("定位格", "产物", "加工周期", "计划开工间隔", "平均开工间隔", "最长开工间隔", "超计划缺料", "超计划堵料"), machines)
    belt_table = table(("物流格", "物料", "计划件/分", "仿真件/分", "设计上限件/分", "计划负荷", "通过秒数"), belts)
    return (f"<section><h2>生产节拍</h2><p>统计空线仿真末 {timing['measurement_seconds']:g} 秒。"
            "开工间隔按秒计，计划值由分配给该机器的目标产量确定。"
            f"单台最长累计超计划停顿 {timing['worst_machine_delay_seconds']:.2f} 秒，"
            f"开工间隔最大超出计划 {timing['max_excess_gap_seconds']:.2f} 秒。"
            f"传送带最高计划负荷 {timing['max_belt_load']:.0%}。</p>{machine_table}"
            f"<h3>高负荷物流格</h3>{belt_table}</section>")


def _render_optimization(data):
    optimization = data.get("optimization", {})
    production = optimization.get("production")
    if not production:
        return ""
    statuses = {0: "最优", 1: "在时间限制内找到可行解", -1: "使用精确供需配平复核值"}
    status = statuses.get(production["status"], "使用复核后的候选")
    bottleneck = optimization.get("logistics")
    grade = ""
    if bottleneck:
        belt_status = statuses.get(bottleneck["status"], "已找到候选")
        changed = bottleneck["changed_cells"]
        outcome = f"采用调整 {changed} 格的方案" if bottleneck["selected"] else f"比较调整 {changed} 格的方案后保留原带级"
        grade = f"带级分配子问题{belt_status}，{outcome}。"
    units = "、".join(f"{escape(name)} × {count}" for name, count in production["units"].items())
    return (f"<section><h2>HiGHS 混合整数求解</h2><p>固定配方链的产能配置子问题{status}，"
            f"机台与进货口功率下界为 {production['minimum_power_kw']:g} kW。{grade}</p>"
            f"<p>固定配方与单出口供料模型的设备数量下界为 {units}。"
            "布局从通过占位、物流和仿真校验的候选中选出。</p></section>")


def _render_report_view(data, svg, filename, text):
    stats = data["statistics"]
    columns = ("id", "position", "item", "direction", "per_minute", "per_day")
    rows = "".join("<tr>" + "".join(f"<td>{escape(str(r[key]))}</td>" for key in columns) + "</tr>" for r in data["inlets"])
    dual_notes = []
    for row in data["inlets"]:
        ports = row.get("feed_ports", ())
        if len(ports) > 1:
            flows = "、".join(f"接口 {port['position']} 每分钟 {port['per_minute']} 件，每日 {port['per_day']} 件" for port in ports)
            dual_notes.append(f"<p>{row['id']} 共用一箱库存。{flows}。</p>")
    materials = "".join(f"<li>{escape(item)} {amount}</li>" for item, amount in stats["materials"].items())
    equipment = ""
    if data.get("equipment"):
        counts = "、".join(f"{escape(name)} × {count}" for name, count in data["equipment"])
        equipment = f"<section><h2>所用设备与物流</h2><p>{counts}。</p></section>"
    power = f"电网接入口 {data['power_entry']}，预留 {stats['power']} kW。" if data["power_entry"] else ""
    output = ("成品按这些定位格的朝向接出。" + "、".join(f"{r['position']} 朝{r['direction']}" for r in data["outlets"]) + "。"
              if data["outlets"] else "产物直接入账。")
    capsules, actions = "", ""
    instructions = "按摆放图和坐标手动建造。"
    if text is not None:
        capsules = f'<p class="capsules">所需建造胶囊 <strong>{stats["components"]}</strong> 个</p>'
        actions = render_blueprint_actions(filename, text, include_script=False)
        instructions = BLUEPRINT_INSTRUCTIONS
    footprints = ""
    if data.get("collision_dimensions"):
        sizes = []
        for entry in data["collision_dimensions"]:
            width, height = entry["pixels"]
            logical_width, logical_height = entry["logical_cells"]
            span = f"{width / STEAM_CELL_PIXELS:g} × {height / STEAM_CELL_PIXELS:g}"
            cols = (entry["name"], entry["direction"], entry["count"],
                    f"{logical_width} × {logical_height}", f"{width} × {height}", span)
            sizes.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in cols) + "</tr>")
        footprints = (f"<section><h2>设施碰撞范围</h2><p>游戏一格为 {STEAM_CELL_PIXELS} × {STEAM_CELL_PIXELS} 像素。"
                      "摆放图的浅色虚线是机台与进货箱的碰撞外框，按安装版放置规则计算。"
                      "贴图和逻辑端口格各有独立尺寸。表中分流按主进料方向，合并及传送带按出料方向列出。</p>"
                      "<table><thead><tr><th>设施</th><th>朝向</th><th>数量</th><th>逻辑格（宽 × 高）</th>"
                      f"<th>碰撞宽 × 高（像素）</th><th>换算格距</th></tr></thead><tbody>{''.join(sizes)}</tbody></table></section>")
    sim = stats.get("simulation")
    simulation = "" if sim is None else f"<p>空线仿真在前 16 分钟产出 {sim['first_day']} 件，末 4 分钟平均 {sim['last_four_per_minute']:g} 件/分钟。</p>"
    plot_note = "<p>该方向超出原配置的可建净空，请按此方向的宽高核对地块。</p>" if data.get("exceeds_plot") else ""
    logistics = ""
    if "belt_counts" in stats:
        actual = {kind: "、".join(f"{level} 级 {count} 格" for level, count in counts.items()) or "无"
                  for kind, counts in stats["belt_counts"].items()}
        allowed = "、".join(map(str, data["allowed_belt_levels"]))
        searched = "、".join(map(str, data["search_belt_levels"]))
        requested = data["requested_belt_levels"]
        request_label = "存档已解锁等级" if requested is None else "、".join(map(str, requested)) + " 级"
        elapsed = data.get("solve_seconds")
        elapsed_text = f"求解用时 {elapsed:.1f} 秒。" if elapsed is not None else ""
        logistics = (f"<section><h2>物流规格</h2><p>配置允许 {request_label}。本轮允许 {allowed} 级，入选方案来自 {searched} 级搜索。{elapsed_text}</p>"
                     f"<p>实际地面带 {actual['building']}，地下带端点 {actual['underground']}。</p>"
                     f"<p>额定输送在途量 {float(Fraction(stats['transport_time'])):.2f} 件，按各段吞吐与通过时间估算。</p></section>")
    audit = ""
    if data.get("direction_counts"):
        directions = ("东", "西", "南", "北", "无")
        headings = "".join(f"<th>{direction}</th>" for direction in directions)
        audit_rows = []
        for kind, counts in data["direction_counts"].items():
            cells = "".join(f"<td>{counts.get(direction, 0)}</td>" for direction in directions)
            audit_rows.append(f"<tr><td>{escape(kind)}</td>{cells}</tr>")
        audit = (f"<section><h2>设施方向核对</h2><p>以下记录已与导入文本回读核对。分流按货物进入时的方向统计，合并和地面带按出货方向统计。</p>"
                 f"<table><thead><tr><th>设施</th>{headings}</tr></thead><tbody>{''.join(audit_rows)}</tbody></table></section>")
    return f'''<header>
<p>{data['size'][0]} × {data['size'][1]} 格 · {data['target_per_minute']} 件/分钟 · 稳态理论日产 {data['theoretical_per_day']} 件</p>{plot_note}{capsules}{actions}</header>
<section><h2>建造与接入</h2><p>{instructions}</p><p>{power}{output}</p></section>{footprints}
<section><h2>进货口与日配额</h2><p>每日有效生产 16 分钟。{escape(data['raw_ratio'])}。</p>
<table><thead><tr><th>口</th><th>定位格</th><th>物料</th><th>朝向</th><th>件/分钟</th><th>日配额</th></tr></thead><tbody>{rows}</tbody></table>{''.join(dual_notes)}</section>
<section><h2>摆放图</h2><div class="drawing">{svg}</div></section>
<section><h2>建材</h2><ul>{materials}</ul></section>{equipment}{logistics}{_render_optimization(data)}{audit}{simulation}{_render_timing(sim)}'''


def render_html(data, svg, title, filename, text, checks="", rotations=(), *, show_rotation=False, unavailable_reasons=None):
    variants = {angle: (detail, drawing, blueprint) for angle, detail, drawing, blueprint in rotations}
    controls, templates = "", ""
    if show_rotation or variants:
        unavailable_reasons = unavailable_reasons or {}
        labels = {"rigid": "纯旋转", "rewired": "同预算重布"}
        options = ['<option value="0">0°（原方向）</option>']
        for angle in ANGLES:
            if angle in variants:
                method = variants[angle][0].get("rotation_method", "rigid")
                options.append(f'<option value="{angle}">{angle}°（{labels[method]}）</option>')
            else:
                reason = escape(unavailable_reasons.get(angle, "未通过布局核验"))
                options.append(f'<option value="{angle}" title="{reason}" disabled>{angle}°（未通过）</option>')
        directions = "、".join(f"{angle}°" for angle in ANGLES if angle not in variants)
        unavailable = (f"<small>{directions} 未找到通过核验且不增加胶囊的布局。</small>" if directions else "")
        controls = ('<p class="rotation-controls" hidden><label for="blueprint-rotation">整体方向（顺时针）</label> '
                    f'<select id="blueprint-rotation">{"".join(options)}</select> {unavailable}</p>')
        name = Path(filename)
        for angle in ANGLES:
            if angle in variants:
                detail, drawing, blueprint = variants[angle]
                view = _render_report_view(detail, drawing, f"{name.stem}-{angle}deg{name.suffix}", blueprint)
                note = escape(detail.get("rotation_note", ""))
                templates += f'<template id="blueprint-view-{angle}"><p role="status">{note}</p>{view}</template>\n'
        if checks:
            checks = f'<p>方案比较表按原方向显示。</p>{checks}'
    has_actions = text is not None or any(blueprint is not None for _, _, blueprint in variants.values())
    style = BLUEPRINT_ACTIONS_STYLE if has_actions else ""
    script = BLUEPRINT_ACTIONS_SCRIPT if has_actions or controls else ""
    view = _render_report_view(data, svg, filename, text)
    return f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>
body{{margin:0;background:#f5f7f2;color:#19362e;font:15px/1.6 "Microsoft YaHei",sans-serif}}main{{max-width:1340px;margin:auto;padding:32px 24px}}h1{{font-size:30px;margin:0}}h2{{font-size:20px;margin:0 0 10px}}section{{margin:24px 0}}p{{margin:8px 0}}.capsules{{font-size:17px}}.capsules strong{{font-size:24px;font-variant-numeric:tabular-nums}}.download{{display:inline-block;border-radius:6px;padding:8px 14px;background:#187467;color:white;text-decoration:none}}.drawing{{overflow:auto;border:1px solid #c4d0c6}}.drawing svg{{display:block;max-width:none}}table{{border-collapse:collapse;width:min(100%,720px);background:#fffef9}}th,td{{padding:6px 12px;text-align:left;border-bottom:1px solid #ccd8cd}}ul{{padding-left:1.4em;columns:3;max-width:700px}}footer{{color:#455f55}}
.rotation-controls select{{font:inherit;padding:5px 9px;border:1px solid #b7ccc1;border-radius:6px;background:#fffef9;color:inherit}}.rotation-controls small{{margin-left:8px;color:#455f55}}
{style}</style><main><h1>{escape(title)}</h1>{controls}
<div id="blueprint-view">{view}</div>
{checks}<footer>已离线检查。游戏内导入、放置与持续生产待实测。</footer>
{templates}{script}</main></html>'''


def layout_view(problem, layout, mode, title):
    steam = uses_steam_geometry(problem)
    if steam:
        if error := collision_error(problem, layout.placements, layout.transport):
            raise Infeasible(error)
        if footprint_bounds(layout) != (0, 0, layout.width, layout.height):
            raise Infeasible("设施碰撞范围超出蓝图的外接地块")
    data = data_for(problem, layout, mode)
    data["equipment"] = [(problem.buildings[kind]["name"], count)
                         for kind, count in layout.statistics["buildings"].items()]
    if layout.wires:
        data["equipment"].append((problem.buildings["wire"]["name"], len(layout.wires)))
    text, game_records = None, None
    if problem.catalog.get("blueprint_format") == FORMAT:
        description = f"{problem.rate} 件/分钟，16 分钟理论产量 {problem.rate * DAY}，额定功耗 {layout.statistics['power']} kW。"
        text, buildings, wires = export(problem, layout, title, description)
        game_records = buildings, wires
        data["direction_counts"] = direction_counts(problem, layout, buildings, wires)
        if steam:
            data["collision_dimensions"] = _collision_dimensions(problem, layout, buildings)
    return data, render_svg(problem, layout, mode, game_records), text


def write_layout(directory, name, problem, layout, mode, checks=""):
    title = f"{problem.target} · {MODE_NAMES[mode]}"
    data, svg, text = layout_view(problem, layout, mode, title)
    directory.mkdir(parents=True, exist_ok=True)
    rotations = []
    unavailable_reasons = {}
    direction_details = {}

    def write_files(stem, drawing, blueprint):
        (directory / f"{stem}.svg").write_text(drawing, encoding="utf-8")
        if blueprint is not None:
            (directory / f"{stem}.txt").write_text(blueprint, encoding="ascii")

    write_files(name, svg, text)
    for angle in ANGLES:
        rotation = rotation_result(problem, layout, angle)
        rotated = rotation.layout
        if rotated is None:
            unavailable_reasons[angle] = rotation.reason
            continue
        try:
            detail, drawing, blueprint = layout_view(problem, rotated, mode, f"{title} · {angle}°")
        except Infeasible as error:
            unavailable_reasons[angle] = str(error)
            continue
        detail["rotation_method"] = rotation.method
        detail["rotation_note"] = rotation.reason
        detail["exceeds_plot"] = any(limit is not None and length > limit
                                     for length, limit in zip(detail["size"], problem.plot_limits))
        direction_details[str(angle)] = {"method": rotation.method, "reason": rotation.reason,
                                         "size": detail["size"], "capsules": rotated.statistics["components"]}
        rotations.append((angle, detail, drawing, blueprint))
        write_files(f"{name}-{angle}deg", drawing, blueprint)

    direction_details.update((str(angle), {"method": "unavailable", "reason": reason})
                             for angle, reason in unavailable_reasons.items())
    data["rotations"] = direction_details
    html = render_html(data, svg, title, f"{name}.txt", text, checks, rotations, show_rotation=True,
                       unavailable_reasons=unavailable_reasons)
    (directory / f"{name}.html").write_text(html, encoding="utf-8")
    (directory / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return data, text is not None


def write_case(directory, key, problem, results):
    directory.mkdir(parents=True, exist_ok=True)
    winner = choose(problem, results)
    lines = [f"# {problem.target} · {problem.count} 台末端机器", "", f"目标 {problem.rate} 件/分钟，稳态理论日产 {problem.rate * DAY} 件，原料比例 {ratio(problem.raw)}。"]
    if problem.power_limit is not None:
        lines.extend(["", f"电力预算 {problem.case['power_limit_kw']} kW。"])
    if winner:
        objectives = " → ".join(OBJECTIVE_NAMES[key] for key in problem.objective)
        lines.extend(["", f"**入选布局为{MODE_NAMES[winner]}。** 优先级为 {objectives}。"])
    lines.extend(["", "| 排法 | 外接矩形（面积） | 加工设备 | 建筑 | 物流格 | 电线格 | 建材总件数 | 功耗 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for mode, (layout, _) in results.items():
        if not layout:
            lines.append(f"| {MODE_NAMES[mode]} | 未找到 | — | — | — | — | — | — |")
            continue
        s = layout.statistics
        lines.append(f"| {MODE_NAMES[mode]} | {layout.width} × {layout.height}（{layout.width*layout.height}） | {s['machines']} | {s['components']} | {s['transport']} | {s['wires']} | {sum(s['materials'].values())} | {s['power']} kW |")

    for mode, (layout, candidates) in results.items():
        if not layout:
            continue
        name = f"{key}-{mode}"
        data, has_text = write_layout(directory, name, problem, layout, mode)
        links = [f"[离线交付页]({name}.html)", f"[SVG 摆放图]({name}.svg)", f"[格位与流量 JSON]({name}.json)"]
        if has_text:
            links.append(f"[导入文本]({name}.txt)")
        lines.extend(["", f"## {MODE_NAMES[mode]}", "", " · ".join(links), "",
                      f"共检查 {candidates} 个通过静态校验的候选。选中方案的分组参数为 `{layout.profile}`。"])
        sim = layout.statistics.get("simulation")
        if sim is not None:
            seconds = "未产出" if sim["first_seconds"] is None else f"{sim['first_seconds']:g} 秒"
            reached = "，末段达到目标" if sim["last_four_per_minute"] >= float(problem.rate) else "，末段低于目标"
            lines.extend(["", f"空线仿真首件 {seconds}，前 {DAY} 分钟产出 {sim['first_day']} 件，末 4 分钟平均 {sim['last_four_per_minute']:g} 件/分钟{reached}。"])
        if layout.wires:
            lines.extend(["", f"从 {layout.power_entry} 接电，预留 {layout.statistics['power']} kW。"])
        lines.extend(["", "| 进货口定位格 | 原料 | 件/分钟 | 日配额（16 分钟） |", "| --- | --- | ---: | ---: |"])
        for row in data["inlets"]:
            lines.append(f"| {row['id']} `{row['position']}` | {row['item']} | {row['per_minute']} | {row['per_day']} |")
        if data["outlets"]:
            locations = "，".join(f"{r['id']} `{r['position']}` 朝{r['direction']}（{r['per_minute']} 件/分钟）" for r in data["outlets"])
            lines.extend(["", f"在所列接出口外预留收货或转运。{locations}。"])
        else:
            lines.extend(["", "成品直接计入晶能余额。"])
        lines.extend(["", "建材用量　" + "、".join(f"{item} {quantity}" for item, quantity in layout.statistics["materials"].items()) + "。"])
    lines.extend(["", "分数配额表示平均需求。按库存和余数跨日调节整数补货。游戏内摆放及持续生产待实测。", ""])
    (directory / f"{key}.md").write_text("\n".join(lines), encoding="utf-8")
    return winner


def write_index(directory, summaries):
    lines = ["# 产线求解算例", "", "按每天 16 分钟计算，入选布局取自程序搜索的候选。", "",
             "| 产物及配额 | 稳态理论日产 | 共用上游 | 独立供料 | 入选 |", "| --- | ---: | ---: | ---: | --- |"]
    for key, problem, results, winner in summaries:
        layouts = {}
        for mode, (layout, _) in results.items():
            layouts[mode] = f"[{layout.width} × {layout.height}]({key}-{mode}.html)" if layout else "未找到"
        power = f"（{problem.case['power_limit_kw']} kW 上限）" if problem.power_limit is not None else ''
        lines.append(f"| [{problem.target}{power}]({key}.md) | {problem.rate * DAY} | {layouts['shared']} | {layouts['separate']} | {MODE_NAMES.get(winner, '—')} |")
    lines.extend(["", "方案详情含离线 HTML、SVG、格位 JSON、逐口配额和建材。适用 Steam 的参数表同时生成导入文本。启用仿真时也记录空线启动后的产量。", ""])
    if directory.resolve() == Path(__file__).resolve().parents[1] / "blueprints" / "solved":
        lines.extend(["[求解器使用与验证边界](../../docs/solver.md)", ""])
    (directory / "README.md").write_text("\n".join(lines), encoding="utf-8")
