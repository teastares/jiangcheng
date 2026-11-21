"""只读提取 Steam 格式 117 的科技、配方、建筑与节点解锁。"""

from dataclasses import dataclass
from os import fstat
from pathlib import Path
from struct import unpack_from
from time import sleep


class SaveError(ValueError):
    pass


@dataclass(frozen=True)
class SaveProfile:
    version: int
    recipes: frozenset[int]
    buildings: frozenset[int]
    technologies: frozenset[int]
    technology_levels: dict[int, int]
    nodes: frozenset[int]


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def skip(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise SaveError(f"存档数据不完整，偏移 {self.pos}")
        self.pos += size

    def integer(self):
        start = self.pos
        self.skip(4)
        return unpack_from("<i", self.data, start)[0]

    def boolean(self):
        start = self.pos
        self.skip(1)
        value = self.data[start]
        if value not in (0, 1):
            raise SaveError(f"存档标记无效，偏移 {start}")
        return bool(value)

    def count(self, nullable=False, maximum=1_000_000):
        value = self.integer()
        if nullable and value == -1:
            return 0
        if not 0 <= value <= maximum:
            raise SaveError(f"存档记录数量无效，偏移 {self.pos-4}")
        return value

    def string(self):
        self.skip(self.count(maximum=16_000_000))

    def strings(self, nullable=False):
        for _ in range(self.count(nullable)):
            self.string()

    def integers(self):
        return [self.integer() for _ in range(self.count())]

    def pairs(self):
        return {self.integer(): self.integer() for _ in range(self.count())}

    def slots(self, size=None, nullable=False):
        for _ in range(self.count(nullable) if size is None else size):
            occupied = self.integer()
            if occupied not in (0, 1):
                raise SaveError(f"物品格标记无效，偏移 {self.pos-4}")
            if occupied:
                self.skip(8)


BOXES = {1, 2, 4, 6, 7, 8}
MACHINES = {50, 51, 52, 54, 55, 56, 60, 61, 62}
BASE_ONLY = {3, 5, 10, 11, 70, 71, 100, 101, 130, 300, 301, 480, 481, 482, 483, 650, 800, 901, 902}


def skip_building(r):
    kind = r.integer()
    r.skip(9 * 4)
    r.boolean()
    r.boolean()
    r.strings(nullable=True)
    r.strings(nullable=True)
    r.string()
    r.string()
    r.strings(nullable=True)
    if kind >= 1000 or kind in BASE_ONLY:
        return
    if kind in BOXES:
        r.slots()
        r.skip(4)
        r.string()
        r.skip(8)
    elif 20 <= kind <= 25 or 30 <= kind <= 34:
        r.skip(4)
        r.skip(8 * r.count())
        if kind >= 30:
            r.string()
    elif kind in (40, 41, 42, 45, 46):
        r.skip(8 * r.count())
        if kind == 42:
            r.slots(3)
            r.boolean()
        if kind in (45, 46):
            r.skip(4)
    elif kind in MACHINES:
        for _ in range(3):
            r.slots()
        r.skip(16)
    elif kind in (63, 64, 65, 110):
        if kind in (63, 64):
            r.skip(4)
            r.slots()
        else:
            r.slots()
            if kind == 65:
                r.slots()
            r.skip(4)
    elif 199 <= kind <= 203:
        r.slots(nullable=True)
        r.skip(8)
    elif kind in (250, 320):
        r.skip(8)
    elif kind in (260, 261, 450, 451):
        r.string()
        r.skip(4)
    elif kind in (452, 453):
        r.string()
        r.skip(8)
        r.skip(8 * r.count())
    elif kind in (12, 220, 430):
        r.skip(4)
    elif kind in (400, 401, 402):
        r.skip(12)
    elif kind == 410:
        r.string()
        r.slots(1)
    elif kind in (600, 605, 610):
        r.slots(nullable=True)
    elif kind == 900:
        r.slots()
        r.skip(8)
        r.boolean()
        r.skip(8 * r.count())
    else:
        raise SaveError(f"存档含当前读取器未识别的建筑 ID {kind}，偏移 {r.pos}")


def skip_factory(r):
    r.skip(4)
    r.string()
    r.skip(4)
    for _ in range(r.count()):
        r.string()
        r.skip(12)
    for _ in range(r.count()):
        r.string()
        skip_building(r)
    for _ in range(2):
        for _ in range(r.count()):
            r.string()
            r.string()
    r.skip(r.count() * 29)
    for _ in range(2):
        for _ in range(r.count()):
            r.string()
            skip_building(r)
    for _ in range(2):
        for _ in range(r.count()):
            r.string()
            r.string()
    for _ in range(r.count()):
        r.string()
        r.skip(8)
    for _ in range(r.count()):
        r.strings()
        r.strings()
    for _ in range(r.count()):
        r.skip(4)
        r.strings()
        r.strings()
    r.skip(8)


def skip_world(r):
    r.skip(260 * 200 * 4)
    for _ in range(r.count()):
        r.skip(8 * 4 + 2 + 4)
    for _ in range(r.count()):
        r.skip(7 * 4)
        for _ in range(3):
            r.string()
        if r.integer() == 1:
            r.skip(4)
            # 没有建材要求的大地图建筑会把列表长度写为 -1。
            r.skip(8 * r.count(nullable=True))
            r.string()
            r.skip(8)
            r.string()
            r.string()
            r.boolean()
    r.skip((260 * 200 + 3) * 4)


def parse(data):
    r = Reader(data)
    version = r.integer()
    if version != 117:
        raise SaveError("需要 Steam 格式 117 的单个 save_… 存档备份")
    for _ in range(3):
        r.string()
    r.skip(15 * 4)
    r.slots(12)
    r.skip(15 * 4)
    skip_world(r)
    for _ in range(r.count()):
        skip_factory(r)
    r.skip(7 * 4 * r.count())
    r.skip(4)
    r.slots()
    r.slots()
    r.skip(8 + 31 * 4)
    r.skip(4 * r.count())
    r.skip(4)
    r.skip(4 * r.count())
    r.slots(4)
    r.skip(8 * 4)
    r.skip(4 * 16)
    r.skip(4)
    r.skip(12 * r.count())
    r.skip(5 * 4)
    recipes, buildings, technology = (frozenset(r.integers()) for _ in range(3))
    r.skip(4)
    nodes = set()
    for _ in range(r.count()):
        key, node = r.integer(), r.integer()
        if key != node:
            raise SaveError("天机庙节点编号不一致")
        if r.boolean():
            nodes.add(node)
        r.slots()
    r.skip(15 * 4)
    for _ in range(r.count()):
        r.skip(4)
        r.slots()
    r.skip(4 * r.count())
    r.skip(10 * 4)
    for _ in range(r.count()):
        r.skip(4)
        kind = r.integer()
        if kind == 0:
            r.boolean()
        elif kind == 1:
            r.skip(4)
        elif kind == 2:
            r.string()
        else:
            raise SaveError("存档变量类型无效")
    for _ in range(r.count()):
        r.skip(8)
        for _ in range(3):
            r.boolean()
        r.skip(4 * r.count())
    r.skip(8)
    r.skip(4 * r.count())
    r.skip(8 * r.count())
    for _ in range(2):
        r.skip(8)
        r.skip(4 * r.count())
    r.skip(8 * r.count())
    for _ in range(3):
        r.skip(4 * r.count())
    r.skip(8)
    r.skip(4 * r.count())
    r.skip(12)
    r.boolean()
    # 科技等级位于资源管理、网络仓库等记录之后，按格式跳过数据。
    for _ in range(r.count()):
        r.skip(12)
        r.skip(8 * r.count())
        r.skip(4)
    r.skip(4)
    r.slots(nullable=True)
    r.skip(8 * r.count())
    r.skip(8 * r.count())
    r.skip(16)
    r.boolean()
    r.skip(8 * r.count())
    levels = r.pairs()
    if len(data) - r.pos < 5:
        raise SaveError("存档缺少空间站与场景记录")
    return SaveProfile(version, recipes | ({600} if 15 in nodes else set()), buildings | {5}, technology,
                       levels, frozenset(nodes))


def read_save(path: Path):
    limit = 128 * 1024 * 1024
    for attempt in range(3):
        if attempt:
            sleep(0.025)
        with path.open("rb") as stream:
            before = fstat(stream.fileno())
            if before.st_size > limit:
                raise SaveError("存档超过 128 MiB 读取上限")
            data = stream.read(limit + 1)
            after = fstat(stream.fileno())
        if len(data) > limit:
            raise SaveError("存档超过 128 MiB 读取上限")
        # 只解析本次读取的内存快照，若游戏在读取途中改写则重试。
        if len(data) == before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return parse(data)
    raise SaveError("读取期间存档持续变化，请在游戏保存完成后重新选择。")
