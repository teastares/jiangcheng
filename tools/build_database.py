"""从本机 Steam 安装表和已下载的 Wiki 配方页刷新 GUI 数据。"""

import argparse
import csv
import html
import io
import json
import re
from pathlib import Path
from struct import unpack_from


WIKI = "https://wiki.biligame.com/jiangcity/产物配方"


def tables(path):
    data = path.read_bytes()
    end, count = unpack_from("<II", data, 6)
    if data[:6] != b"iFFile":
        raise ValueError("安装包格式不符")
    entries, cursor = {}, 14
    for _ in range(count):
        offset, length, size = unpack_from("<III", data, cursor)
        cursor += 12
        name = data[cursor:cursor + size].decode("utf-8")
        cursor += size
        entries[name] = offset, length
    if cursor != end + 10:
        raise ValueError("安装包目录长度不符")
    result = {}
    for name in ("formula", "build", "item", "science", "node"):
        offset, length = entries[f"Setting\\{name}.csv"]
        content = data[cursor + offset:cursor + offset + length].decode("utf-8-sig")
        result[name] = list(csv.DictReader(io.StringIO(content)))
    return result


def values(text):
    return [int(field) for field in re.split(r"[|$]", text) if field]


def quantities(text):
    parts = values(text)
    return list(zip(parts[::2], parts[1::2], strict=True))


def wiki_rows(path):
    document = path.read_text(encoding="utf-8")
    section = document.split("物品名称", 1)[1].split("</table>", 1)[0]
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section, re.S):
        fields = [html.unescape(re.sub(r"<[^>]*>", "", cell)).strip()
                  for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(fields) == 7:
            yield fields[0], fields[1], fields[2]


def normalize(name):
    return re.sub(r"(?:生产|制造|制作)$", "", name.strip())


def build_database(game, wiki):
    data = tables(game)
    items = {int(row["id"]): {"name": row["name"], "stack": int(row["stack"] or 0)} for row in data["item"]}
    buildings = {int(row["id"]): {"name": row["name"], "power": -int(row["pow"] or 0), "water": int(row["water"] or 0),
                                         "materials": quantities(row["stuff"])} for row in data["build"]}
    formulas = [{"id": int(row["id"]), "name": row["name"], "devices": values(row["build"]),
                 "inputs": quantities(row["input"]), "outputs": quantities(row["output"]), "ticks": int(row["time"] or 0)}
                for row in data["formula"]]
    technologies = {int(row["id"]): {"name": row["name"], "buildings": values(row["builds"]),
                                           "recipes": values(row["formula"])} for row in data["science"]}
    nodes = {int(row["id"]): {"name": row["endString"], "tier": int(row["level"]), "index": int(row["index"])} for row in data["node"]}
    presets = []
    for label, unlock, wiki_devices in wiki_rows(wiki):
        matches = [f for f in formulas if normalize(f["name"]) == normalize(label)]
        if not matches:
            matches = [f for f in formulas if any(normalize(items.get(i, {}).get("name", "")) == normalize(label) for i, _ in f["outputs"])]
        output_ids = sorted({i for f in matches for i, _ in f["outputs"]})
        item_id = output_ids[0] if len(output_ids) == 1 else None
        presets.append({"label": label, "unlock": unlock, "devices": wiki_devices, "item": item_id,
                        "recipes": [f["id"] for f in matches]})
    if not presets:
        raise ValueError("Wiki 页面未解析到任何配方")
    return {"format": 117, "steam_build": "25121460", "wiki": WIKI, "items": items, "buildings": buildings,
            "formulas": formulas, "technologies": technologies, "nodes": nodes, "presets": presets}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("wiki_html", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build_database(args.archive, args.wiki_html)
    # 每个预设和版本表都可离线使用。
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
