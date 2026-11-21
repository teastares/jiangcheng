import argparse
import json
import re
import sys
from pathlib import Path

from .layout import solve
from .model import Infeasible, Problem
from .report import MODE_NAMES, write_case, write_index
from .simulate import simulate


def main():
    package = Path(__file__).parent
    parser = argparse.ArgumentParser(description="按固定末端机器数求解江城创业记产线并生成摆放图。")
    parser.add_argument("cases", nargs="*", help="算例编号。省略时处理算例文件中的全部条目。")
    parser.add_argument("--catalog", type=Path, default=package / "catalog.json", help="配方和机型参数 JSON")
    parser.add_argument("--cases-file", type=Path, default=package / "examples.json", help="算例 JSON")
    parser.add_argument("--out", type=Path, default=Path("blueprints/solved"), help="结果目录")
    parser.add_argument("--row-time-limit", type=float, default=0.15, help="HiGHS 每个行位子问题的秒数，0 使用确定性构造搜索")
    parser.add_argument("--no-simulation", action="store_true", help="只执行静态配平和格位校验")
    args = parser.parse_args()
    if not 0 <= args.row_time_limit < float("inf"):
        parser.error("--row-time-limit 应为有限非负数")
    try:
        files = (args.catalog, args.cases_file)
        catalog, cases = (json.loads(path.read_text(encoding="utf-8-sig")) for path in files)
        selected = args.cases or list(cases)
        summaries = []
        failures = 0
        for key in selected:
            if key not in cases or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", key):
                parser.error(f"未知或非法算例编号：{key}")
            try:
                problem = Problem(catalog, cases[key])
                print(f"{key}　{problem.target}　求解中", flush=True)
                results = solve(problem, args.row_time_limit, None if args.no_simulation else simulate)
                winner = write_case(args.out, key, problem, results)
                summaries.append((key, problem, results, winner))
                if winner is None:
                    print(f"{key}　当前搜索未找到满足限制的布局", file=sys.stderr)
                    failures += 1
                else:
                    layout, _ = results[winner]
                    print(f"{key}　{MODE_NAMES[winner]} {layout.width} × {layout.height} 格　{args.out / (key + '.md')}", flush=True)
            except Infeasible as error:
                print(f"{key}　{error}", file=sys.stderr)
                failures += 1
        if not args.cases and summaries:
            write_index(args.out, summaries)
        return int(bool(failures))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"无法求解：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
