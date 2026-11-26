"""使用 Windows 本地存档备份的蓝图生成界面。"""

import json
import os
import queue
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from math import isfinite
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from .geometry import OFFSETS
from .layout import distance
from .model import positive
from .report import MODE_NAMES, OBJECTIVE_NAMES
from .save import SaveError, read_save
from .workshop import BLUEPRINT_NODE, Workshop, configured_external_items, read_database, write_workshop_report


OBJECTIVE_PRESETS = {
    "占地优先": ("area", "buildings", "lines", "cadence", "materials", "power"),
    "稳产优先": ("cadence", "buildings", "lines", "area", "materials", "power"),
    "共用原料优先": ("inlets", "machines", "area", "materials", "power"),
    "少胶囊优先": ("buildings", "materials", "lines", "area", "power"),
    "建材优先": ("materials", "buildings", "area", "lines", "power"),
    "省电优先": ("power", "machines", "area", "materials", "buildings"),
    "输送优先": ("cadence", "transport_time", "lines", "buildings", "area", "materials", "power"),
}
MODE_PRESETS = {"自动比较两种方案": ("shared", "separate"), "只搜索共用上游": ("shared",), "只搜索独立供料": ("separate",)}


def documents_directory():
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            path = Path(os.path.expandvars(value))
            if path.is_absolute():
                return path
        except OSError:
            pass
    return Path.home() / "Documents"


def open_native_directory(folder):
    if os.name != "nt":
        webbrowser.open(folder.as_uri())
        return

    from ctypes import c_void_p, c_wchar_p, windll

    explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
    execute = windll.shell32.ShellExecuteW
    execute.restype = c_void_p
    # 让资源管理器导航到完整路径。只选中文件夹会停在其父目录。
    result = execute(None, c_wchar_p("open"), c_wchar_p(str(explorer)), c_wchar_p(f'"{folder}"'), None, 1)
    if not result or result <= 32:
        raise OSError(f"无法启动 Windows 资源管理器（{result or 0}）")


class BlueprintWindow:
    def __init__(self, root):
        self.root = root
        self.db = read_database()
        self.workshop = None
        self.backup_path = None
        self.report = None
        self.jobs = queue.Queue()
        self.busy = False
        self.current_config = None
        self.output_folder = documents_directory() / "江城蓝图"
        self.presets = {self.db["items"][str(row["item"])]["name"]: row for row in self.db["presets"] if row["item"] is not None}
        self.objective = list(OBJECTIVE_PRESETS["占地优先"])
        self.fields = {key: tk.StringVar(root, value=value) for key, value in {
            "target": "绿色晶能", "machines": "2", "device": "自动选择", "rate": "", "width": "", "height": "", "power": "",
            "mode": "自动比较两种方案", "preset": "占地优先", "belts": "", "technology": "尚未读取存档备份",
            "backup": "未选择存档", "output": str(self.output_folder), "status": "选取存档后，可在右上角开始求解。"}.items()}

        root.title("江城蓝图生成器")
        root.geometry("1160x800")
        root.minsize(960, 720)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#f3f6f0")
        style.configure("TLabel", background="#f3f6f0", foreground="#193b30")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Subtitle.TLabel", foreground="#4f685e")
        style.configure("TLabelframe", background="#f3f6f0", bordercolor="#bdcdc1")
        style.configure("TLabelframe.Label", background="#f3f6f0", foreground="#193b30", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TNotebook.Tab", padding=(18, 8))
        style.configure("Treeview", rowheight=27)
        style.configure("Accent.TButton", background="#247859", foreground="white", padding=8)
        style.map("Accent.TButton", background=[("active", "#196949"), ("disabled", "#9eb3a7")])

        outer = ttk.Frame(root, padding=(22, 16))
        outer.pack(expand=True, fill="both")
        header = ttk.Frame(outer)
        header.pack(fill="x")
        self.generate = ttk.Button(header, text="求解并生成 HTML", style="Accent.TButton", command=self.generate_report)
        self.generate.pack(side="right")
        self.generate.state(["disabled"])
        ttk.Label(header, text="江城蓝图生成器", style="Title.TLabel").pack(side="left")
        ttk.Label(outer, textvariable=self.fields["technology"], style="Subtitle.TLabel").pack(anchor="w", pady=(3, 12))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="选择存档（只读）", command=self.select_backup).pack(side="left")
        ttk.Label(toolbar, textvariable=self.fields["backup"], width=38, anchor="w").pack(side="left", padx=8)
        ttk.Button(toolbar, text="载入配置", command=self.load_configuration).pack(side="right")
        ttk.Button(toolbar, text="保存配置", command=self.save_configuration).pack(side="right", padx=8)
        ttk.Button(toolbar, text="选择输出目录", command=self.select_output).pack(side="right")
        ttk.Label(outer, textvariable=self.fields["output"], style="Subtitle.TLabel").pack(anchor="w")
        ttk.Label(outer, textvariable=self.fields["status"], wraplength=790).pack(anchor="w", pady=(4, 8))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.plan = ttk.Frame(self.tabs, padding=14)
        self.save_view = ttk.Frame(self.tabs, padding=14)
        self.library = ttk.Frame(self.tabs, padding=14)
        self.preview = ttk.Frame(self.tabs, padding=14)
        for tab, text in ((self.plan, "设计目标"), (self.save_view, "存档解锁"), (self.library, "Wiki 产物预设"), (self.preview, "方案预览")):
            self.tabs.add(tab, text=text)
        self.make_plan()
        self.make_save_view()
        self.make_library()
        self.make_preview()

        self.refresh_library()
        self.apply_preset()
        self.refresh_target()
        root.after(100, self.check_jobs)

    def make_plan(self):
        self.plan.columnconfigure((0, 1), weight=1, uniform="plan")
        self.plan.rowconfigure(0, weight=1)
        left = ttk.Labelframe(self.plan, text="产物、工位与地块", padding=13)
        right = ttk.Labelframe(self.plan, text="供料与优化顺序", padding=13)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        left.columnconfigure(1, weight=1)

        def field(row, title, key, options=None):
            ttk.Label(left, text=title).grid(row=row, column=0, sticky="w", pady=5)
            if options is None:
                control = ttk.Entry(left, textvariable=self.fields[key])
            else:
                control = ttk.Combobox(left, values=options, textvariable=self.fields[key], state="readonly")
            control.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=5)
            return control

        target = field(0, "目标产物", "target", list(self.presets))
        target.bind("<<ComboboxSelected>>", lambda _: self.refresh_target(reset_device=True))
        self.devices = field(1, "末端机型", "device", ("自动选择",))
        field(2, "末端台数", "machines")
        field(3, "目标件数/分钟", "rate")
        field(4, "可建净空宽（格）", "width")
        field(5, "可建净空高（格）", "height")
        field(6, "电网可用余量（kW）", "power")
        field(7, "允许传送带等级", "belts")
        ttk.Label(left, text="产速留空按额定满产。宽、高和电力留空各自不限。带级留空使用存档解锁等级。填 1,2,3 可混用，填 3 则只允许三级。",
                  wraplength=370, style="Subtitle.TLabel").grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 4))
        self.target_detail = ttk.Label(left, wraplength=370, justify="left")
        self.target_detail.grid(row=9, column=0, columnspan=2, sticky="w", pady=8)

        ttk.Label(right, text="供料方式").pack(anchor="w")
        ttk.Combobox(right, values=list(MODE_PRESETS), textvariable=self.fields["mode"], state="readonly").pack(fill="x", pady=(2, 8))
        ttk.Label(right, text="多层级目标").pack(anchor="w")
        objective = ttk.Combobox(right, values=[*OBJECTIVE_PRESETS, "自定义顺序"], textvariable=self.fields["preset"], state="readonly")
        objective.pack(fill="x", pady=(2, 6))
        objective.bind("<<ComboboxSelected>>", lambda _: self.apply_preset())
        controls = ttk.Frame(right)
        controls.pack(fill="x")
        self.objective_view = tk.Listbox(controls, height=7, activestyle="none", exportselection=False, bg="#fffefa", fg="#193b30",
                                         font=("Microsoft YaHei UI", 10), relief="flat", selectbackground="#247859")
        self.objective_view.pack(side="left", expand=True, fill="x")
        scrollbar = ttk.Scrollbar(controls, command=self.objective_view.yview)
        scrollbar.pack(side="left", fill="y")
        self.objective_view.configure(yscrollcommand=scrollbar.set)
        buttons = ttk.Frame(controls)
        buttons.pack(side="left", padx=(8, 0), anchor="n")
        ttk.Button(buttons, text="上移", command=lambda: self.move_objective(-1)).pack(fill="x", pady=(0, 5))
        ttk.Button(buttons, text="下移", command=lambda: self.move_objective(1)).pack(fill="x")
        ttk.Label(right, text="指定外供物料（可选）").pack(anchor="w", pady=(12, 2))
        self.external = tk.Text(right, height=3, wrap="none", font=("Microsoft YaHei UI", 10), bg="#fffefa", fg="#193b30", relief="flat")
        self.external.pack(fill="both", expand=True)
        ttk.Label(right, text="原木等基础原料自动外供。需外供的半成品可每行填一个名称。所需数量见 HTML 日配额。",
                  wraplength=390, style="Subtitle.TLabel").pack(anchor="w", pady=(5, 0))

    def table(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=tuple(key for key, _ in columns), show="headings")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        for key, label in columns:
            tree.heading(key, text=label, anchor="w")
            tree.column(key, width=125, anchor="w")
        return tree

    def make_save_view(self):
        self.save_view.columnconfigure((0, 1), weight=1, uniform="save")
        self.save_view.rowconfigure(0, weight=1)
        tech = ttk.Labelframe(self.save_view, text="已学科技与完成节点", padding=8)
        build = ttk.Labelframe(self.save_view, text="已解锁的工坊建筑", padding=8)
        tech.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        build.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        self.technologies = self.table(tech, (("kind", "来源"), ("item", "名称")))
        self.buildings = self.table(build, (("id", "ID"), ("item", "名称"), ("status", "自动规划")))
        self.buildings.column("id", width=45, stretch=False)
        self.buildings.column("status", width=85, stretch=False)
        self.building_detail = ttk.Label(build, text="选择建筑可查看用途和科技来源。", wraplength=445, justify="left")
        self.building_detail.pack(fill="x", pady=(8, 0))
        self.buildings.bind("<<TreeviewSelect>>", self.show_building_detail)

    def show_building_detail(self, _event):
        selected = self.buildings.selection()
        if not selected or not self.workshop:
            self.building_detail.config(text="选择建筑可查看用途和科技来源。")
            return
        ident = int(self.buildings.item(selected[0], "values")[0])
        device = self.device_info.get(ident)
        if device is None:
            self.building_detail.config(text="此 ID 未收录在当前安装版建筑表。")
            return
        technology = "、".join(f"{name}（{tech_id}）" for tech_id, name in device["technologies"])
        source = f"关联科技 {technology}。" if technology else "安装科技表无直接条目。"
        self.building_detail.config(text=f"{device['category']} · {device['function']}\n{source}")

    def make_library(self):
        ttk.Label(self.library, text="双击产物填入设计。状态依据当前存档的配方和末端设备。",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(0, 8))
        self.library_table = self.table(self.library, (("item", "目标产物"), ("unlock", "Wiki 解锁途径"), ("status", "当前存档状态")))
        self.library_table.column("unlock", width=235)
        self.library_table.column("status", width=340)
        self.library_table.bind("<Double-1>", self.library_pick)

    def make_preview(self):
        self.summary = ttk.Label(self.preview, text="生成后在这里预览布局和关键指标。", wraplength=950)
        self.summary.pack(anchor="w", pady=(0, 10))
        self.canvas = tk.Canvas(self.preview, background="#fffefa", highlightthickness=1, highlightbackground="#d2ded3")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.preview_layout = None
        actions = ttk.Frame(self.preview)
        actions.pack(fill="x", pady=(10, 0))
        self.open_report = ttk.Button(actions, text="打开完整 HTML", command=lambda: webbrowser.open(self.report.as_uri()))
        self.open_report.pack(side="left")
        self.open_report.state(["disabled"])
        self.open_folder = ttk.Button(actions, text="打开导出目录", command=self.open_output_directory)
        self.open_folder.pack(side="left", padx=8)
        self.open_folder.state(["disabled"])
        ttk.Label(actions, text="在完整 HTML 中切换整图方向。", style="Subtitle.TLabel").pack(side="left")

    def refresh_target(self, reset_device=False):
        item = self.fields["target"].get()
        preset = self.presets[item]
        self.device_ids = self.workshop.devices_for(item) if self.workshop else {}
        self.devices.configure(values=("自动选择", *self.device_ids))
        if reset_device or self.fields["device"].get() not in self.device_ids:
            self.fields["device"].set("自动选择")
        available = "、".join(self.device_ids) if self.device_ids else "待核对" if self.workshop is None else "暂无"
        self.target_detail.configure(text=f"Wiki 解锁　{preset['unlock']}\n当前可用末端　{available}")

    def refresh_library(self):
        self.library_table.delete(*self.library_table.get_children())
        for item, preset in self.presets.items():
            status = "等待读取存档"
            if self.workshop:
                devices = self.workshop.devices_for(item)
                status = "、".join(devices) if devices else self.workshop.unavailable(item)
            self.library_table.insert("", "end", iid=item, values=(item, preset["unlock"], status))

    def library_pick(self, event):
        item = self.library_table.identify_row(event.y)
        if item:
            self.fields["target"].set(item)
            self.refresh_target(reset_device=True)
            self.tabs.select(self.plan)

    def refresh_objective(self):
        self.objective_view.delete(0, "end")
        for index, key in enumerate(self.objective, 1):
            self.objective_view.insert("end", f"{index}　{OBJECTIVE_NAMES[key]}")

    def apply_preset(self):
        selected = self.fields["preset"].get()
        if selected in OBJECTIVE_PRESETS:
            primary = OBJECTIVE_PRESETS[selected]
            self.objective = [*primary, *(key for key in OBJECTIVE_NAMES if key not in primary)]
            self.refresh_objective()

    def move_objective(self, delta):
        selected = self.objective_view.curselection()
        if selected and 0 <= selected[0] + delta < len(self.objective):
            a, b = selected[0], selected[0] + delta
            self.objective[a], self.objective[b] = self.objective[b], self.objective[a]
            self.fields["preset"].set("自定义顺序")
            self.refresh_objective()
            self.objective_view.selection_set(b)

    def select_backup(self):
        name = filedialog.askopenfilename(parent=self.root, title="选择 Steam 存档（只读）",
                                         initialdir=str(self.backup_path.parent) if self.backup_path else None,
                                         filetypes=[("Steam 存档", "save_*"), ("全部文件", "*.*")])
        if name:
            try:
                self.load_backup(Path(name))
            except (OSError, SaveError, ValueError) as error:
                messagebox.showerror("无法读取存档", str(error), parent=self.root)

    def load_backup(self, path):
        resolved = path.resolve()
        profile = read_save(resolved)
        self.backup_path = resolved
        self.workshop = Workshop(profile, self.db)
        self.device_info = self.workshop.device_capabilities()
        self.fields["backup"].set(resolved.name)
        belts = [str(i) for i in range(1, 6) if i + 19 in profile.buildings]
        blueprint = "蓝图已解锁" if BLUEPRINT_NODE in profile.nodes else "蓝图未解锁"
        belt_text = "、".join(belts) if belts else "暂无"
        self.fields["technology"].set(f"Steam {profile.version}　·　{blueprint}　·　已解锁地面带等级 {belt_text}")
        for view in (self.technologies, self.buildings):
            view.delete(*view.get_children())
        for ident in sorted(profile.technologies | profile.technology_levels.keys()):
            tech = self.db["technologies"].get(str(ident), {}).get("name", f"科技 {ident}")
            level = profile.technology_levels.get(ident, 0)
            self.technologies.insert("", "end", values=(f"科技 {ident}", f"{tech} Lv.{level}" if level else tech))
        for ident in sorted(profile.nodes):
            node = self.db["nodes"].get(str(ident), {"name": f"节点 {ident}", "tier": "?", "index": "?"})
            self.technologies.insert("", "end", values=(f"T{node['tier']}-{node['index']}", node["name"]))
        for ident in sorted(profile.buildings):
            status = self.device_info.get(ident, {}).get("status", "待核对")
            self.buildings.insert("", "end", values=(ident, self.workshop.building_name(ident), status))
        self.apply_loaded_device()
        self.refresh_library()
        self.fields["status"].set("已只读加载存档的解锁进度。可在右上角开始求解。")
        if not self.busy:
            self.generate.state(["!disabled"])

    def apply_loaded_device(self):
        self.refresh_target()
        if not self.current_config or self.current_config["target"] != self.fields["target"].get() or not self.workshop:
            return
        requested = self.current_config.get("terminal")
        if requested is None:
            return
        name = next((name for name, ident in self.device_ids.items() if ident == requested), None)
        if name is None:
            name = f"{self.workshop.building_name(requested)}（当前不可用）"
            self.device_ids[name] = requested
            self.devices.configure(values=("自动选择", *self.device_ids))
        self.fields["device"].set(name)

    def select_output(self):
        initial = next((path for path in (self.output_folder, *self.output_folder.parents) if path.is_dir()), Path.home())
        folder = filedialog.askdirectory(parent=self.root, title="选择蓝图交付目录", initialdir=str(initial), mustexist=True)
        if folder:
            self.output_folder = Path(folder).resolve()
            self.fields["output"].set(str(self.output_folder))

    def open_output_directory(self):
        directory = self.report.parent if self.report is not None else self.output_folder
        try:
            folder = directory.resolve(strict=True)
            if not folder.is_dir():
                raise NotADirectoryError(str(directory))
            open_native_directory(folder)
        except OSError:
            messagebox.showerror("无法打开导出目录", f"请检查目录是否存在并可访问。\n{directory}", parent=self.root)

    def config(self):
        def get(key):
            return self.fields[key].get().strip()

        def grid_count(value, label):
            try:
                number = int(value)
            except ValueError as error:
                raise ValueError(f"{label}应填正整数") from error
            if number < 1:
                raise ValueError(f"{label}应填正整数")
            return number

        count = grid_count(get("machines"), "末端台数")
        config = {"target": get("target"), "machines": count,
                  "modes": MODE_PRESETS[get("mode")], "objective": list(self.objective)}
        external = [name for line in self.external.get("1.0", "end").splitlines() if (name := line.strip())]
        if external:
            config["external"] = list(dict.fromkeys(external))
        plot = [grid_count(value, label) if value else None
                for value, label in ((get("width"), "可建净空宽"), (get("height"), "可建净空高"))]
        if any(limit is not None for limit in plot):
            config["plot"] = plot
        if power := get("power"):
            try:
                budget = float(power)
            except ValueError as error:
                raise ValueError("电网可用余量应填非负的有限数字") from error
            if not isfinite(budget) or budget < 0:
                raise ValueError("电网可用余量应填非负的有限数字")
            config["power_limit_kw"] = budget
        if rate := get("rate"):
            positive(rate, "目标件数/分钟")
            config["rate"] = rate
        if belts := get("belts"):
            try:
                tiers = sorted({int(value.strip()) for value in belts.replace("，", ",").split(",")})
            except ValueError as error:
                raise ValueError("允许带级用逗号分隔，如 1,2") from error
            if not tiers or tiers[0] < 1 or tiers[-1] > 5:
                raise ValueError("传送带等级的范围为 1 至 5")
            config["belt_levels"] = tiers
        if get("device") in self.device_ids:
            config["terminal"] = self.device_ids[get("device")]
        elif self.workshop is None and self.current_config and self.current_config.get("target") == get("target") and "terminal" in self.current_config:
            config["terminal"] = self.current_config["terminal"]
        return config

    def set_configuration(self, config):
        item = config["target"]
        objectives = list(config.get("objective", OBJECTIVE_PRESETS["占地优先"]))
        if item not in self.presets or not objectives or len(set(objectives)) != len(objectives) or not set(objectives) <= set(OBJECTIVE_NAMES):
            raise ValueError("配置的产物或优化指标无效")
        modes = next((name for name, keys in MODE_PRESETS.items() if tuple(config.get("modes", ("shared", "separate"))) == keys), None)
        if modes is None:
            raise ValueError("配置的供料方式无效")
        plot = config.get("plot")
        width, height = plot if plot is not None else (None, None)
        external = configured_external_items(config)
        self.current_config = config
        for key, value in (("target", item), ("machines", config["machines"]), ("rate", config.get("rate")),
                           ("width", width), ("height", height), ("power", config.get("power_limit_kw")), ("mode", modes), ("device", "自动选择"),
                           ("belts", ",".join(str(i) for i in (config.get("belt_levels") or ())))):
            self.fields[key].set("" if value is None else str(value))
        self.external.delete("1.0", "end")
        self.external.insert("1.0", "\n".join(external))
        self.objective = [*objectives, *(key for key in OBJECTIVE_NAMES if key not in objectives)]
        self.fields["preset"].set("自定义顺序")
        self.refresh_objective()
        self.apply_loaded_device()

    def load_configuration(self):
        path = filedialog.askopenfilename(title="载入求解配置", filetypes=[("JSON 配置", "*.json")])
        if path:
            try:
                self.set_configuration(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            except (OSError, ValueError, TypeError, KeyError) as error:
                messagebox.showerror("无法载入配置", str(error), parent=self.root)

    def save_configuration(self):
        try:
            config = self.config()
            path = filedialog.asksaveasfilename(title="保存求解配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json")])
            if path:
                destination = Path(path).resolve()
                if destination == self.backup_path or destination.suffix.casefold() != ".json" or destination.name.casefold().startswith("save_"):
                    raise ValueError("配置请保存为独立的 .json 文件，名称避开存档文件前缀")
                if destination.exists():
                    try:
                        previous = json.loads(destination.read_text(encoding="utf-8-sig"))
                        if (not isinstance(previous, dict) or not isinstance(previous.get("target"), str) or
                                previous["target"] not in self.presets or type(previous.get("machines")) is not int):
                            raise ValueError
                    except (ValueError, UnicodeError) as error:
                        raise ValueError("请选择新文件名或已有的求解配置文件") from error
                destination.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as error:
            messagebox.showerror("无法保存配置", str(error), parent=self.root)

    def generate_report(self):
        if self.busy or self.workshop is None:
            return
        try:
            config = self.config()
        except ValueError as error:
            messagebox.showerror("请核对参数", str(error), parent=self.root)
            return
        self.busy = True
        self.generate.state(["disabled"])
        self.fields["status"].set("正在配平、搜索格位并校验解锁与仿真产量……")
        workshop, folder = self.workshop, self.output_folder

        def work():
            try:
                problem, results, winner = workshop.run(config)
                directory = (folder / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6])).resolve()
                report = write_workshop_report(directory, problem, results, winner)
                self.jobs.put(("ready", (problem, results, winner, report)))
            except Exception as error:
                self.jobs.put(("failed", str(error)))

        threading.Thread(target=work, daemon=True).start()

    def check_jobs(self):
        try:
            status, data = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.generate.state(["!disabled"])
            if status == "failed":
                self.fields["status"].set("本轮未生成蓝图。请查看解锁状态和设计限制。")
                messagebox.showerror("未找到符合条件的方案", data, parent=self.root)
            else:
                self.show_result(*data)
        self.root.after(100, self.check_jobs)

    def show_result(self, problem, results, winner, report):
        layout = results[winner][0]
        self.report = report
        self.preview_layout = layout
        stats = layout.statistics
        self.summary.configure(text=f"{problem.target}　·　{MODE_NAMES[winner]}　·　{layout.width} × {layout.height} 格　·　"
                                     f"{stats['power']} kW　·　{problem.rate} 件/分钟　·　稳态理论日产 {problem.rate*16}")
        self.open_report.state(["!disabled"])
        self.open_folder.state(["!disabled"])
        self.fields["status"].set(f"已导出完整 HTML、导入 TXT 和独立 SVG。{report.parent.name}")
        self.tabs.select(self.preview)
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        layout = self.preview_layout
        if layout is None:
            return
        size = min((self.canvas.winfo_width() - 36) / layout.width, (self.canvas.winfo_height() - 36) / layout.height)
        if size < 1:
            return
        offset = 18
        for x in range(layout.width + 1):
            self.canvas.create_line(offset+x*size, offset, offset+x*size, offset+layout.height*size, fill="#dce4d9")
        for y in range(layout.height + 1):
            self.canvas.create_line(offset, offset+y*size, offset+layout.width*size, offset+y*size, fill="#dce4d9")

        def center(point):
            return offset+(point[0]+.5)*size, offset+(point[1]+.5)*size

        for s in layout.segments:
            underground = distance(s.start, s.end) > 1
            self.canvas.create_line(*center(s.start), *center(s.end), fill="#498472", dash=(3, 3) if underground else (),
                                    arrow="last" if size > 12 else "none", arrowshape=(4, 5, 2))
        for p in layout.placements:
            x, y = offset+p.x*size, offset+p.y*size
            fill = "#f8ead0" if p.building == "inlet" else "#e1eee3"
            self.canvas.create_rectangle(x+1, y+1, x+p.width*size-1, y+p.height*size-1, fill=fill, outline="#4e7867")
            if size > 10:
                dx, dy = OFFSETS[p.direction]
                tx = x + p.width * size / 2 + dx * (p.width * size / 2 - 3)
                ty = y + p.height * size / 2 + dy * (p.height * size / 2 - 3)
                self.canvas.create_polygon(tx, ty, tx - 4*dx + 2*dy, ty - 4*dy - 2*dx,
                                           tx - 4*dx - 2*dy, ty - 4*dy + 2*dx, fill="#4e7867")
            if size >= 13:
                label = "出" if p.building == "outlet" else p.item
                self.canvas.create_text(x+p.width*size/2, y+p.height*size/2, text=label, fill="#16392c", font=("Microsoft YaHei UI", 9))


def main():
    root = tk.Tk()
    BlueprintWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
